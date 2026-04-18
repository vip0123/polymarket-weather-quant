#!/bin/bash
# Wallet A supervisor (uniquely named to dodge wQuant5 watchdog's pgrep).
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 -m weather.trader
  echo "[$(date)] walletA weather trader exited, restarting in 3s"
  sleep 3
done
