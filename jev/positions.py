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

            entry_p = float(t["entry_price"])
            sl_p = float(t["stop_loss"])
            tp1_p = float(t["tp1"])

            # Infer ratchet level from stop loss distance if BE locked
            ratchet_lvl = 0
            if is_be_locked:
                one_r = abs(tp1_p - entry_p) / 1.5
                if one_r > 0:
                    locked_r = (sl_p - entry_p) / one_r if direction == TradeDirection.LONG else (entry_p - sl_p) / one_r
                    if locked_r >= 0.8:
                        ratchet_lvl = max(1, int(round(locked_r)))

            try:
                status_enum = TradeStatus(t["status"])
            except (ValueError, KeyError):
                status_enum = TradeStatus.TP1_HIT_BE_LOCKED if is_be_locked else TradeStatus.OPEN

            pos = ActivePosition(
                trade_id=tid,
                symbol=t["symbol"],
                direction=direction,
                entry_price=entry_p,
                current_price=entry_p,
                initial_qty=initial_qty,
                remaining_qty=remaining_qty,
                stop_loss_price=sl_p,
                tp1_price=tp1_p,
                tp2_price=float(t["tp2"]),
                initial_risk_usd=float(t["initial_risk_usd"]),
                is_tp1_filled=is_tp1_filled,
                is_be_locked=is_be_locked,
                ratchet_level=ratchet_lvl,
                status=status_enum
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
        2. Ratchet Trailing -> After TP1, dynamically ratchets SL to +1.0R at +2.0R, +2.0R at +3.0R, etc.
        3. Checks TP2 trigger (Runner Target) -> Closes remaining runner.
        4. Checks Stop-Loss / Break-Even / Ratchet Hit -> Exits cleanly.
        5. Calls 300ms Watchdog (liquid/d1-ultra) -> Exits early if wall breaks (-0.2R Ninja Cut).
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
                            pos.ratchet_level = 0
                            pos.status = TradeStatus.TP1_HIT_BE_LOCKED
                            self.store.update_trade_be(pos.trade_id)

                            if self.on_pnl_realized:
                                pnl = half_qty * abs(pos.tp1_price - pos.entry_price) - (half_qty * pos.tp1_price * 0.0006)
                                self.on_pnl_realized(pos.trade_id, pnl, "TP1")
                            continue

            # B. Ratchet Trailing on Runner (Post-TP1)
            if pos.is_tp1_filled:
                one_r = abs(pos.tp1_price - pos.entry_price) / 1.5
                if one_r > 0:
                    current_r = (current_price - pos.entry_price) / one_r if is_long else (pos.entry_price - current_price) / one_r
                    if current_r > pos.highest_r_reached:
                        pos.highest_r_reached = current_r

                    # Ratchet Stepping:
                    # At >= +2.0R -> lock +1.0R (ratchet_level 1)
                    # At >= +3.0R -> lock +2.0R (ratchet_level 2)
                    # At >= +4.0R -> lock +3.0R (ratchet_level 3)
                    target_ratchet_lvl = int(current_r) - 1
                    if target_ratchet_lvl > pos.ratchet_level and target_ratchet_lvl >= 1:
                        locked_r = float(target_ratchet_lvl)
                        if is_long:
                            new_sl = round(pos.entry_price + (locked_r * one_r), 8)
                        else:
                            new_sl = round(pos.entry_price - (locked_r * one_r), 8)

                        sl_update_ok = True
                        if not self.dry_run:
                            try:
                                self.client.set_trading_stop(symbol=pos.symbol, stop_loss=new_sl)
                            except Exception as e:
                                logger.error(f"Error ratcheting SL for {pos.symbol} to {new_sl}: {e}")
                                sl_update_ok = False

                        if sl_update_ok or self.dry_run:
                            pos.stop_loss_price = new_sl
                            pos.ratchet_level = target_ratchet_lvl
                            self.store.update_trade_sl(pos.trade_id, new_sl, f"RATCHET_LOCK_{int(locked_r)}R")
                            logger.info(f"🔥 [RATCHET-TRAIL] {pos.symbol} reached +{current_r:.2f}R! SL ratcheted to +{locked_r:.1f}R ({new_sl:.6f}). Profit locked!")

            # C. Check TP2 (Runner Target)
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

            # D. Check Stop-Loss / Break-Even / Ratchet Hit
            hit_sl = (current_price <= pos.stop_loss_price) if is_long else (current_price >= pos.stop_loss_price)
            if hit_sl:
                if pos.ratchet_level >= 1:
                    status = "CLOSED_RATCHET"
                    desc = f"Ratchet Trailing SL (+{pos.ratchet_level}R locked)"
                elif pos.is_be_locked:
                    status = "CLOSED_BE"
                    desc = "Break-Even SL"
                else:
                    status = "CLOSED_SL"
                    desc = "Initial SL"

                logger.info(f"[{status}] {pos.symbol} touched {desc} level {pos.stop_loss_price}. Position closed.")
                pos.status = TradeStatus(status)
                self.store.close_trade(pos.trade_id, status)
                self.store.set_cooldown(pos.symbol, pos.direction.value, 600, f"Hit {status}")

                if self.on_pnl_realized:
                    if pos.ratchet_level >= 1:
                        pnl = pos.remaining_qty * abs(pos.stop_loss_price - pos.entry_price) - (pos.remaining_qty * pos.stop_loss_price * 0.0006)
                    elif pos.is_be_locked:
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

