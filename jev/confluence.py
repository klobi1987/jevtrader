"""5-Factor Confluence Scoring Engine (0 to 100 Points)."""

from typing import Optional
from jev.contracts import TradeDirection, ConfluenceFactors, ConfluenceScore

class ConfluenceEngine:
    def __init__(self, min_threshold: float = 70.0, high_conviction_threshold: float = 83.0):
        self.min_threshold = min_threshold
        self.high_conviction_threshold = high_conviction_threshold

    def calculate_score(self, symbol: str, direction: TradeDirection, factors: ConfluenceFactors) -> ConfluenceScore:
        """
        Computes calibrated 0-100 confluence points across 5 distinct alpha factors.
        Strictly symmetric between LONG and SHORT.
        """
        score = 0.0

        # Factor 1: Relative Strength vs BTC (Max 25 pts)
        rs = factors.relative_strength_vs_btc_pct
        if direction == TradeDirection.LONG:
            if rs > 0:
                # 0% to +30% RS scales 0 to 25 points
                score += min(25.0, (rs / 30.0) * 25.0)
        else: # SHORT
            if rs < 0:
                # 0% to -30% RS scales 0 to 25 points
                score += min(25.0, (abs(rs) / 30.0) * 25.0)

        # Factor 2: Taker Flow / CVD Delta (Max 25 pts)
        tb_ratio = factors.taker_buy_ratio
        if direction == TradeDirection.LONG:
            if tb_ratio > 0.50:
                # 50% to 80% buy ratio scales 0 to 25 points
                score += min(25.0, ((tb_ratio - 0.50) / 0.30) * 25.0)
        else: # SHORT
            ts_ratio = 1.0 - tb_ratio
            if ts_ratio > 0.50:
                score += min(25.0, ((ts_ratio - 0.50) / 0.30) * 25.0)

        # Factor 3: Orderbook Imbalance & Wall Proximity (Max 20 pts)
        imb = factors.orderbook_imbalance_top20
        wall_dist = factors.wall_distance_pct

        if direction == TradeDirection.LONG:
            if imb > 0:
                score += min(12.0, (imb / 0.50) * 12.0)
        else:
            if imb < 0:
                score += min(12.0, (abs(imb) / 0.50) * 12.0)

        # Wall proximity bonus: if major wall is within 0.15%, gives solid foundation
        if wall_dist <= 0.15:
            score += 8.0
        elif wall_dist <= 0.30:
            score += 4.0

        # Factor 4: Breakout & Squeeze Expansion (Max 15 pts)
        if factors.is_squeeze_breakout:
            score += 10.0
        if factors.volume_24h_usd >= 20_000_000.0:
            score += 5.0
        elif factors.volume_24h_usd >= 10_000_000.0:
            score += 2.5

        # Factor 5: VWAP & Volatility Geometry (Max 15 pts)
        vwap_dist = factors.dist_to_vwap_bps
        if direction == TradeDirection.LONG:
            # Ideal long entry: 5 to 50 bps above VWAP
            if 0 <= vwap_dist <= 50:
                score += 10.0
            elif 50 < vwap_dist <= 120:
                score += 5.0
        else: # SHORT
            if -50 <= vwap_dist <= 0:
                score += 10.0
            elif -120 <= vwap_dist < -50:
                score += 5.0

        # Healthy ATR: 0.5% to 3.5%
        if 0.5 <= factors.atr_15m_pct <= 3.5:
            score += 5.0

        total_score = round(min(100.0, max(0.0, score)), 2)
        is_qualified = total_score >= self.min_threshold
        is_high = total_score >= self.high_conviction_threshold

        return ConfluenceScore(
            symbol=symbol,
            direction=direction,
            total_score=total_score,
            factors=factors,
            is_qualified=is_qualified,
            is_high_conviction=is_high
        )
