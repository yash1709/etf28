"""28 SMA momentum scanner for NSE ETFs.

Usage:
    py scanner.py scan [--force]             # run after 3:15 PM IST; blocks on stale data unless --force
    py scanner.py status                     # holdings, cash, P&L
    py scanner.py buy SYMBOL PRICE QTY [--date YYYY-MM-DD]
    py scanner.py sell SYMBOL PRICE [--lot N] [--brokerage X] [--date YYYY-MM-DD]

The signal logic lives in compute_scan / apply_buy / apply_sell (pure functions on DataFrames),
shared by this CLI and the Streamlit app (app.py).
"""
import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
CONFIG, UNIVERSE = ROOT / "config.json", ROOT / "etfs.csv"
HOLDINGS, SOLD = ROOT / "holdings.csv", ROOT / "sold.csv"
REPORTS = ROOT / "reports"

HOLD_COLS = ["buy_date", "symbol", "price", "qty", "amount"]
SOLD_COLS = ["symbol", "first_buy_date", "sell_date", "qty", "invested", "sell_price",
             "proceeds", "profit", "profit_pct", "days", "brokerage"]
ACTION_COLS = ["action", "symbol", "lot", "price", "qty", "note"]


def load_cfg():
    return json.loads(CONFIG.read_text())


def load_csv(path, cols):
    if path.exists() and path.stat().st_size:
        return pd.read_csv(path)
    return pd.DataFrame(columns=cols)


def fetch(symbols):
    tickers = [s + ".NS" for s in symbols]
    raw = yf.download(tickers, period="15mo", interval="1d", auto_adjust=False,
                      progress=False, group_by="ticker", threads=True)
    closes, bad = {}, []
    for s, t in zip(symbols, tickers):
        try:
            c = raw[t]["Close"].dropna()
        except KeyError:
            c = pd.Series(dtype=float)
        if len(c) < 60:
            bad.append(s)
        else:
            closes[s] = c
    return closes, bad


def signals(c, cfg):
    sma = c.rolling(cfg["sma_days"]).mean()
    above = (c.iloc[-3:] > sma.iloc[-3:]).tolist()
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    return {
        "date": c.index[-1].date(),
        "close": last,
        "sma": float(sma.iloc[-1]),
        "breakout": (not above[0]) and above[1] and above[2],
        "breakdown": above[0] and (not above[1]) and (not above[2]),
        "red": last < prev,
        "ret_fast": (last / float(c.iloc[-1 - cfg["fast_move_days"]]) - 1) * 100,
        "off_high": (last / float(c.iloc[-252:].max()) - 1) * 100,
    }


def capital_state(cfg, holdings, sold):
    """Total capital = starting capital + reinvested growth (after tax and withdrawal)."""
    growth = 0.0
    for _, r in sold.iterrows():
        net = r["profit"] - (r.get("brokerage", 0) or 0)
        tax = max(net, 0) * cfg["tax_pct"] / 100 * (1 + cfg["cess_pct"] / 100)
        after = net - tax
        growth += after - max(after, 0) * cfg["withdraw_pct"] / 100
    total = cfg["capital"] + growth
    invested = float(holdings["amount"].sum()) if len(holdings) else 0.0
    per_buy = total * (1 - cfg["reserve_pct"] / 100) / cfg["max_etfs"]
    return total, invested, total - invested, per_buy


def compute_scan(cfg, uni, holdings, sold, closes, bad=(), today=None):
    """Return today's signals and recommended actions as a dict (no printing, no file IO).

    `problems` lists failed data checks; callers decide whether to withhold the actions.
    """
    today = today or date.today()
    sig = {s: signals(c, cfg) for s, c in closes.items()}
    under = dict(zip(uni["symbol"], uni["underlying"]))
    fam = dict(zip(uni["symbol"], uni["family"] if "family" in uni else uni["underlying"]))
    total, invested, free_cash, per_buy = capital_state(cfg, holdings, sold)
    cash = free_cash
    lifo = cfg["mode"].lower() == "lifo"
    exhausted = cash < per_buy
    unit = per_buy / 10
    actions, selling = [], set()

    for sym, lots in holdings.groupby("symbol"):
        if sym not in sig:
            continue
        g = sig[sym]
        fast_exit = g["ret_fast"] >= cfg["fast_move_pct"] and g["red"]
        weak = g["breakdown"] or fast_exit or (lifo and exhausted and g["red"])
        why = "fast-mover red candle" if fast_exit else "28 SMA breakdown" if g["breakdown"] \
            else "capital exhausted + red close"
        if lifo:
            for i, (_, lot) in enumerate(lots.sort_values("buy_date").iterrows(), 1):
                pnl = (g["close"] / lot["price"] - 1) * 100
                if weak and pnl >= cfg["target_pct"]:
                    actions.append(("SELL", sym, f"lot {i} ({lot['buy_date']})", g["close"],
                                    int(lot["qty"]), f"{why}, P&L {pnl:+.2f}%"))
                    selling.add(sym)
        else:
            pnl = (g["close"] / (lots["amount"].sum() / lots["qty"].sum()) - 1) * 100
            if weak and pnl >= cfg["target_pct"]:
                actions.append(("SELL", sym, "all lots", g["close"], int(lots["qty"].sum()),
                                f"{why}, P&L {pnl:+.2f}%"))
                selling.add(sym)

    for sym, lots in holdings.groupby("symbol"):
        g = sig.get(sym)
        if not g or not g["breakout"] or sym in selling:
            continue
        last_buy = lots.sort_values("buy_date").iloc[-1]["price"]
        drop = (g["close"] / last_buy - 1) * 100
        if drop > -cfg["avg_drop_pct"]:
            continue
        amount = abs(drop) / (1 if lifo else 2) * unit
        qty = math.floor(min(amount, max(cash, 0)) / g["close"])
        if qty > 0:
            cash -= qty * g["close"]
            actions.append(("AVERAGE", sym, "", g["close"], qty,
                            f"{drop:.1f}% below last buy {last_buy:.2f}, target ₹{amount:,.0f}"))

    held = set(holdings["symbol"])
    slots = cfg["max_etfs"] - len(held)
    fam_cap = cfg.get("max_per_family", 0)
    fam_used = {}
    for sym in held:
        fam_used[fam.get(sym)] = fam_used.get(fam.get(sym), 0) + 1
    fresh = sorted((s for s, g in sig.items() if g["breakout"] and s not in held),
                   key=lambda s: sig[s]["off_high"])
    bought = 0
    for sym in fresh:
        if bought >= cfg["max_new_buys_per_day"]:
            break
        f = fam.get(sym)
        if fam_cap and fam_used.get(f, 0) >= fam_cap:
            actions.append(("SKIP", sym, "", sig[sym]["close"], 0, f"family '{f}' already held"))
            continue
        qty = math.floor(per_buy / sig[sym]["close"])
        if slots <= 0 or qty < 1 or qty * sig[sym]["close"] > cash:
            actions.append(("SKIP", sym, "", sig[sym]["close"], 0,
                            "breakout but no slot/cash"))
            continue
        slots -= 1
        bought += 1
        fam_used[f] = fam_used.get(f, 0) + 1
        cash -= qty * sig[sym]["close"]
        actions.append(("BUY", sym, "", sig[sym]["close"], qty,
                        f"{sig[sym]['off_high']:.1f}% from 52w high, {under.get(sym, '')} [{f}]"))

    asof = max(g["date"] for g in sig.values()) if sig else today
    stale = [s for s, g in sig.items() if g["date"] < asof]
    limit = cfg.get("max_stale_pct", 10)
    last_weekday = today - timedelta(days=1)
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    problems = []
    if sig and 100 * len(stale) / len(sig) > limit:
        problems.append(f"{len(stale)}/{len(sig)} ETFs lack the {asof} bar (limit {limit}%)")
    bad_held = sorted(s for s in held if s in stale or s not in sig)
    if bad_held:
        problems.append(f"held ETFs without current data: {', '.join(bad_held)}")
    if asof < last_weekday:
        problems.append(f"newest bar {asof} is older than the last weekday {last_weekday} (holiday?)")

    table = pd.DataFrame([
        {"symbol": s, "family": fam.get(s), "underlying": under.get(s), "date": g["date"],
         "close": g["close"], "sma": g["sma"], "vs_sma_%": (g["close"] / g["sma"] - 1) * 100,
         "off_52w_high_%": g["off_high"], "1m_%": g["ret_fast"],
         "signal": "BREAKOUT" if g["breakout"] else "BREAKDOWN" if g["breakdown"] else "",
         "held": s in held}
        for s, g in sig.items()]).sort_values("off_52w_high_%") if sig else pd.DataFrame()
    return {
        "asof": asof, "total": total, "invested": invested, "free_cash": free_cash,
        "planned_cash": cash, "per_buy": per_buy, "held": len(held), "max_etfs": cfg["max_etfs"],
        "covered": len(sig), "universe": len(uni), "bad": list(bad), "stale": stale,
        "problems": problems, "breakouts": sum(g["breakout"] for g in sig.values()),
        "breakdowns": sum(g["breakdown"] for g in sig.values()),
        "actions": pd.DataFrame(actions, columns=ACTION_COLS), "table": table,
    }


def apply_buy(holdings, symbol, price, qty, buy_date):
    row = {"buy_date": str(buy_date), "symbol": symbol.upper(), "price": float(price),
           "qty": int(qty), "amount": round(float(price) * int(qty), 2)}
    return pd.concat([holdings, pd.DataFrame([row])], ignore_index=True)


def apply_sell(holdings, sold, cfg, symbol, price, sell_date, lot=None, brokerage=0.0):
    """Sell all lots (FIFO) or one lot (LIFO, default newest). Returns (holdings, sold, row)."""
    sym = symbol.upper()
    lots = holdings[holdings["symbol"] == sym].sort_values("buy_date")
    if lots.empty:
        raise ValueError(f"No holding for {sym}")
    if cfg["mode"].lower() == "lifo":
        lots = lots.iloc[[(lot or len(lots)) - 1]]
    invested, qty = float(lots["amount"].sum()), int(lots["qty"].sum())
    proceeds = float(price) * qty
    profit = proceeds - invested
    first = lots["buy_date"].iloc[0]
    row = {"symbol": sym, "first_buy_date": first, "sell_date": str(sell_date), "qty": qty,
           "invested": round(invested, 2), "sell_price": float(price), "proceeds": round(proceeds, 2),
           "profit": round(profit, 2), "profit_pct": round(profit / invested * 100, 2),
           "days": (pd.Timestamp(sell_date) - pd.Timestamp(first)).days, "brokerage": brokerage}
    return (holdings.drop(lots.index).reset_index(drop=True),
            pd.concat([sold, pd.DataFrame([row])], ignore_index=True), row)


def scan(cfg, force=False):
    uni = pd.read_csv(UNIVERSE)
    holdings, sold = load_csv(HOLDINGS, HOLD_COLS), load_csv(SOLD, SOLD_COLS)
    closes, bad = fetch(uni["symbol"].tolist())
    r = compute_scan(cfg, uni, holdings, sold, closes, bad)
    print(f"\n28 SMA scan | data as of {r['asof']} | mode {cfg['mode'].upper()}")
    print(f"Capital ₹{r['total']:,.0f} | invested ₹{r['invested']:,.0f} | free cash ₹{r['free_cash']:,.0f} | "
          f"per buy ₹{r['per_buy']:,.0f} | holding {r['held']}/{r['max_etfs']}")
    print(f"Data coverage {r['covered']}/{r['universe']} ETFs"
          + (f" | missing: {', '.join(r['bad'])}" if r["bad"] else ""))
    if r["stale"]:
        print(f"WARNING {len(r['stale'])} ETFs not updated to {r['asof']}: {', '.join(r['stale'])}")
    if r["problems"]:
        print("\nDATA CHECK FAILED:\n  - " + "\n  - ".join(r["problems"]))
        if not force:
            print("\nAction list withheld, because signals from stale data can be wrong.")
            print("Re-run after Yahoo updates (usually after market close), or use --force to see it anyway.")
            sys.exit(2)
        print("\n--force: showing signals computed from incomplete data. Verify prices with your broker.")

    print(f"Breakouts today: {r['breakouts']} | breakdowns: {r['breakdowns']}\n")
    if len(r["actions"]):
        print(r["actions"].to_string(index=False, formatters={"price": "{:.2f}".format}))
        REPORTS.mkdir(exist_ok=True)
        r["actions"].to_csv(REPORTS / f"scan_{r['asof']}.csv", index=False)
    else:
        print("No action today.")
    print("\nPlace orders after 3:15 PM, then record them with the buy/sell commands.")


def status(cfg):
    holdings, sold = load_csv(HOLDINGS, HOLD_COLS), load_csv(SOLD, SOLD_COLS)
    total, invested, cash, per_buy = capital_state(cfg, holdings, sold)
    print(f"Capital ₹{total:,.0f} | invested ₹{invested:,.0f} | free ₹{cash:,.0f} | "
          f"per buy ₹{per_buy:,.0f} | realised ₹{sold['profit'].sum():,.0f}")
    if holdings.empty:
        print("No holdings.")
        return
    closes, _ = fetch(holdings["symbol"].unique().tolist())
    rows = []
    for sym, lots in holdings.groupby("symbol"):
        avg = lots["amount"].sum() / lots["qty"].sum()
        cmp_ = float(closes[sym].iloc[-1]) if sym in closes else float("nan")
        rows.append((sym, int(lots["qty"].sum()), round(avg, 2), round(cmp_, 2),
                     round((cmp_ / avg - 1) * 100, 2), len(lots)))
    print(pd.DataFrame(rows, columns=["symbol", "qty", "avg", "cmp", "pnl_%", "lots"]).to_string(index=False))


def record_buy(a):
    h = apply_buy(load_csv(HOLDINGS, HOLD_COLS), a.symbol, a.price, a.qty, a.date)
    h.to_csv(HOLDINGS, index=False)
    print(f"Recorded buy {a.symbol.upper()} {a.qty} @ {a.price}")


def record_sell(a, cfg):
    try:
        h, s, row = apply_sell(load_csv(HOLDINGS, HOLD_COLS), load_csv(SOLD, SOLD_COLS), cfg,
                               a.symbol, a.price, a.date, a.lot, a.brokerage)
    except ValueError as e:
        sys.exit(str(e))
    s.to_csv(SOLD, index=False)
    h.to_csv(HOLDINGS, index=False)
    print(f"Recorded sell {row['symbol']} {row['qty']} @ {a.price} | profit ₹{row['profit']:,.0f} ({row['profit_pct']}%)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--force", action="store_true", help="show actions even if the data check fails")
    sub.add_parser("status")
    b = sub.add_parser("buy")
    b.add_argument("symbol")
    b.add_argument("price", type=float)
    b.add_argument("qty", type=int)
    b.add_argument("--date", default=date.today().isoformat())
    s = sub.add_parser("sell")
    s.add_argument("symbol")
    s.add_argument("price", type=float)
    s.add_argument("--lot", type=int)
    s.add_argument("--brokerage", type=float, default=0)
    s.add_argument("--date", default=date.today().isoformat())
    a = p.parse_args()
    cfg = load_cfg()
    {"scan": lambda: scan(cfg, a.force), "status": lambda: status(cfg),
     "buy": lambda: record_buy(a), "sell": lambda: record_sell(a, cfg)}[a.cmd]()


if __name__ == "__main__":
    main()
