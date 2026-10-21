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
        # Cache for 15m klines: symbol -> (timestamp, atr_pct, dist_to_vwap_bps, is_squeeze)
        self.kline_cache: Dict[str, Tuple[float, float, float, bool]] = {}
        self.pool = ThreadPoolExecutor(max_workers=6)

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
                    "rs_vs_btc": rs_vs_btc
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

        # 4. Rank Candidates by Relative Strength vs BTC
        valid_usdt_pairs.sort(key=lambda x: x["rs_vs_btc"], reverse=True)
        top_longs = valid_usdt_pairs[:10]  # Top 10 strongest
        top_shorts = valid_usdt_pairs[-10:] # Top 10 weakest (sorted ascending)
        top_shorts.reverse()

        return breadth_state, top_longs, top_shorts, tickers_dict

    def fetch_microstructure_telemetry(self, symbol: str, rs_vs_btc_pct: float, turnover_usd: float, is_short: bool = False) -> Optional[ConfluenceFactors]:
        """
        Pulls 50-level orderbook, recent trades concurrently in parallel (100ms total).
        Uses 3-minute in-memory cache for 15m klines to minimize network overhead.
        """
        try:
            now = time.time()

            # 1. Fetch Orderbook & Recent Trades concurrently in parallel
            f_book = self.pool.submit(self.client.get_orderbook, symbol, 50)
            f_trades = self.pool.submit(self.client.get_recent_trades, symbol, 100)

            # 2. Check Kline Cache (TTL = 180 seconds)
            cached_kline = self.kline_cache.get(symbol)
            if cached_kline and (now - cached_kline[0] < 180.0):
                _, atr_pct, dist_to_vwap_bps, is_squeeze = cached_kline
            else:
                klines = self.client.get_klines(symbol, interval="15", limit=20)
                atr_pct = 1.0
                dist_to_vwap_bps = 15.0
                is_squeeze = False

                if klines and len(klines) >= 10:
                    # Bybit returns klines in reverse chronological order (newest first).
                    # Reverse to chronological order (oldest first) so index i pairs with prev_c at i-1.
                    rev_klines = list(reversed(klines))
                    closes = [float(k[4]) for k in rev_klines]
                    highs = [float(k[2]) for k in rev_klines]
                    lows = [float(k[3]) for k in rev_klines]

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
                    is_squeeze = tr_list[-1] <= median_tr * 0.85 if tr_list else False

                self.kline_cache[symbol] = (now, atr_pct, dist_to_vwap_bps, is_squeeze)

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
                is_squeeze_breakout=is_squeeze
            )
        except Exception as e:
            logger.error(f"Error in parallel microstructure fetch for {symbol}: {e}")
            return None

