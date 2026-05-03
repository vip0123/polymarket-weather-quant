#!/bin/bash
# Weather trader supervisor — cross-platform bash version.
# On Windows, prefer supervise_weather.ps1 instead.
cd "$(dirname "$0")/.."

LOG_DIR="dashboard/runtime"
mkdir -p "$LOG_DIR"

while true; do
  uv run python -m weather.trader
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] weather.trader exited, restarting in 3s..."
  sleep 3
done
