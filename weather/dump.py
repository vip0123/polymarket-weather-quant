"""Dump all parseable live weather markets → CSV with our P(event) vs market.

Run:  uv run python -m weather.dump
Output: weather/edge_table.csv
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import requests

from weather.markets import fetch_weather_events, extract_markets
from weather.parser import parse_market
from weather.sources import fetch_open_meteo_ensemble
from weather.model import compute_p_event
from weather.cities import get_offset_c

OUT_DIR = Path(__file__).resolve().parent
CSV_PATH = OUT_DIR / "edge_table.csv"


def fetch_temperature_events(limit: int = 200) -> list[dict]:
    """Additional discovery: search for 'temperature' / 'rain' in event titles."""
    results: list[dict] = []
    seen_ids: set = set()
    for query in ("temperature", "rain", "snow", "hottest", "coldest"):
        try:
            r = requests.get("https://gamma-api.polymarket.com/events",
                             params={"q": query, "limit": limit,
                                     "closed": "false", "active": "true"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            data = r.json() or []
            for e in data:
                if e.get("id") in seen_ids: continue
                seen_ids.add(e.get("id")); results.append(e)
        except Exception:
            continue
    return results


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def to_fahrenheit(q: dict) -> dict:
    """Normalize all Celsius thresholds to Fahrenheit for model compat."""
    if q.get("unit") == "C":
        if "threshold" in q:
            q["threshold"] = round(c_to_f(q["threshold"]), 1)
        if "threshold_low" in q:
            q["threshold_low"] = round(c_to_f(q["threshold_low"]), 1)
        if "threshold_high" in q:
            q["threshold_high"] = round(c_to_f(q["threshold_high"]), 1)
        q["unit"] = "F"
    return q


def p_event_with_range(ensemble: dict, q: dict) -> dict | None:
    """Handle 'in' (range) queries that compute_p_event doesn't natively do."""
    if q.get("op") != "in":
        return compute_p_event(ensemble, q)

    # Derive per-member daily agg, then count members in range
    hourly = ensemble.get("hourly", {})
    times = hourly.get("time", [])
    target = q.get("target_date").isoformat()
    metric = q.get("metric", "max_temp")
    field_prefix = "temperature_2m" if metric != "precip_in" else "precipitation"
    agg = max if metric == "max_temp" else (min if metric == "min_temp" else sum)

    vals = []
    for key, series in hourly.items():
        if not key.startswith(field_prefix) or key == "time":
            continue
        if not isinstance(series, list): continue
        day = [v for t, v in zip(times, series)
               if v is not None and t[:10] == target]
        if day: vals.append(agg(day))
    if not vals: return None
    lo, hi = q["threshold_low"], q["threshold_high"]
    hits = sum(1 for v in vals if lo <= v <= hi)
    from statistics import median, stdev
    return {
        "p_event": hits / len(vals),
        "member_n": len(vals),
        "forecast": round(median(vals), 2),  # median for robustness vs outliers
        "spread": round(stdev(vals), 2) if len(vals) > 1 else 0.0,
        "members": {},
    }


def collect_events() -> list[dict]:
    events = fetch_weather_events(limit=200)
    # augment with keyword-based discovery
    events += fetch_temperature_events(limit=200)
    # dedupe
    seen = set(); uniq = []
    for e in events:
        if e.get("id") in seen: continue
        seen.add(e.get("id")); uniq.append(e)
    return uniq


def main():
    today = date.today()
    events = collect_events()
    print(f"fetched {len(events)} unique weather/temperature events")

    # Forecast cache: (lat_r, lon_r, date) → ensemble JSON. Most queries
    # share city+date so this reduces API calls by ~20x.
    ens_cache: dict[tuple, dict] = {}

    rows: list[dict] = []
    parsed_ct = forecast_ct = 0
    for ev in events:
        for m in extract_markets(ev):
            q = parse_market(m["question"], today)
            if not q: continue
            parsed_ct += 1
            to_fahrenheit(q)
            td = q.get("target_date")
            if not td or td < today or (td - today).days > 10:
                continue
            cache_key = (round(q["lat"], 2), round(q["lon"], 2), td.isoformat())
            if cache_key in ens_cache:
                ens = ens_cache[cache_key]
            else:
                ens = fetch_open_meteo_ensemble(q["lat"], q["lon"], td, td)
                ens_cache[cache_key] = ens
                if parsed_ct % 10 == 0:
                    print(f"  [progress] {parsed_ct} markets parsed, "
                          f"{len(ens_cache)} unique forecasts cached")
            if not ens or "error" in ens:
                continue
            # Apply per-city station offset to the threshold (equivalent to
            # shifting our forecast). If offset=-1.8°C (oracle reads cooler),
            # we effectively raise the threshold on our forecast scale.
            offset_c = get_offset_c(q["city"])
            if offset_c != 0:
                offset_f = offset_c * 9 / 5
                q_adj = dict(q)
                if "threshold" in q_adj and q_adj["threshold"] is not None:
                    # Oracle reads cooler → our forecast needs to be HIGHER than
                    # raw threshold for YES to hit. So raise threshold by -offset.
                    q_adj["threshold"] = q_adj["threshold"] - offset_f
                if "threshold_low" in q_adj:
                    q_adj["threshold_low"] = q_adj["threshold_low"] - offset_f
                if "threshold_high" in q_adj:
                    q_adj["threshold_high"] = q_adj["threshold_high"] - offset_f
                q = q_adj
            model_out = p_event_with_range(ens, q)
            if not model_out or model_out.get("p_event") is None:
                continue
            forecast_ct += 1

            prices = m.get("prices", [])
            yes_price = prices[0] if len(prices) >= 1 else None
            edge = (model_out["p_event"] - yes_price) if yes_price is not None else None

            rows.append({
                "target_date": td.isoformat(),
                "city": q["city"],
                "metric": q["metric"],
                "op": q.get("op"),
                "threshold": q.get("threshold") or f"{q.get('threshold_low')}-{q.get('threshold_high')}",
                "our_p": round(model_out["p_event"], 3),
                "market_p": yes_price,
                "edge": round(edge, 3) if edge is not None else None,
                "forecast_f": model_out["forecast"],
                "spread_f": model_out["spread"],
                "members": model_out["member_n"],
                "question": m["question"][:100],
                "conditionId": m["conditionId"],
                "tokens": json.dumps(m["tokens"]),
            })

    rows.sort(key=lambda r: abs(r.get("edge") or 0), reverse=True)

    if rows:
        fields = list(rows[0].keys())
        with CSV_PATH.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows → {CSV_PATH}")
    else:
        print("no parseable live weather markets with forecasts")

    print(f"\nparsed {parsed_ct} markets, {forecast_ct} got forecasts")
    print(f"\ntop-20 by |edge|:")
    print(f"{'date':<12}{'city':<14}{'metric':<10}{'op':<5}{'thr':<14}"
          f"{'our':<7}{'mkt':<7}{'edge':<7}{'fcst':<7} question")
    for r in rows[:20]:
        print(f"{r['target_date']:<12}{r['city']:<14}{r['metric']:<10}"
              f"{str(r['op']):<5}{str(r['threshold']):<14}"
              f"{str(r['our_p']):<7}{str(r['market_p']):<7}"
              f"{str(r['edge']):<7}{str(r['forecast_f']):<7} {r['question']}")


if __name__ == "__main__":
    main()
