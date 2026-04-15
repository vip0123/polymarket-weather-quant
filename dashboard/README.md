# Dashboard

Streamlit cockpit for the Polymarket trading bot.

## Run

```bash
./dashboard/run.sh          # port 8501
# or
uv run --extra dashboard streamlit run dashboard/app.py
```

## Pages

- **app.py** — overview: bot status, balances, signal freshness.
- **Live** — open positions (Polymarket data API), recent fills.
- **Copy Trade** — configure followed wallet, view their activity + our mirror trades.
- **Signals** — latest `findings.md` per `signals/<name>/`.
- **History** — trade log, equity curve, filters.

## State files (bot writes, dashboard reads)

- `dashboard/runtime/bot_state.json` — `{running, last_heartbeat, wallet_address, usdc_balance, matic_balance, open_positions, realized_pnl_usd, followed_wallet}`
- `dashboard/runtime/copy_config.json` — written by the dashboard form, read by the bot.
- `dashboard/runtime/bot_trades.csv` — append-only fill log. Columns: `timestamp, market_id, side, direction, price, size_usd, tx_hash, source_wallet, signal`.

## Credentials

Set before running the bot (not needed for read-only dashboard):

```
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
```
