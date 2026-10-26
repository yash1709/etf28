"""Build etfs.csv from data/candidates.csv by validating each ETF on Yahoo data.

Keeps ETFs with enough history, fresh and complete data, and adequate traded value,
then picks the most liquid ETF per underlying. Run: py build_universe.py [--min-value-lakh 25]
Candidate symbols come from a third-party directory, so anything Yahoo can't confirm is dropped.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent
CAND, OUT, REPORT = ROOT / "data" / "candidates.csv", ROOT / "etfs.csv", ROOT / "reports" / "universe_report.csv"


def download(symbols, chunk=40):
    close, volume = {}, {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        raw = yf.download([s + ".NS" for s in part], period="max", interval="1d", auto_adjust=False,
                          progress=False, group_by="ticker", threads=True)
        for s in part:
            try:
                df = raw[s + ".NS"][["Close", "Volume"]].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(df):
                close[s], volume[s] = df["Close"], df["Volume"]
        print(f"downloaded {min(i + chunk, len(symbols))}/{len(symbols)}", flush=True)
    return close, volume


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-value-lakh", type=float, default=25, help="min median daily traded value, ₹ lakh")
    ap.add_argument("--min-history", type=int, default=252)
    ap.add_argument("--max-hole", type=float, default=0.03)
    a = ap.parse_args()

    cand = pd.read_csv(CAND).drop_duplicates("symbol")
    close, volume = download(cand["symbol"].tolist())
    rows = []
    for _, r in cand.iterrows():
        s = r["symbol"]
        row = {"symbol": s, "underlying": r["underlying"], "status": "ok", "history_days": 0,
               "last_date": "", "median_value_lakh": 0.0, "hole_%": 0.0}
        if s not in close:
            rows.append({**row, "status": "no Yahoo data"})
            continue
        c, v = close[s], volume[s].fillna(0)
        med = c.rolling(7, center=True, min_periods=3).median()
        bad = (c / med - 1).abs() > 0.4
        c, v = c[~bad], v[~bad]
        first = c.index[0]
        days = len(c)
        expected = len(pd.bdate_range(first, c.index[-1])) * 0.94  # ~ trading days (weekends excluded, ~15 NSE holidays a year)
        recent = (c * v).tail(60)
        row.update(history_days=days, last_date=str(c.index[-1].date()),
                   median_value_lakh=round(float(recent.median()) / 1e5, 1),
                   **{"hole_%": round(max(0.0, 1 - days / max(expected, 1)) * 100, 1)})
        rows.append(row)

    df = pd.DataFrame(rows)
    latest = pd.to_datetime(df.loc[df["last_date"] != "", "last_date"]).max()
    stale = (latest - pd.to_datetime(df["last_date"].replace("", pd.NA))).dt.days > 7
    df.loc[(df["status"] == "ok") & stale, "status"] = "stale"
    df.loc[(df["status"] == "ok") & (df["history_days"] < a.min_history), "status"] = "history < 1y"
    df.loc[(df["status"] == "ok") & (df["hole_%"] > a.max_hole * 100), "status"] = "data gaps"
    df.loc[(df["status"] == "ok") & (df["median_value_lakh"] < a.min_value_lakh), "status"] = "illiquid"

    excl = ROOT / "data" / "exclude.csv"
    if excl.exists():
        df.loc[(df["status"] == "ok") & df["symbol"].isin(pd.read_csv(excl)["symbol"]), "status"] = "excluded"

    ok = df[df["status"] == "ok"].sort_values("median_value_lakh", ascending=False)
    keep = ok.drop_duplicates("underlying")
    df.loc[df["status"].eq("ok") & ~df["symbol"].isin(keep["symbol"]), "status"] = "duplicate underlying"
    df["status"] = df["status"].where(~df["symbol"].isin(keep["symbol"]), "selected")

    REPORT.parent.mkdir(exist_ok=True)
    df.to_csv(REPORT, index=False)
    if OUT.exists():
        shutil.copy(OUT, ROOT / "data" / f"etfs_backup_{pd.Timestamp.now():%Y%m%d_%H%M%S}.csv")
    fam_file = ROOT / "data" / "families.json"
    fam = {s: f for f, ls in json.loads(fam_file.read_text()).items() for s in ls} if fam_file.exists() else {}
    keep = keep.assign(family=keep["symbol"].map(fam).fillna(keep["underlying"]))
    keep = keep.sort_values(["family", "median_value_lakh"], ascending=[True, False])[
        ["symbol", "underlying", "family", "median_value_lakh", "history_days"]]
    keep.to_csv(OUT, index=False)
    print(f"\n{len(cand)} candidates -> {len(keep)} selected (one per underlying)")
    print(df["status"].value_counts().to_string())
    print("\n" + keep.to_string(index=False))
    print(f"\nwrote {OUT.name} (previous saved to data/etfs_previous.csv); full report {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
