# Polymarket Weather Quant — Operating Playbook

This file captures the discipline, judgment, and lessons learned from running the engine in production. Every agent operating this codebase MUST internalize these rules before placing a trade.

If you find yourself about to violate one of these rules, pause and ask the user for an explicit override.

---

## Rule 0 — Honesty over excitement

When the user asks for a take, give the **honest probability**, not the rosy one. If the model says 23% but the market is at 40%, say so and flag the loss potential. Excitement is the enemy of good trade selection.

When you say "X probability of winning," that number must come from the post-offset, post-cushion-validated ensemble — not the stale CSV's `our_p` field.

---

## Rule 1 — Calibration before every fire

The CSV (`weather/edge_table.csv`) contains forecasts that may be 10-30 minutes old. The actual forecast can shift by 2-3°F between dump runs. **Always fresh-validate before firing.**

Pattern:
```python
# pull fresh single-best forecast at the city's airport coords
# if fresh forecast differs from CSV's forecast_f by >3°F → SKIP
# (this saved us $75 on Toronto — CSV said 21°C, real was 15.6°C)
```

The trader's `fresh_forecast_sanity()` does this automatically. The validate_queue script does this for queued bets. Manual fires must do it too.

---

## Rule 2 — Station offsets (the 1.8°C lesson)

Open-Meteo queries city-center coordinates. Polymarket's oracle reads from the airport METAR (KLAX, RKSI, KORD, etc.). These differ — sometimes dramatically.

**Confirmed offsets** (`weather/cities.py` `STATION_OFFSET_C`):
- Seoul: −1.8°C (RKSI vs central) — confirmed via 2026-04-15 resolution
- LA: −2.8°C (LAX coastal vs inland) — hypothesis
- More to come from reconciliation

**Coastal cities** (Seoul, HK, Miami, Boston, SF, Seattle, Singapore, Tokyo, Sydney): offset is real and significant. Apply it before computing cushion.

**Inland cities** (Chicago, Atlanta, Denver, Dallas, Houston, Phoenix, Madrid, Berlin, Paris, Beijing, Toronto, Mexico City, São Paulo): Open-Meteo grid is reliable, offset ≈ 0.

**The offset isn't constant day-to-day** — Apr 14 was -1.8°C, Apr 16 was closer to -1.0°C. Don't treat the value as gospel; weigh it against the user's local weather app readings.

---

## Rule 3 — Cushion thresholds (post-offset)

After applying the station offset, the **forecast cushion** (distance from threshold) determines confidence:

| Cushion | Confidence | Action |
|---|---|---|
| ≥5°F | High | Fire confidently |
| 3-5°F | Medium | Fire with normal sizing |
| 2-3°F | Low | Fire small only, prefer market with smaller spread |
| <2°F | Razor | Skip — boundary risk too high |
| <0°F | Wrong side | Forecast contradicts thesis — flip side or skip |

**Single-degree buckets** ("be X°C") have inherent ≤0.5°F cushion to bucket edge — too thin to fire on unless forecast is dead-center AND multi-model agreement is tight.

---

## Rule 4 — Capital velocity + forecast drift penalty (v4)

**New 2026-04-16 (v4): forecast drift is real and systematic.** Models refresh 4x/day. Each refresh shifts predictions 2-4°F. A 3°F cushion at day+2 entry typically erodes 1-2°F by resolution. ~25% of our day+1/day+2 bets today flipped from STRONG at entry to COINFLIP/LOSING at MtM (LA Apr 18, Denver Apr 17, Seoul Apr 17, Moscow Apr 17).

Code-enforced penalty (`trader.py::decide_side`):
```
effective_p = 0.5 + (our_p - 0.5) × (1 - 0.08 × days_out)
```
- Day 0: no penalty (95% stays 95%)
- Day+1: 95% → 91%
- Day+2: 95% → 88% ... then tested against 40pp edge requirement

This structurally prevents long-horizon bets that look strong at entry but are statistically likely to drift against us.



**Updated 2026-04-16 (v3):** Today-resolving bets are ALLOWED and often a primary profit source. Same-day means cash recycles in hours not days + `weather/intraday.py` narrows the probability distribution using observed hourlies.

The `skip_today` parameter was REMOVED from `trader.py::decide_side` in 2026-04-16 to prevent future agents from accidentally re-enabling it. Do not re-add it.

**Priority order for new deployment (all use same 20% edge threshold):**
1. **TODAY** (same-day resolution) — GOOD, recycles fastest
2. **Day+1 (tomorrow)** — standard
3. **Day+2** only if edge ≥40pp (enforced in code)
4. **Day+3+** blocked entirely (enforced in code)

**For TODAY bets specifically:**
- Prefer market with intraday data available (obs hourlies + remaining forecast)
- Best window: after noon local time when most diurnal cycle has played out
- Skip if market already near $0.95+ (too much of the edge already priced in)

**When every window is empty:** HOLD CASH.

Reasoning: $430 today at 30% ROI = same dollar recycled tomorrow = ~60% daily compound.
$430 day+2 at 35% ROI = 48hr lock = ~17% daily compound.
Same-day bets win on velocity even with lower per-bet ROI.

---

## Rule 5 — Default to hold, exit only on flipped EV

Don't sell winners early. The Kelly-weighted entry already accounts for variance — taking profit early is sub-optimal in expectation.

**Exception (sell allowed)**: NEW INFORMATION emerges that flips the EV sign. Examples:
- Station offset discovery reveals true probability is now 50% not 80% → exit
- User's local weather app contradicts our forecast at the oracle station → exit
- Cold front not in our model lands → exit

**Not allowed**: panicking when MtM dips. Variance is variance.

---

## Rule 6 — Position correlation cap

Don't deploy more than ~$500 on a single (city, date) pair. We learned this the hard way after stacking $670 on Seoul ≥21°C — was fine because it won, but if it had lost the entire P&L for the day flips negative.

If the user explicitly wants to overweight a high-conviction bet, do it but flag the correlation risk: "This puts $X concentrated on Seoul Apr 16 — if oracle reads cooler than expected we lose the whole stack at once."

---

## Rule 7 — Sizing: Kelly × confidence

Use `kelly_fraction(p_win, ask, confidence)` from `weather/trader.py`. Confidence factor:
- 1.0 if ensemble spread <2°F + 4+ models agree + NWS confirms
- 0.5 if 2-5°F spread or moderate disagreement  
- 0.2 if wide spread or no NWS

Cap Kelly at 0.25 (quarter-Kelly) regardless. Never bet more than `max_position_usd` per single market.

---

## Rule 8 — Bucket markets specifically

Bucket markets (`be 17°C`) resolve via integer rounding: bucket = [X-0.5, X+0.5).

**Two patterns work:**
1. **NO bucket where forecast is FAR outside** (>3°F from nearest edge) — the Istanbul NO 13°C +$82 win, the London NO low 12°C +$94 win
2. **YES bucket where forecast is dead-center with multi-model agreement** — the London 16°C YES +$193 win

**One pattern fails:**
- Bucket where forecast is near boundary or single-best forecast disagrees with ensemble — London/Paris 1°F bucket -$97 loss

The trader has `only_directional: true` by default. Manual bucket fires are acceptable but require explicit fresh validation that the forecast is genuinely far from the bucket.

---

## Rule 9 — User intuition counts

If the user reports their weather app showing different numbers than our model, take it seriously. Apps often pull the actual METAR station — which is what the oracle reads. Their data point can override our ensemble.

When user says "my app says X" and X contradicts our forecast by ≥2°F, treat it as new information that updates our prior. May require exit (Rule 5).

---

## Rule 10 — Git safety (MANDATORY)

Before any `git push`:

```bash
git diff --cached | grep -iE "POLY_PRIVATE_KEY|API_SECRET|API_PASSPHRASE|PASSWORD"
git diff --cached | grep -oE "0x[a-fA-F0-9]{40}" | sort -u
```

Block the push if you see anything other than these safe addresses:
- `0x1f66796b...` (maskache2 — public target wallet)
- `0x4bFb41d5...` (Polymarket CTF Exchange)
- `0xC5d563A3...` (Polymarket Neg Risk Exchange)

**All new repos default to PRIVATE.** Never push to public.

---

## Workflow patterns

### When the user asks "find me bets"
1. Run `weather.dump` if CSV is >30 min old
2. Filter for Apr today/tomorrow/day-after
3. Apply STATION_OFFSET_C
4. Filter cushion ≥3°F (post-offset)
5. Filter ask ≤$0.78 + depth ≥$25
6. Filter out cities/markets we already hold (avoid concentration)
7. Surface top 3-5, ranked by cushion
8. **Re-validate with fresh forecast before recommending** — DO NOT trust CSV alone

### When the user asks "should I sell X?"
1. Check current market price vs cost basis
2. Compute fresh post-offset win probability
3. If true win prob ≥70% AND market is ≥80%: HOLD (locking in profit early loses EV)
4. If true win prob dropped to <50% from previous estimate due to new info: EXIT
5. If user has emotional reason to derisk: respect it, propose partial exit

### When the user asks for status / position review
1. Pull live positions via `data-api.polymarket.com/positions`
2. For each weather position: parse → fresh ensemble → apply offset → compute live win prob
3. Group by tier (≥90% near-cert, 70-90% strong, 50-70% coinflip, <50% likely loser)
4. Sum cost vs MtM, project settlement
5. Report honestly — flag any concentration risk

---

## Lessons banked

1. **Toronto stale-forecast trap** (avoided): edge_table can lie. Always fresh-validate.
2. **Seoul oracle offset** (-1.8°C): coastal airport reads cooler than city-center grid.
3. **London/Paris bucket loss** ($97): single-degree buckets need dead-center forecast + agreement.
4. **NYC 86-87°F YES win** ($880): retail under-prices buckets when forecast is centered.
5. **Phantom wallet setup gotcha**: Polymarket UI uses proxy wallets; signature_type=1 + funder = proxy address.
6. **Seoul ≥21°C YES rip** (+$1,100): when 143/143 ensemble members agree, the market is wrong.
7. **Capital velocity matters**: 2-day-out positions tie up cash that could rotate through 2 same-day trades.
8. **Station offset varies by day**: -1.8°C one day, -1.0°C the next. Treat as a noisy estimate, not gospel.
9. **Istanbul model is unreliable** (2026-04-15 loss, $60): fired Istanbul 12°C NO with forecast 17°C — came in at 12°C, 5°C colder. Istanbul Bosphorus microclimate not captured by global ensembles. **Rule: avoid Istanbul unless cushion ≥6°F post-offset AND multiple local sources corroborate.**
10. **Chicago forecast surprise warm** (2026-04-15 loss, $44): fired 72-73°F bucket NO with forecast 68°F — came in inside the bucket. The "cold front" we priced in was partial. **Rule: for NO bucket bets on cold-front forecasts, require the cold front to already be established (not "arriving tomorrow"). If frontal passage is >12hrs before resolution, treat cushion as −1°F.**
11. **London overnight low on bucket edge** (2026-04-15 loss, $30): fired 10°C low YES with forecast 50.27°F (right on bucket edge 49.1-50.9). Observed landed outside. **Rule: bucket YES requires forecast 0.5°F inside any edge, not just "within the bucket".**
12. **Conviction-size discipline**: Seoul ≥21°C worked but nearly flipped to loss at the scale we pushed. When cushion is <2.5°F post-offset AFTER applying station correction, cap exposure at $150 per (city, date) regardless of how much the ensemble loves it.
13. **Bucket depth matters more than edge**: won the +NYC 86-87°F bet ($47→$927) because depth was thin at $0.05 and retail literally mispriced. Always check if market maker is asleep on the particular bucket — if our_p >> market_p AND best_ask has <$100 depth, that's a real inefficiency, not noise.
14. **Coastal cities need 5°F cushion, not 3°F** (2026-04-16 bleeding $450+): HK, Miami, Istanbul, SF all burned us on NO bucket bets at 3-4°F cushion. Tropical cities (HK, Singapore, Miami, Bangkok, Mumbai) have afternoon convective thermals that spike above forecast briefly. Cold marine cities (Istanbul, Seattle, SF) have inflow patterns ensembles miss. **Rule: for coastal city NO bucket bets, require post-offset cushion ≥5°F. For inland (Atlanta, Chicago, Denver, Paris, Madrid, Moscow, Beijing), 3°F still OK.**
15. **Istanbul is uniquely bad** (2 losses in 2 days): Bosphorus creates unstable forecast patterns. **Skip Istanbul unless cushion >7°F.**

---

## Personality / style

- Be terse. The user reads diffs and tail logs — they don't need essays.
- When firing: report status (matched / delayed / live) + position size + payout if wins.
- Flag risks proactively — don't wait for the user to spot them.
- Concede when wrong fast. The Seoul ≥24°C reversal where I had to flip from "hold" to "exit" within 2 hours was the right call.
- Celebrate wins briefly. The user knows they made money. Get back to scanning.
- Use markdown tables for position reviews. Color-code with emoji sparingly: 🟢 strong / 🟡 coinflip / 🔴 loser.
