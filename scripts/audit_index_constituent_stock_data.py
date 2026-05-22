from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import create_market_engine


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "audits"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = create_market_engine()
    symbols = load_index_symbols(engine, args.index_name, args.as_of_date)
    if not symbols:
        raise ValueError("No index constituents found for the selected filters.")

    rows = []
    with engine.begin() as connection:
        for symbol in symbols:
            table_record = fetch_stock_table_record(connection, args.schema, symbol)
            table_name = table_record["table_name"] if table_record else None
            table_exists = connection.execute(
                text(
                    """
                    select exists (
                        select 1
                        from information_schema.tables
                        where table_schema = :schema and table_name = :table_name
                    )
                    """
                ),
                {"schema": args.schema, "table_name": table_name or ""},
            ).scalar()

            table_stats = empty_stats()
            if table_exists:
                table_stats = fetch_table_stats(connection, args.schema, table_name)

            adjusted_stats = fetch_source_stats(
                connection,
                """
                select count(*) as row_count,
                       min(trade_date) as first_date,
                       max(trade_date) as last_date,
                       count(*) filter (
                           where open_price is null or high_price is null or low_price is null or close_price is null
                       ) as null_ohlc_count
                from stock_adjusted_prices
                where symbol = :symbol and source_provider = :provider
                """,
                {"symbol": symbol, "provider": args.adjusted_provider},
            )
            bhavcopy_stats = fetch_source_stats(
                connection,
                """
                select count(*) as row_count,
                       min(trade_date) as first_date,
                       max(trade_date) as last_date,
                       count(*) filter (
                           where open_price is null or high_price is null or low_price is null or close_price is null
                       ) as null_ohlc_count
                from bhavcopy_prices
                where symbol = :symbol and series = :series
                """,
                {"symbol": symbol, "series": args.series},
            )

            rows.append(
                {
                    "symbol": symbol,
                    "stock_table": f"{args.schema}.{table_name}" if table_name else "",
                    "stock_table_exists": bool(table_exists),
                    "stock_table_rows": table_stats["row_count"],
                    "stock_table_first_date": table_stats["first_date"],
                    "stock_table_last_date": table_stats["last_date"],
                    "stock_table_null_ohlc_count": table_stats["null_ohlc_count"],
                    "adjusted_rows": adjusted_stats["row_count"],
                    "adjusted_first_date": adjusted_stats["first_date"],
                    "adjusted_last_date": adjusted_stats["last_date"],
                    "adjusted_null_ohlc_count": adjusted_stats["null_ohlc_count"],
                    "bhavcopy_rows": bhavcopy_stats["row_count"],
                    "bhavcopy_first_date": bhavcopy_stats["first_date"],
                    "bhavcopy_last_date": bhavcopy_stats["last_date"],
                    "bhavcopy_null_ohlc_count": bhavcopy_stats["null_ohlc_count"],
                    "status": status_for(table_exists, table_stats, adjusted_stats, bhavcopy_stats, args.min_rows),
                }
            )

    report = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.index_name.lower().replace(" ", "_") if args.index_name else "all_index_constituents"
    output_path = output_dir / f"{prefix}_stock_data_audit_{stamp}.csv"
    report.to_csv(output_path, index=False)

    print(f"Index symbols: {len(symbols)}")
    print(f"Per-stock tables present: {int(report['stock_table_exists'].sum())}/{len(report)}")
    print(f"Symbols with adjusted rows: {int((report['adjusted_rows'] > 0).sum())}/{len(report)}")
    print(f"Symbols with bhavcopy rows: {int((report['bhavcopy_rows'] > 0).sum())}/{len(report)}")
    print("Status counts:")
    print(report["status"].value_counts().to_string())
    print(f"Audit CSV: {output_path}")

    problem_rows = report[report["status"] != "ok"].head(args.show)
    if not problem_rows.empty:
        print("\nSample issues:")
        print(problem_rows[["symbol", "status", "stock_table_rows", "adjusted_rows", "bhavcopy_rows"]].to_string(index=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit index constituents against loaded stock price data.")
    parser.add_argument("--index-name", help="Optional index name. Defaults to all latest index constituents.")
    parser.add_argument("--as-of-date", help="Optional constituent as-of date. Defaults to latest available.")
    parser.add_argument("--schema", default="stock_daily", help="Schema for per-symbol stock tables.")
    parser.add_argument("--series", default="EQ", help="Bhavcopy series to check.")
    parser.add_argument("--adjusted-provider", default="yfinance", help="Adjusted price provider to check.")
    parser.add_argument("--min-rows", type=int, default=200, help="Minimum rows expected for a symbol to be considered populated.")
    parser.add_argument("--show", type=int, default=20, help="Number of issue rows to print.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for audit CSV output.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    return parser.parse_args()


def load_index_symbols(engine, index_name: str | None, as_of_date: str | None) -> list[str]:
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

    statement = text(f"select distinct symbol from index_constituents where {' and '.join(conditions)} order by symbol")
    with engine.begin() as connection:
        return [row[0] for row in connection.execute(statement, params).fetchall()]


def fetch_stock_table_record(connection, schema: str, symbol: str):
    table_map_exists = connection.execute(
        text(
            """
            select exists (
                select 1
                from information_schema.tables
                where table_schema = :schema and table_name = 'table_map'
            )
            """
        ),
        {"schema": schema},
    ).scalar()
    if not table_map_exists:
        return None

    return (
        connection.execute(
            text(f'select table_name from "{schema}".table_map where symbol = :symbol'),
            {"symbol": symbol},
        )
        .mappings()
        .first()
    )


def empty_stats() -> dict[str, object]:
    return {"row_count": 0, "first_date": None, "last_date": None, "null_ohlc_count": 0}


def fetch_table_stats(connection, schema: str, table_name: str) -> dict[str, object]:
    statement = text(
        f"""
        select count(*) as row_count,
               min(trade_date) as first_date,
               max(trade_date) as last_date,
               count(*) filter (
                   where open_price is null or high_price is null or low_price is null or close_price is null
               ) as null_ohlc_count
        from "{schema}"."{table_name}"
        """
    )
    return row_to_stats(connection.execute(statement).mappings().one())


def fetch_source_stats(connection, sql: str, params: dict[str, object]) -> dict[str, object]:
    return row_to_stats(connection.execute(text(sql), params).mappings().one())


def row_to_stats(row) -> dict[str, object]:
    return {
        "row_count": int(row["row_count"] or 0),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "null_ohlc_count": int(row["null_ohlc_count"] or 0),
    }


def status_for(table_exists, table_stats, adjusted_stats, bhavcopy_stats, min_rows: int) -> str:
    if not table_exists:
        return "missing_stock_table"
    if table_stats["row_count"] < min_rows:
        return "stock_table_sparse"
    if table_stats["null_ohlc_count"]:
        return "stock_table_has_null_ohlc"
    if adjusted_stats["row_count"] == 0 and bhavcopy_stats["row_count"] == 0:
        return "no_source_price_rows"
    return "ok"


if __name__ == "__main__":
    main()
