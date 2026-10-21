"""Risk Management and Kelly-Optimized Compounding Sizing Engine."""

import math
import logging
from typing import Optional, Tuple
from jev.contracts import TradeDirection, ConfluenceScore, TradePlan

logger = logging.getLogger("jevtrader.risk")

class RiskManager:
    def __init__(self, base_risk_pct: float = 0.040, max_risk_pct: float = 0.050,
                 min_sl_dist_pct: float = 0.006, max_leverage_cap: float = 12.0,
                 liquidation_buffer_pct: float = 0.025):
        self.base_risk_pct = base_risk_pct
        self.max_risk_pct = max_risk_pct
        self.min_sl_dist_pct = min_sl_dist_pct
        self.max_leverage_cap = max_leverage_cap
        self.liquidation_buffer_pct = liquidation_buffer_pct

    def build_trade_plan(self, symbol: str, direction: TradeDirection, 
                         current_price: float, confluence: ConfluenceScore, 
                         equity_usd: float) -> Optional[TradePlan]:
        """
        Computes exact dollar risk, geometric SL/TP levels, and position sizing.
        Enforces min 0.60% SL distance to prevent fee cannibalization.
        """
        if equity_usd <= 0:
            return None

        # 1. Determine SL distance based on microstructure wall or ATR
        atr_pct = (confluence.factors.atr_15m_pct / 100.0) if confluence.factors.atr_15m_pct > 0 else 0.01
        wall_dist = (confluence.factors.wall_distance_pct / 100.0)

        # Place stop loss behind the supporting wall + small buffer
        raw_sl_dist = max(wall_dist * 1.5, atr_pct * 0.8)
        # Enforce minimum 0.60% SL distance
        sl_dist_pct = max(self.min_sl_dist_pct, raw_sl_dist)

        # 2. Compute Entry, SL, TP1, and TP2 Prices
        if direction == TradeDirection.LONG:
            entry_price = current_price
            stop_loss = round(entry_price * (1.0 - sl_dist_pct), 8)
            tp1_price = round(entry_price * (1.0 + (sl_dist_pct * 1.5)), 8) # +1.5R
            tp2_price = round(entry_price * (1.0 + (sl_dist_pct * 2.8)), 8) # +2.8R
        else: # SHORT
            entry_price = current_price
            stop_loss = round(entry_price * (1.0 + sl_dist_pct), 8)
            tp1_price = round(entry_price * (1.0 - (sl_dist_pct * 1.5)), 8) # +1.5R
            tp2_price = round(entry_price * (1.0 - (sl_dist_pct * 2.8)), 8) # +2.8R

        # 3. Dynamic Sizing based on Confluence Conviction
        if confluence.is_high_conviction:
            risk_pct = self.max_risk_pct # 5.0%
        else:
            risk_pct = self.base_risk_pct # 4.0%

        risk_budget_usd = equity_usd * risk_pct

        # 4. Total Loss per unit including estimated taker/maker fees (0.12%)
        fee_est_pct = 0.0012
        total_unit_loss_pct = sl_dist_pct + fee_est_pct

        # Position notional in USD
        position_notional = risk_budget_usd / total_unit_loss_pct

        # Required leverage
        calc_leverage = position_notional / equity_usd
        if calc_leverage > self.max_leverage_cap:
            position_notional = equity_usd * self.max_leverage_cap
            calc_leverage = self.max_leverage_cap

        # Quantity
        quantity = position_notional / entry_price if entry_price > 0 else 0.0

        # Verify liquidation buffer: isolation margin must have buffer > 2.5%
        # For leverage L, estimated liquidation distance is ~ (1/L - MMR)
        mmr = 0.01 # 1% maintenance margin
        est_liq_dist = (1.0 / calc_leverage) - mmr if calc_leverage > 0 else 1.0
        if est_liq_dist < self.liquidation_buffer_pct:
            # Adjust leverage down to satisfy buffer
            calc_leverage = 1.0 / (self.liquidation_buffer_pct + mmr)
            position_notional = equity_usd * calc_leverage
            quantity = position_notional / entry_price

        return TradePlan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=stop_loss,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            sl_distance_pct=sl_dist_pct,
            risk_pct_account=risk_pct,
            risk_usd=risk_budget_usd,
            position_notional_usd=position_notional,
            quantity=quantity,
            leverage=round(calc_leverage, 2),
            confluence=confluence
        )
