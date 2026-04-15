"""Live portfolio view — show all open positions, cost basis, mark-to-market, tail risk.

Run:  uv run python -m weather.positions
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
WALLET = os.environ.get("POLY_WALLET_ADDRESS") or os.environ.get("POLY_FUNDER") or ""
if not WALLET:
    raise SystemExit("POLY_WALLET_ADDRESS not set in .env")


def fetch_positions() -> list[dict]:
    r = requests.get("https://data-api.polymarket.com/positions",
                     params={"user": WALLET, "sizeThreshold": 0.1},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    data = r.json() or []
    return [p for p in data if isinstance(p, dict)]


def fetch_activity(limit: int = 200) -> list[dict]:
    r = requests.get("https://data-api.polymarket.com/activity",
                     params={"user": WALLET, "limit": limit},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    return r.json() or []


def main():
    load_dotenv()
    print(f"\n╔══════════════════════════════════════════════════════════════════════════════╗")
    print(f"║  PolyTerminal — {WALLET[:10]}…{WALLET[-4:]}  @  {datetime.now().strftime('%H:%M:%S')}")
    print(f"╚══════════════════════════════════════════════════════════════════════════════╝")

    positions = fetch_positions()
    if not positions:
        print("no open positions")
        return

    total_cost = total_current = 0.0
    winning = losing = 0

    print(f"\n{'TITLE':<58}{'SIDE':<5}{'SZ':>6}{'AVG':>7}{'CUR':>7}{'COST':>8}{'VAL':>8}{'PNL':>8}")
    print("─" * 107)
    for p in sorted(positions, key=lambda x: -float(x.get("initialValue", 0))):
        title = (p.get("title") or "?")[:56]
        side = p.get("outcome", "?")[:3]
        size = float(p.get("size", 0))
        avg = float(p.get("avgPrice", 0))
        cur = float(p.get("curPrice", 0))
        cost = float(p.get("initialValue", 0))
        val = float(p.get("currentValue", 0))
        pnl = val - cost
        marker = "🟢" if pnl > 0 else ("🔴" if pnl < -1 else "⚪")
        # plain ascii fallback
        marker = "+" if pnl > 0 else ("-" if pnl < -1 else "=")
        print(f"{title:<58}{side:<5}{size:>6.0f}{avg:>7.3f}{cur:>7.3f}"
              f"{cost:>8.2f}{val:>8.2f}{marker}{abs(pnl):>7.2f}")
        total_cost += cost
        total_current += val
        if pnl > 0: winning += 1
        elif pnl < -1: losing += 1

    print("─" * 107)
    total_pnl = total_current - total_cost
    print(f"{'TOTAL':<58}{'':5}{'':6}{'':7}{'':7}"
          f"{total_cost:>8.2f}{total_current:>8.2f}"
          f"{'+' if total_pnl >= 0 else '-'}{abs(total_pnl):>7.2f}")
    print(f"\n  {len(positions)} positions  |  {winning} winning  {losing} losing  "
          f"|  {total_pnl/total_cost*100 if total_cost else 0:+.1f}% MtM")

    # Show last 5 fills for context
    print(f"\n  Recent fills:")
    acts = fetch_activity(limit=10)
    for a in acts[:5]:
        ts = datetime.fromtimestamp(a.get("timestamp", 0)).strftime("%H:%M")
        side = a.get("side", "?")
        price = a.get("price", 0)
        title = (a.get("title") or "?")[:70]
        outcome = a.get("outcome", "?")[:3]
        print(f"    {ts}  {side:<4} @${price:<6.3f} {outcome:<3}  {title}")


if __name__ == "__main__":
    sys.exit(main())
