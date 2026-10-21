"""Position Manager: TP1 Scale-Out, Break-Even Locking, and 300ms Watchdog."""

import time
import logging
from typing import List, Dict, Any, Optional
from jev.bybit import BybitClient
from jev.contracts import ActivePosition, TradeDirection, TradeStatus
from jev.decisions import DecisionsEngine
from jev.store import StateStore

logger = logging.getLogger("jevtrader.positions")

class PositionManager:
    def __init__(self, client: BybitClient, decisions: DecisionsEngine, 
                 store: StateStore, dry_run: bool = False, exit_mode: str = "two_tier",
                 on_pnl_realized=None):
        self.client = client
        self.decisions = decisions
        self.store = store
        self.dry_run = dry_run
        self.exit_mode = exit_mode
        self.on_pnl_realized = on_pnl_realized
        self.active_positions: Dict[str, ActivePosition] = {}

    def rehydrate_from_store(self):
        """Loads open/active positions from SQLite into memory on engine startup."""
        open_trades = self.store.get_open_trades()
        for t in open_trades:
            tid = t["trade_id"]
            direction = TradeDirection(t["direction"])
            is_tp1_filled = bool(t.get("is_tp1_filled", 0))
            is_be_locked = bool(t.get("is_be_locked", 0))
            initial_qty = float(t["qty_entry"])
            remaining_qty = initial_qty * 0.5 if is_tp1_filled else initial_qty

            pos = ActivePosition(
                trade_id=tid,
                symbol=t["symbol"],
                direction=direction,
                entry_price=float(t["entry_price"]),
                current_price=float(t["entry_price"]),
                initial_qty=initial_qty,
                remaining_qty=remaining_qty,
                stop_loss_price=float(t["stop_loss"]),
                tp1_price=float(t["tp1"]),
                tp2_price=float(t["tp2"]),
                initial_risk_usd=float(t["initial_risk_usd"]),
                is_tp1_filled=is_tp1_filled,
                is_be_locked=is_be_locked,
                status=TradeStatus(t["status"])
            )
            self.active_positions[tid] = pos
        if self.active_positions:
            logger.info(f"Rehydrated {len(self.active_positions)} active position(s) from SQLite store.")

    def add_position(self, pos: ActivePosition):
        self.active_positions[pos.trade_id] = pos
        logger.info(f"Tracking new position: {pos.direction.value} {pos.symbol} (ID: {pos.trade_id})")

    def update_and_watch(self, tickers_dict: Dict[str, Dict[str, Any]]) -> List[str]:
        """
        Runs every cycle to monitor active positions:
        1. Checks TP1 trigger (+1.5R) -> Closes 50% and locks SL to Break-Even.
        2. Checks TP2 trigger (+2.8R) -> Closes runner for full profit.
        3. Calls 300ms Watchdog (pplx-decider-flash) -> Exits early if wall breaks (-0.2R Ninja Cut).
        """
        closed_trade_ids = []

        for trade_id, pos in list(self.active_positions.items()):
            ticker = tickers_dict.get(pos.symbol)
            if not ticker:
                continue

            current_price = float(ticker.get("lastPrice", pos.entry_price))
            pos.current_price = current_price

            is_long = pos.direction == TradeDirection.LONG

            # A. Check TP1 (Scale-Out 50% and Lock Break-Even)
            if not pos.is_tp1_filled:
                hit_tp1 = (current_price >= pos.tp1_price) if is_long else (current_price <= pos.tp1_price)
                if hit_tp1:
                    logger.info(f"[TP1-HIT] {pos.symbol} reached {current_price} (Target: {pos.tp1_price})! Scaling out 50%...")
                    half_qty = pos.remaining_qty * 0.5
                    close_side = "Sell" if is_long else "Buy"

                    scaleout_success = True
                    if not self.dry_run:
                        try:
                            self.client.place_order(
                                symbol=pos.symbol,
                                side=close_side,
                                order_type="Market",
                                qty=half_qty,
                                reduce_only=True
                            )
                        except Exception as e:
                            logger.error(f"Error executing TP1 partial close for {pos.symbol}: {e}")
                            scaleout_success = False

                    if scaleout_success:
                        be_price = pos.entry_price * (1.0005 if is_long else 0.9995)
                        be_success = True
                        if not self.dry_run:
                            try:
                                self.client.set_trading_stop(symbol=pos.symbol, stop_loss=be_price)
                            except Exception as e:
                                logger.error(f"Error moving SL to BE for {pos.symbol}: {e}")
                                be_success = False

                        if be_success or self.dry_run:
                            logger.info(f"[BREAK-EVEN-LOCKED] Moving SL for {pos.symbol} to {be_price:.6f}. Trade is now 100% FREE!")
                            pos.remaining_qty -= half_qty
                            pos.is_tp1_filled = True
                            pos.is_be_locked = True
                            pos.stop_loss_price = be_price
                            pos.status = TradeStatus.TP1_HIT_BE_LOCKED
                            self.store.update_trade_be(pos.trade_id)

                            if self.on_pnl_realized:
                                pnl = half_qty * abs(pos.tp1_price - pos.entry_price) - (half_qty * pos.tp1_price * 0.0006)
                                self.on_pnl_realized(pos.trade_id, pnl, "TP1")
                            continue

            # B. Check TP2 (Runner Target)
            hit_tp2 = (current_price >= pos.tp2_price) if is_long else (current_price <= pos.tp2_price)
            if hit_tp2:
                logger.info(f"[TP2-HIT / FULL-RUNNER] {pos.symbol} reached {current_price}! Closing remaining runner...")
                close_side = "Sell" if is_long else "Buy"
                tp2_success = True
                if not self.dry_run:
                    try:
                        self.client.place_order(
                            symbol=pos.symbol,
                            side=close_side,
                            order_type="Market",
                            qty=pos.remaining_qty,
                            reduce_only=True
                        )
                    except Exception as e:
                        err_str = str(e).lower()
                        if "110017" in str(e) or "zero" in err_str or "not modified" in err_str or "order qty" in err_str:
                            logger.info(f"Position for {pos.symbol} already closed on Bybit exchange: {e}")
                            tp2_success = True
                        else:
                            logger.error(f"Error closing TP2 runner for {pos.symbol}: {e}")
                            tp2_success = False


                if tp2_success or self.dry_run:
                    pos.status = TradeStatus.CLOSED_TP2
                    self.store.close_trade(pos.trade_id, "CLOSED_TP2")
                    if self.on_pnl_realized:
                        pnl = pos.remaining_qty * abs(pos.tp2_price - pos.entry_price) - (pos.remaining_qty * pos.tp2_price * 0.0006)
                        self.on_pnl_realized(pos.trade_id, pnl, "CLOSED_TP2")
                    closed_trade_ids.append(trade_id)
                    continue

            # C. Check Stop-Loss / Break-Even Hit
            hit_sl = (current_price <= pos.stop_loss_price) if is_long else (current_price >= pos.stop_loss_price)
            if hit_sl:
                status = "CLOSED_BE" if pos.is_be_locked else "CLOSED_SL"
                logger.info(f"[{status}] {pos.symbol} touched SL level {pos.stop_loss_price}. Position closed.")
                pos.status = TradeStatus.CLOSED_BE if pos.is_be_locked else TradeStatus.CLOSED_SL
                self.store.close_trade(pos.trade_id, status)
                self.store.set_cooldown(pos.symbol, pos.direction.value, 600, f"Hit {status}")

                if self.on_pnl_realized:
                    if pos.is_be_locked:
                        pnl = 0.0
                    else:
                        pnl = -pos.initial_risk_usd
                    self.on_pnl_realized(pos.trade_id, pnl, status)

                closed_trade_ids.append(trade_id)
                continue

            # D. 24/7 Watchdog Early Invalidation (Ninja Cut)
            try:
                book = self.client.get_orderbook(pos.symbol, limit=20)
                bids = [[float(p), float(q)] for p, q in book.get("b", [])]
                asks = [[float(p), float(q)] for p, q in book.get("a", [])]
                if bids and asks:
                    best_bid = bids[0][0]
                    best_ask = asks[0][0]
                    tot_b = sum(p * q for p, q in bids)
                    tot_a = sum(p * q for p, q in asks)
                    cur_imb = (tot_b - tot_a) / (tot_b + tot_a) if (tot_b + tot_a) > 0 else 0.0

                    wall_dist = (best_bid - bids[-1][0]) / best_bid * 100 if is_long else (asks[-1][0] - best_ask) / best_ask * 100

                    should_ninja_cut, conf = self.decisions.evaluate_invalidation(pos, current_price, cur_imb, wall_dist)
                    if should_ninja_cut:
                        logger.warning(f"[NINJA-CUT-TRIGGERED] Watchdog detected wall collapse on {pos.symbol} (Conf: {conf:.2f})! Early exit at -0.2R!")
                        close_side = "Sell" if is_long else "Buy"
                        cut_success = True
                        if not self.dry_run:
                            try:
                                self.client.place_order(
                                    symbol=pos.symbol,
                                    side=close_side,
                                    order_type="Market",
                                    qty=pos.remaining_qty,
                                    reduce_only=True
                                )
                            except Exception as e:
                                logger.error(f"Error executing ninja cut for {pos.symbol}: {e}")
                                cut_success = False

                        if cut_success or self.dry_run:
                            pos.status = TradeStatus.CLOSED_WATCHDOG
                            self.store.close_trade(pos.trade_id, "CLOSED_WATCHDOG")
                            self.store.set_cooldown(pos.symbol, pos.direction.value, 900, "Ninja Cut Invalidation")
                            if self.on_pnl_realized:
                                pnl = -0.2 * pos.initial_risk_usd
                                self.on_pnl_realized(pos.trade_id, pnl, "CLOSED_WATCHDOG")
                            closed_trade_ids.append(trade_id)
                            continue
            except Exception as e:
                logger.debug(f"Watchdog tick skip for {pos.symbol}: {e}")

        for tid in closed_trade_ids:
            if tid in self.active_positions:
                del self.active_positions[tid]

        return closed_trade_ids

