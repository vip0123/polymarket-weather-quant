"""
Streaming variant: reads raw orderFilled events directly (either from
`goldsky/orderFilled.csv` or piped stdin — e.g. `curl ... | xz -d | stream_raw.py`),
derives trade fields inline, and runs the same disposition analysis as
`analyze.py`.

This avoids the need for a fully materialized `processed/trades.csv` on disk.
We use `asset_id` (the non-USDC side) as the position key — equivalent to
(market_id, nonusdc_side) since each asset_id uniquely identifies one outcome
token.

Usage:
    # From an existing orderFilled.csv:
    python stream_raw.py --input goldsky/orderFilled.csv

    # From a stream (no disk):
    curl -sL <url>.xz | xz -dc | python stream_raw.py --input - [--max-rows N]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Tuple


MIN_CLOSED_TRADES = 50
TOP_N = 100
RESOLVED_TOLERANCE = 0.05
MIN_TRADE_USD = 1.0


@dataclass
class Lot:
    qty: float
    price: float


@dataclass
class WalletState:
    lots: Dict[str, Deque[Lot]] = field(default_factory=lambda: defaultdict(deque))
    # running (total_qty, total_cost) per asset — O(1) updates, avoids summing lots
    summary: Dict[str, List[float]] = field(default_factory=dict)  # asset_id -> [qty, cost]
    open_assets: set = field(default_factory=set)  # assets with nonzero qty
    closes: List[tuple] = field(default_factory=list)
    realized_gains: int = 0
    realized_losses: int = 0
    paper_gains: int = 0
    paper_losses: int = 0


def parse_row(row: list, idx: dict):
    """Derive (maker, asset_id, direction, price, qty, usd, ts) from a raw
    orderFilled row (list, with index map). Returns None if malformed or dust."""
    try:
        maker_asset = row[idx["makerAssetId"]]
        taker_asset = row[idx["takerAssetId"]]
        maker = row[idx["maker"]]
        ma = float(row[idx["makerAmountFilled"]]) / 1e6
        ta = float(row[idx["takerAmountFilled"]]) / 1e6
        ts = row[idx["timestamp"]]
    except (KeyError, IndexError, ValueError):
        return None

    if maker_asset == "0" and taker_asset == "0":
        return None
    if maker_asset == "0":
        # maker paid USDC => maker BUY, receive outcome token (taker_asset)
        asset_id = taker_asset
        qty = ta
        usd = ma
        direction = "BUY"
    elif taker_asset == "0":
        # maker received USDC => maker SELL
        asset_id = maker_asset
        qty = ma
        usd = ta
        direction = "SELL"
    else:
        return None  # token-for-token, not a trade vs USDC

    if qty <= 0 or usd < MIN_TRADE_USD:
        return None
    price = usd / qty
    if price <= 0 or price >= 1.0:
        return None
    return (maker, asset_id, direction, price, qty, usd, ts)


def open_input(path: str):
    if path == "-":
        return sys.stdin
    return open(path, "r", newline="")


def run(input_path: str, out_dir: str, max_rows: int | None) -> None:
    os.makedirs(out_dir, exist_ok=True)
    wallets: Dict[str, WalletState] = defaultdict(WalletState)
    last_price: Dict[str, float] = {}

    n_rows = 0
    n_kept = 0
    t0 = time.time()

    fh = open_input(input_path)
    reader = csv.reader(fh)
    header = next(reader)
    idx = {name: i for i, name in enumerate(header)}
    for row in reader:
        n_rows += 1
        if max_rows and n_rows >= max_rows:
            break
        if n_rows % 1_000_000 == 0:
            dt = time.time() - t0
            print(
                f"  ... {n_rows:,} rows, {n_kept:,} kept, "
                f"{len(wallets):,} wallets, {len(last_price):,} assets, {dt:.0f}s",
                file=sys.stderr,
                flush=True,
            )

        parsed = parse_row(row, idx)
        if parsed is None:
            continue
        n_kept += 1
        maker, asset_id, direction, price, qty, usd, ts = parsed

        last_price[asset_id] = price
        ws = wallets[maker]

        if direction == "BUY":
            ws.lots[asset_id].append(Lot(qty=qty, price=price))
            s = ws.summary.get(asset_id)
            if s is None:
                ws.summary[asset_id] = [qty, qty * price]
            else:
                s[0] += qty
                s[1] += qty * price
            ws.open_assets.add(asset_id)
        else:  # SELL
            dq = ws.lots.get(asset_id)
            if not dq:
                continue
            q_remaining = qty
            cost_basis = 0.0
            closed_qty = 0.0
            while q_remaining > 1e-12 and dq:
                lot = dq[0]
                take = min(lot.qty, q_remaining)
                cost_basis += take * lot.price
                closed_qty += take
                lot.qty -= take
                q_remaining -= take
                if lot.qty <= 1e-12:
                    dq.popleft()

            if closed_qty <= 1e-12:
                continue

            # update summary
            s = ws.summary[asset_id]
            s[0] -= closed_qty
            s[1] -= cost_basis
            if s[0] <= 1e-9:
                ws.open_assets.discard(asset_id)
                s[0] = 0.0
                s[1] = 0.0

            sell_proceeds = closed_qty * price
            realized_pnl = sell_proceeds - cost_basis

            if realized_pnl > 0:
                ws.realized_gains += 1
            elif realized_pnl < 0:
                ws.realized_losses += 1

            # paper gain/loss across remaining open positions (O(#open_assets))
            for other_asset in ws.open_assets:
                cur = last_price.get(other_asset)
                if cur is None:
                    continue
                os_ = ws.summary[other_asset]
                if os_[0] <= 1e-12:
                    continue
                avg_cost = os_[1] / os_[0]
                diff = cur - avg_cost
                if diff > 0:
                    ws.paper_gains += 1
                elif diff < 0:
                    ws.paper_losses += 1

            ws.closes.append((ts, asset_id, closed_qty, price, cost_basis, realized_pnl))

    fh.close()
    dt = time.time() - t0
    print(
        f"done: {n_rows:,} rows ({n_kept:,} kept) in {dt:.0f}s, "
        f"{len(wallets):,} wallets, {len(last_price):,} assets",
        file=sys.stderr, flush=True,
    )

    # ---- Leaderboard ----
    lb_rows = []
    for wallet, ws in wallets.items():
        n = len(ws.closes)
        if n < MIN_CLOSED_TRADES:
            continue
        rg, rl, pg, pl = ws.realized_gains, ws.realized_losses, ws.paper_gains, ws.paper_losses
        if rg + pg == 0 or rl + pl == 0:
            continue
        pgr = rg / (rg + pg)
        plr = rl / (rl + pl)
        total_pnl = sum(c[-1] for c in ws.closes)
        total_cost = sum(c[-2] for c in ws.closes)
        lb_rows.append({
            "wallet": wallet,
            "n_closes": n,
            "realized_gains": rg,
            "realized_losses": rl,
            "paper_gains": pg,
            "paper_losses": pl,
            "PGR": round(pgr, 5),
            "PLR": round(plr, 5),
            "disposition_score": round(pgr - plr, 5),
            "total_realized_pnl": round(total_pnl, 2),
            "realized_return_on_cost": round(total_pnl / total_cost, 5) if total_cost > 0 else 0.0,
        })
    lb_rows.sort(key=lambda r: r["disposition_score"], reverse=True)

    lb_path = os.path.join(out_dir, "disposition_leaderboard.csv")
    with open(lb_path, "w", newline="") as f:
        if lb_rows:
            w = csv.DictWriter(f, fieldnames=list(lb_rows[0].keys()))
            w.writeheader()
            w.writerows(lb_rows)
    print(f"wrote {lb_path} ({len(lb_rows)} wallets)", file=sys.stderr)

    # ---- Closed trades dump for leaderboard wallets ----
    lb_set = {r["wallet"] for r in lb_rows}
    ct_path = os.path.join(out_dir, "closed_trades.csv")
    with open(ct_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "wallet", "asset_id", "qty",
                    "sell_price", "cost_basis", "realized_pnl", "return_pct"])
        for wallet in lb_set:
            for (ts, aid, qty, p, cb, pnl) in wallets[wallet].closes:
                ret = pnl / cb if cb > 0 else 0.0
                w.writerow([ts, wallet, aid, f"{qty:.6f}",
                            f"{p:.6f}", f"{cb:.6f}", f"{pnl:.6f}", f"{ret:.6f}"])
    print(f"wrote {ct_path}", file=sys.stderr)

    # ---- Fade backtest ----
    top_cohort = [r["wallet"] for r in lb_rows[:TOP_N]]
    resolved = {
        k: v for k, v in last_price.items()
        if v <= RESOLVED_TOLERANCE or v >= 1.0 - RESOLVED_TOLERANCE
    }
    fade_gain = []
    fade_loss = []
    for wallet in top_cohort:
        for (ts, aid, qty, sp, cb, pnl) in wallets[wallet].closes:
            if aid not in resolved:
                continue
            final = resolved[aid]
            fwd = (final - sp) / sp if sp > 0 else 0.0
            notional = qty * sp
            if pnl > 0:
                fade_gain.append((ts, wallet, aid, sp, final, fwd, notional))
            elif pnl < 0:
                fade_loss.append((ts, wallet, aid, sp, final, fwd, notional))

    fb_path = os.path.join(out_dir, "fade_backtest.csv")
    with open(fb_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["leg", "timestamp", "wallet", "asset_id",
                    "entry_price", "final_price", "forward_return", "notional_usd"])
        for r in fade_gain:
            w.writerow(["fade_winner_sell", *r])
        for r in fade_loss:
            w.writerow(["follow_loser_sell", *r])
    print(f"wrote {fb_path}", file=sys.stderr)

    def _agg(rows):
        if not rows:
            return {"n": 0}
        rets = [r[5] for r in rows]
        wins = sum(1 for r in rets if r > 0)
        avg = sum(rets) / len(rets)
        notional = sum(r[6] for r in rows)
        wret = sum(r[5] * r[6] for r in rows) / notional if notional > 0 else 0.0
        return {
            "n": len(rets),
            "hit_rate": round(wins / len(rets), 4),
            "avg_return": round(avg, 5),
            "notional_weighted_return": round(wret, 5),
            "median_return": round(sorted(rets)[len(rets) // 2], 5),
        }

    summary = {
        "rows_processed": n_rows,
        "rows_kept": n_kept,
        "runtime_seconds": round(dt, 1),
        "n_wallets_total": len(wallets),
        "n_wallets_leaderboard": len(lb_rows),
        "n_resolved_assets": len(resolved),
        "n_assets_seen": len(last_price),
        "min_closed_trades": MIN_CLOSED_TRADES,
        "top_n_cohort": TOP_N,
        "resolved_tolerance": RESOLVED_TOLERANCE,
        "fade_winner_sell": _agg(fade_gain),
        "follow_loser_sell": _agg(fade_loss),
    }
    if lb_rows:
        tops = lb_rows[:TOP_N]
        summary["top_cohort_median_disposition"] = round(
            sorted(r["disposition_score"] for r in tops)[len(tops) // 2], 4
        )
        summary["top_cohort_mean_disposition"] = round(
            sum(r["disposition_score"] for r in tops) / len(tops), 4
        )
        summary["top_cohort_mean_realized_return_on_cost"] = round(
            sum(r["realized_return_on_cost"] for r in tops) / len(tops), 5
        )

    sum_path = os.path.join(out_dir, "summary.json")
    with open(sum_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="CSV path or '-' for stdin")
    ap.add_argument("--out-dir", default="signals/disposition/output")
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args()
    run(args.input, args.out_dir, args.max_rows)


if __name__ == "__main__":
    main()
