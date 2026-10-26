# ETF 28 SMA Momentum

A scanner, backtester and web app for a 28-day SMA momentum strategy on NSE-listed ETFs. It replicates the rules
from a three-part YouTube series ("28 SMA Momentum", FIFO and LIFO versions) and adds tooling around them:
a daily scanner, a ledger, a backtester, a sensitivity grid, a data-driven ETF universe and a Streamlit app.

**Live app:** <https://h6czqqzbk69xfdcz4da3k3.streamlit.app/> (sign-in required; access is set in the app's Share settings)

> **Disclaimer.** Educational tool, not investment advice. In our own backtests the strategy **lagged** an equal-weight
> buy-and-hold basket, and its never-sell-at-a-loss rule can tie capital up for years (see [Results](#results-so-far)).
> Price data comes from Yahoo Finance and can be late or wrong. Verify prices with your broker before trading.

## The strategy

All signals use closing prices.

| Event | Rule |
|---|---|
| **Breakout** | Close below the 28 SMA, then 2 consecutive closes above it |
| **Breakdown** | Close above the 28 SMA, then 2 consecutive closes below it |
| **Buy** | First lot on a breakout. Up to 4 new buys a day, ranked by distance below the 52-week high, at most 2 ETFs per family |
| **Average** | On a new breakout, only if the price is at least 5% below the *last* buy. Amount = (drop % ÷ 2) × (per-buy ÷ 10) for FIFO, (drop % ÷ 1) × (per-buy ÷ 10) for LIFO |
| **Sell** | Only at a profit of at least 3.14%: on a breakdown, or on the first red close of an ETF up 18%+ in a month |
| **LIFO extras** | Sells lot by lot; once free cash drops below one per-buy amount, profitable lots are also sold on a red close |
| **Never** | Sell at a loss |
| **Capital** | 30% reserved for averaging, the rest split into per-buy slots. Profit is reinvested (after tax and withdrawal) |

## Quick start

Requires Python 3.11+ (developed on 3.14).

```powershell
cd path\to\etf28
py -m pip install -r requirements.txt
```

The project also has a `.venv`. It starts empty, so either install into it
(`.venv\Scripts\python.exe -m pip install -r requirements.txt`) and use `.venv\Scripts\python.exe` below,
or use `py` as shown, which uses the packages installed globally.

### Run the web app

```powershell
py -m streamlit run app.py
```

Opens at <http://localhost:8501>. Tabs: **Today's scan**, **Ledger**, **Backtest**, **Universe**, **How it works**.
Turn on *Sync ledger with local CSV files* in the sidebar to keep your trades in `holdings.csv` / `sold.csv`.

### Run the scanner from the command line

Run after 3:15 PM IST (about 3:45 PM or later is better, because Yahoo lags).

```powershell
py scanner.py scan            # today's BUY / AVERAGE / SELL list
py scanner.py scan --force    # show it even if the data check fails
py scanner.py status          # holdings, free cash, P&L

# record a trade after you place it with your broker
py scanner.py buy NIFTYBEES 257.20 68
py scanner.py sell NIFTYBEES 270.10 --lot 1 --brokerage 40    # --lot is for LIFO; omit it for the newest lot
```

Exit codes: `0` fine, `2` data check failed (actions withheld), `1` crash.
The data check blocks the scan when more than 10% of ETFs lack the latest bar, when a held ETF has stale data, or
when the newest bar is older than the last weekday. It does not know NSE holidays; use `--force` on those days.

### Run a backtest

```powershell
py backtest.py portfolio --start 2018-01-01                     # multi-ETF, capital-limited
py backtest.py single NIFTYBEES --start 2016-04-01              # replicates the video's single-ETF test
py backtest.py compare --starts 2018-01-01,2022-01-01           # FIFO vs LIFO, with and without cash yield
py backtest.py grid --start 2018-01-01                          # robustness sweeps (about 2 minutes)
py backtest.py grid --vary sma_days=20,28,36 --vary target_pct=2,3.14,5
```

Useful options: `--end DATE`, `--set key=value` (any `config.json` key plus `cost_bps` and `cash_yield_pct`),
`--tax` (apply tax and withdrawal; default is gross), `--refresh` (re-download prices). Outputs go to `reports/`.

### Daily schedule (Windows)

A scheduled task named **ETF28 Daily Scan** runs `run_scan_task.py` on weekdays at 16:00 and 18:00 IST and writes
`reports\scan_latest.txt` (latest) and `reports\scan_log.txt` (history).

```powershell
Get-ScheduledTask -TaskName "ETF28 Daily Scan"            # status
Start-ScheduledTask -TaskName "ETF28 Daily Scan"          # run now
Disable-ScheduledTask -TaskName "ETF28 Daily Scan"        # pause
```

It launches `pythonw.exe` through a script because console-based launches were killed on this machine.
If you move or reinstall Python, update the interpreter path in the task. The PC must be on and you must be logged in.

### Daily schedule (GitHub Actions)

`.github/workflows/scan.yml` runs the scanner in the cloud on weekdays at **16:25 IST** and **23:30 IST**
(cron `55 10 * * 1-5` and `0 18 * * 1-5` in UTC). The second run retries if Yahoo was still stale at the first.

- Open the repo's **Actions** tab, choose a run, and read the scan in its **Summary**. The raw output is also attached
  as an artifact for 14 days.
- A stale-data block does **not** fail the run (that would email you every evening); only a scanner crash does.
- Run it on demand from **Actions → Daily ETF scan → Run workflow** (tick *force* to show actions despite stale data).
- It scans **without your ledger**, so BUY rows assume you hold nothing. The repo is public, so do not put your
  holdings in it; use the local scan or the app for ledger-aware signals.
- GitHub can start scheduled runs several minutes late, and it pauses schedules in repos with no activity for 60 days.

## Configuration

`config.json` drives the scanner and backtester (the app has matching sidebar controls).

| Key | Default | Meaning |
|---|---|---|
| `mode` | `lifo` | `lifo` or `fifo` |
| `capital` | 500000 | Starting capital (₹) |
| `reserve_pct` | 30 | Share kept back for averaging |
| `max_etfs` | 20 | Maximum ETFs held at once |
| `max_new_buys_per_day` | 4 | New positions per day |
| `max_per_family` | 2 | Maximum ETFs per correlation family (0 = off) |
| `max_stale_pct` | 10 | Block the scan if more than this share of ETFs lack the latest bar |
| `sma_days` | 28 | Moving-average length |
| `target_pct` | 3.14 | Minimum profit to sell |
| `avg_drop_pct` | 5 | Minimum drop from the last buy to average |
| `fast_move_pct`, `fast_move_days` | 18, 21 | Fast-mover test (return over N trading days) |
| `tax_pct`, `cess_pct`, `withdraw_pct` | 20, 4, 10 | Ledger capital: tax on gains, cess, share of profit withdrawn |

## Files

| Path | Purpose |
|---|---|
| `app.py` | Streamlit web app |
| `scanner.py` | Daily scanner and ledger commands (signal logic in `compute_scan`, `apply_buy`, `apply_sell`) |
| `backtest.py` | Backtester, FIFO vs LIFO comparison, sensitivity grid |
| `build_universe.py` | Rebuilds `etfs.csv` from `data/candidates.csv` using live data checks |
| `run_scan_task.py` | Console-less runner used by the scheduled task |
| `etfs.csv` | The ETF universe: symbol, underlying, family, liquidity, history |
| `data/families.json` | Family mapping (ETFs with highly correlated returns share a family) |
| `data/exclude.csv` | ETFs excluded for data problems, with the reason |
| `holdings.csv`, `sold.csv` | Your ledger (created on first trade; git-ignored) |
| `reports/` | Scan outputs, backtest trades, grid results (git-ignored) |

### Refreshing the ETF universe

```powershell
py build_universe.py                          # default: median daily traded value of at least ₹25 lakh
py build_universe.py --min-value-lakh 10      # looser liquidity filter
```

It downloads every candidate, keeps ETFs with at least 1 year of history, fresh and complete data and enough liquidity,
then picks the most liquid ETF per underlying. The candidate list was compiled from a third-party directory, so
anything Yahoo cannot confirm is dropped. A new ETF missing from `families.json` becomes its own family until you add it.

## Host the app on Streamlit Community Cloud

1. **Create a GitHub repository** and push this folder. `.gitignore` already keeps `holdings.csv`, `sold.csv`,
   `reports/` and the price cache out of the repo.
   ```powershell
   git init
   git add .
   git commit -m "ETF 28 SMA momentum app"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. Go to <https://share.streamlit.io>, sign in with GitHub and choose **Create app**.
3. Pick the repo, branch `main` and main file `app.py`. Under *Advanced settings* choose Python 3.12 or 3.13.
4. Deploy. The first backtest downloads price history (about a minute); the scan fetches live prices.

Things to know about hosting:
- **The ledger is not stored on the server.** The filesystem is ephemeral and the app is shared by whoever has the link,
  so your trades live in your browser session only. Use **Download holdings.csv / sold.csv** after each change and
  **Load uploaded files** when you return.
- Keep the repo **private** if you do not want others to see your strategy settings. Community Cloud supports private repos.
- To restrict who can open the app, use the app's *Sharing* settings (viewer allow-list).
- Yahoo Finance may rate-limit shared cloud IPs. If the scan shows missing data, press **Refresh prices** later.
- The scheduled scan is Windows-only and local. The hosted app does not run on a schedule.

## Results so far

Backtests use today's 47-ETF universe, closing-price fills, 5 bps cost per side, no tax unless stated.
Treat these as indicative: the universe has survivorship bias and most ETFs have only 2 to 4 years of history.

| Start | LIFO + 6.5% idle-cash yield | Equal-weight buy-and-hold |
|---|---|---|
| 2018 | 12.6% a year | 13.0% a year |
| 2022 | 13.9% a year | 17.3% a year |

- The channel's headline Nifty result did not reproduce: 10-year XIRR 8.5% vs 11.9% for buy-and-hold on `single NIFTYBEES`.
  The first trade of its 2-year example matched exactly (bought 1 Apr 2024, sold 6 Aug 2024 at +7.82%).
- Parameters are not special (tested on the earlier 16-ETF universe): SMA lengths 10 to 60 all land within about 1.4 points of each other, and 28 ranks 19th of 26.
- Idle cash matters most: about 40% of capital sits uninvested under FIFO, and earning 6.5% on it adds about 4 points a year.
- LIFO beat FIFO in every window we tried, but with deeper drawdowns and positions held for up to about 6 years.
- Never selling at a loss means a 100% win rate by construction, with deep unrealised losses in between (-45% worst).
