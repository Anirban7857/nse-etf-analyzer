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
    "name": ["name", "fund_name", "scheme_name", "etf_name", "fund"],
    "isin": ["isin", "isin_code"],
    "issuer": ["issuer", "amc", "fund_house", "provider"],
    "category": ["category", "theme", "asset_class", "segment"],
    "listing_date": ["listing_date", "listed_on", "launch_date"],
    "is_active": ["is_active", "active", "listed"],
}

UPSERT_SQL = text(
    """
    insert into etf_master (
        symbol,
        name,
        isin,
        issuer,
        category,
        listing_date,
        is_active,
        updated_at
    ) values (
        :symbol,
        :name,
        :isin,
        :issuer,
        :category,
        :listing_date,
        :is_active,
        current_timestamp
    )
    on conflict (symbol) do update set
        name = excluded.name,
        isin = excluded.isin,
        issuer = excluded.issuer,
        category = excluded.category,
        listing_date = excluded.listing_date,
        is_active = excluded.is_active,
        updated_at = current_timestamp
    """
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load NSE ETF master data into PostgreSQL.")
    parser.add_argument("csv_path", type=Path, help="Path to the ETF master CSV file.")
    args = parser.parse_args()

    dataset = load_master_csv(args.csv_path)
    engine = create_db_engine()
    init_database(engine)

    rows = dataset.to_dict(orient="records")
    with engine.begin() as connection:
        connection.execute(UPSERT_SQL, rows)

    print(f"Loaded {len(rows)} ETF master records from {args.csv_path}")


def load_master_csv(csv_path: Path) -> pd.DataFrame:
    dataset = pd.read_csv(csv_path)
    normalized = normalize_columns(dataset)
    normalized["symbol"] = normalized["symbol"].fillna("").astype(str).str.upper().str.strip()
    normalized = normalized[normalized["symbol"] != ""].drop_duplicates(subset=["symbol"], keep="last")
    normalized["name"] = normalized["name"].fillna(normalized["symbol"]).astype(str).str.strip()
    normalized["issuer"] = normalized["issuer"].fillna("Unknown").astype(str).str.strip()
    normalized["category"] = normalized["category"].fillna("Unclassified").astype(str).str.strip()
    normalized["isin"] = normalized["isin"].where(normalized["isin"].notna(), None)
    normalized["listing_date"] = pd.to_datetime(normalized["listing_date"], errors="coerce").dt.date
    normalized["is_active"] = normalized["is_active"].map(parse_bool).fillna(True)
    return normalized[["symbol", "name", "isin", "issuer", "category", "listing_date", "is_active"]]


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


def parse_bool(value: object) -> bool | None:
    if value is None or pd.isna(value):
        return None

    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "active", "listed"}:
        return True
    if normalized in {"false", "0", "no", "n", "inactive", "delisted"}:
        return False
    return None


if __name__ == "__main__":
    main()
