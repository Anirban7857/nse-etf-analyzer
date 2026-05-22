from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class ExportBundle:
    master_csv_path: Path
    daily_csv_path: Path


def export_csv_bundle(dataset: pd.DataFrame, export_dir: Path, as_of: date | None = None) -> ExportBundle:
    export_dir.mkdir(parents=True, exist_ok=True)
    snapshot_date = as_of or date.today()

    master_csv_path = export_dir / "etf_master.generated.csv"
    daily_csv_path = export_dir / "etf_daily_ohlc.generated.csv"

    build_master_export(dataset).to_csv(master_csv_path, index=False)
    build_daily_export(dataset, snapshot_date).to_csv(daily_csv_path, index=False)

    return ExportBundle(master_csv_path=master_csv_path, daily_csv_path=daily_csv_path)


def build_master_export(dataset: pd.DataFrame) -> pd.DataFrame:
    master = pd.DataFrame(
        {
            "symbol": dataset["symbol"].fillna("").astype(str).str.upper().str.strip(),
            "name": dataset["fund_name"].fillna(dataset["symbol"]).astype(str).str.strip(),
            "issuer": dataset["issuer"].fillna("Unknown").astype(str).str.strip(),
            "category": dataset["category"].fillna("Unclassified").astype(str).str.strip(),
            "isin": pd.NA,
            "listing_date": pd.NA,
            "is_active": True,
        }
    )
    master = master[master["symbol"] != ""].drop_duplicates(subset=["symbol"], keep="last")
    return master.sort_values("symbol").reset_index(drop=True)


def build_daily_export(dataset: pd.DataFrame, snapshot_date: date) -> pd.DataFrame:
    close_values = pd.to_numeric(dataset["close_price"], errors="coerce")

    daily = pd.DataFrame(
        {
            "symbol": dataset["symbol"].fillna("").astype(str).str.upper().str.strip(),
            "trade_date": snapshot_date.isoformat(),
            "open": close_values,
            "high": close_values,
            "low": close_values,
            "close": close_values,
            "volume": pd.NA,
            "turnover": pd.NA,
            "source": "app_generated_snapshot",
        }
    )
    daily = daily[daily["symbol"] != ""].drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return daily.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
