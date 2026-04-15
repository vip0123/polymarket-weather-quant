"""History — bot trade log + equity curve."""
from __future__ import annotations

import polars as pl
import streamlit as st

from lib import state

st.set_page_config(page_title="History", layout="wide")
st.title("History")

trades = state.load_bot_trades()
if trades.height == 0:
    st.info("No bot trades yet.")
    st.stop()

sources = ["(all)"] + sorted(trades.get_column("source_wallet").drop_nulls().unique().to_list())
signals = ["(all)"] + sorted(trades.get_column("signal").drop_nulls().unique().to_list())
c1, c2 = st.columns(2)
src = c1.selectbox("Source wallet", sources)
sig = c2.selectbox("Signal", signals)

view = trades
if src != "(all)":
    view = view.filter(pl.col("source_wallet") == src)
if sig != "(all)":
    view = view.filter(pl.col("signal") == sig)

view = view.sort("timestamp")

st.subheader("Equity curve")
if "size_usd" in view.columns and "direction" in view.columns and view.height:
    signed = view.with_columns(
        (pl.when(pl.col("direction") == "SELL").then(pl.col("size_usd")).otherwise(-pl.col("size_usd"))).alias("cashflow")
    )
    eq = signed.select(["timestamp", "cashflow"]).with_columns(pl.col("cashflow").cum_sum().alias("equity"))
    st.line_chart(eq.to_pandas().set_index("timestamp")["equity"])
else:
    st.caption("Need size_usd + direction to plot equity.")

st.subheader("Trade log")
st.dataframe(view.sort("timestamp", descending=True), use_container_width=True)
st.caption(f"{view.height:,} trades")
