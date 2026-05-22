from __future__ import annotations

import os
import re
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError


DEFAULT_MARKET_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5432/nse_market_data"

MARKET_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    create table if not exists bhavcopy_prices (
        trade_date date not null,
        symbol text not null,
        series text not null,
        open_price numeric(18,4),
        high_price numeric(18,4),
        low_price numeric(18,4),
        close_price numeric(18,4),
        prev_close numeric(18,4),
        last_price numeric(18,4),
        volume bigint,
        turnover numeric(22,4),
        total_trades bigint,
        isin text,
        source_file text,
        loaded_at timestamp not null default current_timestamp,
        primary key (trade_date, symbol, series)
    )
    """,
    "create index if not exists idx_bhavcopy_prices_symbol_date on bhavcopy_prices (symbol, trade_date)",
    "create index if not exists idx_bhavcopy_prices_trade_date on bhavcopy_prices (trade_date)",
    "create index if not exists idx_bhavcopy_prices_isin on bhavcopy_prices (isin)",
    """
    create table if not exists stock_adjusted_prices (
        trade_date date not null,
        symbol text not null,
        open_price numeric(18,4),
        high_price numeric(18,4),
        low_price numeric(18,4),
        close_price numeric(18,4),
        volume bigint,
        source_provider text not null,
        source_symbol text,
        loaded_at timestamp not null default current_timestamp,
        primary key (trade_date, symbol, source_provider)
    )
    """,
    "create index if not exists idx_stock_adjusted_prices_symbol_date on stock_adjusted_prices (symbol, trade_date)",
    "create index if not exists idx_stock_adjusted_prices_trade_date on stock_adjusted_prices (trade_date)",
    """
    create table if not exists index_constituents (
        index_name text not null,
        symbol text not null,
        company_name text,
        industry text,
        isin text,
        weight numeric(12,6),
        as_of_date date not null,
        source_file text,
        loaded_at timestamp not null default current_timestamp,
        primary key (index_name, symbol, as_of_date)
    )
    """,
    "create index if not exists idx_index_constituents_symbol on index_constituents (symbol)",
    """
    create table if not exists download_log (
        trade_date date primary key,
        source_url text,
        source_file text,
        status text not null,
        row_count integer not null default 0,
        error_message text,
        downloaded_at timestamp not null default current_timestamp
    )
    """,
)


def get_market_database_url() -> str:
    return os.environ.get("MARKET_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_MARKET_DATABASE_URL


def create_market_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_market_database_url(), pool_pre_ping=True)


def ensure_market_database(database_url: str | None = None) -> None:
    target_url = make_url(database_url or get_market_database_url())
    database_name = target_url.database
    if not database_name:
        raise ValueError("Database URL must include a database name.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_name):
        raise ValueError(f"Unsafe PostgreSQL database name: {database_name!r}")

    server_url = target_url.set(database="postgres")
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    with server_engine.connect() as connection:
        exists = connection.execute(
            text("select 1 from pg_database where datname = :database_name"),
            {"database_name": database_name},
        ).scalar()
        if not exists:
            connection.execute(text(f'create database "{database_name}"'))
    server_engine.dispose()


def init_market_database(
    engine: Engine | None = None,
    statements: Iterable[str] = MARKET_SCHEMA_STATEMENTS,
    create_database: bool = True,
) -> Engine:
    database_url = get_market_database_url()
    if create_database:
        ensure_market_database(database_url)

    target_engine = engine or create_market_engine(database_url)
    try:
        with target_engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    except OperationalError:
        target_engine.dispose()
        raise
    return target_engine
