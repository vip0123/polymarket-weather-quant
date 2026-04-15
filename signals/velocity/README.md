# VELOCITY signal

Sudden accelerations in **volume** or **price** on a Polymarket market either
precede continuation (momentum) or exhaustion (mean reversion). This study
measures *which* regime dominates and over *what horizons*.

## Hypothesis

Let `v_t` be either volume-velocity or price-velocity measured in a bucket of
size `B` minutes for a given `market_id`. Normalise it against the market's
*own* rolling history:

```
z_t = (v_t - mean_{t-W..t-1}(v)) / std_{t-W..t-1}(v)
```

`mean` / `std` are computed over a trailing window `W` buckets (default
`W = 60`) excluding the current bucket to avoid look-ahead. `z` is truncated
when `std < eps` (dead markets).

### H1 - Momentum

High `|z|` buckets predict returns in the same direction as the velocity
(price-velocity) or same direction as the prevailing drift (volume-velocity).

### H2 - Mean reversion

High `|z|` buckets are followed by reversal: forward returns sign-flip vs.
the velocity direction.

### H3 - Horizon crossover

There exists a horizon `H*` such that momentum dominates for `H < H*` and
reversion dominates for `H > H*` (or vice-versa). The study aims to locate
`H*` if it exists per timeframe.

## Definitions

Buckets at `B in {1m, 5m, 1h}`, indexed per `(market_id, bucket_ts)`. For
each bucket:

| symbol      | definition |
|-------------|------------|
| `vol_usd`   | `sum(usd_amount)` in bucket |
| `n_trades`  | count of trades in bucket |
| `vwap`      | `sum(price * usd_amount) / sum(usd_amount)` |
| `close`     | last trade price in bucket |
| `ret`       | `log(vwap_t) - log(vwap_{t-1})` (forward/backward as noted) |

Two velocities:

1. **Volume-velocity** `vv_t = vol_usd_t`. (First-order rate per unit bucket
   is just `vol_usd` since buckets are fixed-width; the "velocity" language
   is retained because the z-score vs trailing distribution is what captures
   acceleration-above-baseline. Equivalently, `dVolume/dt` with a fixed dt
   is collinear with volume itself.)
2. **Price-velocity** `pv_t = ret_t = log(vwap_t / vwap_{t-1})`. Signed so
   that direction is retained.

Z-scoring against trailing `W=60` buckets (excludes current bucket - no
leak):

```
z_vol_t = (vv_t - mean_{W}(vv)) / std_{W}(vv)
z_pv_t  = (pv_t - mean_{W}(pv)) / std_{W}(pv)
```

Trigger sets:

- `TRIG_VOL`: `z_vol_t >= 2` (volume surge)
- `TRIG_UP`:  `z_pv_t  >= 2` (up-price surge)
- `TRIG_DN`:  `z_pv_t  <= -2` (down-price surge)

## Horizons tested

For every timeframe we measure forward **log-return** of `vwap` over
`H in {1, 2, 3, 5, 10, 20, 50}` buckets ahead. At the 1m timeframe this
spans 1 min to 50 min; at 1h it spans 1 h to 50 h.

## Backtests

Two complementary PnL constructions per trigger, *without* transaction
costs (Polymarket taker fees are 0 but there is a half-spread cost that we
flag separately in `findings.md`):

- **Momentum leg**: enter in the direction of `sign(z_pv_t)` (price trigger)
  or in the direction of the *contemporaneous* return (volume trigger),
  hold `H` buckets, close at the `H`-bucket vwap.
- **Reversion leg**: same entries, opposite direction.

Reported per quantile bucket of `z`:

- forward return mean / median
- hit-rate
- Sharpe (per-trade, annualised assuming 365*24*60/B trades/yr but we
  only compare relative numbers)
- `N` trade count

## Liquidity filter

Markets with fewer than `MIN_TRADES = 500` total trades or median bucket
volume `< $100` are reported separately as **noise-dominated**. The main
panel uses the liquid subset so baselines are informative.

## Forward-return pitfalls handled

- `vwap_{t+H}` uses the *bucket* at `t+H` (so execution would happen at
  the vwap of a future minute); not tick-exact but a standard convention.
- We drop buckets without a successor `H` steps forward (right-censoring).
- Rolling baseline uses `min_periods = W // 2` and is computed *strictly
  before* the current bucket via `shift(1)` on the running stats.
- `z` is clipped to `[-10, 10]` before quantile assignment to tame outliers
  caused by thin trailing windows.
- Probabilities live in `(0,1)`; log-returns are well defined provided we
  drop buckets where vwap is exactly 0 or 1 (absorbed markets). These are
  logged and excluded.

## Files

- `analyze.py` - end-to-end analysis, writes `results/<tf>_*.csv`
- `findings.md` - numerical results (populated after running `analyze.py`)
