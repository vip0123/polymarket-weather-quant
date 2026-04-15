"""Reconcile weather trades against observed outcomes.

Reads dashboard/runtime/weather_trader_trades.csv, resolves each settled trade
against Open-Meteo observations, computes realized P&L and calibration stats.
Run: uv run python -m weather.reconcile
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from weather.parser import parse_market

TRADES_CSV = Path("dashboard/runtime/weather_trader_trades.csv")
OUT_CSV = Path("weather/reconciled.csv")
OUT_JSON = Path("weather/calibration.json")

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _mm_to_in(mm: float) -> float:
    return mm / 25.4


def _http_json(url: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[warn] fetch failed: {e}", file=sys.stderr)
        return None


def fetch_observed(lat: float, lon: float, tz: str, d: date) -> Optional[dict]:
    """Return {max_f, min_f, precip_in} for (lat, lon) on date d, or None."""
    today = date.today()
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
    }
    # Archive has ~2-day lag; forecast with past_days handles recent dates.
    use_archive = d <= today - timedelta(days=3)
    if use_archive:
        params["start_date"] = d.isoformat()
        params["end_date"] = d.isoformat()
        url = ARCHIVE + "?" + urllib.parse.urlencode(params)
    else:
        past = max(1, (today - d).days + 1)
        params["past_days"] = min(past, 92)
        params["forecast_days"] = 1
        url = FORECAST + "?" + urllib.parse.urlencode(params)
    js = _http_json(url)
    if not js or "daily" not in js:
        return None
    daily = js["daily"]
    try:
        idx = daily["time"].index(d.isoformat())
    except (ValueError, KeyError):
        return None
    try:
        tmax = daily["temperature_2m_max"][idx]
        tmin = daily["temperature_2m_min"][idx]
        prcp = daily["precipitation_sum"][idx]
        if tmax is None or tmin is None:
            return None
        return {
            "max_f": _c_to_f(tmax),
            "min_f": _c_to_f(tmin),
            "precip_in": _mm_to_in(prcp or 0.0),
        }
    except (IndexError, KeyError, TypeError):
        return None


def _threshold_met(parsed: dict, obs: dict) -> Optional[bool]:
    metric = parsed.get("metric", "max_temp")
    unit = parsed.get("unit", "F")
    if metric == "precip_in":
        val = obs["precip_in"]
    elif metric == "min_temp":
        val = obs["min_f"]
    else:
        val = obs["max_f"]

    def to_f(x: float) -> float:
        if metric == "precip_in":
            return x
        return _c_to_f(x) if unit == "C" else x

    op = parsed.get("op")
    if op == "in":
        lo = to_f(parsed["threshold_low"])
        hi = to_f(parsed["threshold_high"])
        # "between X-Y" inclusive bucket: [lo, hi+1) in original unit
        # For the Celsius "be X" case parser already gave [X-0.5, X+0.5) → convert both bounds.
        # For the "X-Y°F" range case bounds are integer Fahrenheit; extend hi by 1°F if integer-ish.
        if unit == "F" and abs(hi - round(hi)) < 1e-6 and abs(lo - round(lo)) < 1e-6 \
           and (hi - lo) >= 1.0:
            hi = hi + 1.0
        return lo <= val < hi
    if op == ">=":
        return val >= to_f(parsed["threshold"])
    if op == "<=":
        return val <= to_f(parsed["threshold"])
    if op == ">":
        return val > to_f(parsed["threshold"])
    if op == "<":
        return val < to_f(parsed["threshold"])
    return None


def resolve_market(row_or_question, today: Optional[date] = None) -> dict:
    today = today or date.today()
    if isinstance(row_or_question, dict):
        question = row_or_question.get("question", "")
        target_date = row_or_question.get("target_date")
        if isinstance(target_date, str):
            try: target_date = date.fromisoformat(target_date)
            except Exception: target_date = None
    else:
        question = row_or_question
        target_date = None

    parsed = parse_market(question, today)
    if not parsed:
        return {"outcome": "PENDING", "observed": None, "threshold_met": None,
                "reason": "parse_failed"}
    td = target_date or parsed.get("target_date")
    if not td:
        return {"outcome": "PENDING", "observed": None, "threshold_met": None,
                "reason": "no_date"}
    if td > today:
        return {"outcome": "PENDING", "observed": None, "threshold_met": None,
                "reason": "future"}

    obs = fetch_observed(parsed["lat"], parsed["lon"], parsed["tz"], td)
    if not obs:
        return {"outcome": "PENDING", "observed": None, "threshold_met": None,
                "reason": "no_observation"}
    met = _threshold_met(parsed, obs)
    if met is None:
        return {"outcome": "PENDING", "observed": obs, "threshold_met": None,
                "reason": "no_threshold"}
    return {"outcome": "YES" if met else "NO", "observed": obs,
            "threshold_met": bool(met), "metric": parsed.get("metric"),
            "parsed": parsed}


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def reconcile_trades(csv_path: Path = TRADES_CSV) -> list[dict]:
    if not csv_path.exists():
        print(f"[err] no trades file at {csv_path}", file=sys.stderr)
        return []
    today = date.today()
    out = []
    cache: dict[tuple, dict] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            q = row.get("question", "")
            parsed = parse_market(q, today)
            td = parsed.get("target_date") if parsed else None
            if not parsed or not td:
                continue
            if row.get("status") in ("dry", "failed", "rejected"):
                # still record, but no P&L since no fill
                pass
            key = (parsed["lat"], parsed["lon"], td.isoformat())
            if key not in cache:
                if td > today:
                    cache[key] = {"outcome": "PENDING", "observed": None,
                                  "threshold_met": None, "reason": "future"}
                else:
                    obs = fetch_observed(parsed["lat"], parsed["lon"],
                                         parsed["tz"], td)
                    cache[key] = {"obs": obs}
            entry = cache[key]
            if "outcome" in entry:
                resolution = dict(entry)
            else:
                obs = entry.get("obs")
                if not obs:
                    resolution = {"outcome": "PENDING", "observed": None,
                                  "threshold_met": None, "reason": "no_obs"}
                else:
                    met = _threshold_met(parsed, obs)
                    resolution = {
                        "outcome": "YES" if met else "NO" if met is not None else "PENDING",
                        "observed": obs, "threshold_met": met,
                    }

            size_usd = float(row.get("size_usd") or 0)
            size_sh = float(row.get("size_shares") or 0)
            side = (row.get("side") or "").upper()
            status = row.get("status") or ""
            outcome = resolution["outcome"]
            pnl = 0.0
            settled = False
            is_live = status not in ("dry", "failed", "rejected", "")
            if outcome in ("YES", "NO") and is_live:
                settled = True
                won = (side == outcome)
                # Each share pays $1 if correct; cost is size_usd (fill ≈ ask*shares).
                pnl = (size_sh - size_usd) if won else (-size_usd)

            rec = dict(row)
            rec.update({
                "target_date": td.isoformat(),
                "outcome": outcome,
                "observed_max_f": resolution["observed"]["max_f"] if resolution.get("observed") else None,
                "observed_min_f": resolution["observed"]["min_f"] if resolution.get("observed") else None,
                "observed_precip_in": resolution["observed"]["precip_in"] if resolution.get("observed") else None,
                "metric": parsed.get("metric"),
                "settled": settled,
                "won": (settled and side == outcome),
                "realized_pnl_usd": round(pnl, 4),
            })
            out.append(rec)
    return out


def summary(reconciled: list[dict]) -> dict:
    settled = [r for r in reconciled if r["settled"]]
    wins = [r for r in settled if r["won"]]
    losses = [r for r in settled if not r["won"]]
    pending = [r for r in reconciled if not r["settled"]]
    deployed = sum(float(r.get("size_usd") or 0) for r in settled)
    pnl = sum(r["realized_pnl_usd"] for r in settled)

    by_city: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "deployed": 0.0})
    for r in settled:
        c = r.get("city") or "?"
        by_city[c]["n"] += 1
        by_city[c]["wins"] += int(r["won"])
        by_city[c]["pnl"] += r["realized_pnl_usd"]
        by_city[c]["deployed"] += float(r.get("size_usd") or 0)

    by_metric: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for r in settled:
        m = r.get("metric") or "?"
        by_metric[m]["n"] += 1
        by_metric[m]["wins"] += int(r["won"])
        by_metric[m]["pnl"] += r["realized_pnl_usd"]

    # Calibration: bucket our_p (probability we assigned to the side we took)
    calib = {f"{i/10:.1f}-{(i+1)/10:.1f}": {"n": 0, "wins": 0} for i in range(10)}
    for r in settled:
        try:
            p = float(r.get("our_p") or 0)
        except Exception:
            continue
        b = min(9, int(p * 10))
        key = f"{b/10:.1f}-{(b+1)/10:.1f}"
        calib[key]["n"] += 1
        calib[key]["wins"] += int(r["won"])
    for k, v in calib.items():
        v["observed_rate"] = (v["wins"] / v["n"]) if v["n"] else None

    return {
        "total_recorded": len(reconciled),
        "settled": len(settled),
        "pending": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(settled)) if settled else None,
        "total_deployed_usd": round(deployed, 2),
        "total_realized_pnl_usd": round(pnl, 2),
        "roi": (pnl / deployed) if deployed else None,
        "by_city": {k: {**v, "pnl": round(v["pnl"], 2),
                         "win_rate": v["wins"] / v["n"] if v["n"] else None}
                    for k, v in by_city.items()},
        "by_metric": {k: {**v, "pnl": round(v["pnl"], 2),
                          "win_rate": v["wins"] / v["n"] if v["n"] else None}
                      for k, v in by_metric.items()},
        "calibration": calib,
    }


def _write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0].keys())
    for r in rows:
        for k in r.keys():
            if k not in keys: keys.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows: w.writerow(r)


def _print_summary(s: dict) -> None:
    print("=" * 62)
    print(" Weather trader reconciliation")
    print("=" * 62)
    print(f" recorded={s['total_recorded']}  settled={s['settled']}  pending={s['pending']}")
    print(f" wins={s['wins']}  losses={s['losses']}  win_rate={s['win_rate']}")
    print(f" deployed=${s['total_deployed_usd']}  realized P&L=${s['total_realized_pnl_usd']}  ROI={s['roi']}")
    print("-" * 62)
    print(" By city:")
    for c, v in sorted(s["by_city"].items(), key=lambda kv: -kv[1]["pnl"]):
        print(f"   {c:12s} n={v['n']:3d}  wins={v['wins']:3d}  wr={v['win_rate']}  pnl=${v['pnl']}")
    print(" By metric:")
    for m, v in s["by_metric"].items():
        print(f"   {m:10s} n={v['n']:3d}  wins={v['wins']:3d}  wr={v['win_rate']}  pnl=${v['pnl']}")
    print(" Calibration (our_p bucket → observed win rate):")
    for k, v in s["calibration"].items():
        if v["n"]:
            print(f"   {k}  n={v['n']:3d}  observed={v['observed_rate']}")
    print("=" * 62)


def main() -> None:
    rows = reconcile_trades(TRADES_CSV)
    stats = summary(rows)
    _write_csv(rows, OUT_CSV)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(stats, indent=2, default=str))
    _print_summary(stats)
    print(f"wrote {OUT_CSV}  {OUT_JSON}")


if __name__ == "__main__":
    main()
