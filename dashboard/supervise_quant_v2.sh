#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
LOG="dashboard/runtime/quant_v2.log"
SUPLOG="dashboard/runtime/supervisor_v2.log"
MAX_RESTARTS_PER_HOUR=20
declare -a restarts

while true; do
  echo "[$(date -u +%FT%TZ)] supervisor_v2: starting quant_v2_engine.py" >> "$SUPLOG"
  .venv/bin/python3 dashboard/quant_v2_engine.py >> "$LOG" 2>&1
  rc=$?
  now=$(date +%s)
  restarts+=("$now")
  cutoff=$((now - 3600))
  new=()
  for t in "${restarts[@]}"; do [ "$t" -ge "$cutoff" ] && new+=("$t"); done
  restarts=("${new[@]}")
  echo "[$(date -u +%FT%TZ)] supervisor_v2: exited rc=$rc (restarts last hour: ${#restarts[@]})" >> "$SUPLOG"
  if [ "${#restarts[@]}" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    sleep 600; restarts=()
  else
    sleep 3
  fi
done
