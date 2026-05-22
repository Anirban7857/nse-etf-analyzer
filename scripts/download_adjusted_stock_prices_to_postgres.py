from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from time import sleep

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import create_market_engine, get_market_database_url, init_market_database


DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "adjusted_prices"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")

    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    engine = init_market_database(create_database=True)
    if args.init_only:
        print(f"Initialized PostgreSQL database: {get_market_database_url()}")
        return

    symbols = load_symbols(engine, args.index_name, args.as_of_date, args.symbols)
    symbol_map = load_symbol_map(Path(args.symbol_map)) if args.symbol_map else {}

    for index, symbol in enumerate(symbols, start=1):
        source_symbol = symbol_map.get(symbol, f"{symbol}{args.suffix}")
        try:
            frame = fetch_adjusted_history(
                symbol=symbol,
                source_symbol=source_symbol,
                start_date=start_date,
                end_date=end_date,
                provider=args.provider,
            )
            if frame.empty:
                print(f"{index}/{len(symbols)} {symbol}: no rows from {source_symbol}", flush=True)
                continue

            raw_path = raw_dir / f"{symbol}_{args.provider}_{start_date}_{end_date}.csv"
            frame.to_csv(raw_path, index=False)
            row_count = upsert_adjusted_prices(engine, frame)
            print(f"{index}/{len(symbols)} {symbol}: loaded {row_count} rows from {source_symbol}", flush=True)
        except Exception as exc:
            print(f"{index}/{len(symbols)} {symbol}: failed {exc}", flush=True)
        sleep(args.sleep)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download corporate-action-adjusted daily stock prices for index constituents "
            "and load them into stock_adjusted_prices."
        )
    )
    default_end = date.today()
    default_start = default_end.replace(year=default_end.year - 7)
    parser.add_argument("--start", default=default_start.isoformat(), help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date in YYYY-MM-DD format.")
    parser.add_argument("--index-name", help="Optional index name from index_constituents.")
    parser.add_argument("--as-of-date", help="Optional constituent as-of date. Defaults to latest available.")
    parser.add_argument("--symbols", help="Optional CSV/text file containing symbols to load.")
    parser.add_argument("--symbol-map", help="Optional CSV with columns symbol,source_symbol for ticker overrides.")
    parser.add_argument("--provider", default="yfinance", choices=["yfinance"], help="Adjusted price provider.")
    parser.add_argument("--suffix", default=".NS", help="Default Yahoo suffix for NSE symbols.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Directory for raw adjusted-price CSV files.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to wait between symbols.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    parser.add_argument("--init-only", action="store_true", help="Only create database/tables and exit.")
    return parser.parse_args()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_symbols(
    engine: Engine,
    index_name: str | None,
    as_of_date: str | None,
    symbols_path: str | None,
) -> list[str]:
    if symbols_path:
        return load_symbol_file(Path(symbols_path))

    conditions = []
    params: dict[str, object] = {}
    if index_name:
        conditions.append("index_name = :index_name")
        params["index_name"] = index_name.upper()
    if as_of_date:
        conditions.append("as_of_date = :as_of_date")
        params["as_of_date"] = parse_date(as_of_date)
    else:
        conditions.append("as_of_date = (select max(as_of_date) from index_constituents)")

    statement = text(f"select distinct symbol from index_constituents where {' and '.join(conditions)} order by symbol")
    with engine.begin() as connection:
        symbols = [row[0] for row in connection.execute(statement, params).fetchall()]

    if not symbols:
        raise ValueError("No symbols found. Load index constituents first or pass --symbols.")
    return symbols


def load_symbol_file(path: Path) -> list[str]:
    frame = pd.read_csv(path, header=None)
    values = frame.iloc[:, 0].dropna().astype(str).str.strip().str.upper()
    return sorted(value for value in values.unique() if value and value != "SYMBOL")


def load_symbol_map(path: Path) -> dict[str, str]:
    frame = pd.read_csv(path)
    columns = {column.lower().strip(): column for column in frame.columns}
    if "symbol" not in columns or "source_symbol" not in columns:
        raise ValueError("Symbol map must have columns: symbol, source_symbol")
    return {
        str(row[columns["symbol"]]).strip().upper(): str(row[columns["source_symbol"]]).strip()
        for _, row in frame.iterrows()
        if str(row[columns["symbol"]]).strip()
    }


def fetch_adjusted_history(
    symbol: str,
    source_symbol: str,
    start_date: date,
    end_date: date,
    provider: str,
) -> pd.DataFrame:
    if provider != "yfinance":
        raise ValueError(f"Unsupported provider: {provider}")

    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Install yfinance first: pip install yfinance") from exc

    # yfinance end date is exclusive.
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
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
        }
    )
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"]).dt.date
    normalized["symbol"] = symbol
    normalized["source_provider"] = provider
    normalized["source_symbol"] = source_symbol
    columns = [
        "trade_date",
        "symbol",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "source_provider",
        "source_symbol",
    ]
    normalized = normalized[columns].dropna(subset=["open_price", "high_price", "low_price", "close_price"])
    return normalized


def upsert_adjusted_prices(engine: Engine, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0

    statement = text(
        """
        insert into stock_adjusted_prices (
            trade_date, symbol, open_price, high_price, low_price, close_price,
            volume, source_provider, source_symbol
        )
        values (
            :trade_date, :symbol, :open_price, :high_price, :low_price, :close_price,
            :volume, :source_provider, :source_symbol
        )
        on conflict (trade_date, symbol, source_provider) do update set
            open_price = excluded.open_price,
            high_price = excluded.high_price,
            low_price = excluded.low_price,
            close_price = excluded.close_price,
            volume = excluded.volume,
            source_symbol = excluded.source_symbol,
            loaded_at = current_timestamp
        """
    )
    records = frame.to_dict(orient="records")
    with engine.begin() as connection:
        connection.execute(statement, records)
    return len(records)


if __name__ == "__main__":
    main()
