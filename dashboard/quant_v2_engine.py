"""Quant v2 — momentum-burst continuation.

Hypothesis: when BTC/ETH/SOL/XRP/BNB/DOGE makes a sharp move (>= N bps)
in the last 60-120 seconds, the move continues for the rest of a 5-min
window. Buy the direction of the burst at current ask.

Differences from v1:
  - No Brownian/fair-prob model; signal is pure realized momentum
  - No "cheap side" temptation — we pay current ask, which may be $0.70+
  - Skips opening 60s of every market (avoids stub-book trap)
  - Requires tight spread (ask + bid_opp sum close to $1) = real book
  - One bet per (market, direction); no re-entries
  - Separate log/state/trades so we can A/B vs v1

Shares infra (py-clob-client, Binance WS) but isolated state + config.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import websockets.sync.client as wsclient
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
CFG_FILE = RUNTIME / "quant_v2_config.json"
STATE_FILE = RUNTIME / "quant_v2_state.json"
TRADES_FILE = RUNTIME / "quant_v2_trades.csv"
LOG_FILE = RUNTIME / "quant_v2.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("quant_v2")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


# ─── Spot feed with rolling price history ──────────────────────────────────

class SpotFeed:
    def __init__(self, symbols: list[str]):
        self.symbols = [s.lower() for s in symbols]
        self.prices: dict[str, float] = {}
        # (unix_ts_seconds, price) samples, kept last ~15 min per symbol
        self.samples: dict[str, list[tuple[int, float]]] = {s.upper(): [] for s in symbols}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="binance-v2").start()

    def stop(self): self._stop.set()

    def get(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(symbol.upper())

    def momentum_bps(self, symbol: str, lookback_s: int) -> Optional[float]:
        """Realized move in bps over the last `lookback_s` seconds."""
        symbol = symbol.upper()
        now = int(time.time())
        with self._lock:
            hist = self.samples.get(symbol, [])
            if len(hist) < 5:
                return None
            past = [p for ts, p in hist if ts >= now - lookback_s]
            if not past:
                return None
            anchor = past[0]
            current = hist[-1][1]
            if anchor <= 0:
                return None
            return (current / anchor - 1) * 10_000

    def _loop(self):
        streams = "/".join(f"{s}@ticker" for s in self.symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        backoff = 1
        while not self._stop.is_set():
            try:
                with wsclient.connect(url, max_size=2_000_000, open_timeout=15) as ws:
                    backoff = 1
                    while not self._stop.is_set():
                        msg = json.loads(ws.recv(timeout=60))
                        d = msg.get("data", {})
                        sym, c = d.get("s"), d.get("c")
                        if sym and c:
                            now = int(time.time())
                            with self._lock:
                                self.prices[sym] = float(c)
                                samples = self.samples.setdefault(sym, [])
                                samples.append((now, float(c)))
                                # keep last 15 min
                                cutoff = now - 900
                                self.samples[sym] = [s for s in samples if s[0] >= cutoff]
            except Exception as e:
                log.warning("binance ws err: %s — retry %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


# ─── Market discovery (same slug pattern as v1) ────────────────────────────

SLUG_RE = re.compile(r"(btc|eth|sol|xrp|bnb|doge|hype)-updown-(\d+)m-(\d+)")
PREFIX_TO_SYM = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT",
    "bnb": "BNBUSDT", "doge": "DOGEUSDT", "hype": "HYPEUSDT",
}


def discover_markets(symbols: list[str]) -> list[dict]:
    now = int(time.time())
    DURATIONS = [(5, 300), (15, 900)]
    slugs = []
    sym_to_prefix = {v: k for k, v in PREFIX_TO_SYM.items()}
    for dur_min, dur_s in DURATIONS:
        anchor = (now // dur_s) * dur_s
        for sym in symbols:
            pfx = sym_to_prefix.get(sym)
            if not pfx: continue
            for i in range(0, 3):
                b = anchor - i * dur_s
                slugs.append(f"{pfx}-updown-{dur_min}m-{b}")

    markets = []
    for slug in slugs:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            d = r.json() or []
            if d:
                markets.append(d[0])
        except Exception:
            pass

    out = []
    for m in markets:
        slug = m.get("slug") or ""
        match = SLUG_RE.match(slug)
        if not match:
            continue
        prefix, dur_min, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
        sym = PREFIX_TO_SYM.get(prefix)
        if not sym or sym not in symbols:
            continue
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
        except Exception:
            continue
        if len(tokens) != 2 or len(outcomes) != 2:
            continue
        up_idx = 0 if outcomes[0] == "Up" else 1
        out.append({
            "conditionId": m.get("conditionId"), "slug": slug, "symbol": sym,
            "start_ts": start_ts, "end_ts": start_ts + dur_min * 60,
            "duration_min": dur_min,
            "up_token": tokens[up_idx], "dn_token": tokens[1 - up_idx],
        })
    return out


def best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = book.asks
        return min(float(a.price) for a in asks) if asks else None
    except Exception:
        return None


# ─── State / trade log ─────────────────────────────────────────────────────

def load_json(p: Path, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def save_json(p: Path, d):
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=2, default=str))
    tmp.replace(p)


def append_trade(row: dict):
    header = ("timestamp,market,symbol,direction,momentum_bps,ask,size_shares,"
              "size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "direction", "momentum_bps",
            "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


# ─── Main loop ─────────────────────────────────────────────────────────────

def main():
    load_dotenv(ROOT / ".env")
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=os.environ["POLY_PRIVATE_KEY"],
        chain_id=POLYGON,
        signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "0")),
        funder=os.environ.get("POLY_FUNDER"),
        creds=creds,
    )
    log.info("quant_v2 engine up. funder=%s", os.environ.get("POLY_FUNDER"))

    cfg0 = load_json(CFG_FILE, {})
    feed = SpotFeed(cfg0.get("symbols", ["BTCUSDT", "ETHUSDT", "SOLUSDT",
                                         "XRPUSDT", "BNBUSDT", "DOGEUSDT"]))
    feed.start()

    opened_markets: dict[tuple, float] = {}  # (cid, direction) -> entered_at
    last_scan = 0.0
    market_cache: list[dict] = []

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        burst_bps = float(cfg.get("momentum_burst_bps", 25))
        lookback_s = int(cfg.get("momentum_lookback_s", 90))
        skip_open_s = int(cfg.get("skip_opening_seconds", 60))
        max_ask = float(cfg.get("max_ask", 0.85))
        min_book_sum = float(cfg.get("min_book_sum", 0.95))
        max_pos = float(cfg.get("max_position_usd", 100))
        max_open = int(cfg.get("max_open_positions", 10))

        now = time.time()
        if now - last_scan > 20:
            market_cache = discover_markets(cfg.get("symbols", []))
            last_scan = now

        save_json(STATE_FILE, {
            "variant": "v2-momentum-burst",
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(opened_markets),
            "markets_tracked": len(market_cache),
            "spot": {s: feed.get(s) for s in feed.samples.keys()},
            "momentum_bps": {s: feed.momentum_bps(s, lookback_s)
                             for s in feed.samples.keys()},
        })

        if not market_cache:
            time.sleep(3); continue

        for m in market_cache:
            cid = m["conditionId"]
            symbol = m["symbol"]
            time_since_open = now - m["start_ts"]
            time_rem = m["end_ts"] - now
            # skip opening noise; also skip if too close to resolution
            if time_since_open < skip_open_s or time_rem < 30:
                continue

            mom = feed.momentum_bps(symbol, lookback_s)
            if mom is None:
                continue
            if abs(mom) < burst_bps:
                continue

            direction = "UP" if mom > 0 else "DN"
            if (cid, direction) in opened_markets:
                continue

            token = m["up_token"] if direction == "UP" else m["dn_token"]
            other_token = m["dn_token"] if direction == "UP" else m["up_token"]
            ask = best_ask(client, token)
            other_ask = best_ask(client, other_token)
            if ask is None or other_ask is None:
                continue
            # Require tight book: ask + other_ask should be close to $1
            book_sum = ask + other_ask
            if book_sum < min_book_sum or book_sum > 1.05:
                continue
            # We accept mid-to-high prices because the thesis is momentum
            # continuation; pure-cheap prices would be stub/info asymmetry.
            if ask > max_ask or ask < 0.15:
                continue

            size_shares = max_pos / max(ask, 0.01)
            size_shares = max(size_shares, 5.0)
            our_usd = size_shares * ask

            log.info("[BURST] %s mom_%ds=%+.0fbps → %s @%.3f size=%.2f (≈$%.2f) book_sum=%.3f",
                     m["slug"], lookback_s, mom, direction, ask, size_shares, our_usd, book_sum)

            if not enabled:
                continue
            if len(opened_markets) >= max_open:
                log.info("[SKIP-V2] open cap reached")
                continue
            opened_markets[(cid, direction)] = now

            if dry:
                log.info("[DRY-V2] would buy %s %s size=%.2f @%.3f",
                         m["slug"], direction, size_shares, ask)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": symbol, "direction": direction,
                    "momentum_bps": round(mom, 1), "ask": ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=str(token), price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[BUY-V2] %s %s size=%.2f @%.3f ≈$%.2f → %s",
                         m["slug"], direction, size_shares, ask, our_usd,
                         resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": symbol, "direction": direction,
                    "momentum_bps": round(mom, 1), "ask": ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("v2 order failed: %s", e)

        # purge old opens (5m+15m max 900s)
        opened_markets = {k: t for k, t in opened_markets.items()
                          if now - t < 1200}

        time.sleep(3)

    feed.stop()
    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("quant_v2 engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
