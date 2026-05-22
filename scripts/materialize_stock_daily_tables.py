from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import create_market_engine, init_market_database


DEFAULT_SCHEMA = "stock_daily"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    init_market_database(create_database=True)
    engine = create_market_engine()
    schema = safe_identifier(args.schema)
    symbols = load_symbols(engine, args.symbols, args.source, args.index_name, args.as_of_date)

    with engine.begin() as connection:
        connection.execute(text(f'create schema if not exists "{schema}"'))
        connection.execute(
            text(
                f"""
                create table if not exists "{schema}".table_map (
                    symbol text primary key,
                    series text not null,
                    table_schema text not null,
                    table_name text not null unique,
                    row_count integer not null default 0,
                    min_trade_date date,
                    max_trade_date date,
                    refreshed_at timestamp not null default current_timestamp
                )
                """
            )
        )

    created = 0
    skipped = 0
    for index, symbol in enumerate(symbols, start=1):
        table_name = table_name_for_symbol(symbol)
        if not args.replace and stock_table_exists(engine, schema, table_name):
            skipped += 1
            print(f"{index}/{len(symbols)} {symbol}: exists, skipped", flush=True)
            continue

        row_count = materialize_symbol_table(
            engine=engine,
            schema=schema,
            table_name=table_name,
            symbol=symbol,
            series=args.series,
            replace=args.replace,
            source=args.source,
            adjusted_provider=args.adjusted_provider,
        )
        upsert_table_map(engine, schema, symbol, args.series, table_name, row_count, args.source)
        created += 1
        print(f"{index}/{len(symbols)} {symbol}: materialized {row_count} rows into {schema}.{table_name}", flush=True)

    print(f"Done. Materialized {created} tables; skipped {skipped} existing tables.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create one PostgreSQL daily-price table per stock symbol.")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="Target PostgreSQL schema for stock tables.")
    parser.add_argument("--series", default="EQ", help="Bhavcopy series to materialize.")
    parser.add_argument("--symbols", help="Optional CSV/text file with symbols to materialize. Defaults to all symbols.")
    parser.add_argument("--index-name", help="Optional index name from index_constituents when --source adjusted.")
    parser.add_argument("--as-of-date", help="Optional constituent as-of date. Defaults to latest available.")
    parser.add_argument(
        "--source",
        choices=["bhavcopy", "adjusted"],
        default="bhavcopy",
        help="Source table for per-stock materialization.",
    )
    parser.add_argument("--adjusted-provider", default="yfinance", help="Provider in stock_adjusted_prices.")
    parser.add_argument("--replace", action="store_true", help="Drop and rebuild existing per-symbol tables.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    return parser.parse_args()


def load_symbols(
    engine: Engine,
    symbols_path: str | None,
    source: str,
    index_name: str | None,
    as_of_date: str | None,
) -> list[str]:
    if symbols_path:
        path = Path(symbols_path)
        if path.suffix.lower() == ".csv":
            import pandas as pd

            frame = pd.read_csv(path)
            symbol_column = next(
                (column for column in frame.columns if normalize_column_name(column) == "symbol"),
                frame.columns[0],
            )
            values = frame[symbol_column]
        else:
            values = path.read_text().splitlines()
        symbols = sorted({str(value).strip().upper() for value in values if str(value).strip()})
        return symbols

    with engine.begin() as connection:
        if source == "adjusted":
            if index_name or as_of_date:
                conditions = []
                params: dict[str, object] = {}
                if index_name:
                    conditions.append("index_name = :index_name")
                    params["index_name"] = index_name.upper()
                if as_of_date:
                    conditions.append("as_of_date = :as_of_date")
                    params["as_of_date"] = as_of_date
                else:
                    conditions.append("as_of_date = (select max(as_of_date) from index_constituents)")
                rows = connection.execute(
                    text(f"select distinct symbol from index_constituents where {' and '.join(conditions)} order by symbol"),
                    params,
                ).fetchall()
            else:
                rows = connection.execute(
                    text("select distinct symbol from stock_adjusted_prices order by symbol")
                ).fetchall()
        else:
            rows = connection.execute(
                text("select distinct symbol from bhavcopy_prices where series = 'EQ' order by symbol")
            ).fetchall()
    return [row[0] for row in rows]


def materialize_symbol_table(
    engine: Engine,
    schema: str,
    table_name: str,
    symbol: str,
    series: str,
    replace: bool,
    source: str,
    adjusted_provider: str,
) -> int:
    qualified_table = f'"{schema}"."{table_name}"'
    with engine.begin() as connection:
        if replace:
            connection.execute(text(f"drop table if exists {qualified_table}"))

        if source == "adjusted":
            connection.execute(
                text(
                    f"""
                    create table if not exists {qualified_table} as
                    select
                        trade_date,
                        symbol,
                        'ADJUSTED'::text as series,
                        open_price,
                        high_price,
                        low_price,
                        close_price,
                        null::numeric(18,4) as prev_close,
                        close_price as last_price,
                        volume,
                        null::numeric(22,4) as turnover,
                        null::bigint as total_trades,
                        null::text as isin,
                        source_provider || ':' || coalesce(source_symbol, symbol) as source_file
                    from stock_adjusted_prices
                    where symbol = :symbol and source_provider = :adjusted_provider
                    order by trade_date
                    """
                ),
                {"symbol": symbol, "adjusted_provider": adjusted_provider},
            )
        else:
            connection.execute(
                text(
                    f"""
                    create table if not exists {qualified_table} as
                    select
                        trade_date,
                        symbol,
                        series,
                        open_price,
                        high_price,
                        low_price,
                        close_price,
                        prev_close,
                        last_price,
                        volume,
                        turnover,
                        total_trades,
                        isin,
                        source_file
                    from bhavcopy_prices
                    where symbol = :symbol and series = :series
                    order by trade_date
                    """
                ),
                {"symbol": symbol, "series": series},
            )
        connection.execute(
            text(
                f"""
                create unique index if not exists "{table_name}_trade_date_idx"
                on {qualified_table} (trade_date)
                """
            )
        )
        connection.execute(
            text(
                f"""
                create index if not exists "{table_name}_symbol_date_idx"
                on {qualified_table} (symbol, trade_date)
                """
            )
        )
        return connection.execute(text(f"select count(*) from {qualified_table}")).scalar_one()


def upsert_table_map(
    engine: Engine,
    schema: str,
    symbol: str,
    series: str,
    table_name: str,
    row_count: int,
    source: str,
) -> None:
    with engine.begin() as connection:
        date_bounds = connection.execute(
            text(f'select min(trade_date), max(trade_date) from "{schema}"."{table_name}"')
        ).first()
        connection.execute(
            text(
                f"""
                insert into "{schema}".table_map (
                    symbol, series, table_schema, table_name, row_count,
                    min_trade_date, max_trade_date, refreshed_at
                )
                values (
                    :symbol, :series, :table_schema, :table_name, :row_count,
                    :min_trade_date, :max_trade_date, current_timestamp
                )
                on conflict (symbol) do update set
                    series = excluded.series,
                    table_schema = excluded.table_schema,
                    table_name = excluded.table_name,
                    row_count = excluded.row_count,
                    min_trade_date = excluded.min_trade_date,
                    max_trade_date = excluded.max_trade_date,
                    refreshed_at = current_timestamp
                """
            ),
            {
                "symbol": symbol,
                "series": "ADJUSTED" if source == "adjusted" else series,
                "table_schema": schema,
                "table_name": table_name,
                "row_count": row_count,
                "min_trade_date": date_bounds[0],
                "max_trade_date": date_bounds[1],
            },
        )


def stock_table_exists(engine: Engine, schema: str, table_name: str) -> bool:
    with engine.begin() as connection:
        return (
            connection.execute(
                text(
                    """
                    select 1
                    from information_schema.tables
                    where table_schema = :schema and table_name = :table_name
                    """
                ),
                {"schema": schema, "table_name": table_name},
            ).scalar()
            is not None
        )


def table_name_for_symbol(symbol: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", symbol.lower()).strip("_")
    if not normalized:
        normalized = "symbol"
    if normalized[0].isdigit():
        normalized = f"s_{normalized}"
    digest = hashlib.sha1(symbol.encode("utf-8")).hexdigest()[:8]
    return f"{normalized[:50]}_{digest}"


def safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe PostgreSQL identifier: {value!r}")
    return value


def normalize_column_name(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


if __name__ == "__main__":
    main()
