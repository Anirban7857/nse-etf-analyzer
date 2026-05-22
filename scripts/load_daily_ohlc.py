from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.db import create_db_engine, init_database


COLUMN_ALIASES = {
    "symbol": ["symbol", "ticker", "tradingsymbol", "trading_symbol"],
    "trade_date": ["trade_date", "date", "trading_date", "timestamp"],
    "open": ["open", "open_price"],
    "high": ["high", "high_price"],
    "low": ["low", "low_price"],
    "close": ["close", "close_price", "ltp", "last_price"],
    "volume": ["volume", "traded_quantity", "shares_traded"],
    "turnover": ["turnover", "turnover_rs", "value", "traded_value"],
    "source": ["source"],
}

NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "turnover"]

UPSERT_SQL = text(
    """
    insert into etf_daily_ohlc (
        symbol,
        trade_date,
        open,
        high,
        low,
        close,
        volume,
        turnover,
        source,
        loaded_at
    ) values (
        :symbol,
        :trade_date,
        :open,
        :high,
        :low,
        :close,
        :volume,
        :turnover,
        :source,
        current_timestamp
    )
    on conflict (symbol, trade_date) do update set
        open = excluded.open,
        high = excluded.high,
        low = excluded.low,
        close = excluded.close,
        volume = excluded.volume,
        turnover = excluded.turnover,
        source = excluded.source,
        loaded_at = current_timestamp
    """
)

MASTER_UPSERT_SQL = text(
    """
    insert into etf_master (
        symbol,
        name,
        issuer,
        category,
        is_active,
        updated_at
    ) values (
        :symbol,
        :name,
        :issuer,
        :category,
        true,
        current_timestamp
    )
    on conflict (symbol) do update set
        updated_at = current_timestamp
    """
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load daily ETF OHLC data into PostgreSQL.")
    parser.add_argument("csv_path", type=Path, help="Path to the daily OHLC CSV file.")
    parser.add_argument(
        "--source-name",
        default=None,
        help="Optional source label stored with each row. Defaults to the CSV filename.",
    )
    args = parser.parse_args()

    dataset = load_daily_csv(args.csv_path, args.source_name)
    engine = create_db_engine()
    init_database(engine)

    ensure_symbols_exist(engine, dataset)
    rows = dataset.to_dict(orient="records")
    with engine.begin() as connection:
        connection.execute(UPSERT_SQL, rows)

    print(f"Loaded {len(rows)} OHLC rows from {args.csv_path}")


def load_daily_csv(csv_path: Path, source_name: str | None) -> pd.DataFrame:
    dataset = pd.read_csv(csv_path)
    normalized = normalize_columns(dataset)
    normalized["symbol"] = normalized["symbol"].fillna("").astype(str).str.upper().str.strip()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce").dt.date

    for column in NUMERIC_COLUMNS:
        normalized[column] = (
            normalized[column]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("%", "", regex=False)
            .str.replace("₹", "", regex=False)
            .replace({"nan": pd.NA, "": pd.NA, "<NA>": pd.NA})
        )
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized["source"] = normalized["source"].fillna(source_name or csv_path.name).astype(str)
    normalized = normalized[(normalized["symbol"] != "") & normalized["trade_date"].notna()]
    normalized = normalized.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return normalized[["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "source"]]


def normalize_columns(dataset: pd.DataFrame) -> pd.DataFrame:
    lowered = {column.lower().strip(): column for column in dataset.columns}
    rename_map: dict[str, str] = {}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                rename_map[lowered[alias]] = canonical
                break

    renamed = dataset.rename(columns=rename_map).copy()
    for canonical in COLUMN_ALIASES:
        if canonical not in renamed.columns:
            renamed[canonical] = pd.NA
    return renamed


def ensure_symbols_exist(engine, dataset: pd.DataFrame) -> None:
    master_rows = [
        {
            "symbol": symbol,
            "name": symbol,
            "issuer": "Unknown",
            "category": "Unclassified",
        }
        for symbol in dataset["symbol"].dropna().astype(str).unique()
    ]

    with engine.begin() as connection:
        connection.execute(MASTER_UPSERT_SQL, master_rows)


if __name__ == "__main__":
    main()
