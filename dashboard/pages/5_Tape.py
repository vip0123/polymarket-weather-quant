"""Tape — one row per target fill, joined with our mirror (if any)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from lib import state
from lib.theme import apply

st.set_page_config(page_title="Tape", layout="wide")
apply()
st.title("Tape")
st.caption("Target's trades joined with ours by tx hash. Newest first.")

cfg = state.load_copy_config()
target = cfg.get("followed_wallet")
if not target:
    st.warning("No followed wallet configured.")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 4])
n_rows = c1.number_input("Rows", 20, 200, 60)
refresh_s = c2.number_input("Refresh (s)", 1, 30, 3)

@st.cache_data(ttl=2)
def fetch_target(addr: str, limit: int):
    try:
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": addr, "limit": limit, "type": "TRADE"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        return r.json() or []
    except Exception:
        return []

POST_RE = re.compile(
    r"^(?P<ts>\S+ \S+),\d+ INFO posted order tx=(?P<tx>\w+) lag=(?P<lag>[-\d.]+)s"
    r"(?: slip=(?P<slip>[-+\d]+)bps \(target=(?P<tp>[\d.]+) ours=(?P<op>[\d.]+)\))?"
    r".*?'takingAmount': '(?P<tk>[\d.]*)', 'makingAmount': '(?P<mk>[\d.]*)'"
    r".*?'status': '(?P<status>\w+)'"
)
BLOCK_RE = re.compile(r"\[BLOCKED\] .* tx=(?P<tx>\w+) size=\$(?P<usd>[\d.]+)")
FAIL_RE = re.compile(r"ERROR mirror failed.* 'error': '(?P<err>[^']+)'")

def parse_our_fills():
    """Return {tx_prefix: record} indexed by first 10 chars of target tx."""
    log_path = Path("/Users/kevinbahrabadi/POLYMARKET TRADER/poly_data/dashboard/runtime/engine.log")
    if not log_path.exists():
        return {}, []
    lines = log_path.read_text().splitlines()[-20000:]
    by_tx = {}
    recent = []
    for line in lines:
        m = POST_RE.search(line)
        if m:
            d = m.groupdict()
            try:
                rec = {
                    "tx_short": d["tx"],
                    "our_time": d["ts"].split()[1][:8],
                    "lag_s": float(d["lag"]),
                    "slip_bps": int(d["slip"]) if d["slip"] else 0,
                    "our_price": float(d["op"]) if d["op"] else 0.0,
                    "our_usd": float(d["mk"] or 0),
                    "status": d["status"],
                }
                by_tx[d["tx"][:10]] = rec
                recent.append(rec)
            except (TypeError, ValueError):
                pass
        elif (m2 := BLOCK_RE.search(line)):
            by_tx[m2.group("tx")[:10]] = {
                "tx_short": m2.group("tx"),
                "status": "blocked", "our_usd": float(m2.group("usd")),
                "lag_s": 0, "slip_bps": 0, "our_price": 0, "our_time": "",
            }
    return by_tx, recent

target_fills = fetch_target(target, n_rows)
by_tx, recent_mirrors = parse_our_fills()

# Build joined rows
rows = []
for t in target_fills:
    tx_short = (t.get("transactionHash", "") or "")[:10]
    ours = by_tx.get(tx_short)
    ts = datetime.fromtimestamp(int(t.get("timestamp", 0)), tz=timezone.utc).strftime("%H:%M:%S")
    rows.append({
        "time": ts,
        "market": (t.get("title", "") or "")[:48],
        "side": t.get("side", ""),
        "tgt $": float(t.get("price", 0) or 0),
        "tgt $ size": float(t.get("usdcSize", 0) or 0),
        "our $": ours["our_price"] if ours else None,
        "our $ size": ours["our_usd"] if ours else None,
        "lag": f'{ours["lag_s"]:.1f}s' if ours else "",
        "slip": ours["slip_bps"] if ours else None,
        "status": ours["status"].upper() if ours else "MISSED",
    })

df = pd.DataFrame(rows)

def style_row(row):
    style = [""] * len(row)
    status = row.get("status", "")
    slip = row.get("slip")
    if status == "MATCHED":
        style = ["color: #c8d4d0"] * len(row)
    elif status == "LIVE":
        style = ["color: #ffb020"] * len(row)  # amber: on book
    elif status == "MISSED":
        style = ["color: #5a6b64"] * len(row)  # dim: we skipped/never saw
    elif status == "BLOCKED":
        style = ["color: #ff4466"] * len(row)
    return style

def color_slip(v):
    if v is None or pd.isna(v):
        return ""
    if v < 0:
        return "color: #00ff88; font-weight:600"  # favorable
    if v > 0:
        return "color: #ff4466; font-weight:600"  # adverse
    return "color: #5a6b64"

def color_side(v):
    return "color: #00ff88" if v == "BUY" else ("color: #ff4466" if v == "SELL" else "")

styled = (df.style
          .apply(style_row, axis=1)
          .map(color_slip, subset=["slip"])
          .map(color_side, subset=["side"])
          .format({"tgt $": "{:.3f}", "our $": "{:.3f}", "tgt $ size": "${:.2f}",
                   "our $ size": "${:.2f}", "slip": "{:+d}"}, na_rep="—"))

st.dataframe(styled, use_container_width=True, hide_index=True, height=640)

# summary
matched = sum(1 for r in rows if r["status"] == "MATCHED")
missed = sum(1 for r in rows if r["status"] == "MISSED")
blocked = sum(1 for r in rows if r["status"] == "BLOCKED")
live = sum(1 for r in rows if r["status"] == "LIVE")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Matched", matched)
c2.metric("Live on book", live)
c3.metric("Blocked", blocked)
c4.metric("Missed", missed)

time.sleep(refresh_s)
st.rerun()
