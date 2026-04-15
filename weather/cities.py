"""City registry for Polymarket weather markets.

Each entry: name → (airport_lat, airport_lon, tz, icao, city_center_lat, city_center_lon).

The FIRST lat/lon is the resolving airport's coords — this is what we query
Open-Meteo at, because Polymarket weather markets almost always resolve from
the city's primary airport METAR (via weatherapi.com or similar).

City center kept for reference / fallback.

Lesson learned 2026-04-15: using city-center coords for Seoul gave us a
1.8°C warm bias vs Polymarket's reading (which pulls from RKSI airport).
Using airport coords directly eliminates that systematic offset.
"""

# For INLAND airports: use airport coords directly (grid model gives correct temp)
# For COASTAL airports: Open-Meteo grid cell over water → bad forecast. Keep city coords + apply station_offset.
# Offsets are learned from reconciliation after markets resolve.
CITIES = {
    # US — (lat, lon, tz, icao)  — inland airports use airport; coastal use city + offset
    "new york":      (40.7769, -73.8740, "America/New_York",     "KLGA"),  # LGA inland-ish
    "nyc":           (40.7769, -73.8740, "America/New_York",     "KLGA"),
    "los angeles":   (34.0522, -118.2437, "America/Los_Angeles", "KLAX"),  # LAX is coastal — use city, offset
    "la":            (34.0522, -118.2437, "America/Los_Angeles", "KLAX"),
    "chicago":       (41.9742, -87.9073, "America/Chicago",      "KORD"),  # ORD inland — airport is fine
    "miami":         (25.7617, -80.1918, "America/New_York",     "KMIA"),  # MIA near bay — use city
    "houston":       (29.9902, -95.3368, "America/Chicago",      "KIAH"),  # IAH inland
    "phoenix":       (33.4373, -112.0078, "America/Phoenix",     "KPHX"),  # inland
    "dallas":        (32.8998, -97.0403, "America/Chicago",      "KDFW"),  # inland
    "boston":        (42.3601, -71.0589, "America/New_York",     "KBOS"),  # BOS coastal — use city
    "san francisco": (37.7749, -122.4194, "America/Los_Angeles", "KSFO"),  # SFO coastal — use city
    "seattle":       (47.6062, -122.3321, "America/Los_Angeles", "KSEA"),  # SEA coastal — use city
    "atlanta":       (33.6407, -84.4277, "America/New_York",     "KATL"),  # ATL inland
    "denver":        (39.8561, -104.6737, "America/Denver",      "KDEN"),  # inland
    "washington":    (38.9072, -77.0369, "America/New_York",     "KDCA"),  # near river — use city
    "dc":            (38.9072, -77.0369, "America/New_York",     "KDCA"),
    "philadelphia":  (39.9526, -75.1652, "America/New_York",     "KPHL"),  # PHL coastal — use city
    "las vegas":     (36.0840, -115.1537, "America/Los_Angeles", "KLAS"),  # inland
    # International
    "london":        (51.4700, -0.4543, "Europe/London",          "EGLL"),  # Heathrow inland
    "paris":         (49.0097, 2.5479, "Europe/Paris",            "LFPG"),  # CDG inland
    "tokyo":         (35.6762, 139.6503, "Asia/Tokyo",            "RJTT"),  # Haneda coastal — use city
    "moscow":        (55.9726, 37.4146, "Europe/Moscow",          "UUEE"),  # inland
    "dubai":         (25.2048, 55.2708, "Asia/Dubai",             "OMDB"),  # coastal
    "singapore":     (1.3521, 103.8198, "Asia/Singapore",         "WSSS"),  # island — use city
    "sydney":        (-33.8688, 151.2093, "Australia/Sydney",     "YSSY"),  # coastal
    "mumbai":        (19.0760, 72.8777, "Asia/Kolkata",           "VABB"),  # coastal
    "berlin":        (52.3667, 13.5033, "Europe/Berlin",          "EDDB"),  # BER inland
    "madrid":        (40.4936, -3.5668, "Europe/Madrid",          "LEMD"),  # Barajas inland
    "seoul":         (37.5665, 126.9780, "Asia/Seoul",            "RKSI"),  # Incheon is island — use city, offset
    "beijing":       (40.0799, 116.6031, "Asia/Shanghai",         "ZBAA"),  # inland
    "hong kong":     (22.3193, 114.1694, "Asia/Hong_Kong",        "VHHH"),  # island — use city
    "bangkok":       (13.6900, 100.7501, "Asia/Bangkok",          "VTBS"),  # inland
    "toronto":       (43.6777, -79.6248, "America/Toronto",       "CYYZ"),  # Pearson inland
    "mexico city":   (19.4361, -99.0719, "America/Mexico_City",   "MMMX"),  # inland
    "sao paulo":     (-23.4356, -46.4731, "America/Sao_Paulo",    "SBGR"),  # inland
    "istanbul":      (41.0082, 28.9784, "Europe/Istanbul",        "LTFM"),  # use city — new IST airport grid uncertain
    "rio de janeiro":(-22.9068, -43.1729, "America/Sao_Paulo",    "SBGL"),  # coastal
}


# Per-city station offset (°C) — how much cooler/warmer the oracle reads vs
# our Open-Meteo forecast. Learned from reconciliation after markets resolve.
# Applied BEFORE edge computation: effective_forecast_C = raw_forecast_C + offset.
STATION_OFFSET_C = {
    "seoul":       -1.8,   # CONFIRMED 2026-04-15: central Seoul +1.8°C warmer than RKSI oracle
    "los angeles": -2.8,   # HYPOTHESIS 2026-04-15: LAX coastal reads ~5°F (2.8°C) cooler
    "la":          -2.8,   # same as LA
    # below awaiting first-resolution confirmation
    # "miami":      -1.0,  # bayside, mild cooling
    # "san francisco": -2.0,  # strong marine layer
    # "seattle":    -1.0,  # Puget Sound
    # "boston":     -1.5,  # coastal
    # "hong kong":  -1.5,  # island airport
    # "singapore":  -1.0,  # island
    # "sydney":     -1.5,  # coastal
    # "tokyo":      -1.5,  # Haneda near bay
    # Default for unknown = 0
}


def get_offset_c(city: str) -> float:
    return STATION_OFFSET_C.get(city.lower(), 0.0)


def find_city(text: str) -> tuple | None:
    """Return (name, lat, lon, tz, icao). Word-boundary match."""
    import re as _re
    t = text.lower()
    matches = [n for n in CITIES if _re.search(r"\b" + _re.escape(n) + r"\b", t)]
    if not matches:
        return None
    best = max(matches, key=len)
    lat, lon, tz, icao = CITIES[best][:4]
    return (best, lat, lon, tz, icao)
