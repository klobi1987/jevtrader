"""OpenRouter Decisions API Engine (Sub-400ms System One Models)."""

import time
import json
import logging
import requests
from typing import Dict, Any, Optional, Tuple
from jev.contracts import TradeDirection, ConfluenceScore, DecisionResponse, ActivePosition

logger = logging.getLogger("jevtrader.decisions")

class DecisionsEngine:
    def __init__(self, api_key: str, decisions_url: str = "https://openrouter.ai/api/alpha/decisions",
                 entry_model: str = "perplexity/pplx-decider-v1.2",
                 watchdog_model: str = "perplexity/pplx-decider-flash-v1.2",
                 min_entry_prob: float = 0.70,
                 invalidation_threshold: float = 0.75):
        self.api_key = api_key
        self.decisions_url = decisions_url
        self.entry_model = entry_model
        self.watchdog_model = watchdog_model
        self.min_entry_prob = min_entry_prob
        self.invalidation_threshold = invalidation_threshold
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def evaluate_entry(self, symbol: str, direction: TradeDirection, 
                       confluence: ConfluenceScore, 
                       entry_price: float, sl_price: float, 
                       tp1_price: float) -> DecisionResponse:
        """
        Calls Perplexity Decider v1.2 or Liquid D1-Ultra to confirm edge before order entry.
        Runs in ~350ms and costs ~$0.000008.
        """
        f = confluence.factors
        state = {
            "symbol": symbol,
            "direction": direction.value,
            "confluence_score": confluence.total_score,
            "factors": {
                "rs_vs_btc_pct": f.relative_strength_vs_btc_pct,
                "volume_24h_usd": f.volume_24h_usd,
                "taker_buy_ratio": f.taker_buy_ratio,
                "orderbook_imbalance_top20": f.orderbook_imbalance_top20,
                "wall_dist_pct": f.wall_distance_pct,
                "wall_vol_usd": f.wall_volume_usd,
                "dist_to_vwap_bps": f.dist_to_vwap_bps,
                "is_squeeze_breakout": f.is_squeeze_breakout,
                "funding_rate": getattr(f, "funding_rate", 0.0),
                "mtf_matrix": {
                    "4h_trend": getattr(f, "tf_4h_trend", "NEUTRAL"),
                    "4h_change_pct": getattr(f, "tf_4h_change_pct", 0.0),
                    "1h_trend": getattr(f, "tf_1h_trend", "NEUTRAL"),
                    "1h_change_pct": getattr(f, "tf_1h_change_pct", 0.0),
                    "5m_trend": getattr(f, "tf_5m_trend", "NEUTRAL"),
                    "mtf_aligned": getattr(f, "mtf_aligned", False)
                }
            },
            "geometry": {
                "entry_price": entry_price,
                "stop_loss": sl_price,
                "tp1": tp1_price
            }
        }

        fr_val = getattr(f, "funding_rate", 0.0)
        h4_str = f"{getattr(f, 'tf_4h_trend', '')} ({getattr(f, 'tf_4h_change_pct', 0.0):+.2f}%)"
        h1_str = f"{getattr(f, 'tf_1h_trend', '')} ({getattr(f, 'tf_1h_change_pct', 0.0):+.2f}%)"
        m5_str = getattr(f, 'tf_5m_trend', '')
        questions = {
            "entry_edge": {
                "type": "noul",
                "instructions": f"Determine probability that price reaches TP1 before SL given {direction.value} momentum, 4H trend {h4_str}, 1H trend {h1_str}, 5M trend {m5_str}, funding rate {fr_val:.6f}, and orderbook wall."
            },
            "wall_defense": {
                "type": "noul",
                "instructions": "Is there genuine passive limit depth absorption defending this entry?"
            }
        }

        payload = {
            "model": self.entry_model,
            "state": state,
            "questions": questions
        }

        t0 = time.time()
        try:
            resp = requests.post(self.decisions_url, headers=self.headers, json=payload, timeout=8)
            lat = time.time() - t0
            if resp.status_code == 200:
                data = resp.json()
                answers = data.get("answers", {})
                edge_noul = float(answers.get("entry_edge", {}).get("noul", 0.0))
                wall_noul = float(answers.get("wall_defense", {}).get("noul", 0.0))
                cost = float(data.get("usage", {}).get("cost", 0.0))

                is_approved = edge_noul >= self.min_entry_prob
                return DecisionResponse(
                    model=self.entry_model,
                    action_probability=edge_noul,
                    wall_absorption_probability=wall_noul,
                    latency_seconds=lat,
                    cost_usd=cost,
                    is_approved=is_approved
                )
            else:
                logger.warning(f"Decisions API returned HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.error(f"Decisions API call failed for {symbol}: {e}")

        # Fallback if API fails
        return DecisionResponse(
            model=self.entry_model,
            action_probability=0.0,
            wall_absorption_probability=0.0,
            latency_seconds=0.0,
            cost_usd=0.0,
            is_approved=False
        )

    def evaluate_invalidation(self, pos: ActivePosition, current_price: float,
                              current_imbalance: float, wall_distance_pct: float) -> Tuple[bool, float]:
        """
        24/7 Watchdog: Calls pplx-decider-flash-v1.2 (320ms, $0.000007).
        Determines if supporting orderbook wall has collapsed and thesis is broken.
        If True -> triggers Ninja Cut (-0.2R) to save capital!
        """
        state = {
            "symbol": pos.symbol,
            "direction": pos.direction.value,
            "entry_price": pos.entry_price,
            "current_price": current_price,
            "stop_loss": pos.stop_loss_price,
            "tp1": pos.tp1_price,
            "current_imbalance": current_imbalance,
            "wall_distance_pct": wall_distance_pct,
            "hold_time_seconds": round(time.time() - pos.entry_time, 1)
        }

        questions = {
            "thesis_invalidated": {
                "type": "noul",
                "instructions": "Has the supporting orderbook structure collapsed, warranting an immediate early exit before hard SL?"
            }
        }

        payload = {
            "model": self.watchdog_model,
            "state": state,
            "questions": questions
        }

        try:
            resp = requests.post(self.decisions_url, headers=self.headers, json=payload, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                inval_noul = float(data.get("answers", {}).get("thesis_invalidated", {}).get("noul", 0.0))
                should_exit = inval_noul >= self.invalidation_threshold
                return should_exit, inval_noul
        except Exception as e:
            logger.warning(f"Watchdog decision check failed for {pos.symbol}: {e}")

        return False, 0.0
