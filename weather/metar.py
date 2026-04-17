"""Direct METAR station polling — reads the ACTUAL airport observation
that Polymarket's oracle uses. Bypasses Open-Meteo grid + offset entirely.

Sources:
  1. Iowa State Mesonet (free, no API key) — ASOS/METAR for US + intl stations
  2. weatherapi.com (optional, needs WEATHERAPI_KEY in .env)

Usage:
  from weather.metar import metar_current, metar_peak_today, metar_hourly

  # Current obs at O'Hare
  cur = metar_current("KORD")  # → {"temp_f": 72.3, "time": "2026-04-17T14:53"}

  # Today's peak so far at Incheon
  peak = metar_peak_today("RKSI", "Asia/Seoul")  # → 21.3°C / 70.3°F

  # Hourly obs for cushion validation
  obs = metar_hourly("KLGA", "2026-04-17", "America/New_York")
"""
from __future__ import annotations

import os
import time
from datetime import date as dt_date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "").strip()

# Module-level cache: (station, date_str) → {data, fetched_at}
_CACHE: dict[tuple, dict] = {}
CACHE_TTL = 300  # 5 min


def _cache_get(key: tuple) -> dict | None:
    entry = _CACHE.get(key)
    if entry and time.time() - entry["fetched_at"] < CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: tuple, data: dict):
    _CACHE[key] = {"data": data, "fetched_at": time.time()}


# ─── Iowa State Mesonet (free, primary) ─────────────────────────────

# Network lookup for US states (ICAO prefix → network)
_US_NETWORKS = {
    "K": {  # US stations start with K
        "AL": "AL_ASOS", "AK": "AK_ASOS", "AZ": "AZ_ASOS", "AR": "AR_ASOS",
        "CA": "CA_ASOS", "CO": "CO_ASOS", "CT": "CT_ASOS", "DE": "DE_ASOS",
        "FL": "FL_ASOS", "GA": "GA_ASOS", "HI": "HI_ASOS", "ID": "ID_ASOS",
        "IL": "IL_ASOS", "IN": "IN_ASOS", "IA": "IA_ASOS", "KS": "KS_ASOS",
        "KY": "KY_ASOS", "LA": "LA_ASOS", "ME": "ME_ASOS", "MD": "MD_ASOS",
        "MA": "MA_ASOS", "MI": "MI_ASOS", "MN": "MN_ASOS", "MS": "MS_ASOS",
        "MO": "MO_ASOS", "MT": "MT_ASOS", "NE": "NE_ASOS", "NV": "NV_ASOS",
        "NH": "NH_ASOS", "NJ": "NJ_ASOS", "NM": "NM_ASOS", "NY": "NY_ASOS",
        "NC": "NC_ASOS", "ND": "ND_ASOS", "OH": "OH_ASOS", "OK": "OK_ASOS",
        "OR": "OR_ASOS", "PA": "PA_ASOS", "RI": "RI_ASOS", "SC": "SC_ASOS",
        "SD": "SD_ASOS", "TN": "TN_ASOS", "TX": "TX_ASOS", "UT": "UT_ASOS",
        "VT": "VT_ASOS", "VA": "VA_ASOS", "WA": "WA_ASOS", "WV": "WV_ASOS",
        "WI": "WI_ASOS", "WY": "WY_ASOS",
    }
}

# ICAO → state mapping for common airports
_ICAO_STATE = {
    "KLGA": "NY", "KJFK": "NY", "KEWR": "NJ",
    "KORD": "IL", "KMDW": "IL",
    "KLAX": "CA", "KSFO": "CA", "KSAN": "CA",
    "KATL": "GA", "KDEN": "CO", "KDFW": "TX", "KIAH": "TX",
    "KMIA": "FL", "KBOS": "MA", "KSEA": "WA", "KPHX": "AZ",
    "KDCA": "VA", "KPHL": "PA", "KLAS": "NV",
    "CYYZ": "ON",  # Toronto — use CA network
}


def _get_network(icao: str) -> str:
    """Map ICAO code to Iowa Mesonet network name."""
    state = _ICAO_STATE.get(icao.upper())
    if state and icao[0] == "K":
        return f"{state}_ASOS"
    if icao.startswith("C"):
        return "CA_ASOS"  # Canadian
    # International — try the country-level ASOS
    return ""


def metar_current_mesonet(icao: str) -> dict | None:
    """Fetch current METAR obs from Iowa State Mesonet. Returns
    {temp_f, temp_c, dewpoint_f, wind_mph, time_utc} or None."""
    key = (icao, "current")
    cached = _cache_get(key)
    if cached: return cached
    network = _get_network(icao)
    if not network: return None
    try:
        r = requests.get(
            f"https://mesonet.agron.iastate.edu/json/current.py",
            params={"station": icao, "network": network},
            timeout=8,
        )
        d = r.json()
        if "last_ob" not in d: return None
        ob = d["last_ob"]
        result = {
            "temp_f": ob.get("tmpf"),
            "temp_c": ob.get("tmpc"),
            "dewpoint_f": ob.get("dwpf"),
            "wind_mph": ob.get("sknt", 0) * 1.151 if ob.get("sknt") else None,
            "time_utc": ob.get("utc_valid"),
            "station": icao,
        }
        _cache_set(key, result)
        return result
    except Exception:
        return None


def metar_hourly_mesonet(icao: str, date_s: str, tz: str) -> list[dict]:
    """Fetch hourly METAR obs for a specific date. Returns list of
    {time_local, temp_f} sorted by time."""
    key = (icao, date_s)
    cached = _cache_get(key)
    if cached: return cached
    network = _get_network(icao)
    if not network: return []
    try:
        d = dt_date.fromisoformat(date_s)
        d2 = d + timedelta(days=1)
        r = requests.get(
            "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
            params={
                "station": icao, "data": "tmpf",
                "year1": d.year, "month1": d.month, "day1": d.day,
                "year2": d2.year, "month2": d2.month, "day2": d2.day,
                "tz": tz, "format": "onlycomma",
                "latlon": "no", "elev": "no",
                "missing": "M", "trace": "T", "direct": "no",
                "report_type": "3",
            },
            timeout=10,
        )
        lines = r.text.strip().split("\n")
        if not lines: return []
        obs = []
        for line in lines:
            parts = line.split(",")
            if len(parts) < 3: continue
            try:
                tf_str = parts[2].strip()
                if tf_str == "M" or tf_str == "": continue  # missing
                tf = float(tf_str)
                t_str = parts[1].strip()
                obs.append({"time_local": t_str, "temp_f": tf})
            except (ValueError, IndexError):
                continue
        _cache_set(key, obs)
        return obs
    except Exception:
        return []


# ─── weatherapi.com (optional, paid) ────────────────────────────────

def metar_current_weatherapi(lat: float, lon: float) -> dict | None:
    """Fetch current conditions via weatherapi.com (if key set)."""
    if not WEATHERAPI_KEY: return None
    key = ("wapi", f"{lat:.2f},{lon:.2f}", "current")
    cached = _cache_get(key)
    if cached: return cached
    try:
        r = requests.get(
            "http://api.weatherapi.com/v1/current.json",
            params={"key": WEATHERAPI_KEY, "q": f"{lat},{lon}"},
            timeout=8,
        )
        d = r.json()
        cur = d.get("current", {})
        result = {
            "temp_f": cur.get("temp_f"),
            "temp_c": cur.get("temp_c"),
            "feelslike_f": cur.get("feelslike_f"),
            "wind_mph": cur.get("wind_mph"),
            "time_utc": cur.get("last_updated"),
            "source": "weatherapi.com",
        }
        _cache_set(key, result)
        return result
    except Exception:
        return None


# ─── Unified interface ───────────────────────────────────────────────

def metar_current(icao: str, tz: str = "UTC",
                   lat: float = None, lon: float = None) -> dict | None:
    """Best-effort current obs. Uses hourly ASOS (most reliable), falls
    back to current.py then weatherapi."""
    # Try ASOS hourly for today — last entry is most recent
    today_s = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    obs = metar_hourly_mesonet(icao, today_s, tz)
    if obs:
        latest = obs[-1]
        return {
            "temp_f": latest["temp_f"],
            "temp_c": round((latest["temp_f"] - 32) * 5 / 9, 1),
            "time_local": latest["time_local"],
            "station": icao,
            "source": "mesonet_asos",
        }
    result = metar_current_mesonet(icao)
    if result and result.get("temp_f") is not None:
        return result
    if lat is not None and lon is not None:
        return metar_current_weatherapi(lat, lon)
    return None


def metar_peak_today(icao: str, tz: str, metric: str = "max_temp") -> dict | None:
    """Returns {peak_f, peak_c, obs_count, peak_time} for today at the station.
    This is the ACTUAL OBSERVED high — no forecast, no offset."""
    today_s = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    obs = metar_hourly_mesonet(icao, today_s, tz)
    if not obs:
        return None
    temps = [o["temp_f"] for o in obs if o.get("temp_f") is not None]
    if not temps:
        return None
    if metric == "min_temp":
        peak = min(temps)
        peak_time = next(o["time_local"] for o in obs if o["temp_f"] == peak)
    else:
        peak = max(temps)
        peak_time = next(o["time_local"] for o in obs if o["temp_f"] == peak)
    return {
        "peak_f": peak,
        "peak_c": round((peak - 32) * 5 / 9, 1),
        "obs_count": len(temps),
        "peak_time": peak_time,
        "station": icao,
    }


def metar_cushion(icao: str, tz: str, threshold_f: float, op: str,
                  side: str, metric: str = "max_temp") -> dict | None:
    """Compute REAL cushion from actual METAR obs (not forecast).
    For today-resolving markets, this is the ground truth.
    Returns {cushion_f, peak_f, resolved: bool, wins: bool}."""
    peak_data = metar_peak_today(icao, tz, metric)
    if not peak_data:
        return None
    peak = peak_data["peak_f"]
    if op == ">=":
        yes_true = peak >= threshold_f
    elif op == "<=":
        yes_true = peak <= threshold_f
    elif op == "in":
        # caller should pass threshold_f as tuple (lo, hi) but we handle float
        yes_true = False  # can't determine from single threshold
    else:
        return None
    wins = (yes_true and side == "YES") or (not yes_true and side == "NO")
    if side == "YES":
        cushion = (peak - threshold_f) if op == ">=" else (threshold_f - peak)
    else:
        cushion = (threshold_f - peak) if op == ">=" else (peak - threshold_f)
    return {
        "cushion_f": round(cushion, 1),
        "peak_f": peak,
        "peak_c": peak_data["peak_c"],
        "obs_count": peak_data["obs_count"],
        "peak_time": peak_data["peak_time"],
        "wins": wins,
        "station": icao,
    }


# ─── CLI demo ────────────────────────────────────────────────────────

if __name__ == "__main__":
    from weather.cities import CITIES
    demo_stations = [
        ("chicago", "KORD", "America/Chicago"),
        ("new york", "KLGA", "America/New_York"),
        ("los angeles", "KLAX", "America/Los_Angeles"),
        ("seattle", "KSEA", "America/Los_Angeles"),
        ("dallas", "KDFW", "America/Chicago"),
        ("atlanta", "KATL", "America/New_York"),
    ]
    for city, icao, tz in demo_stations:
        cur = metar_current(icao)
        peak = metar_peak_today(icao, tz)
        if cur:
            print(f"{city:14} {icao}  current: {cur.get('temp_f')}°F  "
                  f"peak: {peak.get('peak_f') if peak else '?'}°F "
                  f"({peak.get('obs_count',0)} obs) "
                  f"at {peak.get('peak_time','?') if peak else '?'}")
        else:
            print(f"{city:14} {icao}  no data")
