"""Watchlist ↔ Trader communication layer.

Provides:
  1. Watchlist → Trader:  snapshot history + drift analysis when item enters window
  2. Trader → Watchlist:  mark items as fired so watcher stops tracking
  3. Stability scoring:   cushion drift over time → confidence multiplier

The trader imports these functions to make smarter decisions on
watchlist-sourced candidates vs fresh-from-CSV unknowns.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

RUNTIME = Path(__file__).resolve().parents[1] / "dashboard" / "runtime"
SNAPSHOT_DIR = RUNTIME / "watchlist_snapshots"
WATCHLIST_FILE = RUNTIME / "watchlist.json"


def get_snapshot_history(condition_id: str, city: str = "") -> list[dict]:
    """Pull all hourly snapshots for a specific market."""
    for f in SNAPSHOT_DIR.glob("*.json"):
        if condition_id[:16] in f.name:
            try:
                return json.loads(f.read_text())
            except Exception:
                return []
    # Try city-based match
    if city:
        for f in SNAPSHOT_DIR.glob(f"*{city.lower()}*.json"):
            try:
                return json.loads(f.read_text())
            except Exception:
                pass
    return []


def analyze_drift(snapshots: list[dict]) -> dict:
    """Analyze forecast drift from snapshot history.

    Returns:
      cushion_trend:  "stable" | "eroding" | "improving" | "unknown"
      cushion_first:  first recorded cushion
      cushion_last:   most recent cushion
      cushion_delta:  change over observation period
      forecast_drift_f: total forecast movement (°F)
      n_snapshots:    how many data points
      stability_score: 0.0-1.0 (higher = more stable/improving)
      hours_tracked:  how long we've been watching
    """
    if not snapshots:
        return {"cushion_trend": "unknown", "stability_score": 0.5,
                "n_snapshots": 0}

    cushions = [s.get("cushion_f") for s in snapshots
                if s.get("cushion_f") is not None]
    forecasts = [s.get("forecast_f") for s in snapshots
                 if s.get("forecast_f") is not None]

    if len(cushions) < 2:
        return {"cushion_trend": "unknown", "stability_score": 0.5,
                "n_snapshots": len(snapshots),
                "cushion_last": cushions[-1] if cushions else None}

    first = cushions[0]
    last = cushions[-1]
    delta = last - first
    fcst_drift = (forecasts[-1] - forecasts[0]) if len(forecasts) >= 2 else 0

    # Trend
    if delta > 1.0:
        trend = "improving"
    elif delta < -1.0:
        trend = "eroding"
    else:
        trend = "stable"

    # Stability score (0-1):
    #   1.0 = cushion stable or improving over many snapshots
    #   0.5 = unknown or few snapshots
    #   0.2 = cushion eroding significantly
    #   0.0 = cushion went negative (forecast crossed threshold)
    if last < 0:
        score = 0.0  # forecast on wrong side now
    elif trend == "eroding" and abs(delta) > 2.0:
        score = 0.2  # big erosion
    elif trend == "eroding":
        score = 0.4  # mild erosion
    elif trend == "stable":
        score = 0.8  # held steady
    elif trend == "improving":
        score = 1.0  # getting better
    else:
        score = 0.5

    # Bonus for more snapshots (more data = more trust)
    data_bonus = min(0.2, len(cushions) * 0.02)  # up to +0.2 for 10+ snapshots
    score = min(1.0, score + data_bonus)

    # Compute hours tracked
    hours = 0
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        t0 = datetime.fromisoformat(snapshots[0]["timestamp"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(snapshots[-1]["timestamp"].replace("Z", "+00:00"))
        hours = (t1 - t0).total_seconds() / 3600
    except Exception:
        pass

    return {
        "cushion_trend": trend,
        "cushion_first": round(first, 1),
        "cushion_last": round(last, 1),
        "cushion_delta": round(delta, 1),
        "forecast_drift_f": round(fcst_drift, 1),
        "n_snapshots": len(snapshots),
        "stability_score": round(score, 2),
        "hours_tracked": round(hours, 1),
    }


def mark_fired(condition_id: str, side: str):
    """Tell the watchlist this market was fired — stop tracking it."""
    if not WATCHLIST_FILE.exists():
        return
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        items = data.get("watching", [])
        data["watching"] = [
            w for w in items
            if not (w.get("conditionId") == condition_id and w.get("side") == side)
        ]
        data["last_fired"] = {"conditionId": condition_id, "side": side}
        WATCHLIST_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        pass


def get_watchlist_context(condition_id: str, city: str = "") -> dict:
    """Full context package for the trader when evaluating a watchlist candidate.

    Returns everything the trader needs to make a high-conviction decision:
      - snapshot history
      - drift analysis
      - stability score (use as Kelly confidence multiplier)
      - recommendation: "FIRE" | "CAUTION" | "SKIP"
    """
    snaps = get_snapshot_history(condition_id, city)
    drift = analyze_drift(snaps)

    # ── Rich analytics layer ──
    # Don't just pass/fail — give the trader AND the user a full picture
    score = drift.get("stability_score", 0.5)
    trend = drift.get("cushion_trend", "unknown")
    cushion_last = drift.get("cushion_last")
    cushion_first = drift.get("cushion_first")
    n_snaps = drift.get("n_snapshots", 0)
    hours = drift.get("hours_tracked", 0)
    fcst_drift = drift.get("forecast_drift_f", 0)

    # Market price momentum (are other traders catching on?)
    mkt_prices = [s.get("market_yes_ask") for s in snaps
                  if s.get("market_yes_ask") is not None]
    mkt_momentum = "unknown"
    mkt_delta = 0
    if len(mkt_prices) >= 2:
        mkt_delta = mkt_prices[-1] - mkt_prices[0]
        if mkt_delta > 0.05:
            mkt_momentum = "rising"  # market moving toward YES
        elif mkt_delta < -0.05:
            mkt_momentum = "falling"  # market moving toward NO
        else:
            mkt_momentum = "flat"

    # METAR trend (actual obs vs forecast — is reality matching?)
    metar_temps = [s.get("metar_temp_f") for s in snaps
                   if s.get("metar_temp_f") is not None]
    metar_vs_forecast = "no_data"
    if metar_temps and snaps:
        last_fcst = next((s.get("forecast_f") for s in reversed(snaps)
                         if s.get("forecast_f")), None)
        if last_fcst and metar_temps[-1]:
            gap = metar_temps[-1] - last_fcst
            if abs(gap) < 2:
                metar_vs_forecast = "tracking"
            elif gap > 2:
                metar_vs_forecast = "warmer_than_forecast"
            else:
                metar_vs_forecast = "cooler_than_forecast"

    # Recommendation with full reasoning
    reasons = []
    if cushion_last is not None and cushion_last < 2.0:
        rec = "SKIP"
        reasons.append(f"cushion eroded to {cushion_last:+.1f}°F — below 2°F floor")
    elif cushion_last is not None and cushion_last < 3.0 and trend == "eroding":
        rec = "SKIP"
        reasons.append(f"cushion {cushion_first}→{cushion_last}°F and still falling")
    elif trend == "eroding" and abs(drift.get("cushion_delta", 0)) > 2.0:
        rec = "CAUTION"
        reasons.append(f"big erosion: {drift.get('cushion_delta',0):+.1f}°F over {hours:.0f}hrs")
    elif trend == "improving" and n_snaps >= 3:
        rec = "FIRE"
        reasons.append(f"cushion GROWING: {cushion_first}→{cushion_last}°F over {hours:.0f}hrs")
    elif trend == "stable" and n_snaps >= 3 and cushion_last and cushion_last >= 4.0:
        rec = "FIRE"
        reasons.append(f"rock solid: {cushion_last}°F cushion held across {n_snaps} snapshots")
    elif n_snaps >= 2:
        rec = "CAUTION"
        reasons.append(f"cushion {trend} ({drift.get('cushion_delta',0):+.1f}°F), needs monitoring")
    else:
        rec = "WATCH"
        reasons.append(f"only {n_snaps} snapshot(s) — need more data before firing")

    # Add market momentum context
    if mkt_momentum == "rising" and rec in ("FIRE", "CAUTION"):
        reasons.append(f"market YES moving up ({mkt_delta:+.2f}) — retail catching on, edge shrinking")
    elif mkt_momentum == "falling":
        reasons.append(f"market YES dropping ({mkt_delta:+.2f}) — edge may be widening")

    # Add METAR context
    if metar_vs_forecast == "warmer_than_forecast":
        reasons.append("⚠️ METAR running WARMER than forecast — offset risk")
    elif metar_vs_forecast == "cooler_than_forecast":
        reasons.append("METAR running cooler than forecast — favorable for our thesis")

    return {
        "snapshots": snaps,
        "drift": drift,
        "recommendation": rec,
        "reasons": reasons,
        "reason": " | ".join(reasons),
        "stability_score": score,
        "market_momentum": mkt_momentum,
        "market_price_delta": round(mkt_delta, 3),
        "metar_vs_forecast": metar_vs_forecast,
        "analytics": {
            "cushion_trajectory": f"{cushion_first}→{cushion_last}°F" if cushion_first else "?",
            "forecast_drift": f"{fcst_drift:+.1f}°F",
            "hours_tracked": hours,
            "snapshots": n_snaps,
            "market_trend": mkt_momentum,
            "metar_alignment": metar_vs_forecast,
        },
    }
