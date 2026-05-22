from __future__ import annotations

import os
import re
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url


DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5432/nse_etf"

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    create table if not exists etf_master (
        symbol text primary key,
        name text,
        isin text,
        issuer text,
        category text,
        listing_date date,
        is_active boolean not null default true,
        created_at timestamp not null default current_timestamp,
        updated_at timestamp not null default current_timestamp
    )
    """,
    """
    create table if not exists etf_daily_ohlc (
        symbol text not null,
        trade_date date not null,
        open numeric(18,4),
        high numeric(18,4),
        low numeric(18,4),
        close numeric(18,4),
        volume bigint,
        turnover numeric(20,2),
        source text,
        loaded_at timestamp not null default current_timestamp,
        primary key (symbol, trade_date),
        foreign key (symbol) references etf_master(symbol)
    )
    """,
    "create index if not exists idx_etf_daily_ohlc_trade_date on etf_daily_ohlc (trade_date)",
    """
    create table if not exists etf_adjusted_ohlc (
        symbol text not null,
        trade_date date not null,
        open numeric(18,4),
        high numeric(18,4),
        low numeric(18,4),
        close numeric(18,4),
        volume bigint,
        source_provider text not null,
        source_symbol text,
        loaded_at timestamp not null default current_timestamp,
        primary key (symbol, trade_date, source_provider),
        foreign key (symbol) references etf_master(symbol)
    )
    """,
    "create index if not exists idx_etf_adjusted_ohlc_symbol_date on etf_adjusted_ohlc (symbol, trade_date)",
    "create index if not exists idx_etf_adjusted_ohlc_trade_date on etf_adjusted_ohlc (trade_date)",
)


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url(), pool_pre_ping=True)


def ensure_database(database_url: str | None = None) -> None:
    target_url = make_url(database_url or get_database_url())
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


def init_database(engine: Engine | None = None, statements: Iterable[str] = SCHEMA_STATEMENTS) -> None:
    ensure_database()
    target_engine = engine or create_db_engine()
    with target_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
