"""JevTrader Core Autonomous Trading Engine."""

import time
import logging
from typing import Dict, Any, List, Optional
from jev.config import JevConfig
from jev.bybit import BybitClient
from jev.contracts import MarketRegime, TradeDirection, TradeStatus
from jev.market import MarketScanner
from jev.confluence import ConfluenceEngine
from jev.decisions import DecisionsEngine
from jev.risk import RiskManager
from jev.store import StateStore
from jev.execution import ExecutionEngine
from jev.positions import PositionManager

logger = logging.getLogger("jevtrader.engine")

class JevTraderEngine:
    def __init__(self, config: JevConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        
        # 1. Initialize Subsystems
        self.client = BybitClient(
            api_key=config.bybit_api_key,
            api_secret=config.bybit_api_secret,
            hosts=config.hosts
        )
        self.store = StateStore(config.db_path)
        self.scanner = MarketScanner(
            client=self.client,
            bull_threshold=config.bull_breadth_threshold,
            bear_threshold=config.bear_breadth_threshold,
            min_turnover_usd=config.min_24h_turnover_usd
        )
        self.confluence = ConfluenceEngine(
            min_threshold=config.min_score_threshold,
            high_conviction_threshold=config.high_conviction_threshold
        )
        self.decisions = DecisionsEngine(
            api_key=config.openrouter_api_key,
            decisions_url=config.decisions_url,
            entry_model=config.entry_model,
            watchdog_model=config.watchdog_model,
            min_entry_prob=config.min_entry_probability,
            invalidation_threshold=config.invalidation_threshold
        )
        self.risk = RiskManager(
            base_risk_pct=config.risk_per_trade_pct,
            max_risk_pct=config.max_risk_per_trade_pct,
            min_sl_dist_pct=config.min_sl_dist_pct,
            max_leverage_cap=config.max_leverage_cap,
            liquidation_buffer_pct=config.liquidation_buffer_pct
        )
        self.execution = ExecutionEngine(
            client=self.client,
            store=self.store,
            dry_run=self.dry_run
        )
        self.positions = PositionManager(
            client=self.client,
            decisions=self.decisions,
            store=self.store,
            dry_run=self.dry_run,
            exit_mode=config.exit_mode,
            on_pnl_realized=self._on_pnl_realized
        )

        self.running = False
        self.simulated_balance = 500.0 # Default starting balance for dry-run
        self.total_realized_pnl = 0.0
        self.consecutive_losses = 0
        self.circuit_breaker_until = 0.0
        self.max_same_direction = 3  # Anti-clustering: max 3 unhedged positions in same direction

        # Rehydrate any existing open positions from SQLite database on startup
        self.positions.rehydrate_from_store()

    def _on_pnl_realized(self, trade_id: str, pnl: float, reason: str):
        """Callback from PositionManager when a position reaches a profit/loss milestone."""
        self.total_realized_pnl += pnl
        if pnl < -0.01:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 3:
                self.circuit_breaker_until = time.time() + 900.0 # 15 min protection
                logger.warning("[CIRCUIT-BREAKER-ACTIVE] 3 consecutive losses hit! Throttling new entries for 15 min to prevent drawdown spiral.")
        elif pnl > 0.01:
            self.consecutive_losses = 0

        if self.dry_run:
            self.simulated_balance += pnl
            logger.info(f"[REALIZED-PNL] {reason} on {trade_id}: ${pnl:+.2f} | New Simulated Balance: ${self.simulated_balance:.2f} (Total PnL: ${self.total_realized_pnl:+.2f})")
        else:
            logger.info(f"[REALIZED-PNL] {reason} on {trade_id}: ${pnl:+.2f} (Session Total: ${self.total_realized_pnl:+.2f})")


    def start(self):
        """Starts the autonomous trading loop."""
        self.running = True
        logger.info(f"=== JEVTRADER APEX ENGINE STARTED (Mode: {'DRY-RUN' if self.dry_run else 'LIVE'}) ===")

        while self.running:
            try:
                self.tick()
            except KeyboardInterrupt:
                logger.info("Stopping JevTrader engine...")
                self.running = False
                break
            except Exception as e:
                logger.error(f"Error in engine tick: {e}", exc_info=True)
                time.sleep(3)

    def tick(self):
        """Single autonomous decision-execution cycle."""
        # 1. Fetch current equity
        if self.dry_run:
            equity = self.simulated_balance
        else:
            equity = self.client.get_wallet_balance()
            if equity <= 0:
                logger.warning("Wallet balance is 0 or unreadable. Checking fallback...")
                equity = 100.0

        # 2. Scan Bybit universe in a single batch call (~200ms)
        breadth_state, top_longs, top_shorts, tickers_dict = self.scanner.scan_universe()
        regime = breadth_state.regime
        logger.info(f"[REGIME: {regime.value}] Breadth: {breadth_state.breadth_pct*100:.1f}% above VWAP ({breadth_state.pairs_above_vwap}/{breadth_state.total_pairs} USDT pairs) | BTC: ${breadth_state.btc_price:,.1f} ({breadth_state.btc_change_24h_pct*100:+.2f}%) | Equity: ${equity:,.2f}")

        # 3. Update & Watch Active Positions (TP1 scale-out, BE lock, Watchdog Ninja Cut)
        self.positions.update_and_watch(tickers_dict)

        # Circuit breaker cooldown check
        now = time.time()
        circuit_breaker_active = (now < self.circuit_breaker_until)
        if circuit_breaker_active:
            rem = int(self.circuit_breaker_until - now)
            logger.info(f"[CIRCUIT-BREAKER] Cooldown active ({rem}s remaining). Telemetry & scan active, new entries throttled.")

        # 4. Dynamic Risk & Margin Capacity (Continuous Trading while Balance Allows)
        # If an active position has locked Break-Even, its risk is 0, so slot is effectively free!
        unhedged_risk_count = sum(1 for p in self.positions.active_positions.values() if not p.is_be_locked)
        current_open_risk_usd = sum(p.initial_risk_usd for p in self.positions.active_positions.values() if not p.is_be_locked)
        max_portfolio_risk_usd = equity * self.config.max_portfolio_risk_pct

        # Used margin calculation: approximate used margin from active positions
        used_margin_est = sum((p.remaining_qty * p.entry_price) / 8.0 for p in self.positions.active_positions.values())
        free_margin_est = max(0.0, equity - used_margin_est)

        if free_margin_est < self.config.min_available_margin_usd:
            logger.info(f"Free margin ${free_margin_est:.2f} is below min ${self.config.min_available_margin_usd:.2f}. Holding open positions...")
            time.sleep(self.config.watchdog_interval_seconds)
            return

        if unhedged_risk_count >= self.config.max_active_positions:
            logger.info(f"Max active unhedged risk slots reached ({unhedged_risk_count}/{self.config.max_active_positions}). Monitoring active positions...")
            time.sleep(self.config.watchdog_interval_seconds)
            return

        # 5. Determine Candidates to Hunt based on Dynamic Market Regime
        candidates_to_evaluate: List[tuple] = [] # [(symbol, TradeDirection, rs, turnover)]

        if regime == MarketRegime.BULL_ALPHA:
            # Bull Alpha: Evaluate top 8 relative strength & squeeze leaders
            for t in top_longs[:8]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.LONG, t["rs_vs_btc"], t["turnover"], t.get("funding_rate", 0.0)))
        elif regime == MarketRegime.BLOOD_STREETS:
            # Blood in the Streets: Evaluate top 8 relative weakness & long cascade laggards
            for t in top_shorts[:8]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.SHORT, t["rs_vs_btc"], t["turnover"], t.get("funding_rate", 0.0)))
        else: # CHOP_ROTATION
            # Opportunistic Hedged: Top 4 Long leaders + Top 4 Short laggards
            for t in top_longs[:4]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.LONG, t["rs_vs_btc"], t["turnover"], t.get("funding_rate", 0.0)))
            for t in top_shorts[:4]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.SHORT, t["rs_vs_btc"], t["turnover"], t.get("funding_rate", 0.0)))

        # 6. Evaluate Microstructure and Confluence for Candidates
        entries_this_tick = 0
        max_entries_per_tick = 2  # Allow up to 2 rapid fills per tick if margin allows
        evaluated_candidates = []

        # 1. Evaluate top candidates for MTF matrix and dashboard telemetry
        for cand in candidates_to_evaluate[:10]:
            symbol, direction, rs_val, vol_val = cand[0], cand[1], cand[2], cand[3]
            fr_val = cand[4] if len(cand) > 4 else 0.0

            # Fetch orderbook, CVD, squeeze, and funding telemetry
            is_short = (direction == TradeDirection.SHORT)
            factors = self.scanner.fetch_microstructure_telemetry(symbol, rs_val, vol_val, is_short=is_short, funding_rate=fr_val)
            if not factors:
                continue

            # Compute 7-factor Confluence Score (0-100) with 4H/1H MTF Hard Veto
            score_res = self.confluence.calculate_score(symbol, direction, factors)
            logger.info(f"[EVAL {direction.value:<5}] {symbol:<12} | Score: {score_res.total_score:5.1f}/100 | RS: {factors.relative_strength_vs_btc_pct:+6.1f}% | [4H:{factors.tf_4h_trend} 1H:{factors.tf_1h_trend} 15M:{factors.tf_15m_trend} 5M:{factors.tf_5m_trend} 1M:{factors.tf_1m_trend}] | VETO: {factors.htf_conflict} | CVD Buy: {factors.taker_buy_ratio*100:4.1f}%")

            evaluated_candidates.append({
                "symbol": symbol,
                "direction": direction.value,
                "score": round(score_res.total_score, 1),
                "relative_strength": round(factors.relative_strength_vs_btc_pct, 1),
                "cvd_buy_pct": round(factors.taker_buy_ratio * 100, 1),
                "imbalance": round(factors.orderbook_imbalance_top20, 2),
                "wall_distance_pct": round(factors.wall_distance_pct, 2),
                "funding_rate": round(getattr(factors, "funding_rate", 0.0) * 100, 4),
                "is_squeeze_breakout": factors.is_squeeze_breakout,
                "tf_4h_trend": getattr(factors, "tf_4h_trend", "NEUTRAL"),
                "tf_4h_change_pct": getattr(factors, "tf_4h_change_pct", 0.0),
                "tf_1h_trend": getattr(factors, "tf_1h_trend", "NEUTRAL"),
                "tf_1h_change_pct": getattr(factors, "tf_1h_change_pct", 0.0),
                "tf_15m_trend": getattr(factors, "tf_15m_trend", "NEUTRAL"),
                "tf_5m_trend": getattr(factors, "tf_5m_trend", "NEUTRAL"),
                "tf_1m_trend": getattr(factors, "tf_1m_trend", "NEUTRAL"),
                "mtf_aligned": getattr(factors, "mtf_aligned", False),
                "htf_conflict": getattr(factors, "htf_conflict", False),
                "is_qualified": score_res.is_qualified,
                "price": float(tickers_dict.get(symbol, {}).get("lastPrice", 0.0))
            })

            # 2. Gate Order Execution (Pyramiding or New Entry)
            if entries_this_tick >= max_entries_per_tick:
                continue

            existing_pos = next((p for p in self.positions.active_positions.values() if p.symbol == symbol), None)
            is_pyramid_candidate = False
            if existing_pos:
                if existing_pos.direction == direction and existing_pos.is_be_locked and existing_pos.layer == 1:
                    is_pyramid_candidate = True
                else:
                    continue  # Already in position and not de-risked

            # Check execution safety gates
            if not is_pyramid_candidate:
                if circuit_breaker_active:
                    continue
                if self.store.is_cooldown_active(symbol, direction.value):
                    continue
                same_dir_count = sum(1 for p in self.positions.active_positions.values() if p.direction == direction and not p.is_be_locked)
                if same_dir_count >= self.max_same_direction:
                    continue
                if free_margin_est < self.config.min_available_margin_usd:
                    continue

            # Pyramiding Scale-in on Free Runner (Layer 2)
            if is_pyramid_candidate and existing_pos:
                if score_res.total_score >= 78.0 and not factors.htf_conflict:
                    curr_price = float(tickers_dict.get(symbol, {}).get("lastPrice", 0.0))
                    logger.info(f"[AI-CHECK-PYRAMID] High conviction ({score_res.total_score}/100). Calling {self.config.entry_model} for Layer 2 scale-in on {symbol}...")
                    decision = self.decisions.evaluate_entry(
                        symbol=symbol,
                        direction=direction,
                        confluence=score_res,
                        entry_price=curr_price,
                        sl_price=existing_pos.stop_loss_price,
                        tp1_price=existing_pos.tp2_price
                    )
                    if decision.is_approved:
                        pyramid_qty = round(existing_pos.initial_qty * 0.5, 4)
                        order_side = "Buy" if direction == TradeDirection.LONG else "Sell"
                        if not self.dry_run:
                            try:
                                self.client.place_order(
                                    symbol=symbol,
                                    side=order_side,
                                    order_type="Market",
                                    qty=pyramid_qty
                                )
                                self.client.set_trading_stop(symbol=symbol, stop_loss=existing_pos.stop_loss_price)
                            except Exception as e:
                                logger.error(f"Error executing Layer 2 pyramid order for {symbol}: {e}")
                                continue

                        existing_pos.remaining_qty += pyramid_qty
                        existing_pos.layer = 2
                        logger.info(f"🔥 [PYRAMID-SCALEIN] Layer 2 active on {symbol}! Added {pyramid_qty} units. Stop-loss remains protected at {existing_pos.stop_loss_price}. Capital risk: $0.00!")
                        entries_this_tick += 1
                continue

            # Standard New Entry if score qualifies (>= 70) and not vetoed
            if score_res.is_qualified and not factors.htf_conflict:
                curr_price = float(tickers_dict.get(symbol, {}).get("lastPrice", 0.0))
                if curr_price <= 0:
                    continue

                # Build draft plan to send geometry to AI
                draft_plan = self.risk.build_trade_plan(symbol, direction, curr_price, score_res, equity)
                if not draft_plan:
                    continue

                # Portfolio-level risk gate: check if adding this trade exceeds portfolio budget
                if (current_open_risk_usd + draft_plan.risk_usd) > max_portfolio_risk_usd:
                    logger.warning(f"Skipping {symbol}: new risk ${draft_plan.risk_usd:.2f} + current open risk ${current_open_risk_usd:.2f} exceeds cap ${max_portfolio_risk_usd:.2f}")
                    continue

                # Call OpenRouter Decisions API (~350ms, $0.000008)
                logger.info(f"[AI-CHECK] Calling {self.config.entry_model} on {symbol} {direction.value}...")
                decision = self.decisions.evaluate_entry(
                    symbol=symbol,
                    direction=direction,
                    confluence=score_res,
                    entry_price=curr_price,
                    sl_price=draft_plan.stop_loss_price,
                    tp1_price=draft_plan.tp1_price
                )

                logger.info(f"[AI-DECISION] {decision.model} | Edge: {decision.action_probability:.4f} | Absorb: {decision.wall_absorption_probability:.4f} | Approved: {decision.is_approved} ({decision.latency_seconds:.3f}s)")

                # If AI approves (edge >= 0.70), EXECUTE IMMEDIATELY!
                if decision.is_approved:
                    draft_plan.decision = decision
                    pos = self.execution.execute_entry(draft_plan)
                    if pos:
                        pos.funding_rate = getattr(factors, "funding_rate", 0.0)
                        pos.layer = 1
                        self.positions.add_position(pos)
                        logger.info(f"[TRADE-ENTERED] {direction.value} {symbol} active! Entry: {pos.entry_price} | SL: {pos.stop_loss_price} | TP1: {pos.tp1_price} | TP2: {pos.tp2_price} | Funding: {pos.funding_rate*100:+.4f}%")
                        current_open_risk_usd += draft_plan.risk_usd
                        margin_used_trade = draft_plan.position_notional_usd / max(1.0, draft_plan.leverage)
                        free_margin_est = max(0.0, free_margin_est - margin_used_trade)
                        entries_this_tick += 1

            time.sleep(0.1)

        # Export live snapshot for the Web Dashboard UI
        self._export_dashboard_state(equity, free_margin_est, used_margin_est, breadth_state, tickers_dict, evaluated_candidates)

        # Sleep cadence before next scan
        time.sleep(self.config.scan_interval_seconds)

    def _export_dashboard_state(self, equity: float, free_margin: float, used_margin: float,
                                breadth_state: Any, tickers_dict: Dict[str, Any],
                                evaluated_candidates: List[Dict[str, Any]]):
        """Atomically saves dashboard snapshot for the Web UI."""
        import json
        from pathlib import Path

        pos_list = []
        for pos in self.positions.active_positions.values():
            is_long = (pos.direction == TradeDirection.LONG)
            cp = pos.current_price
            ep = pos.entry_price
            if is_long:
                unrealized_usd = pos.remaining_qty * (cp - ep)
                unrealized_pct = ((cp - ep) / ep * 100) if ep > 0 else 0.0
            else:
                unrealized_usd = pos.remaining_qty * (ep - cp)
                unrealized_pct = ((ep - cp) / ep * 100) if ep > 0 else 0.0

            lev = getattr(pos, "leverage", 10.0)
            mmr = 0.01
            est_liq_dist = (1.0 / lev) - mmr if lev > 0 else 0.09
            if is_long:
                est_liq_price = ep * (1.0 - est_liq_dist)
                liq_buffer_pct = ((pos.stop_loss_price - est_liq_price) / ep * 100) if ep > 0 else 0.0
            else:
                est_liq_price = ep * (1.0 + est_liq_dist)
                liq_buffer_pct = ((est_liq_price - pos.stop_loss_price) / ep * 100) if ep > 0 else 0.0

            pos_list.append({
                "trade_id": pos.trade_id,
                "symbol": pos.symbol,
                "direction": pos.direction.value,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "initial_qty": pos.initial_qty,
                "remaining_qty": pos.remaining_qty,
                "stop_loss_price": pos.stop_loss_price,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "initial_risk_usd": pos.initial_risk_usd,
                "leverage": lev,
                "liq_price": round(est_liq_price, 8),
                "liq_buffer_pct": round(liq_buffer_pct, 2),
                "layer": getattr(pos, "layer", 1),
                "funding_rate": round(getattr(pos, "funding_rate", 0.0) * 100, 4),
                "is_tp1_filled": pos.is_tp1_filled,
                "is_be_locked": pos.is_be_locked,
                "ratchet_level": pos.ratchet_level,
                "highest_r_reached": round(pos.highest_r_reached, 2),
                "status": pos.status.value,
                "unrealized_pnl_usd": round(unrealized_usd, 4),
                "unrealized_pnl_pct": round(unrealized_pct, 2)
            })

        now = time.time()
        state = {
            "timestamp": now,
            "equity": round(equity, 2),
            "free_margin": round(free_margin, 2),
            "used_margin": round(used_margin, 2),
            "total_realized_pnl": round(self.total_realized_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_active": now < self.circuit_breaker_until,
            "circuit_breaker_seconds_left": max(0, int(self.circuit_breaker_until - now)),
            "regime": breadth_state.regime.value,
            "breadth_pct": round(breadth_state.breadth_pct * 100, 1),
            "pairs_above_vwap": breadth_state.pairs_above_vwap,
            "total_pairs": breadth_state.total_pairs,
            "btc_price": round(breadth_state.btc_price, 1),
            "btc_change_24h_pct": round(breadth_state.btc_change_24h_pct * 100, 2),
            "btc_impulse_15m": round(breadth_state.btc_impulse_15m * 100, 2),
            "system": {
                "mode": "DRY-RUN" if self.dry_run else "LIVE",
                "entry_model": self.config.entry_model,
                "watchdog_model": self.config.watchdog_model,
                "exit_mode": self.config.exit_mode,
                "max_positions": self.config.max_active_positions,
                "risk_per_trade_pct": round(self.config.risk_per_trade_pct * 100, 1),
                "max_same_direction": self.max_same_direction
            },
            "active_positions": pos_list,
            "evaluated_candidates": evaluated_candidates
        }

        try:
            db_dir = Path(self.config.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            target = db_dir / "dashboard_state.json"
            tmp = db_dir / "dashboard_state.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            tmp.replace(target)
        except Exception as e:
            logger.debug(f"Could not export dashboard state: {e}")

