"""Streamlit app for the 28 SMA momentum strategy (NSE ETFs).

Run locally:   py -m streamlit run app.py
Host:          Streamlit Community Cloud (see README.md). The hosted filesystem is ephemeral, so the
               ledger lives in the browser session; use the upload/download buttons to keep it.
"""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

import backtest as bt
import scanner as sc

ROOT = Path(__file__).parent
st.set_page_config(page_title="ETF 28 SMA Momentum", page_icon="📈", layout="wide")


# ------------------------------------------------------------------ data

@st.cache_data
def load_universe():
    return pd.read_csv(ROOT / "etfs.csv")


@st.cache_data(ttl=900, show_spinner="Fetching latest prices from Yahoo Finance...")
def get_closes(symbols):
    return sc.fetch(list(symbols))


@st.cache_data(ttl=86400, show_spinner="Downloading price history (first run can take a minute)...")
def get_history(symbols):
    return bt.calendar(bt.load_prices(list(symbols)))


def clean_hold(df):
    df = df.reindex(columns=sc.HOLD_COLS)
    for c in ("price", "qty", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["buy_date"] = df["buy_date"].astype(str)
    return df.dropna(subset=["symbol", "price", "qty"]).reset_index(drop=True)


def clean_sold(df):
    df = df.reindex(columns=sc.SOLD_COLS)
    for c in sc.SOLD_COLS[3:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["symbol", "profit"]).reset_index(drop=True)


# ------------------------------------------------------------------ sidebar / state

base = sc.load_cfg()
uni = load_universe()
symbols = tuple(uni["symbol"])

with st.sidebar:
    st.title("📈 ETF 28 SMA")
    st.caption("Momentum strategy for NSE ETFs")
    cfg = dict(base)
    cfg["mode"] = st.radio("Mode", ["lifo", "fifo"], index=0 if base["mode"] == "lifo" else 1,
                           horizontal=True, help="LIFO sells lots one at a time; FIFO sells everything at once.")
    cfg["capital"] = st.number_input("Starting capital (₹)", 10000, 100_000_000, int(base["capital"]), step=50000)
    cfg["reserve_pct"] = st.slider("Averaging reserve (%)", 0, 60, int(base["reserve_pct"]))
    cfg["max_etfs"] = st.number_input("Max ETFs held", 1, 60, int(base["max_etfs"]))
    cfg["max_new_buys_per_day"] = st.number_input("Max new buys per day", 1, 20, int(base["max_new_buys_per_day"]))
    cfg["max_per_family"] = st.number_input("Max ETFs per family (0 = off)", 0, 10, int(base["max_per_family"]))
    with st.expander("Signal parameters"):
        cfg["sma_days"] = st.number_input("SMA days", 5, 200, int(base["sma_days"]))
        cfg["target_pct"] = st.number_input("Min profit to sell (%)", 0.0, 50.0, float(base["target_pct"]), step=0.1)
        cfg["avg_drop_pct"] = st.number_input("Min drop to average (%)", 0.0, 50.0, float(base["avg_drop_pct"]))
        cfg["fast_move_pct"] = st.number_input("Fast-mover threshold, 1 month (%)", 1.0, 100.0, float(base["fast_move_pct"]))
        cfg["max_stale_pct"] = st.number_input("Block scan if stale ETFs exceed (%)", 0, 100, int(base["max_stale_pct"]))
    with st.expander("Tax and withdrawal (ledger capital)"):
        cfg["tax_pct"] = st.number_input("Short-term gains tax (%)", 0.0, 50.0, float(base["tax_pct"]))
        cfg["cess_pct"] = st.number_input("Cess (%)", 0.0, 10.0, float(base["cess_pct"]))
        cfg["withdraw_pct"] = st.number_input("Withdraw from profit (%)", 0.0, 100.0, float(base["withdraw_pct"]))
    st.divider()
    sync = st.toggle("Sync ledger with local CSV files", value=False,
                     help="Local use only: reads and writes holdings.csv / sold.csv next to app.py. "
                          "Leave off when hosted.")
    st.caption("Educational tool, not investment advice. Backtests do not guarantee future results.")

if "holdings" not in st.session_state:
    st.session_state.holdings = pd.DataFrame(columns=sc.HOLD_COLS)
    st.session_state.sold = pd.DataFrame(columns=sc.SOLD_COLS)
if sync and not st.session_state.get("loaded_local"):
    st.session_state.holdings = clean_hold(sc.load_csv(sc.HOLDINGS, sc.HOLD_COLS))
    st.session_state.sold = clean_sold(sc.load_csv(sc.SOLD, sc.SOLD_COLS))
    st.session_state.loaded_local = True
if not sync:
    st.session_state.loaded_local = False


def persist():
    if sync:
        st.session_state.holdings.to_csv(sc.HOLDINGS, index=False)
        st.session_state.sold.to_csv(sc.SOLD, index=False)


tab_scan, tab_ledger, tab_bt, tab_uni, tab_help = st.tabs(
    ["Today's scan", "Ledger", "Backtest", "Universe", "How it works"])

# ------------------------------------------------------------------ scan

with tab_scan:
    c1, c2 = st.columns([4, 1])
    c1.subheader("Today's signals")
    if c2.button("Refresh prices", width="stretch"):
        get_closes.clear()
    closes, bad = get_closes(symbols)
    hold, sold = clean_hold(st.session_state.holdings), clean_sold(st.session_state.sold)
    r = sc.compute_scan(cfg, uni, hold, sold, closes, bad)

    m = st.columns(6)
    m[0].metric("Data as of", str(r["asof"]))
    m[1].metric("Capital", f"₹{r['total']:,.0f}")
    m[2].metric("Invested", f"₹{r['invested']:,.0f}")
    m[3].metric("Free cash", f"₹{r['free_cash']:,.0f}")
    m[4].metric("Per buy", f"₹{r['per_buy']:,.0f}")
    m[5].metric("Holding", f"{r['held']}/{r['max_etfs']}")
    st.caption(f"Coverage {r['covered']}/{r['universe']} ETFs"
               + (f" · missing: {', '.join(r['bad'])}" if r["bad"] else "")
               + f" · breakouts {r['breakouts']} · breakdowns {r['breakdowns']}")

    show = True
    if r["problems"]:
        st.error("**Data check failed**\n\n" + "\n".join(f"- {p}" for p in r["problems"])
                 + "\n\nSignals from stale data can be wrong. Try again after the market close "
                   "(prices usually update by evening).")
        if r["stale"]:
            with st.expander(f"{len(r['stale'])} ETFs without the {r['asof']} bar"):
                st.write(", ".join(r["stale"]))
        show = st.checkbox("Show the action list anyway (verify prices with your broker)")
    else:
        st.success("Data check passed.")

    if show:
        acts = r["actions"]
        if acts.empty:
            st.info("No action today.")
        else:
            st.dataframe(acts, hide_index=True, width="stretch", column_config={
                "price": st.column_config.NumberColumn(format="%.2f")})
            with st.expander("Record an action as executed"):
                i = st.selectbox("Action", range(len(acts)), format_func=lambda i: (
                    f"{acts.loc[i, 'action']} {acts.loc[i, 'symbol']} {acts.loc[i, 'lot']} "
                    f"{acts.loc[i, 'qty']} @ {acts.loc[i, 'price']:.2f}"))
                row = acts.loc[i]
                if row["action"] == "SKIP":
                    st.caption("SKIP rows are informational.")
                else:
                    a, b_ = st.columns(2)
                    price = a.number_input("Fill price", value=float(row["price"]), step=0.05, key="scan_px")
                    qty = b_.number_input("Quantity", value=int(row["qty"]), min_value=1, key="scan_qty")
                    if st.button("Record trade"):
                        if row["action"] in ("BUY", "AVERAGE"):
                            st.session_state.holdings = sc.apply_buy(
                                hold, row["symbol"], price, qty, date.today())
                        else:
                            mt = re.match(r"lot (\d+)", str(row["lot"]))
                            h2, s2, _ = sc.apply_sell(hold, sold, cfg, row["symbol"], price, date.today(),
                                                      int(mt.group(1)) if mt else None)
                            st.session_state.holdings, st.session_state.sold = h2, s2
                        persist()
                        st.success("Recorded. See the Ledger tab.")
                        st.rerun()

    with st.expander("All ETF signals", expanded=False):
        t = r["table"]
        only = st.checkbox("Only breakouts, breakdowns and holdings", value=True)
        if only and not t.empty:
            t = t[(t["signal"] != "") | t["held"]]
        st.dataframe(t, hide_index=True, width="stretch", column_config={
            c: st.column_config.NumberColumn(format="%.2f") for c in
            ("close", "sma", "vs_sma_%", "off_52w_high_%", "1m_%")})

# ------------------------------------------------------------------ ledger

with tab_ledger:
    st.subheader("Open positions")
    hold, sold = clean_hold(st.session_state.holdings), clean_sold(st.session_state.sold)
    if hold.empty:
        st.info("No holdings yet. Record trades below, or upload your CSV files.")
    else:
        rows = []
        for sym, lots in hold.groupby("symbol"):
            avg = lots["amount"].sum() / lots["qty"].sum()
            now = float(closes[sym].iloc[-1]) if sym in closes else float("nan")
            rows.append({"symbol": sym, "lots": len(lots), "qty": int(lots["qty"].sum()), "avg": avg,
                         "now": now, "pnl_%": (now / avg - 1) * 100,
                         "invested": float(lots["amount"].sum())})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            c: st.column_config.NumberColumn(format="%.2f") for c in ("avg", "now", "pnl_%", "invested")})

    a, b_ = st.columns(2)
    with a:
        st.markdown("**Record a buy**")
        with st.form("buy"):
            sym = st.selectbox("ETF", symbols)
            px = st.number_input("Price", min_value=0.01, value=100.0, step=0.05)
            q = st.number_input("Quantity", min_value=1, value=10)
            d = st.date_input("Date", date.today())
            if st.form_submit_button("Add buy"):
                st.session_state.holdings = sc.apply_buy(hold, sym, px, q, d)
                persist()
                st.rerun()
    with b_:
        st.markdown("**Record a sell**")
        held_syms = sorted(hold["symbol"].unique()) if not hold.empty else []
        with st.form("sell"):
            sym = st.selectbox("ETF", held_syms)
            px = st.number_input("Sell price", min_value=0.01, value=100.0, step=0.05)
            lot = st.number_input("Lot # (LIFO only, 0 = newest)", min_value=0, value=0)
            brk = st.number_input("Brokerage + charges (₹)", min_value=0.0, value=0.0)
            d = st.date_input("Date", date.today(), key="sell_date")
            if st.form_submit_button("Add sell") and sym:
                h2, s2, row = sc.apply_sell(hold, sold, cfg, sym, px, d, lot or None, brk)
                st.session_state.holdings, st.session_state.sold = h2, s2
                persist()
                st.rerun()

    st.markdown("**Edit holdings**")
    edited = st.data_editor(hold, num_rows="dynamic", width="stretch", key="hold_editor")
    if st.button("Save holding edits"):
        e = clean_hold(edited)
        e["amount"] = (e["price"] * e["qty"]).round(2)
        st.session_state.holdings = e
        persist()
        st.rerun()

    st.subheader("Closed trades")
    st.dataframe(sold, hide_index=True, width="stretch")
    if not sold.empty:
        st.caption(f"Realised profit ₹{sold['profit'].sum():,.0f} across {len(sold)} trades")

    st.subheader("Import / export")
    st.caption("When hosted, the ledger exists only in this browser session. Download it to keep it.")
    u1, u2 = st.columns(2)
    up_h = u1.file_uploader("holdings.csv", type="csv", key="up_h")
    up_s = u2.file_uploader("sold.csv", type="csv", key="up_s")
    if st.button("Load uploaded files") and (up_h or up_s):
        if up_h:
            st.session_state.holdings = clean_hold(pd.read_csv(up_h))
        if up_s:
            st.session_state.sold = clean_sold(pd.read_csv(up_s))
        persist()
        st.rerun()
    d1, d2 = st.columns(2)
    d1.download_button("Download holdings.csv", hold.to_csv(index=False), "holdings.csv", "text/csv")
    d2.download_button("Download sold.csv", sold.to_csv(index=False), "sold.csv", "text/csv")

# ------------------------------------------------------------------ backtest

with tab_bt:
    st.subheader("Backtest")
    st.caption("Signals use closing prices; fills assumed at that close less trading cost. Uses today's ETF "
               "universe, so results carry survivorship bias.")
    b1, b2, b3, b4 = st.columns(4)
    start = b1.date_input("Start", date(2018, 1, 1), min_value=date(2010, 1, 1))
    cost = b2.number_input("Cost per side (bps)", 0.0, 100.0, 5.0)
    cash_y = b3.number_input("Idle cash yield (% p.a.)", 0.0, 15.0, 6.5, step=0.5,
                             help="Return on uninvested cash, e.g. a liquid ETF.")
    with_tax = b4.checkbox("Apply tax and withdrawal", value=False)
    p = {**bt.DEFAULTS, **cfg, "cost_bps": cost, "cash_yield_pct": cash_y}
    if not with_tax:
        p.update(tax_pct=0, cess_pct=0, withdraw_pct=0)

    run, compare = st.columns(2)
    if run.button("Run backtest", type="primary", width="stretch"):
        hist = get_history(symbols)
        with st.spinner("Simulating..."):
            st.session_state.bt = bt.run_portfolio(p, hist, str(start))
    if compare.button("Compare FIFO vs LIFO (about 1 minute)", width="stretch"):
        hist = get_history(symbols)
        with st.spinner("Running 4 variants..."):
            combos = [{"mode": m_, "cash_yield_pct": y} for y in (0, cash_y) for m_ in ("fifo", "lifo")]
            st.session_state.cmp = bt.run_grid(hist, p, combos, str(start), None)

    res = st.session_state.get("bt")
    if res:
        m = res["metrics"]
        k = st.columns(5)
        k[0].metric("Strategy CAGR", f"{m['cagr_%']:.2f}%")
        k[1].metric("Buy & hold CAGR", f"{res['bench_cagr']:.2f}%",
                    delta=f"{m['cagr_%'] - res['bench_cagr']:+.2f} pts", delta_color="off")
        k[2].metric("Max drawdown", f"{m['max_dd_%']:.1f}%", help=f"Buy & hold: {res['bench_dd']:.1f}%")
        k[3].metric("Avg deployed", f"{m['deployed_%']:.0f}%", help="Of starting capital")
        k[4].metric("Trades", m["trades"])
        st.line_chart(pd.DataFrame({"Strategy": res["equity"], f"Equal-weight buy & hold ({res['n_bench']} ETFs)":
                                    res["bench"]}, index=res["dates"]))
        st.caption(f"{res['years']:.1f} years · {res['n_etfs']} ETFs · longest hold {m['max_hold_d']} days · "
                   f"worst open position {m['worst_open_%']:.1f}% · win rate {m['win_%']:.0f}% "
                   "(the never-sell-at-a-loss rule makes this high by design)")
        with st.expander("Open positions at the end"):
            st.dataframe(res["open"], hide_index=True, width="stretch")
        with st.expander("Closed trades"):
            st.dataframe(res["trades"], hide_index=True, width="stretch")
            st.download_button("Download trades", res["trades"].to_csv(index=False), "backtest_trades.csv")
    cmp_df = st.session_state.get("cmp")
    if cmp_df is not None:
        st.markdown("**FIFO vs LIFO**")
        cols = ["mode", "cash_yield_pct", "cagr_%", "vs_bh_%", "max_dd_%", "deployed_%", "trades",
                "max_hold_d", "worst_open_%", "h1_cagr_%", "h2_cagr_%", "bh_cagr_%"]
        st.dataframe(cmp_df[cols].round(2), hide_index=True, width="stretch")

# ------------------------------------------------------------------ universe

with tab_uni:
    st.subheader(f"Universe: {len(uni)} ETFs in {uni['family'].nunique()} families")
    st.caption("One ETF per underlying, most liquid first; families group ETFs with highly correlated returns.")
    fams = uni.groupby("family").agg(etfs=("symbol", lambda s: ", ".join(s)), count=("symbol", "size")).reset_index()
    st.dataframe(fams, hide_index=True, width="stretch")
    st.dataframe(uni, hide_index=True, width="stretch")

# ------------------------------------------------------------------ help

with tab_help:
    st.markdown(f"""
### Rules
- **Breakout:** the close is below the {cfg['sma_days']}-day SMA, then closes above it for **2 consecutive days**.
- **Breakdown:** above the SMA, then closes below it for 2 consecutive days.
- **Buy** the first lot on a breakout. At most {cfg['max_new_buys_per_day']} new buys a day, ranked by distance below the
  52-week high, and at most {cfg['max_per_family']} per family.
- **Average** on a new breakout only if the price is at least {cfg['avg_drop_pct']}% below the *last* buy.
  Amount = (drop % ÷ {1 if cfg['mode'] == 'lifo' else 2}) × (per-buy ÷ 10).
- **Sell** only at a profit of at least {cfg['target_pct']}%: on a breakdown, or on the first red close of an ETF that
  rose {cfg['fast_move_pct']}% or more in a month. LIFO also sells profitable lots on a red close once capital runs out.
- Never sell at a loss. Reinvest profit after tax.

### Caveats
- A strategy from a third-party video series. This app replicates its published rules; it does not endorse it.
- In our backtests it lagged equal-weight buy-and-hold, and the never-sell-at-a-loss rule can tie up capital for years.
- Yahoo Finance data can be late or wrong, so verify prices with your broker. The scan is blocked when too many ETFs are stale.
- Not investment advice.
""")
