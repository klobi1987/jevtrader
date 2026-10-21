"""Unit tests for Golden Combo: Negative Funding Squeeze, Breakout, and MTF Hard Veto."""

import pytest
from jev.contracts import TradeDirection, ConfluenceFactors
from jev.confluence import ConfluenceEngine

def test_negative_funding_squeeze_scoring():
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    # Base factors with moderate RS and orderbook
    base_factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=15.0, # 15/30 * 25 = 12.5 pts
        volume_24h_usd=25_000_000.0,       # 5 pts volume
        taker_buy_ratio=0.65,              # (0.15/0.30)*25 = 12.5 pts
        orderbook_imbalance_top20=0.25,    # (0.25/0.50)*12 = 6.0 pts
        wall_distance_pct=0.10,            # 8.0 pts wall defense
        wall_volume_usd=500_000.0,
        dist_to_vwap_bps=20.0,             # 10.0 pts ideal entry
        atr_15m_pct=1.8,                   # 5.0 pts healthy ATR
        is_squeeze_breakout=True,          # 10.0 pts breakout
        funding_rate=-0.0012,              # Extreme negative funding (-0.12% / 8h) -> +15.0 pts
        mtf_aligned=True,                  # +20.0 pts
        htf_conflict=False
    )

    score_res = engine.calculate_score("SQUEEZEUSDT", TradeDirection.LONG, base_factors)
    assert score_res.total_score >= 80.0
    assert score_res.is_qualified is True
    assert score_res.is_high_conviction is True

def test_htf_hard_veto_blocks_trade():
    """Verify that a 1H reversal/dump instantly vetoes a trade even with good 15m stats (IOST/CVX protection)."""
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    veto_factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=40.0, # Looks amazing on 24h
        volume_24h_usd=50_000_000.0,
        taker_buy_ratio=0.75,
        orderbook_imbalance_top20=0.40,
        wall_distance_pct=0.05,
        wall_volume_usd=1_000_000.0,
        dist_to_vwap_bps=10.0,
        atr_15m_pct=2.0,
        is_squeeze_breakout=True,
        funding_rate=-0.0020,
        tf_1h_trend="DUMPING",
        tf_1h_change_pct=-4.5,
        tf_5m_trend="BEAR",
        mtf_aligned=False,
        htf_conflict=True # 1H is crashing!
    )

    score_res = engine.calculate_score("IOSTUSDT", TradeDirection.LONG, veto_factors)
    # Must be 100% BLOCKED
    assert score_res.total_score == 0.0
    assert score_res.is_qualified is False
    assert score_res.is_high_conviction is False

def test_short_funding_cascade_scoring():
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    # Short candidate with positive funding (+0.15% / 8h) where longs are bleeding fees
    short_factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=-20.0,
        volume_24h_usd=30_000_000.0,
        taker_buy_ratio=0.30,              # Taker sell ratio = 0.70
        orderbook_imbalance_top20=-0.35,
        wall_distance_pct=0.12,
        wall_volume_usd=600_000.0,
        dist_to_vwap_bps=-30.0,
        atr_15m_pct=2.1,
        is_squeeze_breakout=True,
        funding_rate=+0.0015,              # Extreme positive funding (+0.15% / 8h) -> +15.0 pts
        tf_1h_trend="BEAR",
        tf_5m_trend="BEAR",
        mtf_aligned=True,
        htf_conflict=False
    )

    score_res = engine.calculate_score("BLEEDUSDT", TradeDirection.SHORT, short_factors)
    assert score_res.total_score >= 80.0
    assert score_res.is_qualified is True
    assert score_res.direction == TradeDirection.SHORT

def test_4h_bear_veto_blocks_long():
    """Verify that a 4H downtrend (-3.0%) hard vetoes any LONG candidate even if 15m/5m look bullish."""
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=25.0,
        volume_24h_usd=40_000_000.0,
        taker_buy_ratio=0.70,
        orderbook_imbalance_top20=0.30,
        wall_distance_pct=0.10,
        wall_volume_usd=800_000.0,
        dist_to_vwap_bps=15.0,
        atr_15m_pct=2.0,
        is_squeeze_breakout=True,
        funding_rate=-0.0010,
        tf_4h_trend="BEAR",
        tf_4h_change_pct=-3.2,
        tf_1h_trend="BULL",
        tf_5m_trend="BULL",
        mtf_aligned=False,
        htf_conflict=False # confluence engine must trigger veto based on 4H BEAR & -3.2%
    )

    score_res = engine.calculate_score("TRAPUSDT", TradeDirection.LONG, factors)
    assert score_res.total_score == 0.0
    assert score_res.is_qualified is False

def test_anticipatory_entry_does_not_require_all_green():
    """Verify that an anticipatory entry (15M in pullback/consolidation, but 1M/5M igniting with taker CVD)
    qualifies with high score WITHOUT requiring all 5 timeframes to be green."""
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=22.0,
        volume_24h_usd=35_000_000.0,
        taker_buy_ratio=0.68,
        orderbook_imbalance_top20=0.35,
        wall_distance_pct=0.08,
        wall_volume_usd=750_000.0,
        dist_to_vwap_bps=25.0, # Healthy pullback near VWAP
        atr_15m_pct=1.9,
        is_squeeze_breakout=True,
        funding_rate=-0.0008,
        tf_4h_trend="BULL",
        tf_4h_change_pct=1.5,
        tf_1h_trend="BULL",
        tf_1h_change_pct=0.6,
        tf_15m_trend="CHOP", # 15M is NOT green yet! Coiled spring pullback
        tf_5m_trend="BULL",
        tf_1m_trend="PUMPING", # 1M is igniting early
        mtf_aligned=False,    # NOT all 5 green
        is_anticipatory_entry=True,
        htf_conflict=False
    )

    score_res = engine.calculate_score("EARLYUSDT", TradeDirection.LONG, factors)
    assert score_res.total_score >= 75.0
    assert score_res.is_qualified is True

def test_wick_absorption_does_not_veto():
    """Verify that a previous 1H wick rejection does NOT trigger a hard veto when buyers are absorbing it."""
    engine = ConfluenceEngine(min_threshold=70.0, high_conviction_threshold=83.0)

    factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=25.0,
        volume_24h_usd=40_000_000.0,
        taker_buy_ratio=0.65,
        orderbook_imbalance_top20=0.25,
        wall_distance_pct=0.10,
        wall_volume_usd=600_000.0,
        dist_to_vwap_bps=20.0,
        atr_15m_pct=2.0,
        is_squeeze_breakout=True,
        funding_rate=-0.0005,
        tf_4h_trend="BULL",
        tf_1h_trend="BULL",
        tf_15m_trend="BULL",
        tf_5m_trend="BULL",
        tf_1m_trend="BULL",
        mtf_aligned=True,
        has_upper_wick_rejection=True, # Previous 1H candle had upper wick
        htf_conflict=False
    )

    score_res = engine.calculate_score("ALTUSDT", TradeDirection.LONG, factors)
    # Must NOT be 0.0 Veto! Should qualify strongly with wick absorption bonus
    assert score_res.total_score >= 80.0
    assert score_res.is_qualified is True

if __name__ == "__main__":
    test_negative_funding_squeeze_scoring()
    test_htf_hard_veto_blocks_trade()
    test_short_funding_cascade_scoring()
    test_4h_bear_veto_blocks_long()
    test_anticipatory_entry_does_not_require_all_green()
    test_wick_absorption_does_not_veto()
    print("All Golden Combo + MTF Veto + Anticipatory tests PASSED successfully!")
