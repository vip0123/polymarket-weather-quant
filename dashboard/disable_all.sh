#!/usr/bin/env bash
# Flip every engine config to enabled=false. Does NOT kill processes —
# engines stay alive to resolve positions/TP but stop placing new orders.
set -u
cd "$(dirname "$0")/.."
python3 - <<'PY'
import json
from pathlib import Path
RUNTIME = Path("dashboard/runtime")
for name in ("copy_config.json", "quant_config.json", "quant_v3_config.json",
             "sniper_config.json"):
    p = RUNTIME / name
    if not p.exists(): continue
    d = json.loads(p.read_text())
    d["enabled"] = False
    p.write_text(json.dumps(d, indent=2))
    print(f"disabled: {name}")
PY
echo "[$(date -u +%FT%TZ)] all engines disabled" >> dashboard/runtime/shutdown.log
