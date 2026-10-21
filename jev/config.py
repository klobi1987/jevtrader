"""Configuration loader and validator for JevTrader."""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class JevConfig:
    # Credentials
    bybit_api_key: str
    bybit_api_secret: str
    openrouter_api_key: str
    
    # Endpoints
    hosts: List[str]
    db_path: str
    decisions_url: str
    
    # Risk & Portfolio
    max_active_positions: int
    risk_per_trade_pct: float
    max_risk_per_trade_pct: float
    max_portfolio_risk_pct: float
    min_available_margin_usd: float
    min_sl_dist_pct: float
    max_leverage_cap: float
    liquidation_buffer_pct: float
    exit_mode: str

    
    # Regime
    bull_breadth_threshold: float
    bear_breadth_threshold: float
    
    # Confluence
    min_score_threshold: float
    high_conviction_threshold: float
    min_24h_turnover_usd: float
    max_spread_bps: float
    
    # Models
    entry_model: str
    entry_model_flow: str
    watchdog_model: str
    min_entry_probability: float
    invalidation_threshold: float
    
    # Cadence
    scan_interval_seconds: float
    watchdog_interval_seconds: float
    post_only_timeout_seconds: float

def load_config(config_path: str = "config.yaml") -> JevConfig:
    p = Path(config_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / config_path
        
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
        
    # Check local jevtrader/.env first, then fallback to configured path (../.env)
    local_env = p.parent / ".env"
    configured_env = (p.parent / raw.get("system", {}).get("env_file", "../.env")).resolve()
    if local_env.exists():
        load_dotenv(local_env)
    elif configured_env.exists():
        load_dotenv(configured_env)
    else:
        load_dotenv()

        
    bybit_key = os.getenv("BYBIT_API_KEY", "")
    bybit_secret = os.getenv("BYBIT_API_SECRET", "")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
    
    system = raw.get("system", {})
    portfolio = raw.get("portfolio", {})
    regime = raw.get("regime", {})
    confluence = raw.get("confluence", {})
    models = raw.get("models", {})
    cadence = raw.get("cadence", {})
    
    return JevConfig(
        bybit_api_key=bybit_key,
        bybit_api_secret=bybit_secret,
        openrouter_api_key=openrouter_key,
        hosts=system.get("hosts", ["https://api.bytick.com", "https://api.bybit.com"]),
        db_path=str((p.parent / system.get("db_path", "data/jevtrader.sqlite3")).resolve()),
        decisions_url=models.get("decisions_url", "https://openrouter.ai/api/alpha/decisions"),
        max_active_positions=int(portfolio.get("max_active_positions", 8)),
        risk_per_trade_pct=float(portfolio.get("risk_per_trade_pct", 0.035)),
        max_risk_per_trade_pct=float(portfolio.get("max_risk_per_trade_pct", 0.045)),
        max_portfolio_risk_pct=float(portfolio.get("max_portfolio_risk_pct", 0.150)),
        min_available_margin_usd=float(portfolio.get("min_available_margin_usd", 8.0)),
        min_sl_dist_pct=float(portfolio.get("min_sl_dist_pct", 0.006)),
        max_leverage_cap=float(portfolio.get("max_leverage_cap", 10.0)),

        liquidation_buffer_pct=portfolio.get("liquidation_buffer_pct", 0.025),
        exit_mode=portfolio.get("exit_mode", "two_tier"),
        bull_breadth_threshold=regime.get("bull_breadth_threshold", 0.65),
        bear_breadth_threshold=regime.get("bear_breadth_threshold", 0.35),
        min_score_threshold=float(confluence.get("min_score_threshold", 70.0)),
        high_conviction_threshold=float(confluence.get("high_conviction_threshold", 83.0)),
        min_24h_turnover_usd=float(confluence.get("min_24h_turnover_usd", 5000000.0)),
        max_spread_bps=float(confluence.get("max_spread_bps", 6.0)),
        entry_model=models.get("entry_model", "perplexity/pplx-decider-v1.2"),
        entry_model_flow=models.get("entry_model_flow", "liquid/d1-ultra"),
        watchdog_model=models.get("watchdog_model", "perplexity/pplx-decider-flash-v1.2"),
        min_entry_probability=models.get("min_entry_probability", 0.70),
        invalidation_threshold=models.get("invalidation_threshold", 0.75),
        scan_interval_seconds=cadence.get("scan_interval_seconds", 10.0),
        watchdog_interval_seconds=cadence.get("watchdog_interval_seconds", 3.0),
        post_only_timeout_seconds=cadence.get("post_only_timeout_seconds", 6.0),
    )
