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
            exit_mode=config.exit_mode
        )

        self.running = False
        self.simulated_balance = 500.0 # Default starting balance for dry-run

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

        # 2. Scan entire Bybit universe (912 pairs in ~1.6s)
        breadth_state, top_longs, top_shorts = self.scanner.scan_universe()
        regime = breadth_state.regime
        logger.info(f"[REGIME: {regime.value}] Breadth: {breadth_state.breadth_pct*100:.1f}% above VWAP | BTC: ${breadth_state.btc_price:,.1f} ({breadth_state.btc_change_24h_pct*100:+.2f}%) | Equity: ${equity:,.2f}")

        # Map tickers for fast lookup in position watcher
        all_tickers = self.client.get_all_linear_tickers()
        tickers_dict = {t.get("symbol"): t for t in all_tickers}

        # 3. Update & Watch Active Positions (TP1 scale-out, BE lock, Watchdog Ninja Cut)
        self.positions.update_and_watch(tickers_dict)

        # 4. Check Chained Compounding Risk Capacity
        active_count = len(self.positions.active_positions)
        # If an active position has locked Break-Even, its risk is 0, so slot is effectively free!
        unhedged_risk_count = sum(1 for p in self.positions.active_positions.values() if not p.is_be_locked)

        if unhedged_risk_count >= self.config.max_active_positions:
            logger.info(f"Max active unhedged risk slots reached ({unhedged_risk_count}/{self.config.max_active_positions}). Monitoring active positions...")
            time.sleep(self.config.watchdog_interval_seconds)
            return

        # 5. Determine Candidates to Hunt based on Dynamic Market Regime
        candidates_to_evaluate: List[tuple] = [] # [(symbol, TradeDirection)]

        if regime == MarketRegime.BULL_ALPHA:
            # 2 Longs: Evaluate top 3 relative strength leaders
            for t in top_longs[:3]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.LONG, t["rs_vs_btc"], t["turnover"]))
        elif regime == MarketRegime.BLOOD_STREETS:
            # 2 Shorts: Evaluate top 3 relative weakness laggards
            for t in top_shorts[:3]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.SHORT, t["rs_vs_btc"], t["turnover"]))
        else: # CHOP_ROTATION
            # Opportunistic Hedged: Top 2 Long leaders + Top 2 Short laggards
            for t in top_longs[:2]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.LONG, t["rs_vs_btc"], t["turnover"]))
            for t in top_shorts[:2]:
                candidates_to_evaluate.append((t["symbol"], TradeDirection.SHORT, t["rs_vs_btc"], t["turnover"]))

        # 6. Evaluate Microstructure and Confluence for Candidates
        for symbol, direction, rs_val, vol_val in candidates_to_evaluate:
            # Skip if already holding a position in this symbol
            if any(p.symbol == symbol for p in self.positions.active_positions.values()):
                continue

            # Skip if cooldown is active
            if self.store.is_cooldown_active(symbol, direction.value):
                continue

            # Fetch orderbook, CVD, and squeeze telemetry
            factors = self.scanner.fetch_microstructure_telemetry(symbol, rs_val, vol_val)
            if not factors:
                continue

            # Compute 5-factor Confluence Score (0-100)
            score_res = self.confluence.calculate_score(symbol, direction, factors)
            logger.info(f"[EVAL {direction.value}] {symbol:<12} | Score: {score_res.total_score:5.1f}/100 | RS: {factors.relative_strength_vs_btc_pct:+6.1f}% | CVD Buy: {factors.taker_buy_ratio*100:4.1f}% | Imb: {factors.orderbook_imbalance_top20:+5.2f}")

            # If score qualifies (>= 70), call AI Decision Model!
            if score_res.is_qualified:
                curr_price = float(tickers_dict.get(symbol, {}).get("lastPrice", 0.0))
                if curr_price <= 0:
                    continue

                # Build draft plan to send geometry to AI
                draft_plan = self.risk.build_trade_plan(symbol, direction, curr_price, score_res, equity)
                if not draft_plan:
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
                        self.positions.add_position(pos)
                        logger.info(f"[TRADE-ENTERED] {direction.value} {symbol} active! Entry: {pos.entry_price} | SL: {pos.stop_loss_price} | TP1: {pos.tp1_price} | TP2: {pos.tp2_price}")
                        break # One new entry per cycle

            time.sleep(0.1)

        # Sleep cadence before next scan
        time.sleep(self.config.scan_interval_seconds)
