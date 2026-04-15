"""Additional data sources: multi-exchange spot aggregation + book depth."""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

import websockets.sync.client as wsclient


class CoinbaseSpotFeed:
    """Subscribes to Coinbase Advanced Trade ticker channel for BTC/ETH/SOL/XRP/etc.
    Complements Binance; engines can median the two."""

    # Coinbase product id mapping (they use BTC-USD not BTCUSDT)
    CB_MAP = {
        "BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD",
        "SOLUSDT": "SOL-USD", "XRPUSDT": "XRP-USD",
        "BNBUSDT": None, "DOGEUSDT": "DOGE-USD",
        "HYPEUSDT": None,  # not on Coinbase
    }

    def __init__(self, binance_symbols: list[str]):
        # normalize to Coinbase ids where supported
        self.product_ids = [p for s in binance_symbols for p in [self.CB_MAP.get(s)] if p]
        # map product_id back to binance symbol for unified lookup
        self.pid_to_sym = {v: k for k, v in self.CB_MAP.items() if v}
        self.prices: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="coinbase-ws").start()

    def stop(self):
        self._stop.set()

    def get(self, binance_symbol: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(binance_symbol.upper())

    def _loop(self):
        if not self.product_ids:
            return
        url = "wss://advanced-trade-ws.coinbase.com"
        backoff = 1
        while not self._stop.is_set():
            try:
                with wsclient.connect(url, max_size=2_000_000, open_timeout=15) as ws:
                    sub = {
                        "type": "subscribe",
                        "product_ids": self.product_ids,
                        "channel": "ticker",
                    }
                    ws.send(json.dumps(sub))
                    backoff = 1
                    while not self._stop.is_set():
                        raw = ws.recv(timeout=60)
                        msg = json.loads(raw)
                        if msg.get("channel") != "ticker":
                            continue
                        for event in msg.get("events", []):
                            for tick in event.get("tickers", []):
                                pid = tick.get("product_id")
                                price = tick.get("price")
                                if pid and price:
                                    sym = self.pid_to_sym.get(pid)
                                    if sym:
                                        with self._lock:
                                            self.prices[sym] = float(price)
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


def combined_spot(binance_price: Optional[float],
                  coinbase_price: Optional[float]) -> Optional[float]:
    """Median of available feeds. Fall back to whichever is present."""
    prices = [p for p in (binance_price, coinbase_price) if p]
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]
    return sum(prices) / len(prices)


class FundingRates:
    """Binance USDM perpetual funding + predicted funding.
    Positive funding → longs paying shorts (longs crowded, often reverts down).
    Negative funding → shorts crowded (often reverts up).

    Cached; refresh every 60s. Returns funding rate as decimal (0.0001 = 1bp).
    """
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.cache: dict[str, tuple[float, int]] = {}  # symbol -> (rate, ts)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="funding").start()

    def stop(self):
        self._stop.set()

    def get(self, symbol: str) -> Optional[float]:
        with self._lock:
            entry = self.cache.get(symbol.upper())
        return entry[0] if entry else None

    def _loop(self):
        import requests
        while not self._stop.is_set():
            for sym in self.symbols:
                try:
                    r = requests.get(
                        "https://fapi.binance.com/fapi/v1/premiumIndex",
                        params={"symbol": sym}, timeout=5,
                    )
                    d = r.json()
                    rate = float(d.get("lastFundingRate") or 0)
                    with self._lock:
                        self.cache[sym.upper()] = (rate, int(time.time()))
                except Exception:
                    pass
            # Binance recommends updating funding index every ~1 minute
            for _ in range(60):
                if self._stop.is_set(): return
                time.sleep(1)


class PolymarketFillFlow:
    """Tracks aggregate fill flow by market.
    For a given market condition id, get net USD flow (BUYs - SELLs) in last N seconds.
    If multiple wallets are buying one side heavily, that's informed-flow signal.
    """
    def __init__(self):
        self.cache: dict[str, tuple[list, int]] = {}  # cid -> (activities, fetched_at)

    def fetch(self, condition_id: str, max_age_s: int = 15) -> list:
        """Fetch recent activity for a market; cache briefly to avoid spam."""
        import requests
        now = int(time.time())
        entry = self.cache.get(condition_id)
        if entry and now - entry[1] < max_age_s:
            return entry[0]
        try:
            r = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"market": condition_id, "limit": 50},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=5,
            )
            acts = r.json() or []
            self.cache[condition_id] = (acts, now)
            return acts
        except Exception:
            return []

    def net_flow_last_seconds(self, condition_id: str, window_s: int = 60) -> tuple[float, float]:
        """Return (buy_usd, sell_usd) within last window_s seconds on Up side."""
        now = int(time.time())
        acts = self.fetch(condition_id)
        buy_usd = sell_usd = 0.0
        for a in acts:
            ts = int(a.get("timestamp") or 0)
            if ts < now - window_s:
                continue
            usd = float(a.get("usdcSize") or 0)
            side = (a.get("side") or "").upper()
            if side == "BUY":
                buy_usd += usd
            elif side == "SELL":
                sell_usd += usd
        return buy_usd, sell_usd


def count_open_by_direction(funder_address: str, max_age_s: int = 8) -> tuple[int, int]:
    """Cross-engine direction concurrency check. Returns (up_count, dn_count)
    of currently-open positions in our wallet. Cached briefly to avoid spam.
    Used by engines to enforce a per-direction cap so we don't end up with
    10 correlated Up bets that all blow up together on a reversal."""
    import requests
    cache = getattr(count_open_by_direction, "_cache", None)
    now = int(time.time())
    if cache and now - cache[0] < max_age_s:
        return cache[1]
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "limit": 100},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5,
        )
        positions = r.json() or []
    except Exception:
        return (0, 0)
    up = dn = 0
    for p in positions:
        size = float(p.get("size") or 0)
        cur = float(p.get("curPrice") or 0)
        # only count truly-open positions (not resolved 0/1)
        if size <= 0 or cur <= 0.01 or cur >= 0.99:
            continue
        out = (p.get("outcome") or "").upper()
        if out in ("UP", "YES"):
            up += 1
        elif out in ("DOWN", "DN", "NO"):
            dn += 1
    result = (up, dn)
    count_open_by_direction._cache = (now, result)
    return result


def orderbook_imbalance(book, depth_levels: int = 3) -> Optional[float]:
    """Given a py-clob-client OrderBookSummary, compute a scalar in [-1, +1]:
      +1 = overwhelming bid pressure, -1 = overwhelming ask pressure.

    Uses top `depth_levels` of each side, size-weighted.
    """
    try:
        bids = getattr(book, "bids", []) or []
        asks = getattr(book, "asks", []) or []
        if not bids or not asks:
            return None
        top_bids = sorted(bids, key=lambda b: -float(b.price))[:depth_levels]
        top_asks = sorted(asks, key=lambda a: float(a.price))[:depth_levels]
        bid_sz = sum(float(b.size) for b in top_bids)
        ask_sz = sum(float(a.size) for a in top_asks)
        total = bid_sz + ask_sz
        if total <= 0:
            return None
        return (bid_sz - ask_sz) / total
    except Exception:
        return None
