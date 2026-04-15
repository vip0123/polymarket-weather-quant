"""Parse Polymarket weather market questions into structured queries.

Example questions we handle:
  - "Highest temperature in NYC on April 15?"       → max_temp(nyc, 2026-04-15)
  - "Will NYC high be above 70°F on Tuesday?"       → max_temp > 70
  - "Will it rain in Miami on Friday?"              → precip > 0.01"
  - "High in Chicago above 50°F on April 15?"       → max_temp > 50

Returns dict: {"city": ..., "metric": "max_temp|min_temp|precip_in",
               "op": ">|<|>=|<=|=", "threshold": float, "target_date": date}
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from weather.cities import find_city

MONTH = {m: i for i, m in enumerate(
    ["january","february","march","april","may","june",
     "july","august","september","october","november","december"], 1)}
WDAY = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
        "friday":4,"saturday":5,"sunday":6}


def _parse_date(text: str, today: Optional[date] = None) -> Optional[date]:
    today = today or date.today()
    t = text.lower()
    # "April 15" / "April 15, 2026"
    m = re.search(r"(january|february|march|april|may|june|july|august|"
                  r"september|october|november|december)\s+(\d{1,2})(?:,?\s+(\d{4}))?", t)
    if m:
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(yr, MONTH[m.group(1)], int(m.group(2)))
            # if parsed date already passed this year and no year given, roll to next
            if d < today and not m.group(3): d = d.replace(year=yr+1)
            return d
        except ValueError:
            pass
    # weekday → next occurrence
    for name, idx in WDAY.items():
        if name in t:
            days_ahead = (idx - today.weekday()) % 7 or 7
            return today + timedelta(days=days_ahead)
    # "today" / "tomorrow"
    if "tomorrow" in t: return today + timedelta(days=1)
    if "today" in t: return today
    # ISO date
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except: pass
    return None


def parse_market(question: str, today: Optional[date] = None) -> dict | None:
    """Return structured query or None if not a recognized weather question."""
    q = question.strip()
    city = find_city(q)
    if not city:
        return None

    target_date = _parse_date(q, today)

    # Metric + operator + threshold detection
    ql = q.lower()
    out = {
        "city": city[0], "lat": city[1], "lon": city[2],
        "tz": city[3], "icao": city[4],
        "target_date": target_date,
        "question": q,
    }

    # detect Celsius vs Fahrenheit unit (defaults to F)
    unit = "C" if re.search(r"°?\s*c\b", ql) else "F"
    out["unit"] = unit

    # Order matters: specific directional phrasings BEFORE exact-match.
    # 1. "between X-Y" or "X-Y°F" range bucket → exact range
    m = re.search(r"between\s+(-?\d{1,3}(?:\.\d+)?)\s*(?:°?\s*[fFcC]?\s*)?(?:-|to|and)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql)
    if not m:
        m = re.search(r"be\s+(-?\d{1,3}(?:\.\d+)?)\s*-\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql)
    if m:
        out["op"] = "in"
        out["threshold_low"] = float(m.group(1))
        out["threshold_high"] = float(m.group(2))
    # 2. "X°F or higher/above/more" — check BEFORE bare "be X" so we don't
    #    swallow "be 19°C or higher" as exact match.
    elif re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?\s*(?:or\s+(?:higher|greater|more|above|hotter|warmer))", ql):
        mm = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?\s*(?:or\s+(?:higher|greater|more|above|hotter|warmer))", ql)
        out["op"] = ">="
        out["threshold"] = float(mm.group(1))
    # 3. "X or lower/less/below/cooler"
    elif re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?\s*(?:or\s+(?:lower|less|below|cooler|colder))", ql):
        mm = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?\s*(?:or\s+(?:lower|less|below|cooler|colder))", ql)
        out["op"] = "<="
        out["threshold"] = float(mm.group(1))
    # 4. "above X", "over X", "reach X"
    elif re.search(r"(above|over|exceed|greater than|>|at least|reach|hit)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql):
        mm = re.search(r"(above|over|exceed|greater than|>|at least|reach|hit)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql)
        out["op"] = ">="
        out["threshold"] = float(mm.group(2))
    # 5. "below X"
    elif re.search(r"(below|under|less than|<)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql):
        mm = re.search(r"(below|under|less than|<)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]?", ql)
        out["op"] = "<="
        out["threshold"] = float(mm.group(2))
    # 6. Bare "be X°F/°C" — Polymarket resolves by NEAREST-INTEGER rounding,
    #    so "be 17°C" = high rounds to 17 = [16.5, 17.5). Confirmed empirically
    #    against maskache2's fills + actual observations (Paris 18.0 = YES for 18°C).
    elif re.search(r"\bbe\s+(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]\b", ql):
        mm = re.search(r"\bbe\s+(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[fFcC]\b", ql)
        v = float(mm.group(1))
        out["op"] = "in"
        out["threshold_low"] = v - 0.5
        out["threshold_high"] = v + 0.5

    # metric (word-boundary — "below"/"blown" must not trigger "low")
    if any(w in ql for w in ["rain", "precipitation", "precip", "snow"]):
        out["metric"] = "precip_in"
        if "threshold" not in out:
            out["op"] = ">="; out["threshold"] = 0.01
    elif re.search(r"\b(lowest|minimum|coldest|min)\b", ql):
        out["metric"] = "min_temp"
    else:
        out["metric"] = "max_temp"

    return out
