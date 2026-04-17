#!/bin/bash
# Health monitor — runs every 5 min via cron
# Checks trader + watcher, restarts if down, rotates large logs

REPO_DIR="/Users/kevinbahrabadi/POLYMARKET TRADER/poly_data"
LOG_FILE="$REPO_DIR/dashboard/runtime/health_monitor.log"
MAX_LOG_BYTES=10485760  # 10MB

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"; }

# 1. Check trader
if ! pgrep -f "weather.trader" > /dev/null; then
    log "❌ TRADER DOWN — restarting via launchctl"
    launchctl unload ~/Library/LaunchAgents/com.weatherquant.trader.plist 2>/dev/null
    sleep 2
    launchctl load ~/Library/LaunchAgents/com.weatherquant.trader.plist
    log "✅ Trader restart issued"
else
    log "✅ Trader running (PID $(pgrep -f 'weather.trader' | head -1))"
fi

# 2. Check position watcher
if ! pgrep -f "position_watcher" > /dev/null; then
    log "❌ WATCHER DOWN — restarting via launchctl"
    launchctl unload ~/Library/LaunchAgents/com.weatherquant.watcher.plist 2>/dev/null
    sleep 2
    launchctl load ~/Library/LaunchAgents/com.weatherquant.watcher.plist
    log "✅ Watcher restart issued"
else
    log "✅ Watcher running (PID $(pgrep -f 'position_watcher' | head -1))"
fi

# 3. Check heartbeat staleness (>15 min = stale)
STATE_FILE="$REPO_DIR/dashboard/runtime/weather_trader_state.json"
if [ -f "$STATE_FILE" ]; then
    STATE_AGE=$(( $(date +%s) - $(stat -f%m "$STATE_FILE") ))
    if [ "$STATE_AGE" -gt 900 ]; then
        log "⚠️  State file stale (${STATE_AGE}s old) — trader may be hung"
    fi
fi

# 4. Rotate large logs
for LOGF in "$REPO_DIR/dashboard/runtime/weather_trader.log" \
            "$REPO_DIR/dashboard/runtime/position_watcher.log"; do
    if [ -f "$LOGF" ]; then
        LOG_SIZE=$(stat -f%z "$LOGF" 2>/dev/null || echo 0)
        if [ "$LOG_SIZE" -gt "$MAX_LOG_BYTES" ]; then
            log "🔄 Rotating $(basename "$LOGF") (${LOG_SIZE} bytes)"
            mv "$LOGF" "$LOGF.old"
        fi
    fi
done
