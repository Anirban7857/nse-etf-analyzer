from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import bindparam, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.db import create_db_engine, init_database
from etf_web.market_data_db import create_market_engine


NSE_HOME_URL = "https://www.nseindia.com"
NSE_ETF_URL = "https://www.nseindia.com/api/etf"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "etfs"

MASTER_UPSERT_SQL = text(
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
        true,
        current_timestamp
    )
    on conflict (symbol) do update set
        name = excluded.name,
        isin = excluded.isin,
        issuer = excluded.issuer,
        category = excluded.category,
        listing_date = excluded.listing_date,
        is_active = true,
        updated_at = current_timestamp
    """
)

OHLC_UPSERT_SQL = text(
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


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if args.market_database_url:
        os.environ["MARKET_DATABASE_URL"] = args.market_database_url

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    listed_etfs = fetch_nse_etfs()
    selected_etfs = select_oldest_by_category(listed_etfs)

    etf_engine = create_db_engine()
    init_database(etf_engine)
    market_engine = create_market_engine()

    with etf_engine.begin() as connection:
        connection.execute(MASTER_UPSERT_SQL, listed_etfs.to_dict(orient="records"))

    ohlc = load_bhavcopy_ohlc(
        market_engine=market_engine,
        symbols=selected_etfs["symbol"].tolist(),
        start_date=parse_date(args.start) if args.start else None,
        end_date=parse_date(args.end) if args.end else None,
    )
    if not ohlc.empty:
        ohlc["source"] = "bhavcopy_prices"
        with etf_engine.begin() as connection:
            connection.execute(OHLC_UPSERT_SQL, ohlc_records_for_db(ohlc))

    report = build_selection_report(listed_etfs, selected_etfs, ohlc)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_path = output_dir / f"nse_listed_etfs_selection_{stamp}.csv"
    included_path = output_dir / f"nse_included_oldest_category_etfs_{stamp}.csv"
    excluded_path = output_dir / f"nse_excluded_etfs_{stamp}.csv"
    ohlc_path = output_dir / f"included_etf_ohlc_loaded_{stamp}.csv"

    report.to_csv(all_path, index=False)
    report[report["included"]].to_csv(included_path, index=False)
    report[~report["included"]].to_csv(excluded_path, index=False)
    ohlc.to_csv(ohlc_path, index=False)

    print(f"NSE listed ETFs: {len(listed_etfs)}")
    print(f"Categories selected: {selected_etfs['category'].nunique()}")
    print(f"Included ETFs: {len(selected_etfs)}")
    print(f"Excluded ETFs: {len(listed_etfs) - len(selected_etfs)}")
    print(f"OHLC rows loaded: {len(ohlc)}")
    print(f"Latest OHLC date: {ohlc['trade_date'].max() if not ohlc.empty else 'none'}")
    print(f"All ETF selection CSV: {all_path}")
    print(f"Included CSV: {included_path}")
    print(f"Excluded CSV: {excluded_path}")
    print(f"Loaded OHLC CSV: {ohlc_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch all listed NSE ETFs, select the oldest ETF per normalized category, "
            "and load OHLC for only those selected ETFs from bhavcopy_prices."
        )
    )
    parser.add_argument("--start", help="Optional OHLC start date in YYYY-MM-DD format.")
    parser.add_argument("--end", help="Optional OHLC end date in YYYY-MM-DD format.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated CSV reports.")
    parser.add_argument("--database-url", help="ETF PostgreSQL SQLAlchemy URL. Overrides DATABASE_URL.")
    parser.add_argument("--market-database-url", help="Market PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    return parser.parse_args()


def fetch_nse_etfs() -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/exchange-traded-funds-etf",
        }
    )
    session.get(NSE_HOME_URL, timeout=30)
    response = session.get(NSE_ETF_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") or []
    if not rows:
        raise ValueError("NSE ETF endpoint returned no ETF rows.")

    normalized_rows = []
    for row in rows:
        meta = row.get("meta") or {}
        symbol = clean_text(row.get("symbol") or meta.get("symbol")).upper()
        if not symbol:
            continue
        name = clean_text(meta.get("companyName") or symbol)
        assets = clean_text(row.get("assets"))
        normalized_rows.append(
            {
                "symbol": symbol,
                "name": name,
                "isin": clean_text(meta.get("isin")) or None,
                "issuer": infer_issuer(name),
                "category": infer_category(assets, name),
                "nse_assets": assets,
                "listing_date": parse_nse_date(meta.get("listingDate")),
                "series": clean_text(row.get("series") or "EQ"),
            }
        )

    frame = pd.DataFrame(normalized_rows).drop_duplicates(subset=["symbol"], keep="last")
    frame["listing_date"] = pd.to_datetime(frame["listing_date"], errors="coerce").dt.date
    frame = frame.sort_values(["category", "listing_date", "symbol"], na_position="last").reset_index(drop=True)
    return frame


def select_oldest_by_category(listed_etfs: pd.DataFrame) -> pd.DataFrame:
    sortable = listed_etfs.copy()
    sortable["sort_listing_date"] = pd.to_datetime(sortable["listing_date"], errors="coerce")
    sortable["sort_listing_date"] = sortable["sort_listing_date"].fillna(pd.Timestamp.max)
    selected = (
        sortable.sort_values(["category", "sort_listing_date", "symbol"])
        .groupby("category", as_index=False)
        .first()
        .drop(columns=["sort_listing_date"])
    )
    return selected.sort_values(["category", "symbol"]).reset_index(drop=True)


def load_bhavcopy_ohlc(
    market_engine,
    symbols: list[str],
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    conditions = [
        "symbol in :symbols",
        "series = 'EQ'",
        "open_price is not null",
        "high_price is not null",
        "low_price is not null",
        "close_price is not null",
    ]
    params: dict[str, object] = {"symbols": symbols}
    if start_date:
        conditions.append("trade_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("trade_date <= :end_date")
        params["end_date"] = end_date

    statement = (
        text(
            f"""
            select
                symbol,
                trade_date,
                open_price as open,
                high_price as high,
                low_price as low,
                close_price as close,
                volume,
                turnover
            from bhavcopy_prices
            where {' and '.join(conditions)}
            order by symbol, trade_date
            """
        )
        .bindparams(bindparam("symbols", expanding=True))
    )
    with market_engine.begin() as connection:
        frame = pd.read_sql(statement, connection, params=params)
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    for column in ["open", "high", "low", "close", "turnover"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame[column] = frame[column].where(frame[column].notna(), None)
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["volume"] = frame["volume"].apply(lambda value: int(value) if pd.notna(value) else None)
    return frame


def build_selection_report(
    listed_etfs: pd.DataFrame,
    selected_etfs: pd.DataFrame,
    ohlc: pd.DataFrame,
) -> pd.DataFrame:
    selected_symbols = set(selected_etfs["symbol"])
    selected_by_category = dict(zip(selected_etfs["category"], selected_etfs["symbol"]))
    if ohlc.empty:
        ohlc_stats = pd.DataFrame(columns=["symbol", "ohlc_rows", "first_ohlc_date", "last_ohlc_date"])
    else:
        ohlc_stats = (
            ohlc.groupby("symbol")
            .agg(
                ohlc_rows=("trade_date", "size"),
                first_ohlc_date=("trade_date", "min"),
                last_ohlc_date=("trade_date", "max"),
            )
            .reset_index()
        )

    report = listed_etfs.merge(ohlc_stats, on="symbol", how="left")
    report["included"] = report["symbol"].isin(selected_symbols)
    report["selected_symbol_for_category"] = report["category"].map(selected_by_category)
    report["selection_reason"] = report.apply(selection_reason, axis=1)
    report["ohlc_rows"] = report["ohlc_rows"].fillna(0).astype(int)
    return report.sort_values(["category", "included", "listing_date", "symbol"], ascending=[True, False, True, True])


def ohlc_records_for_db(ohlc: pd.DataFrame) -> list[dict[str, object]]:
    records = []
    for row in ohlc.itertuples(index=False):
        records.append(
            {
                "symbol": row.symbol,
                "trade_date": row.trade_date,
                "open": none_if_na(row.open),
                "high": none_if_na(row.high),
                "low": none_if_na(row.low),
                "close": none_if_na(row.close),
                "volume": int(row.volume) if pd.notna(row.volume) else None,
                "turnover": none_if_na(row.turnover),
                "source": row.source,
            }
        )
    return records


def none_if_na(value):
    return None if pd.isna(value) else value


def selection_reason(row) -> str:
    if row["included"]:
        return "included_oldest_listed_etf_for_category"
    return f"excluded_newer_than_{row['selected_symbol_for_category']}"


def infer_category(assets: str, name: str) -> str:
    text_value = normalize_spaces(f"{assets} {name}")
    lowered = text_value.lower()
    if "silver" in lowered:
        return "Silver"
    if "gold" in lowered:
        return "Gold"
    if any(token in lowered for token in ["1d rate", "liquid", "overnight"]):
        return "Liquid"
    if "government securit" in lowered or "g-sec" in lowered or "bharat bond" in lowered:
        return normalize_category(assets or name)
    return normalize_category(assets or name)


def normalize_category(value: str) -> str:
    category = clean_text(value)
    category = re.sub(r"\b(total return|tri|index|etf|exchange traded fund)\b", "", category, flags=re.IGNORECASE)
    category = re.sub(r"\s+", " ", category).strip(" -")
    if not category:
        return "Unclassified"
    replacements = {
        "NIFTY": "Nifty",
        "SENSEX": "Sensex",
        "MSCI": "MSCI",
        "BSE": "BSE",
        "PSU": "PSU",
        "IT": "IT",
    }
    words = []
    for word in category.split():
        upper = word.upper()
        words.append(replacements.get(upper, word.capitalize()))
    return " ".join(words)


def infer_issuer(name: str) -> str:
    upper_name = name.upper()
    known_issuers = [
        ("NIPPON", "Nippon India"),
        ("ICICI", "ICICI Prudential"),
        ("HDFC", "HDFC Mutual Fund"),
        ("SBI", "SBI Mutual Fund"),
        ("KOTAK", "Kotak Mutual Fund"),
        ("UTI", "UTI Mutual Fund"),
        ("MIRAE", "Mirae Asset"),
        ("MOTILAL", "Motilal Oswal"),
        ("AXIS", "Axis Mutual Fund"),
        ("DSP", "DSP Mutual Fund"),
        ("TATA", "Tata Mutual Fund"),
        ("ZERODHA", "Zerodha Fund House"),
        ("ADITYA BIRLA", "Aditya Birla Sun Life"),
        ("ABSL", "Aditya Birla Sun Life"),
        ("QUANTUM", "Quantum Mutual Fund"),
        ("EDELWEISS", "Edelweiss Mutual Fund"),
        ("BANDHAN", "Bandhan Mutual Fund"),
        ("BAJAJ", "Bajaj Finserv"),
        ("MAHINDRA", "Mahindra Manulife"),
        ("LIC", "LIC Mutual Fund"),
        ("INVESCO", "Invesco Mutual Fund"),
    ]
    for marker, issuer in known_issuers:
        if marker in upper_name:
            return issuer
    return "Unknown"


def parse_nse_date(value: object) -> date | None:
    text_value = clean_text(value)
    if not text_value or text_value == "-":
        return None
    parsed = pd.to_datetime(text_value, errors="coerce", dayfirst=False)
    if pd.isna(parsed):
        parsed = pd.to_datetime(text_value, errors="coerce", dayfirst=True)
    return parsed.date() if not pd.isna(parsed) else None


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return normalize_spaces(str(value).replace("\xa0", " ")).strip()


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


if __name__ == "__main__":
    main()
