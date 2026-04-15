"""Main entry: list Polymarket weather markets, attach ensemble forecast,
compute P(event) vs market price, output edge table.

Run:  uv run python -m weather.edge
"""
from __future__ import annotations

import json
from datetime import date

from weather.markets import fetch_weather_events, extract_markets
from weather.parser import parse_market
from weather.sources import fetch_open_meteo_ensemble
from weather.model import compute_p_event


def run(today: date | None = None, verbose: bool = True) -> list[dict]:
    today = today or date.today()
    events = fetch_weather_events(limit=200)
    if verbose:
        print(f"fetched {len(events)} weather events")

    rows: list[dict] = []
    for ev in events:
        for m in extract_markets(ev):
            q = parse_market(m["question"], today)
            if not q:
                continue
            if not q.get("target_date"):
                continue
            if q["target_date"] < today or (q["target_date"] - today).days > 14:
                continue

            ens = fetch_open_meteo_ensemble(q["lat"], q["lon"],
                                            q["target_date"], q["target_date"])
            if not ens or "error" in ens:
                continue
            model_out = compute_p_event(ens, q)
            if not model_out or model_out.get("p_event") is None:
                continue

            # compare to market: assume first token = "Yes"/"Up" outcome,
            # use outcomePrices when available (mid; refine later with real book)
            prices = m.get("prices", [])
            if len(prices) >= 2:
                yes_price = prices[0]
            else:
                yes_price = None

            edge = None
            if yes_price is not None:
                edge = model_out["p_event"] - yes_price

            rows.append({
                "question": m["question"][:80],
                "city": q["city"],
                "target_date": q["target_date"].isoformat(),
                "metric": q["metric"],
                "op": q.get("op"),
                "threshold": q.get("threshold"),
                "our_p": round(model_out["p_event"], 3),
                "forecast": model_out["forecast"],
                "spread": model_out["spread"],
                "member_n": model_out["member_n"],
                "market_p": yes_price,
                "edge": round(edge, 3) if edge is not None else None,
                "conditionId": m["conditionId"],
                "tokens": m["tokens"],
            })

    rows.sort(key=lambda r: abs(r.get("edge") or 0), reverse=True)
    return rows


def main():
    rows = run(verbose=True)
    if not rows:
        print("no matchable weather markets found")
        return
    print(f"\n{len(rows)} markets with model output:\n")
    print(f"{'date':<12}{'city':<15}{'metric':<10}{'op':<4}{'thr':<8}"
          f"{'ours':<8}{'mkt':<8}{'edge':<8}{'fcst':<8}{'N':<4}question")
    for r in rows[:40]:
        print(f"{r['target_date']:<12}{r['city']:<15}{r['metric']:<10}"
              f"{str(r['op']):<4}{str(r['threshold']):<8}"
              f"{str(r['our_p']):<8}{str(r['market_p']):<8}"
              f"{str(r['edge']):<8}{str(r['forecast']):<8}{r['member_n']:<4}"
              f"{r['question']}")


if __name__ == "__main__":
    main()
