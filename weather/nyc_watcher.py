"""NYC ≥77°F Apr 17 NO watcher.

Runs every 15 min. Pulls fresh 143-member ensemble, applies coastal offset,
computes p_win (probability NYC does NOT breach 77°F).

Writes status to dashboard/runtime/nyc_watcher.log.
Writes ALERT line when p_win crosses key thresholds (0.75, 0.50, 0.30).

Run as daemon:
  nohup .venv/bin/python3 -m weather.nyc_watcher > /dev/null 2>&1 &
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "dashboard" / "runtime" / "nyc_watcher.log"

NYC_LAT, NYC_LON = 40.7769, -73.8740
TZ = "America/New_York"
TARGET_DATE = "2026-04-17"
THRESHOLD_F = 77.0
OFFSET_F = 3.2  # coastal
POLL_SECONDS = 900  # 15 min

KEY = os.environ.get("OPEN_METEO_API_KEY", "").strip()
ENSEMBLE_HOST = (
    "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
    if KEY else "https://ensemble-api.open-meteo.com/v1/ensemble"
)
FORECAST_HOST = (
    "https://customer-api.open-meteo.com/v1/forecast"
    if KEY else "https://api.open-meteo.com/v1/forecast"
)


def fetch_ensemble():
    params = {
        "latitude": NYC_LAT, "longitude": NYC_LON,
        "hourly": "temperature_2m",
        "models": "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_global",
        "start_date": TARGET_DATE, "end_date": TARGET_DATE,
        "temperature_unit": "fahrenheit", "timezone": TZ,
    }
    if KEY: params["apikey"] = KEY
    r = requests.get(ENSEMBLE_HOST, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_single():
    params = {
        "latitude": NYC_LAT, "longitude": NYC_LON,
        "daily": "temperature_2m_max",
        "start_date": TARGET_DATE, "end_date": TARGET_DATE,
        "temperature_unit": "fahrenheit", "timezone": TZ,
    }
    if KEY: params["apikey"] = KEY
    r = requests.get(FORECAST_HOST, params=params, timeout=15)
    return r.json().get("daily", {}).get("temperature_2m_max", [None])[0]


def analyze(ens):
    times = ens["hourly"]["time"]
    maxes = []
    by_model = defaultdict(list)
    for key, vals in ens["hourly"].items():
        if key == "time" or not isinstance(vals, list): continue
        day = [v for t, v in zip(times, vals) if v is not None and t[:10] == TARGET_DATE]
        if not day: continue
        m = max(day)
        maxes.append(m)
        for name in ["ecmwf", "gfs", "icon", "gem"]:
            if name in key:
                by_model[name.upper()].append(m)
                break
    adj = [v - OFFSET_F for v in maxes]  # apply coastal offset
    n = len(adj)
    if n == 0: return None
    srt = sorted(adj)
    return {
        "n_members": n,
        "median": srt[n // 2],
        "p10": srt[n // 10] if n >= 10 else srt[0],
        "p90": srt[(n * 9) // 10] if n >= 10 else srt[-1],
        "min": srt[0], "max": srt[-1],
        "p_above_77": sum(1 for v in adj if v >= THRESHOLD_F) / n,
        "p_win_NO": sum(1 for v in adj if v < THRESHOLD_F) / n,
        "by_model": {m: {"median": sorted(v)[len(v)//2], "max": max(v), "min": min(v)}
                     for m, v in by_model.items() if v},
    }


def log(line: str):
    with LOG_FILE.open("a") as f:
        f.write(f"{datetime.now().isoformat()}  {line}\n")
    print(f"{datetime.now().strftime('%H:%M:%S')}  {line}")


def main():
    log(f"=== NYC ≥77°F Apr 17 watcher START (threshold={THRESHOLD_F}°F, offset={OFFSET_F}°F) ===")
    last_alert_band = None
    while True:
        try:
            ens = fetch_ensemble()
            stats = analyze(ens)
            single = fetch_single()
            if stats is None:
                log("[ERR] no ensemble data"); time.sleep(POLL_SECONDS); continue
            p_win = stats["p_win_NO"]
            # Classify band
            if p_win >= 0.75: band = "STRONG"
            elif p_win >= 0.50: band = "COINFLIP"
            elif p_win >= 0.30: band = "AGAINST"
            else: band = "LOSING"
            alert = " 🚨 BAND CHANGE" if last_alert_band and band != last_alert_band else ""
            last_alert_band = band
            em_median_c = {m: v['median'] for m, v in stats['by_model'].items()}
            log(f"p_win={p_win*100:.1f}%  band={band}{alert}  median={stats['median']:.1f}°F  "
                f"single={single}°F  members={stats['n_members']}  "
                f"p10={stats['p10']:.1f} p90={stats['p90']:.1f}  by={em_median_c}")
        except Exception as e:
            log(f"[ERR] {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
