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
                 store: StateStore, dry_run: bool = False, exit_mode: str = "two_tier"):
        self.client = client
        self.decisions = decisions
        self.store = store
        self.dry_run = dry_run
        self.exit_mode = exit_mode
        self.active_positions: Dict[str, ActivePosition] = {}

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

                    # Execute 50% scale-out
                    close_side = "Sell" if is_long else "Buy"
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

                    # Ratchet Stop-Loss to Break-Even (Entry price + 0.05% fee cushion)
                    be_price = pos.entry_price * (1.0005 if is_long else 0.9995)
                    logger.info(f"[BREAK-EVEN-LOCKED] Moving SL for {pos.symbol} to {be_price:.6f}. Trade is now 100% FREE!")
                    if not self.dry_run:
                        try:
                            self.client.set_trading_stop(symbol=pos.symbol, stop_loss=be_price)
                        except Exception as e:
                            logger.error(f"Error moving SL to BE for {pos.symbol}: {e}")

                    pos.remaining_qty -= half_qty
                    pos.is_tp1_filled = True
                    pos.is_be_locked = True
                    pos.stop_loss_price = be_price
                    pos.status = TradeStatus.TP1_HIT_BE_LOCKED
                    self.store.update_trade_be(pos.trade_id)
                    continue

            # B. Check TP2 (Runner Target)
            hit_tp2 = (current_price >= pos.tp2_price) if is_long else (current_price <= pos.tp2_price)
            if hit_tp2:
                logger.info(f"[TP2-HIT / FULL-RUNNER] {pos.symbol} reached {current_price}! Closing remaining runner...")
                close_side = "Sell" if is_long else "Buy"
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
                        logger.error(f"Error closing TP2 runner for {pos.symbol}: {e}")

                pos.status = TradeStatus.CLOSED_TP2
                self.store.close_trade(pos.trade_id, "CLOSED_TP2")
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
                closed_trade_ids.append(trade_id)
                continue

            # D. 24/7 Watchdog Early Invalidation (Ninja Cut)
            # Fetch lightweight orderbook to inspect current wall
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

                        pos.status = TradeStatus.CLOSED_WATCHDOG
                        self.store.close_trade(pos.trade_id, "CLOSED_WATCHDOG")
                        self.store.set_cooldown(pos.symbol, pos.direction.value, 900, "Ninja Cut Invalidation")
                        closed_trade_ids.append(trade_id)
                        continue
            except Exception as e:
                logger.debug(f"Watchdog tick skip for {pos.symbol}: {e}")

        for tid in closed_trade_ids:
            del self.active_positions[tid]

        return closed_trade_ids
