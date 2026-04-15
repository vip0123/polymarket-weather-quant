# Whale Flow Signal

## Hypothesis

Large, consistently profitable traders ("whales") on Polymarket possess edge
— information, modeling, or execution — that the rest of the book lacks. If
their net flow into a specific market/outcome token leads price, we can
piggyback by entering on the same side and exiting after a short horizon.

> H0: whale net USDC flow into `(market_id, nonusdc_side)` during bar `t`
> has zero correlation with the forward return of that token's price over
> horizon `h`.
>
> H1: the correlation is positive (buying whales predict price up, selling
> whales predict price down) at horizons on the order of minutes to hours.

## Definitions

### Whale

A wallet is labeled a whale at time `t` if, using **only trades strictly
before `t`** (to avoid look-ahead), it satisfies one of:

- **Volume whale**: cumulative USDC notional traded in top `N_V` percentile
  (default top-200 wallets by lifetime-to-date volume).
- **P&L whale**: realized P&L using an average-cost / FIFO-style mark at
  the last observed trade price for remaining inventory, in the top
  `N_P` wallets (default top-200) with at least `MIN_TRADES` (default 50)
  fills.

The whale set used in the backtest is the **intersection** of volume and
P&L leaders, recomputed in rolling fashion (see `analyze.py`).

We rank wallets with a **single expanding snapshot taken at the midpoint
of the dataset** for the primary run (avoids per-bar recomputation cost,
still free of look-ahead for the second half which is the test period).
A `--rolling` mode recomputes the cohort at each bar end using only past
data; it is slower but fully leak-proof. The expanding snapshot is used
for the headline numbers; the rolling variant is run as a robustness
check and reported in `findings.md`.

Platform wallets (`poly_utils.PLATFORM_WALLETS`) are excluded.

### Net whale flow

For each `(market_id, nonusdc_side, bucket)` where bucket ∈ {15m, 1h, 6h,
24h}:

```
flow = Σ usd_amount * (+1 if maker_direction == "BUY" else −1)
       over trades where maker ∈ WhaleSet
```

`maker_direction` is BUY when the whale (maker) receives outcome tokens and
pays USDC — i.e., going long on that token. We sum USDC notional, signed.

### Predictive variable

For the same `(market_id, nonusdc_side)`, the VWAP of **all** trades in
bucket `t` is `p_t`. Forward return at horizon `h` bars is
`r_{t→t+h} = p_{t+h} / p_t − 1`. We also report raw price change
`Δp = p_{t+h} − p_t` because Polymarket prices live in [0,1] and log
returns are ugly near the bounds.

### Horizons tested

15m, 1h, 6h, 24h.

### Metrics

- **Pearson / Spearman correlation** between signed flow (or z-scored
  flow) and forward return.
- **Information Coefficient (IC)**: Spearman rank correlation, computed
  per-day and averaged (daily-panel IC), plus pooled Spearman.
- **Bucketed hit-rate**: P(forward return same sign as flow | |flow| > τ)
  for a few thresholds.
- **Naive backtest**: at each bar end, for every market, if
  `flow_t > +τ` go long one unit of outcome token at next-bar open;
  `flow_t < −τ` go short. Exit after `h` bars. Report gross Sharpe
  (no fees, no slippage), turnover, hit rate, sample size. Long legs
  easy; short legs represented as buying the complementary outcome
  (price = 1 − p) since Polymarket supports both outcome tokens.

## Files

- `analyze.py` — the full pipeline. Run from the repo root:
  `uv run python signals/whale_flow/analyze.py`
  Options:
  - `--horizons 15m,1h,6h,24h`
  - `--cohort-sizes 50,100,200,500`
  - `--min-trades 50`
  - `--rolling` (slow, leak-proof cohort recomputation)
  - `--out signals/whale_flow/out/` (writes per-horizon stats and
    backtest equity curves as parquet)
- `findings.md` — numeric results with sample sizes, honest discussion
  of look-ahead and survivorship.

## Caveats & known biases

1. **Survivorship in the P&L ranking**: wallets that blew up and stopped
   trading are still included (they have finite trade counts and realized
   P&L through the date of their last fill). This biases the cohort
   toward "still active and lucky". The rolling variant mitigates this.
2. **Inventory mark**: realized P&L marks remaining inventory at the last
   traded price observed so far. For wallets that closed positions fully
   the mark is irrelevant; for wallets with large open positions the mark
   is noisy.
3. **Maker-only view**: `maker` column gives the complete set of a
   wallet's trades (per CLAUDE.md) but the price is the execution price
   from the maker's side; we do not separately price-in CLOB queue
   position advantage. Whales being market makers is part of their
   edge; we are measuring that edge, which is fine.
4. **Price staleness**: sparse markets have bars with no trades. We
   forward-fill VWAP across at most 2 consecutive bars, drop the rest.
5. **No fees/slippage** in the headline backtest. Polymarket charges
   ~2% round-trip effective on many markets; a signal needs to clear
   that. The `findings.md` includes a 1% and 2% round-trip variant.
