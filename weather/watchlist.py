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


def scan_watchlist_candidates(max_days: int = 5, min_edge: float = 0.20,
                               min_cushion: float = 3.5) -> list[dict]:
    """Scan edge_table for markets 30h+ out that look promising."""
    if not EDGE_CSV.exists():
        return []
    with EDGE_CSV.open() as f:
        rows = list(csv.DictReader(f))

    today = dt_date.today()
    candidates = []

    for r in rows:
        # Only directional
        if r.get("op") not in (">=", "<="):
            continue
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

        # Check if OUTSIDE the 30-hour window (those are for the trader, not us)
        hrs = hours_to_resolution(td, city)
        if hrs <= 30:
            continue  # already in firing window — trader handles this

        try:
            our = float(r["our_p"])
            mkt = float(r["market_p"])
            edge = abs(our - mkt)
            if edge < min_edge:
                continue
            thr = float(r["threshold"])
            fcst = float(r["forecast_f"])
        except (ValueError, TypeError, KeyError):
            continue

        offset_f = get_offset_c(city) * 9 / 5
        eff = fcst + offset_f
        side = "YES" if our > mkt else "NO"

        if r["op"] == ">=":
            cushion = (eff - thr) if side == "YES" else (thr - eff)
        else:
            cushion = (thr - eff) if side == "YES" else (eff - thr)

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


def refresh():
    """Full watchlist refresh: scan → filter → save."""
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
    else:
        items = refresh()
        print(f"Watchlist refreshed: {len(items)} candidates")
        show()


if __name__ == "__main__":
    sys.exit(main())
