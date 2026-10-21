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

        # Factor 6: Funding Squeeze Alpha (Bonus up to 15 pts)
        fr = getattr(factors, "funding_rate", 0.0)
        if direction == TradeDirection.LONG:
            # Negative funding = shorts pay longs -> explosive short squeeze fuel
            if fr <= -0.0010:
                score += 15.0
            elif fr <= -0.0005:
                score += 10.0
            elif fr <= -0.0002:
                score += 6.0
            elif fr < 0.0:
                score += 3.0
        else: # SHORT
            # Positive funding = longs pay shorts -> long liquidation cascade fuel
            if fr >= 0.0010:
                score += 15.0
            elif fr >= 0.0005:
                score += 10.0
            elif fr > 0.0001:
                score += 3.0

        # Factor 7: Multi-Timeframe Matrix & Kinematic Inflection (Gemini 4 Argon Architecture)
        htf_conflict = getattr(factors, "htf_conflict", False)
        tf_4h = getattr(factors, "tf_4h_trend", "NEUTRAL")
        tf_4h_chg = getattr(factors, "tf_4h_change_pct", 0.0)
        tf_1h = getattr(factors, "tf_1h_trend", "NEUTRAL")
        tf_1h_chg = getattr(factors, "tf_1h_change_pct", 0.0)
        tf_15m = getattr(factors, "tf_15m_trend", "NEUTRAL")
        taker_ratio = getattr(factors, "taker_buy_ratio", 0.5)

        if direction == TradeDirection.LONG:
            # HARD VETO ONLY on actual cascade crashes (IOSTUSDT waterfall protection)
            if (tf_4h == "BEAR" and tf_4h_chg <= -2.5) or tf_1h == "DUMPING" or tf_1h_chg <= -2.0:
                htf_conflict = True
        else: # SHORT
            if (tf_4h == "BULL" and tf_4h_chg >= 2.5) or tf_1h == "PUMPING" or tf_1h_chg >= 2.0:
                htf_conflict = True

        if htf_conflict:
            # HARD VETO: 4H or 1H is in an active cascade waterfall dump/pump!
            # Immediate disqualification to prevent catching falling knives.
            return ConfluenceScore(
                symbol=symbol,
                direction=direction,
                total_score=0.0,
                factors=factors,
                is_qualified=False,
                is_high_conviction=False
            )

        # Smart 1H Wick Handling (Sol 6.1: Reward Wick Absorption only when price is actively advancing)
        has_wick = getattr(factors, "has_upper_wick_rejection", False)
        tf_1m = getattr(factors, "tf_1m_trend", "NEUTRAL")
        if has_wick:
            if direction == TradeDirection.LONG:
                if taker_ratio >= 0.55 and tf_1m in ("BULL", "PUMPING"):
                    score += 8.0  # Wick Absorption: aggressive buyers breaking resistance with price moving up!
                else:
                    score -= 5.0  # Mild overhead caution
            else:
                if taker_ratio <= 0.45 and tf_1m in ("BEAR", "DUMPING"):
                    score += 8.0
                else:
                    score -= 5.0

        # Preduhitreni Ulaz (Anticipatory Entry) vs Full Alignment
        is_anticipatory = getattr(factors, "is_anticipatory_entry", False)
        if getattr(factors, "mtf_aligned", False):
            # Perfect trend continuation across all 5 tiers
            score += 20.0
        elif is_anticipatory:
            # Golden Anticipation: HTF permission + 15M base/pullback + 1M/5M momentum ignition
            score += 20.0
        elif tf_1h == ("BULL" if direction == TradeDirection.LONG else "BEAR"):
            score += 10.0
        elif (tf_1h_chg > -1.2 if direction == TradeDirection.LONG else tf_1h_chg < 1.2):
            score += 5.0  # HTF Permission granted (healthy pause)

        # Discount Pullback Bonus on 15M
        if direction == TradeDirection.LONG and tf_15m in ("CHOP", "BEAR"):
            if 0 <= factors.dist_to_vwap_bps <= 50 and taker_ratio >= 0.55:
                score += 8.0  # Coiled Spring discount near VWAP support

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
