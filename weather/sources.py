"""Weather data sources.

Open-Meteo ensemble is the main source — free, no auth, multi-model.
NOAA NWS API for US cities as cross-check.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import requests

OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
NWS_POINTS = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"

# Pick a handful of ECMWF+GFS+GEM ensemble members; add ICON for redundancy
ENS_MODELS = "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_global"


def fetch_open_meteo_ensemble(lat: float, lon: float,
                              start: date, end: date) -> dict | None:
    """Fetch hourly temp/precip from multiple models. Returns raw JSON."""
    try:
        r = requests.get(OPEN_METEO_ENSEMBLE, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation,cloud_cover,wind_speed_10m",
            "models": ENS_MODELS,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto",
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def fetch_open_meteo_forecast(lat: float, lon: float,
                              start: date, end: date) -> dict | None:
    """Single-best-model forecast (default = blend). Includes daily max/min."""
    try:
        r = requests.get(OPEN_METEO_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_speed_10m_max",
            "hourly": "temperature_2m,precipitation,precipitation_probability",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto",
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def fetch_nws_forecast(lat: float, lon: float) -> dict | None:
    """NWS grid forecast for US cities. Adds authoritative source."""
    try:
        h = {"User-Agent": "poly-weather-research/1.0", "Accept": "application/geo+json"}
        pt = requests.get(NWS_POINTS.format(lat=lat, lon=lon), headers=h, timeout=10)
        pt.raise_for_status()
        fc_url = pt.json()["properties"]["forecast"]
        fc = requests.get(fc_url, headers=h, timeout=10)
        fc.raise_for_status()
        return fc.json()
    except Exception as e:
        return {"error": str(e)}
