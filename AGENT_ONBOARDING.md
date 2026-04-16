# Agent Onboarding — Read In Order

You are operating a real Polymarket wallet with real capital. Before firing any
order, read these in the order listed. Do not skip.

---

## Reading list (sequence matters)

### 1. `CLAUDE.md` — first 5 minutes
Commands, architecture, pipeline stages. Tells you how the repo works.
**Key signal**: if this session involves trading, it will point you to this file.

### 2. `PLAYBOOK.md` — the constitution (10-15 min read)
THE rules. 15+ lessons from actual losses and wins.
**Every rule here exists because violating it cost money.** Take them literally.

If you only read one document, read this one.

### 3. `WEATHER_QUANT_README.md` — module map
What each file does, how the engine is wired, how to run it.

### 4. `weather/cities.py::STATION_OFFSET_C` — calibration
Confirmed per-city offsets between Open-Meteo forecast and Polymarket oracle.
**Apply these BEFORE computing cushion. Non-negotiable.**

### 5. Your wallet-specific state file (if one exists)
e.g. `WALLET_2_STATE.md`, `WALLET_3_STATE.md`. Starting capital, sizing caps,
any per-wallet caveats.

---

## Rules encoded in code (automatic — cannot override without code changes)

| Rule | Enforcement |
|---|---|
| `only_directional=true` default | `dashboard/runtime/weather_trader_config.json` |
| Bucket markets (op="in") hard-rejected | `trader.py::decide_side` line 127 |
| Skip today-resolving markets | `trader.py::decide_side` line 129 |
| `max_days_out=1` (day+2 needs ≥40pp edge) | `trader.py::decide_side` |
| `kelly_cap=0.25` × confidence | `trader.py::kelly_fraction` |
| Station offset applied pre-edge | `dump.py` via `cities.py` |
| Smoothed p_event (1.5°F gaussian noise) | `model.py::compute_p_event` |
| Fresh-validate before fire | `trader.py::fresh_forecast_sanity` |
| Stale-skip if forecast drifts >3°F | trader loop |

The code enforces the minimum discipline. You **cannot** accidentally fire
day+3 bets, bucket markets, or sub-cushion markets through the autonomous path.

---

## Rules that require YOUR discipline (not in code)

Manual fires can bypass the code checks. These rules apply to manual fires:

### 🔴 Rule 0 — Honesty over excitement
If you catch yourself feeling "excited" about a bet, re-verify the cushion.
When asked for a take, give the **honest probability**, not the rosy one.

### 🔴 Rule 14 — Coastal NO buckets need ≥5°F cushion (not 3°F)
Tropical cities (HK, Singapore, Miami, Bangkok) have afternoon convective
thermals. Cold-marine cities (Istanbul, SF, Seattle) have inflow patterns
ensembles miss. 3°F cushion FAILED on 2026-04-16 — HK and Istanbul lost $450.
Inland (Atlanta, Chicago, Denver, Paris, Madrid, Moscow) still 3°F OK.

### 🔴 Rule 15 — Istanbul SKIP unless cushion >7°F
Bosphorus cold inflow produces forecast errors of 3-5°C. Skip Istanbul bets
entirely unless the cushion is extreme.

### 🔴 Rule 4 — Day+1 priority for capital velocity
Day+2 bets lock cash for 48hr. Day+1 bets recycle in 24hr. At similar ROI,
day+1 compounds 2x faster. The code enforces this for autonomous fires; you
must enforce it for manual ones.

### 🔴 Rule 6 — Position correlation cap
Don't deploy more than ~$500 on a single (city, date) pair. And treat adjacent
dates on the same city as CORRELATED (Istanbul Apr 15 + Apr 16 NO = one
thesis, not two).

### 🔴 Rule 9 — User intuition counts
If the user reports their weather app differing from our model by ≥2°F,
treat it as new information. May require partial exit or skip.

### 🔴 Rule 10 — Git safety (MANDATORY before push)
```bash
git diff --cached | grep -iE "POLY_PRIVATE_KEY|API_SECRET|API_PASSPHRASE|PASSWORD"
git diff --cached | grep -oE "0x[a-fA-F0-9]{40}" | sort -u
```
Block push if unexpected. All new repos PRIVATE by default.

---

## First 24 hours — do this

1. **Read PLAYBOOK.md fully.** No exceptions.
2. `uv run python -m weather.dump` — populate edge_table.
3. `uv run python -m weather.positions` — see current book.
4. `uv run python -m weather.reconcile` — see what's resolved + calibration.
5. **Watch the autonomous trader.** Tail `dashboard/runtime/weather_trader.log`
   and read every `[BUY]` / `[EDGE]` / `[REFRESH]` line from the last 48hr.
   Understand what patterns the filter catches.
6. **Do NOT manual-fire for the first 24 hours.** Let the autonomous trader
   teach you the edge filter. Match its patterns.
7. Graduate to manual fires only after:
   - 5+ resolutions with positive calibration
   - You can correctly predict which candidates the autonomous filter will
     reject before running it
   - You've re-read PLAYBOOK.md once more

---

## Workflow patterns (copy these directly)

### "Find me bets"
1. Run `weather.dump` if CSV is >30 min old
2. Filter for today+1 (tomorrow) resolution
3. Apply `STATION_OFFSET_C` from `cities.py`
4. Filter **inland cushion ≥3°F, coastal cushion ≥5°F** (Rule 14)
5. Filter ask $0.30-$0.78, depth ≥$50
6. Skip cities/dates already in the book
7. **Fresh-validate every candidate** with `fetch_open_meteo_forecast` before
   surfacing it. Do NOT trust CSV alone (Toronto lesson).
8. Surface top 3-5, ranked by cushion
9. Size by Kelly × confidence, capped at max_position_usd

### "Should I sell X?"
1. Pull current market price via `clob.polymarket.com/book`
2. Compute fresh post-offset win probability
3. If true win prob ≥70% AND market price ≥80%: **HOLD** (taking profit
   early loses EV)
4. If true win prob dropped to <50% due to NEW INFO: **EXIT**
5. If user has emotional/derisk reason: respect it, propose partial exit

### "Status / position review"
1. `uv run python -m weather.positions`
2. Group by tier (≥90% near-cert, 70-90% strong, 50-70% coinflip, <50% loser)
3. Sum cost vs MtM, project settlement
4. **Flag concentration risk proactively** — don't wait for user to ask

---

## The deeper reading

Once you've operated for a day and want to understand WHY the rules exist:

```bash
# Every lesson was added in a commit — see the context behind each
git log -p PLAYBOOK.md

# Evolution of the trading engine
git log --oneline weather/

# Every past resolution with our_p vs observed outcome
cat weather/reconciled.csv | column -t -s,
```

---

## Personality / style

- **Be terse.** The user reads diffs and tail logs — they don't need essays.
- **When firing**: status (matched/delayed/live) + size + payout if wins. One
  line per bet.
- **Flag risks proactively.** Don't wait for the user to spot them.
- **Concede when wrong FAST.** If you're about to violate a rule, say so and
  offer the rule-compliant alternative.
- **Celebrate wins briefly.** User knows they made money. Get back to scanning.
- **Markdown tables** for position reviews. Emoji sparingly: 🟢 strong /
  🟡 coinflip / 🔴 loser.

---

## The test of this onboarding

If you can read these docs + PLAYBOOK.md and fire a correct first trade
within 10 minutes of starting, the onboarding worked. If you burn the first
day on the same mistakes wallet #1 made (bucket markets, no offset, day+3
bets), this doc wasn't clear enough — improve it for wallet #N+1.

Good luck. The edge is real, the discipline is everything.
