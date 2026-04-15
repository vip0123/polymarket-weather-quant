"""VELOCITY signal analysis for Polymarket trades.

Reads ``processed/trades.csv``, buckets trades at multiple timeframes,
computes volume- and price-velocity z-scores against each market's own
trailing window, and evaluates forward returns at multiple horizons for
both momentum and mean-reversion interpretations.

Run from repo root::

    uv run python signals/velocity/analyze.py

Outputs land in ``signals/velocity/results/``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = REPO_ROOT / "processed" / "trades.csv"
OUT_DIR = Path(__file__).resolve().parent / "results"

# ---- parameters ----------------------------------------------------------

TIMEFRAMES = {
    "1m": "1m",
    "5m": "5m",
    "1h": "1h",
}
HORIZONS = [1, 2, 3, 5, 10, 20, 50]
ROLL_W = 60                 # trailing buckets used for z-score baseline
ROLL_MIN_PERIODS = 30
Z_CLIP = 10.0
Z_TRIGGER = 2.0
MIN_TRADES_MARKET = 500     # liquidity cut
MIN_MEDIAN_VOL = 100.0      # $ per bucket
EPS = 1e-9


# ---- loading -------------------------------------------------------------

def load_trades(path: Path = TRADES_CSV) -> pl.LazyFrame:
    """Lazy-load trades. Coerces timestamp to datetime if it's epoch seconds."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download the snapshot (see README.md) and "
            "run the pipeline first."
        )
    lf = pl.scan_csv(path)
    # timestamp may be int (unix seconds) or already a datetime string
    schema = lf.collect_schema()
    ts_dtype = schema["timestamp"]
    if ts_dtype in (pl.Int64, pl.Int32, pl.Float64):
        lf = lf.with_columns(
            pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="s")
            .alias("timestamp")
        )
    else:
        lf = lf.with_columns(pl.col("timestamp").cast(pl.Datetime))
    return lf.select(
        "timestamp", "market_id", "price",
        pl.col("usd_amount").cast(pl.Float64),
        pl.col("token_amount").cast(pl.Float64),
    )


# ---- bucketing -----------------------------------------------------------

def bucket_trades(lf: pl.LazyFrame, every: str) -> pl.DataFrame:
    """Aggregate trades into fixed-width buckets per market."""
    grouped = (
        lf.with_columns(pl.col("timestamp").dt.truncate(every).alias("bucket"))
        .group_by(["market_id", "bucket"])
        .agg(
            pl.col("usd_amount").sum().alias("vol_usd"),
            pl.len().alias("n_trades"),
            (pl.col("price") * pl.col("usd_amount")).sum().alias("_pv_num"),
            pl.col("usd_amount").sum().alias("_pv_den"),
            pl.col("price").last().alias("close"),
            pl.col("price").first().alias("open"),
        )
        .with_columns(
            (pl.col("_pv_num") / (pl.col("_pv_den") + EPS)).alias("vwap"),
        )
        .drop("_pv_num", "_pv_den")
        .sort(["market_id", "bucket"])
    )
    return grouped.collect()


# ---- liquidity filter ----------------------------------------------------

def liquid_markets(buckets: pl.DataFrame) -> pl.Series:
    per_mkt = (
        buckets.group_by("market_id")
        .agg(
            pl.col("n_trades").sum().alias("tot_trades"),
            pl.col("vol_usd").median().alias("med_vol"),
        )
    )
    liq = per_mkt.filter(
        (pl.col("tot_trades") >= MIN_TRADES_MARKET)
        & (pl.col("med_vol") >= MIN_MEDIAN_VOL)
    )
    return liq["market_id"]


# ---- velocity / z-score --------------------------------------------------

def add_velocity(buckets: pl.DataFrame) -> pl.DataFrame:
    """Add lagged log-return, z-scored volume and price velocities.

    Baselines use a trailing window that *excludes* the current bucket via
    ``shift(1)`` so there is no look-ahead.
    """
    # log vwap, drop degenerate prices (0 or 1 exactly => absorbed market)
    b = buckets.filter(
        (pl.col("vwap") > 1e-4) & (pl.col("vwap") < 1 - 1e-4)
    ).with_columns(pl.col("vwap").log().alias("log_vwap"))

    b = b.with_columns(
        (pl.col("log_vwap") - pl.col("log_vwap").shift(1).over("market_id"))
        .alias("ret_1")
    )

    roll_kwargs = dict(window_size=ROLL_W, min_samples=ROLL_MIN_PERIODS)

    b = b.with_columns(
        pl.col("vol_usd").shift(1).rolling_mean(**roll_kwargs).over("market_id")
            .alias("vol_mean"),
        pl.col("vol_usd").shift(1).rolling_std(**roll_kwargs).over("market_id")
            .alias("vol_std"),
        pl.col("ret_1").shift(1).rolling_mean(**roll_kwargs).over("market_id")
            .alias("ret_mean"),
        pl.col("ret_1").shift(1).rolling_std(**roll_kwargs).over("market_id")
            .alias("ret_std"),
    )

    b = b.with_columns(
        ((pl.col("vol_usd") - pl.col("vol_mean"))
         / (pl.col("vol_std") + EPS)).clip(-Z_CLIP, Z_CLIP).alias("z_vol"),
        ((pl.col("ret_1") - pl.col("ret_mean"))
         / (pl.col("ret_std") + EPS)).clip(-Z_CLIP, Z_CLIP).alias("z_pv"),
    )

    return b


# ---- forward returns -----------------------------------------------------

def add_forward_returns(b: pl.DataFrame, horizons=HORIZONS) -> pl.DataFrame:
    exprs = []
    for h in horizons:
        exprs.append(
            (pl.col("log_vwap").shift(-h).over("market_id")
             - pl.col("log_vwap")).alias(f"fwd_{h}")
        )
    return b.with_columns(exprs)


# ---- analysis ------------------------------------------------------------

def quantile_table(df: pl.DataFrame, z_col: str, horizons=HORIZONS,
                   n_quantiles: int = 10) -> pl.DataFrame:
    """Bucket by z_col quantile and report forward-return stats per horizon."""
    valid = df.filter(pl.col(z_col).is_not_null())
    if valid.height == 0:
        return pl.DataFrame()
    qs = np.linspace(0, 1, n_quantiles + 1)[1:-1]
    edges = np.quantile(valid[z_col].to_numpy(), qs)
    # assign a bucket id 0..n_quantiles-1 per row using numpy then back to polars
    zvals = valid[z_col].to_numpy()
    qbucket = np.digitize(zvals, edges)  # 0..n_quantiles-1
    valid = valid.with_columns(pl.Series("qbucket", qbucket))

    rows = []
    for q in range(n_quantiles):
        sub = valid.filter(pl.col("qbucket") == q)
        rec = {"qbucket": q, "n": sub.height,
               "z_min": float(sub[z_col].min() or np.nan),
               "z_max": float(sub[z_col].max() or np.nan),
               "z_mean": float(sub[z_col].mean() or np.nan)}
        for h in horizons:
            col = f"fwd_{h}"
            s = sub.filter(pl.col(col).is_not_null())[col]
            if s.len() == 0:
                rec[f"mean_{h}"] = np.nan
                rec[f"median_{h}"] = np.nan
                rec[f"sharpe_{h}"] = np.nan
                rec[f"hit_{h}"] = np.nan
                rec[f"n_{h}"] = 0
                continue
            arr = s.to_numpy()
            rec[f"mean_{h}"] = float(arr.mean())
            rec[f"median_{h}"] = float(np.median(arr))
            rec[f"sharpe_{h}"] = float(arr.mean() / (arr.std(ddof=1) + EPS))
            rec[f"hit_{h}"] = float((arr > 0).mean())
            rec[f"n_{h}"] = int(arr.size)
        rows.append(rec)
    return pl.DataFrame(rows)


def trigger_backtest(df: pl.DataFrame, trigger_col: str, direction_col: str,
                     horizons=HORIZONS, z_trig: float = Z_TRIGGER) -> pl.DataFrame:
    """Backtest both momentum and reversion legs for a trigger.

    `direction_col` provides the sign (+1/-1) of the momentum bet. The
    reversion bet is its negation. Entries taken when ``trigger_col`` passes
    ``z_trig`` (positive side).
    """
    hits = df.filter(pl.col(trigger_col) >= z_trig)
    rows = []
    for h in horizons:
        col = f"fwd_{h}"
        sub = hits.filter(pl.col(col).is_not_null() & pl.col(direction_col).is_not_null())
        if sub.height == 0:
            continue
        signed = (sub[col].to_numpy() * sub[direction_col].to_numpy())
        rev = -signed
        rows.append({
            "horizon": h,
            "n": int(sub.height),
            "mom_mean": float(signed.mean()),
            "mom_sharpe": float(signed.mean() / (signed.std(ddof=1) + EPS)),
            "mom_hit": float((signed > 0).mean()),
            "rev_mean": float(rev.mean()),
            "rev_sharpe": float(rev.mean() / (rev.std(ddof=1) + EPS)),
            "rev_hit": float((rev > 0).mean()),
        })
    return pl.DataFrame(rows)


# ---- driver --------------------------------------------------------------

def run_timeframe(every: str, label: str, trades_lf: pl.LazyFrame,
                  out_dir: Path) -> dict:
    print(f"\n=== timeframe {label} ({every}) ===")
    buckets = bucket_trades(trades_lf, every)
    print(f"  total buckets: {buckets.height:,}")

    liq_ids = liquid_markets(buckets)
    print(f"  liquid markets: {liq_ids.len():,} "
          f"(of {buckets['market_id'].n_unique():,})")

    buckets_liq = buckets.filter(pl.col("market_id").is_in(liq_ids.implode()))
    b = add_velocity(buckets_liq)
    b = add_forward_returns(b)

    # summary stats -------------------------------------------------------
    summary = {
        "timeframe": label,
        "buckets_total": buckets.height,
        "buckets_liquid": buckets_liq.height,
        "markets_total": int(buckets["market_id"].n_unique()),
        "markets_liquid": int(liq_ids.len()),
    }

    # quantile tables -----------------------------------------------------
    qt_vol = quantile_table(b, "z_vol")
    qt_pv = quantile_table(b, "z_pv")
    qt_vol.write_csv(out_dir / f"{label}_quantile_zvol.csv")
    qt_pv.write_csv(out_dir / f"{label}_quantile_zpv.csv")

    # trigger backtests ---------------------------------------------------
    # price-velocity trigger: direction = sign(z_pv_t) at entry
    b_with_dir = b.with_columns(
        pl.col("z_pv").sign().alias("dir_pv"),
        pl.col("ret_1").sign().alias("dir_vol"),
    )
    # split positive and negative price-velocity triggers
    up = trigger_backtest(
        b_with_dir.with_columns(pl.lit(1).alias("dir_up")),
        "z_pv", "dir_up")
    dn_df = b_with_dir.with_columns((-pl.col("z_pv")).alias("z_pv_neg"),
                                    pl.lit(-1).alias("dir_dn"))
    dn = trigger_backtest(dn_df, "z_pv_neg", "dir_dn")
    vol = trigger_backtest(b_with_dir, "z_vol", "dir_vol")

    up.write_csv(out_dir / f"{label}_trigger_price_up.csv")
    dn.write_csv(out_dir / f"{label}_trigger_price_dn.csv")
    vol.write_csv(out_dir / f"{label}_trigger_volume.csv")

    # crossover detection -------------------------------------------------
    def find_crossover(bt: pl.DataFrame) -> int | None:
        if bt.is_empty():
            return None
        for row in bt.iter_rows(named=True):
            # momentum edge = mom_mean - rev_mean = 2*mom_mean
            if row["mom_mean"] < 0:
                return int(row["horizon"])
        return None

    summary["crossover_price_up"] = find_crossover(up)
    summary["crossover_price_dn"] = find_crossover(dn)
    summary["crossover_volume"] = find_crossover(vol)

    print(f"  price-up trigger xover: {summary['crossover_price_up']}")
    print(f"  price-dn trigger xover: {summary['crossover_price_dn']}")
    print(f"  volume trigger xover:   {summary['crossover_volume']}")

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default=str(TRADES_CSV))
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    trades_lf = load_trades(Path(args.trades))

    summaries = []
    for label, every in TIMEFRAMES.items():
        summaries.append(run_timeframe(every, label, trades_lf, out_dir))

    pl.DataFrame(summaries).write_csv(out_dir / "summary.csv")
    print(f"\nWrote results to {out_dir}")


if __name__ == "__main__":
    main()
