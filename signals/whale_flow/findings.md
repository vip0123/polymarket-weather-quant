# Whale Flow — Findings

## Status of this run

**No production numbers were produced in this environment.**

`processed/trades.csv` was not present on disk at authoring time, the
snapshot archive is ~6.2 GB xz (the decompressed CSV is larger), and the
working volume had ~6 GB free. I did not download the snapshot because
there was not enough disk headroom to decompress it. The archive link is
in `../../README.md`.

To reproduce, run the pipeline (`uv run python update_all.py`) or
download the snapshot, then:

```bash
uv run python signals/whale_flow/analyze.py
```

Results land in `signals/whale_flow/out/stats.csv` and
`signals/whale_flow/out/backtest.csv`; the driver also prints a human
summary line per `(cohort, horizon, fee)`.

## Code validation

`analyze.py` was executed end-to-end on a 20k-row synthetic fixture
(random makers/takers, random prices in [0.01, 0.99], uniform BUY/SELL
directions) with `--cohort-sizes 20,50 --min-trades 5`. It ran to
completion and produced the expected CSV outputs. On random data we
expect near-zero correlation, which is what we saw:

| cohort | horizon | n    | Pearson | Spearman | daily IC (mean±σ) | hit@p90       |
|--------|---------|------|---------|----------|-------------------|---------------|
| 20     | 15m     | 1546 | +0.022  | +0.025   | −0.003 ± 0.144    | 1.000 (n=2)   |
| 20     | 1h      | 4280 | +0.009  | +0.006   | +0.009 ± 0.057    | 0.714 (n=7)   |
| 20     | 6h      | 4061 | +0.003  | −0.002   | −0.006 ± 0.059    | 0.538 (n=13)  |
| 20     | 24h     | 1159 | +0.012  | −0.006   | +0.002 ± 0.136    | 0.417 (n=12)  |
| 50     | 15m     | 1546 | +0.037  | +0.033   | +0.012 ± 0.154    | 0.833 (n=6)   |
| 50     | 1h      | 4280 | +0.008  | +0.006   | +0.011 ± 0.052    | 0.737 (n=19)  |
| 50     | 6h      | 4061 | −0.011  | −0.006   | −0.006 ± 0.048    | 0.486 (n=35)  |
| 50     | 24h     | 1159 | +0.014  | +0.008   | +0.014 ± 0.147    | 0.516 (n=31)  |

On random synthetic data this is the correct answer: zero signal, tiny
ICs drowned in per-day noise. The eye-popping "Sharpe" values on the
smallest bucket (e.g. 15m with n=2) are pure small-sample artifacts, not
evidence of signal — they illustrate exactly why the `findings.md` on
real data must lean on large-n gated samples and on the daily IC
distribution, not on the point Sharpe of a gated backtest with a handful
of trades.

## What a real run must report

When run on `processed/trades.csv`, the expected output covers, at
minimum:

1. **IC by horizon** (15m, 1h, 6h, 24h): per-day Spearman rank
   correlation between whale net flow and forward VWAP return on the
   same `(market_id, nonusdc_side)`, averaged across days with ≥20
   observations, plus pooled Spearman and Pearson.
2. **Sharpe of the naive long/short strategy** gated at |flow| ≥ P90,
   for fees ∈ {0%, 0.5%, 1%, 2%} round-trip, at each horizon.
3. **Sensitivity to cohort size** ∈ {50, 100, 200, 500}. A signal that
   only appears at cohort=50 and dies at cohort=500 is a bet that the
   very top tail is qualitatively different, which is plausible but
   needs to be flagged.
4. **Sample sizes everywhere**, including how many daily-IC days, how
   many gated trades, and how many `(market, bar)` cells survive the
   forward-join tolerance.
5. **The top-P&L and top-volume wallet lists** (`out/top_pnl_wallets.csv`,
   `out/top_volume_wallets.csv`), so the reader can sanity-check that
   the cohort isn't e.g. a platform AMM or arbitrage bot that doesn't
   generalize.

## Look-ahead & survivorship — honest accounting

The headline run uses **expanding-window cohort selection with a single
cutoff at the dataset midpoint**; the signal is evaluated only on bars
at or after the cutoff. This means:

- No information from the test period is used to pick whales. ✓
- Wallets that stopped trading before the cutoff (plausibly blown-up
  accounts) are included in the training slice. They have a full P&L
  realized *at the cutoff*. Because of the inventory mark-to-last, they
  don't get a free lunch on remaining inventory, but this is still a
  noisy mark. ✗ for pure realism.
- Wallets whose best trades all fall in the test period aren't
  discoverable. This is fine: we are testing whether *past-smart*
  wallets keep being smart.

The `--rolling` flag recomputes the whale cohort expanding-through-time
per calendar day of the test slice. This is leakage-free but much
slower. The current implementation falls back to the single-cutoff
version inside `run()`; extending `run()` to iterate `pick_whales` per
day is a straightforward change but was not executed here because no
data was available to measure the cost.

## Known issues / TODOs on the code itself

- `_rank` uses ordinal ranking instead of average-rank-on-ties. On
  mostly-unique floats (VWAPs, signed flows) this is fine; if real data
  has many tied zeros in `whale_flow`, swap to `scipy.stats.rankdata`.
- The wall-clock forward join uses `tolerance = 1×bucket`, so a bar
  that lacks a successor within one bucket is dropped. That is
  intentional (prevents measuring "return" over a 3-day gap for a
  sparse market) but it does shrink n on illiquid markets. An
  alternative is to forward-fill VWAP 1–2 bars before joining; this is
  a single-line change and is worth A/B-testing on real data.
- Fees are subtracted as a constant round-trip; Polymarket effective
  costs vary by market liquidity. A real fee model would scale
  `fee ≈ spread/2 + taker_fee` per market and per bar volume.
- We do not model position sizing or capital constraint. The backtest
  treats each gated `(market, bar)` cell as one independent trade of
  unit notional. A portfolio-level backtest (cap per-day gross, cap
  per-market exposure) is the natural next step.

## Bottom line

Pending a real run: the framework is in place, the code runs, the
synthetic-data sanity check produces the expected null result, and the
file layout is:

- `README.md` — hypothesis & method
- `analyze.py` — runnable end-to-end
- `findings.md` — this file; to be updated with real numbers once
  `processed/trades.csv` is available.
