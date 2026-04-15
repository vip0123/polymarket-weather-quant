"""Quant v3 — order-book imbalance scalper.

Hypothesis: heavy one-sided depth in the Polymarket book predicts the next
price tick. Separate signal from spot (v1) and spot-direction (sniper).

Logic:
  For each live 5-min Up/Down market with >30s remaining:
    1. Fetch book, compute imbalance [-1, +1] at top 3 levels
    2. If |imbalance| >= min_imbalance (e.g. 0.60):
       - Positive imbalance (bid-heavy) on Up side → buy Up
       - Positive on Down side → buy Down
       - (No fade logic for v3 simplicity; can extend later)
    3. Only trade when spread is tight (book_sum 0.95-1.05) and ask ≤ 0.70
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
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
CFG_FILE = RUNTIME / "quant_v3_config.json"
STATE_FILE = RUNTIME / "quant_v3_state.json"
TRADES_FILE = RUNTIME / "quant_v3_trades.csv"
LOG_FILE = RUNTIME / "quant_v3.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("quant_v3")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


# ─── Market discovery (same as v1/v2) ──────────────────────────────────────

SLUG_RE = re.compile(r"(btc|eth|sol|xrp|bnb|doge|hype)-updown-(\d+)m-(\d+)")
PREFIX_TO_SYM = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT",
    "bnb": "BNBUSDT", "doge": "DOGEUSDT", "hype": "HYPEUSDT",
}


def discover_markets(symbols: list[str]) -> list[dict]:
    now = int(time.time())
    sym_to_prefix = {v: k for k, v in PREFIX_TO_SYM.items()}
    DURATIONS = [(5, 300), (15, 900)]
    slugs = []
    for dur_min, dur_s in DURATIONS:
        anchor = (now // dur_s) * dur_s
        for sym in symbols:
            pfx = sym_to_prefix.get(sym)
            if not pfx: continue
            for i in range(0, 2):
                slugs.append(f"{pfx}-updown-{dur_min}m-{anchor - i * dur_s}")
    markets = []
    for slug in slugs:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            d = r.json() or []
            if d: markets.append(d[0])
        except Exception:
            pass
    out = []
    for m in markets:
        slug = m.get("slug") or ""
        match = SLUG_RE.match(slug)
        if not match: continue
        prefix, dur_min, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
        sym = PREFIX_TO_SYM.get(prefix)
        if not sym or sym not in symbols: continue
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


def compute_imbalance_and_asks(client: ClobClient, up_token: str, dn_token: str,
                               depth: int = 3):
    """Return (up_imbalance, dn_imbalance, up_ask, dn_ask) for both sides."""
    def parse(token_id):
        try:
            book = client.get_order_book(token_id)
            asks = getattr(book, "asks", []) or []
            bids = getattr(book, "bids", []) or []
            if not asks or not bids:
                return None, None
            top_asks = sorted(asks, key=lambda a: float(a.price))[:depth]
            top_bids = sorted(bids, key=lambda b: -float(b.price))[:depth]
            bid_sz = sum(float(b.size) for b in top_bids)
            ask_sz = sum(float(a.size) for a in top_asks)
            tot = bid_sz + ask_sz
            imb = ((bid_sz - ask_sz) / tot) if tot > 0 else 0.0
            best_ask = min(float(a.price) for a in top_asks)
            return imb, best_ask
        except Exception as e:
            log.warning("book fetch failed %s: %s", token_id[:10], e)
            return None, None
    up_imb, up_ask = parse(up_token)
    dn_imb, dn_ask = parse(dn_token)
    return up_imb, dn_imb, up_ask, dn_ask


# ─── State / trades ────────────────────────────────────────────────────────

def load_json(p, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def save_json(p, d):
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=2, default=str))
    tmp.replace(p)


def append_trade(row):
    header = ("timestamp,market,symbol,direction,imbalance,ask,size_shares,"
              "size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "direction", "imbalance",
            "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


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
    log.info("quant_v3 engine up. funder=%s", os.environ.get("POLY_FUNDER"))

    seen_bets: dict[tuple, float] = {}
    imbalance_streak: dict[tuple, int] = {}  # (cid, direction) -> consec scans
    last_scan = 0.0
    markets: list[dict] = []

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        min_imbalance = float(cfg.get("min_imbalance", 0.60))
        max_ask = float(cfg.get("max_ask", 0.70))
        min_book_sum = float(cfg.get("min_book_sum", 0.95))
        max_book_sum = float(cfg.get("max_book_sum", 1.05))
        max_pos = float(cfg.get("max_position_usd", 30))
        max_open = int(cfg.get("max_open_positions", 3))
        min_time_rem = float(cfg.get("min_time_remaining_s", 30))
        skip_open_s = int(cfg.get("skip_opening_seconds", 45))

        now = time.time()
        if now - last_scan > 20:
            markets = discover_markets(cfg.get("symbols", [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]))
            last_scan = now

        save_json(STATE_FILE, {
            "variant": "v3-book-imbalance",
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(seen_bets),
            "markets_tracked": len(markets),
        })

        if not markets:
            time.sleep(3); continue

        for m in markets:
            cid = m["conditionId"]
            t_rem = m["end_ts"] - now
            t_since_open = now - m["start_ts"]
            if t_rem < min_time_rem or t_since_open < skip_open_s:
                continue

            up_imb, dn_imb, up_ask, dn_ask = compute_imbalance_and_asks(
                client, m["up_token"], m["dn_token"])
            if up_imb is None or dn_imb is None or up_ask is None or dn_ask is None:
                continue

            book_sum = up_ask + dn_ask
            if book_sum < min_book_sum or book_sum > max_book_sum:
                continue

            # Strongest imbalance wins. Skip deep longshots (< min_ask) —
            # those resolve to zero 90%+ of the time and bleed us on variance.
            min_ask = float(cfg.get("min_ask", 0.15))
            direction = None
            token = None
            ask = None
            imb = 0.0
            if up_imb >= min_imbalance and up_imb > dn_imb and min_ask <= up_ask <= max_ask:
                direction, token, ask, imb = "UP", m["up_token"], up_ask, up_imb
            elif dn_imb >= min_imbalance and min_ask <= dn_ask <= max_ask:
                direction, token, ask, imb = "DN", m["dn_token"], dn_ask, dn_imb

            if direction is None:
                # reset both streaks when no signal this scan
                imbalance_streak.pop((cid, "UP"), None)
                imbalance_streak.pop((cid, "DN"), None)
                continue
            # Persistence filter — require same direction N consecutive scans
            streak_key = (cid, direction)
            opp_key = (cid, "DN" if direction == "UP" else "UP")
            imbalance_streak.pop(opp_key, None)  # clear opposite streak
            imbalance_streak[streak_key] = imbalance_streak.get(streak_key, 0) + 1
            min_streak = int(cfg.get("min_streak_scans", 3))
            if imbalance_streak[streak_key] < min_streak:
                log.info("[IMB-HOLD] %s %s streak=%d/%d (waiting for persistence)",
                         m["slug"], direction, imbalance_streak[streak_key], min_streak)
                continue
            if (cid, direction) in seen_bets:
                continue

            size_shares = max(max_pos / max(ask, 0.01), 5.0)
            our_usd = size_shares * ask

            log.info("[IMB] %s %s imb_up=%+.2f imb_dn=%+.2f up_ask=%.3f dn_ask=%.3f "
                     "→ %s @%.3f size=%.2f (≈$%.2f)",
                     m["slug"], m["symbol"], up_imb, dn_imb, up_ask, dn_ask,
                     direction, ask, size_shares, our_usd)

            if not enabled:
                continue
            if len(seen_bets) >= max_open:
                log.info("[SKIP-V3] open cap reached")
                continue
            # Cross-engine direction concurrency cap — prevents stacking N
            # correlated Up (or Down) bets that all blow up together
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
            seen_bets[(cid, direction)] = now

            if dry:
                log.info("[DRY-V3] would buy %s %s size=%.2f @%.3f",
                         m["slug"], direction, size_shares, ask)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"],
                    "direction": direction, "imbalance": round(imb, 3),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=str(token), price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[BUY-V3] %s %s size=%.2f @%.3f ≈$%.2f → %s",
                         m["slug"], direction, size_shares, ask, our_usd,
                         resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"],
                    "direction": direction, "imbalance": round(imb, 3),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("v3 order failed: %s", e)

        # purge oldest
        seen_bets = {k: t for k, t in seen_bets.items() if now - t < 1200}
        time.sleep(3)

    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("quant_v3 engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
