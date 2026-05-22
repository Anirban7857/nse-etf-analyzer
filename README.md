---
title: NSE ETF Analyzer
sdk: docker
app_port: 7860
license: mit
short_description: Analysis of NSE ETF and strategy data
---

# NSE ETF Analyzer

ETF-first Flask app for viewing and analyzing NSE-listed ETF datasets.

## Features

- Search, filter, and rank ETFs by symbol, category, and issuer
- Summary metrics for ETF count, AUM, expense ratios, returns, and a composite score
- Category and issuer breakdowns
- Starter sample CSV included so the app runs immediately
- CSV upload flow for replacing the sample with a broader NSE ETF export
- Data layer built to expand into stocks and other instruments later

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Open `http://127.0.0.1:5000`.

## Deploy To Hugging Face Spaces

Create a free Hugging Face Space with these settings:

- SDK: Docker
- Hardware: Free CPU
- App port: 7860

Upload the project files, including the CSV files used by the UI:

- `data/nse_etfs_sample.csv`
- `data/generated/*.csv`
- `data/generated/backtests/**`

Do not upload local-only or heavy/generated runtime folders:

- `.venv/`
- `.idea/`
- `data/raw/`
- `__pycache__/`
- generated `.xlsx` reports

The public Space runs from bundled/generated CSV files. It will not connect to a local PostgreSQL database. The RELIANCE candlestick chart uses `yfinance` unless a cloud `MARKET_DATABASE_URL` or `DATABASE_URL` secret is configured.

Every app run also generates two CSV files under `data/generated/`:

- `etf_master.generated.csv`
- `etf_daily_ohlc.generated.csv`

The generated daily OHLC export is a current-day snapshot scaffold based on each ETF's `close_price`. It is useful for bootstrapping the PostgreSQL loader flow, but it is not a substitute for true NSE historical OHLC ingestion.

## PostgreSQL setup

Set a database URL for your local PostgreSQL instance before running the loader scripts.

```powershell
$env:DATABASE_URL="postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/nse_etf"
```

Install the extra database dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Initialize the schema:

```powershell
.\.venv\Scripts\python.exe scripts\init_db.py
```

Load ETF master data from a CSV:

```powershell
.\.venv\Scripts\python.exe scripts\load_etf_master.py path\to\etf_master.csv
```

Load daily OHLC data from a CSV:

```powershell
.\.venv\Scripts\python.exe scripts\load_daily_ohlc.py path\to\daily_ohlc.csv
```

The loader scripts accept common column aliases such as `ticker`, `scheme_name`, `amc`, `date`, `open_price`, and `close_price`.

## NSE bhavcopy PostgreSQL ingestion

The market-data ingestion flow creates the PostgreSQL database and tables if they do not exist, downloads NSE CM bhavcopy files, keeps raw files under `data/raw/bhavcopy/`, and upserts normalized rows into `bhavcopy_prices`.

Set a market database URL for your local PostgreSQL instance:

```powershell
$env:MARKET_DATABASE_URL="postgresql+psycopg://postgres:@localhost:5432/nse_market_data"
```

Initialize only:

```powershell
.\.venv\Scripts\python.exe scripts\download_bhavcopy_to_postgres.py --init-only
```

Download and load a date range:

```powershell
.\.venv\Scripts\python.exe scripts\download_bhavcopy_to_postgres.py --start 2019-05-11 --end 2026-05-11
```

By default only `EQ` series rows are loaded. Use `--symbols path\to\symbols.csv` to load only a chosen index constituent list.

## Adjusted stock price ingestion

Bhavcopy prices are raw exchange prices and are not adjusted for splits, bonuses, dividends, or symbol changes. For backtests that need corporate-action-adjusted price series, load adjusted prices for the symbols already present in `index_constituents`:

```powershell
.\.venv\Scripts\python.exe scripts\load_index_constituents_to_postgres.py
.\.venv\Scripts\python.exe scripts\download_adjusted_stock_prices_to_postgres.py --index-name "NIFTY 100" --start 2019-05-11 --end 2026-05-11
```

The adjusted loader writes to `stock_adjusted_prices`, leaving `bhavcopy_prices` untouched. It uses Yahoo Finance symbols by default, e.g. `RELIANCE.NS`. If a symbol needs a different provider ticker, pass a CSV with columns `symbol,source_symbol`:

```powershell
.\.venv\Scripts\python.exe scripts\download_adjusted_stock_prices_to_postgres.py --index-name "NIFTY 100" --symbol-map data\symbol_map.csv
```

Run the momentum backtest on adjusted prices:

```powershell
.\.venv\Scripts\python.exe scripts\backtest_weekly_momentum_top2.py --index-name "NIFTY 100" --price-source adjusted
```

## Dataset schema

The loader normalizes common column aliases. The canonical columns are:

- `symbol`
- `fund_name`
- `category`
- `issuer`
- `aum_cr`
- `expense_ratio`
- `nav`
- `close_price`
- `one_year_return`
- `three_year_return`
- `volatility`
- `tracking_error`

You can upload a CSV with these columns or common alternatives such as `ticker`, `scheme_name`, `amc`, `1y_return`, `ltp`, or `ter`.

