"""Quant — side-by-side P&L comparison: copy-engine vs quant-engine."""
from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl
import streamlit as st

from lib.theme import apply

st.set_page_config(page_title="Quant vs Copy", layout="wide")
apply()
st.title("Quant vs Copy")
st.caption("My own ML strategy (quant) vs 1:1 copy-trade — live comparison.")

RUNTIME = Path("/Users/kevinbahrabadi/POLYMARKET TRADER/poly_data/dashboard/runtime")


def load_json(p: Path, default):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except: return default


def read_trades(path: Path):
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_csv(path)
    except:
        return pl.DataFrame()


copy_state = load_json(RUNTIME / "bot_state.json", {})
quant_state = load_json(RUNTIME / "quant_state.json", {})
copy_cfg = load_json(RUNTIME / "copy_config.json", {})
quant_cfg = load_json(RUNTIME / "quant_config.json", {})

copy_trades = read_trades(RUNTIME / "bot_trades.csv")
quant_trades = read_trades(RUNTIME / "quant_trades.csv")

# ── KPI row ────────────────────────────────────────────────────────────────
c1, c2 = st.columns(2)
with c1:
    st.markdown('<div class="panel"><h4>Copy Engine</h4></div>', unsafe_allow_html=True)
    k1, k2, k3 = st.columns(3)
    k1.metric("Status", "LIVE" if copy_cfg.get("enabled") and not copy_cfg.get("dry_run") else "DRY")
    k2.metric("Fills", copy_trades.height if copy_trades.height else 0)
    k3.metric("Cash", f"${copy_state.get('usdc_balance') or 0:.2f}")
with c2:
    st.markdown('<div class="panel"><h4>Quant Engine</h4></div>', unsafe_allow_html=True)
    k1, k2, k3 = st.columns(3)
    k1.metric("Status", "LIVE" if quant_cfg.get("enabled") and not quant_cfg.get("dry_run") else "DRY")
    k2.metric("Fills", quant_trades.height if quant_trades.height else 0)
    k3.metric("Allocation", f"${quant_cfg.get('allocation_usd', 0):.0f}")

st.divider()

# ── controls for quant engine ──────────────────────────────────────────────
st.markdown("### Quant engine controls")
cc1, cc2, cc3 = st.columns([1,1,3])
with cc1:
    if quant_cfg.get("enabled", False):
        if st.button("STOP QUANT", type="primary"):
            quant_cfg["enabled"] = False
            (RUNTIME / "quant_config.json").write_text(json.dumps(quant_cfg, indent=2))
            st.rerun()
    else:
        if st.button("ENABLE QUANT"):
            quant_cfg["enabled"] = True
            (RUNTIME / "quant_config.json").write_text(json.dumps(quant_cfg, indent=2))
            st.rerun()
with cc2:
    dry = quant_cfg.get("dry_run", True)
    if st.button(f"DRY-RUN: {'ON' if dry else 'OFF'}"):
        quant_cfg["dry_run"] = not dry
        (RUNTIME / "quant_config.json").write_text(json.dumps(quant_cfg, indent=2))
        st.rerun()

with st.form("quant_params"):
    c1, c2, c3 = st.columns(3)
    min_edge = c1.number_input("Min edge", 0.01, 0.30, float(quant_cfg.get("min_edge", 0.05)), step=0.01)
    max_pos = c2.number_input("Max position $", 5.0, 500.0, float(quant_cfg.get("max_position_usd", 50.0)))
    min_time = c3.number_input("Min time left (s)", 30, 600, int(quant_cfg.get("min_time_remaining_s", 120)))
    if st.form_submit_button("Save"):
        quant_cfg.update({"min_edge": min_edge, "max_position_usd": max_pos,
                          "min_time_remaining_s": min_time})
        (RUNTIME / "quant_config.json").write_text(json.dumps(quant_cfg, indent=2))
        st.success("Saved.")

st.divider()

# ── spot prices from quant state ───────────────────────────────────────────
st.subheader("Spot feed (Binance)")
spot = quant_state.get("spot", {})
cols = st.columns(len(spot) or 1)
for col, (sym, price) in zip(cols, spot.items()):
    col.metric(sym.replace("USDT", ""), f"${price:,.2f}" if price else "—")

st.divider()

# ── trade tables ───────────────────────────────────────────────────────────
col1, col2 = st.columns(2)
with col1:
    st.subheader("Copy trades (recent)")
    if copy_trades.height:
        st.dataframe(copy_trades.tail(25).reverse(), use_container_width=True, height=400)
    else:
        st.info("No copy trades yet.")
with col2:
    st.subheader("Quant trades (recent)")
    if quant_trades.height:
        st.dataframe(quant_trades.tail(25).reverse(), use_container_width=True, height=400)
    else:
        st.info("No quant trades yet.")

time.sleep(5)
st.rerun()
