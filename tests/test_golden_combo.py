"""Unit tests for Golden Combo: Negative Funding Squeeze and Breakout Confluence."""

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
        funding_rate=-0.0012               # Extreme negative funding (-0.12% / 8h) -> +15.0 pts
    )

    score_res = engine.calculate_score("SQUEEZEUSDT", TradeDirection.LONG, base_factors)
    # Expected: 12.5 + 12.5 + 6.0 + 8.0 + (10.0+5.0) + (10.0+5.0) + 15.0 = 84.0 pts
    assert score_res.total_score >= 80.0
    assert score_res.is_qualified is True
    assert score_res.is_high_conviction is True

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
        funding_rate=+0.0015               # Extreme positive funding (+0.15% / 8h) -> +15.0 pts
    )

    score_res = engine.calculate_score("BLEEDUSDT", TradeDirection.SHORT, short_factors)
    assert score_res.total_score >= 80.0
    assert score_res.is_qualified is True
    assert score_res.direction == TradeDirection.SHORT

if __name__ == "__main__":
    test_negative_funding_squeeze_scoring()
    test_short_funding_cascade_scoring()
    print("All Golden Combo tests PASSED successfully!")
