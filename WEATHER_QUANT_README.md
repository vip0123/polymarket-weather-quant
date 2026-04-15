# Polymarket Weather Quant

A calibrated weather-prediction trading engine for Polymarket. Uses Open-Meteo ensemble forecasts + station-offset calibration + Kelly sizing to find and fire on mispriced weather markets.

## What's included

**Working quants** — `weather/` package
- `dump.py` — scrape Polymarket weather markets, compute P(event) via 143-member ensemble
- `trader.py` — autonomous fires with edge + cushion + Kelly × confidence checks
- `model.py` — gaussian-smoothed probability with ensemble quantiles
- `cities.py` — city registry + learned station offsets (`STATION_OFFSET_C`)
- `parser.py` — structured parser for "be X°C" / "≥X°F" / "between X-Y" markets
- `sources.py` — Open-Meteo ensemble + single-best fetchers
- `intraday.py` — hourly-obs override for same-day markets
- `nws.py` — NWS cross-check confidence gating (US cities)
- `reconcile.py` — post-resolution P&L + calibration table
- `positions.py` — live portfolio CLI
- `feed.py` — unified buy/edge stream viewer

**Copy engine** — `dashboard/engine.py` — optional, mirrors a target Polymarket wallet. Configured via `dashboard/runtime/copy_config.json`.

**Supervisor** — `dashboard/supervise_weather.sh` — bash loop that keeps trader alive.

## Setup on a fresh machine

```bash
# 1. Clone
git clone <your-private-repo-url>
cd poly_data

# 2. Python deps
uv sync

# 3. Generate a new Polymarket wallet (via Polymarket UI)
#    — fund with USDC on Polygon
#    — generate CLOB API creds via the Polymarket account settings or py_clob_client

# 4. Copy env template and fill in
cp .env.example .env
# edit .env with YOUR_NEW_WALLET's keys

# 5. Seed the runtime configs (start disabled for safety)
mkdir -p dashboard/runtime
cp dashboard/runtime_examples/weather_trader_config.example.json \
   dashboard/runtime/weather_trader_config.json
cp dashboard/runtime_examples/copy_config.example.json \
   dashboard/runtime/copy_config.json

# 6. Test the stack in dry mode
uv run python -m weather.dump           # generates edge_table.csv
uv run python -m weather.positions      # shows (empty) portfolio
uv run python -m weather.trader         # starts scanner in dry mode

# 7. Enable live trading
# Set enabled=true + dry_run=false in weather_trader_config.json.
# Start under supervisor:
chmod +x dashboard/supervise_weather.sh
nohup ./dashboard/supervise_weather.sh > dashboard/runtime/supervisor_weather.log 2>&1 &
```

## Per-wallet state (NEVER commit)

Each wallet keeps its own:
- `.env` — private key + CLOB creds
- `dashboard/runtime/*_state.json` — in-memory fired dicts
- `dashboard/runtime/*_trades.csv` — trade history
- `dashboard/runtime/*.log` — engine logs
- `weather/edge_table.csv` — derived but wallet-agnostic (safe to share but tiny)
- `weather/reconciled.csv` — per-wallet P&L

All wallet-specific state is excluded via `.gitignore`. Only code + config examples get pushed.

## Station offsets (calibration data)

`weather/cities.py` has `STATION_OFFSET_C` — confirmed/hypothesized temperature biases between Open-Meteo's city-center coords and each city's Polymarket oracle station. Updated as reconciliation produces real data. Seeded with:
- Seoul: −1.8°C (confirmed 2026-04-15 via Seoul ≥21°C resolution)
- LA: −2.8°C (hypothesis)
- Placeholders for Miami, SF, Seattle, Boston, HK, Singapore, Tokyo, Sydney

New machine starts with the same seed values. Reconciliation at both machines will refine independently — you can merge offsets periodically.

## Deprecated (do not run)

These lost money on the first day and are kept only for reference:
- `dashboard/sniper_engine.py`
- `dashboard/flip_engine.py`
- `dashboard/quant_engine.py` (v1)
- `dashboard/quant_v3_engine.py`
- `dashboard/ta_engine.py`

Their supervisor scripts are similarly obsolete.

## Lessons banked (check `weather/cities.py` + comments)

1. **Never fire single-degree buckets** — Polymarket uses integer-rounding, cushion is always ≤0.5°F
2. **Apply station offset** before computing edge — coastal airports run 1.5-3°C cooler than city-center grids
3. **Use median, not mean** — ensemble mean gets pulled by outlier members
4. **Pre-fire stale-forecast check** — re-fetch single-best to verify edge_table isn't old
5. **Prefer same-day/next-day resolutions** — capital velocity matters
6. **Coastal airports need +2°F extra cushion** — sea breeze caps afternoon warming
7. **Gaussian-smoothed p_event** — each ensemble member has ~1.5°F own-model error; smooth probs near threshold

## Operational notes

- `caffeinate -d &` keeps Mac awake to prevent engine pausing
- Supervisor auto-restarts on crash (no circuit breaker — watch P&L manually)
- 15-city limit for autonomous fires (edit `max_open_positions` in config)
- Daily reconciliation: `uv run python -m weather.reconcile`
