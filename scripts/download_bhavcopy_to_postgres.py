from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from time import sleep
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zipfile import ZipFile

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import get_market_database_url, init_market_database

DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "bhavcopy"
SWITCH_DATE = date(2024, 7, 8)
MONTH_CODES = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}


@dataclass(frozen=True)
class DownloadResult:
    trade_date: date
    status: str
    source_url: str
    source_file: str | None = None
    content: bytes | None = None
    error_message: str | None = None


class NseClient:
    def __init__(self) -> None:
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/all-reports",
        }
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def warm_up(self) -> None:
        self.get("https://www.nseindia.com/all-reports", timeout=20)

    def get(self, url: str, timeout: int = 30) -> tuple[int, bytes]:
        request = Request(url, headers=self.headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
        except TimeoutError as exc:
            raise ConnectionError(str(exc)) from exc
        except URLError as exc:
            raise ConnectionError(str(exc.reason)) from exc


def main() -> None:
    args = parse_args()
    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    if args.init_only:
        init_market_database(create_database=True)
        print(f"Initialized PostgreSQL database: {get_market_database_url()}")
        return

    engine = init_market_database(create_database=True)
    client = create_nse_client()

    for trade_date in date_range(start_date, end_date):
        if trade_date.weekday() >= 5 and not args.include_weekends:
            continue

        if not args.force and is_successfully_loaded(engine, trade_date):
            print(f"{trade_date}: already loaded")
            continue

        result = download_bhavcopy(client, trade_date, raw_dir, force=args.force)
        if result.status != "downloaded" or not result.content:
            write_download_log(engine, result, row_count=0)
            print(f"{trade_date}: {result.status} {result.error_message or ''}".strip())
            sleep(args.sleep)
            continue

        try:
            frame = parse_bhavcopy_zip(result.content, trade_date, result.source_file or "")
            if args.series:
                frame = frame[frame["series"].isin(args.series)]
            if args.symbols:
                symbols = load_symbol_filter(Path(args.symbols))
                frame = frame[frame["symbol"].isin(symbols)]

            row_count = upsert_bhavcopy_prices(engine, frame)
            write_download_log(engine, result, row_count=row_count)
            print(f"{trade_date}: loaded {row_count} rows from {result.source_file}")
        except Exception as exc:
            failed = DownloadResult(
                trade_date=trade_date,
                status="parse_or_load_failed",
                source_url=result.source_url,
                source_file=result.source_file,
                error_message=str(exc),
            )
            write_download_log(engine, failed, row_count=0)
            print(f"{trade_date}: failed {exc}")

        sleep(args.sleep)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download NSE CM bhavcopy files into PostgreSQL.")
    default_end = date.today()
    default_start = default_end.replace(year=default_end.year - 7)
    parser.add_argument("--start", default=default_start.isoformat(), help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date in YYYY-MM-DD format.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Directory for raw downloaded ZIP files.")
    parser.add_argument(
        "--database-url",
        help=(
            "PostgreSQL SQLAlchemy URL. Defaults to MARKET_DATABASE_URL, DATABASE_URL, "
            "or postgresql+psycopg://postgres@localhost:5432/nse_market_data."
        ),
    )
    parser.add_argument("--symbols", help="Optional CSV/text file containing symbols to load.")
    parser.add_argument("--series", action="append", default=["EQ"], help="Series to keep. Repeat for multiple series.")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds to wait between NSE requests.")
    parser.add_argument("--force", action="store_true", help="Redownload/reload dates already marked successful.")
    parser.add_argument("--include-weekends", action="store_true", help="Try weekends too.")
    parser.add_argument("--init-only", action="store_true", help="Only create database/tables and exit.")
    return parser.parse_args()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def create_nse_client() -> NseClient:
    client = NseClient()
    client.warm_up()
    return client


def bhavcopy_urls(trade_date: date) -> list[str]:
    if trade_date >= SWITCH_DATE:
        ymd = trade_date.strftime("%Y%m%d")
        return [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F.csv.zip",
        ]

    year = trade_date.strftime("%Y")
    month_code = MONTH_CODES[trade_date.month]
    date_code = f"{trade_date.day:02d}{month_code}{year}"
    return [
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{year}/{month_code}/cm{date_code}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{trade_date.strftime('%d%m%Y')}.csv",
    ]


def download_bhavcopy(
    client: NseClient,
    trade_date: date,
    raw_dir: Path,
    force: bool = False,
) -> DownloadResult:
    target_dir = raw_dir / str(trade_date.year)
    target_dir.mkdir(parents=True, exist_ok=True)

    last_error = None
    for url in bhavcopy_urls(trade_date):
        source_file = url.rsplit("/", 1)[-1]
        target_path = target_dir / source_file
        if target_path.exists() and not force:
            return DownloadResult(
                trade_date=trade_date,
                status="downloaded",
                source_url=url,
                source_file=source_file,
                content=target_path.read_bytes(),
            )

        try:
            status_code, content = client.get(url, timeout=30)
        except ConnectionError as exc:
            last_error = str(exc)
            continue

        if status_code == 200 and content:
            if source_file.endswith(".zip") and content[:2] != b"PK":
                last_error = "Response was not a ZIP file."
                continue
            target_path.write_bytes(content)
            return DownloadResult(
                trade_date=trade_date,
                status="downloaded",
                source_url=url,
                source_file=source_file,
                content=content,
            )

        last_error = f"HTTP {status_code}"

    return DownloadResult(
        trade_date=trade_date,
        status="not_available",
        source_url=bhavcopy_urls(trade_date)[0],
        error_message=last_error,
    )


def parse_bhavcopy_zip(content: bytes, trade_date: date, source_file: str) -> pd.DataFrame:
    if source_file.endswith(".zip"):
        with ZipFile(BytesIO(content)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("ZIP did not contain a CSV file.")
            with archive.open(csv_names[0]) as csv_file:
                raw = pd.read_csv(csv_file)
    else:
        raw = pd.read_csv(BytesIO(content))

    normalized = normalize_bhavcopy_columns(raw, trade_date, source_file)
    normalized = normalized.dropna(subset=["symbol", "trade_date"])
    normalized["symbol"] = normalized["symbol"].astype(str).str.strip().str.upper()
    normalized["series"] = normalized["series"].fillna("").astype(str).str.strip().str.upper()
    return normalized[normalized["symbol"] != ""]


def normalize_bhavcopy_columns(raw: pd.DataFrame, trade_date: date, source_file: str) -> pd.DataFrame:
    column_map = {normalize_column_name(column): column for column in raw.columns}

    def get(*names: str, default=None):
        for name in names:
            source = column_map.get(normalize_column_name(name))
            if source is not None:
                return raw[source]
        if default is not None:
            return pd.Series([default] * len(raw))
        return pd.Series([None] * len(raw))

    trade_dates = get("trad_dt", "trad_dt_tm", "trad_dt_dt", "trad_dt_date", "trad_dt", "timestamp", default=trade_date)
    parsed_dates = parse_trade_dates(trade_dates, trade_date)

    frame = pd.DataFrame(
        {
            "trade_date": parsed_dates,
            "symbol": get("symbol", "tckrsymb", "tckr_symb", "security_symbol"),
            "series": get("series", "sctysrs", "scty_srs", default="EQ"),
            "open_price": parse_number(get("open", "opnpric", "opn_pric", "open_price")),
            "high_price": parse_number(get("high", "hghpric", "hgh_pric", "high_price")),
            "low_price": parse_number(get("low", "lwpric", "lw_pric", "low_price")),
            "close_price": parse_number(get("close", "clspric", "cls_pric", "close_price")),
            "prev_close": parse_number(get("prevclose", "prvsclsgpric", "prvs_clsg_pric", "prev_close")),
            "last_price": parse_number(get("last", "lastpric", "last_pric", "last_price")),
            "volume": parse_integer(get("tottrdqty", "ttltradgvol", "ttl_tradg_vol", "total_traded_quantity")),
            "turnover": parse_number(get("tottrdval", "ttltrfval", "ttl_trf_val", "total_traded_value")),
            "total_trades": parse_integer(get("totaltrades", "ttlnboftxsexctd", "ttl_nb_of_txs_exctd", "total_trades")),
            "isin": get("isin", "isin_cd", "isin_code"),
            "source_file": source_file,
        }
    )
    return frame


def normalize_column_name(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def parse_trade_dates(series: pd.Series, fallback: date) -> pd.Series:
    values = series.astype(str).str.strip()
    iso_mask = values.str.match(r"^\d{4}-\d{2}-\d{2}")
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    parsed.loc[iso_mask] = pd.to_datetime(values.loc[iso_mask], errors="coerce")
    parsed.loc[~iso_mask] = pd.to_datetime(values.loc[~iso_mask], errors="coerce", dayfirst=True)
    return parsed.dt.date.fillna(fallback)


def parse_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")


def parse_integer(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce").astype("Int64")


def load_symbol_filter(path: Path) -> set[str]:
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
        symbol_column = next((column for column in frame.columns if normalize_column_name(column) == "symbol"), frame.columns[0])
        values = frame[symbol_column]
    else:
        values = path.read_text().splitlines()
    return {str(value).strip().upper() for value in values if str(value).strip()}


def upsert_bhavcopy_prices(engine: Engine, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0

    rows = frame.where(pd.notna(frame), None).to_dict("records")
    table = text(
        """
        insert into bhavcopy_prices (
            trade_date, symbol, series, open_price, high_price, low_price, close_price,
            prev_close, last_price, volume, turnover, total_trades, isin, source_file
        )
        values (
            :trade_date, :symbol, :series, :open_price, :high_price, :low_price, :close_price,
            :prev_close, :last_price, :volume, :turnover, :total_trades, :isin, :source_file
        )
        on conflict (trade_date, symbol, series) do update set
            open_price = excluded.open_price,
            high_price = excluded.high_price,
            low_price = excluded.low_price,
            close_price = excluded.close_price,
            prev_close = excluded.prev_close,
            last_price = excluded.last_price,
            volume = excluded.volume,
            turnover = excluded.turnover,
            total_trades = excluded.total_trades,
            isin = excluded.isin,
            source_file = excluded.source_file,
            loaded_at = current_timestamp
        """
    )
    with engine.begin() as connection:
        connection.execute(table, rows)
    return len(rows)


def write_download_log(engine: Engine, result: DownloadResult, row_count: int) -> None:
    statement = text(
        """
        insert into download_log (
            trade_date, source_url, source_file, status, row_count, error_message, downloaded_at
        )
        values (
            :trade_date, :source_url, :source_file, :status, :row_count, :error_message, current_timestamp
        )
        on conflict (trade_date) do update set
            source_url = excluded.source_url,
            source_file = excluded.source_file,
            status = excluded.status,
            row_count = excluded.row_count,
            error_message = excluded.error_message,
            downloaded_at = current_timestamp
        """
    )
    with engine.begin() as connection:
        connection.execute(
            statement,
            {
                "trade_date": result.trade_date,
                "source_url": result.source_url,
                "source_file": result.source_file,
                "status": result.status,
                "row_count": row_count,
                "error_message": result.error_message,
            },
        )


def is_successfully_loaded(engine: Engine, trade_date: date) -> bool:
    statement = text("select 1 from download_log where trade_date = :trade_date and status = 'downloaded'")
    with engine.begin() as connection:
        return connection.execute(statement, {"trade_date": trade_date}).scalar() is not None


if __name__ == "__main__":
    main()
