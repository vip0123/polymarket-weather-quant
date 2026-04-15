# Disposition Effect Signal

## Hypothesis

Retail traders exhibit the **disposition effect** (Shefrin & Statman 1985;
Odean 1998): they realize gains faster than they realize losses. On Polymarket
this should manifest as:

1. **Selling winners too early** — closing profitable outcome-token positions
   well before resolution, leaving money on the table when the winner ultimately
   pays 1.0.
2. **Riding losers too long** — refusing to close losing positions, which on
   Polymarket can eventually go to 0 at resolution (or require a large,
   distressed capitulation sell).

If a cohort of wallets displays stable disposition bias, their sells contain
information. Specifically:

- When they sell a winner, the position is likely *under*-priced relative to
  the terminal payoff — **fade the sell** (be the buyer).
- When they finally sell a loser, the position is likely *over*-priced in the
  immediate aftermath of a capitulation — **don't fade**; if anything, join
  them.

## Measuring disposition per wallet

We follow Odean (1998)'s event-level PGR/PLR construction.

For each wallet (`maker` column), maintain FIFO lots per position key. The
position key is:

- In the processed-trade form: `(market_id, nonusdc_side)` (i.e.
  one outcome token per market).
- In the raw-event form: `asset_id` — which is equivalent because an asset_id
  uniquely identifies one outcome token.

Each trade event from that wallet either:

- **BUY**: append a `Lot(qty, price)` to the FIFO queue for that asset.
- **SELL**: FIFO-close lots. Compute `realized_pnl = sell_proceeds - cost_basis`
  on the closed quantity. This sell is a "realization event."

At every realization event we increment counters:

```
if realized_pnl > 0: realized_gains  += 1
if realized_pnl < 0: realized_losses += 1

for every OTHER asset still open in this wallet:
    avg_cost_per_token = sum(lot.qty * lot.price) / sum(lot.qty)
    paper_pnl_per_token = current_price - avg_cost_per_token
    if paper_pnl_per_token > 0: paper_gains  += 1
    if paper_pnl_per_token < 0: paper_losses += 1
```

`current_price` for an asset is taken as the last observed trade price on that
asset across the whole dataset up to this moment (we use an online map that
updates on every trade).

**Disposition metrics** (per wallet):

```
PGR = realized_gains  / (realized_gains  + paper_gains )
PLR = realized_losses / (realized_losses + paper_losses)
Disposition score = PGR − PLR
```

Classic disposition bias → `PGR > PLR` → score **> 0**. Rationally-playing
Kelly traders or momentum/"letting winners run, cutting losers" types should
score **≤ 0**.

We require at least `MIN_CLOSED_TRADES = 50` sell-event realizations per wallet
to rank it, killing noise from one-shot bettors.

## Turning it into a tradable signal

Two legs, both evaluated in `fade_backtest.csv`:

1. **`fade_winner_sell`**: top-disposition cohort sells a winner → we *buy* at
   that price, hold to final observed price. Expected edge positive if the sell
   is early.
2. **`follow_loser_sell`**: top-disposition cohort finally capitulates on a
   loser → we *sell* at that price, effectively neutral but tracked for
   symmetry. Expected edge should *not* be positive (if disposition sellers are
   informed on their losers, which would contradict the theory).

We restrict backtest to assets where `final_price` is within
`RESOLVED_TOLERANCE = 0.05` of 0 or 1 — a rough filter for "market has
essentially resolved in the dataset." Forward return is
`(final_price − entry_price) / entry_price`.

Reported metrics per leg:

- `n` — sample size
- `hit_rate` — fraction of trades with positive forward return
- `avg_return` — equal-weighted mean forward return
- `notional_weighted_return` — weighted by `qty × entry_price`
- `median_return`

## Files

- `analyze.py` — canonical script, reads `processed/trades.csv` (the pipeline's
  standard output) and writes all artefacts to `output/`.
- `stream_raw.py` — equivalent analysis directly off the raw orderFilled
  stream, so you can run it without materializing `trades.csv` on disk
  (`curl … | xz -dc | python stream_raw.py --input -`). Uses `asset_id` as the
  position key.
- `output/disposition_leaderboard.csv` — per-wallet PGR/PLR, ranked by
  disposition score.
- `output/closed_trades.csv` — every realized-close event for leaderboard
  wallets.
- `output/fade_backtest.csv` — per-event forward-return simulation.
- `output/summary.json` — aggregate numbers (sample sizes, hit rate,
  avg return) referenced by `findings.md`.

## Caveats / honest limits

- **No true market-resolution data.** We proxy "winner at close" with the last
  observed trade price. Markets that never traded near 0/1 (cancelled, thinly
  resolved via claims, or still-open near-term markets) are excluded. A proper
  version would join on `markets.csv` `closedTime`/resolution.
- **Odean counters are event-weighted, not dollar-weighted.** A $5 dust sell
  and a $50k position close each count once. This mirrors Odean but is not
  economic-significance-weighted.
- **Paper gains/losses use the global last trade price.** This undercounts
  stale positions in rarely-traded markets. A finer proxy is "price at the time
  of the realization event" using a sorted history per asset; the current
  implementation is a streaming approximation.
- **Maker/taker asymmetry.** We filter on `maker` per CLAUDE.md; that is the
  complete set of a user's trades. On the raw-event side we likewise use the
  maker column.
- **Naked shorts** (sell with no open lot) are skipped, not shorted. Polymarket
  doesn't really support outcome-token shorts outside of buying the opposite
  side; skipping these events avoids pathologies.
