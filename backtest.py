"""Backtester and sensitivity grid for the 28 SMA momentum strategy.

Usage:
    py backtest.py single NIFTYBEES --start 2016-04-01      # replicates the video's single-ETF test
    py backtest.py portfolio --start 2018-01-01             # multi-ETF, capital-limited
    py backtest.py grid --start 2018-01-01                  # standard robustness sweeps
    py backtest.py grid --vary sma_days=20,28,36 --vary target_pct=2,3.14,5
    py backtest.py compare [--starts 2018-01-01,2020-01-01] [--yield-pct 6.5]   # FIFO vs LIFO, with/without cash yield

Common options: --end DATE, --set key=value (any config.json key, plus cost_bps),
--tax (apply tax and withdrawal; default is gross), --refresh (re-download prices).
Signals use the close; fills are assumed at that close, less cost_bps per side.
"""
import argparse
import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
DATA, REPORTS = ROOT / "data", ROOT / "reports"
DEFAULTS = {**json.loads((ROOT / "config.json").read_text()), "cost_bps": 5, "min_history": 252,
            "cash_yield_pct": 0}


# ---------------------------------------------------------------- data

def clean(c, name):
    """Drop one-day spikes that reverse next day (bad ticks); warn on unreversed big jumps."""
    spike = (c / c.rolling(7, center=True, min_periods=3).median() - 1).abs() > 0.4
    if spike.any():
        print(f"data: dropped {int(spike.sum())} bad tick(s) in {name}: "
              f"{', '.join(str(d.date()) for d in c.index[spike])}")
    c = c[~spike]
    jump = (c / c.shift(1) - 1).abs() > 0.35
    if jump.any():
        print(f"data: WARNING {name} has >35% daily move(s) on "
              f"{', '.join(str(d.date()) for d in c.index[jump])} (unadjusted split?)")
    return c


def load_prices(symbols, refresh=False):
    DATA.mkdir(exist_ok=True)
    cache = DATA / "prices.pkl"
    old = pd.read_pickle(cache) if cache.exists() else pd.DataFrame()
    need = list(symbols) if refresh else [s for s in symbols if s not in old.columns]
    if need:
        import yfinance as yf
        raw = yf.download([s + ".NS" for s in need], period="max", interval="1d",
                          auto_adjust=False, progress=False, group_by="ticker", threads=True)
        cols = {}
        for s in need:
            try:
                c = raw[s + ".NS"]["Close"].dropna()
            except KeyError:
                continue
            if len(c):
                cols[s] = c
        old = pd.concat([old.drop(columns=list(cols), errors="ignore"), pd.DataFrame(cols)], axis=1, sort=False).sort_index()
        old.to_pickle(cache)
    have = [s for s in symbols if s in old.columns]
    return pd.DataFrame({s: clean(old[s].dropna(), s) for s in have}).sort_index()


def calendar(close):
    """Keep days when at least half of the already-listed ETFs traded; fill short gaps."""
    started = close.notna().cumsum() > 0
    keep = close.notna().sum(axis=1) >= 0.5 * started.sum(axis=1).clip(lower=1)
    close = close[keep]
    started = started[keep]
    holes = (close.isna() & started).sum() / started.sum().clip(lower=1)
    for s in holes[holes > 0.03].index:
        print(f"data: excluded {s}: {holes[s] * 100:.0f}% of days missing after listing")
    return close.drop(columns=list(holes[holes > 0.03].index)).ffill(limit=5)


def load_families():
    uni = pd.read_csv(ROOT / "etfs.csv")
    return dict(zip(uni["symbol"], uni["family"] if "family" in uni else uni["underlying"]))


def prep(close, n, fast_days, min_hist, fam=None):
    fam = fam or load_families()
    sma = close.rolling(n).mean()
    ab = close > sma
    bo = (~ab.shift(2, fill_value=False)) & ab.shift(1, fill_value=False) & ab & sma.shift(2).notna()
    bd = ab.shift(2, fill_value=False) & (~ab.shift(1, fill_value=True)) & (~ab) & sma.notna()
    eligible = close.notna().cumsum() >= min_hist
    return {
        "dates": close.index, "symbols": list(close.columns), "C": close.values,
        "FAM": [fam.get(s, s) for s in close.columns],
        "BO": (bo & eligible).values, "BD": bd.values,
        "RED": (close < close.shift(1)).values,
        "FAST": ((close / close.shift(fast_days) - 1) * 100).values,
        "OFF": ((close / close.rolling(252, min_periods=60).max() - 1) * 100).values,
    }


# ---------------------------------------------------------------- simulation

def simulate(P, p, i0, i1, pocket=False):
    """pocket=True mimics the video's single-ETF test: buys are never cash-limited and any
    shortfall is injected from outside (tracked as a cash flow)."""
    C, BO, BD, RED, FAST, OFF = (P[k] for k in ("C", "BO", "BD", "RED", "FAST", "OFF"))
    cap0, cost, lifo = p["capital"], p["cost_bps"] / 1e4, p["mode"].lower() == "lifo"
    cash = 0.0 if pocket else float(cap0)
    invested = realized = 0.0
    lots, flows, trades = {}, [], []
    n = i1 - i0 + 1
    eq, inv, npos, unreal = np.zeros(n), np.zeros(n), np.zeros(n, int), np.zeros(n)

    def per_buy():
        if pocket:
            return cap0 + realized
        return (cash + invested) * (1 - p["reserve_pct"] / 100) / p["max_etfs"]

    def buy(t, s, qty):
        nonlocal cash, invested
        px = C[t, s]
        amt = qty * px * (1 + cost)
        if pocket and cash < amt:
            flows.append((t, -(amt - cash)))
            cash = 0.0
        else:
            cash -= amt
        lots.setdefault(s, []).append([t, px * (1 + cost), qty, amt, px])
        invested += amt

    def sell(t, s, chosen):
        nonlocal cash, invested, realized
        qty, amt = sum(l[2] for l in chosen), sum(l[3] for l in chosen)
        proceeds = qty * C[t, s] * (1 - cost)
        profit = proceeds - amt
        tax = max(profit, 0) * p["tax_pct"] / 100 * (1 + p["cess_pct"] / 100)
        after = profit - tax
        wd = max(after, 0) * p["withdraw_pct"] / 100
        cash += proceeds - tax - wd
        realized += after - wd
        invested -= amt
        if wd:
            flows.append((t, wd))
        trades.append((P["symbols"][s], min(l[0] for l in chosen), t, qty, amt, proceeds,
                       profit, profit / amt * 100))
        ids = {id(l) for l in chosen}
        lots[s] = [l for l in lots[s] if id(l) not in ids]
        if not lots[s]:
            del lots[s]

    cy = 0.0 if pocket else p["cash_yield_pct"] / 100
    for t in range(i0, i1 + 1):
        if cy and t > i0 and cash > 0:
            cash *= (1 + cy) ** ((P["dates"][t] - P["dates"][t - 1]).days / 365)
        pb = per_buy()
        exhausted = (not pocket) and cash < pb
        sold_today = set()

        for s in sorted(lots):
            px = C[t, s]
            if np.isnan(px):
                continue
            fast_exit = FAST[t, s] >= p["fast_move_pct"] and RED[t, s]
            weak = BD[t, s] or fast_exit or (lifo and exhausted and RED[t, s])
            if not weak:
                continue
            sp = px * (1 - cost)
            if lifo:
                for l in [l for l in lots[s] if (sp / l[1] - 1) * 100 >= p["target_pct"]]:
                    sell(t, s, [l])
                    sold_today.add(s)
            else:
                avg = sum(l[3] for l in lots[s]) / sum(l[2] for l in lots[s])
                if (sp / avg - 1) * 100 >= p["target_pct"]:
                    sell(t, s, list(lots[s]))
                    sold_today.add(s)

        pb = per_buy()
        unit = (cap0 if pocket else pb) / 10
        for s in sorted(lots):
            if s in sold_today or not BO[t, s]:
                continue
            drop = (C[t, s] / lots[s][-1][4] - 1) * 100
            if drop > -p["avg_drop_pct"]:
                continue
            amount = abs(drop) / (1 if lifo else 2) * unit
            budget = amount if pocket else min(amount, max(cash, 0))
            qty = math.floor(budget / (C[t, s] * (1 + cost)))
            if qty > 0:
                buy(t, s, qty)

        slots = p["max_etfs"] - len(lots)
        fam_used = {}
        for s in lots:
            fam_used[P["FAM"][s]] = fam_used.get(P["FAM"][s], 0) + 1
        cands = [s for s in np.nonzero(BO[t])[0] if s not in lots and not np.isnan(C[t, s])]
        bought = 0
        for s in sorted(cands, key=lambda s: OFF[t, s]):
            f = P["FAM"][s]
            if bought >= p["max_new_buys_per_day"]:
                break
            if p.get("max_per_family") and fam_used.get(f, 0) >= p["max_per_family"]:
                continue
            qty = math.floor(pb / (C[t, s] * (1 + cost)))
            if slots <= 0 or qty < 1 or (not pocket and qty * C[t, s] * (1 + cost) > cash):
                continue
            buy(t, s, qty)
            slots -= 1
            bought += 1
            fam_used[f] = fam_used.get(f, 0) + 1

        k = t - i0
        value = sum(l[2] * C[t, s] for s, ls in lots.items() for l in ls)
        invested = sum(l[3] for ls in lots.values() for l in ls)
        eq[k], inv[k], npos[k] = cash + value, invested, len(lots)
        unreal[k] = (value / invested - 1) * 100 if invested else 0.0

    end_val = cash + sum(l[2] * C[i1, s] * (1 - cost) for s, ls in lots.items() for l in ls)
    flows = ([] if pocket else [(i0, -float(cap0))]) + flows + [(i1, end_val)]
    return {"eq": eq, "inv": inv, "npos": npos, "unreal": unreal, "trades": trades,
            "flows": flows, "final": end_val, "open": {P["symbols"][s]: ls for s, ls in lots.items()}}


# ---------------------------------------------------------------- metrics

def xirr(flows, dates):
    pts = [(dates[t], a) for t, a in flows]
    if not any(a < 0 for _, a in pts) or not any(a > 0 for _, a in pts):
        return float("nan")
    t0 = min(d for d, _ in pts)
    yrs = np.array([(d - t0).days / 365 for d, _ in pts])
    amt = np.array([a for _, a in pts])
    npv = lambda r: float((amt / (1 + r) ** yrs).sum())
    lo, hi = -0.95, 10.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(lo) * npv(mid) > 0 else (lo, mid)
    return (lo + hi) / 2 * 100


def max_dd(eq):
    return float((eq / np.maximum.accumulate(eq) - 1).min() * 100)


def benchmark(P, cap0, i0, i1):
    C = P["C"]
    ok = ~np.isnan(C[i0])
    shares = cap0 / ok.sum() / C[i0, ok]
    eq = (C[i0:i1 + 1, ok] * shares).sum(axis=1)
    return eq, ok.sum()


def metrics(P, p, res, i0, i1, pocket=False):
    dates = P["dates"]
    yrs = max((dates[i1] - dates[i0]).days / 365.25, 1e-9)
    tr = res["trades"]
    hold_days = [(dates[x[2]] - dates[x[1]]).days for x in tr]
    open_age = [(dates[i1] - dates[min(l[0] for l in ls)]).days for ls in res["open"].values()]
    m = {
        "xirr_%": xirr(res["flows"], dates),
        "trades": len(tr),
        "win_%": 100 * np.mean([x[7] > 0 for x in tr]) if tr else float("nan"),
        "avg_hold_d": float(np.mean(hold_days)) if hold_days else float("nan"),
        "max_hold_d": max(hold_days + open_age) if (hold_days or open_age) else 0,
        "avg_invested": float(res["inv"].mean()),
        "max_invested": float(res["inv"].max()),
        "worst_open_%": float(res["unreal"].min()),
        "end_open_%": float(res["unreal"][-1]),
        "max_pos": int(res["npos"].max()),
    }
    if pocket:
        injected = -sum(a for _, a in res["flows"] if a < 0)
        m["injected"] = injected
        m["profit"] = res["final"] - injected
        m["profit_on_avg_inv_%"] = m["profit"] / m["avg_invested"] * 100 if m["avg_invested"] else float("nan")
    else:
        cap0 = p["capital"]
        m["cagr_%"] = ((res["final"] / cap0) ** (1 / yrs) - 1) * 100
        m["max_dd_%"] = max_dd(res["eq"])
        m["deployed_%"] = res["inv"].mean() / cap0 * 100
        m["ret_on_deployed_%"] = (res["final"] - cap0) / res["inv"].mean() / yrs * 100 \
            if res["inv"].mean() else float("nan")
    return m


# ---------------------------------------------------------------- commands

def setup(args, symbols=None):
    p = dict(DEFAULTS)
    if not args.tax:
        p.update(tax_pct=0, cess_pct=0, withdraw_pct=0)
    for kv in args.set or []:
        k, v = kv.split("=")
        p[k] = json.loads(v) if v.replace(".", "").replace("-", "").isdigit() else v
    uni = pd.read_csv(ROOT / "etfs.csv")["symbol"].tolist()
    close = calendar(load_prices(symbols or uni, args.refresh))
    return p, close


def window(P, args):
    d = P["dates"]
    i0 = int(d.searchsorted(pd.Timestamp(args.start)))
    i1 = int(d.searchsorted(pd.Timestamp(args.end), side="right")) - 1 if args.end else len(d) - 1
    return i0, i1


def plot(name, dates, curves):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    REPORTS.mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))
    for label, y in curves.items():
        ax.plot(dates, y, label=label)
    ax.legend()
    ax.grid(alpha=.3)
    fig.savefig(REPORTS / f"{name}.png", dpi=110, bbox_inches="tight")
    print(f"chart: reports/{name}.png")


def save_trades(name, P, res):
    REPORTS.mkdir(exist_ok=True)
    rows = [(s, P["dates"][a].date(), P["dates"][b].date(), q, round(i, 2), round(o, 2),
             round(pr, 2), round(pc, 2)) for s, a, b, q, i, o, pr, pc in res["trades"]]
    pd.DataFrame(rows, columns=["symbol", "first_buy", "sell", "qty", "invested", "proceeds",
                                "profit", "profit_%"]).to_csv(REPORTS / f"{name}_trades.csv", index=False)


def show(title, m):
    print(f"\n{title}")
    for k, v in m.items():
        print(f"  {k:<22}{v:>14,.2f}" if isinstance(v, (float, np.floating)) else f"  {k:<22}{v:>14}")


def cmd_single(args):
    sym = args.symbol.upper()
    p, close = setup(args, [sym])
    p.update(capital=args.capital, max_etfs=1)
    P = prep(close, p["sma_days"], p["fast_move_days"], p["min_history"])
    i0, i1 = window(P, args)
    res = simulate(P, p, i0, i1, pocket=True)
    m = metrics(P, p, res, i0, i1, pocket=True)
    d = P["dates"]
    yrs = (d[i1] - d[i0]).days / 365.25
    bh = P["C"][i1, 0] / P["C"][i0, 0] - 1
    print(f"{sym} {d[i0].date()} -> {d[i1].date()} ({yrs:.1f} yrs), base ₹{args.capital:,.0f}, "
          f"{p['mode'].upper()}, cost {p['cost_bps']} bps/side")
    show("Strategy", m)
    print(f"\nBuy & hold lumpsum:  total {bh * 100:,.1f}%  |  CAGR {((1 + bh) ** (1 / yrs) - 1) * 100:.2f}%  "
          f"|  profit ₹{args.capital * bh:,.0f}")
    print(f"Strategy profit ₹{m['profit']:,.0f} on avg invested ₹{m['avg_invested']:,.0f}; "
          f"XIRR {m['xirr_%']:.2f}% vs buy&hold CAGR {((1 + bh) ** (1 / yrs) - 1) * 100:.2f}%")
    save_trades(f"single_{sym}", P, res)


def cmd_portfolio(args):
    p, close = setup(args)
    P = prep(close, p["sma_days"], p["fast_move_days"], p["min_history"])
    i0, i1 = window(P, args)
    res = simulate(P, p, i0, i1)
    m = metrics(P, p, res, i0, i1)
    beq, nb = benchmark(P, p["capital"], i0, i1)
    yrs = (P["dates"][i1] - P["dates"][i0]).days / 365.25
    print(f"Portfolio {P['dates'][i0].date()} -> {P['dates'][i1].date()} ({yrs:.1f} yrs) | "
          f"{len(P['symbols'])} ETFs | {p['mode'].upper()} | capital ₹{p['capital']:,.0f} | "
          f"max {p['max_etfs']} ETFs | reserve {p['reserve_pct']}%")
    show("Strategy", m)
    print(f"\nBenchmark: equal-weight buy&hold of {nb} ETFs listed at start, same capital")
    print(f"  CAGR {((beq[-1] / p['capital']) ** (1 / yrs) - 1) * 100:.2f}% | "
          f"max drawdown {max_dd(beq):.1f}%")
    print("\nOpen positions at end:")
    for s, ls in res["open"].items():
        q, a = sum(l[2] for l in ls), sum(l[3] for l in ls)
        print(f"  {s:<12} lots {len(ls)} | avg {a / q:,.2f} | now {P['C'][i1, P['symbols'].index(s)]:,.2f} | "
              f"{(P['C'][i1, P['symbols'].index(s)] * q / a - 1) * 100:+.1f}%")
    save_trades("portfolio", P, res)
    plot("portfolio_equity", P["dates"][i0:i1 + 1], {"Strategy equity": res["eq"], "Equal-weight buy&hold": beq})


def run_portfolio(p, close, start, end=None):
    """Portfolio backtest returning data (no printing/files); used by the Streamlit app."""
    P = prep(close, p["sma_days"], p["fast_move_days"], p["min_history"])
    d = P["dates"]
    i0 = int(d.searchsorted(pd.Timestamp(start)))
    i1 = int(d.searchsorted(pd.Timestamp(end), side="right")) - 1 if end else len(d) - 1
    res = simulate(P, p, i0, i1)
    m = metrics(P, p, res, i0, i1)
    beq, nb = benchmark(P, p["capital"], i0, i1)
    yrs = (d[i1] - d[i0]).days / 365.25
    trades = pd.DataFrame(
        [(s, d[a].date(), d[b].date(), q, i, o, pr, pc) for s, a, b, q, i, o, pr, pc in res["trades"]],
        columns=["symbol", "first_buy", "sell", "qty", "invested", "proceeds", "profit", "profit_%"])
    open_pos = []
    for s, ls in res["open"].items():
        q, a = sum(l[2] for l in ls), sum(l[3] for l in ls)
        now = float(P["C"][i1, P["symbols"].index(s)])
        open_pos.append({"symbol": s, "lots": len(ls), "avg": a / q, "now": now, "pnl_%": (now * q / a - 1) * 100})
    return {"metrics": m, "dates": d[i0:i1 + 1], "equity": res["eq"], "bench": beq, "n_bench": int(nb),
            "bench_cagr": ((beq[-1] / p["capital"]) ** (1 / yrs) - 1) * 100, "bench_dd": max_dd(beq),
            "years": yrs, "trades": trades, "open": pd.DataFrame(open_pos), "n_etfs": len(P["symbols"])}


def run_grid(close, base, combos, i0_date, i1_date):
    cache, rows = {}, []
    for combo in combos:
        p = {**base, **combo}
        key = (p["sma_days"], p["fast_move_days"])
        if key not in cache:
            cache[key] = prep(close, p["sma_days"], p["fast_move_days"], p["min_history"])
        P = cache[key]
        d = P["dates"]
        i0 = int(d.searchsorted(pd.Timestamp(i0_date)))
        i1 = int(d.searchsorted(pd.Timestamp(i1_date), side="right")) - 1 if i1_date else len(d) - 1
        mid = (i0 + i1) // 2
        full = metrics(P, p, simulate(P, p, i0, i1), i0, i1)
        h1 = metrics(P, p, simulate(P, p, i0, mid), i0, mid)
        h2 = metrics(P, p, simulate(P, p, mid + 1, i1), mid + 1, i1)
        beq, _ = benchmark(P, p["capital"], i0, i1)
        yrs = (d[i1] - d[i0]).days / 365.25
        bh = ((beq[-1] / p["capital"]) ** (1 / yrs) - 1) * 100
        rows.append({**combo, "cagr_%": full["cagr_%"], "vs_bh_%": full["cagr_%"] - bh,
                     "max_dd_%": full["max_dd_%"], "deployed_%": full["deployed_%"],
                     "ret_on_dep_%": full["ret_on_deployed_%"], "trades": full["trades"],
                     "avg_hold_d": full["avg_hold_d"], "max_hold_d": full["max_hold_d"],
                     "worst_open_%": full["worst_open_%"], "end_open_%": full["end_open_%"],
                     "h1_cagr_%": h1["cagr_%"], "h2_cagr_%": h2["cagr_%"], "bh_cagr_%": bh})
    return pd.DataFrame(rows)


def cmd_compare(args):
    p, close = setup(args)
    cols = ["start", "mode", "cash_yield_pct", "cagr_%", "vs_bh_%", "max_dd_%", "deployed_%", "trades",
            "avg_hold_d", "max_hold_d", "worst_open_%", "end_open_%", "h1_cagr_%", "h2_cagr_%", "bh_cagr_%"]
    out = []
    for start in args.starts.split(","):
        combos = [{"mode": m, "cash_yield_pct": y} for y in (0, args.yield_pct) for m in ("fifo", "lifo")]
        out.append(run_grid(close, p, combos, start, args.end).assign(start=start))
    df = pd.concat(out)[cols]
    pd.set_option("display.width", 250)
    print(df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    REPORTS.mkdir(exist_ok=True)
    df.to_csv(REPORTS / "compare_fifo_lifo.csv", index=False)
    print("\nsaved reports/compare_fifo_lifo.csv")


def cmd_grid(args):
    p, close = setup(args)
    if args.vary:
        axes = {}
        for v in args.vary:
            k, vals = v.split("=")
            axes[k] = [float(x) if "." in x else int(x) for x in vals.split(",")]
        combos = [dict(zip(axes, c)) for c in itertools.product(*axes.values())]
        sweeps = {"custom": combos}
    else:
        sweeps = {
            "sma_days": [{"sma_days": v} for v in range(10, 62, 2)],
            "target_pct": [{"target_pct": v} for v in (0, 1, 2, 3.14, 4, 5, 7, 10)],
            "avg_drop_pct": [{"avg_drop_pct": v} for v in (3, 5, 7, 10, 15)],
            "reserve_pct": [{"reserve_pct": v} for v in (10, 20, 30, 40, 50)],
            "sma x target": [{"sma_days": a, "target_pct": b}
                             for a in (20, 24, 28, 32, 36, 50) for b in (2, 3.14, 5)],
        }
    REPORTS.mkdir(exist_ok=True)
    out = []
    pd.set_option("display.width", 220)
    for name, combos in sweeps.items():
        df = run_grid(close, p, combos, args.start, args.end)
        out.append(df.assign(sweep=name))
        print(f"\n=== {name} ===")
        print(df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        if name == "sma_days":
            r = df.set_index("sma_days")["cagr_%"]
            print(f"\n28 SMA CAGR {r.loc[28]:.2f}% | rank {int(r.rank(ascending=False).loc[28])}/{len(r)} | "
                  f"median of all {r.median():.2f}% | best {r.idxmax()} at {r.max():.2f}%")
    pd.concat(out).to_csv(REPORTS / "grid_results.csv", index=False)
    print("\nsaved reports/grid_results.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--start", default="2018-01-01")
    common.add_argument("--end")
    common.add_argument("--set", action="append")
    common.add_argument("--tax", action="store_true")
    common.add_argument("--refresh", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("single", parents=[common])
    s.add_argument("symbol")
    s.add_argument("--capital", type=float, default=15000)
    sub.add_parser("portfolio", parents=[common])
    g = sub.add_parser("grid", parents=[common])
    g.add_argument("--vary", action="append")
    c = sub.add_parser("compare", parents=[common])
    c.add_argument("--starts", default="2018-01-01,2020-01-01,2022-01-01")
    c.add_argument("--yield-pct", dest="yield_pct", type=float, default=6.5)
    args = ap.parse_args()
    {"single": cmd_single, "portfolio": cmd_portfolio, "grid": cmd_grid,
     "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    main()
