"""Upcoming-edges watchlist.

Shows day+2 and day+3 candidates that would be fireable once they roll into
day+1. These are NOT traded yet — they're just queued for tomorrow's scan.

Run:  uv run python -m weather.upcoming
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import requests

from weather.cities import CITIES, STATION_OFFSET_C

ROOT = Path(__file__).resolve().parent
EDGE_CSV = ROOT / "edge_table.csv"
INLAND = {"chicago", "atlanta", "denver", "dallas", "houston", "phoenix",
          "madrid", "berlin", "paris", "beijing", "toronto", "mexico city",
          "sao paulo", "moscow"}

RESET = "\033[0m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"; C = "\033[36m"


def main():
    if not EDGE_CSV.exists():
        print("No edge_table.csv — run weather.dump first")
        return
    with EDGE_CSV.open() as f:
        rows = list(csv.DictReader(f))

    today = date.today()
    by_days_out = {1: [], 2: [], 3: []}

    for r in rows:
        try:
            td = date.fromisoformat(r["target_date"])
            d = (td - today).days
        except Exception:
            continue
        if d not in by_days_out:
            continue
        if r.get("op") != "in":
            continue  # bucket-only for this view
        try:
            our = float(r["our_p"]); mkt = float(r["market_p"])
            fcst = float(r["forecast_f"])
            lo, hi = map(float, r["threshold"].split("-"))
        except Exception:
            continue
        city = r["city"]
        offset_f = 0 if city in INLAND else 3.2
        fcst_oracle = fcst - offset_f
        # NO cushion
        if fcst_oracle < lo - 3.0 or fcst_oracle > hi + 3.0:
            cushion = min(abs(fcst_oracle - lo), abs(fcst_oracle - hi))
            edge = mkt - our
            side = "NO"
        # YES cushion (forecast in bucket)
        elif lo <= fcst_oracle <= hi and our > 0.4 and mkt < 0.4:
            cushion = min(fcst_oracle - lo, hi - fcst_oracle)
            edge = our - mkt
            side = "YES"
        else:
            continue
        if edge < 0.15:
            continue
        by_days_out[d].append((cushion, edge, side, r))

    print(f"\n{B}═══ Upcoming edges — tracked but not yet fired ═══{RESET}")
    print(f"{B}Today: {today} — engine trades day 0 + day+1 only{RESET}\n")

    for days, label in [(2, "📅 Day+2 — will graduate to day+1 tomorrow"),
                         (3, "📅 Day+3 — on the bench for 2 days")]:
        items = sorted(by_days_out[days], key=lambda x: -x[0])
        if not items:
            continue
        color = Y if days == 2 else B
        print(f"{color}{label}{RESET}")
        print(f"  {'date':<12}{'city':<14}{'side':<5}{'cushion':<10}{'edge':<8}{'market':<8}{'our':<7}")
        for cushion, edge, side, r in items[:8]:
            mkt = float(r["market_p"])
            our = float(r["our_p"])
            print(f"  {r['target_date']:<12}{r['city']:<14}{side:<5}"
                  f"+{cushion:<8.1f}°F{edge:<8.2f}{mkt:<8.2f}{our:<7.2f}")
        print()

    # Closing note
    d1 = len(by_days_out[1])
    d2 = len(by_days_out[2])
    d3 = len(by_days_out[3])
    print(f"{C}Summary:{RESET} day+1 candidates={d1}  day+2 watch={d2}  day+3 watch={d3}")
    print(f"{C}Strategy:{RESET} fire day+1 now, re-check day+2 tomorrow when they roll forward")


if __name__ == "__main__":
    main()
