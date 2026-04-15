#!/bin/bash
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 dashboard/flip_engine.py
  echo "[$(date)] flip engine exited, restarting in 3s"
  sleep 3
done
