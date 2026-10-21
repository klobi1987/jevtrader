"""JevTrader Core Data Contracts and Models."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Optional, List
import time

class MarketRegime(str, Enum):
    BULL_ALPHA = "BULL_ALPHA"          # > 65% above VWAP -> 2 Longs allowed, 0 Shorts
    BLOOD_STREETS = "BLOOD_STREETS"    # < 35% above VWAP -> 2 Shorts allowed, 0 Longs
    CHOP_ROTATION = "CHOP_ROTATION"    # 35-65% above VWAP -> 1 Long + 1 Short (Hedged)

class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class TradeStatus(str, Enum):
    PENDING_ORDER = "PENDING_ORDER"
    OPEN = "OPEN"
    TP1_HIT_BE_LOCKED = "TP1_HIT_BE_LOCKED"
    CLOSED_TP2 = "CLOSED_TP2"
    CLOSED_BE = "CLOSED_BE"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_RATCHET = "CLOSED_RATCHET"
    CLOSED_WATCHDOG = "CLOSED_WATCHDOG"
    CANCELLED = "CANCELLED"

@dataclass
class MarketBreadthState:
    total_pairs: int
    pairs_above_vwap: int
    breadth_pct: float
    btc_price: float
    btc_change_24h_pct: float
    btc_impulse_15m: float
    regime: MarketRegime
    timestamp: float = field(default_factory=time.time)

@dataclass
class ConfluenceFactors:
    relative_strength_vs_btc_pct: float  # +% or -% vs BTC
    volume_24h_usd: float
    taker_buy_ratio: float               # 0.0 to 1.0 (agresivni kupci)
    orderbook_imbalance_top20: float     # -1.0 to +1.0
    wall_distance_pct: float             # % to closest major bid/ask wall
    wall_volume_usd: float               # Size of supporting wall
    dist_to_vwap_bps: float              # Basis points to 15m VWAP
    atr_15m_pct: float                   # Volatility %
    is_squeeze_breakout: bool            # True if breaking out of ATR compression

@dataclass
class ConfluenceScore:
    symbol: str
    direction: TradeDirection
    total_score: float                   # 0.0 to 100.0
    factors: ConfluenceFactors
    is_qualified: bool                   # True if total_score >= 70.0
    is_high_conviction: bool             # True if total_score >= 83.0

@dataclass
class DecisionResponse:
    model: str
    action_probability: float            # 0.0000 to 1.0000 (noul from OpenRouter)
    wall_absorption_probability: float   # 0.0000 to 1.0000
    latency_seconds: float
    cost_usd: float
    is_approved: bool

@dataclass
class TradePlan:
    symbol: str
    direction: TradeDirection
    entry_price: float
    stop_loss_price: float
    tp1_price: float                     # +1.5R target (closes 50%)
    tp2_price: float                     # +2.5R to +3.0R runner (closes remaining 50%)
    sl_distance_pct: float               # must be >= 0.60%
    risk_pct_account: float              # 3.5% or 5.0%
    risk_usd: float
    position_notional_usd: float
    quantity: float
    leverage: float
    confluence: ConfluenceScore
    decision: Optional[DecisionResponse] = None

@dataclass
class ActivePosition:
    trade_id: str
    symbol: str
    direction: TradeDirection
    entry_price: float
    current_price: float
    initial_qty: float
    remaining_qty: float
    stop_loss_price: float
    tp1_price: float
    tp2_price: float
    initial_risk_usd: float
    is_tp1_filled: bool = False
    is_be_locked: bool = False
    ratchet_level: int = 0
    highest_r_reached: float = 0.0
    entry_time: float = field(default_factory=time.time)
    order_link_id: str = ""
    status: TradeStatus = TradeStatus.OPEN
