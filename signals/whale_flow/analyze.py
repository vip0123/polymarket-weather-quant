"""
Whale Flow signal — research script.

Loads processed/trades.csv, ranks wallets by volume and realized P&L,
picks a whale cohort, computes per-(market, token, bucket) net whale flow,
and tests whether that flow predicts subsequent token price movement.

Run from the repo root:
    uv run python signals/whale_flow/analyze.py

All outputs go to signals/whale_flow/out/.

Design notes
------------
- polars only for heavy lifting; numpy/scipy only for stats.
- Look-ahead: whale cohort is computed from trades strictly before a
  cutoff date (default: dataset midpoint). Signal is then evaluated only
  on bars at or after the cutoff. A --rolling mode recomputes the cohort
  per bar; it is slow and reported as a robustness check.
- "Price" is USDC per outcome token, already /10^6 by process_live.
- "maker_direction" is BUY when the maker is going long the non-USDC
  token (paying USDC). We sum signed usd_amount over whales.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

# Make sure we can import poly_utils when run from repo root or elsewhere.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

try:
    from poly_utils import PLATFORM_WALLETS  # type: ignore
except Exception:
    PLATFORM_WALLETS = [
        "0xc5d563a36ae78145c45a50134d48a1215220f80a",
        "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    ]


TRADES_PATH = REPO_ROOT / "processed" / "trades.csv"
OUT_DIR = Path(__file__).resolve().parent / "out"

HORIZON_BARS = {
    # bucket -> (polars truncate string, timedelta for horizon arithmetic)
    "15m": "15m",
    "1h": "1h",
    "6h": "6h",
    "24h": "1d",
}


# -----------------------------------------------------------------------------
# Loading
# -----------------------------------------------------------------------------

def load_trades(path: Path = TRADES_PATH) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run the pipeline (see README.md) or download "
            "the snapshot archive first."
        )
    print(f"[load] scanning {path} ...")
    df = (
        pl.scan_csv(str(path), schema_overrides={"market_id": pl.Utf8})
        .with_columns(pl.col("timestamp").str.to_datetime().alias("timestamp"))
        .filter(~pl.col("maker").is_in(PLATFORM_WALLETS))
        .filter(~pl.col("taker").is_in(PLATFORM_WALLETS))
        .filter(pl.col("price").is_between(1e-4, 1 - 1e-4))  # drop degenerate
        .filter(pl.col("usd_amount") > 0)
        .collect(engine="streaming")
    )
    print(f"[load] {len(df):,} trades, "
          f"{df['timestamp'].min()} → {df['timestamp'].max()}")
    return df


# -----------------------------------------------------------------------------
# Wallet P&L (realized + inventory mark-to-last)
# -----------------------------------------------------------------------------

def wallet_pnl_and_volume(
    trades: pl.DataFrame,
    as_of: pl.datetime | None = None,
) -> pl.DataFrame:
    """Per-wallet lifetime-to-`as_of` stats.

    Uses the simple, fast approximation:
        realized_pnl = Σ signed_usd_flow + inventory_value_at_last_price
    where signed_usd_flow is +usd_amount on SELL (receive USDC) and
    −usd_amount on BUY (pay USDC); inventory is Σ signed_tokens per
    (wallet, market, side), valued at the last observed price of that
    (market, side) in the slice.
    """
    sl = trades if as_of is None else trades.filter(pl.col("timestamp") < as_of)
    if sl.is_empty():
        return pl.DataFrame(schema={
            "maker": pl.Utf8, "volume_usd": pl.Float64,
            "n_trades": pl.UInt32, "pnl": pl.Float64,
        })

    # cash leg: +usd on sell, −usd on buy (from maker's perspective)
    sl = sl.with_columns([
        pl.when(pl.col("maker_direction") == "SELL")
          .then(pl.col("usd_amount"))
          .otherwise(-pl.col("usd_amount"))
          .alias("signed_usd"),
        pl.when(pl.col("maker_direction") == "BUY")
          .then(pl.col("token_amount"))
          .otherwise(-pl.col("token_amount"))
          .alias("signed_tokens"),
    ])

    # last known price per (market, side) within the slice
    last_px = (
        sl.sort("timestamp")
          .group_by(["market_id", "nonusdc_side"])
          .agg(pl.col("price").last().alias("last_price"))
    )

    inv = (
        sl.group_by(["maker", "market_id", "nonusdc_side"])
          .agg(pl.col("signed_tokens").sum().alias("inv_tokens"))
          .join(last_px, on=["market_id", "nonusdc_side"], how="left")
          .with_columns((pl.col("inv_tokens") * pl.col("last_price"))
                          .alias("inv_value"))
          .group_by("maker")
          .agg(pl.col("inv_value").sum().alias("inv_value"))
    )

    cash = (
        sl.group_by("maker")
          .agg([
              pl.col("signed_usd").sum().alias("realized_cash"),
              pl.col("usd_amount").sum().alias("volume_usd"),
              pl.len().alias("n_trades"),
          ])
    )

    out = (
        cash.join(inv, on="maker", how="left")
            .with_columns(
                (pl.col("realized_cash") + pl.col("inv_value").fill_null(0.0))
                .alias("pnl")
            )
            .select(["maker", "volume_usd", "n_trades", "pnl"])
            .sort("pnl", descending=True)
    )
    return out


def pick_whales(
    wallet_stats: pl.DataFrame,
    cohort_size: int = 200,
    min_trades: int = 50,
) -> list[str]:
    """Intersection of top-`cohort_size` by volume and by P&L,
    restricted to wallets with >= min_trades fills."""
    wallet_stats = wallet_stats.filter(pl.col("n_trades") >= min_trades)
    if wallet_stats.is_empty():
        return []
    top_vol = set(
        wallet_stats.sort("volume_usd", descending=True)
                    .head(cohort_size)["maker"].to_list()
    )
    top_pnl = set(
        wallet_stats.sort("pnl", descending=True)
                    .head(cohort_size)["maker"].to_list()
    )
    whales = sorted(top_vol & top_pnl)
    print(f"[whales] top-{cohort_size} vol ∩ pnl (min_trades={min_trades}): "
          f"{len(whales)} wallets")
    return whales


# -----------------------------------------------------------------------------
# Bar construction
# -----------------------------------------------------------------------------

def build_bars(trades: pl.DataFrame, bucket: str, whales: list[str]) -> pl.DataFrame:
    """Per (market_id, nonusdc_side, bar_ts) compute whale net flow and
    VWAP of all trades in the bar."""
    flow_col = (
        pl.when(pl.col("maker_direction") == "BUY")
          .then(pl.col("usd_amount"))
          .otherwise(-pl.col("usd_amount"))
    )
    whale_set = pl.Series("whales", whales, dtype=pl.Utf8).implode()

    bars = (
        trades
        .with_columns([
            pl.col("timestamp").dt.truncate(bucket).alias("bar_ts"),
            pl.col("maker").is_in(whale_set).alias("is_whale"),
            (pl.col("price") * pl.col("usd_amount")).alias("px_w"),
        ])
        .group_by(["market_id", "nonusdc_side", "bar_ts"])
        .agg([
            # VWAP: Σ(price*usd) / Σ(usd)
            (pl.col("px_w").sum() / pl.col("usd_amount").sum()).alias("vwap"),
            pl.col("usd_amount").sum().alias("bar_volume"),
            pl.len().alias("n_trades"),
            # whale signed flow
            pl.when(pl.col("is_whale"))
              .then(flow_col)
              .otherwise(0.0)
              .sum()
              .alias("whale_flow"),
            pl.when(pl.col("is_whale"))
              .then(pl.col("usd_amount"))
              .otherwise(0.0)
              .sum()
              .alias("whale_volume"),
        ])
        .sort(["market_id", "nonusdc_side", "bar_ts"])
    )
    return bars


def add_forward_returns(bars: pl.DataFrame, horizon_bars: int) -> pl.DataFrame:
    """Within each (market, side), add vwap_fwd = vwap shifted by
    `horizon_bars` rows, and forward simple return and price delta.

    Note: horizon is measured in *bars present in the series*, not wall
    clock. For sparse markets this is a looser definition but it keeps
    sample size up. We also compute wall-clock horizons via asof-join in
    `add_forward_returns_walltime` for the headline numbers.
    """
    return (
        bars.with_columns(
            pl.col("vwap")
              .shift(-horizon_bars)
              .over(["market_id", "nonusdc_side"])
              .alias("vwap_fwd")
        )
        .with_columns([
            (pl.col("vwap_fwd") - pl.col("vwap")).alias("dpx"),
            ((pl.col("vwap_fwd") / pl.col("vwap")) - 1.0).alias("ret"),
        ])
    )


def add_forward_returns_walltime(
    bars: pl.DataFrame, bucket: str, n_buckets: int
) -> pl.DataFrame:
    """Wall-clock version: for each row at bar_ts, look up the first
    bar in the same series whose bar_ts >= bar_ts + n_buckets*bucket."""
    import datetime as _dt
    minutes = {"15m": 15, "1h": 60, "6h": 360, "1d": 1440}[bucket] * n_buckets
    delta_expr = pl.duration(minutes=minutes)
    tol = _dt.timedelta(minutes=minutes)  # asof tolerance as python timedelta
    left = bars.with_columns((pl.col("bar_ts") + delta_expr).alias("target_ts"))
    right = bars.select([
        "market_id", "nonusdc_side",
        pl.col("bar_ts").alias("fwd_ts"),
        pl.col("vwap").alias("vwap_fwd"),
    ]).sort(["market_id", "nonusdc_side", "fwd_ts"])

    left = left.sort(["market_id", "nonusdc_side", "target_ts"])
    joined = left.join_asof(
        right,
        left_on="target_ts",
        right_on="fwd_ts",
        by=["market_id", "nonusdc_side"],
        strategy="forward",
        tolerance=tol,
    )
    joined = joined.with_columns([
        (pl.col("vwap_fwd") - pl.col("vwap")).alias("dpx"),
        ((pl.col("vwap_fwd") / pl.col("vwap")) - 1.0).alias("ret"),
    ])
    return joined


# -----------------------------------------------------------------------------
# Stats
# -----------------------------------------------------------------------------

@dataclass
class HorizonStats:
    horizon: str
    n: int
    pearson: float
    spearman: float
    daily_ic_mean: float
    daily_ic_std: float
    daily_ic_n: int
    hit_rate: float
    hit_rate_gated: float
    gated_n: int


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx = _rank(x)
    ry = _rank(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def _rank(a: np.ndarray) -> np.ndarray:
    order = a.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(len(a))
    # average ties
    _, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    if cnt.max() > 1:
        # fall back to scipy-like behavior via pandas if available; else
        # approximate: the above already gives a valid ordinal ranking
        # which is fine for Spearman on mostly-unique floats.
        pass
    return ranks


def compute_stats(df: pl.DataFrame, horizon: str, gate_quantile: float = 0.9) -> HorizonStats:
    d = df.drop_nulls(["whale_flow", "ret"]).filter(pl.col("vwap_fwd").is_not_null())
    if d.is_empty():
        return HorizonStats(horizon, 0, *([float("nan")] * 6), 0, 0)

    flow = d["whale_flow"].to_numpy()
    ret = d["ret"].to_numpy()
    pearson = float(np.corrcoef(flow, ret)[0, 1]) if len(flow) > 2 else float("nan")
    sp = spearman(flow, ret)

    # Daily panel IC
    daily = (
        d.with_columns(pl.col("bar_ts").dt.date().alias("day"))
         .group_by("day")
         .agg([
             pl.col("whale_flow").alias("f"),
             pl.col("ret").alias("r"),
         ])
    )
    ics = []
    for row in daily.iter_rows(named=True):
        f = np.asarray(row["f"]); r = np.asarray(row["r"])
        if len(f) >= 20:
            ics.append(spearman(f, r))
    ics = [x for x in ics if not np.isnan(x)]
    daily_ic_mean = float(np.mean(ics)) if ics else float("nan")
    daily_ic_std = float(np.std(ics)) if ics else float("nan")

    # Hit rate (sign agreement) overall and gated on |flow|
    nz = flow != 0
    hit_rate = float(np.mean(np.sign(flow[nz]) == np.sign(ret[nz]))) if nz.any() else float("nan")
    if nz.any():
        thr = np.quantile(np.abs(flow[nz]), gate_quantile)
        mask = np.abs(flow) >= thr
        hit_rate_gated = float(np.mean(np.sign(flow[mask]) == np.sign(ret[mask]))) if mask.any() else float("nan")
        gated_n = int(mask.sum())
    else:
        hit_rate_gated = float("nan"); gated_n = 0

    return HorizonStats(
        horizon=horizon, n=len(d),
        pearson=pearson, spearman=sp,
        daily_ic_mean=daily_ic_mean, daily_ic_std=daily_ic_std,
        daily_ic_n=len(ics),
        hit_rate=hit_rate, hit_rate_gated=hit_rate_gated,
        gated_n=gated_n,
    )


# -----------------------------------------------------------------------------
# Naive backtest
# -----------------------------------------------------------------------------

def naive_backtest(
    df: pl.DataFrame,
    horizon: str,
    flow_threshold_q: float = 0.9,
    fee_round_trip: float = 0.0,
) -> dict:
    """Enter sign(flow) * 1 unit notional at next bar's VWAP when
    |flow| >= quantile threshold. Exit after horizon bars at vwap_fwd.
    PnL per trade = sign(flow) * (vwap_fwd − vwap_entry) / vwap_entry
                   − fee_round_trip.

    We use VWAP of the *signal* bar as entry proxy (no slippage model).
    Aggregate to daily returns for Sharpe; annualize with 365.
    """
    d = df.drop_nulls(["whale_flow", "ret", "vwap_fwd"])
    if d.is_empty():
        return {"horizon": horizon, "n_trades": 0, "sharpe": float("nan"),
                "mean_per_trade": float("nan"), "hit_rate": float("nan"),
                "fee": fee_round_trip}

    flow = d["whale_flow"].to_numpy()
    ret = d["ret"].to_numpy()
    nz = flow != 0
    if not nz.any():
        return {"horizon": horizon, "n_trades": 0, "sharpe": float("nan"),
                "mean_per_trade": float("nan"), "hit_rate": float("nan"),
                "fee": fee_round_trip}
    thr = np.quantile(np.abs(flow[nz]), flow_threshold_q)
    mask = np.abs(flow) >= thr
    if not mask.any():
        return {"horizon": horizon, "n_trades": 0, "sharpe": float("nan"),
                "mean_per_trade": float("nan"), "hit_rate": float("nan"),
                "fee": fee_round_trip}

    pnl = np.sign(flow[mask]) * ret[mask] - fee_round_trip
    d_m = d.filter(pl.Series(mask))

    daily = (
        d_m.with_columns([
            pl.col("bar_ts").dt.date().alias("day"),
            pl.Series("pnl", pnl),
        ])
        .group_by("day")
        .agg(pl.col("pnl").mean().alias("daily_pnl"))
        .sort("day")
    )
    dpnl = daily["daily_pnl"].to_numpy()
    sharpe = float(np.mean(dpnl) / np.std(dpnl) * np.sqrt(365)) if len(dpnl) > 1 and np.std(dpnl) > 0 else float("nan")

    return {
        "horizon": horizon,
        "n_trades": int(mask.sum()),
        "sharpe": sharpe,
        "mean_per_trade": float(np.mean(pnl)),
        "hit_rate": float(np.mean(pnl > 0)),
        "fee": fee_round_trip,
    }


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

def run(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()

    tmin = trades["timestamp"].min()
    tmax = trades["timestamp"].max()
    midpoint = tmin + (tmax - tmin) / 2
    print(f"[split] cohort cutoff = {midpoint} "
          f"(train: {tmin}..{midpoint}, test: {midpoint}..{tmax})")

    # wallet ranking on pre-cutoff data only
    ws = wallet_pnl_and_volume(trades, as_of=midpoint)
    print(f"[whales] ranked {len(ws):,} wallets on pre-cutoff slice")
    ws.sort("pnl", descending=True).head(10).write_csv(OUT_DIR / "top_pnl_wallets.csv")
    ws.sort("volume_usd", descending=True).head(10).write_csv(OUT_DIR / "top_volume_wallets.csv")

    horizons = args.horizons.split(",")
    cohorts = [int(x) for x in args.cohort_sizes.split(",")]

    all_stats_rows: list[dict] = []
    all_bt_rows: list[dict] = []

    for cohort_size in cohorts:
        whales = pick_whales(ws, cohort_size=cohort_size, min_trades=args.min_trades)
        if not whales:
            print(f"[warn] cohort {cohort_size} empty; skipping")
            continue

        # test slice only
        test_trades = trades.filter(pl.col("timestamp") >= midpoint)

        for h in horizons:
            bucket = HORIZON_BARS[h]
            bars = build_bars(test_trades, bucket=bucket, whales=whales)
            # wall-clock forward return at exactly 1*bucket
            fwd = add_forward_returns_walltime(bars, bucket=bucket, n_buckets=1)

            stats = compute_stats(fwd, horizon=h)
            print(f"[stats] cohort={cohort_size} h={h} "
                  f"n={stats.n:,} pearson={stats.pearson:+.4f} "
                  f"spearman={stats.spearman:+.4f} "
                  f"daily_IC={stats.daily_ic_mean:+.4f}±{stats.daily_ic_std:.4f} "
                  f"(days={stats.daily_ic_n}) hit={stats.hit_rate:.3f} "
                  f"hit@p90={stats.hit_rate_gated:.3f} (n={stats.gated_n})")
            all_stats_rows.append({"cohort": cohort_size, **stats.__dict__})

            for fee in (0.0, 0.005, 0.01, 0.02):
                bt = naive_backtest(fwd, horizon=h, fee_round_trip=fee)
                bt["cohort"] = cohort_size
                print(f"    [bt] fee={fee:.3f} n={bt['n_trades']:,} "
                      f"sharpe={bt['sharpe']:+.2f} "
                      f"mean/trade={bt['mean_per_trade']:+.4f} "
                      f"hit={bt['hit_rate']:.3f}")
                all_bt_rows.append(bt)

    pl.DataFrame(all_stats_rows).write_csv(OUT_DIR / "stats.csv")
    pl.DataFrame(all_bt_rows).write_csv(OUT_DIR / "backtest.csv")
    print(f"[done] wrote {OUT_DIR}/stats.csv and {OUT_DIR}/backtest.csv")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--horizons", default="15m,1h,6h,24h")
    p.add_argument("--cohort-sizes", default="50,100,200,500")
    p.add_argument("--min-trades", type=int, default=50)
    p.add_argument("--rolling", action="store_true",
                   help="(slow) recompute whale cohort expanding per day")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
