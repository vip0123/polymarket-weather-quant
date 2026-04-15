#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
LOG="dashboard/runtime/sniper.log"
SUPLOG="dashboard/runtime/supervisor_sniper.log"
MAX_RESTARTS_PER_HOUR=20
declare -a restarts

while true; do
  echo "[$(date -u +%FT%TZ)] supervisor_sniper: starting sniper_engine.py" >> "$SUPLOG"
  .venv/bin/python3 dashboard/sniper_engine.py >> "$LOG" 2>&1
  rc=$?
  now=$(date +%s)
  restarts+=("$now")
  cutoff=$((now - 3600))
  new=()
  for t in "${restarts[@]}"; do [ "$t" -ge "$cutoff" ] && new+=("$t"); done
  restarts=("${new[@]}")
  echo "[$(date -u +%FT%TZ)] supervisor_sniper: exited rc=$rc (restarts: ${#restarts[@]})" >> "$SUPLOG"
  if [ "${#restarts[@]}" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    sleep 600; restarts=()
  else
    sleep 3
  fi
done
