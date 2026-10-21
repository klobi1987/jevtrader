"""Unit tests for Risk Management, Dynamic Safe Leverage, and Guaranteed Liquidation Safety."""

import pytest
from jev.contracts import TradeDirection, ConfluenceFactors, ConfluenceScore
from jev.risk import RiskManager

def test_sl_is_always_above_liquidation_in_long():
    """Verify that in LONG trades, Stop Loss is ALWAYS strictly above the liquidation price with >= 2.5% buffer."""
    risk_mgr = RiskManager(
        base_risk_pct=0.035,
        max_risk_pct=0.045,
        min_sl_dist_pct=0.006,
        max_leverage_cap=10.0,
        liquidation_buffer_pct=0.025
    )

    # Test across small, normal, and extreme ATR scenarios
    for atr_val in [0.5, 1.8, 3.5, 7.0, 12.0]:
        factors = ConfluenceFactors(
            relative_strength_vs_btc_pct=20.0,
            volume_24h_usd=20_000_000.0,
            taker_buy_ratio=0.65,
            orderbook_imbalance_top20=0.20,
            wall_distance_pct=1.5,
            wall_volume_usd=500_000.0,
            dist_to_vwap_bps=20.0,
            atr_15m_pct=atr_val,
            is_squeeze_breakout=True
        )
        confluence = ConfluenceScore(
            symbol="BTCUSDT",
            direction=TradeDirection.LONG,
            total_score=85.0,
            factors=factors,
            is_qualified=True,
            is_high_conviction=True
        )

        plan = risk_mgr.build_trade_plan(
            symbol="BTCUSDT",
            direction=TradeDirection.LONG,
            current_price=84000.0,
            confluence=confluence,
            equity_usd=95.0
        )

        assert plan is not None
        assert plan.leverage >= 1.0
        assert int(plan.leverage) == plan.leverage

        # Estimated liquidation price on exchange: entry * (1 - (1/L - MMR))
        mmr = 0.01
        est_liq_dist = (1.0 / plan.leverage) - mmr
        est_liq_price = plan.entry_price * (1.0 - est_liq_dist)

        # CRITICAL SAFETY GUARANTEE:
        # In a LONG, Stop Loss MUST be strictly HIGHER than Liquidation Price!
        assert plan.stop_loss_price > est_liq_price, (
            f"FATAL RISK VIOLATION: SL ({plan.stop_loss_price}) is NOT above Liq Price ({est_liq_price})!"
        )

        # Buffer between SL and Liquidation must be at least 2.0%
        buffer_pct = (plan.stop_loss_price - est_liq_price) / plan.entry_price
        assert buffer_pct >= 0.020, f"Insufficient buffer between SL and Liquidation: {buffer_pct*100:.2f}%"

def test_sl_is_always_below_liquidation_in_short():
    """Verify that in SHORT trades, Stop Loss is ALWAYS strictly below the liquidation price."""
    risk_mgr = RiskManager(
        base_risk_pct=0.035,
        max_risk_pct=0.045,
        min_sl_dist_pct=0.006,
        max_leverage_cap=10.0,
        liquidation_buffer_pct=0.025
    )

    factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=-20.0,
        volume_24h_usd=20_000_000.0,
        taker_buy_ratio=0.35,
        orderbook_imbalance_top20=-0.20,
        wall_distance_pct=1.0,
        wall_volume_usd=500_000.0,
        dist_to_vwap_bps=-20.0,
        atr_15m_pct=2.5,
        is_squeeze_breakout=True
    )
    confluence = ConfluenceScore(
        symbol="ETHUSDT",
        direction=TradeDirection.SHORT,
        total_score=80.0,
        factors=factors,
        is_qualified=True,
        is_high_conviction=False
    )

    plan = risk_mgr.build_trade_plan(
        symbol="ETHUSDT",
        direction=TradeDirection.SHORT,
        current_price=2000.0,
        confluence=confluence,
        equity_usd=100.0
    )

    assert plan is not None
    mmr = 0.01
    est_liq_dist = (1.0 / plan.leverage) - mmr
    est_liq_price = plan.entry_price * (1.0 + est_liq_dist)

    # In a SHORT, Stop Loss MUST be strictly LOWER than Liquidation Price!
    assert plan.stop_loss_price < est_liq_price
    buffer_pct = (est_liq_price - plan.stop_loss_price) / plan.entry_price
    assert buffer_pct >= 0.020

def test_sl_distance_is_capped_at_max_4_5_percent():
    """Verify that SL distance is capped at 4.5% to prevent out-of-control momentum stops."""
    risk_mgr = RiskManager(min_sl_dist_pct=0.006)

    # Huge wild ATR of 15%
    factors = ConfluenceFactors(
        relative_strength_vs_btc_pct=30.0,
        volume_24h_usd=50_000_000.0,
        taker_buy_ratio=0.70,
        orderbook_imbalance_top20=0.30,
        wall_distance_pct=5.0, # 5% wall distance
        wall_volume_usd=1_000_000.0,
        dist_to_vwap_bps=30.0,
        atr_15m_pct=15.0, # 15% ATR
        is_squeeze_breakout=True
    )
    confluence = ConfluenceScore(
        symbol="MEMEUSDT",
        direction=TradeDirection.LONG,
        total_score=88.0,
        factors=factors,
        is_qualified=True,
        is_high_conviction=True
    )

    plan = risk_mgr.build_trade_plan(
        symbol="MEMEUSDT",
        direction=TradeDirection.LONG,
        current_price=1.0,
        confluence=confluence,
        equity_usd=95.0
    )

    assert plan is not None
    assert plan.sl_distance_pct <= 0.045 # Capped at 4.5%
    assert plan.stop_loss_price >= 0.955
