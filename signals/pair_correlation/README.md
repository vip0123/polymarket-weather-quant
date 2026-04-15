# Pair Correlation / Statistical Arbitrage on Polymarket

## Hypothesis

Polymarket lists many markets whose probabilities are mechanically or
economically linked:

1. **Intra-event competitors.** A `neg_risk=True` market is one outcome of a
   multi-outcome event (e.g. "Will X win the 2024 election?" vs. "Will Y win
   the 2024 election?"). Within a single `ticker` (== event) the probabilities
   of the `YES` tokens must sum to approximately 1. A pair of such YES tokens
   has a *known* no-arb constraint: `P_A + P_B <= 1`, often `< 1` if more than
   two candidates exist, but always mean-reverting around a shared event mean.
2. **Binary complements inside one market.** `token1` and `token2` within a
   single market sum to ~1 by construction. We *exclude* these from the signal
   — the arbitrage is well-known, typically only ~0.5–2% wide, and Polymarket
   orderbook liquidity on both sides is usually correlated so the spread
   collapses faster than the 2% assumed trading cost.
3. **Cross-event logical links.** "Will X be the Republican nominee?" and
   "Will X win the general election?" are tied because the second is bounded
   above by the first. These are harder to detect automatically — we rely on
   text similarity on `question` as a weak candidate filter, and discard pairs
   with no statistically significant cointegration.

The central hypothesis: for candidate pair `(A, B)`, let
`S_t = P_A,t - beta * P_B,t`. If `S_t` is stationary (cointegrated),
deviations from its rolling mean revert within a measurable half-life
`H`. Entering at `|z| > z_enter` and exiting at `|z| < z_exit` (or on timeout)
produces positive expectancy *if* reversion is faster than the pair's market
resolution and the spread round-trip cost is less than the edge captured.

## Candidate-pair construction

We use three pair sources, in order of signal strength (as we expect it):

| Source | Definition | Expected quality |
|---|---|---|
| `neg_risk` group | Same `ticker`, both `neg_risk=True`, comparing the YES side (`token1`) of each | Highest — structural link |
| Same `ticker` but independent | Same event grouping on `ticker`, not in a neg_risk group (e.g. multiple yes/no questions under one event) | Medium — event-level link |
| Question similarity | Cross-ticker pairs whose `question` strings share >= 3 significant tokens (after stopword/punctuation strip) AND whose close times overlap | Low — exploratory only |

We also require: (a) both markets have ≥ 500 trades, (b) overlapping active
period ≥ 14 days, (c) neither market has closed during the in-sample window.

## Price-series construction

`processed/trades.csv` is event-time. We convert each market's trade stream
to an **hourly** bar series using:

- bucket = `timestamp.truncate('1h')`
- bar price = **last trade price** within the bucket (not VWAP — we want the
  most recent mid-estimate, and Polymarket has enough tick noise that VWAP
  can mask true moves in low-volume hours)
- forward-fill across empty hours up to a 72-hour gap; beyond that, mark as
  NA (the market is essentially inactive).

Rationale: the trade-price ladder is the only observable we have from
`trades.csv` (no L2 book snapshots). Using the last trade biases our
"price" toward whichever side was most recently aggressive, which is
acceptable because the signal operates on hour-to-day divergences, not
sub-minute mispricing.

## Methodology

1. **Cointegration test** (Engle–Granger two-step): regress `P_A` on `P_B`
   + constant, take residuals, run an augmented Dickey–Fuller test. Keep
   pairs with ADF p-value < 0.05 on the *first half* of the sample (to
   avoid look-ahead).
2. **Hedge ratio** `beta`: OLS slope from the in-sample fit.
3. **Spread** `S_t = P_A - beta * P_B`.
4. **Rolling z-score**: `(S_t - mean_72h) / std_72h`, where the 72-hour
   window is chosen to cover a typical news cycle without being so short
   that single outliers dominate.
5. **Reversion half-life**: fit AR(1) to demeaned `S_t` (`dS = lambda * S
   + noise`), `H = -ln(2) / ln(1 + lambda)`. We keep pairs with `H` between
   4 hours and 14 days.

## Backtest rules

- **Entry**: when `|z_t| > 2.0`, take the spread: long the cheap leg,
  short the rich leg, sized so the two legs have equal notional at entry.
  On Polymarket "short" = buy the opposing `NO` token of that market
  (price `1 - P`), so the actual implementation is: buy A's YES and buy
  B's NO (or vice versa). See `analyze.py` for the mechanic.
- **Exit**: when `|z_t| < 0.25` (mean reversion), OR after `max_hold =
  max(7 days, 3 * H)`, OR if either market's `closedTime` is reached
  (forced unwind at last observable price — this is a known
  pessimistic-case).
- **Costs**: flat **2%** per round trip per leg (4% total on the pair).
  Polymarket spreads are typically 0.5–3% one-way depending on liquidity;
  we use 2% per leg as a conservative one-spread + half-spread slippage
  assumption for a single position open-and-close.
- **Sizing**: unit notional per trade, no compounding. Metrics reported
  per-pair and pooled.

## What "reversion" looks like (operational definition)

A "successful" pair trade is one where, after entry at `|z|>2`, the
spread crosses `|z|<0.25` *before* timeout and *before* resolution of
either market. We report:

- Win rate (share of trades that hit mean exit).
- Mean realized spread change (z-units) at exit.
- Half-life `H` distribution across kept pairs.
- PnL distribution before/after the 2% round-trip cost.

## Known failure modes (we explicitly check for these)

1. **Resolution asymmetry.** If market A resolves YES while market B is
   still trading, the spread collapses by a large amount that has nothing
   to do with reversion. Forced unwind at last observable price often
   realizes the loss. Mitigation: drop observations after either market's
   `closedTime`; report how many trades were terminated by resolution vs.
   reversion.
2. **Non-stationary beta.** A pair can look cointegrated in-sample and
   blow out out-of-sample when the event landscape changes (e.g. new
   candidate enters a race). We do in-sample/out-of-sample split and
   report the degradation.
3. **Thin books.** A market with 500 trades over 60 days has ~0.35
   trades/hour — the hourly last-price is stale and z-score spikes are
   phantom. We require 500 trades *minimum* but report performance
   stratified by liquidity decile.
4. **Neg-risk sum constraint.** For pairs within the same neg_risk
   group, the "arbitrage" is often really just a bet on which of the
   two leading candidates gains — if a third candidate collapses, both
   rise together and no reversion happens. We flag this by checking
   whether `P_A + P_B` trended up or down during the trade.
5. **Self-fulfilling cost.** A 2% round-trip assumption can be
   optimistic for the thin half of the market; we report what fraction
   of pairs' edges survive a 4% stress.

## Files

- `analyze.py` — end-to-end pipeline: load markets and trades, build pair
  candidates, build hourly price series, run cointegration/z-score,
  backtest. Writes `results_pairs.csv`, `results_trades.csv` and prints a
  summary.
- `findings.md` — actual numbers from running `analyze.py` against the
  locally available snapshot, honest negative results included.
