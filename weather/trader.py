"""Weather trader — fires limit buys on edges in the edge_table.

Workflow each cycle:
  1. Refresh edge_table (calls dump.py as subprocess in background; use latest CSV)
  2. Load edge_table.csv
  3. For each row with |edge| > edge_bps, determine favored side:
       our_p > market_p + threshold → buy YES
       our_p < market_p - threshold → buy NO
  4. Fetch live book, check liquidity depth on favored side
  5. Post limit order at best ask (takes what's there), size $max_pos_usd
  6. Track open conditions so we don't double-fire
"""
from __future__ import annotations

import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds, OrderArgs, Side
from py_clob_client_v2.constants import POLYGON

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
CFG_FILE = RUNTIME / "weather_trader_config.json"
STATE_FILE = RUNTIME / "weather_trader_state.json"
TRADES_FILE = RUNTIME / "weather_trader_trades.csv"
LOG_FILE = RUNTIME / "weather_trader.log"
EDGE_CSV = ROOT / "weather" / "edge_table.csv"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("wthr")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN; SHUTDOWN = True


def load_json(p, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def save_json(p, d):
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(p)
    except FileNotFoundError:
        pass


def append_trade(row):
    header = ("timestamp,question,city,threshold,side,our_p,market_p,edge,ask,"
              "size_shares,size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")).replace(",", ";") for k in (
            "timestamp", "question", "city", "threshold", "side", "our_p",
            "market_p", "edge", "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


def fetch_book(token_id: str) -> dict | None:
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=8)
        return r.json()
    except Exception as e:
        log.warning("book %s: %s", token_id[:10], e)
        return None


def ask_depth_usd(book: dict, max_price: float) -> tuple[float, float]:
    """Return (best_ask, total_usd_depth_at_or_below_max_price)."""
    asks = book.get("asks", []) or []
    if not asks:
        return (None, 0.0)
    priced = sorted([(float(a["price"]), float(a["size"])) for a in asks])
    best = priced[0][0]
    total = sum(p * s for p, s in priced if p <= max_price)
    return (best, total)


def load_edges(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def decide_side(row: dict, edge_threshold: float,
                 only_directional: bool = False,
                 max_days_out: int = 1,
                 day2_plus_min_edge: float = 0.40,
                 forecast_drift_penalty_per_day: float = 0.08) -> Optional[tuple]:
    """Return (side, target_ask_max, token_idx) or None.

    Playbook Rule 4 (v4 2026-04-16): Forecast drift penalty.
      Models refresh 4x/day. Each refresh can shift prediction 2-4°F. A 3°F
      cushion at entry typically erodes 1-2°F by day+2 resolution. We apply
      a drift penalty on p_win to reflect this:
          effective_p = raw_p × (1 - 0.08 × days_out)
      So day+1 p_win 95% → effective 87%; day+2 p_win 95% → 80%.
      This prevents firing long-horizon bets that look strong but are
      statistically likely to drift against us.

    Rules:
      - TODAY-resolving: ALLOWED (no drift, fastest recycle)
      - DAY+1: allowed with normal edge threshold after drift penalty
      - DAY+2: only if edge ≥40pp AFTER drift penalty
      - DAY+3+: blocked entirely
    """
    from datetime import date
    try:
        our = float(row["our_p"])
        mkt = float(row["market_p"])
    except (ValueError, TypeError):
        return None
    if only_directional and row.get("op") not in (">=", "<="):
        return None
    # Bucket bets allowed when only_directional=false, but ONLY on NO side
    # (forecast outside bucket). YES-bucket requires manual override.
    # Rule 14 cushion enforcement happens downstream in the trader loop.

    # Apply forecast drift penalty based on days_out (Rule 4 v4)
    days_out = 0
    td_str = row.get("target_date")
    if td_str:
        try:
            td = date.fromisoformat(td_str)
            days_out = (td - date.today()).days
            if days_out < 0:
                return None
            if days_out > max_days_out:
                return None
            # 30-HOUR RULE (Miami post-mortem 2026-04-17):
            # 2-day forecast error (~4.4°F) eats typical cushion.
            # Only fire if market resolves within 30 hours of NOW.
            # Resolution = end-of-day in the CITY'S timezone (not system tz).
            from datetime import datetime as dt_cls, timedelta
            from zoneinfo import ZoneInfo
            from weather.cities import CITIES
            city = row.get("city", "").lower()
            city_tz_str = CITIES[city][2] if city in CITIES else "UTC"
            city_tz = ZoneInfo(city_tz_str)
            # Market resolves at midnight local = start of next day in city tz
            resolution_local = dt_cls(td.year, td.month, td.day, 23, 59, tzinfo=city_tz)
            now_utc = dt_cls.now(ZoneInfo("UTC"))
            hours_to_resolution = (resolution_local - now_utc).total_seconds() / 3600
            if hours_to_resolution > 30:
                return None  # too far out — forecast hasn't earned trust
            if hours_to_resolution < -2:
                return None  # already resolved
        except Exception:
            pass

    # Penalize expected prob by forecast drift. At days_out=2 and default 0.08,
    # a 95% entry prob becomes 80% effective — most bets won't clear the bar.
    drift_factor = max(0.5, 1.0 - forecast_drift_penalty_per_day * days_out)
    our_adj = 0.5 + (our - 0.5) * drift_factor  # pull toward 50/50 as horizon extends
    delta_p_adj = abs(our_adj - mkt)

    # Day+2 needs 40pp AFTER drift penalty
    if days_out > max_days_out and delta_p_adj < day2_plus_min_edge:
        return None
    if delta_p_adj < edge_threshold:
        return None

    tokens = json.loads(row["tokens"])
    if (our - mkt) > 0:
        return ("YES", our_adj, tokens[0])
    else:
        # Market-consensus guard: don't fade a market where Yes is >72% confident.
        # No token priced below ~28¢ means strong crowd agreement — model rarely has
        # edge here on weather bets. Both confirmed losses (Seoul 18°C, London 14°C)
        # had No tokens at 10-17¢ (Yes >83%). This is the #2 loss-prevention filter.
        if mkt > 0.72:
            return None
        return ("NO", 1.0 - our_adj, tokens[1])


def refresh_edge_table() -> bool:
    """Run weather.dump as subprocess to refresh edge_table.csv.
    Returns True if success; keeps existing CSV on failure."""
    import subprocess
    try:
        log.info("[REFRESH] running weather.dump ...")
        import sys as _sys
        python_exe = str(Path(_sys.executable))
        r = subprocess.run(
            [python_exe, "-m", "weather.dump"],
            cwd=str(ROOT), timeout=300, capture_output=True, text=True,
        )
        if r.returncode == 0:
            # Extract last interesting line from stdout
            tail = [ln for ln in r.stdout.splitlines() if "wrote" in ln or "parsed" in ln]
            log.info("[REFRESH] ok — %s", "; ".join(tail[-2:]) if tail else "done")
            return True
        log.warning("[REFRESH] exit=%d stderr=%s", r.returncode, r.stderr[:300])
    except subprocess.TimeoutExpired:
        log.warning("[REFRESH] timed out")
    except Exception as e:
        log.warning("[REFRESH] failed: %s", e)
    return False


def kelly_fraction(our_p: float, ask: float, cap: float = 0.25,
                    confidence: float = 1.0) -> float:
    """Kelly criterion: f* = (p*b - q) / b where b = (1-ask)/ask.
    Capped at `cap` (quarter-Kelly default) then scaled by `confidence` [0,1].
    Returns 0 if edge is negative."""
    if ask <= 0 or ask >= 1:
        return 0.0
    b = (1.0 - ask) / ask
    q = 1.0 - our_p
    f = (our_p * b - q) / b
    if f <= 0:
        return 0.0
    return min(f, cap) * max(0.0, min(1.0, confidence))


def confidence_from_row(row: dict, nws_conf: float = 1.0) -> float:
    """Confidence score [0, 1] combining ensemble spread, members, and NWS agreement."""
    try:
        spread = float(row.get("spread_f") or 0)
        members = int(row.get("members") or 0)
    except (ValueError, TypeError):
        return 0.5
    spread_score = max(0.0, 1.0 - spread / 5.0)
    member_score = min(1.0, members / 4.0)
    # NWS cross-check: 1.0 if both models agree within 2°F, 0.5 if 2-5°F,
    # 0.0 if > 5°F apart. Non-US cities return 1.0 (neutral).
    return max(0.2, spread_score * member_score * nws_conf)


def nws_confidence_for_row(row: dict, cache: dict) -> float:
    """Query NWS for US cities, cache per (city, date). Returns confidence [0,1]."""
    try:
        from weather.nws import cross_check, is_us_city
        from weather.cities import CITIES
    except Exception:
        return 1.0
    city = row.get("city", "").lower()
    if city not in CITIES:
        return 1.0
    lat, lon, tz, _ = CITIES[city]
    if not is_us_city(lat, lon):
        return 1.0
    target_date = row.get("target_date")
    if not target_date:
        return 1.0
    key = (city, target_date)
    if key in cache:
        return cache[key]
    try:
        from datetime import date as dt_date
        d = dt_date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
        om_max = float(row.get("forecast_f") or 0)
        om_min = om_max - 10  # rough; NWS only needs one for comparison
        cc = cross_check(lat, lon, d, tz, om_max, om_min)
        conf = cc.get("confidence", 1.0) if cc else 1.0
    except Exception:
        conf = 1.0
    cache[key] = conf
    return conf


def intraday_p_for_row(row: dict) -> Optional[float]:
    """For markets resolving today, compute P using hourly observations.
    Returns None if market doesn't resolve today or data unavailable."""
    from datetime import date as dt_date
    from weather.cities import CITIES
    target_date = row.get("target_date")
    if not target_date:
        return None
    try:
        td = dt_date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    except Exception:
        return None
    if td != dt_date.today():
        return None
    city = row.get("city", "").lower()
    if city not in CITIES:
        return None
    lat, lon, tz, _ = CITIES[city]
    try:
        from weather.intraday import p_event_intraday
        metric = row.get("metric", "max_temp")
        op = row.get("op", ">=")
        thr_str = str(row.get("threshold", ""))
        if "-" in thr_str:
            lo, hi = thr_str.split("-")
            return p_event_intraday(lat, lon, tz, metric, "in",
                                     threshold_low=float(lo), threshold_high=float(hi))
        return p_event_intraday(lat, lon, tz, metric, op,
                                 threshold=float(thr_str))
    except Exception as e:
        log.warning("[INTRADAY] %s %s: %s", city, target_date, e)
        return None


def fresh_forecast_sanity(row: dict) -> tuple[bool, str]:
    """Re-fetch single-best forecast for the candidate's city+date and verify
    the edge_table's forecast_f is still roughly right. Returns (ok, reason).

    Added after 2026-04-15 Toronto near-miss where edge_table had a stale/outlier
    forecast implying 21°C peak when fresh data said 15.6°C. Cushion evaporated.
    """
    try:
        from datetime import date as dt_date
        from weather.cities import CITIES
        import requests as _r
        city = (row.get("city") or "").lower()
        date_s = row.get("target_date", "")
        if not city or city not in CITIES or not date_s:
            return True, "no-city-skip"
        lat, lon, tz, _ic = CITIES[city][:4]
        metric = row.get("metric", "max_temp")
        daily = "temperature_2m_max" if metric != "min_temp" else "temperature_2m_min"
        resp = _r.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "daily": daily,
            "start_date": date_s, "end_date": date_s,
            "temperature_unit": "fahrenheit", "timezone": tz,
        }, timeout=6).json()
        fresh_peak = resp["daily"][daily][0]
        csv_forecast = float(row.get("forecast_f", 0))
        gap = abs(fresh_peak - csv_forecast)
        if gap > 3.0:
            return False, f"forecast drift {csv_forecast:.1f}→{fresh_peak:.1f}°F (gap {gap:.1f}°F)"
        return True, f"ok ({fresh_peak:.1f}°F)"
    except Exception as e:
        return True, f"check-skip: {e}"


def check_order_status(client: ClobClient, order_id: str) -> Optional[str]:
    """Poll a single order's status. Returns 'matched' | 'live' | 'cancelled' | None."""
    try:
        o = client.get_order(order_id)
        if isinstance(o, dict):
            return o.get("status")
    except Exception as e:
        log.warning("[POLL] %s: %s", order_id[:12], e)
    return None


def _apply_proxy(proxy_url: str) -> None:
    """Patch the module-level httpx client used by py_clob_client_v2 to route
    all CLOB requests through a proxy (needed in geo-blocked regions like NL/US).
    Also sets HTTPS_PROXY so that any bare requests.get() calls pick it up."""
    import httpx
    import py_clob_client_v2.http_helpers.helpers as _hh
    _hh._http_client = httpx.Client(http2=False, proxy=proxy_url)
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    host_port = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
    log.info("proxy active: ...%s", host_port)


def main():
    load_dotenv(ROOT / ".env")
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    proxy_url = os.environ.get("POLY_PROXY_URL", "").strip()
    if proxy_url:
        _apply_proxy(proxy_url)

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ.get("POLY_API_SECRET", "") or "",
        api_passphrase=os.environ.get("POLY_API_PASSPHRASE", "") or "",
    )
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=os.environ["POLY_PRIVATE_KEY"],
        chain_id=POLYGON,
        signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "0")),
        funder=os.environ.get("POLY_FUNDER"),
        creds=creds,
    )

    # Polymarket v2 "Relayer API Keys" (created via browser UI) have no
    # secret/passphrase — they use L1 EIP-712 auth for order posting.
    # Monkey-patch post_order to send L1 headers instead of HMAC headers.
    if not creds.api_secret:
        import json as _json, types as _types
        from py_clob_client_v2.order_utils.model.order_data_v2 import order_to_json_v2
        from py_clob_client_v2.order_utils.model.order_data_v1 import order_to_json_v1
        from py_clob_client_v2.endpoints import POST_ORDER as _POST_ORDER
        from py_clob_client_v2.clob_types import OrderType as _OT

        def _post_order_l1(self, order, order_type=_OT.GTC, post_only=False, defer_exec=False):
            _has_v2 = hasattr(order, 'salt')
            owner = self.creds.api_key or ""
            payload = order_to_json_v2(order, owner, order_type, post_only, defer_exec) if _has_v2 else order_to_json_v1(order, owner, order_type, post_only, defer_exec)
            serialized = _json.dumps(payload, separators=(',', ':'))
            headers = self._l1_headers()
            headers['Content-Type'] = 'application/json'
            return self._post(f"{self.host}{_POST_ORDER}", headers=headers, data=serialized)

        client.post_order = _types.MethodType(_post_order_l1, client)
        log.info("using L1 auth for post_order (Relayer API Key mode)")

    log.info("weather trader up. funder=%s", os.environ.get("POLY_FUNDER"))

    fired: dict[str, float] = {}  # (cid, side) -> entered_at
    # Restore fired positions from previous run (survives crashes/restarts).
    # Prevents the bot from re-buying a position it already entered this session.
    _prev = load_json(STATE_FILE, {})
    for _fk, _ft in _prev.get("fired_positions", {}).items():
        if isinstance(_ft, (int, float)) and time.time() - _ft < 6 * 3600:
            parts = _fk.split("|", 1)
            if len(parts) == 2:
                fired[tuple(parts)] = _ft
    if fired:
        log.info("[RESTORE] %d fired position(s) restored from previous run", len(fired))
    last_watchlist_refresh = 0.0
    live_orders: dict[str, dict] = {}  # order_id → {market, side, ask, size_usd, posted_at}
    last_refresh = 0.0

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        # Auto-refresh edge_table every refresh_every_s (default 30 min)
        refresh_every_s = float(cfg.get("refresh_every_s", 1800))
        now_t = time.time()
        if now_t - last_refresh > refresh_every_s:
            refresh_edge_table()
            last_refresh = now_t
            # Also refresh the pre-window watchlist (every dump cycle)
            try:
                from weather.watchlist import refresh as wl_refresh, check_window_entries, load_watchlist
                wl_refresh()
                entered = check_window_entries(load_watchlist())
                if entered:
                    log.info("[WATCHLIST] %d item(s) entered 30hr window: %s",
                             len(entered),
                             ", ".join(f"{e['city']} {e['side']} {e['op']}{e['threshold_f']:.0f}"
                                       for e in entered[:3]))
            except Exception as e:
                log.warning("[WATCHLIST] refresh err: %s", e)
            # Get watchlist context for any items entering window
            try:
                from weather.watchlist_comms import get_watchlist_context, mark_fired
                for e in entered:
                    ctx = get_watchlist_context(e.get("conditionId", ""), e.get("city", ""))
                    drift = ctx.get("drift", {})
                    rec = ctx.get("recommendation", "?")
                    reason = ctx.get("reason", "")
                    log.info("[WATCHLIST-INTEL] %s %s: %s — %s (stability=%.2f, "
                             "cushion %s→%s°F over %.0fhrs, %d snapshots)",
                             e.get("city"), e.get("side"), rec, reason,
                             ctx.get("stability_score", 0),
                             drift.get("cushion_first", "?"),
                             drift.get("cushion_last", "?"),
                             drift.get("hours_tracked", 0),
                             drift.get("n_snapshots", 0))
            except Exception:
                pass
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        edge_thr = float(cfg.get("edge_threshold", 0.15))
        min_liq_usd = float(cfg.get("min_liq_usd", 30.0))
        max_pos_usd = float(cfg.get("max_position_usd", 50.0))
        max_open = int(cfg.get("max_open_positions", 6))
        max_ask = float(cfg.get("max_ask", 0.85))
        min_ask = float(cfg.get("min_ask", 0.02))

        rows = load_edges(EDGE_CSV)
        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "edge_rows": len(rows), "open_positions": len(fired),
            "fired_positions": {f"{k[0]}|{k[1]}": v for k, v in fired.items()},
        })

        if not rows:
            log.info("[WAIT] no edge table")
            time.sleep(30); continue

        actionable = 0
        only_dir = bool(cfg.get("only_directional", True))
        exclude = set(c.lower() for c in cfg.get("exclude_cities", []))
        for row in rows:
            if row.get("city", "").lower() in exclude:
                continue
            decision = decide_side(row, edge_thr, only_directional=only_dir)
            if not decision:
                continue
            side, fair_max, token = decision
            key = (row["conditionId"], side)
            if key in fired:
                continue

            # Cumulative position check — prevent stacking across restarts.
            # Pull live positions and skip if we already hold this market.
            # Rule 17: lottery tickets (ask < $0.15) capped at $25 total.
            try:
                if not hasattr(main, "_held_tokens"):
                    import requests as _r
                    _pr = _r.get("https://data-api.polymarket.com/positions",
                                 params={"user": os.environ.get("POLY_FUNDER", ""),
                                         "sizeThreshold": 0.1},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
                    main._held_tokens = {}
                    for _p in _pr:
                        if float(_p.get("curPrice", 0)) < 0.02: continue
                        _asset = _p.get("asset", "")
                        _cost = float(_p.get("initialValue", 0))
                        main._held_tokens[_asset] = _cost
                    main._held_tokens_ts = time.time()
                # Refresh held tokens every 5 min
                if time.time() - getattr(main, "_held_tokens_ts", 0) > 300:
                    main._held_tokens = None  # force refresh next cycle
                    delattr(main, "_held_tokens")
                    continue
                existing_cost = main._held_tokens.get(token, 0)
                if existing_cost >= max_pos_usd:
                    log.info("[POS-CAP] %s %s already $%.0f deployed (cap $%.0f)",
                             row.get("city"), row.get("target_date"),
                             existing_cost, max_pos_usd)
                    continue
            except Exception:
                pass  # if check fails, proceed with normal flow

            book = fetch_book(token)
            if not book:
                continue
            best_ask, depth = ask_depth_usd(book, fair_max)
            if best_ask is None:
                continue
            if best_ask < min_ask or best_ask > max_ask:
                continue
            if depth < min_liq_usd:
                continue
            # Pre-fire sanity check: re-verify forecast hasn't drifted vs CSV.
            ok, why = fresh_forecast_sanity(row)
            if not ok:
                log.info("[STALE-SKIP] %s %s: %s",
                         row.get("city"), row.get("target_date"), why)
                continue
            # Cushion gate: compute post-offset distance from threshold.
            # Fewer bets, higher conviction — only fire with real cushion.
            min_cushion = float(cfg.get("min_cushion_f", 4.0))
            try:
                from weather.cities import get_offset_c
                offset_f = get_offset_c(row.get("city", "")) * 9 / 5
                fcst = float(row.get("forecast_f", 0))
                eff = fcst + offset_f
                thr_str = str(row.get("threshold", ""))
                if row.get("op") in (">=", "<="):
                    thr = float(thr_str)
                    if side == "YES":
                        cushion = (eff - thr) if row["op"] == ">=" else (thr - eff)
                    else:
                        cushion = (thr - eff) if row["op"] == ">=" else (eff - thr)
                elif "-" in thr_str:
                    lo, hi = [float(x) for x in thr_str.split("-")]
                    if lo <= eff <= hi:
                        cushion = -1  # in bucket = no cushion for NO
                    else:
                        cushion = min(abs(eff - lo), abs(eff - hi))
                else:
                    cushion = 99  # can't compute, allow
                if cushion < min_cushion:
                    log.info("[CUSHION-SKIP] %s %s cushion=%.1f°F < %.1f",
                             row.get("city"), row.get("target_date"),
                             cushion, min_cushion)
                    continue
            except Exception:
                pass  # if cushion calc fails, allow (don't block on error)
            actionable += 1

            # Intraday obs override: for today's markets, use hourly obs.
            our = float(row["our_p"])
            intraday_override = intraday_p_for_row(row)
            intraday_used = False
            if intraday_override is not None:
                log.info("[INTRADAY] %s raw=%.3f → intraday=%.3f", row["city"],
                         our, intraday_override)
                our = intraday_override
                intraday_used = True

            # Kelly sizing: size scales with edge strength × confidence.
            # Watchlist-sourced trades get stability_score as bonus confidence.
            p_win = our if side == "YES" else (1.0 - our)
            kelly_cap = float(cfg.get("kelly_cap", 0.20))
            nws_cache = getattr(main, "_nws_cache", {})
            main._nws_cache = nws_cache
            nws_conf = nws_confidence_for_row(row, nws_cache)
            conf = confidence_from_row(row, nws_conf=nws_conf)
            # Intraday observations are ground truth — override ensemble confidence.
            # When hourly obs fully confirm (p≥0.95 or p≤0.05), use full confidence.
            if intraday_used and (our >= 0.95 or our <= 0.05):
                conf = max(conf, 0.9)
                log.info("[INTRADAY-CONF] %s conf boosted to %.2f (intraday p=%.3f)",
                         row["city"], conf, our)
            # Check if this candidate has watchlist history → boost/penalize
            wl_source = False
            try:
                from weather.watchlist_comms import get_watchlist_context, mark_fired
                cid = row.get("conditionId", "")
                ctx = get_watchlist_context(cid, row.get("city", ""))
                if ctx.get("drift", {}).get("n_snapshots", 0) >= 2:
                    wl_source = True
                    stability = ctx.get("stability_score", 0.5)
                    rec = ctx.get("recommendation", "CAUTION")
                    if rec == "SKIP":
                        log.info("[WL-SKIP] %s %s: watchlist says SKIP — %s",
                                 row.get("city"), side, ctx.get("reason", ""))
                        continue
                    elif rec == "FIRE":
                        conf = min(1.0, conf * 1.3)  # 30% confidence boost
                        log.info("[WL-BOOST] %s %s: stability=%.2f → conf boosted",
                                 row.get("city"), side, stability)
                    elif rec == "CAUTION":
                        conf = conf * 0.7  # 30% confidence reduction
                        log.info("[WL-CAUTION] %s %s: %s → conf reduced",
                                 row.get("city"), side, ctx.get("reason", ""))
            except Exception:
                pass
            f = kelly_fraction(p_win, best_ask, cap=kelly_cap, confidence=conf)
            bankroll = float(cfg.get("allocation_usd", 300.0))
            kelly_usd = f * bankroll
            size_usd = min(max_pos_usd, depth * 0.8, kelly_usd)
            if size_usd < 5.0:  # below Polymarket min
                continue
            size_shares = size_usd / best_ask
            if size_shares < 5.0:
                continue

            log.info("[EDGE] %s %s ask=%.3f depth=$%.1f our=%s mkt=%s  %s",
                     row["city"], side, best_ask, depth, row["our_p"],
                     row["market_p"], row["question"][:70])

            if not enabled:
                continue
            if len(fired) >= max_open:
                log.info("[CAP] max_open=%d reached", max_open)
                break

            fired[key] = time.time()
            if dry:
                log.info("[DRY] would buy %s %s size=%.2f @%.3f ≈$%.2f",
                         row["city"], side, size_shares, best_ask, size_usd)
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "question": row["question"][:60], "city": row["city"],
                    "threshold": row["threshold"], "side": side,
                    "our_p": row["our_p"], "market_p": row["market_p"],
                    "edge": row["edge"], "ask": best_ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(size_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=best_ask, size=size_shares, side=Side.BUY,
                ))
                resp = client.post_order(order)
                oid = resp.get("orderID", "")
                wl_tag = " [WL-SOURCED]" if wl_source else ""
                log.info("[BUY]%s %s %s kelly=%.3f ≈$%.2f @ %.3f → %s",
                         wl_tag, row["city"], side, f, size_usd, best_ask, resp.get("status"))
                # Tell watchlist this market was fired — stop tracking
                if wl_source:
                    try:
                        mark_fired(row.get("conditionId", ""), side)
                    except Exception:
                        pass
                if oid:
                    live_orders[oid] = {
                        "market": row["question"][:60], "side": side,
                        "ask": best_ask, "size_usd": size_usd,
                        "posted_at": time.time(),
                    }
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "question": row["question"][:60], "city": row["city"],
                    "threshold": row["threshold"], "side": side,
                    "our_p": row["our_p"], "market_p": row["market_p"],
                    "edge": row["edge"], "ask": best_ask,
                    "size_shares": round(size_shares, 2),
                    "size_usd": round(size_usd, 2), "token_id": token,
                    "order_id": oid, "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("order failed: %s", e)

        # Poll any delayed/live orders every cycle to catch fills
        if live_orders and not dry:
            for oid in list(live_orders.keys()):
                meta = live_orders[oid]
                status = check_order_status(client, oid)
                if status == "matched":
                    log.info("[FILL] order %s: %s %s ≈$%.2f filled",
                             oid[:12], meta["market"][:40], meta["side"],
                             meta["size_usd"])
                    del live_orders[oid]
                elif status in ("cancelled", "canceled"):
                    log.info("[CANCEL] order %s: %s was %s",
                             oid[:12], meta["market"][:40], status)
                    del live_orders[oid]
                elif time.time() - meta["posted_at"] > 3600:
                    # Give up tracking after 1h — market probably stale
                    del live_orders[oid]

        log.info("[SCAN] rows=%d actionable=%d open=%d", len(rows), actionable, len(fired))

        # purge fired set after 6h (markets resolve daily at midnight)
        fired = {k: t for k, t in fired.items() if time.time() - t < 6 * 3600}
        time.sleep(60)

    log.info("weather trader stopped.")


if __name__ == "__main__":
    sys.exit(main())
