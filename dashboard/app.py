"""PolyTerminal — Bloomberg-style cockpit for the Polymarket trading bot."""
from __future__ import annotations

import streamlit as st

from lib import polymarket, state
from lib.theme import (
    apply, ticker_bar, status_bar, panel_card, position_row, module_card,
)

st.set_page_config(page_title="PolyTerminal", page_icon="*", layout="wide", initial_sidebar_state="collapsed")
apply()

bot = state.load_bot_state()
cfg = state.load_copy_config()

# ── Top ticker ───────────────────────────────────────────────────────────────
ticker_bar(btc_price=None, btc_change=None, poly_spot=None)

# ── 3-column layout ──────────────────────────────────────────────────────────
left, center, right = st.columns([1, 3, 1], gap="small")

# ── LEFT: trader + positions ────────────────────────────────────────────────
with left:
    followed = cfg.get("followed_wallet") or "(not set)"
    short = followed[:12] + "…" if followed.startswith("0x") else followed
    st.markdown(
        f'<div class="panel"><h4>Trader · Polymarket</h4>'
        f'<div class="name" style="color:#c8d4d0;font-size:13px;">{short}</div>'
        f'<div class="sub">copy-trade target</div></div>',
        unsafe_allow_html=True,
    )

    panel_card("P&L · Past Month",
               f"${bot.realized_pnl_usd:,.0f}" if bot.realized_pnl_usd else "$0",
               "realized · bot lifetime")

    c1, c2 = st.columns(2)
    with c1:
        panel_card("Week", "$0", "7-day P&L")
    with c2:
        panel_card("Today", "$0", "24h P&L")

    st.markdown(
        f'<div class="panel" style="display:flex;justify-content:space-around;text-align:center;">'
        f'<div><div class="big" style="font-size:16px;">${bot.usdc_balance or 0:,.0f}</div>'
        f'<div class="sub">USDC</div></div>'
        f'<div><div class="big" style="font-size:16px;">{bot.open_positions}</div>'
        f'<div class="sub">Positions</div></div>'
        f'<div><div class="big" style="font-size:16px;">{bot.matic_balance or 0:.2f}</div>'
        f'<div class="sub">MATIC</div></div></div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="panel"><h4>Active Positions</h4></div>', unsafe_allow_html=True)
    if bot.wallet_address:
        positions = polymarket.wallet_positions(bot.wallet_address)
        for p in positions[:6]:
            price = p.get("curPrice") or 0
            pnl = p.get("cashPnl") or 0
            pct = p.get("percentPnl") or 0
            side = "up" if pnl >= 0 else "down"
            title = (p.get("title") or "")[:32]
            position_row(
                pill_text=f"{side.upper()} {price*100:.0f}¢",
                pill_kind=side,
                name=title,
                meta=(p.get("outcome") or ""),
                pnl=f"${pnl:+,.0f}\n{pct:+.1f}%",
                pnl_neg=pnl < 0,
            )
        if not positions:
            st.markdown('<div class="sub" style="padding:8px;">No open positions.</div>',
                        unsafe_allow_html=True)
    else:
        st.markdown('<div class="sub" style="padding:8px;">Wallet not configured.</div>',
                    unsafe_allow_html=True)

# ── CENTER: chart ────────────────────────────────────────────────────────────
with center:
    st.markdown(
        '<div class="panel" style="min-height:480px;">'
        '<h4>BTC/USD · Coinbase &nbsp;·&nbsp; Polymarket Implied Odds</h4>'
        '<div class="sub" style="margin-bottom:10px;">5-MIN CHART · waiting for live feed</div>'
        '<div style="color:#5a6b64;text-align:center;padding:160px 0;font-size:12px;letter-spacing:2px;">'
        'CHART RENDERS ONCE API KEYS ARE WIRED'
        '</div></div>',
        unsafe_allow_html=True,
    )

    cc1, cc2, cc3, cc4 = st.columns(4)
    for col, label, up, dn in [
        (cc1, "BTC APR 11 10AM ET", "100¢", "0¢"),
        (cc2, "BTC APR 11 12PM ET", "99¢", "1¢"),
        (cc3, "BTC 12:00–12:15PM", "100¢", "0¢"),
        (cc4, "BTC 12:00–4:00PM", "73¢", "27¢"),
    ]:
        with col:
            st.markdown(
                f'<div class="panel" style="text-align:center;">'
                f'<div class="sub" style="margin-bottom:6px;">{label}</div>'
                f'<div style="display:flex;justify-content:space-around;">'
                f'<div><div style="color:#00ff88;font-size:18px;font-weight:700;">{up}</div>'
                f'<div class="sub">UP</div></div>'
                f'<div><div style="color:#ff4466;font-size:18px;font-weight:700;">{dn}</div>'
                f'<div class="sub">DOWN</div></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

# ── RIGHT: AI stack modules ──────────────────────────────────────────────────
with right:
    st.markdown(
        '<div class="panel"><h4>AI Stack · Modules</h4>'
        '<div class="sub" style="color:#00ff88;">ALL SYSTEMS GO</div></div>',
        unsafe_allow_html=True,
    )
    module_card("polymarket-mcp", "Market data + activity feed", "LIVE")
    module_card("copy-engine", "1:1 mirror of target wallet", "RUNNING" if bot.running else "IDLE", online=bot.running)
    module_card("disposition", "PGR/PLR wallet scoring", "RESEARCH")
    module_card("whale-flow", "Smart-money net flow", "RESEARCH")
    module_card("pair-corr", "Stat-arb on neg-risk pairs", "RESEARCH")
    module_card("velocity", "dVol/dt momentum z-score", "RESEARCH")
    module_card("goldsky-sync", "Orderbook event ingest", "LIVE")
    module_card("risk-guard", "Max-position + slippage", "LIVE")

# ── Bottom status bar ────────────────────────────────────────────────────────
status_bar([
    ("COPY-ENGINE", "RUNNING" if bot.running else "IDLE", bot.running),
    ("SIGNALS", f"{len(state.signal_reports())}/4", True),
    ("API", "LIVE" if polymarket.creds_configured() else "KEYS PENDING", polymarket.creds_configured()),
    ("UPTIME", "24/7", True),
])
