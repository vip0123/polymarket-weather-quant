# Polymarket Weather Quant

A calibrated weather-prediction trading engine for Polymarket, combined with a full data pipeline for market and trade data collection. Uses Open-Meteo ensemble forecasts, station-offset calibration, and Kelly sizing to find and trade mispriced weather markets.

## Quick Download (data pipeline)

**First-time users**: Download the [latest data snapshot](https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz) (Credits to [@PendulumFlow](https://x.com/PendulumFlow)) and extract it in the repo root before your first run [(backup)](https://polydata-archive.s3.us-east-1.amazonaws.com/archive.tar.xz). This saves 2+ days of backfill time.

## Overview

Three-stage data pipeline plus an autonomous weather trading engine:

1. **Market Data Collection** — fetches all Polymarket markets with metadata
2. **Order Event Scraping** — collects order-filled events from Goldsky subgraph
3. **Trade Processing** — transforms raw events into structured trade data
4. **Weather Trader** — scans weather markets, computes edge, places Kelly-sized orders

## Installation

This project uses [UV](https://docs.astral.sh/uv/) for package management.

```powershell
# Install UV (Windows)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Install dependencies
uv sync

# With Jupyter/notebook support
uv sync --extra dev
```

## Wallet Setup (MetaMask — recommended)

Polymarket uses a **Gnosis Safe** automatically deployed for each MetaMask account. The Safe is your trading wallet (holds USDC, shown on polymarket.com). Your MetaMask EOA is the Safe owner and signer.

### 1. Export your MetaMask private key

MetaMask → account menu → **Account details** → **Show private key**

Verify it resolves to the correct address:
```powershell
uv run python -c "from eth_account import Account; print(Account.from_key('0xYOUR_KEY').address)"
# Should match the owner shown at https://app.safe.global for your Polymarket Safe
```

To find which address owns your Polymarket Safe (`0xYOUR_SAFE_ADDRESS`):
```powershell
uv run python check_contract.py   # reads POLY_WALLET_ADDRESS from .env
```

### 2. Create the `.env` file

```ini
# CLOB API credentials (generate via: uv run python create_api_key_cffi.py)
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=

# MetaMask private key (Safe owner/signer)
POLY_PRIVATE_KEY=0xYOUR_METAMASK_PRIVATE_KEY

# Gnosis Safe address (shown on polymarket.com — your trading wallet)
POLY_WALLET_ADDRESS=0xYOUR_SAFE_ADDRESS
POLY_FUNDER=0xYOUR_SAFE_ADDRESS   # same as POLY_WALLET_ADDRESS for MetaMask users
POLY_SIGNATURE_TYPE=2             # 2 = Gnosis Safe

# Proxy (required if your region is geo-blocked by Polymarket)
# Format: http://user:pass@host:port
POLY_PROXY_URL=

# Copy engine target (optional)
COPY_TARGET_WALLET=
```

> **Signature types**: `0` = EOA (MetaMask direct), `1` = Magic Link proxy, `2` = Gnosis Safe (MetaMask via Polymarket). Most MetaMask users are type `2`.

### 3. Generate CLOB API credentials

```powershell
uv run python create_api_key_cffi.py
```

This creates a new API key bound to your MetaMask EOA and writes it directly to `.env`. Uses `curl_cffi` with Cloudflare impersonation — works even without a proxy.

### 4. Verify the full stack

```powershell
uv run python test_proxy.py
# Expected: "✅ SUCCESS — proxy + L2 auth working!"
```

### 5. Seed runtime configs

```powershell
New-Item -ItemType Directory -Force -Path dashboard\runtime
Copy-Item dashboard\runtime_examples\weather_trader_config.example.json dashboard\runtime\weather_trader_config.json
Copy-Item dashboard\runtime_examples\copy_config.example.json dashboard\runtime\copy_config.json
```

Edit `dashboard/runtime/weather_trader_config.json`:
- `"enabled": false` and `"dry_run": true` to start
- Set `"enabled": true, "dry_run": false` when ready to trade live

### 6. Start the weather trader

**Windows (PowerShell):**
```powershell
# In a dedicated PowerShell window — auto-restarts on crash
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\dashboard\supervise_weather.ps1
```

**Linux/macOS (background shell loop):**
```bash
nohup ./dashboard/supervise_weather.sh > dashboard/runtime/supervisor_weather.log 2>&1 &
```

**Linux (systemd user service — recommended for servers):**
```bash
# 1. Install UV
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and install deps (with dashboard extras)
git clone https://github.com/kevinbadi/polymarket-weather-quant.git ~/polymarket-weather-quant
cd ~/polymarket-weather-quant
~/.local/bin/uv sync --extra dashboard

# 3. Copy your .env from local machine
# From Windows: scp .env user@server:~/polymarket-weather-quant/.env
# From Linux:   scp .env user@server:~/polymarket-weather-quant/.env

# 4. Seed runtime configs
mkdir -p dashboard/runtime
cp dashboard/runtime_examples/weather_trader_config.example.json dashboard/runtime/weather_trader_config.json

# 5. Create the systemd user service
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/weather-trader.service << 'EOF'
[Unit]
Description=Polymarket Weather Trader
After=network-online.target

[Service]
WorkingDirectory=/home/YOUR_USER/polymarket-weather-quant
ExecStart=/home/YOUR_USER/.local/bin/uv run python -m weather.trader
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Replace YOUR_USER with your actual username, then:
systemctl --user daemon-reload
systemctl --user enable weather-trader
systemctl --user start weather-trader

# 6. Enable linger so it survives logout/reboot (requires sudo once)
sudo loginctl enable-linger $USER
```

## Data Pipeline

### Run the full pipeline

```powershell
uv run python update_all.py
```

### Run individual stages

```powershell
uv run python -c "from update_utils.update_markets import update_markets; update_markets()"
uv run python -c "from update_utils.update_goldsky import update_goldsky; update_goldsky()"
uv run python -c "from update_utils.process_live import process_live; process_live()"
```

### Fast parallel Goldsky backfill

```powershell
uv run python parallel_sync.py --workers 5
```

All pipeline stages are **idempotent and resumable** — re-running picks up from the last checkpoint.

## Project Structure

```
├── update_all.py                   # Full pipeline orchestrator
├── parallel_sync.py                # Parallel Goldsky backfill
├── update_utils/
│   ├── update_markets.py           # Fetch markets from Polymarket API
│   ├── update_goldsky.py           # Scrape order events from Goldsky
│   └── process_live.py             # Process orders into trades
├── poly_utils/
│   └── utils.py                    # get_markets(), PLATFORM_WALLETS
├── weather/
│   ├── trader.py                   # Autonomous trading engine
│   ├── model.py                    # Gaussian ensemble probability model
│   ├── cities.py                   # City registry + STATION_OFFSET_C calibration
│   ├── parser.py                   # Market title parser (°C / °F / range)
│   ├── sources.py                  # Open-Meteo ensemble fetcher
│   ├── intraday.py                 # Same-day hourly-obs override
│   ├── nws.py                      # NWS cross-check (US cities)
│   ├── positions.py                # Live portfolio CLI
│   └── feed.py                     # Buy/edge stream viewer
├── dashboard/
│   ├── supervise_weather.ps1       # Windows supervisor
│   ├── supervise_weather.sh        # Linux/macOS supervisor
│   └── runtime/                    # Per-wallet state (gitignored)
│       ├── weather_trader_config.json
│       ├── weather_trader_state.json
│       └── weather_trader_trades.csv
├── markets.csv                     # Market metadata (tracked)
├── goldsky/orderFilled.csv         # Raw events (auto-generated, large)
└── processed/trades.csv            # Structured trades (auto-generated)
```

## Per-wallet state (never commit)

- `.env` — private key + CLOB creds
- `dashboard/runtime/*_state.json` — in-memory position state
- `dashboard/runtime/*_trades.csv` — trade history
- `weather/edge_table.csv` — computed edge (wallet-agnostic, safe to share)

All wallet-specific files are excluded via `.gitignore`.

## Station offsets (calibration)

`weather/cities.py::STATION_OFFSET_C` holds learned temperature biases between Open-Meteo city-center coordinates and each city's Polymarket oracle station. **Always apply the offset before computing cushion.** Updated from real resolution data — see comments in the file.

## Key trading rules (see PLAYBOOK.md for full list)

1. Apply `STATION_OFFSET_C` before computing edge — coastal airports run 1.5–3 °C cooler than city grids
2. Never fire single-degree buckets — integer rounding means cushion ≤ 0.5 °F
3. Use ensemble median, not mean — outlier members skew the mean
4. Re-fetch forecast just before firing — stale edge tables lose money
5. Prefer same-day / next-day resolutions for capital velocity

## Geo-block

Polymarket blocks certain regions (US, UK, NL, etc.). Set `POLY_PROXY_URL` in `.env` to a residential or mobile proxy from an unblocked country. Test with:

```powershell
uv run python -c "
import httpx, os; from dotenv import load_dotenv; load_dotenv('.env')
proxy = os.environ['POLY_PROXY_URL']
with httpx.Client(proxy=proxy, timeout=15) as hc:
    print(hc.get('https://polymarket.com/api/geoblock').json())
"
```

`blocked: False` means you're good.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `invalid signature` | Wrong `POLY_FUNDER` or `POLY_SIGNATURE_TYPE` | MetaMask users: `POLY_SIGNATURE_TYPE=2`, `POLY_FUNDER=<Safe address>` |
| `Could not derive api key` | No key exists for this EOA at nonce 0-4 | Run `create_api_key_cffi.py` to create one |
| `not enough balance` | Wrong funder address or funds not deposited | Verify `POLY_FUNDER` matches the address shown on polymarket.com |
| `403 Cloudflare` on `/auth/api-key` | Plain httpx blocked by CF | Use `create_api_key_cffi.py` (curl_cffi with `impersonate='chrome'`) |
| `blocked: True` on geoblock | Region not supported | Set `POLY_PROXY_URL` to a proxy in LV, DE, etc. |
| `private key must be exactly 32 bytes` | Key missing `0x` prefix or wrong length | Ensure key is `0x` + 64 hex chars |

## Data files

### markets.csv
Market metadata: question, outcomes, tokens, close time, volume, condition ID, neg_risk flag.

**Fields**: `createdAt`, `id`, `question`, `answer1`, `answer2`, `neg_risk`, `market_slug`, `token1`, `token2`, `condition_id`, `volume`, `ticker`, `closedTime`

### goldsky/orderFilled.csv
Raw order-filled events: maker/taker addresses, asset IDs, fill amounts, tx hashes, timestamps.

**Fields**: `timestamp`, `maker`, `makerAssetId`, `makerAmountFilled`, `taker`, `takerAssetId`, `takerAmountFilled`, `transactionHash`

### processed/trades.csv
Structured trades: market mapping, BUY/SELL direction, price in USDC, amounts.

**Fields**: `timestamp`, `market_id`, `maker`, `taker`, `nonusdc_side`, `maker_direction`, `taker_direction`, `price`, `usd_amount`, `token_amount`, `transactionHash`

## Trade semantics

- Each fill pairs USDC (assetId `"0"`) with one outcome token.
- Raw amounts are 10⁶-scaled — divide before use.
- **Taker**: BUY if pays USDC, SELL if receives USDC. **Maker**: opposite.
- Filter on `maker` column to get a user's complete trade history (contract events are emitted from maker perspective).

## Analysis

```python
import polars as pl
from poly_utils import get_markets, PLATFORM_WALLETS

markets_df = get_markets()

df = pl.scan_csv("processed/trades.csv").collect(streaming=True)
df = df.with_columns(pl.col("timestamp").str.to_datetime())

# All trades for a specific wallet
my_trades = df.filter(pl.col("maker") == "0xYOUR_SAFE_ADDRESS")
```

## License

Go wild with it

## Monitoring (Ubuntu server)

All commands run on `192.168.1.100` as user `polymarket`.

### Check if the trader is running

```bash
ssh polymarket@192.168.1.100 "systemctl --user status weather-trader --no-pager"
```

Expected: `Active: active (running)` with the trader PID visible.

### Watch live logs

```bash
ssh polymarket@192.168.1.100 "journalctl --user -u weather-trader -f"
```

Log lines to watch for:
- `INFO weather trader up.` — startup
- `[REFRESH] running weather.dump` — forecast data refresh (every 10 min)
- `[SCAN]` — scanning markets for edge
- `BUY` — order placed
- `ERROR` or `Traceback` — crash (service auto-restarts in 3 s)

### Check current state (open positions, last heartbeat)

```bash
ssh polymarket@192.168.1.100 "cat ~/polymarket-weather-quant/dashboard/runtime/weather_trader_state.json"
```

### Check trade history

```bash
ssh polymarket@192.168.1.100 "tail -20 ~/polymarket-weather-quant/dashboard/runtime/weather_trader_trades.csv"
```

### Pause trading without stopping the process

```bash
# Edit config: set "enabled": false
ssh polymarket@192.168.1.100 "python3 -c \"
import json, pathlib
p = pathlib.Path('polymarket-weather-quant/dashboard/runtime/weather_trader_config.json')
c = json.loads(p.read_text()); c['enabled'] = False; p.write_text(json.dumps(c, indent=2))
print('Trading disabled — trader will stop opening new positions')
\""
```

### Stop / restart the service

```bash
# Stop
ssh polymarket@192.168.1.100 "systemctl --user stop weather-trader"

# Restart (e.g. after pulling a code update)
ssh polymarket@192.168.1.100 "systemctl --user restart weather-trader"
```

### Pull code updates and restart

```bash
ssh polymarket@192.168.1.100 "cd ~/polymarket-weather-quant && git pull && ~/.local/bin/uv sync --extra dashboard && systemctl --user restart weather-trader"
```

### Check portfolio on-chain

```bash
ssh polymarket@192.168.1.100 "cd ~/polymarket-weather-quant && ~/.local/bin/uv run python check_portfolio.py"
```

> **Note**: the trader runs on both Windows (PowerShell supervisor) and Linux (systemd) with separate state files. Both can run simultaneously against the same wallet — but if the same market is scanned from both machines within the same cycle, a duplicate position could open. Stop one instance once the other is confirmed working.



