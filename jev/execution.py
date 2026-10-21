"""Execution Engine with Post-Only Maker and Native Exchange Protection."""

import time
import uuid
import logging
from typing import Dict, Any, Optional
from jev.bybit import BybitClient
from jev.contracts import TradePlan, TradeDirection, ActivePosition, TradeStatus
from jev.store import StateStore

logger = logging.getLogger("jevtrader.execution")

class ExecutionEngine:
    def __init__(self, client: BybitClient, store: StateStore, dry_run: bool = False):
        self.client = client
        self.store = store
        self.dry_run = dry_run
        self.instrument_cache: Dict[str, Dict[str, Any]] = {}

    def _get_lot_filters(self, symbol: str) -> Dict[str, Any]:
        if symbol not in self.instrument_cache:
            info_list = self.client.get_instruments_info(symbol)
            if info_list:
                info = info_list[0]
                lot_filter = info.get("lotSizeFilter", {})
                price_filter = info.get("priceFilter", {})
                self.instrument_cache[symbol] = {
                    "qtyStep": float(lot_filter.get("qtyStep", "1.0")),
                    "minOrderQty": float(lot_filter.get("minOrderQty", "1.0")),
                    "minNotionalValue": float(lot_filter.get("minNotionalValue", "5.0")),
                    "tickSize": float(price_filter.get("tickSize", "0.01"))
                }
            else:
                self.instrument_cache[symbol] = {
                    "qtyStep": 1.0,
                    "minOrderQty": 1.0,
                    "minNotionalValue": 5.0,
                    "tickSize": 0.01
                }
        return self.instrument_cache[symbol]

    def _round_step(self, val: float, step: float) -> float:
        from decimal import Decimal, ROUND_HALF_UP
        if step <= 0:
            return val
        d_val = Decimal(str(val))
        d_step = Decimal(str(step))
        rounded = (d_val / d_step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * d_step
        return float(rounded)

    def execute_entry(self, plan: TradePlan) -> Optional[ActivePosition]:
        """
        Executes Maker/Taker entry order with native exchange Hard SL & TP2.
        Saves intent in SQLite store before sending.
        """
        symbol = plan.symbol
        side = "Buy" if plan.direction == TradeDirection.LONG else "Sell"
        filters = self._get_lot_filters(symbol)

        # 1. Round quantity to exchange step
        adj_qty = self._round_step(plan.quantity, filters["qtyStep"])
        adj_qty = max(adj_qty, filters["minOrderQty"])
        notional = adj_qty * plan.entry_price

        if notional < filters["minNotionalValue"]:
            logger.warning(f"Position notional ${notional:.2f} is below exchange min ${filters['minNotionalValue']}. Skipping.")
            return None

        # 2. Round SL and TP prices to tick size
        adj_sl = self._round_step(plan.stop_loss_price, filters["tickSize"])
        adj_tp2 = self._round_step(plan.tp2_price, filters["tickSize"])

        trade_id = f"tr_{symbol}_{int(time.time()*1000)}"
        order_link_id = f"jev_{trade_id[:28]}"

        # 3. Log intent in SQLite store
        self.store.create_trade(
            trade_id=trade_id,
            symbol=symbol,
            direction=plan.direction.value,
            entry_price=plan.entry_price,
            stop_loss=adj_sl,
            tp1=plan.tp1_price,
            tp2=adj_tp2,
            qty=adj_qty,
            initial_risk_usd=plan.risk_usd
        )

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulating {side} {adj_qty} {symbol} @ {plan.entry_price} | SL: {adj_sl} | TP1: {plan.tp1_price} | TP2: {adj_tp2}")
            return ActivePosition(
                trade_id=trade_id,
                symbol=symbol,
                direction=plan.direction,
                entry_price=plan.entry_price,
                current_price=plan.entry_price,
                initial_qty=adj_qty,
                remaining_qty=adj_qty,
                stop_loss_price=adj_sl,
                tp1_price=plan.tp1_price,
                tp2_price=adj_tp2,
                initial_risk_usd=plan.risk_usd,
                order_link_id=order_link_id,
                status=TradeStatus.OPEN
            )

        # 4. LIVE Order Execution on Bybit
        try:
            logger.info(f"[LIVE] Sending {side} order for {adj_qty} {symbol} @ Market with attached SL {adj_sl} & TP2 {adj_tp2}...")
            res = self.client.place_order(
                symbol=symbol,
                side=side,
                order_type="Market", # Market order ensures instant action on confirmed setup
                qty=adj_qty,
                stop_loss=adj_sl,
                take_profit=adj_tp2,
                order_link_id=order_link_id
            )
            logger.info(f"Order placed successfully: {res}")
            return ActivePosition(
                trade_id=trade_id,
                symbol=symbol,
                direction=plan.direction,
                entry_price=plan.entry_price,
                current_price=plan.entry_price,
                initial_qty=adj_qty,
                remaining_qty=adj_qty,
                stop_loss_price=adj_sl,
                tp1_price=plan.tp1_price,
                tp2_price=adj_tp2,
                initial_risk_usd=plan.risk_usd,
                order_link_id=order_link_id,
                status=TradeStatus.OPEN
            )
        except Exception as e:
            logger.error(f"Failed to place live order for {symbol}: {e}")
            self.store.close_trade(trade_id, "FAILED_ENTRY")
            return None
