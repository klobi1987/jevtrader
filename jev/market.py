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
        
        # Timeframe-Aware Event-Driven Kline Caches:
        # 4H cache: symbol -> (boundary_ts, open_price, ema20, last_fetch_time)
        self.cache_4h: Dict[str, Tuple[float, float, float, float]] = {}
        # 1H cache: symbol -> (boundary_ts, open_price, ema20, has_upper_wick_rejection, last_fetch_time)
        self.cache_1h: Dict[str, Tuple[float, float, float, bool, float]] = {}
        # 15M cache: symbol -> (last_fetch_time, klines_15) (TTL = 90s)
        self.cache_15m: Dict[str, Tuple[float, Any]] = {}
        # 5M cache: symbol -> (last_fetch_time, klines_5) (TTL = 30s)
        self.cache_5m: Dict[str, Tuple[float, Any]] = {}
        self.pool = ThreadPoolExecutor(max_workers=14)

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
        top_longs_candidates = sorted(
            valid_usdt_pairs,
            key=lambda x: x["rs_vs_btc"] + (abs(x["funding_rate"]) * 50.0 if x["funding_rate"] < -0.0002 else 0.0),
            reverse=True
        )
        top_longs = top_longs_candidates[:12]

        top_shorts_candidates = sorted(
            valid_usdt_pairs,
            key=lambda x: x["rs_vs_btc"] - (x["funding_rate"] * 50.0 if x["funding_rate"] > 0.0002 else 0.0)
        )
        top_shorts = top_shorts_candidates[:12]

        return breadth_state, top_longs, top_shorts, tickers_dict

    def fetch_microstructure_telemetry(self, symbol: str, rs_vs_btc_pct: float, turnover_usd: float, is_short: bool = False, funding_rate: float = 0.0) -> Optional[ConfluenceFactors]:
        """
        Pulls 50-level orderbook and recent trades in parallel.
        Optimized HTF caching: 4H and 1H klines are cached until candle boundary (when they change).
        During candle lifetime, 4H and 1H trends are evaluated dynamically against real-time price in 0ms.
        """
        try:
            now = time.time()

            # 1. Fetch Orderbook & Recent Trades concurrently in parallel
            f_book = self.pool.submit(self.client.get_orderbook, symbol, 50)
            f_trades = self.pool.submit(self.client.get_recent_trades, symbol, 100)

            # 2. Check 4H Kline Cache (boundary-aware: only refresh when 4H candle closes or missing)
            cached_4h = self.cache_4h.get(symbol)
            f_k4h = None
            if not cached_4h or now >= cached_4h[0] or (now - cached_4h[3] > 14400.0):
                f_k4h = self.pool.submit(self.client.get_klines, symbol, "240", 12)

            # 3. Check 1H Kline Cache (boundary-aware: refresh at hour boundary or 15m mid-hour sync)
            cached_1h = self.cache_1h.get(symbol)
            f_k1h = None
            if not cached_1h or now >= cached_1h[0] or (now - cached_1h[4] > 900.0):
                f_k1h = self.pool.submit(self.client.get_klines, symbol, "60", 12)

            # 4. Check 15M Kline Cache (TTL = 90s)
            cached_15m = self.cache_15m.get(symbol)
            klines_15 = None
            f_k15 = None
            if cached_15m and (now - cached_15m[0] < 90.0):
                klines_15 = cached_15m[1]
            else:
                f_k15 = self.pool.submit(self.client.get_klines, symbol, "15", 20)

            # 5. Check 5M Kline Cache (TTL = 30s)
            cached_5m = self.cache_5m.get(symbol)
            klines_5 = None
            f_k5 = None
            if cached_5m and (now - cached_5m[0] < 30.0):
                klines_5 = cached_5m[1]
            else:
                f_k5 = self.pool.submit(self.client.get_klines, symbol, "5", 12)

            # Resolve any pending Kline futures
            if f_k4h:
                try:
                    klines_4h = f_k4h.result(timeout=4)
                    if klines_4h and len(klines_4h) >= 5:
                        rev_4h = list(reversed(klines_4h))
                        open_4h = float(rev_4h[-1][1])
                        closes_4h = [float(k[4]) for k in rev_4h]
                        ema20_4h = closes_4h[0]
                        m20_4 = 2.0 / (len(closes_4h) + 1)
                        for c in closes_4h[1:]:
                            ema20_4h = (c - ema20_4h) * m20_4 + ema20_4h
                        candle_start_4h = int(rev_4h[-1][0])
                        boundary_4h = (candle_start_4h / 1000.0) + 14400.0
                        cached_4h = (boundary_4h, open_4h, ema20_4h, now)
                        self.cache_4h[symbol] = cached_4h
                except Exception as e:
                    logger.debug(f"4H kline fetch failed for {symbol}: {e}")

            if f_k1h:
                try:
                    klines_1h = f_k1h.result(timeout=4)
                    if klines_1h and len(klines_1h) >= 5:
                        rev_1h = list(reversed(klines_1h))
                        open_1h = float(rev_1h[-1][1])
                        closes_1h = [float(k[4]) for k in rev_1h]
                        opens_1h = [float(k[1]) for k in rev_1h]
                        highs_1h = [float(k[2]) for k in rev_1h]
                        ema20_1h = closes_1h[0]
                        m20_1 = 2.0 / (len(closes_1h) + 1)
                        for c in closes_1h[1:]:
                            ema20_1h = (c - ema20_1h) * m20_1 + ema20_1h

                        has_wick_rejection = False
                        if len(highs_1h) >= 2:
                            prev_body = abs(closes_1h[-2] - opens_1h[-2])
                            prev_wick = highs_1h[-2] - max(closes_1h[-2], opens_1h[-2])
                            if prev_wick > max(0.00001, prev_body * 1.5):
                                has_wick_rejection = True

                        candle_start_1h = int(rev_1h[-1][0])
                        boundary_1h = (candle_start_1h / 1000.0) + 3600.0
                        cached_1h = (boundary_1h, open_1h, ema20_1h, has_wick_rejection, now)
                        self.cache_1h[symbol] = cached_1h
                except Exception as e:
                    logger.debug(f"1H kline fetch failed for {symbol}: {e}")

            if f_k15:
                try:
                    klines_15 = f_k15.result(timeout=4)
                    self.cache_15m[symbol] = (now, klines_15)
                except Exception as e:
                    logger.debug(f"15M kline fetch failed for {symbol}: {e}")

            if f_k5:
                try:
                    klines_5 = f_k5.result(timeout=4)
                    self.cache_5m[symbol] = (now, klines_5)
                except Exception as e:
                    logger.debug(f"5M kline fetch failed for {symbol}: {e}")

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
            current_price = (best_bid + best_ask) / 2.0
            spread_bps = (best_ask - best_bid) / best_bid * 10000

            bid_depth_20 = sum(p * q for p, q in bids[:20])
            ask_depth_20 = sum(p * q for p, q in asks[:20])
            tot_depth = bid_depth_20 + ask_depth_20
            imbalance = (bid_depth_20 - ask_depth_20) / tot_depth if tot_depth > 0 else 0.0

            if is_short:
                max_ask_bucket = max(asks[:20], key=lambda x: x[0] * x[1])
                wall_dist_pct = (max_ask_bucket[0] - best_ask) / best_ask * 100
                wall_vol_usd = max_ask_bucket[0] * max_ask_bucket[1]
            else:
                max_bid_bucket = max(bids[:20], key=lambda x: x[0] * x[1])
                wall_dist_pct = (best_bid - max_bid_bucket[0]) / best_bid * 100
                wall_vol_usd = max_bid_bucket[0] * max_bid_bucket[1]

            # Process Recent Trades (CVD Delta)
            buy_vol = sum(float(tr.get("size", 0)) * float(tr.get("price", 0)) for tr in trades if tr.get("side") == "Buy")
            sell_vol = sum(float(tr.get("size", 0)) * float(tr.get("price", 0)) for tr in trades if tr.get("side") == "Sell")
            tot_flow = buy_vol + sell_vol
            taker_buy_ratio = buy_vol / tot_flow if tot_flow > 0 else 0.5

            # Process 15M klines (ATR, VWAP, Squeeze, consecutive red bars)
            atr_pct = 1.0
            dist_to_vwap_bps = 15.0
            is_squeeze = False
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

            # 4H Dynamic Evaluation (Instant 0ms against real-time price)
            tf_4h_trend = "NEUTRAL"
            tf_4h_change_pct = 0.0
            if cached_4h:
                open_4h = cached_4h[1]
                ema20_4h = cached_4h[2]
                tf_4h_change_pct = round((current_price - open_4h) / open_4h * 100, 2) if open_4h > 0 else 0.0
                if current_price >= open_4h and current_price >= ema20_4h:
                    tf_4h_trend = "BULL"
                elif current_price < open_4h and (current_price < ema20_4h or tf_4h_change_pct <= -2.0):
                    tf_4h_trend = "BEAR"
                else:
                    tf_4h_trend = "CHOP"

            # 1H Dynamic Evaluation (Instant 0ms against real-time price)
            tf_1h_trend = "NEUTRAL"
            tf_1h_change_pct = 0.0
            has_upper_wick_rejection = False
            if cached_1h:
                open_1h = cached_1h[1]
                ema20_1h = cached_1h[2]
                has_upper_wick_rejection = cached_1h[3]
                tf_1h_change_pct = round((current_price - open_1h) / open_1h * 100, 2) if open_1h > 0 else 0.0
                if current_price >= open_1h and current_price >= ema20_1h:
                    tf_1h_trend = "BULL"
                elif current_price < open_1h and (current_price < ema20_1h or tf_1h_change_pct <= -1.2):
                    tf_1h_trend = "DUMPING" if tf_1h_change_pct <= -1.5 else "BEAR"
                else:
                    tf_1h_trend = "CHOP"

            # 5M Dynamic Evaluation (LTF Trigger Confirmation)
            tf_5m_trend = "NEUTRAL"
            if klines_5 and len(klines_5) >= 5:
                rev_5 = list(reversed(klines_5))
                closes_5 = [float(k[4]) for k in rev_5]
                opens_5 = [float(k[1]) for k in rev_5]

                ema9_5 = closes_5[0]
                m9 = 2.0 / 10.0
                for c in closes_5[1:]:
                    ema9_5 = (c - ema9_5) * m9 + ema9_5

                c_5 = current_price
                o_5 = opens_5[-1]
                if c_5 >= ema9_5 and c_5 >= o_5:
                    tf_5m_trend = "BULL"
                elif c_5 < ema9_5 and c_5 < o_5:
                    tf_5m_trend = "BEAR"
                else:
                    tf_5m_trend = "CHOP"

            # Multi-Timeframe Alignment & HTF Hard Conflict Filter
            htf_conflict = False
            if not is_short:  # LONG
                if (tf_4h_trend == "BEAR" and tf_4h_change_pct <= -2.5) or \
                   tf_1h_trend in ("DUMPING", "BEAR") or \
                   tf_1h_change_pct <= -1.0 or \
                   consecutive_red_15m >= 3 or \
                   has_upper_wick_rejection:
                    htf_conflict = True
                mtf_aligned = (tf_1h_trend == "BULL" and tf_5m_trend == "BULL" and tf_4h_trend in ("BULL", "CHOP") and not htf_conflict)
            else:  # SHORT
                if (tf_4h_trend == "BULL" and tf_4h_change_pct >= 2.5) or \
                   tf_1h_trend == "BULL" or \
                   tf_1h_change_pct >= 1.0:
                    htf_conflict = True
                mtf_aligned = (tf_1h_trend in ("BEAR", "DUMPING") and tf_5m_trend == "BEAR" and tf_4h_trend in ("BEAR", "CHOP") and not htf_conflict)

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
                tf_4h_trend=tf_4h_trend,
                tf_4h_change_pct=tf_4h_change_pct,
                tf_1h_trend=tf_1h_trend,
                tf_1h_change_pct=tf_1h_change_pct,
                tf_5m_trend=tf_5m_trend,
                mtf_aligned=mtf_aligned,
                htf_conflict=htf_conflict
            )
        except Exception as e:
            logger.error(f"Error in parallel microstructure fetch for {symbol}: {e}")
            return None

