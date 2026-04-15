#!/bin/bash
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 -m weather.trader
  echo "[$(date)] weather trader exited, restarting in 3s"
  sleep 3
done
