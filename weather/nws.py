"""NWS (National Weather Service) cross-check for US weather markets.

Independent second source beside Open-Meteo ensemble. We only bet confidently
when both models agree within a small temperature delta.

API: https://api.weather.gov (free, no auth). Flow is two-hop:
  1. GET /points/{lat},{lon} -> properties.forecast URL (gridpoint)
  2. GET that URL -> 12-hour periods (day/night) with temp + precip prob.

NWS updates hourly; we cache gridpoint responses for 1h to be polite.
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

NWS_POINTS = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
HEADERS = {
    "User-Agent": "poly-weather-research/1.0",
    "Accept": "application/geo+json",
}
CACHE_TTL = 3600  # 1 hour
_CACHE: dict[tuple[float, float], tuple[float, list[dict]]] = {}


def is_us_city(lat: float, lon: float) -> bool:
    """Rough CONUS bounding box check. Excludes AK/HI, but NWS covers those too;
    we keep it conservative because our confidence pairing is CONUS-focused."""
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -67.0


def _get_with_retry(url: str, tries: int = 3, timeout: int = 10) -> dict:
    """GET with retry on 502/503/504 and transient errors. Exponential backoff."""
    last_err: Optional[Exception] = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code in (502, 503, 504):
                last_err = RuntimeError(f"NWS {r.status_code} for {url}")
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"NWS request failed after {tries} tries: {last_err}")


def fetch_nws_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch 12-hour NWS periods for the gridpoint nearest (lat, lon).

    Returns list of dicts with normalized keys:
      start_time (datetime, tz-aware), end_time, temperature_f (int),
      is_daytime (bool), short_forecast (str), precip_prob (int or None).

    Cached per-(lat, lon) for 1h. Raises RuntimeError on persistent failure.
    """
    key = (round(lat, 4), round(lon, 4))
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    pt = _get_with_retry(NWS_POINTS.format(lat=lat, lon=lon))
    fc_url = pt["properties"]["forecast"]
    fc = _get_with_retry(fc_url)

    periods = []
    for p in fc["properties"]["periods"]:
        pop = p.get("probabilityOfPrecipitation") or {}
        periods.append({
            "start_time": datetime.fromisoformat(p["startTime"]),
            "end_time": datetime.fromisoformat(p["endTime"]),
            "temperature_f": p["temperature"] if p.get("temperatureUnit", "F") == "F"
                             else p["temperature"] * 9 / 5 + 32,
            "is_daytime": p["isDaytime"],
            "short_forecast": p.get("shortForecast", ""),
            "precip_prob": pop.get("value"),
        })
    _CACHE[key] = (now, periods)
    return periods


def nws_daily_minmax(periods: list[dict], target_date: date, tz: str) -> dict:
    """Reduce 12-hour periods to {max_f, min_f} for target_date in local tz.

    NWS periods are day/night in gridpoint-local time. Max-of-day comes from
    daytime periods that overlap target_date; min-of-day from nighttime periods
    whose start is the evening of target_date (NWS labels 'Tonight' as the night
    starting that evening, which contains the coldest hour near dawn next day).

    We use a more robust overlap test: include any period whose midpoint falls
    on target_date in local tz.
    """
    zone = ZoneInfo(tz)
    day_temps: list[float] = []
    night_temps: list[float] = []
    for p in periods:
        mid = p["start_time"] + (p["end_time"] - p["start_time"]) / 2
        mid_local = mid.astimezone(zone).date()
        if mid_local != target_date:
            continue
        if p["is_daytime"]:
            day_temps.append(p["temperature_f"])
        else:
            night_temps.append(p["temperature_f"])
    return {
        "max_f": max(day_temps) if day_temps else None,
        "min_f": min(night_temps) if night_temps else None,
    }


def cross_check(lat: float, lon: float, target_date: date, tz: str,
                om_max: Optional[float], om_min: Optional[float]) -> Optional[dict]:
    """Compare Open-Meteo daily max/min vs NWS for a US city.

    Returns None for non-US locations. Otherwise:
      {nws_max, nws_min, om_max, om_min, agree, delta_max_f, delta_min_f, confidence}
    confidence: 1.0 if worst delta <=2F, 0.5 if 2-5F, 0.0 if >5F.
    """
    if not is_us_city(lat, lon):
        return None
    try:
        periods = fetch_nws_forecast(lat, lon)
    except RuntimeError as e:
        return {"error": str(e), "confidence": 0.0, "agree": False}

    mm = nws_daily_minmax(periods, target_date, tz)
    nws_max, nws_min = mm["max_f"], mm["min_f"]

    def _delta(a, b):
        if a is None or b is None:
            return None
        return abs(a - b)

    dmax = _delta(nws_max, om_max)
    dmin = _delta(nws_min, om_min)
    worst = max([d for d in (dmax, dmin) if d is not None], default=None)

    if worst is None:
        confidence = 0.0
        agree = False
    elif worst <= 2.0:
        confidence = 1.0
        agree = True
    elif worst <= 5.0:
        confidence = 0.5
        agree = True
    else:
        confidence = 0.0
        agree = False

    return {
        "nws_max": nws_max, "nws_min": nws_min,
        "om_max": om_max, "om_min": om_min,
        "agree": agree,
        "delta_max_f": dmax, "delta_min_f": dmin,
        "confidence": confidence,
    }


def _cli(city_key: str) -> None:
    from weather.cities import CITIES
    from weather.sources import fetch_open_meteo_forecast

    if city_key not in CITIES:
        print(f"unknown city: {city_key}. options: {', '.join(sorted(CITIES))}")
        sys.exit(1)
    lat, lon, tz, _icao = CITIES[city_key]
    today = datetime.now(ZoneInfo(tz)).date()
    end = today + timedelta(days=3)

    om = fetch_open_meteo_forecast(lat, lon, today, end)
    daily = (om or {}).get("daily", {}) if isinstance(om, dict) else {}
    dates = daily.get("time", [])
    maxs = daily.get("temperature_2m_max", [])
    mins = daily.get("temperature_2m_min", [])
    om_by_date = {}
    for i, d in enumerate(dates):
        try:
            om_by_date[date.fromisoformat(d)] = (maxs[i], mins[i])
        except Exception:
            pass

    print(f"=== {city_key} ({lat:.3f},{lon:.3f}) tz={tz} ===")
    for i in range(1, 4):
        d = today + timedelta(days=i)
        om_max, om_min = om_by_date.get(d, (None, None))
        res = cross_check(lat, lon, d, tz, om_max, om_min)
        print(f"{d}: {res}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["seattle", "nyc", "chicago"]
    for a in args:
        _cli(a.lower())
