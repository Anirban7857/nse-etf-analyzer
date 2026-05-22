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
from scripts.load_oldest_category_etf_ohlc import (
    MASTER_UPSERT_SQL,
    build_selection_report,
    fetch_nse_etfs,
    select_oldest_by_category,
)


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "etfs"
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "adjusted_etf_prices"

ADJUSTED_UPSERT_SQL = text(
    """
    insert into etf_adjusted_ohlc (
        symbol,
        trade_date,
        open,
        high,
        low,
        close,
        volume,
        source_provider,
        source_symbol,
        loaded_at
    ) values (
        :symbol,
        :trade_date,
        :open,
        :high,
        :low,
        :close,
        :volume,
        :source_provider,
        :source_symbol,
        current_timestamp
    )
    on conflict (symbol, trade_date, source_provider) do update set
        open = excluded.open,
        high = excluded.high,
        low = excluded.low,
        close = excluded.close,
        volume = excluded.volume,
        source_symbol = excluded.source_symbol,
        loaded_at = current_timestamp
    """
)


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    listed_etfs = fetch_nse_etfs()
    selected_etfs = select_oldest_by_category(listed_etfs)

    engine = create_db_engine()
    init_database(engine)
    with engine.begin() as connection:
        connection.execute(MASTER_UPSERT_SQL, listed_etfs.to_dict(orient="records"))

    loaded_frames = []
    failures = []
    for index, row in enumerate(selected_etfs.itertuples(index=False), start=1):
        source_symbol = f"{row.symbol}{args.suffix}"
        try:
            frame = fetch_adjusted_history(
                symbol=row.symbol,
                source_symbol=source_symbol,
                start_date=start_date,
                end_date=end_date,
                provider=args.provider,
            )
            if frame.empty:
                failures.append({"symbol": row.symbol, "source_symbol": source_symbol, "error": "no rows"})
                print(f"{index}/{len(selected_etfs)} {row.symbol}: no rows from {source_symbol}", flush=True)
                continue

            raw_path = raw_dir / f"{row.symbol}_{args.provider}_{start_date}_{end_date}.csv"
            frame.to_csv(raw_path, index=False)
            with engine.begin() as connection:
                connection.execute(ADJUSTED_UPSERT_SQL, adjusted_records_for_db(frame))
            loaded_frames.append(frame)
            print(f"{index}/{len(selected_etfs)} {row.symbol}: loaded {len(frame)} rows from {source_symbol}", flush=True)
        except Exception as exc:
            failures.append({"symbol": row.symbol, "source_symbol": source_symbol, "error": str(exc)})
            print(f"{index}/{len(selected_etfs)} {row.symbol}: failed {exc}", flush=True)
        sleep(args.sleep)

    adjusted = pd.concat(loaded_frames, ignore_index=True) if loaded_frames else pd.DataFrame()
    report = build_selection_report_for_adjusted(listed_etfs, selected_etfs, adjusted)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_path = output_dir / f"nse_listed_etfs_adjusted_selection_{stamp}.csv"
    included_path = output_dir / f"nse_included_oldest_category_etfs_adjusted_{stamp}.csv"
    excluded_path = output_dir / f"nse_excluded_etfs_adjusted_{stamp}.csv"
    adjusted_path = output_dir / f"included_etf_adjusted_ohlc_loaded_{stamp}.csv"
    failures_path = output_dir / f"included_etf_adjusted_failures_{stamp}.csv"

    report.to_csv(all_path, index=False)
    report[report["included"]].to_csv(included_path, index=False)
    report[~report["included"]].to_csv(excluded_path, index=False)
    adjusted.to_csv(adjusted_path, index=False)
    pd.DataFrame(failures).to_csv(failures_path, index=False)

    print(f"NSE listed ETFs: {len(listed_etfs)}")
    print(f"Categories selected: {selected_etfs['category'].nunique()}")
    print(f"Included ETFs: {len(selected_etfs)}")
    print(f"Adjusted ETFs loaded: {adjusted['symbol'].nunique() if not adjusted.empty else 0}/{len(selected_etfs)}")
    print(f"Adjusted OHLC rows loaded: {len(adjusted)}")
    print(f"Latest adjusted date: {adjusted['trade_date'].max() if not adjusted.empty else 'none'}")
    print(f"All ETF adjusted selection CSV: {all_path}")
    print(f"Included adjusted CSV: {included_path}")
    print(f"Excluded adjusted CSV: {excluded_path}")
    print(f"Loaded adjusted OHLC CSV: {adjusted_path}")
    print(f"Failures CSV: {failures_path}")


def parse_args() -> argparse.Namespace:
    default_end = date.today()
    default_start = default_end.replace(year=default_end.year - 20)
    parser = argparse.ArgumentParser(
        description=(
            "Fetch all listed NSE ETFs, select the oldest ETF per normalized category, "
            "and download adjusted OHLC for those selected ETFs without bhavcopy."
        )
    )
    parser.add_argument("--start", default=default_start.isoformat(), help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date in YYYY-MM-DD format.")
    parser.add_argument("--provider", default="yfinance", choices=["yfinance"], help="Adjusted price provider.")
    parser.add_argument("--suffix", default=".NS", help="Default Yahoo suffix for NSE symbols.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to wait between symbols.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Directory for raw adjusted ETF CSV files.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated CSV reports.")
    parser.add_argument("--database-url", help="ETF PostgreSQL SQLAlchemy URL. Overrides DATABASE_URL.")
    return parser.parse_args()


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
    normalized["source_provider"] = provider
    normalized["source_symbol"] = source_symbol
    columns = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "source_provider", "source_symbol"]
    return normalized[columns].dropna(subset=["open", "high", "low", "close"])


def adjusted_records_for_db(frame: pd.DataFrame) -> list[dict[str, object]]:
    records = []
    for row in frame.itertuples(index=False):
        records.append(
            {
                "symbol": row.symbol,
                "trade_date": row.trade_date,
                "open": none_if_na(row.open),
                "high": none_if_na(row.high),
                "low": none_if_na(row.low),
                "close": none_if_na(row.close),
                "volume": int(row.volume) if pd.notna(row.volume) else None,
                "source_provider": row.source_provider,
                "source_symbol": row.source_symbol,
            }
        )
    return records


def build_selection_report_for_adjusted(
    listed_etfs: pd.DataFrame,
    selected_etfs: pd.DataFrame,
    adjusted: pd.DataFrame,
) -> pd.DataFrame:
    if adjusted.empty:
        proxy = pd.DataFrame(columns=["symbol", "trade_date"])
    else:
        proxy = adjusted.rename(columns={"source_provider": "source"})
    return build_selection_report(listed_etfs, selected_etfs, proxy).rename(
        columns={
            "ohlc_rows": "adjusted_rows",
            "first_ohlc_date": "first_adjusted_date",
            "last_ohlc_date": "last_adjusted_date",
        }
    )


def none_if_na(value):
    return None if pd.isna(value) else value


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


if __name__ == "__main__":
    main()
