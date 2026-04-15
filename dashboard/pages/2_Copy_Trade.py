"""Copy Trade — followed wallet config, risk caps, kill switch, live mirror log."""
from __future__ import annotations

import polars as pl
import streamlit as st

from lib import polymarket, state
from lib.theme import apply

st.set_page_config(page_title="Copy Trade", layout="wide")
apply()
st.title("Copy Trade")

cfg = state.load_copy_config()

# Big red kill switch at the top
col_k1, col_k2, col_k3 = st.columns([1, 1, 3])
with col_k1:
    if cfg.get("enabled", False):
        if st.button("KILL SWITCH — STOP COPYING", type="primary", use_container_width=True):
            cfg["enabled"] = False
            state.save_copy_config(cfg)
            st.rerun()
    else:
        if st.button("ENABLE COPY TRADING", use_container_width=True):
            cfg["enabled"] = True
            state.save_copy_config(cfg)
            st.rerun()
with col_k2:
    dry = cfg.get("dry_run", True)
    if st.button("DRY-RUN: ON" if dry else "DRY-RUN: OFF", use_container_width=True):
        cfg["dry_run"] = not dry
        state.save_copy_config(cfg)
        st.rerun()
with col_k3:
    status = "LIVE" if cfg.get("enabled") and not cfg.get("dry_run") else (
             "DRY-RUN" if cfg.get("enabled") else "DISABLED")
    st.metric("Status", status)

st.divider()

with st.form("copy_cfg"):
    c1, c2 = st.columns(2)
    with c1:
        followed = st.text_input("Followed wallet (0x…)", cfg.get("followed_wallet") or "")
        max_pos = st.number_input("Max position size (USD)", 1.0, 10_000.0,
                                  float(cfg.get("max_position_usd", 25.0)))
        max_open = st.number_input("Max open positions", 1, 100,
                                   int(cfg.get("max_open_positions", 3)))
    with c2:
        scale = st.number_input("Size scale (our/their)", 0.0, 1.0,
                                float(cfg.get("scale", 0.05)))
        max_loss = st.number_input("Max daily loss (USD)", 1.0, 10_000.0,
                                   float(cfg.get("max_daily_loss_usd", 50.0)))
        max_slip = st.number_input("Max slippage (bps)", 0.0, 2000.0,
                                   float(cfg.get("max_slippage_bps", 300.0)))
    if st.form_submit_button("Save config"):
        cfg.update({
            "followed_wallet": followed.strip() or None,
            "max_position_usd": max_pos, "max_open_positions": max_open,
            "scale": scale, "max_daily_loss_usd": max_loss,
            "max_slippage_bps": max_slip,
        })
        state.save_copy_config(cfg)
        st.success("Saved. Engine picks up on next loop.")

st.divider()

followed = cfg.get("followed_wallet")
if not followed:
    st.info("Set a followed wallet to see their activity.")
    st.stop()

st.subheader(f"Target activity · {followed[:12]}…")
activity = polymarket.wallet_activity(followed, limit=50)
if activity:
    df = pl.DataFrame(activity)
    keep = [c for c in ["timestamp", "title", "outcome", "side", "price", "size", "usdcSize"] if c in df.columns]
    st.dataframe(df.select(keep) if keep else df, use_container_width=True, height=300)
else:
    st.warning("No activity from data API (address may be wrong or wallet inactive).")

st.subheader("Our mirror trades")
trades = state.load_bot_trades()
ours = trades.filter(pl.col("source_wallet") == followed) if trades.height else trades
if ours.height:
    st.dataframe(ours.sort("timestamp", descending=True), use_container_width=True, height=300)
else:
    st.info("No mirror trades yet.")
