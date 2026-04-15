"""Weather trader — fires limit buys on edges in the edge_table.

Workflow each cycle:
  1. Refresh edge_table (calls dump.py as subprocess in background; use latest CSV)
  2. Load edge_table.csv
  3. For each row with |edge| > edge_bps, determine favored side:
       our_p > market_p + threshold → buy YES
       our_p < market_p - threshold → buy NO
  4. Fetch live book, check liquidity depth on favored side
  5. Post limit order at best ask (takes what's there), size $max_pos_usd
  6. Track open conditions so we don't double-fire
"""
from __future__ import annotations

import csv
import json
import logging
import os
import signal
import sys
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
CFG_FILE = RUNTIME / "weather_trader_config.json"
STATE_FILE = RUNTIME / "weather_trader_state.json"
TRADES_FILE = RUNTIME / "weather_trader_trades.csv"
LOG_FILE = RUNTIME / "weather_trader.log"
EDGE_CSV = ROOT / "weather" / "edge_table.csv"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("wthr")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN; SHUTDOWN = True


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
    header = ("timestamp,question,city,threshold,side,our_p,market_p,edge,ask,"
              "size_shares,size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")).replace(",", ";") for k in (
            "timestamp", "question", "city", "threshold", "side", "our_p",
            "market_p", "edge", "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


def fetch_book(token_id: str) -> dict | None:
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=8)
        return r.json()
    except Exception as e:
        log.warning("book %s: %s", token_id[:10], e)
        return None


def ask_depth_usd(book: dict, max_price: float) -> tuple[float, float]:
    """Return (best_ask, total_usd_depth_at_or_below_max_price)."""
    asks = book.get("asks", []) or []
    if not asks:
        return (None, 0.0)
    priced = sorted([(float(a["price"]), float(a["size"])) for a in asks])
    best = priced[0][0]
    total = sum(p * s for p, s in priced if p <= max_price)
    return (best, total)


def load_edges(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def decide_side(row: dict, edge_threshold: float,
                 only_directional: bool = False,
                 skip_today: bool = True) -> Optional[tuple]:
    """Return (side, target_ask_max, token_idx) or None.

    only_directional: if True, only trade op in (>=, <=) not "in" buckets —
    these are more robust to ensemble model precision limits.
    skip_today: skip markets where target_date == today (intra-day drift risk).
    """
    from datetime import date
    try:
        our = float(row["our_p"])
        mkt = float(row["market_p"])
    except (ValueError, TypeError):
        return None
    if only_directional and row.get("op") not in (">=", "<="):
        # Hard reject bucket ("in") and exact ("=") markets — only >= / <=
        # are robust to our ensemble's 1-2°F imprecision.
        return None
    if row.get("op") == "in":
        return None  # never fire on buckets, period
    if skip_today and row.get("target_date") == date.today().isoformat():
        return None
    delta = our - mkt
    if abs(delta) < edge_threshold:
        return None
    tokens = json.loads(row["tokens"])
    if delta > 0:
        return ("YES", our, tokens[0])
    else:
        return ("NO", 1.0 - our, tokens[1])


def refresh_edge_table() -> bool:
    """Run weather.dump as subprocess to refresh edge_table.csv.
    Returns True if success; keeps existing CSV on failure."""
    import subprocess
    try:
        log.info("[REFRESH] running weather.dump ...")
        r = subprocess.run(
            [".venv/bin/python3", "-m", "weather.dump"],
            cwd=str(ROOT), timeout=300, capture_output=True, text=True,
        )
        if r.returncode == 0:
            # Extract last interesting line from stdout
            tail = [ln for ln in r.stdout.splitlines() if "wrote" in ln or "parsed" in ln]
            log.info("[REFRESH] ok — %s", "; ".join(tail[-2:]) if tail else "done")
            return True
        log.warning("[REFRESH] exit=%d stderr=%s", r.returncode, r.stderr[:300])
    except subprocess.TimeoutExpired:
        log.warning("[REFRESH] timed out")
    except Exception as e:
        log.warning("[REFRESH] failed: %s", e)
    return False


def kelly_fraction(our_p: float, ask: float, cap: float = 0.25,
                    confidence: float = 1.0) -> float:
    """Kelly criterion: f* = (p*b - q) / b where b = (1-ask)/ask.
    Capped at `cap` (quarter-Kelly default) then scaled by `confidence` [0,1].
    Returns 0 if edge is negative."""
    if ask <= 0 or ask >= 1:
        return 0.0
    b = (1.0 - ask) / ask
    q = 1.0 - our_p
    f = (our_p * b - q) / b
    if f <= 0:
        return 0.0
    return min(f, cap) * max(0.0, min(1.0, confidence))


def confidence_from_row(row: dict, nws_conf: float = 1.0) -> float:
    """Confidence score [0, 1] combining ensemble spread, members, and NWS agreement."""
    try:
        spread = float(row.get("spread_f") or 0)
        members = int(row.get("members") or 0)
    except (ValueError, TypeError):
        return 0.5
    spread_score = max(0.0, 1.0 - spread / 5.0)
    member_score = min(1.0, members / 4.0)
    # NWS cross-check: 1.0 if both models agree within 2°F, 0.5 if 2-5°F,
    # 0.0 if > 5°F apart. Non-US cities return 1.0 (neutral).
    return max(0.2, spread_score * member_score * nws_conf)


def nws_confidence_for_row(row: dict, cache: dict) -> float:
    """Query NWS for US cities, cache per (city, date). Returns confidence [0,1]."""
    try:
        from weather.nws import cross_check, is_us_city
        from weather.cities import CITIES
    except Exception:
        return 1.0
    city = row.get("city", "").lower()
    if city not in CITIES:
        return 1.0
    lat, lon, tz, _ = CITIES[city]
    if not is_us_city(lat, lon):
        return 1.0
    target_date = row.get("target_date")
    if not target_date:
        return 1.0
    key = (city, target_date)
    if key in cache:
        return cache[key]
    try:
        from datetime import date as dt_date
        d = dt_date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
        om_max = float(row.get("forecast_f") or 0)
        om_min = om_max - 10  # rough; NWS only needs one for comparison
        cc = cross_check(lat, lon, d, tz, om_max, om_min)
        conf = cc.get("confidence", 1.0) if cc else 1.0
    except Exception:
        conf = 1.0
    cache[key] = conf
    return conf


def intraday_p_for_row(row: dict) -> Optional[float]:
    """For markets resolving today, compute P using hourly observations.
    Returns None if market doesn't resolve today or data unavailable."""
    from datetime import date as dt_date
    from weather.cities import CITIES
    target_date = row.get("target_date")
    if not target_date:
        return None
    try:
        td = dt_date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    except Exception:
        return None
    if td != dt_date.today():
        return None
    city = row.get("city", "").lower()
    if city not in CITIES:
        return None
    lat, lon, tz, _ = CITIES[city]
    try:
        from weather.intraday import p_event_intraday
        metric = row.get("metric", "max_temp")
        op = row.get("op", ">=")
        thr_str = str(row.get("threshold", ""))
        if "-" in thr_str:
            lo, hi = thr_str.split("-")
            return p_event_intraday(lat, lon, tz, metric, "in",
                                     threshold_low=float(lo), threshold_high=float(hi))
        return p_event_intraday(lat, lon, tz, metric, op,
                                 threshold=float(thr_str))
    except Exception as e:
        log.warning("[INTRADAY] %s %s: %s", city, target_date, e)
        return None


def fresh_forecast_sanity(row: dict) -> tuple[bool, str]:
    """Re-fetch single-best forecast for the candidate's city+date and verify
    the edge_table's forecast_f is still roughly right. Returns (ok, reason).

    Added after 2026-04-15 Toronto near-miss where edge_table had a stale/outlier
    forecast implying 21°C peak when fresh data said 15.6°C. Cushion evaporated.
    """
    try:
        from datetime import date as dt_date
        from weather.cities import CITIES
        import requests as _r
        city = (row.get("city") or "").lower()
        date_s = row.get("target_date", "")
        if not city or city not in CITIES or not date_s:
            return True, "no-city-skip"
        lat, lon, tz, _ic = CITIES[city][:4]
        metric = row.get("metric", "max_temp")
        daily = "temperature_2m_max" if metric != "min_temp" else "temperature_2m_min"
        resp = _r.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "daily": daily,
            "start_date": date_s, "end_date": date_s,
            "temperature_unit": "fahrenheit", "timezone": tz,
        }, timeout=6).json()
        fresh_peak = resp["daily"][daily][0]
        csv_forecast = float(row.get("forecast_f", 0))
        gap = abs(fresh_peak - csv_forecast)
        if gap > 3.0:
            return False, f"forecast drift {csv_forecast:.1f}→{fresh_peak:.1f}°F (gap {gap:.1f}°F)"
        return True, f"ok ({fresh_peak:.1f}°F)"
    except Exception as e:
        return True, f"check-skip: {e}"


def check_order_status(client: ClobClient, order_id: str) -> Optional[str]:
    """Poll a single order's status. Returns 'matched' | 'live' | 'cancelled' | None."""
    try:
        o = client.get_order(order_id)
        if isinstance(o, dict):
            return o.get("status")
    except Exception as e:
        log.warning("[POLL] %s: %s", order_id[:12], e)
    return None


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
    log.info("weather trader up. funder=%s", os.environ.get("POLY_FUNDER"))

    fired: dict[str, float] = {}  # (cid, side) -> entered_at
    live_orders: dict[str, dict] = {}  # order_id → {market, side, ask, size_usd, posted_at}
    last_refresh = 0.0

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        # Auto-refresh edge_table every refresh_every_s (default 30 min)
        refresh_every_s = float(cfg.get("refresh_every_s", 1800))
        now_t = time.time()
        if now_t - last_refresh > refresh_every_s:
            refresh_edge_table()
            last_refresh = now_t
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        edge_thr = float(cfg.get("edge_threshold", 0.15))
        min_liq_usd = float(cfg.get("min_liq_usd", 30.0))
        max_pos_usd = float(cfg.get("max_position_usd", 50.0))
        max_open = int(cfg.get("max_open_positions", 6))
        max_ask = float(cfg.get("max_ask", 0.85))
        min_ask = float(cfg.get("min_ask", 0.02))

        rows = load_edges(EDGE_CSV)
        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "edge_rows": len(rows), "open_positions": len(fired),
        })

        if not rows:
            log.info("[WAIT] no edge table")
            time.sleep(30); continue

        actionable = 0
        only_dir = bool(cfg.get("only_directional", True))
        for row in rows:
            decision = decide_side(row, edge_thr, only_directional=only_dir)
            if not decision:
                continue
            side, fair_max, token = decision
            key = (row["conditionId"], side)
            if key in fired:
                continue

            book = fetch_book(token)
            if not book:
                continue
            best_ask, depth = ask_depth_usd(book, fair_max)
            if best_ask is None:
                continue
            if best_ask < min_ask or best_ask > max_ask:
                continue
            if depth < min_liq_usd:
                continue
            # Pre-fire sanity check: re-verify forecast hasn't drifted vs CSV.
            ok, why = fresh_forecast_sanity(row)
            if not ok:
                log.info("[STALE-SKIP] %s %s: %s",
                         row.get("city"), row.get("target_date"), why)
                continue
            actionable += 1

            # Intraday obs override: for today's markets, use hourly obs.
            our = float(row["our_p"])
            intraday_override = intraday_p_for_row(row)
            if intraday_override is not None:
                log.info("[INTRADAY] %s raw=%.3f → intraday=%.3f", row["city"],
                         our, intraday_override)
                our = intraday_override

            # Kelly sizing: size scales with edge strength × confidence.
            p_win = our if side == "YES" else (1.0 - our)
            kelly_cap = float(cfg.get("kelly_cap", 0.20))
            nws_cache = getattr(main, "_nws_cache", {})
            main._nws_cache = nws_cache
            nws_conf = nws_confidence_for_row(row, nws_cache)
            conf = confidence_from_row(row, nws_conf=nws_conf)
            f = kelly_fraction(p_win, best_ask, cap=kelly_cap, confidence=conf)
            bankroll = float(cfg.get("allocation_usd", 300.0))
            kelly_usd = f * bankroll
            size_usd = min(max_pos_usd, depth * 0.8, kelly_usd)
            if size_usd < 5.0:  # below Polymarket min
                continue
            size_shares = size_usd / best_ask
            if size_shares < 5.0:
                continue

            log.info("[EDGE] %s %s ask=%.3f depth=$%.1f our=%s mkt=%s  %s",
                     row["city"], side, best_ask, depth, row["our_p"],
                     row["market_p"], row["question"][:70])

            if not enabled:
                continue
            if len(fired) >= max_open:
                log.info("[CAP] max_open=%d reached", max_open)
                break

            fired[key] = time.time()
            if dry:
                log.info("[DRY] would buy %s %s size=%.2f @%.3f ≈$%.2f",
                         row["city"], side, size_shares, best_ask, size_usd)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "question": row["question"][:60], "city": row["city"],
                    "threshold": row["threshold"], "side": side,
                    "our_p": row["our_p"], "market_p": row["market_p"],
                    "edge": row["edge"], "ask": best_ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(size_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=best_ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                oid = resp.get("orderID", "")
                log.info("[BUY] %s %s kelly=%.3f ≈$%.2f @ %.3f → %s",
                         row["city"], side, f, size_usd, best_ask, resp.get("status"))
                if oid:
                    live_orders[oid] = {
                        "market": row["question"][:60], "side": side,
                        "ask": best_ask, "size_usd": size_usd,
                        "posted_at": time.time(),
                    }
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "question": row["question"][:60], "city": row["city"],
                    "threshold": row["threshold"], "side": side,
                    "our_p": row["our_p"], "market_p": row["market_p"],
                    "edge": row["edge"], "ask": best_ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(size_usd, 2), "token_id": token,
                    "order_id": oid, "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("order failed: %s", e)

        # Poll any delayed/live orders every cycle to catch fills
        if live_orders and not dry:
            for oid in list(live_orders.keys()):
                meta = live_orders[oid]
                status = check_order_status(client, oid)
                if status == "matched":
                    log.info("[FILL] order %s: %s %s ≈$%.2f filled",
                             oid[:12], meta["market"][:40], meta["side"],
                             meta["size_usd"])
                    del live_orders[oid]
                elif status in ("cancelled", "canceled"):
                    log.info("[CANCEL] order %s: %s was %s",
                             oid[:12], meta["market"][:40], status)
                    del live_orders[oid]
                elif time.time() - meta["posted_at"] > 3600:
                    # Give up tracking after 1h — market probably stale
                    del live_orders[oid]

        log.info("[SCAN] rows=%d actionable=%d open=%d", len(rows), actionable, len(fired))

        # purge fired set after 6h (markets resolve daily at midnight)
        fired = {k: t for k, t in fired.items() if time.time() - t < 6 * 3600}
        time.sleep(60)

    log.info("weather trader stopped.")


if __name__ == "__main__":
    sys.exit(main())
