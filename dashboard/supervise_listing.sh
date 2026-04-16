#!/bin/bash
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python3 -m weather.listing_watcher
  echo "[$(date)] listing watcher exited, restart in 3s"
  sleep 3
done
