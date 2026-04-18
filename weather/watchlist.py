"""Pre-window watchlist — tracks fat edges BEFORE they enter the 30-hour
firing window. When a watched market crosses into window, the trader
already has full context and fires with conviction.

Solves: "nothing tradeable right now" → always a pipeline of pre-analyzed
candidates waiting to become eligible.

Flow:
  1. Scan ALL future markets (up to 5 days out)
  2. Score each with ensemble + offset + cushion
  3. Store promising ones in watchlist.json
  4. Each trader cycle: check which watchlist items entered the 30hr window
  5. Those get fast-tracked through validation → fire

Run standalone:  uv run python -m weather.watchlist          # refresh watchlist
                 uv run python -m weather.watchlist --show   # print current watchlist
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import date as dt_date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from weather.cities import CITIES, STATION_OFFSET_C, get_offset_c
from weather.sources import fetch_open_meteo_ensemble
from weather.model import compute_p_event

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
WATCHLIST_FILE = RUNTIME / "watchlist.json"
EDGE_CSV = ROOT / "weather" / "edge_table.csv"


def load_watchlist() -> list[dict]:
    if not WATCHLIST_FILE.exists():
        return []
    try:
        return json.loads(WATCHLIST_FILE.read_text()).get("watching", [])
    except Exception:
        return []


def save_watchlist(items: list[dict]):
    tmp = WATCHLIST_FILE.with_suffix(f".{os.getpid()}.tmp")
    data = {
        "watching": items,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(items),
    }
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(WATCHLIST_FILE)


def hours_to_resolution(target_date: dt_date, city: str) -> float:
    """Hours from now until market resolves (end-of-day in city's tz)."""
    tz_str = CITIES[city][2] if city in CITIES else "UTC"
    tz = ZoneInfo(tz_str)
    resolution = datetime(target_date.year, target_date.month, target_date.day,
                          23, 59, tzinfo=tz)
    now = datetime.now(ZoneInfo("UTC"))
    return (resolution - now).total_seconds() / 3600


def fresh_forecast(lat: float, lon: float, tz: str, date_s: str,
                   metric: str = "max_temp") -> float | None:
    d = "temperature_2m_max" if metric != "min_temp" else "temperature_2m_min"
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "daily": d,
            "start_date": date_s, "end_date": date_s,
            "temperature_unit": "fahrenheit", "timezone": tz,
        }, timeout=6).json()
        return r["daily"][d][0]
    except Exception:
        return None


def scan_watchlist_candidates(max_days: int = 5, min_edge: float = 0.08,
                               min_cushion: float = 1.5) -> list[dict]:
    """Cast a WIDE net for watching — low thresholds because we're gathering
    intelligence, not firing. A market with 1.5°F cushion today might have
    4°F cushion tomorrow. We want to be tracking it BEFORE it becomes tradeable.
    The trader's own gates (4°F cushion, 25pp edge) decide what actually fires."""
    """Scan edge_table for markets 30h+ out that look promising."""
    if not EDGE_CSV.exists():
        return []
    with EDGE_CSV.open() as f:
        rows = list(csv.DictReader(f))

    today = dt_date.today()
    candidates = []

    for r in rows:
        # Watch BOTH directional AND bucket markets — we're scouting, not firing
        td_str = r.get("target_date")
        if not td_str:
            continue
        try:
            td = dt_date.fromisoformat(td_str)
        except Exception:
            continue
        days_out = (td - today).days
        if days_out < 0 or days_out > max_days:
            continue
        city = r.get("city", "").lower()
        if city not in CITIES:
            continue

        hrs = hours_to_resolution(td, city)
        if hrs <= 30:
            continue  # already in firing window — trader handles this

        try:
            our = float(r["our_p"])
            mkt = float(r["market_p"])
            edge = abs(our - mkt)
            if edge < min_edge:
                continue
            fcst = float(r["forecast_f"])
        except (ValueError, TypeError, KeyError):
            continue

        offset_f = get_offset_c(city) * 9 / 5
        eff = fcst + offset_f
        side = "YES" if our > mkt else "NO"

        # Cushion calculation — handle both directional and buckets
        if r.get("op") in (">=", "<="):
            thr = float(r["threshold"])
            if r["op"] == ">=":
                cushion = (eff - thr) if side == "YES" else (thr - eff)
            else:
                cushion = (thr - eff) if side == "YES" else (eff - thr)
        elif r.get("op") == "in":
            try:
                lo, hi = [float(x) for x in r["threshold"].split("-")]
            except Exception:
                continue
            thr = (lo + hi) / 2  # midpoint for display
            if side == "YES":
                cushion = min(eff - lo, hi - eff) if lo <= eff <= hi else -1
            else:
                cushion = -1 if lo <= eff <= hi else min(abs(eff - lo), abs(eff - hi))
        else:
            continue

        if cushion < min_cushion:
            continue

        candidates.append({
            "city": city,
            "target_date": td_str,
            "op": r["op"],
            "threshold_f": thr,
            "side": side,
            "our_p": round(our, 3),
            "market_p": round(mkt, 3),
            "edge": round(edge, 3),
            "forecast_f": round(fcst, 1),
            "effective_f": round(eff, 1),
            "cushion_f": round(cushion, 1),
            "hours_to_resolution": round(hrs, 1),
            "hours_to_window": round(hrs - 30, 1),
            "question": r.get("question", "")[:80],
            "conditionId": r.get("conditionId", ""),
            "tokens": r.get("tokens", ""),
            "metric": r.get("metric", "max_temp"),
            "scouted_at": datetime.utcnow().isoformat() + "Z",
        })

    # Sort by edge × cushion (best candidates first)
    candidates.sort(key=lambda c: -(c["edge"] * c["cushion_f"]))
    return candidates


def check_window_entries(watchlist: list[dict]) -> list[dict]:
    """Return watchlist items that have entered the 30-hour firing window."""
    entered = []
    for item in watchlist:
        city = item.get("city", "")
        td_str = item.get("target_date", "")
        if not city or not td_str or city not in CITIES:
            continue
        try:
            td = dt_date.fromisoformat(td_str)
        except Exception:
            continue
        hrs = hours_to_resolution(td, city)
        if 0 < hrs <= 30:
            item["current_hours_to_resolution"] = round(hrs, 1)
            entered.append(item)
    return entered


SNAPSHOT_DIR = RUNTIME / "watchlist_snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)
SNAPSHOT_INTERVAL_S = 3600  # every 1 hour — why not, data is free


def snapshot_watched():
    """Take a point-in-time snapshot of METAR + ensemble + market price for
    each watchlist item. Appends to per-market JSON files so we can see
    forecast drift over time before the market enters our firing window.

    Each snapshot records:
      - timestamp
      - METAR current obs (if available for that station)
      - Open-Meteo fresh single-best forecast peak
      - Ensemble min/median/max + %members crossing threshold
      - Live market YES price
      - Computed cushion at this moment
    """
    items = load_watchlist()
    if not items:
        return

    for item in items:
        cid = item.get("conditionId", "")
        if not cid:
            continue
        city = item.get("city", "").lower()
        if city not in CITIES:
            continue

        snap_file = SNAPSHOT_DIR / f"{cid[:16]}_{city}.json"

        # Load existing snapshots
        existing = []
        if snap_file.exists():
            try:
                existing = json.loads(snap_file.read_text())
            except Exception:
                existing = []

        # Rate limit: skip if last snapshot < SNAPSHOT_INTERVAL_S ago
        if existing:
            last_ts = existing[-1].get("timestamp", "")
            try:
                from datetime import datetime as dt_cls
                last_dt = dt_cls.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_s = (datetime.now(ZoneInfo("UTC")) - last_dt).total_seconds()
                if age_s < SNAPSHOT_INTERVAL_S:
                    continue  # too recent, skip
            except Exception:
                pass

        lat, lon, tz_str, icao = CITIES[city][:4]
        td_str = item.get("target_date", "")
        metric = item.get("metric", "max_temp")
        snap = {
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
            "city": city,
            "target_date": td_str,
            "side": item.get("side"),
            "threshold_f": item.get("threshold_f"),
        }

        # 1. METAR current obs
        try:
            from weather.metar import metar_current
            mc = metar_current(icao, tz_str)
            if mc:
                snap["metar_temp_f"] = mc.get("temp_f")
                snap["metar_time"] = mc.get("time_local") or mc.get("time_utc")
                snap["metar_station"] = icao
        except Exception:
            pass

        # 2. Fresh single-best forecast
        fcst = fresh_forecast(lat, lon, tz_str, td_str, metric)
        if fcst is not None:
            offset_f = get_offset_c(city) * 9 / 5
            snap["forecast_f"] = round(fcst, 1)
            snap["effective_f"] = round(fcst + offset_f, 1)
            thr = item.get("threshold_f", 0)
            side = item.get("side", "NO")
            op = item.get("op", ">=")
            eff = fcst + offset_f
            if side == "YES":
                snap["cushion_f"] = round((eff - thr) if op == ">=" else (thr - eff), 1)
            else:
                snap["cushion_f"] = round((thr - eff) if op == ">=" else (eff - thr), 1)

        # 3. Ensemble quick stats
        try:
            ens = fetch_open_meteo_ensemble(lat, lon,
                                            dt_date.fromisoformat(td_str),
                                            dt_date.fromisoformat(td_str))
            if ens and "hourly" in ens:
                times = ens["hourly"]["time"]
                agg = max if metric != "min_temp" else min
                vals = []
                for k, v in ens["hourly"].items():
                    if k == "time" or not isinstance(v, list):
                        continue
                    day = [x for t, x in zip(times, v)
                           if x is not None and t[:10] == td_str]
                    if day:
                        vals.append(agg(day))
                if vals:
                    offset_f = get_offset_c(city) * 9 / 5
                    adj = [v + offset_f for v in vals]
                    snap["ensemble_n"] = len(adj)
                    snap["ensemble_min_f"] = round(min(adj), 1)
                    snap["ensemble_median_f"] = round(sorted(adj)[len(adj) // 2], 1)
                    snap["ensemble_max_f"] = round(max(adj), 1)
                    thr = item.get("threshold_f", 0)
                    op = item.get("op", ">=")
                    if op == ">=":
                        snap["pct_above_threshold"] = round(
                            100 * sum(1 for v in adj if v >= thr) / len(adj), 1)
                    elif op == "<=":
                        snap["pct_below_threshold"] = round(
                            100 * sum(1 for v in adj if v <= thr) / len(adj), 1)
        except Exception:
            pass

        # 4. Live market price
        try:
            tokens = json.loads(item.get("tokens", "[]"))
            if tokens:
                tok = tokens[0]  # YES token
                r = requests.get("https://clob.polymarket.com/book",
                                 params={"token_id": tok}, timeout=6).json()
                asks = [float(a["price"]) for a in r.get("asks", [])]
                bids = [float(b["price"]) for b in r.get("bids", [])]
                if asks:
                    snap["market_yes_ask"] = min(asks)
                if bids:
                    snap["market_yes_bid"] = max(bids)
        except Exception:
            pass

        existing.append(snap)
        # Keep last 50 snapshots per market
        existing = existing[-50:]
        try:
            snap_file.write_text(json.dumps(existing, indent=2, default=str))
        except Exception:
            pass


def show_snapshots(city: str = None):
    """Print snapshot history for a watched market."""
    for f in sorted(SNAPSHOT_DIR.glob("*.json")):
        snaps = json.loads(f.read_text())
        if not snaps:
            continue
        if city and city.lower() not in f.name:
            continue
        print(f"\n{'═' * 70}")
        print(f"  {snaps[0].get('city','?')} — {snaps[0].get('target_date','?')} "
              f"({snaps[0].get('side','?')} {snaps[0].get('threshold_f','?')}°F)")
        print(f"{'═' * 70}")
        print(f"  {'time':<22}{'forecast':<10}{'cushion':<9}{'ensemble':<15}{'metar':<10}{'mkt_yes'}")
        for s in snaps:
            ts = s.get("timestamp", "")[:16]
            fcst = f"{s.get('forecast_f', '?')}°F" if s.get("forecast_f") else "?"
            cush = f"{s.get('cushion_f', '?'):+.1f}" if s.get("cushion_f") is not None else "?"
            ens_med = f"{s.get('ensemble_median_f', '?')}°F" if s.get("ensemble_median_f") else "?"
            metar = f"{s.get('metar_temp_f', '?')}°F" if s.get("metar_temp_f") else "—"
            mkt = f"${s.get('market_yes_ask', '?')}" if s.get("market_yes_ask") else "?"
            print(f"  {ts:<22}{fcst:<10}{cush:<9}{ens_med:<15}{metar:<10}{mkt}")


def refresh():
    """Full watchlist refresh: scan → filter → save → snapshot hourly."""
    candidates = scan_watchlist_candidates()
    # Merge with existing (keep scouted_at from first discovery)
    existing = {(w["conditionId"], w["side"]): w for w in load_watchlist()}
    merged = []
    for c in candidates:
        key = (c["conditionId"], c["side"])
        if key in existing:
            # Keep first-scouted timestamp, update everything else
            c["scouted_at"] = existing[key].get("scouted_at", c["scouted_at"])
        merged.append(c)
    save_watchlist(merged[:50])  # cap at 50
    # Take hourly snapshots of all watched items
    snapshot_watched()
    return merged


def show():
    """Print current watchlist."""
    items = load_watchlist()
    if not items:
        print("Watchlist empty. Run refresh first.")
        return
    entered = check_window_entries(items)
    entered_ids = {(e["conditionId"], e["side"]) for e in entered}

    print(f"{'city':<14}{'date':<12}{'op':<3}{'side':<4}{'edge':<7}{'cush':<7}"
          f"{'hrs_to_res':<11}{'hrs_to_win':<11}status")
    for item in items:
        key = (item["conditionId"], item["side"])
        status = "🔥 IN WINDOW" if key in entered_ids else f"⏳ {item['hours_to_window']:.0f}h"
        print(f"{item['city']:<14}{item['target_date']:<12}{item['op']:<3}"
              f"{item['side']:<4}{item['edge']:<7}{item['cushion_f']:<7.1f}"
              f"{item['hours_to_resolution']:<11.1f}{item.get('hours_to_window',0):<11.1f}"
              f"{status}")
    if entered:
        print(f"\n🔥 {len(entered)} item(s) entered the 30-hour firing window!")


def main():
    load_dotenv(ROOT / ".env")
    if "--show" in sys.argv:
        show()
    elif "--snapshots" in sys.argv:
        city = sys.argv[sys.argv.index("--snapshots") + 1] if len(sys.argv) > sys.argv.index("--snapshots") + 1 else None
        show_snapshots(city)
    else:
        items = refresh()
        print(f"Watchlist refreshed: {len(items)} candidates (snapshots taken)")
        show()


if __name__ == "__main__":
    sys.exit(main())
