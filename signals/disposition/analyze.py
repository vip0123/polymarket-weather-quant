"""
Disposition-effect signal analysis for Polymarket trades.

Self-contained. Reads `processed/trades.csv` (path configurable via --trades) and
produces:
  - disposition_leaderboard.csv (per-wallet PGR, PLR, PGR-PLR, etc.)
  - closed_trades.csv           (every realized close, with P&L, unrealized-peer counts)
  - fade_backtest.csv           (simulated "fade their sells" trades with forward return to final price)
  - summary.json                (aggregate numbers referenced in findings.md)

Method
------
For each wallet (filtered via the `maker` column — see CLAUDE.md), we maintain
FIFO lots per (market_id, nonusdc_side). A SELL closes lots; a BUY opens a lot.
(Polymarket lets you hold both YES and NO of a market, but we treat sides
independently — which is how P&L actually accrues on-chain.)

At every SELL event (a "realization") we compute:
  * realized P&L on the closed quantity
  * whether this was a GAIN (realized_pnl > 0) or LOSS (realized_pnl < 0)
  * the unrealized position snapshot across this wallet's *other* open lots:
    each other open lot is at a paper gain or paper loss relative to the
    CURRENT price of its asset (best proxy available = this sell's price if
    same asset, else the last observed trade price for that asset).

Disposition metrics (Odean 1998):
  PGR = realized gains / (realized gains + paper gains)
  PLR = realized losses / (realized losses + paper losses)
  Disposition score = PGR - PLR   (positive = classic disposition bias)

We count "gains/losses" at the *trade event* level, not dollar-weighted, which
matches the original Odean formulation.

Fade backtest
-------------
For each wallet in the top-disposition cohort (min MIN_CLOSED_TRADES closes), we
look at every SELL event: if the sell is a realized gain (disposition bias =
"selling winners too early"), we BUY at that price and hold until the final
observed price on that asset. Fade return = (final_price - sell_price) /
sell_price.

We also test "follow their capitulation losses" (sell of a losing position) as
the opposite leg.

Caveats (see findings.md): final price is a proxy for resolution, and only
assets that have actually resolved (final price ~0 or ~1) give a clean payoff.
We restrict the backtest to assets whose last observed price is near 0 or 1
(within RESOLVED_TOLERANCE) to approximate "closed markets."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterator, List, Tuple

import csv


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MIN_CLOSED_TRADES = 50          # per-wallet sell-event minimum for leaderboard
TOP_N = 100                     # leaderboard size used in backtest
RESOLVED_TOLERANCE = 0.05       # |final_price - {0,1}| <= tol => treat as resolved
MIN_TRADE_USD = 1.0             # ignore dust (< $1)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Lot:
    qty: float           # remaining token qty
    price: float         # avg price paid per token


@dataclass
class WalletState:
    # open lots: key=(market_id, side) -> FIFO queue of Lot
    lots: Dict[Tuple[str, str], Deque[Lot]] = field(default_factory=lambda: defaultdict(deque))
    # realized: list of (timestamp, market_id, side, qty, sell_price, cost_basis, realized_pnl)
    closes: List[tuple] = field(default_factory=list)
    # running Odean counters (event-weighted)
    realized_gains: int = 0
    realized_losses: int = 0
    paper_gains: int = 0     # unrealized "other" positions at each sell, that are up
    paper_losses: int = 0    # unrealized "other" positions at each sell, that are down


# ---------------------------------------------------------------------------
# Streaming core
# ---------------------------------------------------------------------------
def iter_trades(trades_path: str) -> Iterator[dict]:
    """Yield trade dicts in file order. Streams the CSV row-by-row."""
    with open(trades_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def process(trades_path: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    wallets: Dict[str, WalletState] = defaultdict(WalletState)
    # last observed price per (market_id, side) — our "current price" proxy
    last_price: Dict[Tuple[str, str], float] = {}
    # track first/last timestamp per asset (useful metadata)
    asset_first_ts: Dict[Tuple[str, str], str] = {}
    asset_last_ts: Dict[Tuple[str, str], str] = {}

    n_rows = 0
    n_skipped = 0

    for row in iter_trades(trades_path):
        n_rows += 1
        if n_rows % 500_000 == 0:
            print(f"  ... {n_rows:,} rows, {len(wallets):,} wallets", file=sys.stderr)

        try:
            price = float(row["price"])
            usd = float(row["usd_amount"])
            qty = float(row["token_amount"])
        except (ValueError, KeyError):
            n_skipped += 1
            continue

        if usd < MIN_TRADE_USD or qty <= 0 or price <= 0 or price >= 1.0:
            # skip dust & degenerate prices (price must be in (0,1) for a prob)
            n_skipped += 1
            continue

        maker = row["maker"]
        market_id = row["market_id"]
        side = row["nonusdc_side"]           # "token1" or "token2"
        direction = row["maker_direction"]   # BUY / SELL (from maker's POV)
        ts = row["timestamp"]

        if not market_id or not side or market_id == "" or side == "":
            n_skipped += 1
            continue

        key = (market_id, side)
        last_price[key] = price
        asset_last_ts[key] = ts
        asset_first_ts.setdefault(key, ts)

        ws = wallets[maker]

        if direction == "BUY":
            ws.lots[key].append(Lot(qty=qty, price=price))
        elif direction == "SELL":
            # FIFO close against existing lots
            q_remaining = qty
            cost_basis = 0.0
            closed_qty = 0.0
            dq = ws.lots[key]
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
                # naked short / no open lot — ignore for disposition
                continue

            sell_proceeds = closed_qty * price
            realized_pnl = sell_proceeds - cost_basis

            # Odean event counters
            if realized_pnl > 0:
                ws.realized_gains += 1
            elif realized_pnl < 0:
                ws.realized_losses += 1
            # snapshot OTHER open lots (not the key we just sold, and
            # also exclude the just-sold key if still has lots) — actually
            # Odean includes the *remaining* portfolio across all other
            # positions at the time of the sale. We'll include all other
            # keys with open lots.
            for other_key, other_dq in ws.lots.items():
                if not other_dq:
                    continue
                cur = last_price.get(other_key, None)
                if cur is None:
                    continue
                # paper P&L per token = cur - avg_cost
                avg_cost = sum(l.price * l.qty for l in other_dq) / max(
                    sum(l.qty for l in other_dq), 1e-12
                )
                paper = cur - avg_cost
                if paper > 0:
                    ws.paper_gains += 1
                elif paper < 0:
                    ws.paper_losses += 1

            ws.closes.append((
                ts, market_id, side, closed_qty, price, cost_basis, realized_pnl
            ))

    print(f"processed rows={n_rows:,} skipped={n_skipped:,}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------
    leaderboard_rows = []
    for wallet, ws in wallets.items():
        n_closes = len(ws.closes)
        if n_closes < MIN_CLOSED_TRADES:
            continue
        rg, rl, pg, pl = ws.realized_gains, ws.realized_losses, ws.paper_gains, ws.paper_losses
        pgr_denom = rg + pg
        plr_denom = rl + pl
        if pgr_denom == 0 or plr_denom == 0:
            continue
        pgr = rg / pgr_denom
        plr = rl / plr_denom
        total_pnl = sum(c[-1] for c in ws.closes)
        total_cost = sum(c[-2] for c in ws.closes)
        ret = total_pnl / total_cost if total_cost > 0 else 0.0
        leaderboard_rows.append({
            "wallet": wallet,
            "n_closes": n_closes,
            "realized_gains": rg,
            "realized_losses": rl,
            "paper_gains": pg,
            "paper_losses": pl,
            "PGR": round(pgr, 5),
            "PLR": round(plr, 5),
            "disposition_score": round(pgr - plr, 5),
            "total_realized_pnl": round(total_pnl, 2),
            "realized_return_on_cost": round(ret, 5),
        })

    leaderboard_rows.sort(key=lambda r: r["disposition_score"], reverse=True)

    lb_path = os.path.join(out_dir, "disposition_leaderboard.csv")
    with open(lb_path, "w", newline="") as f:
        if leaderboard_rows:
            w = csv.DictWriter(f, fieldnames=list(leaderboard_rows[0].keys()))
            w.writeheader()
            w.writerows(leaderboard_rows)
    print(f"wrote {lb_path} ({len(leaderboard_rows)} wallets)", file=sys.stderr)

    # ------------------------------------------------------------------
    # Closed-trade dump (only for leaderboard wallets, to keep size bounded)
    # ------------------------------------------------------------------
    lb_wallets = {r["wallet"] for r in leaderboard_rows}
    ct_path = os.path.join(out_dir, "closed_trades.csv")
    with open(ct_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "wallet", "market_id", "side", "qty",
                    "sell_price", "cost_basis", "realized_pnl", "return_pct"])
        for wallet in lb_wallets:
            for (ts, mid, side, qty, p, cb, pnl) in wallets[wallet].closes:
                ret = pnl / cb if cb > 0 else 0.0
                w.writerow([ts, wallet, mid, side, f"{qty:.6f}",
                            f"{p:.6f}", f"{cb:.6f}", f"{pnl:.6f}", f"{ret:.6f}"])
    print(f"wrote {ct_path}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Fade backtest: top TOP_N most-disposition-biased wallets
    # ------------------------------------------------------------------
    top_cohort = [r["wallet"] for r in leaderboard_rows[:TOP_N]]
    top_set = set(top_cohort)

    # "resolved" assets: final price near 0 or 1
    resolved = {
        k: v for k, v in last_price.items()
        if v <= RESOLVED_TOLERANCE or v >= 1.0 - RESOLVED_TOLERANCE
    }

    fade_rows_gain = []    # fade their winner-sells (buy at sell price, hold to final)
    fade_rows_loss = []    # follow their loser-sells (sell at sell price — already out)
    for wallet in top_cohort:
        for (ts, mid, side, qty, sell_price, cb, pnl) in wallets[wallet].closes:
            key = (mid, side)
            if key not in resolved:
                continue
            final = resolved[key]
            fwd_ret = (final - sell_price) / sell_price if sell_price > 0 else 0.0
            notional = qty * sell_price
            if pnl > 0:
                # they sold a winner — fade means buy at sell_price, hold to final
                fade_rows_gain.append((ts, wallet, mid, side, sell_price, final, fwd_ret, notional))
            elif pnl < 0:
                fade_rows_loss.append((ts, wallet, mid, side, sell_price, final, fwd_ret, notional))

    fb_path = os.path.join(out_dir, "fade_backtest.csv")
    with open(fb_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["leg", "timestamp", "wallet", "market_id", "side",
                    "entry_price", "final_price", "forward_return", "notional_usd"])
        for r in fade_rows_gain:
            w.writerow(["fade_winner_sell", *r])
        for r in fade_rows_loss:
            w.writerow(["follow_loser_sell", *r])
    print(f"wrote {fb_path}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def _agg(rows):
        if not rows:
            return {"n": 0}
        rets = [r[6] for r in rows]
        wins = sum(1 for r in rets if r > 0)
        avg = sum(rets) / len(rets)
        notional = sum(r[7] for r in rows)
        wret = sum(r[6] * r[7] for r in rows) / notional if notional > 0 else 0.0
        return {
            "n": len(rets),
            "hit_rate": round(wins / len(rets), 4),
            "avg_return": round(avg, 5),
            "notional_weighted_return": round(wret, 5),
            "median_return": round(sorted(rets)[len(rets) // 2], 5),
        }

    summary = {
        "rows_processed": n_rows,
        "rows_skipped": n_skipped,
        "n_wallets_total": len(wallets),
        "n_wallets_leaderboard": len(leaderboard_rows),
        "n_resolved_assets": len(resolved),
        "n_assets_seen": len(last_price),
        "min_closed_trades": MIN_CLOSED_TRADES,
        "top_n_cohort": TOP_N,
        "resolved_tolerance": RESOLVED_TOLERANCE,
        "fade_winner_sell": _agg(fade_rows_gain),
        "follow_loser_sell": _agg(fade_rows_loss),
    }
    # Cohort stats
    if leaderboard_rows:
        top_scores = [r["disposition_score"] for r in leaderboard_rows[:TOP_N]]
        summary["top_cohort_median_disposition"] = round(
            sorted(top_scores)[len(top_scores) // 2], 4
        )
        summary["top_cohort_mean_disposition"] = round(
            sum(top_scores) / len(top_scores), 4
        )
        summary["top_cohort_mean_realized_return_on_cost"] = round(
            sum(r["realized_return_on_cost"] for r in leaderboard_rows[:TOP_N])
            / len(leaderboard_rows[:TOP_N]),
            5,
        )

    sum_path = os.path.join(out_dir, "summary.json")
    with open(sum_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"wrote {sum_path}", file=sys.stderr)
    print(json.dumps(summary, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="processed/trades.csv")
    ap.add_argument("--out-dir", default="signals/disposition/output")
    args = ap.parse_args()
    process(args.trades, args.out_dir)


if __name__ == "__main__":
    main()
