#!/usr/bin/env bash
# Quant-engine supervisor: restart on crash, log each restart.
# Run: nohup ./dashboard/supervise_quant.sh > dashboard/runtime/supervisor.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
LOG="dashboard/runtime/quant.log"
SUPLOG="dashboard/runtime/supervisor.log"
MAX_RESTARTS_PER_HOUR=20
declare -a restarts

while true; do
  echo "[$(date -u +%FT%TZ)] supervisor: starting quant_engine.py" >> "$SUPLOG"
  .venv/bin/python3 dashboard/quant_engine.py >> "$LOG" 2>&1
  rc=$?
  now=$(date +%s)
  restarts+=("$now")
  # prune restarts older than 1h
  cutoff=$((now - 3600))
  new=()
  for t in "${restarts[@]}"; do
    [ "$t" -ge "$cutoff" ] && new+=("$t")
  done
  restarts=("${new[@]}")
  echo "[$(date -u +%FT%TZ)] supervisor: quant exited rc=$rc (restarts last hour: ${#restarts[@]})" >> "$SUPLOG"
  if [ "${#restarts[@]}" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    echo "[$(date -u +%FT%TZ)] supervisor: too many restarts, sleeping 10min" >> "$SUPLOG"
    sleep 600
    restarts=()
  else
    sleep 3
  fi
done
