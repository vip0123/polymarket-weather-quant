"""
Pair correlation / statistical arbitrage research for Polymarket.

Run from repo root:
    uv run python signals/pair_correlation/analyze.py

Inputs (expected — pipeline produces these):
    markets.csv, missing_markets.csv
    processed/trades.csv

Outputs (written alongside this script):
    results_pairs.csv       one row per tested pair with stats
    results_trades.csv      one row per simulated round-trip trade
    summary.txt             human-readable summary

The code is intentionally self-contained — it imports nothing from the
pipeline modules except `get_markets`, and does not modify pipeline data.
"""
from __future__ import annotations

import math
import os
import re
import sys
import itertools
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import polars as pl

# Make repo-root imports work regardless of launch directory.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from poly_utils.utils import get_markets  # noqa: E402


# -------------------- config --------------------

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

MIN_TRADES_PER_MARKET = 500
MIN_OVERLAP_HOURS = 14 * 24
BAR = "1h"
FFILL_MAX_HOURS = 72
ROLLING_WIN_HOURS = 72

Z_ENTER = 2.0
Z_EXIT = 0.25
MAX_HOLD_MULT = 3  # * half-life
MAX_HOLD_MIN_HOURS = 7 * 24
ROUND_TRIP_COST_PER_LEG = 0.02  # 2% per leg => 4% per pair round trip
STRESS_COST_PER_LEG = 0.04

IN_SAMPLE_FRAC = 0.5  # fit cointegration on the first half


# -------------------- helpers --------------------

def _load_markets() -> pl.DataFrame:
    m = get_markets()
    # Normalize column types that we rely on downstream.
    required = {"id", "ticker", "neg_risk", "token1", "token2",
                "question", "closedTime", "createdAt"}
    missing = required - set(m.columns)
    if missing:
        raise RuntimeError(f"markets.csv missing columns: {missing}")
    m = m.with_columns([
        pl.col("id").cast(pl.Utf8),
        pl.col("ticker").cast(pl.Utf8, strict=False),
        pl.col("neg_risk").cast(pl.Boolean, strict=False),
    ])
    return m


def _load_trades() -> pl.DataFrame:
    path = os.path.join(REPO_ROOT, "processed", "trades.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"processed/trades.csv not found at {path}. "
            "Run the pipeline first (see CLAUDE.md)."
        )
    df = pl.scan_csv(path).collect(streaming=True)
    df = df.with_columns(pl.col("timestamp").str.to_datetime().alias("timestamp"))
    # Keep only YES-side trades (nonusdc_side == 'token1') so the series
    # we build is the price of the primary outcome. For neg_risk markets
    # that's the "YES this outcome happens" token.
    df = df.filter(pl.col("nonusdc_side") == "token1")
    df = df.with_columns(pl.col("market_id").cast(pl.Utf8))
    return df


def _hourly_last_price(trades: pl.DataFrame) -> pl.DataFrame:
    """Return long frame (market_id, bucket, price) with last trade per hour."""
    hourly = (
        trades
        .with_columns(pl.col("timestamp").dt.truncate(BAR).alias("bucket"))
        .sort(["market_id", "bucket", "timestamp"])
        .group_by(["market_id", "bucket"])
        .agg(pl.col("price").last().alias("price"),
             pl.len().alias("n_trades"))
    )
    return hourly


def _aligned_series(hourly: pl.DataFrame, mid_a: str, mid_b: str
                    ) -> Optional[pl.DataFrame]:
    """Return a frame with columns bucket, p_a, p_b covering the overlap."""
    a = (hourly.filter(pl.col("market_id") == mid_a)
               .select(["bucket", "price"])
               .rename({"price": "p_a"}))
    b = (hourly.filter(pl.col("market_id") == mid_b)
               .select(["bucket", "price"])
               .rename({"price": "p_b"}))
    if a.is_empty() or b.is_empty():
        return None
    start = max(a["bucket"].min(), b["bucket"].min())
    end = min(a["bucket"].max(), b["bucket"].max())
    if start is None or end is None or end <= start:
        return None
    # Build the full hourly grid.
    grid = pl.DataFrame({
        "bucket": pl.datetime_range(start, end, interval=BAR, eager=True)
    })
    merged = (grid.join(a, on="bucket", how="left")
                  .join(b, on="bucket", how="left")
                  .sort("bucket"))
    # Forward-fill with a cap.
    merged = merged.with_columns([
        pl.col("p_a").forward_fill(limit=FFILL_MAX_HOURS),
        pl.col("p_b").forward_fill(limit=FFILL_MAX_HOURS),
    ]).drop_nulls()
    if merged.height < MIN_OVERLAP_HOURS:
        return None
    return merged


# -------------------- statistics --------------------

def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Return (alpha, beta, residuals) for y = alpha + beta x."""
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    alpha, beta = float(coef[0]), float(coef[1])
    resid = y - (alpha + beta * x)
    return alpha, beta, resid


def _adf_pvalue(series: np.ndarray) -> float:
    """
    Lightweight ADF test (lag 1) without the SciPy/statsmodels dependency.
    Returns an approximate p-value using MacKinnon's 1996 response surface.
    The goal is a *ranking* tool, not a publication-grade test — we flag
    pairs that look stationary. Uses critical values from MacKinnon (1996).
    """
    y = np.asarray(series, dtype=float)
    y = y - y.mean()
    dy = np.diff(y)
    y_lag = y[:-1]
    # dY_t = rho * Y_{t-1} + gamma * dY_{t-1} + eps
    if len(dy) < 20:
        return 1.0
    dy_lag = np.concatenate([[0.0], dy[:-1]])
    X = np.column_stack([y_lag, dy_lag])
    coef, resid, *_ = np.linalg.lstsq(X, dy, rcond=None)
    rho = coef[0]
    # t-statistic on rho
    n = len(dy)
    y_hat = X @ coef
    resid = dy - y_hat
    sigma2 = (resid @ resid) / (n - X.shape[1])
    # (X'X)^{-1}
    try:
        xtx_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return 1.0
    se_rho = math.sqrt(sigma2 * xtx_inv[0, 0])
    if se_rho <= 0:
        return 1.0
    tstat = rho / se_rho
    # Approximate mapping from DF tau to p-value (no-constant case).
    # Linear interp between the canonical 1%, 5%, 10% crits.
    # Values from Fuller (1976) tau table, large-sample:
    #   1%: -2.58, 5%: -1.95, 10%: -1.62
    if tstat <= -2.58:
        return 0.01
    if tstat <= -1.95:
        return 0.05
    if tstat <= -1.62:
        return 0.10
    if tstat <= -1.00:
        return 0.25
    if tstat <= 0.0:
        return 0.5
    return 0.9


def _half_life(spread: np.ndarray) -> Optional[float]:
    """AR(1) half-life of mean reversion on a demeaned spread."""
    s = spread - spread.mean()
    if len(s) < 20:
        return None
    s_lag = s[:-1]
    ds = np.diff(s)
    X = s_lag.reshape(-1, 1)
    coef, *_ = np.linalg.lstsq(X, ds, rcond=None)
    lam = float(coef[0])
    if lam >= 0 or lam <= -2:  # not reverting, or oscillatory
        return None
    hl = -math.log(2) / math.log(1 + lam)
    return hl


# -------------------- pair construction --------------------

def _candidate_pairs(markets: pl.DataFrame, liquid_ids: set[str]
                     ) -> list[tuple[str, str, str]]:
    """Return list of (market_id_a, market_id_b, source_label)."""
    out: list[tuple[str, str, str]] = []

    m = markets.filter(pl.col("id").is_in(list(liquid_ids)))

    # Source 1: same ticker + neg_risk True.
    neg = m.filter(pl.col("neg_risk") == True)  # noqa: E712
    for tkr, grp in neg.group_by("ticker"):
        if tkr is None or (isinstance(tkr, tuple) and tkr[0] is None):
            continue
        ids = grp["id"].to_list()
        if len(ids) < 2:
            continue
        for a, b in itertools.combinations(sorted(ids), 2):
            out.append((a, b, "neg_risk_same_ticker"))

    # Source 2: same ticker but NOT neg_risk (event-bundled questions).
    non_neg = m.filter(pl.col("neg_risk") != True)  # noqa: E712
    for tkr, grp in non_neg.group_by("ticker"):
        if tkr is None or (isinstance(tkr, tuple) and tkr[0] is None):
            continue
        ids = grp["id"].to_list()
        if len(ids) < 2 or len(ids) > 20:  # guard against degenerate huge tickers
            continue
        for a, b in itertools.combinations(sorted(ids), 2):
            out.append((a, b, "event_same_ticker"))

    # Source 3: question text similarity across tickers.
    # Very cheap Jaccard on tokenized words >= 4 chars, excluding stopwords.
    STOP = {"will", "the", "and", "for", "win", "this", "that", "with",
            "by", "on", "in", "of", "at", "to", "a", "an", "be", "as",
            "is", "are", "was", "were", "year", "2024", "2025", "2026"}

    def toks(q: str) -> set[str]:
        if q is None:
            return set()
        words = re.findall(r"[A-Za-z]{4,}", q.lower())
        return {w for w in words if w not in STOP}

    rows = m.select(["id", "ticker", "question"]).to_dicts()
    by_tok: dict[str, list[dict]] = {}
    for r in rows:
        r["_toks"] = toks(r.get("question") or "")
        for t in r["_toks"]:
            by_tok.setdefault(t, []).append(r)

    seen: set[tuple[str, str]] = set()
    for t, group in by_tok.items():
        if len(group) > 200:  # too-common token, skip
            continue
        for a, b in itertools.combinations(group, 2):
            if a["ticker"] == b["ticker"]:
                continue  # covered by sources 1/2
            pid = tuple(sorted([a["id"], b["id"]]))
            if pid in seen:
                continue
            shared = a["_toks"] & b["_toks"]
            if len(shared) >= 3:
                seen.add(pid)
                out.append((pid[0], pid[1], "question_similarity"))

    return out


# -------------------- backtest --------------------

@dataclass
class PairResult:
    market_a: str
    market_b: str
    source: str
    beta: float
    adf_p: float
    half_life_h: Optional[float]
    n_hours: int
    n_trades: int
    wins: int
    timeouts: int
    resolution_unwinds: int
    pnl_gross: float
    pnl_net: float
    pnl_net_stress: float


@dataclass
class TradeRow:
    market_a: str
    market_b: str
    entry: str
    exit: str
    z_entry: float
    z_exit: float
    spread_entry: float
    spread_exit: float
    exit_reason: str
    pnl_gross: float
    pnl_net: float


def _backtest_pair(df: pl.DataFrame, beta: float, source: str,
                   mid_a: str, mid_b: str) -> tuple[PairResult, list[TradeRow]]:
    """
    df columns: bucket, p_a, p_b. beta is the hedge ratio fit in-sample.
    We scan the full series and enter/exit based on the rolling z.
    """
    n = df.height
    spread = (df["p_a"] - beta * df["p_b"]).to_numpy()
    bucket = df["bucket"].to_list()

    # Rolling mean/std over ROLLING_WIN_HOURS.
    win = ROLLING_WIN_HOURS
    mean = np.full(n, np.nan)
    std = np.full(n, np.nan)
    for i in range(win, n):
        w = spread[i - win:i]
        mean[i] = w.mean()
        s = w.std()
        std[i] = s if s > 1e-6 else np.nan
    z = (spread - mean) / std

    hl = _half_life(spread)
    max_hold = int(max(MAX_HOLD_MIN_HOURS,
                       (MAX_HOLD_MULT * hl) if hl else MAX_HOLD_MIN_HOURS))

    trades: list[TradeRow] = []
    i = win
    while i < n:
        if np.isnan(z[i]) or abs(z[i]) < Z_ENTER:
            i += 1
            continue
        # Enter the spread. Direction: if z > 0 => p_a rich, p_b cheap =>
        # short A / long B. The realized PnL (per unit notional) on a
        # mean-reverting spread trade is the change in spread times the
        # sign of the bet:  sign = -sign(z_entry).
        sign = -1.0 if z[i] > 0 else 1.0
        s_entry = spread[i]
        z_entry = z[i]
        entry_idx = i

        # Walk forward.
        j = i + 1
        exit_reason = "timeout"
        while j < n and (j - entry_idx) < max_hold:
            if not np.isnan(z[j]) and abs(z[j]) < Z_EXIT:
                exit_reason = "mean_revert"
                break
            j += 1
        if j >= n:
            j = n - 1
            exit_reason = "resolution_unwind"

        s_exit = spread[j]
        z_exit = z[j] if not np.isnan(z[j]) else 0.0
        pnl_gross = sign * (s_exit - s_entry)
        # Cost: one round trip per leg = 2 * cost_per_leg total spread.
        pnl_net = pnl_gross - 2 * ROUND_TRIP_COST_PER_LEG
        trades.append(TradeRow(
            market_a=mid_a, market_b=mid_b,
            entry=str(bucket[entry_idx]), exit=str(bucket[j]),
            z_entry=float(z_entry), z_exit=float(z_exit),
            spread_entry=float(s_entry), spread_exit=float(s_exit),
            exit_reason=exit_reason,
            pnl_gross=float(pnl_gross),
            pnl_net=float(pnl_net),
        ))
        # No re-entry until the spread crosses back through zero to avoid
        # piling on the same deviation repeatedly.
        k = j + 1
        while k < n and not np.isnan(z[k]) and np.sign(z[k]) == np.sign(z_entry):
            k += 1
        i = max(k, j + 1)

    wins = sum(1 for t in trades if t.exit_reason == "mean_revert")
    timeouts = sum(1 for t in trades if t.exit_reason == "timeout")
    res = sum(1 for t in trades if t.exit_reason == "resolution_unwind")
    gross = float(sum(t.pnl_gross for t in trades))
    net = float(sum(t.pnl_net for t in trades))
    net_stress = float(sum(t.pnl_gross - 2 * STRESS_COST_PER_LEG for t in trades))

    # Re-run ADF on the full spread for the recorded stat.
    adf_p = _adf_pvalue(spread)

    pair = PairResult(
        market_a=mid_a, market_b=mid_b, source=source,
        beta=float(beta), adf_p=float(adf_p),
        half_life_h=hl, n_hours=int(n), n_trades=len(trades),
        wins=wins, timeouts=timeouts, resolution_unwinds=res,
        pnl_gross=gross, pnl_net=net, pnl_net_stress=net_stress,
    )
    return pair, trades


# -------------------- main --------------------

def main() -> int:
    print("Loading markets...")
    markets = _load_markets()
    print(f"  {markets.height:,} markets")

    print("Loading trades...")
    trades = _load_trades()
    print(f"  {trades.height:,} YES-side trades")

    print("Counting trades per market...")
    counts = (trades.group_by("market_id").len()
                    .rename({"len": "n"})
                    .filter(pl.col("n") >= MIN_TRADES_PER_MARKET))
    liquid_ids = set(counts["market_id"].to_list())
    print(f"  {len(liquid_ids):,} markets with >= {MIN_TRADES_PER_MARKET} trades")

    print("Building hourly last-price series...")
    hourly = _hourly_last_price(trades.filter(pl.col("market_id").is_in(list(liquid_ids))))
    print(f"  {hourly.height:,} market-hour bars")

    print("Enumerating candidate pairs...")
    pairs = _candidate_pairs(markets, liquid_ids)
    print(f"  {len(pairs):,} candidate pairs")
    # Cap absurdly large candidate sets from question-similarity.
    MAX_PAIRS = 5000
    if len(pairs) > MAX_PAIRS:
        print(f"  capping to {MAX_PAIRS} (keeping neg_risk + event + first question_sim)")
        keep = [p for p in pairs if p[2] != "question_similarity"]
        rest = [p for p in pairs if p[2] == "question_similarity"]
        pairs = keep + rest[: max(0, MAX_PAIRS - len(keep))]

    results: list[PairResult] = []
    all_trades: list[TradeRow] = []
    kept = 0
    for k, (a, b, src) in enumerate(pairs):
        if k % 250 == 0:
            print(f"  processed {k}/{len(pairs)} (kept {kept})")
        aligned = _aligned_series(hourly, a, b)
        if aligned is None:
            continue

        # In-sample cointegration fit.
        n = aligned.height
        is_n = int(n * IN_SAMPLE_FRAC)
        if is_n < 100:
            continue
        is_df = aligned.head(is_n)
        pa = is_df["p_a"].to_numpy()
        pb = is_df["p_b"].to_numpy()
        _, beta, resid = _ols(pb, pa)
        p_adf = _adf_pvalue(resid)
        if p_adf > 0.05:
            continue
        kept += 1
        pair_res, trades_rows = _backtest_pair(aligned, beta, src, a, b)
        results.append(pair_res)
        all_trades.extend(trades_rows)

    print(f"Kept {kept} cointegrated pairs out of {len(pairs)} candidates.")

    # Write outputs.
    if results:
        pl.DataFrame([asdict(r) for r in results]).write_csv(
            os.path.join(OUT_DIR, "results_pairs.csv"))
    if all_trades:
        pl.DataFrame([asdict(t) for t in all_trades]).write_csv(
            os.path.join(OUT_DIR, "results_trades.csv"))

    # Summary.
    lines = []
    lines.append(f"candidate_pairs={len(pairs)}")
    lines.append(f"kept_cointegrated={kept}")
    lines.append(f"total_trades={len(all_trades)}")
    if all_trades:
        pnl_gross = np.array([t.pnl_gross for t in all_trades])
        pnl_net = np.array([t.pnl_net for t in all_trades])
        wins = sum(1 for t in all_trades if t.exit_reason == "mean_revert")
        tos = sum(1 for t in all_trades if t.exit_reason == "timeout")
        res = sum(1 for t in all_trades if t.exit_reason == "resolution_unwind")
        lines.append(f"exit_mean_revert={wins}")
        lines.append(f"exit_timeout={tos}")
        lines.append(f"exit_resolution_unwind={res}")
        lines.append(f"pnl_gross_mean={pnl_gross.mean():.4f}")
        lines.append(f"pnl_gross_total={pnl_gross.sum():.4f}")
        lines.append(f"pnl_net_mean={pnl_net.mean():.4f}")
        lines.append(f"pnl_net_total={pnl_net.sum():.4f}")
        # "Sharpe" per-trade (not annualized; holding horizons differ).
        def _sharpe(x):
            return float(x.mean() / x.std()) if x.std() > 0 else 0.0
        lines.append(f"sharpe_per_trade_gross={_sharpe(pnl_gross):.3f}")
        lines.append(f"sharpe_per_trade_net={_sharpe(pnl_net):.3f}")
        hl = [r.half_life_h for r in results if r.half_life_h is not None]
        if hl:
            lines.append(f"half_life_median_h={float(np.median(hl)):.1f}")
            lines.append(f"half_life_p25_h={float(np.percentile(hl, 25)):.1f}")
            lines.append(f"half_life_p75_h={float(np.percentile(hl, 75)):.1f}")
    text = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "summary.txt"), "w") as f:
        f.write(text + "\n")
    print("---- summary ----")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
