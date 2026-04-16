"""First-mover watcher for freshly-listed Polymarket weather markets.

Runs alongside the main trader. Polls gamma events every 15s, detects new
event IDs, and fires on fresh weather markets immediately — before retail
sees them and prices converge.

Logic per new market:
  1. Parse the question via weather.parser
  2. Fetch ensemble for (city, date)
  3. Apply station offset + cushion check
  4. If edge > LISTING_EDGE_THRESHOLD (higher than trader's regular 0.15),
     fire immediately with a capped first-mover size
  5. Log to weather/listing_watcher.log + dashboard/runtime/listing_trades.csv

Separate process because:
  - Must not block the main trader's scan loop
  - Faster poll cadence (15s vs 10 min) specifically for new markets
  - Tighter edge threshold (0.25) because first-mover = less info
"""
from __future__ import annotations

import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, date as dt_date
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

from weather.cities import CITIES, STATION_OFFSET_C, get_offset_c
from weather.parser import parse_market
from weather.sources import fetch_open_meteo_ensemble
from weather.model import compute_p_event

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
CFG_FILE = RUNTIME / "listing_watcher_config.json"
SEEN_FILE = RUNTIME / "listing_watcher_seen.json"
TRADES_FILE = RUNTIME / "listing_watcher_trades.csv"
LOG_FILE = RUNTIME / "listing_watcher.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("listing")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


# ─── API ──────────────────────────────────────────────────────────────

def fetch_recent_events(limit: int = 60) -> list[dict]:
    """Fetch most-recently-listed active events. Order by start date desc."""
    results: list[dict] = []
    for params in [
        {"tag_slug": "weather", "limit": limit, "closed": "false", "active": "true",
         "order": "startDate", "ascending": "false"},
        {"q": "temperature", "limit": limit, "closed": "false", "active": "true"},
    ]:
        try:
            r = requests.get("https://gamma-api.polymarket.com/events",
                             params=params, headers={"User-Agent": "Mozilla/5.0"},
                             timeout=8)
            data = r.json() or []
            results.extend(data)
        except Exception as e:
            log.warning("gamma fetch err: %s", e)
    # dedupe
    seen = set(); uniq = []
    for e in results:
        eid = e.get("id")
        if eid in seen: continue
        seen.add(eid); uniq.append(e)
    return uniq


def book_best_ask(client: ClobClient, token_id: str) -> tuple[Optional[float], float]:
    """Returns (best_ask, depth_at_or_below_0.85)."""
    try:
        b = client.get_order_book(token_id)
        asks = sorted(
            [(float(a.price), float(a.size)) for a in getattr(b, "asks", []) or []]
        )
        if not asks:
            return None, 0.0
        depth = sum(p * s for p, s in asks if p <= 0.85)
        return asks[0][0], depth
    except Exception:
        return None, 0.0


# ─── State ────────────────────────────────────────────────────────────

def load_seen() -> set:
    if not SEEN_FILE.exists(): return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(s: set):
    try:
        tmp = SEEN_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(list(s)[-2000:]))
        tmp.replace(SEEN_FILE)
    except Exception:
        pass


def append_trade(row: dict):
    header = ("timestamp,market,city,date,side,ask,our_p,market_p,cushion_f,"
              "size_shares,size_usd,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")).replace(",", ";") for k in (
            "timestamp", "market", "city", "date", "side", "ask", "our_p",
            "market_p", "cushion_f", "size_shares", "size_usd",
            "order_id", "status")) + "\n")


def load_cfg() -> dict:
    if not CFG_FILE.exists():
        return {"enabled": False, "dry_run": True,
                "edge_threshold": 0.25, "min_cushion_f": 3.0,
                "min_ask": 0.05, "max_ask": 0.75,
                "min_liq_usd": 20.0, "max_position_usd": 80.0,
                "poll_seconds": 15}
    return json.loads(CFG_FILE.read_text())


# ─── Core eval ────────────────────────────────────────────────────────

def score_market(mkt: dict, today: dt_date) -> Optional[dict]:
    """Given a single market JSON from gamma, return trading decision or None."""
    question = mkt.get("question") or ""
    q = parse_market(question, today)
    if not q: return None
    td = q.get("target_date")
    if not td or td < today or (td - today).days > 3:
        return None  # only near-term
    if q.get("op") == "in":
        return None  # no buckets
    if q.get("op") not in (">=", "<="):
        return None
    city = q["city"]
    if city not in CITIES: return None
    lat, lon, tz, _ = CITIES[city][:4]
    # Offset-adjust threshold
    offset_c = get_offset_c(city)
    offset_f = offset_c * 9 / 5
    if q.get("unit") == "C":
        thr_f = q["threshold"] * 9 / 5 + 32
    else:
        thr_f = q["threshold"]
    # Effective threshold on our (grid) scale = threshold - offset (so
    # we need forecast >= effective_thr for oracle to see >= real_thr)
    # Fetch ensemble
    ens = fetch_open_meteo_ensemble(lat, lon, td, td)
    if not ens or "error" in ens: return None
    # Convert to Fahrenheit for consistency
    hourly = ens.get("hourly", {})
    times = hourly.get("time", [])
    member_maxes = []
    for key, vals in hourly.items():
        if not key.startswith("temperature_2m") or key == "time": continue
        if not isinstance(vals, list): continue
        day = [v for t, v in zip(times, vals)
               if v is not None and t[:10] == td.isoformat()]
        if day: member_maxes.append(max(day) if q.get("metric") != "min_temp" else min(day))
    if not member_maxes: return None
    # Open-Meteo ensemble returns celsius by default (we'd have to pass F).
    # Sources.fetch_open_meteo_ensemble already requests fahrenheit — so these
    # are already °F.
    median = sorted(member_maxes)[len(member_maxes) // 2]
    effective = median + offset_f  # apply station offset
    side = "YES" if q["op"] == ">=" and effective >= thr_f else (
           "YES" if q["op"] == "<=" and effective <= thr_f else "NO")
    if side == "YES":
        cushion = (effective - thr_f) if q["op"] == ">=" else (thr_f - effective)
    else:
        cushion = (thr_f - effective) if q["op"] == ">=" else (effective - thr_f)
    if cushion < 0:  # forecast on wrong side
        side = "NO" if side == "YES" else "YES"
        cushion = abs(cushion)
    # hit count
    if q["op"] == ">=":
        hits = sum(1 for v in member_maxes if (v + offset_f) >= thr_f)
    else:
        hits = sum(1 for v in member_maxes if (v + offset_f) <= thr_f)
    our_p_yes = hits / len(member_maxes)
    our_p_win = our_p_yes if side == "YES" else (1 - our_p_yes)
    # Extract market price
    try:
        prices = json.loads(mkt.get("outcomePrices", "[]"))
        tokens = json.loads(mkt.get("clobTokenIds", "[]"))
        yes_price = float(prices[0]) if prices else None
    except Exception:
        yes_price = None; tokens = []
    if yes_price is None or len(tokens) != 2: return None
    mkt_p_yes = yes_price
    mkt_p_win = mkt_p_yes if side == "YES" else (1 - mkt_p_yes)
    edge = our_p_win - mkt_p_win
    return {
        "question": question, "city": city, "target_date": td,
        "side": side, "token": tokens[0] if side == "YES" else tokens[1],
        "our_p": round(our_p_win, 3), "mkt_p": round(mkt_p_win, 3),
        "edge": round(edge, 3), "cushion_f": round(cushion, 1),
        "forecast_f": round(median, 1), "effective_f": round(effective, 1),
        "op": q["op"], "threshold_f": round(thr_f, 1),
    }


# ─── Main ─────────────────────────────────────────────────────────────

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
    log.info("listing watcher up")

    seen = load_seen()
    log.info("loaded %d seen event ids", len(seen))

    while not SHUTDOWN:
        cfg = load_cfg()
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        edge_thr = float(cfg.get("edge_threshold", 0.25))
        min_cushion = float(cfg.get("min_cushion_f", 3.0))
        min_ask = float(cfg.get("min_ask", 0.05))
        max_ask = float(cfg.get("max_ask", 0.75))
        min_liq = float(cfg.get("min_liq_usd", 20.0))
        max_pos = float(cfg.get("max_position_usd", 80.0))
        poll_s = float(cfg.get("poll_seconds", 15))

        events = fetch_recent_events(limit=60)
        today = dt_date.today()

        new_event_count = 0
        for ev in events:
            eid = ev.get("id")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            new_event_count += 1
            # evaluate each market in the event
            for mkt in ev.get("markets", []) or []:
                try:
                    d = score_market(mkt, today)
                except Exception as e:
                    log.warning("score err: %s", e); continue
                if not d: continue
                label = f'{d["city"]} {d["side"]} {d["op"]}{d["threshold_f"]:.0f}°F {d["target_date"]}'
                if abs(d["edge"]) < edge_thr:
                    continue
                if d["cushion_f"] < min_cushion:
                    log.info("[FRESH-SKIP] %s cushion %.1f°F too thin",
                             label, d["cushion_f"])
                    continue
                # Fetch live book
                ask, depth = book_best_ask(client, d["token"])
                if ask is None or ask < min_ask or ask > max_ask or depth < min_liq:
                    log.info("[FRESH-BOOK] %s ask=%s depth=$%.0f — skip",
                             label, ask, depth)
                    continue
                size_usd = min(max_pos, depth * 0.8)
                size_shares = round(size_usd / ask, 2)
                if size_shares < 5: continue
                log.info("[FRESH-EDGE] %s ask=%.3f edge=%+.2f cushion=%.1f°F "
                         "our=%.2f mkt=%.2f size=$%.0f",
                         label, ask, d["edge"], d["cushion_f"],
                         d["our_p"], d["mkt_p"], size_usd)
                if not enabled: continue
                if dry:
                    log.info("[FRESH-DRY] would buy %s @%.3f × %.0f",
                             label, ask, size_shares)
                    append_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "market": d["question"][:70], "city": d["city"],
                        "date": d["target_date"], "side": d["side"], "ask": ask,
                        "our_p": d["our_p"], "market_p": d["mkt_p"],
                        "cushion_f": d["cushion_f"],
                        "size_shares": size_shares, "size_usd": round(size_usd, 2),
                        "order_id": "DRY", "status": "dry",
                    })
                    continue
                try:
                    order = client.create_order(OrderArgs(
                        token_id=d["token"], price=ask,
                        size=size_shares, side=BUY,
                    ))
                    resp = client.post_order(order)
                    log.info("[FRESH-BUY] %s ≈$%.0f @%.3f → %s",
                             label, size_usd, ask, resp.get("status"))
                    append_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "market": d["question"][:70], "city": d["city"],
                        "date": d["target_date"], "side": d["side"], "ask": ask,
                        "our_p": d["our_p"], "market_p": d["mkt_p"],
                        "cushion_f": d["cushion_f"],
                        "size_shares": size_shares, "size_usd": round(size_usd, 2),
                        "order_id": resp.get("orderID", ""),
                        "status": resp.get("status", ""),
                    })
                except Exception as e:
                    log.exception("fresh order failed: %s", e)

        if new_event_count:
            log.info("[POLL] %d new events scanned, seen=%d",
                     new_event_count, len(seen))
        save_seen(seen)
        # sleep with shutdown responsiveness
        for _ in range(int(poll_s)):
            if SHUTDOWN: break
            time.sleep(1)

    log.info("listing watcher stopped")


if __name__ == "__main__":
    sys.exit(main())
