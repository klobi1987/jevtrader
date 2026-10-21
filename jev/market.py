"""Market Scanner and Microstructure Telemetry Engine."""

import math
import time
import logging
from typing import List, Dict, Any, Tuple, Optional
from jev.bybit import BybitClient
from jev.contracts import MarketRegime, MarketBreadthState, ConfluenceFactors

from concurrent.futures import ThreadPoolExecutor
logger = logging.getLogger("jevtrader.market")

class MarketScanner:
    def __init__(self, client: BybitClient, bull_threshold: float = 0.65, bear_threshold: float = 0.35, min_turnover_usd: float = 5_000_000.0):
        self.client = client
        self.bull_threshold = float(bull_threshold)
        self.bear_threshold = float(bear_threshold)
        self.min_turnover_usd = float(min_turnover_usd)
        # Tiered MTF Caches (Optimized per timeframe duration):
        self.cache_5m: Dict[str, Tuple[float, Any]] = {}   # TTL = 30s
        self.cache_15m: Dict[str, Tuple[float, Any]] = {}  # TTL = 90s
        self.cache_60m: Dict[str, Tuple[float, Any]] = {}  # TTL = 300s (5m)
        self.kline_cache: Dict[str, Tuple[float, float, float, bool, str, float, str, bool, bool]] = {}
        self.pool = ThreadPoolExecutor(max_workers=12)

    def scan_universe(self) -> Tuple[MarketBreadthState, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Scans all Bybit linear instruments in ~1.6s.
        Computes Global Market Regime and ranks Top Long and Top Short candidates.
        """
        tickers = self.client.get_all_linear_tickers()
        if not tickers:
            raise RuntimeError("Received empty tickers list from Bybit")

        # 1. Locate BTCUSDT
        btc_ticker = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
        if not btc_ticker:
            raise RuntimeError("BTCUSDT ticker not found")

        btc_price = float(btc_ticker.get("lastPrice", 0.0))
        btc_change_24h = float(btc_ticker.get("price24hPcnt", 0.0))

        # 2. Compute Market Breadth (% of USDT pairs above 24h midpoint as VWAP proxy)
        valid_usdt_pairs = []
        above_midpoint_count = 0
        total_usdt_pairs = 0
        tickers_dict = {}

        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym == "USDCUSDT":
                continue

            total_usdt_pairs += 1
            turnover = float(t.get("turnover24h", 0.0))
            last_price = float(t.get("lastPrice", 0.0))
            high_24h = float(t.get("highPrice24h", 0.0))
            low_24h = float(t.get("lowPrice24h", 0.0))
            midpoint = (high_24h + low_24h) / 2.0 if (high_24h + low_24h) > 0 else last_price
            funding_rate = float(t.get("fundingRate", 0.0))

            tickers_dict[sym] = t

            if last_price > midpoint:
                above_midpoint_count += 1

            if turnover >= self.min_turnover_usd:
                change = float(t.get("price24hPcnt", 0.0))
                rs_vs_btc = change - btc_change_24h
                valid_usdt_pairs.append({
                    "symbol": sym,
                    "lastPrice": last_price,
                    "turnover": turnover,
                    "change24h": change,
                    "rs_vs_btc": rs_vs_btc,
                    "funding_rate": funding_rate
                })

        breadth_pct = above_midpoint_count / total_usdt_pairs if total_usdt_pairs > 0 else 0.5

        # 3. Determine Market Regime (Argon GMRI Engine)
        if breadth_pct > self.bull_threshold:
            regime = MarketRegime.BULL_ALPHA
        elif breadth_pct < self.bear_threshold:
            regime = MarketRegime.BLOOD_STREETS
        else:
            regime = MarketRegime.CHOP_ROTATION

        breadth_state = MarketBreadthState(
            total_pairs=total_usdt_pairs,
            pairs_above_vwap=above_midpoint_count,
            breadth_pct=breadth_pct,
            btc_price=btc_price,
            btc_change_24h_pct=btc_change_24h,
            btc_impulse_15m=0.0,
            regime=regime
        )

        # 4. Rank Candidates by Relative Strength vs BTC + Funding Squeeze Fuel
        # Long candidates: highest RS + bonus if shorts are paying funding (negative funding rate)
        top_longs_candidates = sorted(
            valid_usdt_pairs,
            key=lambda x: x["rs_vs_btc"] + (abs(x["funding_rate"]) * 50.0 if x["funding_rate"] < -0.0002 else 0.0),
            reverse=True
        )
        top_longs = top_longs_candidates[:12]

        # Short candidates: weakest RS + bonus if longs are paying funding (positive funding rate)
        top_shorts_candidates = sorted(
            valid_usdt_pairs,
            key=lambda x: x["rs_vs_btc"] - (x["funding_rate"] * 50.0 if x["funding_rate"] > 0.0002 else 0.0)
        )
        top_shorts = top_shorts_candidates[:12]

        return breadth_state, top_longs, top_shorts, tickers_dict

    def fetch_microstructure_telemetry(self, symbol: str, rs_vs_btc_pct: float, turnover_usd: float, is_short: bool = False, funding_rate: float = 0.0) -> Optional[ConfluenceFactors]:
        """
        Pulls 50-level orderbook, recent trades concurrently in parallel (100ms total).
        Uses 3-minute in-memory cache for 15m klines to minimize network overhead.
        """
        try:
            now = time.time()

            # 1. Fetch Orderbook & Recent Trades concurrently in parallel
            f_book = self.pool.submit(self.client.get_orderbook, symbol, 50)
            f_trades = self.pool.submit(self.client.get_recent_trades, symbol, 100)

            # 2. Check Tiered MTF Kline Caches (Per-Timeframe Smart Expiry)
            # 1H klines: 300s TTL (5 min) - candle moves slowly, only refreshed on macro updates
            cached_60m = self.cache_60m.get(symbol)
            if cached_60m and (now - cached_60m[0] < 300.0):
                f_k60 = None
                klines_60 = cached_60m[1]
            else:
                f_k60 = self.pool.submit(self.client.get_klines, symbol, "60", 12)

            # 15M klines: 90s TTL
            cached_15m = self.cache_15m.get(symbol)
            if cached_15m and (now - cached_15m[0] < 90.0):
                f_k15 = None
                klines_15 = cached_15m[1]
            else:
                f_k15 = self.pool.submit(self.client.get_klines, symbol, "15", 20)

            # 5M klines: 30s TTL
            cached_5m = self.cache_5m.get(symbol)
            if cached_5m and (now - cached_5m[0] < 30.0):
                f_k5 = None
                klines_5 = cached_5m[1]
            else:
                f_k5 = self.pool.submit(self.client.get_klines, symbol, "5", 12)

            # Resolve any pending futures and update per-timeframe caches
            if f_k60:
                klines_60 = f_k60.result(timeout=4)
                self.cache_60m[symbol] = (now, klines_60)
            if f_k15:
                klines_15 = f_k15.result(timeout=4)
                self.cache_15m[symbol] = (now, klines_15)
            if f_k5:
                klines_5 = f_k5.result(timeout=4)
                self.cache_5m[symbol] = (now, klines_5)

                atr_pct = 1.0
                dist_to_vwap_bps = 15.0
                is_squeeze = False
                tf_1h_trend = "NEUTRAL"
                tf_1h_change_pct = 0.0
                tf_5m_trend = "NEUTRAL"
                mtf_aligned = False
                htf_conflict = False

                # A. Process 15m klines
                consecutive_red_15m = 0
                if klines_15 and len(klines_15) >= 10:
                    rev_klines = list(reversed(klines_15))
                    closes = [float(k[4]) for k in rev_klines]
                    opens = [float(k[1]) for k in rev_klines]
                    highs = [float(k[2]) for k in rev_klines]
                    lows = [float(k[3]) for k in rev_klines]

                    for c_b, o_b in zip(reversed(closes), reversed(opens)):
                        if c_b < o_b:
                            consecutive_red_15m += 1
                        else:
                            break

                    tr_list = []
                    for i in range(1, len(rev_klines)):
                        h = highs[i]
                        l = lows[i]
                        prev_c = closes[i - 1]
                        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                        tr_list.append(tr)

                    atr = sum(tr_list[-14:]) / min(14, len(tr_list)) if tr_list else (highs[-1] - lows[-1])
                    current_price = closes[-1]
                    atr_pct = (atr / current_price) * 100 if current_price > 0 else 1.0

                    cum_vol = sum(float(k[5]) for k in rev_klines)
                    cum_turnover = sum(float(k[6]) for k in rev_klines)
                    vwap = cum_turnover / cum_vol if cum_vol > 0 else current_price
                    dist_to_vwap_bps = (current_price - vwap) / vwap * 10000

                    median_tr = sorted(tr_list)[len(tr_list) // 2] if tr_list else atr
                    prior_bars = tr_list[-5:-1] if len(tr_list) >= 5 else tr_list[:-1]
                    prior_avg_tr = sum(prior_bars) / len(prior_bars) if prior_bars else median_tr
                    is_compressed = prior_avg_tr <= median_tr * 0.90
                    is_expanding = tr_list[-1] >= median_tr * 1.10
                    is_squeeze = (is_compressed and is_expanding) or (tr_list[-1] <= median_tr * 0.70)

                # B. Process 1h klines (HTF Permission & Conflict Filter)
                if klines_60 and len(klines_60) >= 5:
                    rev_60 = list(reversed(klines_60))
                    closes_60 = [float(k[4]) for k in rev_60]
                    opens_60 = [float(k[1]) for k in rev_60]
                    highs_60 = [float(k[2]) for k in rev_60]

                    ema20_60 = closes_60[0]
                    m20 = 2.0 / (len(closes_60) + 1)
                    for c in closes_60[1:]:
                        ema20_60 = (c - ema20_60) * m20 + ema20_60

                    c_1h = closes_60[-1]
                    o_1h = opens_60[-1]
                    tf_1h_change_pct = round((c_1h - o_1h) / o_1h * 100, 2) if o_1h > 0 else 0.0

                    # Detect 1H Upper Wick Blow-off / Rejection
                    has_upper_wick_rejection = False
                    if len(highs_60) >= 2:
                        prev_body = abs(closes_60[-2] - opens_60[-2])
                        prev_wick = highs_60[-2] - max(closes_60[-2], opens_60[-2])
                        if prev_wick > max(0.00001, prev_body * 1.5):
                            has_upper_wick_rejection = True

                    if c_1h >= o_1h and c_1h >= ema20_60:
                        tf_1h_trend = "BULL"
                    elif c_1h < o_1h and (c_1h < ema20_60 or tf_1h_change_pct <= -1.2):
                        tf_1h_trend = "DUMPING" if tf_1h_change_pct <= -1.5 else "BEAR"
                    else:
                        tf_1h_trend = "CHOP"

                    # HTF Hard Conflict Check
                    if not is_short:  # LONG
                        if tf_1h_trend in ("DUMPING", "BEAR") or tf_1h_change_pct <= -1.0 or consecutive_red_15m >= 3 or has_upper_wick_rejection:
                            htf_conflict = True
                    else:  # SHORT
                        if tf_1h_trend == "BULL" or tf_1h_change_pct >= 1.0:
                            htf_conflict = True

                # C. Process 5m klines (LTF Trigger Confirmation)
                if klines_5 and len(klines_5) >= 5:
                    rev_5 = list(reversed(klines_5))
                    closes_5 = [float(k[4]) for k in rev_5]
                    opens_5 = [float(k[1]) for k in rev_5]

                    ema9_5 = closes_5[0]
                    m9 = 2.0 / 10.0
                    for c in closes_5[1:]:
                        ema9_5 = (c - ema9_5) * m9 + ema9_5

                    c_5 = closes_5[-1]
                    o_5 = opens_5[-1]
                    if c_5 >= ema9_5 and c_5 >= o_5:
                        tf_5m_trend = "BULL"
                    elif c_5 < ema9_5 and c_5 < o_5:
                        tf_5m_trend = "BEAR"
                    else:
                        tf_5m_trend = "CHOP"

                if not is_short:
                    mtf_aligned = (tf_1h_trend == "BULL" and tf_5m_trend == "BULL" and not htf_conflict)
                else:
                    mtf_aligned = (tf_1h_trend in ("BEAR", "DUMPING") and tf_5m_trend == "BEAR" and not htf_conflict)

                self.kline_cache[symbol] = (now, atr_pct, dist_to_vwap_bps, is_squeeze, tf_1h_trend, tf_1h_change_pct, tf_5m_trend, mtf_aligned, htf_conflict)

            # Wait for parallel orderbook & trades to resolve
            book_res = f_book.result(timeout=4)
            trades = f_trades.result(timeout=4)

            # Process Orderbook
            bids = [[float(p), float(q)] for p, q in book_res.get("b", [])]
            asks = [[float(p), float(q)] for p, q in book_res.get("a", [])]
            if not bids or not asks:
                return None

            best_bid = bids[0][0]
            best_ask = asks[0][0]
            spread_bps = (best_ask - best_bid) / best_bid * 10000

            bid_depth_20 = sum(p * q for p, q in bids[:20])
            ask_depth_20 = sum(p * q for p, q in asks[:20])
            tot_depth = bid_depth_20 + ask_depth_20
            imbalance = (bid_depth_20 - ask_depth_20) / tot_depth if tot_depth > 0 else 0.0

            if is_short:
                # For SHORT, wall of resistance sits in asks above best ask
                max_ask_bucket = max(asks[:20], key=lambda x: x[0] * x[1])
                wall_dist_pct = (max_ask_bucket[0] - best_ask) / best_ask * 100
                wall_vol_usd = max_ask_bucket[0] * max_ask_bucket[1]
            else:
                # For LONG, wall of support sits in bids below best bid
                max_bid_bucket = max(bids[:20], key=lambda x: x[0] * x[1])
                wall_dist_pct = (best_bid - max_bid_bucket[0]) / best_bid * 100
                wall_vol_usd = max_bid_bucket[0] * max_bid_bucket[1]

            # Process Recent Trades (CVD Delta)
            buy_vol = sum(float(tr.get("size", 0)) * float(tr.get("price", 0)) for tr in trades if tr.get("side") == "Buy")
            sell_vol = sum(float(tr.get("size", 0)) * float(tr.get("price", 0)) for tr in trades if tr.get("side") == "Sell")
            tot_flow = buy_vol + sell_vol
            taker_buy_ratio = buy_vol / tot_flow if tot_flow > 0 else 0.5

            return ConfluenceFactors(
                relative_strength_vs_btc_pct=rs_vs_btc_pct * 100,
                volume_24h_usd=turnover_usd,
                taker_buy_ratio=taker_buy_ratio,
                orderbook_imbalance_top20=imbalance,
                wall_distance_pct=wall_dist_pct,
                wall_volume_usd=wall_vol_usd,
                dist_to_vwap_bps=dist_to_vwap_bps,
                atr_15m_pct=atr_pct,
                is_squeeze_breakout=is_squeeze,
                funding_rate=funding_rate,
                tf_1h_trend=tf_1h_trend,
                tf_1h_change_pct=tf_1h_change_pct,
                tf_5m_trend=tf_5m_trend,
                mtf_aligned=mtf_aligned,
                htf_conflict=htf_conflict
            )
        except Exception as e:
            logger.error(f"Error in parallel microstructure fetch for {symbol}: {e}")
            return None

