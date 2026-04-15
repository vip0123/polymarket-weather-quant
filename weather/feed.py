"""Unified live buy/fill feed across copy engine + weather quant.

Tails both logs and prints a clean, colorized stream of just BUY/FILL/EDGE
events. Ignores scan heartbeats, reconnects, cooldowns.

Run:  uv run python -m weather.feed
"""
from __future__ import annotations

import os
import re
import select
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_LOG = ROOT / "dashboard" / "runtime" / "engine.log"
QUANT_LOG = ROOT / "dashboard" / "runtime" / "weather_trader.log"

# ANSI colors
G = "\033[32m"   # green — buys / fills
Y = "\033[33m"   # yellow — edges / detections
B = "\033[34m"   # blue — metadata
R = "\033[31m"   # red — blocked / errors
C = "\033[36m"   # cyan — quant
M = "\033[35m"   # magenta — copy
RST = "\033[0m"

EVENTS = [
    # (regex, color, short_tag, source)
    (re.compile(r"posted order tx=(\S+).*target=([\d.]+) ours=([\d.]+).*status': '(\w+)'"),
     M, "COPY-BUY", "COPY"),
    (re.compile(r"\[BLOCKLIST\] (.+)"), R, "COPY-BLOCKED", "COPY"),
    (re.compile(r"\[COOLDOWN\] skip"), B, "COPY-COOLDOWN", "COPY"),
    (re.compile(r"\[BUY\] (\S+) (\w+) kelly=([\d.]+) ≈\$([\d.]+) @ ([\d.]+) → (\w+)"),
     G, "QUANT-BUY", "QUANT"),
    (re.compile(r"\[BUY\] (\S+) (\w+) ≈\$([\d.]+) @ ([\d.]+) → (\w+)"),
     G, "QUANT-BUY", "QUANT"),
    (re.compile(r"\[EDGE\] (\S+) (\w+) ask=([\d.]+) depth=\$([\d.]+) our=([\d.]+) mkt=([\d.]+)  (.+)"),
     Y, "EDGE", "QUANT"),
    (re.compile(r"\[FILL\] order (\S+): (.+)"), G, "QUANT-FILL", "QUANT"),
    (re.compile(r"\[REFRESH\] (.+)"), B, "REFRESH", "QUANT"),
]


def format_line(line: str, source: str) -> str | None:
    for rx, color, tag, src in EVENTS:
        m = rx.search(line)
        if not m:
            continue
        ts = line[:19] if len(line) >= 19 else ""
        rest = m.group(0)[:100]
        src_tag = C + "Q" + RST if src == "QUANT" else M + "C" + RST
        return f"{B}{ts}{RST} [{src_tag}] {color}{tag:<14}{RST} {rest}"
    return None


def tail_forever():
    files = {"COPY": COPY_LOG, "QUANT": QUANT_LOG}
    fhs = {}
    for src, p in files.items():
        try:
            fhs[src] = open(p, "r")
            fhs[src].seek(0, 2)  # seek to end
        except Exception as e:
            print(f"[!] can't open {p}: {e}")
    print(f"{B}══════════════════════════════════════════════════════════════════════════════{RST}")
    print(f"  {C}PolyTerminal live feed{RST}  —  {M}C=copy engine{RST}  {C}Q=weather quant{RST}")
    print(f"  Ctrl-C to exit")
    print(f"{B}══════════════════════════════════════════════════════════════════════════════{RST}\n")
    try:
        while True:
            idle = True
            for src, fh in fhs.items():
                line = fh.readline()
                while line:
                    idle = False
                    formatted = format_line(line.rstrip(), src)
                    if formatted:
                        print(formatted)
                        sys.stdout.flush()
                    line = fh.readline()
            if idle:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{B}feed stopped{RST}")


if __name__ == "__main__":
    tail_forever()
