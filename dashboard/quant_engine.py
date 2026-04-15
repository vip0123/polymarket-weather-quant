"""Quant engine — spot-vs-Polymarket arbitrage on 5-min crypto Up/Down binaries.

Strategy:
  1. Subscribe to Binance spot tickers (BTC/ETH/SOL/XRP) for real-time price.
  2. Scan Polymarket gamma for open 5-min binaries matching those assets.
  3. For each market, compute fair probability of Up from current spot move
     vs window-open price, using normal-model with crypto-specific sigma.
  4. If Polymarket asks for the winning side < fair - threshold, BUY taker.
  5. Risk-capped per-position, per-market, daily-loss stop.

Runs alongside copy-engine. Uses its own capital allocation, own state/log files.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Optional

import requests
import websockets.sync.client as wsclient
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, BalanceAllowanceParams, AssetType, OrderArgs,
)
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
CFG_FILE = RUNTIME / "quant_config.json"
STATE_FILE = RUNTIME / "quant_state.json"
TRADES_FILE = RUNTIME / "quant_trades.csv"
LOG_FILE = RUNTIME / "quant.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("quant")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


# ─── Binance spot feed ─────────────────────────────────────────────────────

class SpotFeed:
    """Maintains last-trade price per symbol. Updates from Binance WS ticker.
    Also caches recent klines for rolling volatility estimation."""
    def __init__(self, symbols: list[str]):
        self.symbols = [s.lower() for s in symbols]
        self.prices: dict[str, float] = {}
        self.history: dict[str, list[tuple[int, float]]] = {s.upper(): [] for s in symbols}
        # (sigma_per_min, refreshed_at) per symbol
        self.sigma_cache: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def rolling_sigma(self, symbol: str, lookback_min: int = 60) -> Optional[float]:
        """Log-return std per minute, refreshed every ~5 min."""
        symbol = symbol.upper()
        now = int(time.time())
        cached = self.sigma_cache.get(symbol)
        if cached and (now - cached[1]) < 300:
            return cached[0]
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "1m", "limit": lookback_min},
                timeout=5,
            )
            data = r.json()
            closes = [float(c[4]) for c in data]
            if len(closes) < 10:
                return None
            rets = []
            for i in range(1, len(closes)):
                if closes[i-1] > 0:
                    rets.append(math.log(closes[i] / closes[i-1]))
            if not rets:
                return None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
            sigma = math.sqrt(var)
            self.sigma_cache[symbol] = (sigma, now)
            return sigma
        except Exception as e:
            log.warning("sigma fetch %s: %s", symbol, e)
            return None

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="binance-ws")
        t.start()

    def stop(self):
        self._stop.set()

    def get(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(symbol.upper())

    def price_at_or_before(self, symbol: str, unix_ts: int) -> Optional[float]:
        """Approx historical price via Binance 1m klines, cached in history."""
        symbol = symbol.upper()
        with self._lock:
            hist = self.history.get(symbol, [])
            for ts, p in reversed(hist):
                if ts <= unix_ts:
                    return p
        # fallback: REST klines
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "1m",
                        "startTime": (unix_ts - 60) * 1000,
                        "endTime": (unix_ts + 60) * 1000, "limit": 5},
                timeout=5,
            )
            data = r.json()
            if data:
                open_price = float(data[0][1])  # open of the bar covering ts
                with self._lock:
                    self.history[symbol].append((unix_ts, open_price))
                    self.history[symbol] = self.history[symbol][-500:]
                return open_price
        except Exception as e:
            log.warning("kline fetch failed %s: %s", symbol, e)
        return None

    def _loop(self):
        streams = "/".join(f"{s}@ticker" for s in self.symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        backoff = 1
        while not self._stop.is_set():
            try:
                log.info("binance ws connecting (%s symbols)", len(self.symbols))
                with wsclient.connect(url, max_size=2_000_000, open_timeout=15) as ws:
                    backoff = 1
                    while not self._stop.is_set():
                        msg = json.loads(ws.recv(timeout=60))
                        data = msg.get("data", {})
                        sym = data.get("s")
                        price = float(data.get("c", 0)) if data.get("c") else None
                        if sym and price:
                            with self._lock:
                                self.prices[sym] = price
                                hist = self.history.setdefault(sym, [])
                                now_min = int(time.time() // 60) * 60
                                if not hist or hist[-1][0] != now_min:
                                    hist.append((now_min, price))
                                    self.history[sym] = hist[-500:]
            except Exception as e:
                log.warning("binance ws err: %s — retry %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


# ─── Polymarket market discovery ───────────────────────────────────────────

SYMBOL_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "XRPUSDT": ["xrp"],
}
SLUG_RE = re.compile(r"(btc|eth|sol|xrp|bnb|doge|hype)-updown-(\d+)m-(\d+)")


def discover_updown_markets(symbols: list[str]) -> list[dict]:
    """Return LIVE 5-min Up/Down markets by constructing slugs for the current
    and last few 5-min buckets (the list endpoint sorts them out of reach)."""
    now = int(time.time())
    prefix_map = {
        "BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol", "XRPUSDT": "xrp",
        "BNBUSDT": "bnb", "DOGEUSDT": "doge", "HYPEUSDT": "hype",
    }

    # Build slugs for both 5-min and 15-min duration windows, across the
    # current and last 2 buckets (latter in case a market is still resolving).
    DURATIONS = [(5, 300), (15, 900)]
    slugs = []
    for dur_min, dur_s in DURATIONS:
        anchor = (now // dur_s) * dur_s
        for sym in symbols:
            prefix = prefix_map.get(sym)
            if not prefix: continue
            for i in range(0, 3):
                b = anchor - i * dur_s
                slugs.append((sym, prefix, b, f"{prefix}-updown-{dur_min}m-{b}"))

    markets = []
    for sym, prefix, bucket_start, slug in slugs:
        for attempt in range(2):
            try:
                r = requests.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"slug": slug},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
                )
                d = r.json() or []
                if d:
                    markets.append(d[0])
                break
            except Exception as e:
                if attempt == 1:
                    log.warning("gamma slug fetch failed %s: %s", slug, e)
                time.sleep(0.3)
    out = []

    for m in markets:
        slug = m.get("slug", "") or ""
        q = m.get("question", "") or ""
        match = SLUG_RE.match(slug)
        if not match:
            continue
        prefix, dur_min, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
        symbol = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
                  "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT",
                  "hype": "HYPEUSDT"}.get(prefix)
        if not symbol or symbol not in symbols:
            continue
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
        except Exception:
            continue
        if len(token_ids) != 2 or len(outcomes) != 2:
            continue
        up_idx = 0 if outcomes[0] == "Up" else 1
        dn_idx = 1 - up_idx
        up_price = prices[up_idx] if len(prices) > up_idx else 0.5
        dn_price = prices[dn_idx] if len(prices) > dn_idx else 0.5
        out.append({
            "conditionId": m.get("conditionId"),
            "question": q, "slug": slug, "symbol": symbol,
            "start_ts": start_ts,
            "end_ts": start_ts + dur_min * 60,
            "duration_min": dur_min,
            "up_token": token_ids[up_idx],
            "dn_token": token_ids[dn_idx],
            "up_last": up_price,
            "dn_last": dn_price,
        })
    return out


def get_orderbook_snapshot(client: ClobClient, token_id: str):
    """Fetch full book snapshot once; engine derives ask + imbalance from it."""
    try:
        return client.get_order_book(token_id)
    except Exception as e:
        log.warning("orderbook fetch failed %s: %s", token_id[:10], e)
        return None


def get_orderbook_ask(client: ClobClient, token_id: str) -> Optional[float]:
    book = get_orderbook_snapshot(client, token_id)
    if book is None:
        return None
    asks = getattr(book, "asks", []) or []
    if not asks:
        return None
    return min(float(a.price) for a in asks)


def get_orderbook_bid(client: ClobClient, token_id: str) -> Optional[float]:
    """Fetch best bid from CLOB for a token."""
    try:
        book = client.get_order_book(token_id)
        bids = book.bids
        if not bids:
            return None
        return max(float(b.price) for b in bids)
    except Exception as e:
        log.warning("orderbook bid fetch failed %s: %s", token_id[:10], e)
        return None


_tp_recent_sells: dict[str, float] = {}  # token_id -> last sell timestamp


def scan_take_profits(client: ClobClient, funder_address: str, cfg: dict) -> None:
    """For each open position with bid >= take_profit_price, market-sell it.
    Locks in gains rather than holding to binary resolution. Dedupes attempts
    within a cooldown window so Polymarket's stale positions endpoint doesn't
    cause repeated "insufficient balance" failures."""
    tp_threshold = float(cfg.get("take_profit_price", 0.92))
    cooldown_s = float(cfg.get("tp_cooldown_s", 90))
    dry = cfg.get("dry_run", True)
    if tp_threshold <= 0 or tp_threshold >= 1.0:
        return
    # prune cooldown
    now = time.time()
    for tid in list(_tp_recent_sells):
        if now - _tp_recent_sells[tid] > cooldown_s:
            del _tp_recent_sells[tid]
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "limit": 50},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
        positions = r.json() or []
    except Exception as e:
        log.warning("tp positions fetch: %s", e)
        return

    for p in positions:
        size = float(p.get("size") or 0)
        cur = float(p.get("curPrice") or 0)
        if size < 5 or cur < tp_threshold or cur >= 1.0:
            continue
        token_id = p.get("asset")
        if not token_id:
            continue
        if token_id in _tp_recent_sells:
            continue  # recently attempted, skip while positions endpoint catches up
        bid = get_orderbook_bid(client, token_id)
        if bid is None or bid < tp_threshold:
            continue
        sell_size = size
        title = (p.get("title") or "")[:40]

        if dry:
            log.info("[TP-DRY] would sell %s size=%.2f @%.3f (invested=$%.2f now=$%.2f)",
                     title, sell_size, bid, p.get("initialValue") or 0, p.get("currentValue") or 0)
            continue

        try:
            order = client.create_order(OrderArgs(
                token_id=str(token_id), price=bid, size=sell_size, side=SELL,
            ))
            resp = client.post_order(order)
            log.info("[TP-SELL] %s size=%.2f @%.3f → %s",
                     title, sell_size, bid, resp.get("status"))
            _tp_recent_sells[str(token_id)] = time.time()
        except Exception as e:
            # mark attempted even on failure so we don't hammer the endpoint
            _tp_recent_sells[str(token_id)] = time.time()
            log.warning("tp sell failed %s: %s", title, str(e)[:140])


# ─── Edge calculation ──────────────────────────────────────────────────────

def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_probability_up(
    spot_now: float, spot_at_open: float, seconds_remaining: float,
    sigma_per_min: float,
) -> float:
    """
    Probability that spot finishes >= open_price given current move and
    time remaining. Uses a Brownian-motion continuation model:
      P(up) = 1 - Φ( -move / (σ · sqrt(minutes_remaining)) )
    If at open price, returns 0.5. If far above with little time, → 1.0.
    """
    if seconds_remaining <= 0 or spot_at_open <= 0:
        return 0.5
    move = (spot_now - spot_at_open) / spot_at_open
    minutes_left = max(seconds_remaining / 60.0, 0.01)
    residual_std = sigma_per_min * math.sqrt(minutes_left)
    if residual_std <= 0:
        return 1.0 if move > 0 else 0.0
    z = move / residual_std
    return normal_cdf(z)


# ─── State/trade logging ───────────────────────────────────────────────────

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def append_trade(row: dict):
    header = ("timestamp,market,symbol,side,direction,fair_prob,ask,edge,"
              "price,size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "side", "direction",
            "fair_prob", "ask", "edge", "price", "size_usd",
            "token_id", "order_id", "status")) + "\n")


# ─── Main loop ─────────────────────────────────────────────────────────────

def main():
    load_dotenv(ROOT / ".env")
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    priv = os.environ["POLY_PRIVATE_KEY"]
    funder = os.environ.get("POLY_FUNDER")
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    client = ClobClient(
        host="https://clob.polymarket.com", key=priv, chain_id=POLYGON,
        signature_type=sig_type, funder=funder, creds=creds,
    )
    log.info("quant engine up. signer=%s funder=%s", client.get_address(), funder)

    cfg = load_json(CFG_FILE, {})
    feed = SpotFeed(cfg.get("symbols", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]))
    feed.start()

    # Secondary feeds: Coinbase (median spot), funding rates (positioning),
    # Polymarket fill flow (informed flow per market)
    try:
        from data_sources import CoinbaseSpotFeed, FundingRates, PolymarketFillFlow
        cb_feed = CoinbaseSpotFeed(cfg.get("symbols", []))
        cb_feed.start()
        funding = FundingRates(cfg.get("symbols", []))
        funding.start()
        flow = PolymarketFillFlow()
        log.info("aux feeds started (coinbase, funding, fill-flow)")
    except Exception as e:
        log.warning("aux feeds disabled: %s", e)
        cb_feed = funding = flow = None

    # state
    opened_markets: dict[str, dict] = {}   # conditionId -> {entered_at, side, cost}
    seen_decisions: dict[str, float] = {}  # conditionId -> last decision ts

    loop_interval = 4.0
    last_market_scan = 0.0
    last_tp_scan = 0.0
    market_cache: list[dict] = []
    session_peak_usdc: Optional[float] = None
    session_halted = False
    # Circuit breaker: pause new orders for cooldown_s after peak-drawdown ≥ X
    cooloff_until: float = 0.0
    last_cash_check: float = 0.0

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        min_edge = float(cfg.get("min_edge", 0.05))
        max_pos = float(cfg.get("max_position_usd", 50.0))
        max_open = int(cfg.get("max_open_positions", 10))
        min_time = float(cfg.get("min_time_remaining_s", 120))
        max_time = float(cfg.get("max_time_remaining_s", 280))
        sigmas = cfg.get("volatility_per_min", {})

        now = time.time()

        # rescan markets every 30s
        if now - last_market_scan > 30:
            market_cache = discover_updown_markets(cfg.get("symbols", []))
            last_market_scan = now

        # Scan for take-profit opportunities every 10s
        if now - last_tp_scan > 10:
            try:
                scan_take_profits(client, funder or wallet, cfg)
            except Exception as e:
                log.warning("tp scan err: %s", e)
            last_tp_scan = now

        # Trailing-peak stop-loss with hysteresis. Baseline rises with cash
        # (wins get locked in). Halt fires when drawdown from peak exceeds
        # max_daily_loss_usd. Halt auto-releases when drawdown recovers to
        # less than half the threshold (hysteresis, prevents flap).
        max_session_loss = float(cfg.get("max_daily_loss_usd", 150))
        unhalt_threshold = max_session_loss * 0.5
        try:
            bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            cur_usdc = float(bal.get("balance", 0)) / 1_000_000
            if session_peak_usdc is None or cur_usdc > session_peak_usdc:
                was_none = session_peak_usdc is None
                session_peak_usdc = cur_usdc
                if was_none:
                    log.info("[SESSION] peak=$%.2f max_loss=$%.2f (trailing)",
                             session_peak_usdc, max_session_loss)
            drawdown = session_peak_usdc - cur_usdc
            if not session_halted and drawdown >= max_session_loss:
                log.warning("[HALT] drawdown=$%.2f >= $%.2f (peak=$%.2f, cash=$%.2f)",
                            drawdown, max_session_loss, session_peak_usdc, cur_usdc)
                session_halted = True
            elif session_halted and drawdown < unhalt_threshold:
                log.info("[UNHALT] drawdown recovered to $%.2f (< $%.2f), resuming",
                         drawdown, unhalt_threshold)
                session_halted = False
        except Exception:
            cur_usdc = None

        # heartbeat
        open_count = len(opened_markets)
        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_positions": open_count,
            "markets_tracked": len(market_cache),
            "spot": {s: feed.get(s) for s in ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]},
        })

        if not market_cache:
            time.sleep(loop_interval); continue

        # diagnostic: how many markets are in time window
        in_window = sum(1 for m in market_cache
                        if min_time <= (m["end_ts"] - now) <= max_time)
        if in_window > 0 and int(now) % 20 < 4:
            log.info("[LOOP] %d markets tracked, %d in window [%ds-%ds]",
                     len(market_cache), in_window, int(min_time), int(max_time))

        for m in market_cache:
            cid = m["conditionId"]
            time_rem = m["end_ts"] - now
            if time_rem < min_time or time_rem > max_time:
                continue
            if cid in opened_markets:
                continue
            if now - seen_decisions.get(cid, 0) < 20:
                continue  # recent decision, wait
            seen_decisions[cid] = now

            symbol = m["symbol"]
            # Median of Binance + Coinbase for more robust reference
            spot_binance = feed.get(symbol)
            spot_cb = cb_feed.get(symbol) if cb_feed else None
            try:
                from data_sources import combined_spot
                spot_now = combined_spot(spot_binance, spot_cb)
            except Exception:
                spot_now = spot_binance
            spot_open = feed.price_at_or_before(symbol, m["start_ts"])
            if not spot_now or not spot_open:
                log.info("[SKIP-SPOT] %s spot_now=%s spot_open=%s", m["slug"], spot_now, spot_open)
                continue
            # Rolling sigma from realized 1-min returns, falls back to config
            rolling = feed.rolling_sigma(symbol)
            sigma = rolling if rolling else sigmas.get(symbol, 0.0015)
            fair_up = fair_probability_up(spot_now, spot_open, time_rem, sigma)

            # decide which side is mispriced
            # note: up_ask / dn_ask are last trade prices from gamma; pull real book
            up_ask = get_orderbook_ask(client, m["up_token"])
            dn_ask = get_orderbook_ask(client, m["dn_token"])
            if up_ask is None or dn_ask is None:
                log.info("[SKIP-BOOK] %s up_ask=%s dn_ask=%s", m["slug"], up_ask, dn_ask)
                continue

            # Skip asks that sum to < 0.95 — that means the BOOK itself is
            # pricing both sides cheap, which is an arb opportunity for market-
            # makers, not for takers. Usually happens during market creation.
            if (up_ask + dn_ask) < 0.95:
                log.info("[SKIP-STUB] %s up=%.3f+dn=%.3f=%.3f (stub book)",
                         m["slug"], up_ask, dn_ask, up_ask+dn_ask)
                continue

            edge_up = fair_up - up_ask
            edge_dn = (1.0 - fair_up) - dn_ask
            move_bps = (spot_now / spot_open - 1) * 10_000

            # Require spot to have ALREADY moved in the direction we're taking.
            # Without this, cheap Up asks (e.g. $0.07) when spot is flat are
            # informed-seller signals, not mispriced liquidity. Sprint 1
            # (2026-04-13) lost –$195 buying all 4 "cheap Ups" when spot
            # went down. Signal must align with existing spot drift.
            min_align_bps = float(cfg.get("min_spot_align_bps", 15))

            # Fill-flow veto: if a lot of recent taker flow is going against
            # our intended direction in this market, that's informed flow.
            # Require 60s window net flow to not contradict >2× our size.
            flow_veto_usd = float(cfg.get("flow_veto_usd", 50))

            # Trend-reversal guard — if last-60s spot move is opposite the
            # window-total move, market is reversing. Skip the trade.
            # (Saved us would have been: when spot is up +8bps over 2min but
            # last minute was -5bps, v1 used to still buy Up → lost on reversal.)
            recent_move_bps = None
            try:
                recent_spot_start = feed.price_at_or_before(symbol, int(now - 60))
                if recent_spot_start:
                    recent_move_bps = (spot_now / recent_spot_start - 1) * 10_000
            except Exception:
                pass

            # INVERTED MODE — v1's signal has been systematically wrong all session.
            # When the model says BUY UP, we BUY DN instead (and vice versa).
            # If the model truly has anti-edge, this captures the inverse.
            invert = bool(cfg.get("invert_signal", True))

            direction = None
            reversal_veto = False
            if edge_up >= min_edge and edge_up > edge_dn and move_bps >= min_align_bps:
                if recent_move_bps is not None and recent_move_bps <= -5:
                    reversal_veto = True
                else:
                    if invert:
                        # signal says UP — fade it
                        direction, token, ask, fair = "DN", m["dn_token"], dn_ask, 1 - fair_up
                    else:
                        direction, token, ask, fair = "UP", m["up_token"], up_ask, fair_up
            elif edge_dn >= min_edge and move_bps <= -min_align_bps:
                if recent_move_bps is not None and recent_move_bps >= 5:
                    reversal_veto = True
                else:
                    if invert:
                        # signal says DN — fade it
                        direction, token, ask, fair = "UP", m["up_token"], up_ask, fair_up
                    else:
                        direction, token, ask, fair = "DN", m["dn_token"], dn_ask, 1 - fair_up
            if reversal_veto:
                log.info("[REVERSAL-VETO] %s window_move=%+.0fbps last60s=%+.0fbps (against)",
                         m["slug"], move_bps, recent_move_bps)

            # Secondary info: funding rate + fill flow (logged for now, not yet
            # used to gate, so we can see if signal adds value before wiring)
            funding_rate = funding.get(symbol) if funding else None
            flow_buy_usd, flow_sell_usd = (0.0, 0.0)
            if flow and direction:
                flow_buy_usd, flow_sell_usd = flow.net_flow_last_seconds(cid, 60)

            log.info(
                "[SCAN] %s spot=%.2f→%.2f move=%+.3f%% t_rem=%.0fs fair_up=%.3f "
                "up_ask=%.3f edge_up=%+.3f dn_ask=%.3f edge_dn=%+.3f %s",
                m["slug"], spot_open, spot_now, (spot_now/spot_open-1)*100,
                time_rem, fair_up, up_ask, edge_up, dn_ask, edge_dn,
                direction or "(no-edge)",
            )

            if direction is None:
                continue

            log.info(
                "[EDGE] %s %s t_rem=%.0fs spot=%.2f→%.2f move=%+.3f%% fair_up=%.3f "
                "up_ask=%.3f dn_ask=%.3f → %s edge=%.3f fund=%s flow60s=+$%.0f/-$%.0f",
                m["slug"], symbol, time_rem, spot_open, spot_now,
                (spot_now/spot_open-1)*100, fair_up, up_ask, dn_ask, direction,
                max(edge_up, edge_dn),
                f"{funding_rate*10000:+.1f}bp" if funding_rate is not None else "—",
                flow_buy_usd, flow_sell_usd,
            )

            if not enabled:
                continue
            if session_halted:
                log.info("[SKIP] session halted — max loss hit")
                continue
            # Cross-engine direction concurrency cap (sister of v3's)
            try:
                from data_sources import count_open_by_direction
                up_n, dn_n = count_open_by_direction(
                    os.environ.get("POLY_FUNDER", "")
                )
                max_per_dir = int(cfg.get("max_per_direction", 4))
                cur_n = up_n if direction == "UP" else dn_n
                if cur_n >= max_per_dir:
                    log.info("[DIR-CAP] %s side has %d open (cap %d), skipping",
                             direction, cur_n, max_per_dir)
                    continue
            except Exception:
                pass
            # Drawdown circuit breaker: pause 5 min if down >15% from peak
            if session_peak_usdc and cur_usdc is not None:
                dd_pct = (session_peak_usdc - cur_usdc) / max(session_peak_usdc, 1)
                if dd_pct > 0.15 and now > cooloff_until:
                    cooloff_until = now + 300
                    log.warning("[COOLOFF] drawdown %.1f%% from peak; pausing 5 min", dd_pct*100)
            if now < cooloff_until:
                continue
            if open_count >= max_open:
                log.info("[SKIP] open cap reached"); continue

            # If trade passes filters (spot-aligned + edge ≥ min + non-stub
            # book), size at full max_pos — we've already gated for conviction.
            size_shares = max_pos / max(ask, 0.01)
            size_shares = max(size_shares, 5.0)  # Polymarket min
            our_usd = size_shares * ask

            if dry:
                log.info("[DRY] would buy %s %s size=%.2f @%.3f ≈$%.2f",
                         m["slug"], direction, size_shares, ask, our_usd)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": symbol,
                    "side": m["question"], "direction": direction,
                    "fair_prob": round(fair, 4), "ask": ask,
                    "edge": round(max(edge_up, edge_dn), 4),
                    "price": ask, "size_usd": round(our_usd, 2),
                    "token_id": token, "order_id": "DRY", "status": "dry",
                })
                opened_markets[cid] = {"entered_at": now, "direction": direction,
                                       "cost": our_usd}
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[BUY] %s %s size=%.2f @%.3f ≈$%.2f → %s",
                         m["slug"], direction, size_shares, ask, our_usd,
                         resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": symbol,
                    "side": m["question"], "direction": direction,
                    "fair_prob": round(fair, 4), "ask": ask,
                    "edge": round(max(edge_up, edge_dn), 4),
                    "price": ask, "size_usd": round(our_usd, 2),
                    "token_id": token, "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
                opened_markets[cid] = {"entered_at": now, "direction": direction,
                                       "cost": our_usd}
            except Exception as e:
                log.exception("order failed: %s", e)

        # purge resolved markets from opened_markets tracking
        opened_markets = {cid: v for cid, v in opened_markets.items()
                          if any(m["conditionId"] == cid for m in market_cache)}

        time.sleep(loop_interval)

    feed.stop()
    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("quant engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
