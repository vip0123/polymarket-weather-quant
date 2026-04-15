"""Signals — latest research agent output per signal dir."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import streamlit as st

from lib import state

st.set_page_config(page_title="Signals", layout="wide")
st.title("Signals")

reports = state.signal_reports()
if not reports:
    st.info("No signals yet. Research agents write to signals/<name>/.")
    st.stop()

tabs = st.tabs([r["name"] for r in reports])
for tab, rep in zip(tabs, reports):
    with tab:
        ts = datetime.fromtimestamp(rep["mtime"]).strftime("%Y-%m-%d %H:%M") if rep["mtime"] else "—"
        st.caption(f"Last update: {ts}")

        if rep["findings"]:
            st.markdown(Path(rep["findings"]).read_text())
        elif rep["readme"]:
            st.warning("No findings.md yet — showing README.")
            st.markdown(Path(rep["readme"]).read_text())
        else:
            st.info("No README or findings yet.")

        if rep["summary"]:
            st.divider()
            st.subheader("Summary")
            p = Path(rep["summary"])
            if p.suffix == ".csv":
                try:
                    st.dataframe(pl.read_csv(p), use_container_width=True)
                except Exception as e:
                    st.error(f"Could not parse {p.name}: {e}")
            else:
                st.code(p.read_text())
