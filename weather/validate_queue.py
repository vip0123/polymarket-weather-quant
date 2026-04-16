"""Validate (and optionally fire) queued bets with fresh real-time data.

Reads dashboard/runtime/queued_bets.json, and for each queued bet:
  1. Fetches FRESH ensemble for (city, date) — no stale cache
  2. Applies STATION_OFFSET_C
  3. Computes fresh win probability via median + gaussian smoothing
  4. Fetches LIVE orderbook — ask price + depth
  5. Computes fresh cushion post-offset
  6. Decides PASS / SKIP with reason
  7. If --fire and PASS, places the order

Run:   uv run python -m weather.validate_queue         # validate only
       uv run python -m weather.validate_queue --fire  # validate + fire passers
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date as dt_date
from pathlib import Path

import requests
from dotenv import load_dotenv

from weather.cities import CITIES, STATION_OFFSET_C

ROOT = Path(__file__).resolve().parents[1]
QUEUE_FILE = ROOT / "dashboard" / "runtime" / "queued_bets.json"


def ensemble_peaks_f(lat: float, lon: float, tz: str, date_s: str,
                     metric: str = "max_temp") -> list[float]:
    r = requests.get(
        "https://ensemble-api.open-meteo.com/v1/ensemble", params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "models": "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_global",
            "start_date": date_s, "end_date": date_s,
            "temperature_unit": "fahrenheit", "timezone": tz,
        }, timeout=15,
    ).json()
    times = r["hourly"]["time"]
    agg = max if metric != "min_temp" else min
    vals = []
    for key, v in r["hourly"].items():
        if key == "time" or not isinstance(v, list): continue
        day = [x for t, x in zip(times, v) if x is not None and t[:10] == date_s]
        if day: vals.append(agg(day))
    return vals


def fetch_market(city: str, date_s: str, op: str, thr_f: float, metric: str) -> dict | None:
    """Search gamma for matching weather market. Returns dict with tokens[]."""
    tc = thr_f  # in °F but Polymarket questions use °C in their slugs for Asia/Europe
    queries = [f"{city} {int(thr_f)}", city]
    for q in queries:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"q": q, "closed": "false", "active": "true", "limit": 50},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            data = r.json() or []
        except Exception:
            continue
        for m in data:
            question = (m.get("question") or "").lower()
            if city not in question: continue
            if date_s[-5:] not in m.get("endDate", "") and date_s not in str(m.get("endDate", "")):
                # Cross-check end date
                pass
            # Threshold must match
            thr_c = round((thr_f - 32) * 5 / 9)
            thr_f_i = int(round(thr_f))
            if op == ">=" and f"{thr_c}°c or higher" not in question and f"{thr_f_i}°f or higher" not in question:
                continue
            if op == "<=" and f"{thr_c}°c or below" not in question and f"{thr_f_i}°f or below" not in question:
                continue
            # Metric check
            want_low = metric == "min_temp"
            has_low = "lowest" in question
            if want_low != has_low: continue
            return m
    return None


def live_book_ask(token_id: str) -> tuple[float | None, float]:
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=8).json()
        asks = sorted([(float(a["price"]), float(a["size"])) for a in r.get("asks", [])])
        if not asks: return None, 0.0
        depth = sum(p * s for p, s in asks if p <= 0.85)
        return asks[0][0], depth
    except Exception:
        return None, 0.0


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def smooth_p(members: list[float], op: str, thr_f: float,
             err_f: float = 1.5) -> float:
    if op == ">=":
        probs = [1 - phi((thr_f - v) / err_f) for v in members]
    elif op == "<=":
        probs = [phi((thr_f - v) / err_f) for v in members]
    else:
        return 0.0
    return sum(probs) / len(probs)


def validate_one(bet: dict) -> dict:
    """Return {pass, reason, context...}"""
    city = bet["city"]
    if city not in CITIES:
        return {"pass": False, "reason": f"unknown city {city}"}
    lat, lon, tz, _ = CITIES[city][:4]
    offset_f = STATION_OFFSET_C.get(city, 0) * 9 / 5
    thr_f = float(bet["threshold_f"])
    op = bet["op"]
    side = bet["side"]
    metric = bet.get("metric", "max_temp")
    date_s = bet["target_date"]

    # 1. Fresh ensemble
    try:
        peaks = ensemble_peaks_f(lat, lon, tz, date_s, metric)
    except Exception as e:
        return {"pass": False, "reason": f"ensemble fetch fail: {e}"}
    if not peaks:
        return {"pass": False, "reason": "no ensemble data"}
    median_f = sorted(peaks)[len(peaks) // 2]
    eff_f = median_f + offset_f  # oracle-scale

    # 2. Cushion check
    if side == "YES":
        cushion = (eff_f - thr_f) if op == ">=" else (thr_f - eff_f)
    else:
        cushion = (thr_f - eff_f) if op == ">=" else (eff_f - thr_f)
    if cushion < bet["min_cushion_f"]:
        return {"pass": False, "reason": f"cushion {cushion:+.1f}°F < {bet['min_cushion_f']}",
                "median_f": median_f, "eff_f": eff_f, "cushion": cushion}

    # 3. Smoothed probability
    # apply offset to effective threshold for scoring on our (grid) scale
    eff_thr = thr_f - offset_f  # threshold on grid scale
    p_yes = smooth_p(peaks, op, eff_thr)
    p_win = p_yes if side == "YES" else (1 - p_yes)

    # 4. Market lookup + live book
    mkt = fetch_market(city, date_s, op, thr_f, metric)
    if not mkt:
        return {"pass": False, "reason": "market not found in gamma",
                "cushion": cushion, "p_win": p_win}
    try:
        tokens = json.loads(mkt.get("clobTokenIds", "[]"))
        prices = json.loads(mkt.get("outcomePrices", "[]"))
    except Exception:
        return {"pass": False, "reason": "token parse fail"}
    if len(tokens) != 2:
        return {"pass": False, "reason": "bad token count"}
    tok = tokens[0] if side == "YES" else tokens[1]
    ask, depth = live_book_ask(tok)
    if ask is None:
        return {"pass": False, "reason": "no live ask", "cushion": cushion, "p_win": p_win}
    if ask > bet["max_ask"]:
        return {"pass": False, "reason": f"ask ${ask} > max ${bet['max_ask']}",
                "cushion": cushion, "p_win": p_win}
    if depth < 20:
        return {"pass": False, "reason": f"depth ${depth:.0f} < $20",
                "cushion": cushion, "p_win": p_win}

    # 5. Edge
    mkt_p_yes = float(prices[0]) if prices else ask
    mkt_p_win = mkt_p_yes if side == "YES" else (1 - mkt_p_yes)
    edge = p_win - mkt_p_win
    if edge < bet["min_edge"]:
        return {"pass": False, "reason": f"edge {edge:+.2f} < {bet['min_edge']}",
                "cushion": cushion, "p_win": p_win, "ask": ask, "mkt_p_win": mkt_p_win}

    return {
        "pass": True, "reason": "PASS",
        "median_f": median_f, "eff_f": eff_f, "cushion": cushion,
        "p_win": p_win, "mkt_p_win": mkt_p_win, "edge": edge,
        "ask": ask, "depth": depth, "token": tok, "mkt_question": mkt.get("question", ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fire", action="store_true", help="also place orders on PASS")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    queue = json.loads(QUEUE_FILE.read_text())
    bets = queue.get("queued", [])
    if not bets:
        print("queue empty"); return

    print(f"Validating {len(bets)} queued bet(s)...\n")
    passers = []
    for bet in bets:
        print(f"─── {bet['id']}")
        res = validate_one(bet)
        status = "✅ PASS" if res["pass"] else "❌ SKIP"
        print(f"  {status} — {res.get('reason', '')}")
        if "median_f" in res: print(f"  forecast median: {res['median_f']:.1f}°F")
        if "eff_f" in res:    print(f"  oracle effective: {res['eff_f']:.1f}°F (offset applied)")
        if "cushion" in res:  print(f"  cushion: {res['cushion']:+.1f}°F")
        if "p_win" in res:    print(f"  our P(win): {res['p_win']*100:.0f}%")
        if "mkt_p_win" in res: print(f"  mkt P(win): {res['mkt_p_win']*100:.0f}%")
        if "edge" in res:     print(f"  edge: {res['edge']:+.2f}")
        if "ask" in res:      print(f"  ask: ${res['ask']} depth: ${res.get('depth',0):.0f}")
        print()
        if res["pass"]:
            passers.append((bet, res))

    if not args.fire:
        print(f"Validation done. {len(passers)}/{len(bets)} passed. Run with --fire to deploy.")
        return

    if not passers:
        print("No passers to fire.")
        return

    # Fire
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs
    from py_clob_client.constants import POLYGON
    from py_clob_client.order_builder.constants import BUY
    creds = ApiCreds(api_key=os.environ["POLY_API_KEY"],
                     api_secret=os.environ["POLY_API_SECRET"],
                     api_passphrase=os.environ["POLY_API_PASSPHRASE"])
    c = ClobClient(host="https://clob.polymarket.com",
                   key=os.environ["POLY_PRIVATE_KEY"],
                   chain_id=POLYGON,
                   signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "0")),
                   funder=os.environ.get("POLY_FUNDER"), creds=creds)

    for bet, res in passers:
        size_usd = min(bet["target_size_usd"], res["depth"] * 0.8)
        size_shares = round(size_usd / res["ask"], 0)
        if size_shares < 5:
            print(f"  {bet['id']}: size <5 shares, skip"); continue
        try:
            order = c.create_order(OrderArgs(
                token_id=res["token"], price=res["ask"],
                size=size_shares, side=BUY,
            ))
            resp = c.post_order(order)
            print(f"  🔥 {bet['id']} FIRED @ ${res['ask']} × {size_shares} ≈ ${size_usd:.0f} → {resp.get('status')}")
        except Exception as e:
            print(f"  ❌ {bet['id']} order err: {e}")


if __name__ == "__main__":
    sys.exit(main())
