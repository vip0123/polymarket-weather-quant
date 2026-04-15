"""Live — wallet balance, open positions, recent fills."""
from __future__ import annotations

import polars as pl
import streamlit as st

from lib import polymarket, state

st.set_page_config(page_title="Live", layout="wide")
st.title("Live")

bot = state.load_bot_state()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Bot", "RUNNING" if bot.running else "IDLE")
c2.metric("USDC", f"{bot.usdc_balance:,.2f}" if bot.usdc_balance is not None else "—")
c3.metric("MATIC", f"{bot.matic_balance:,.4f}" if bot.matic_balance is not None else "—")
c4.metric("Realized PnL", f"${bot.realized_pnl_usd:,.2f}")

st.divider()

st.subheader("Open positions")
if bot.wallet_address:
    positions = polymarket.wallet_positions(bot.wallet_address)
    if positions:
        df = pl.DataFrame(positions)
        keep = [c for c in ["title", "outcome", "size", "avgPrice", "curPrice", "cashPnl", "percentPnl"] if c in df.columns]
        st.dataframe(df.select(keep) if keep else df, use_container_width=True)
    else:
        st.info("No open positions (or data API unreachable).")
else:
    st.warning("Wallet address not set. Configure on Copy Trade page.")

st.divider()

st.subheader("Recent fills")
trades = state.load_bot_trades()
if trades.height == 0:
    st.info("No bot trades yet.")
else:
    st.dataframe(trades.sort("timestamp", descending=True).head(50), use_container_width=True)

st.caption("Auto-refresh: reload the page or set a Streamlit auto-refresh component.")
