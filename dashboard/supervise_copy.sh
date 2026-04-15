#!/bin/bash
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 dashboard/engine.py
  echo "[$(date)] copy engine exited, restarting in 3s"
  sleep 3
done
