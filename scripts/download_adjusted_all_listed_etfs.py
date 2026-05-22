from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from time import sleep

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.db import create_db_engine, init_database
from scripts.download_adjusted_oldest_category_etfs import ADJUSTED_UPSERT_SQL, adjusted_records_for_db
from scripts.load_oldest_category_etf_ohlc import MASTER_UPSERT_SQL, fetch_nse_etfs


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "etfs"
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "adjusted_etf_prices"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    raw_dir = Path(args.raw_dir)
    output_dir = Path(args.output_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    listed_etfs = fetch_nse_etfs()
    engine = create_db_engine()
    init_database(engine)
    with engine.begin() as connection:
        connection.execute(MASTER_UPSERT_SQL, listed_etfs.to_dict(orient="records"))

    failures = []
    loaded = []
    for index, row in enumerate(listed_etfs.itertuples(index=False), start=1):
        symbol = row.symbol
        source_symbol = f"{symbol}{args.suffix}"
        try:
            frame = fetch_adjusted_history(symbol, source_symbol, start_date, end_date)
            if frame.empty:
                failures.append({"symbol": symbol, "source_symbol": source_symbol, "error": "no rows"})
                print(f"{index}/{len(listed_etfs)} {symbol}: no rows from {source_symbol}", flush=True)
                continue
            frame.to_csv(raw_dir / f"{symbol}_yfinance_{start_date}_{end_date}.csv", index=False)
            with engine.begin() as connection:
                connection.execute(ADJUSTED_UPSERT_SQL, adjusted_records_for_db(frame))
            loaded.append(
                {
                    "symbol": symbol,
                    "rows": len(frame),
                    "first_date": frame["trade_date"].min(),
                    "last_date": frame["trade_date"].max(),
                }
            )
            print(f"{index}/{len(listed_etfs)} {symbol}: loaded {len(frame)} rows", flush=True)
        except Exception as exc:
            failures.append({"symbol": symbol, "source_symbol": source_symbol, "error": str(exc)})
            print(f"{index}/{len(listed_etfs)} {symbol}: failed {exc}", flush=True)
        sleep(args.sleep)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    loaded_path = output_dir / f"all_listed_etfs_adjusted_loaded_{stamp}.csv"
    failures_path = output_dir / f"all_listed_etfs_adjusted_failures_{stamp}.csv"
    pd.DataFrame(loaded).to_csv(loaded_path, index=False)
    pd.DataFrame(failures).to_csv(failures_path, index=False)
    print(f"NSE listed ETFs: {len(listed_etfs)}")
    print(f"Adjusted ETFs loaded: {len(loaded)}")
    print(f"Failures: {len(failures)}")
    print(f"Loaded CSV: {loaded_path}")
    print(f"Failures CSV: {failures_path}")


def parse_args() -> argparse.Namespace:
    default_end = date.today()
    parser = argparse.ArgumentParser(description="Download adjusted OHLC for every currently listed NSE ETF.")
    parser.add_argument("--start", default="2006-01-01", help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date in YYYY-MM-DD format.")
    parser.add_argument("--suffix", default=".NS", help="Yahoo suffix for NSE symbols.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to wait between symbols.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw adjusted ETF CSV directory.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Generated report directory.")
    parser.add_argument("--database-url", help="ETF PostgreSQL SQLAlchemy URL. Overrides DATABASE_URL.")
    return parser.parse_args()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_adjusted_history(symbol: str, source_symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Install yfinance first: pip install yfinance") from exc

    raw = yf.download(
        source_symbol,
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        auto_adjust=True,
        progress=False,
        actions=False,
        threads=False,
    )
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    normalized = raw.reset_index().rename(
        columns={
            "Date": "trade_date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"]).dt.date
    normalized["symbol"] = symbol
    normalized["source_provider"] = "yfinance"
    normalized["source_symbol"] = source_symbol
    columns = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "source_provider", "source_symbol"]
    normalized = normalized[columns].dropna(subset=["open", "high", "low", "close"])
    return normalized


if __name__ == "__main__":
    main()
