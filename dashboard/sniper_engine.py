"""Resolution sniper — buy near-certain outcomes in last 30s of 5-min binaries.

Logic:
  For each open Up/Down 5-min market with <SNIPE_WINDOW_S left:
    1. Read spot from Binance feed (BTC/ETH/SOL/XRP)
    2. Compute spot move from window-open price
    3. If |move| > MIN_DIRECTIONAL_BPS, the winning side is "obvious"
    4. If winning side ask < TARGET_DISCOUNT (e.g. 0.985), buy it
    5. Hold to resolution → $1 payout
    6. Edge: $1 - ask, typically 1-3 cents on a 30s hold

Same py-clob-client + risk caps as the other engines. Separate state/log files.
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
CFG_FILE = RUNTIME / "sniper_config.json"
STATE_FILE = RUNTIME / "sniper_state.json"
TRADES_FILE = RUNTIME / "sniper_trades.csv"
LOG_FILE = RUNTIME / "sniper.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("sniper")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


# ─── Spot feed (reuses pattern from quant_engine) ─────────────────────────

class SpotFeed:
    def __init__(self, symbols: list[str]):
        self.symbols = [s.lower() for s in symbols]
        self.prices: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="binance-snipe").start()

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
                        "startTime": (unix_ts) * 1000,
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


# ─── Polymarket discovery ─────────────────────────────────────────────────

SLUG_RE = re.compile(r"(btc|eth|sol|xrp)-updown-(\d+)m-(\d+)")
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


def discover_markets() -> list[dict]:
    now = int(time.time())
    sym_to_prefix = {v: k for k, v in SYM.items()}
    DURATIONS = [(5, 300), (15, 900)]
    slugs = []
    for dur_min, dur_s in DURATIONS:
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
        except Exception as e:
            log.warning("gamma fetch %s: %s", slug, e)
    out = []
    for m in ms:
        slug = m.get("slug", "") or ""
        match = SLUG_RE.match(slug)
        if not match: continue
        prefix, dur, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
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
            "start_ts": start_ts, "end_ts": start_ts + dur * 60,
            "duration_min": dur,
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
        log.warning("book fetch %s: %s", token_id[:10], e)
        return None


# ─── State / trade log ────────────────────────────────────────────────────

def load_json(p: Path, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def save_json(p: Path, d):
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(p)
    except FileNotFoundError:
        pass


def append_trade(row: dict):
    header = ("timestamp,market,symbol,direction,move_pct,t_remaining_s,ask,size_shares,"
              "size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists(): TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "direction", "move_pct",
            "t_remaining_s", "ask", "size_shares", "size_usd",
            "token_id", "order_id", "status")) + "\n")


# ─── Main ─────────────────────────────────────────────────────────────────

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
    log.info("sniper engine up. funder=%s", os.environ.get("POLY_FUNDER"))

    feed = SpotFeed(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
    feed.start()

    snipe_window: dict[str, dict] = {}  # conditionId -> {entered_at}
    open_cache_ts: dict[tuple, float] = {}  # (symbol,start_ts) -> open_price

    last_scan = 0.0
    market_cache: list[dict] = []

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        snipe_window_s = float(cfg.get("snipe_window_s", 30))
        min_move_bps = float(cfg.get("min_directional_bps", 30))
        max_ask = float(cfg.get("max_ask", 0.985))
        max_pos = float(cfg.get("max_position_usd", 25))
        max_open = int(cfg.get("max_open_positions", 5))

        now = time.time()
        if now - last_scan > 15:
            market_cache = discover_markets()
            last_scan = now

        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_snipes": len(snipe_window),
            "markets_tracked": len(market_cache),
            "spot": {s: feed.get(s) for s in ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]},
        })

        if not market_cache:
            time.sleep(2); continue

        in_window = 0; no_spot = 0; below_move = 0
        for m in market_cache:
            cid = m["conditionId"]
            t_rem = m["end_ts"] - now
            if t_rem <= 5 or t_rem > snipe_window_s:
                continue
            in_window += 1
            if cid in snipe_window:
                continue

            spot_now = feed.get(m["symbol"])
            cache_key = (m["symbol"], m["start_ts"])
            spot_open = open_cache_ts.get(cache_key)
            if spot_open is None:
                spot_open = feed.kline_open(m["symbol"], m["start_ts"])
                if spot_open:
                    open_cache_ts[cache_key] = spot_open
            if not spot_now or not spot_open:
                no_spot += 1
                log.info("[SNIPE-NODATA] %s t_rem=%.0fs spot_now=%s spot_open=%s",
                         m["slug"], t_rem, spot_now, spot_open)
                continue

            move_bps = (spot_now / spot_open - 1) * 10_000
            if abs(move_bps) < min_move_bps:
                below_move += 1
                log.info("[SNIPE-WEAK] %s t_rem=%.0fs move=%+.0fbps (<%.0f)",
                         m["slug"], t_rem, move_bps, min_move_bps)
                continue

            direction = "UP" if move_bps > 0 else "DN"
            token = m["up_token"] if direction == "UP" else m["dn_token"]
            ask = best_ask(client, token)
            min_ask_bound = float(cfg.get("min_ask", 0.5))
            if ask is None:
                log.info("[SNIPE-SKIP] %s %s t_rem=%.0fs move=%+.0fbps no ask",
                         m["slug"], direction, t_rem, move_bps)
                continue
            if ask > max_ask or ask < min_ask_bound:
                log.info("[SNIPE-SKIP] %s %s t_rem=%.0fs move=%+.0fbps ask=%.3f out-of-band",
                         m["slug"], direction, t_rem, move_bps, ask)
                continue
            # Stale-price trap: ask at 0.95+ needs a STRONG move to justify,
            # otherwise we're paying 95¢ for 5¢ upside against 1-min noise.
            required_bps = max(min_move_bps, (ask - 0.80) * 200)
            if abs(move_bps) < required_bps:
                log.info("[SNIPE-STALE] %s %s t_rem=%.0fs move=%+.0fbps ask=%.3f need>=%.0fbps",
                         m["slug"], direction, t_rem, move_bps, ask, required_bps)
                continue

            edge = 1.0 - ask  # we get $1, paid ask
            target_usd = max_pos
            size_shares = max(target_usd / ask, 5.0)
            our_usd = size_shares * ask
            if our_usd > max_pos:
                size_shares = max_pos / ask
                our_usd = size_shares * ask

            log.info("[SNIPE] %s %s t_rem=%.0fs spot=%.2f→%.2f move=%+.0fbps "
                     "ask=%.3f edge=%.3f size=%.2f ($%.2f)",
                     m["slug"], direction, t_rem, spot_open, spot_now,
                     move_bps, ask, edge, size_shares, our_usd)

            if not enabled:
                continue
            if len(snipe_window) >= max_open:
                continue

            snipe_window[cid] = {"entered_at": now, "ask": ask}

            if dry:
                log.info("[DRY-SNIPE] would buy %s %s size=%.2f @%.3f",
                         m["slug"], direction, size_shares, ask)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "move_pct": round(move_bps/100, 3), "t_remaining_s": round(t_rem, 1),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[SNIPE-BUY] %s %s ≈$%.2f → %s",
                         m["slug"], direction, our_usd, resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "move_pct": round(move_bps/100, 3), "t_remaining_s": round(t_rem, 1),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("snipe order failed: %s", e)

        log.info("[SCAN] markets=%d in_window=%d no_spot=%d below_move=%d",
                 len(market_cache), in_window, no_spot, below_move)

        # purge old snipes (5-min market lifetime)
        snipe_window = {cid: v for cid, v in snipe_window.items()
                        if now - v["entered_at"] < 360}

        time.sleep(2)

    feed.stop()
    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("sniper engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
