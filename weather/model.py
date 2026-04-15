"""Compute P(event) from ensemble forecast.

Takes Open-Meteo ensemble hourly data, groups by day, extracts per-model
daily max/min/precip, returns empirical probability across ensemble members.
"""
from __future__ import annotations

from datetime import date
from statistics import mean, median, stdev
from typing import Optional


def _daily_maxmin(hourly: dict, target: date) -> dict[str, list[float]]:
    """Return {model: [values]} of per-hour temps on target date."""
    times = hourly.get("time", [])
    out: dict[str, list[float]] = {}
    for key, vals in hourly.items():
        if key == "time" or not isinstance(vals, list): continue
        series = []
        for t, v in zip(times, vals):
            if v is None: continue
            if t[:10] == target.isoformat():
                series.append(v)
        if series:
            out[key] = series
    return out


def compute_p_event(ensemble: dict, query: dict) -> Optional[dict]:
    """From Open-Meteo ensemble JSON + parsed query, compute probability.

    Returns dict with keys:
      p_event    — fraction of ensemble members satisfying the condition
      member_n   — number of ensemble members considered
      forecast   — point estimate (mean across members)
      spread     — std across members
    """
    if not ensemble or "hourly" not in ensemble:
        return None
    target = query.get("target_date")
    if not target:
        return None

    hourly = ensemble["hourly"]
    metric = query.get("metric", "max_temp")
    op = query.get("op", ">=")
    thr = query.get("threshold")

    # Pick which hourly field and aggregator based on metric
    if metric == "max_temp":
        field_prefix, agg = "temperature_2m", max
    elif metric == "min_temp":
        field_prefix, agg = "temperature_2m", min
    elif metric == "precip_in":
        field_prefix, agg = "precipitation", sum
    else:
        return None

    # Each ensemble member appears as field_prefix_memberN or field_prefix_<model>
    # open-meteo ensemble returns one series per model, naming like
    # "temperature_2m_ecmwf_ifs025" OR just per-member for a single model.
    member_vals: list[float] = []
    model_aggregates: dict[str, float] = {}
    times = hourly.get("time", [])
    for key, vals in hourly.items():
        if not key.startswith(field_prefix) or key == field_prefix:
            # keep the base key too if no variant present
            if key != field_prefix: continue
        if not isinstance(vals, list): continue
        day_vals = [v for t, v in zip(times, vals)
                    if v is not None and t[:10] == target.isoformat()]
        if not day_vals: continue
        agg_val = agg(day_vals)
        model_aggregates[key] = agg_val
        member_vals.append(agg_val)

    if not member_vals:
        return None

    # P(event) computed two ways:
    #   1. HARD — empirical fraction of members crossing threshold (old behavior)
    #   2. SMOOTH — convolve each member with gaussian noise (own-model error).
    #      This accounts for the fact that each member forecast has ~1.5°F
    #      inherent uncertainty beyond ensemble spread. Reduces overconfidence
    #      near the threshold.
    #
    # We use SMOOTH as primary p_event for trading. Model error std = 1.5°F.
    import math
    OWN_MODEL_ERR_F = 1.5

    def _phi(x):  # standard-normal CDF via erf
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    if thr is not None:
        if op in (">", ">="):
            hits = sum(1 for v in member_vals if v >= thr)
            probs = [1 - _phi((thr - v) / OWN_MODEL_ERR_F) for v in member_vals]
        elif op in ("<", "<="):
            hits = sum(1 for v in member_vals if v <= thr)
            probs = [_phi((thr - v) / OWN_MODEL_ERR_F) for v in member_vals]
        else:
            hits = sum(1 for v in member_vals if abs(v - thr) < 0.5)
            probs = [_phi((thr + 0.5 - v) / OWN_MODEL_ERR_F)
                     - _phi((thr - 0.5 - v) / OWN_MODEL_ERR_F)
                     for v in member_vals]
        p_event_hard = hits / len(member_vals)
        p_event = sum(probs) / len(probs)  # smoothed
    else:
        p_event = p_event_hard = None

    # Ensemble quantiles for conservative sizing.
    sorted_vals = sorted(member_vals)
    n = len(sorted_vals)
    p10 = sorted_vals[max(0, n // 10)]
    p90 = sorted_vals[min(n - 1, n - 1 - n // 10)]

    # Use MEDIAN not mean — robust to outliers (Toronto 2026-04-15 lesson).
    return {
        "p_event": p_event,
        "p_event_hard": p_event_hard,
        "member_n": len(member_vals),
        "forecast": round(median(member_vals), 2),
        "spread": round(stdev(member_vals), 2) if len(member_vals) > 1 else 0.0,
        "p10": round(p10, 2),
        "p90": round(p90, 2),
        "members": {k: round(v, 2) for k, v in model_aggregates.items()},
    }
