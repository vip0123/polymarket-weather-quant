"""Dynamic position watcher — auto-tracks every live weather position.

Every cycle:
  1. Pull live positions from Polymarket
  2. Filter to weather bets still in play (cur price 0.05-0.95)
  3. Parse city/date/threshold from title
  4. Compute fresh 143-member ensemble probability post-offset
  5. Log status + alert on band changes

New bets auto-appear. Settled bets auto-disappear. No manual list.

Run:  nohup .venv/bin/python3 -m weather.position_watcher > /dev/null 2>&1 &
Log:  dashboard/runtime/position_watcher.log
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "dashboard" / "runtime" / "position_watcher.log"
WALLET = os.environ.get("POLY_FUNDER") or os.environ.get("POLY_WALLET_ADDRESS", "")

KEY = os.environ.get("OPEN_METEO_API_KEY", "").strip()
ENS = ("https://customer-ensemble-api.open-meteo.com/v1/ensemble" if KEY
       else "https://ensemble-api.open-meteo.com/v1/ensemble")

POLL_SECONDS = 900
OFFSET_F = 3.2
INLAND = {"chicago", "atlanta", "denver", "dallas", "houston", "phoenix",
          "madrid", "berlin", "paris", "beijing", "toronto", "mexico city",
          "sao paulo", "moscow"}

# Known Polymarket cities → (lat, lon, tz)
CITIES = {
    "new york city": (40.7769, -73.8740, "America/New_York"),
    "los angeles": (34.0522, -118.2437, "America/Los_Angeles"),
    "chicago": (41.9742, -87.9073, "America/Chicago"),
    "miami": (25.7617, -80.1918, "America/New_York"),
    "houston": (29.9902, -95.3368, "America/Chicago"),
    "dallas": (32.8998, -97.0403, "America/Chicago"),
    "san francisco": (37.7749, -122.4194, "America/Los_Angeles"),
    "seattle": (47.6062, -122.3321, "America/Los_Angeles"),
    "atlanta": (33.6407, -84.4277, "America/New_York"),
    "denver": (39.8561, -104.6737, "America/Denver"),
    "philadelphia": (39.9526, -75.1652, "America/New_York"),
    "london": (51.4700, -0.4543, "Europe/London"),
    "paris": (49.0097, 2.5479, "Europe/Paris"),
    "madrid": (40.4936, -3.5668, "Europe/Madrid"),
    "berlin": (52.3667, 13.5033, "Europe/Berlin"),
    "moscow": (55.9726, 37.4146, "Europe/Moscow"),
    "istanbul": (41.0082, 28.9784, "Europe/Istanbul"),
    "tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
    "seoul": (37.5665, 126.9780, "Asia/Seoul"),
    "beijing": (40.0799, 116.6031, "Asia/Shanghai"),
    "hong kong": (22.3193, 114.1694, "Asia/Hong_Kong"),
    "singapore": (1.3521, 103.8198, "Asia/Singapore"),
    "mexico city": (19.4361, -99.0719, "America/Mexico_City"),
    "sao paulo": (-23.4356, -46.4731, "America/Sao_Paulo"),
    "toronto": (43.6777, -79.6248, "America/Toronto"),
}


def parse_position(title: str) -> dict | None:
    """Extract city, date, threshold from a position title."""
    t = title.lower()
    city = None
    for name in sorted(CITIES, key=len, reverse=True):
        if name in t:
            city = name; break
    if not city:
        return None
    # date (April DD)
    m = re.search(r"april\s+(\d{1,2})", t)
    if not m:
        return None
    day = int(m.group(1))
    year = 2026
    target_date = f"{year}-04-{day:02d}"
    # threshold — supports ≥/≤, bucket, and "be X°"
    out = {"city": city, "target_date": target_date}
    # Directional ≥ X°F or higher / ≤ X°F or below
    if "or higher" in t or "or above" in t:
        m = re.search(r"be\s+(\d{1,3}(?:\.\d+)?)\s*°?\s*[cf]?\s+or\s+(?:higher|above)", t)
        if m:
            v = float(m.group(1))
            unit = "c" if "°c" in t[:m.end()+3] else "f"
            if unit == "c": v = v * 9/5 + 32
            out["op"] = ">=" ; out["threshold_f"] = v
            return out
    if "or below" in t or "or lower" in t:
        m = re.search(r"be\s+(\d{1,3}(?:\.\d+)?)\s*°?\s*[cf]?\s+or\s+(?:below|lower)", t)
        if m:
            v = float(m.group(1))
            unit = "c" if "°c" in t[:m.end()+3] else "f"
            if unit == "c": v = v * 9/5 + 32
            out["op"] = "<=" ; out["threshold_f"] = v
            return out
    # Bucket: "between X-Y°F"
    m = re.search(r"between\s+(\d{1,3})-(\d{1,3})°f", t)
    if m:
        out["op"] = "in"
        out["bucket_lo"] = float(m.group(1))
        out["bucket_hi"] = float(m.group(2))
        return out
    # Bare "be X°C" — nearest-int bucket [X-0.5, X+0.5)
    m = re.search(r"be\s+(\d{1,3}(?:\.\d+)?)\s*°c", t)
    if m:
        v_c = float(m.group(1))
        v_f = v_c * 9/5 + 32
        out["op"] = "in"
        out["bucket_lo"] = v_f - 0.9  # ±0.5°C in F
        out["bucket_hi"] = v_f + 0.9
        return out
    return None


def fetch_positions() -> list[dict]:
    if not WALLET:
        return []
    try:
        r = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": WALLET, "sizeThreshold": 0.1},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        return [p for p in r.json() if isinstance(p, dict)]
    except Exception as e:
        log(f"[ERR] fetch positions: {e}")
        return []


def fetch_ensemble(lat, lon, tz, d):
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "models": "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_global",
        "start_date": d, "end_date": d,
        "temperature_unit": "fahrenheit", "timezone": tz,
    }
    if KEY: params["apikey"] = KEY
    r = requests.get(ENS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def daily_max(ens, d):
    times = ens["hourly"]["time"]
    out = []
    by_model = defaultdict(list)
    for key, vals in ens["hourly"].items():
        if key == "time" or not isinstance(vals, list): continue
        day = [v for t, v in zip(times, vals) if v is not None and t[:10] == d]
        if not day: continue
        m = max(day)
        out.append(m)
        for name in ["ecmwf", "gfs", "icon", "gem"]:
            if name in key:
                by_model[name.upper()].append(m); break
    return out, by_model


def daily_min(ens, d):
    times = ens["hourly"]["time"]
    out = []
    by_model = defaultdict(list)
    for key, vals in ens["hourly"].items():
        if key == "time" or not isinstance(vals, list): continue
        day = [v for t, v in zip(times, vals) if v is not None and t[:10] == d]
        if not day: continue
        m = min(day)
        out.append(m)
        for name in ["ecmwf", "gfs", "icon", "gem"]:
            if name in key:
                by_model[name.upper()].append(m); break
    return out, by_model


def band(p_win):
    if p_win >= 0.80: return "STRONG"
    if p_win >= 0.60: return "OK"
    if p_win >= 0.45: return "COINFLIP"
    if p_win >= 0.30: return "AGAINST"
    return "LOSING"


def log(line: str):
    with LOG_FILE.open("a") as f:
        f.write(f"{datetime.now().isoformat()}  {line}\n")


def main():
    log(f"=== dynamic position_watcher START — polling {POLL_SECONDS}s ===")
    last_band: dict[str, str] = {}
    ens_cache: dict[tuple, tuple] = {}  # (lat_r, lon_r, date, metric) → vals

    while True:
        positions = fetch_positions()
        tracked = 0
        # Flush cache each cycle (fresh forecast each poll)
        ens_cache.clear()

        for p in positions:
            cur = float(p.get("curPrice", 0))
            if cur < 0.05 or cur > 0.95: continue  # settled-ish, skip
            title = p.get("title", "") or ""
            if "temperature" not in title.lower() and "precip" not in title.lower():
                continue
            parsed = parse_position(title)
            if not parsed: continue
            side = p.get("outcome", "?")  # "Yes" or "No"
            city = parsed["city"]
            if city not in CITIES: continue
            lat, lon, tz = CITIES[city]
            d = parsed["target_date"]
            # Determine what metric to aggregate
            is_min = "lowest" in title.lower() or "minimum" in title.lower()
            cache_key = (round(lat, 2), round(lon, 2), d, "min" if is_min else "max")

            if cache_key not in ens_cache:
                try:
                    ens = fetch_ensemble(lat, lon, tz, d)
                    if is_min:
                        vals, by_model = daily_min(ens, d)
                    else:
                        vals, by_model = daily_max(ens, d)
                    ens_cache[cache_key] = (vals, by_model)
                except Exception as e:
                    log(f"[{title[:30]}] ensemble err: {e}")
                    continue
            vals, by_model = ens_cache[cache_key]
            if not vals: continue
            # Apply offset
            if city not in INLAND:
                vals = [v - OFFSET_F for v in vals]
                by_model = {m: [v - OFFSET_F for v in lst] for m, lst in by_model.items()}
            n = len(vals)
            srt = sorted(vals)
            median_v = srt[n // 2]

            # Compute p_win based on op and side
            op = parsed.get("op")
            p_win = None
            thr_desc = ""
            if op == ">=":
                thr = parsed["threshold_f"]
                p_above = sum(1 for v in vals if v >= thr) / n
                p_win = (1 - p_above) if side == "No" else p_above
                thr_desc = f"≥{thr:.0f}°F"
            elif op == "<=":
                thr = parsed["threshold_f"]
                p_below = sum(1 for v in vals if v <= thr) / n
                p_win = (1 - p_below) if side == "No" else p_below
                thr_desc = f"≤{thr:.0f}°F"
            elif op == "in":
                lo, hi = parsed["bucket_lo"], parsed["bucket_hi"]
                p_hit = sum(1 for v in vals if lo <= v <= hi) / n
                p_win = (1 - p_hit) if side == "No" else p_hit
                thr_desc = f"[{lo:.1f}-{hi:.1f}]°F"
            if p_win is None: continue

            b = band(p_win)
            key = title[:40]
            alert = ""
            if key in last_band and last_band[key] != b:
                alert = f"  🚨 {last_band[key]}→{b}"
            last_band[key] = b
            model_medians = {m: f"{sorted(lst)[len(lst)//2]:.1f}" for m, lst in by_model.items() if lst}
            log(f"[{city[:9]:<9} {d} {side} {thr_desc}]  p_win={p_win*100:.1f}%  "
                f"band={b}  median={median_v:.1f}°F  cur=${cur:.2f}  by={model_medians}{alert}")
            tracked += 1

        if tracked == 0:
            log("[no active weather positions to track]")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
