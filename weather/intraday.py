"""Intra-day price adjustment for same-day weather markets.

For markets resolving TODAY, we have partial hourly observations already — blend
those with remaining-hours forecast rather than leaning solely on morning model
output. Open-Meteo's forecast endpoint with `past_days=1` backfills observed
hourlies alongside forthcoming forecast hours.

All functions return None for non-today target dates; this module is
intentionally single-day.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import requests

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 min


def _today_local(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _fetch_raw(lat: float, lon: float, tz: str) -> dict | None:
    key = (round(lat, 4), round(lon, 4), tz)
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]
    try:
        r = requests.get(OPEN_METEO_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "past_days": 1, "forecast_days": 1,
            "temperature_unit": "fahrenheit",
            "timezone": tz,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        _CACHE[key] = (now, data)
        return data
    except Exception:
        return None


def fetch_today_hourly(lat: float, lon: float, tz: str,
                       target_date: Optional[date] = None) -> dict | None:
    """Hourlies for today (local tz). Returns dict with times, temps_f,
    observed_until ('HH:MM' = current local hour). None if target_date != today."""
    today = _today_local(tz)
    if target_date is not None and target_date != today:
        return None
    raw = _fetch_raw(lat, lon, tz)
    if not raw or "hourly" not in raw:
        return None
    times = raw["hourly"]["time"]
    temps = raw["hourly"]["temperature_2m"]
    day_str = today.isoformat()
    filt = [(t, x) for t, x in zip(times, temps) if t.startswith(day_str) and x is not None]
    if not filt:
        return None
    now_local = datetime.now(ZoneInfo(tz))
    observed_until = now_local.strftime("%H:00")
    return {
        "times": [t for t, _ in filt],
        "temps_f": [float(x) for _, x in filt],
        "observed_until": observed_until,
    }


def current_state(lat: float, lon: float, tz: str,
                  target_date: Optional[date] = None) -> dict | None:
    """Observed max/min so far today + remaining-hours count. None if not today."""
    h = fetch_today_hourly(lat, lon, tz, target_date)
    if h is None:
        return None
    now_local = datetime.now(ZoneInfo(tz))
    cur_hour = now_local.hour
    observed = [(t, x) for t, x in zip(h["times"], h["temps_f"])
                if int(t[11:13]) <= cur_hour]
    if not observed:
        observed = [(h["times"][0], h["temps_f"][0])]
    temps_obs = [x for _, x in observed]
    max_so_far = max(temps_obs)
    min_so_far = min(temps_obs)
    peak_t = max(observed, key=lambda p: p[1])[0]
    peak_hour = peak_t[11:16]
    current_f = observed[-1][1]
    hours_remaining = max(0, 23 - cur_hour)
    return {
        "max_so_far_f": max_so_far,
        "min_so_far_f": min_so_far,
        "current_f": current_f,
        "hours_remaining": hours_remaining,
        "peak_hour": peak_hour,
    }


def _remaining_forecast(lat: float, lon: float, tz: str) -> list[float]:
    raw = _fetch_raw(lat, lon, tz)
    if not raw:
        return []
    today = _today_local(tz).isoformat()
    cur_hour = datetime.now(ZoneInfo(tz)).hour
    times = raw["hourly"]["time"]
    temps = raw["hourly"]["temperature_2m"]
    return [float(x) for t, x in zip(times, temps)
            if t.startswith(today) and int(t[11:13]) > cur_hour and x is not None]


def _check(op: str, val: float, thr: float, lo: float | None, hi: float | None) -> bool:
    # "in" treats bucket as [lo, hi+1) to match "86-87°F bucket" convention
    # (integer rounding of measured temp: 86.0..87.999 rounds into bucket ≤ 87).
    if op == ">=":
        return val >= thr
    if op == ">":
        return val > thr
    if op == "<=":
        return val <= thr
    if op == "<":
        return val < thr
    if op == "in":
        return lo <= val < (hi + 1.0)
    raise ValueError(f"bad op: {op}")


def p_event_intraday(lat: float, lon: float, tz: str, metric: str, op: str,
                     threshold: float | None = None,
                     threshold_low: float | None = None,
                     threshold_high: float | None = None,
                     target_date: Optional[date] = None,
                     n_samples: int = 2000, noise_std: float = 1.0) -> float | None:
    """P(event) given current obs + remaining-hours forecast. None if not today.

    metric: 'max_temp' or 'min_temp'
    op: '>=','>','<=','<','in' (for 'in' use threshold_low/high for a °F bucket)
    """
    st = current_state(lat, lon, tz, target_date)
    if st is None:
        return None
    remaining = _remaining_forecast(lat, lon, tz)
    thr = threshold
    lo, hi = threshold_low, threshold_high

    if metric == "max_temp":
        cur_best = st["max_so_far_f"]
        # Fast path: already-won cases for open-ended ops.
        if op in (">=", ">") and cur_best >= (thr if op == ">=" else thr + 1e-9):
            return 0.999
        if op in ("<=", "<") and cur_best > (thr if op == "<=" else thr - 1e-9):
            # Max already above threshold — cannot be ≤ anymore.
            return 0.001
        if not remaining:
            return 1.0 if _check(op, cur_best, thr or 0, lo, hi) else 0.0
        rng = np.random.default_rng(42)
        remaining_arr = np.array(remaining)
        samples = remaining_arr[None, :] + rng.normal(0, noise_std, (n_samples, len(remaining_arr)))
        sample_max = np.maximum(samples.max(axis=1), cur_best)
        hits = np.array([_check(op, v, thr or 0, lo, hi) for v in sample_max])
        return float(hits.mean())

    if metric == "min_temp":
        cur_min = st["min_so_far_f"]
        if op in ("<=", "<") and cur_min <= (thr if op == "<=" else thr - 1e-9):
            return 0.999
        if op in (">=", ">") and cur_min < (thr if op == ">=" else thr + 1e-9):
            return 0.001
        if not remaining:
            return 1.0 if _check(op, cur_min, thr or 0, lo, hi) else 0.0
        rng = np.random.default_rng(42)
        remaining_arr = np.array(remaining)
        samples = remaining_arr[None, :] + rng.normal(0, noise_std, (n_samples, len(remaining_arr)))
        sample_min = np.minimum(samples.min(axis=1), cur_min)
        hits = np.array([_check(op, v, thr or 0, lo, hi) for v in sample_min])
        return float(hits.mean())

    raise ValueError(f"unknown metric: {metric}")


def combine_forecast(raw_p: float, intraday_p: float, hours_remaining: int,
                     total_hours: int = 24) -> float:
    """Blend raw ensemble P with intraday-obs P. As the day progresses
    (hours_remaining shrinks), intraday gets more weight."""
    w = (total_hours - hours_remaining) / total_hours
    w = max(0.0, min(1.0, w))
    return w * intraday_p + (1 - w) * raw_p


def _demo() -> None:
    lat, lon, tz = 40.7128, -74.0060, "America/New_York"
    print(f"[NYC] today = {_today_local(tz)}")
    h = fetch_today_hourly(lat, lon, tz)
    print(f"hourly observed_until: {h['observed_until'] if h else None}, "
          f"n_hours: {len(h['times']) if h else 0}")
    st = current_state(lat, lon, tz)
    print(f"state: {st}")
    p_lo = p_event_intraday(lat, lon, tz, "max_temp", ">=", 85)
    p_hi = p_event_intraday(lat, lon, tz, "max_temp", ">=", 90)
    p_bucket = p_event_intraday(lat, lon, tz, "max_temp", "in",
                                threshold_low=86, threshold_high=87)
    print(f"P(high >= 85)       = {p_lo:.4f}")
    print(f"P(high >= 90)       = {p_hi:.4f}")
    print(f"P(high in [86,87])  = {p_bucket:.4f}")
    if st:
        blended = combine_forecast(raw_p=0.45, intraday_p=p_lo,
                                   hours_remaining=st["hours_remaining"])
        print(f"combine_forecast(raw=0.45, intraday={p_lo:.3f}, "
              f"hrs_left={st['hours_remaining']}) = {blended:.4f}")


if __name__ == "__main__":
    _demo()
