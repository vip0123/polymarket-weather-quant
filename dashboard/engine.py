"""Copy-engine — polls target wallet activity, mirrors fills to our wallet
with size scaling, risk guards, and kill-switch. Writes live state to
dashboard/runtime/ so the Streamlit dashboard reflects it.

Run: uv run --extra dashboard python dashboard/engine.py
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, OrderArgs, AssetType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
RUNTIME.mkdir(exist_ok=True)
STATE_FILE = RUNTIME / "bot_state.json"
CONFIG_FILE = RUNTIME / "copy_config.json"
TRADES_FILE = RUNTIME / "bot_trades.csv"
SEEN_FILE = RUNTIME / "seen_tx.json"

DATA_API = "https://data-api.polymarket.com"
POLL_SECS = 3
SHUTDOWN = False


def sigterm(*_):
    global SHUTDOWN
    SHUTDOWN = True
_LOG_FILE = RUNTIME / "engine.log"
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(_LOG_FILE), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("engine")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def append_trade(row: dict):
    header = "timestamp,market_id,side,direction,price,size_usd,tx_hash,source_wallet,signal\n"
    if not TRADES_FILE.exists():
        TRADES_FILE.write_text(header)
    with TRADES_FILE.open("a") as f:
        f.write(",".join(str(row.get(k, "")) for k in
                ["timestamp", "market_id", "side", "direction", "price",
                 "size_usd", "tx_hash", "source_wallet", "signal"]) + "\n")


def fetch_target_activity(target: str, limit: int = 20) -> list[dict]:
    try:
        r = requests.get(f"{DATA_API}/activity",
                         params={"user": target, "limit": limit, "type": "TRADE"},
                         timeout=10)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        log.warning("activity fetch failed: %s", e)
        return []


def wallet_usdc_balance(client: ClobClient) -> float:
    try:
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(bal.get("balance", 0)) / 1_000_000
    except Exception as e:
        log.warning("balance fetch failed: %s", e)
        return 0.0


class RiskGuard:
    def __init__(self, cfg: dict, trades_today: list[dict]):
        self.max_pos = float(cfg.get("max_position_usd", 25))
        self.max_open = int(cfg.get("max_open_positions", 3))
        self.max_daily_loss = float(cfg.get("max_daily_loss_usd", 50))
        self.max_slippage_bps = float(cfg.get("max_slippage_bps", 300))
        self.trades_today = trades_today

    def check(self, size_usd: float, target_price: float, current_price: float,
              open_positions: int) -> tuple[bool, str]:
        if size_usd > self.max_pos:
            return False, f"size {size_usd:.2f} > max_pos {self.max_pos}"
        if open_positions >= self.max_open:
            return False, f"open_positions {open_positions} >= max {self.max_open}"
        slippage_bps = abs(current_price - target_price) / max(target_price, 1e-9) * 10_000
        if slippage_bps > self.max_slippage_bps:
            return False, f"slippage {slippage_bps:.0f}bps > max {self.max_slippage_bps:.0f}"
        return True, "ok"


def mirror_trade(client: ClobClient, activity: dict, cfg: dict, seen: set,
                 guard: "RiskGuard", open_positions: int) -> None:
    tx = activity.get("transactionHash") or activity.get("id")
    token_id = activity.get("asset")
    price = float(activity.get("price", 0))
    their_size = float(activity.get("size", 0))
    scale = float(cfg.get("scale", 0.05))
    min_size = float(cfg.get("min_size", 5.0))
    min_notional = float(cfg.get("min_notional_usd", 1.05))
    raw_size = their_size * scale
    our_size = max(raw_size, min_size, min_notional / max(price, 0.01))
    our_usd = our_size * price
    # Cap at max_position_usd instead of blocking — target may be accumulating
    # a bigger position than our allocation allows, still want in.
    if our_usd > guard.max_pos:
        our_size = guard.max_pos / max(price, 0.01)
        our_usd = our_size * price
        log.info("[CAPPED] tx=%s scaled_down $%.2f→$%.2f",
                 tx[:10], their_size * scale * price, our_usd)
    side = BUY if activity.get("side", "").upper() == "BUY" else SELL
    dry = cfg.get("dry_run", True)

    ok, reason = guard.check(size_usd=our_usd, target_price=price,
                             current_price=price, open_positions=open_positions)
    if not ok:
        log.info("[BLOCKED] %s tx=%s size=$%.2f", reason, tx[:10], our_usd)
        return

    target_ts = int(activity.get("timestamp", 0))
    lag_s = time.time() - target_ts if target_ts else -1

    # Pre-trade book check — skip if current ask differs from target price by
    # more than max_slippage_bps. Only enforced for LIVE orders, not DRY.
    max_slip = float(cfg.get("max_slippage_bps", 300))
    if not dry and max_slip > 0:
        try:
            book = client.get_order_book(token_id)
            asks = getattr(book, "asks", []) or []
            bids = getattr(book, "bids", []) or []
            if side == BUY and asks:
                current = min(float(a.price) for a in asks)
            elif side == SELL and bids:
                current = max(float(b.price) for b in bids)
            else:
                current = price
            slip_bps = abs(current - price) / max(price, 1e-9) * 10_000
            if slip_bps > max_slip:
                log.info("[BLOCKED] slip %.0fbps > max %.0fbps (target=%.3f book=%.3f) tx=%s",
                         slip_bps, max_slip, price, current, tx[:10])
                return
        except Exception as e:
            log.warning("pre-trade book check failed: %s", e)

    if dry:
        log.info("[DRY] mirror tx=%s mkt=%s %s @%.3f size=%.2f (≈$%.2f) lag=%.1fs",
                 tx[:10], str(activity.get("conditionId", ""))[:12], side,
                 price, our_size, our_usd, lag_s)
        append_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": activity.get("conditionId", ""),
            "side": activity.get("outcome", ""), "direction": side,
            "price": price, "size_usd": our_usd,
            "tx_hash": "DRY-" + str(tx)[:16],
            "source_wallet": cfg.get("followed_wallet", ""),
            "signal": "copy-dry",
        })
        return

    # LIVE path
    order = client.create_order(OrderArgs(
        token_id=token_id, price=price, size=our_size, side=side,
    ))
    resp = client.post_order(order)
    # Capture fill price diff: their price vs our execution price, so we can
    # quantify adverse-selection loss per fill.
    our_taking = float(resp.get("takingAmount") or 0)
    our_making = float(resp.get("makingAmount") or 0)
    our_price = (our_making / our_taking) if our_taking else price
    slip_bps = ((our_price - price) / max(price, 1e-9)) * 10_000
    log.info("posted order tx=%s lag=%.1fs slip=%+.0fbps (target=%.3f ours=%.3f) resp=%s",
             tx[:10], lag_s, slip_bps, price, our_price, resp)

    append_trade({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_id": activity.get("conditionId", ""),
        "side": activity.get("outcome", ""),
        "direction": activity.get("side", ""),
        "price": price,
        "size_usd": our_size * price,
        "tx_hash": resp.get("orderID", str(tx)[:16]),
        "source_wallet": cfg.get("followed_wallet", ""),
        "signal": "copy",
    })


def main():
    load_dotenv(ROOT / ".env")
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)
    from ws_watcher import start_ws_watcher  # lazy to avoid import if unused

    priv = os.environ["POLY_PRIVATE_KEY"]
    funder = os.environ.get("POLY_FUNDER")
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )
    client = ClobClient(
        host="https://clob.polymarket.com", key=priv, chain_id=POLYGON,
        signature_type=sig_type, funder=funder, creds=creds,
    )
    wallet = client.get_address()
    log.info("engine up. signer=%s funder=%s", wallet, funder)

    seen = set(load_json(SEEN_FILE, []))

    # Spawn Polygon WebSocket watcher if configured. It publishes fills as they
    # land on-chain (~2-4s) — much faster than the data-api poll.
    initial_cfg = load_json(CONFIG_FILE, {})
    ws_target = initial_cfg.get("followed_wallet")
    ws_queue = start_ws_watcher(os.environ.get("POLYGON_WSS_URL", ""), ws_target or "")

    while not SHUTDOWN:
        cfg = load_json(CONFIG_FILE, {})
        target = cfg.get("followed_wallet")
        enabled = cfg.get("enabled", False)

        # heartbeat
        usdc = wallet_usdc_balance(client)
        save_json(STATE_FILE, {
            "running": True, "enabled": enabled,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "followed_wallet": target, "wallet_address": wallet,
            "usdc_balance": usdc, "matic_balance": None,
            "open_positions": 0, "realized_pnl_usd": 0.0,
        })

        if not target:
            time.sleep(POLL_SECS); continue

        activity = fetch_target_activity(target)

        # Drain any WS events that beat the REST poller to the punch
        while not ws_queue.empty():
            try:
                activity.insert(0, ws_queue.get_nowait())
            except Exception:
                break

        # Aggregate fills into "decisions" keyed by (conditionId, side, asset).
        # One target decision often shows as many fills in activity.
        groups: dict[tuple, dict] = {}
        for item in activity:
            tx = item.get("transactionHash") or item.get("id")
            if not tx or tx in seen:
                continue
            key = (item.get("conditionId"), item.get("side"), item.get("asset"))
            g = groups.setdefault(key, {"txs": [], "size": 0.0,
                                        "usdc": 0.0, "sample": item,
                                        "ts": int(item.get("timestamp", 0))})
            g["txs"].append(tx)
            g["size"] += float(item.get("size", 0))
            g["usdc"] += float(item.get("usdcSize", 0))
            g["ts"] = max(g["ts"], int(item.get("timestamp", 0)))

        # Per-market cooldown: don't re-mirror the same (market, side, asset)
        # within a window — prevents double-posts when target's fills trickle in
        # across polls.
        cooldown_s = float(cfg.get("mirror_cooldown_s", 45))
        decision_seen = getattr(main, "_decision_seen", {})
        main._decision_seen = decision_seen
        now_ts = time.time()
        # purge expired
        for k in list(decision_seen):
            if now_ts - decision_seen[k] > cooldown_s:
                del decision_seen[k]

        title_blocklist = [s.lower() for s in cfg.get("title_blocklist", [])]
        for key, g in groups.items():
            sample = g["sample"]
            # mark all fills in this group as processed
            for t in g["txs"]:
                seen.add(t)
            # Skip markets matching any blocklisted title fragment
            title = (sample.get("title") or "").lower()
            blocked = next((kw for kw in title_blocklist if kw in title), None)
            if blocked:
                log.info("[BLOCKLIST] skip '%s' (matched '%s')", title[:60], blocked)
                continue
            if key in decision_seen:
                log.info("[COOLDOWN] skip mkt=%s %s (last mirror %.1fs ago)",
                         str(sample.get("conditionId",""))[:12], sample.get("side"),
                         now_ts - decision_seen[key])
                continue
            decision_seen[key] = now_ts
            if not enabled:
                log.info("[KILL] target decision mkt=%s %s size=%.2f ($%.2f) [%d fills]",
                         str(sample.get("conditionId", ""))[:12],
                         sample.get("side"), g["size"], g["usdc"], len(g["txs"]))
                continue
            # synthesize an aggregated activity for mirror_trade
            agg = dict(sample)
            agg["size"] = g["size"]
            agg["usdcSize"] = g["usdc"]
            agg["transactionHash"] = g["txs"][0]  # first tx as group id
            agg["timestamp"] = g["ts"]
            try:
                guard = RiskGuard(cfg, trades_today=[])
                mirror_trade(client, agg, cfg, seen, guard, open_positions=0)
            except Exception as e:
                log.exception("mirror failed: %s", e)

        save_json(SEEN_FILE, list(seen)[-5000:])
        time.sleep(POLL_SECS)

    save_json(STATE_FILE, {**load_json(STATE_FILE, {}), "running": False})
    log.info("engine stopped.")


if __name__ == "__main__":
    sys.exit(main())
