#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
LOG="dashboard/runtime/quant_v3.log"
SUPLOG="dashboard/runtime/supervisor_v3.log"
while true; do
  echo "[$(date -u +%FT%TZ)] supervisor_v3: starting" >> "$SUPLOG"
  .venv/bin/python3 dashboard/quant_v3_engine.py >> "$LOG" 2>&1
  echo "[$(date -u +%FT%TZ)] supervisor_v3: exited" >> "$SUPLOG"
  sleep 3
done
