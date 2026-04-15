"""TA-based entry on 5-min Up/Down markets.

Entry window: t_rem 120-260s (early, while asks are still 0.55-0.80).
Signal: vote of 4 TA indicators on 1m Binance klines:
  1. RSI(14) — <30 bullish, >70 bearish
  2. EMA(3) vs EMA(8) cross
  3. Last 3 candle direction (momentum)
  4. Last candle volume > 1.5x 20-avg (confirmation)
Score ranges -4..+4. Enter when |score| >= 3.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
CFG_FILE = RUNTIME / "ta_config.json"
STATE_FILE = RUNTIME / "ta_state.json"
TRADES_FILE = RUNTIME / "ta_trades.csv"
LOG_FILE = RUNTIME / "ta.log"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ta")

SHUTDOWN = False
def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True


SLUG_RE = re.compile(r"(btc|eth|sol|xrp|bnb|doge|hype)-updown-(\d+)m-(\d+)")
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT"}


def discover_markets() -> list[dict]:
    now = int(time.time())
    sym_to_prefix = {v: k for k, v in SYM.items()}
    slugs = []
    anchor = (now // 300) * 300  # only 5m markets
    for sym in SYM.values():
        pfx = sym_to_prefix.get(sym)
        if not pfx: continue
        for i in range(0, 2):
            slugs.append(f"{pfx}-updown-5m-{anchor - i * 300}")
    ms = []
    for slug in slugs:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            d = r.json() or []
            if d: ms.append(d[0])
        except Exception:
            pass
    out = []
    for m in ms:
        slug = m.get("slug", "") or ""
        match = SLUG_RE.match(slug)
        if not match: continue
        prefix, dur_min, start_ts = match.group(1), int(match.group(2)), int(match.group(3))
        if dur_min != 5: continue
        sym = SYM.get(prefix)
        if not sym: continue
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
        except Exception:
            continue
        if len(tokens) != 2 or len(outcomes) != 2: continue
        up_idx = 0 if outcomes[0] == "Up" else 1
        out.append({
            "conditionId": m.get("conditionId"), "slug": slug, "symbol": sym,
            "start_ts": start_ts, "end_ts": start_ts + 300,
            "up_token": tokens[up_idx], "dn_token": tokens[1 - up_idx],
        })
    return out


def fetch_klines_1m(symbol: str, limit: int = 30) -> list[list]:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": limit},
            timeout=6,
        )
        return r.json() or []
    except Exception as e:
        log.warning("klines %s: %s", symbol, e)
        return []


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0: return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ta_score(klines: list[list]) -> tuple[int, dict]:
    """Return (score -4..+4, details). Positive = UP bias."""
    if len(klines) < 20:
        return 0, {"reason": "not_enough_klines"}
    closes = [float(k[4]) for k in klines]
    vols = [float(k[5]) for k in klines]
    score = 0
    d = {}
    # 1. RSI
    r = rsi(closes, 14)
    d["rsi"] = round(r, 1) if r else None
    if r is not None:
        if r < 35: score += 1   # oversold → bounce → UP
        elif r > 65: score -= 1  # overbought → DN
    # 2. EMA cross
    e3 = ema(closes[-10:], 3); e8 = ema(closes[-10:], 8)
    d["ema3"] = round(e3, 4) if e3 else None
    d["ema8"] = round(e8, 4) if e8 else None
    if e3 and e8:
        if e3 > e8: score += 1
        elif e3 < e8: score -= 1
    # 3. Momentum: last 3 closes
    if closes[-1] > closes[-2] > closes[-3]: score += 1
    elif closes[-1] < closes[-2] < closes[-3]: score -= 1
    # 4. Volume confirmation
    avg_v = sum(vols[-20:-1]) / 19
    d["vol_ratio"] = round(vols[-1] / avg_v, 2) if avg_v else None
    if avg_v and vols[-1] > avg_v * 1.5:
        if closes[-1] > closes[-2]: score += 1
        elif closes[-1] < closes[-2]: score -= 1
    return score, d


def best_ask(client: ClobClient, token_id: str) -> Optional[float]:
    try:
        book = client.get_order_book(token_id)
        asks = getattr(book, "asks", []) or []
        if not asks: return None
        return min(float(a.price) for a in asks)
    except Exception as e:
        log.warning("book %s: %s", token_id[:10], e)
        return None


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
    header = ("timestamp,market,symbol,direction,score,rsi,vol_ratio,ask,"
              "size_shares,size_usd,token_id,order_id,status\n")
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in (
            "timestamp", "market", "symbol", "direction", "score", "rsi",
            "vol_ratio", "ask", "size_shares", "size_usd", "token_id",
            "order_id", "status")) + "\n")


def main():
    load_dotenv(ROOT / ".env")
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=os.environ["POLY_PRIVATE_KEY"],
        chain_id=POLYGON,
        signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "0")),
        funder=os.environ.get("POLY_FUNDER"),
        creds=creds,
    )
    log.info("ta engine up. funder=%s", os.environ.get("POLY_FUNDER"))

    seen: dict = {}   # cid -> entered_at
    kline_cache: dict = {}  # symbol -> (ts, klines)
    markets: list = []
    last_discover = 0.0

    while not SHUTDOWN:
        cfg = load_json(CFG_FILE, {})
        enabled = cfg.get("enabled", False)
        dry = cfg.get("dry_run", True)
        t_min = float(cfg.get("t_rem_min_s", 120))
        t_max = float(cfg.get("t_rem_max_s", 260))
        min_score = int(cfg.get("min_score", 3))
        min_ask = float(cfg.get("min_ask", 0.40))
        max_ask = float(cfg.get("max_ask", 0.80))
        max_pos = float(cfg.get("max_position_usd", 30))
        max_open = int(cfg.get("max_open_positions", 5))

        now = time.time()
        if now - last_discover > 20:
            markets = discover_markets()
            last_discover = now

        save_json(STATE_FILE, {
            "running": True, "enabled": enabled, "dry_run": dry,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(seen),
            "markets_tracked": len(markets),
        })

        if not markets:
            time.sleep(3); continue

        # refresh klines for each symbol every 20s
        for sym in set(m["symbol"] for m in markets):
            cts, _ = kline_cache.get(sym, (0, []))
            if now - cts > 20:
                kl = fetch_klines_1m(sym)
                if kl: kline_cache[sym] = (now, kl)

        in_win = skipped_score = skipped_ask = 0
        for m in markets:
            cid = m["conditionId"]
            t_rem = m["end_ts"] - now
            if t_rem < t_min or t_rem > t_max:
                continue
            in_win += 1
            if cid in seen:
                continue

            klines = kline_cache.get(m["symbol"], (0, []))[1]
            if not klines:
                continue
            score, det = ta_score(klines)

            if abs(score) < min_score:
                skipped_score += 1
                continue

            direction = "UP" if score > 0 else "DN"
            token = m["up_token"] if direction == "UP" else m["dn_token"]
            ask = best_ask(client, token)
            if ask is None or ask < min_ask or ask > max_ask:
                skipped_ask += 1
                log.info("[TA-SKIP-ASK] %s %s score=%+d ask=%s t_rem=%.0fs",
                         m["slug"], direction, score, ask, t_rem)
                continue

            size_shares = max(max_pos / ask, 5.0)
            our_usd = size_shares * ask
            if our_usd > max_pos:
                size_shares = max_pos / ask
                our_usd = size_shares * ask

            log.info("[TA] %s %s score=%+d rsi=%s vol=%s ask=%.3f t_rem=%.0fs size=%.2f ($%.2f)",
                     m["slug"], direction, score, det.get("rsi"),
                     det.get("vol_ratio"), ask, t_rem, size_shares, our_usd)

            if not enabled:
                continue
            if len(seen) >= max_open:
                continue
            seen[cid] = now

            if dry:
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "score": score, "rsi": det.get("rsi"), "vol_ratio": det.get("vol_ratio"),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": "DRY", "status": "dry",
                })
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=token, price=ask, size=size_shares, side=BUY,
                ))
                resp = client.post_order(order)
                log.info("[TA-BUY] %s %s ≈$%.2f → %s",
                         m["slug"], direction, our_usd, resp.get("status"))
                append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": m["slug"], "symbol": m["symbol"], "direction": direction,
                    "score": score, "rsi": det.get("rsi"), "vol_ratio": det.get("vol_ratio"),
                    "ask": ask, "size_shares": round(size_shares, 2),
                    "size_usd": round(our_usd, 2), "token_id": token,
                    "order_id": resp.get("orderID", ""),
                    "status": resp.get("status", ""),
                })
            except Exception as e:
                log.exception("ta order failed: %s", e)

        log.info("[TA-SCAN] markets=%d in_window=%d weak_score=%d bad_ask=%d",
                 len(markets), in_win, skipped_score, skipped_ask)

        seen = {k: t for k, t in seen.items() if now - t < 360}
        time.sleep(3)

    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("ta engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
