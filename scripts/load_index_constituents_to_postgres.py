from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import create_market_engine, init_market_database


DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "index_constituents"

INDEX_SOURCES: tuple[tuple[str, str], ...] = (
    ("NIFTY 50", "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"),
    ("NIFTY NEXT 50", "https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv"),
    ("NIFTY 100", "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"),
    ("NIFTY MIDCAP 150", "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv"),
    ("NIFTY SMALLCAP 250", "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv"),
    ("NIFTY MICROCAP 250", "https://www.niftyindices.com/IndexConstituent/ind_niftymicrocap250_list.csv"),
)


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    as_of_date = datetime.strptime(args.as_of_date, "%Y-%m-%d").date()
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    engine = init_market_database(create_database=True)
    total_rows = 0
    for index_name, url in INDEX_SOURCES:
        content = download_csv(url)
        source_file = save_raw_csv(raw_dir, index_name, as_of_date, content)
        frame = normalize_constituents(content, index_name, as_of_date, source_file.name)
        row_count = upsert_index_constituents(engine, frame)
        total_rows += row_count
        print(f"{index_name}: loaded {row_count} rows from {source_file.name}")

    print(f"Total loaded: {total_rows} rows")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Nifty index constituent CSVs into PostgreSQL.")
    parser.add_argument("--as-of-date", default=date.today().isoformat(), help="As-of date in YYYY-MM-DD format.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Directory for raw downloaded CSV files.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    return parser.parse_args()


def download_csv(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/csv,application/octet-stream,*/*",
            "Referer": "https://www.niftyindices.com/",
        },
    )
    with urlopen(request, timeout=90) as response:
        content = response.read()
    if b"Company Name" not in content[:200]:
        raise ValueError(f"Unexpected response for {url}")
    return content


def save_raw_csv(raw_dir: Path, index_name: str, as_of_date: date, content: bytes) -> Path:
    safe_name = index_name.lower().replace(" ", "_")
    path = raw_dir / f"{safe_name}_{as_of_date.isoformat()}.csv"
    path.write_bytes(content)
    return path


def normalize_constituents(content: bytes, index_name: str, as_of_date: date, source_file: str) -> pd.DataFrame:
    raw = pd.read_csv(BytesIO(content))
    columns = {normalize_column_name(column): column for column in raw.columns}

    def get(*names: str, default=None):
        for name in names:
            source = columns.get(normalize_column_name(name))
            if source is not None:
                return raw[source]
        if default is not None:
            return pd.Series([default] * len(raw))
        return pd.Series([None] * len(raw))

    frame = pd.DataFrame(
        {
            "index_name": index_name,
            "symbol": get("Symbol"),
            "company_name": get("Company Name", "Company"),
            "industry": get("Industry", "Sector"),
            "isin": get("ISIN Code", "ISIN"),
            "weight": pd.to_numeric(get("Weightage", "Weight", default=None), errors="coerce"),
            "as_of_date": as_of_date,
            "source_file": source_file,
        }
    )
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["isin"] = frame["isin"].astype(str).str.strip().replace({"nan": None})
    return frame[frame["symbol"] != ""]


def normalize_column_name(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def upsert_index_constituents(engine: Engine, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0

    rows = frame.where(pd.notna(frame), None).to_dict("records")
    statement = text(
        """
        insert into index_constituents (
            index_name, symbol, company_name, industry, isin, weight, as_of_date, source_file
        )
        values (
            :index_name, :symbol, :company_name, :industry, :isin, :weight, :as_of_date, :source_file
        )
        on conflict (index_name, symbol, as_of_date) do update set
            company_name = excluded.company_name,
            industry = excluded.industry,
            isin = excluded.isin,
            weight = excluded.weight,
            source_file = excluded.source_file,
            loaded_at = current_timestamp
        """
    )
    with engine.begin() as connection:
        connection.execute(statement, rows)
    return len(rows)


if __name__ == "__main__":
    main()
