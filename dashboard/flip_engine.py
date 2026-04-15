"""Flip sniper — buy the CHEAP side on last-minute reversals.

Pattern: 5m Up/Down market has strong early move (say +40bps UP) so DN is
priced at 0.05-0.15. In the final 60s spot flips to -15bps so DN is now
the real winner. DN ask sits at its old cheap price for 1-5 seconds before
market-makers lift it. We buy during that window.

Win condition: we hold DN token from 0.10, resolves to $1.00 → ~10x payout.
Edge: only fire when flip magnitude >= flip_bps AND new-winner ask still cheap.

Tick loop runs every 300ms for low latency.
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
from collections import deque
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
CFG_FILE = RUNTIME / "flip_config.json"
STATE_FILE = RUNTIME / "flip_state.json"
TRADES_FILE = RUNTIME / "flip_trades.csv"
LOG_FILE = RUNTIME / "flip.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("flip")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


SLUG_RE = re.compile(r"(btc|eth|sol|xrp|bnb|doge|hype)-updown-(\d+)m-(\d+)")
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT",
       "hype": "HYPEUSDT"}


class SpotFeed:
    def __init__(self, symbols: list[str]):
        self.symbols = [s.lower() for s in symbols]
        self.prices: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def get(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(symbol.upper())

    def kline_open(self, symbol: str, unix_ts: int) -> Optional[float]:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol.upper(), "interval": "1m",
                        "startTime": unix_ts * 1000,
                        "endTime": (unix_ts + 60) * 1000, "limit": 2},
                timeout=5,
            )
            data = r.json()
            if data:
                return float(data[0][1])
        except Exception as e:
            log.warning("kline %s: %s", symbol, e)
        return None

    def _loop(self):
        streams = "/".join(f"{s}@ticker" for s in self.symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        backoff = 1
        while not self._stop.is_set():
            try:
                with wsclient.connect(url, max_size=2_000_000, open_timeout=15) as ws:
                    backoff = 1
                    log.info("binance ws up")
                    while not self._stop.is_set():
                        msg = json.loads(ws.recv(timeout=60))
                        d = msg.get("data", {})
                        sym = d.get("s")
                        c = d.get("c")
                        if sym and c:
                            with self._lock:
                                self.prices[sym] = float(c)
            except Exception as e:
                log.warning("binance ws err: %s — retry %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


def discover_markets() -> list[dict]:
    now = int(time.time())
    sym_to_prefix = {v: k for k, v in SYM.items()}
    slugs = []
    for dur_min, dur_s in [(5, 300), (15, 900)]:
        anchor = (now // dur_s) * dur_s
        for sym in SYM.values():
            pfx = sym_to_prefix.get(sym)
            if not pfx: continue
            for i in range(0, 2):
                slugs.append(f"{pfx}-updown-{dur_min}m-{anchor - i * dur_s}")
    ms = []
    for slug in slugs:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            d = r.json() or []
            if d: ms.append(d[0])
        except Exception:
            pass
    out = []
    for m in ms:
        slug = m.get("slug", "") or ""
        match = SLUG_RE.match(slug)
        if not match: continue
        prefix, dur_min, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
        if dur_min not in (5, 15): continue
        sym = SYM.get(prefix)
        if not sym: continue
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
        except Exception:
            continue
        if len(tokens) != 2 or len(outcomes) != 2: continue
        up_idx = 0 if outcomes[0] == "Up" else 1
        out.append({
            "conditionId": m.get("conditionId"), "slug": slug, "symbol": sym,
            "start_ts": start_ts, "end_ts": start_ts + dur_min * 60,
            "up_token": tokens[up_idx], "dn_token": tokens[1 - up_idx],
        })
    return out


def best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = getattr(book, "asks", []) or []
        if not asks: return None
        return min(float(a.price) for a in asks)
    except Exception as e:
        log.warning("book %s: %s", token_id[:10], e)
        return None


def load_json(p, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def save_json(p, d):
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(p)
    except FileNotFoundError:
        pass


def append_trade(row):
    header = ("timestamp,market,symbol,direction,peak_bps,flip_bps,curr_bps,ask,"
              "size_shares,size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "direction", "peak_bps", "flip_bps",
            "curr_bps", "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


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
    symbols = list(SYM.values())
    feed = SpotFeed(symbols); feed.start()
    log.info("flip engine up. funder=%s", os.environ.get("POLY_FUNDER"))

    # per-market rolling bps history (cid -> deque of (ts, bps))
    history: dict[str, deque] = {}
    # per-bucket kline cache
    spot_open_cache: dict = {}
    fired: dict = {}  # cid -> entered_at
    markets: list = []
    last_discover = 0.0

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        t_min = float(cfg.get("t_rem_min_s", 5))
        t_max = float(cfg.get("t_rem_max_s", 60))
        flip_bps_threshold = float(cfg.get("flip_bps", 30))
        min_peak_bps = float(cfg.get("min_peak_bps", 20))
        max_ask = float(cfg.get("max_ask", 0.25))
        min_ask = float(cfg.get("min_ask", 0.03))
        max_pos = float(cfg.get("max_position_usd", 30))
        max_open = int(cfg.get("max_open_positions", 5))
        history_window_s = float(cfg.get("history_window_s", 90))

        now = time.time()
        if now - last_discover > 20:
            markets = discover_markets()
            last_discover = now

        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(fired),
            "markets_tracked": len(markets),
            "spot": {s: feed.get(s) for s in symbols},
        })

        if not markets:
            time.sleep(1); continue

        scan_count = flips = 0
        for m in markets:
            cid = m["conditionId"]
            t_rem = m["end_ts"] - now
            if t_rem < t_min or t_rem > t_max + 60:
                continue
            scan_count += 1

            spot_now = feed.get(m["symbol"])
            spot_open = spot_open_cache.get(m["start_ts"], {}).get(m["symbol"])
            if spot_open is None:
                spot_open = feed.kline_open(m["symbol"], m["start_ts"])
                if spot_open:
                    spot_open_cache.setdefault(m["start_ts"], {})[m["symbol"]] = spot_open
            if not spot_now or not spot_open:
                continue

            curr_bps = (spot_now / spot_open - 1) * 10_000

            h = history.setdefault(cid, deque())
            h.append((now, curr_bps))
            # prune old entries
            while h and now - h[0][0] > history_window_s:
                h.popleft()

            if cid in fired:
                continue
            if t_rem > t_max:
                continue  # in tracking window but not yet in fire window

            # detect flip: peak of opposite sign in last history_window
            if len(h) < 5:
                continue
            peak_up = max((b for _, b in h), default=0)
            peak_dn = min((b for _, b in h), default=0)

            # Flip UP→DN: was strongly positive, now negative
            if peak_up >= min_peak_bps and curr_bps < -5 and (peak_up - curr_bps) >= flip_bps_threshold:
                direction, token = "DN", m["dn_token"]
                peak_bps = peak_up
            # Flip DN→UP: was strongly negative, now positive
            elif peak_dn <= -min_peak_bps and curr_bps > 5 and (curr_bps - peak_dn) >= flip_bps_threshold:
                direction, token = "UP", m["up_token"]
                peak_bps = peak_dn
            else:
                continue

            flips += 1
            ask = best_ask(client, token)
            if ask is None:
                log.info("[FLIP-NOASK] %s %s t_rem=%.0fs peak=%+.0f curr=%+.0f",
                         m["slug"], direction, t_rem, peak_bps, curr_bps)
                continue
            if ask < min_ask or ask > max_ask:
                log.info("[FLIP-SKIP] %s %s t_rem=%.0fs peak=%+.0f curr=%+.0f ask=%.3f (want %.2f-%.2f)",
                         m["slug"], direction, t_rem, peak_bps, curr_bps, ask, min_ask, max_ask)
                continue

            size_shares = max(max_pos / ask, 5.0)
            our_usd = size_shares * ask
            if our_usd > max_pos:
                size_shares = max_pos / ask
                our_usd = size_shares * ask

            flip_mag = abs(peak_bps - curr_bps)
            log.info("[FLIP] %s %s t_rem=%.0fs peak=%+.0f curr=%+.0f swing=%.0fbps "
                     "ask=%.3f size=%.2f ($%.2f)",
                     m["slug"], direction, t_rem, peak_bps, curr_bps, flip_mag,
                     ask, size_shares, our_usd)

            if not enabled:
                continue
            if len(fired) >= max_open:
                log.info("[FLIP-CAP] max_open=%d reached", max_open)
                continue
            fired[cid] = now

            if dry:
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "peak_bps": round(peak_bps, 1), "flip_bps": round(flip_mag, 1),
                    "curr_bps": round(curr_bps, 1), "ask": ask,
                    "size_shares": round(size_shares, 2), "size_usd": round(our_usd, 2),
                    "token_id": token, "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[FLIP-BUY] %s %s ≈$%.2f @ %.3f → %s",
                         m["slug"], direction, our_usd, ask, resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "peak_bps": round(peak_bps, 1), "flip_bps": round(flip_mag, 1),
                    "curr_bps": round(curr_bps, 1), "ask": ask,
                    "size_shares": round(size_shares, 2), "size_usd": round(our_usd, 2),
                    "token_id": token, "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("flip order failed: %s", e)

        if scan_count > 0 and flips == 0:
            # one-line heartbeat of what we're watching
            samples = []
            for m in markets[:4]:
                cid = m["conditionId"]
                h = history.get(cid)
                if not h: continue
                bps_vals = [b for _, b in h]
                if bps_vals:
                    samples.append(f"{m['symbol'][:3]}:{bps_vals[-1]:+.0f}(pk{max(bps_vals):+.0f}/{min(bps_vals):+.0f})")
            if samples:
                log.info("[FLIP-WATCH] t=%.0fs live=%d  %s",
                         min(m["end_ts"] - now for m in markets if m["end_ts"] - now > 0),
                         scan_count, " ".join(samples))

        # purge fired set after bucket resolution
        fired = {k: t for k, t in fired.items() if now - t < 400}
        # purge history for resolved buckets
        history = {cid: h for cid, h in history.items()
                   if any(now - ts < 600 for ts, _ in h)}

        time.sleep(0.3)  # tick every 300ms for low latency

    feed.stop()
    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("flip engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
