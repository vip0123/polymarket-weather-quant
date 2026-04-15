#!/bin/bash
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 dashboard/ta_engine.py
  echo "[$(date)] ta engine exited, restarting in 3s"
  sleep 3
done
