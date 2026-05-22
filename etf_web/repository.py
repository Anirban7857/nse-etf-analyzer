from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pandas as pd


COLUMN_ALIASES = {
    "symbol": ["symbol", "ticker", "tradingsymbol", "trading_symbol"],
    "fund_name": ["fund_name", "fund", "name", "scheme_name", "etf_name"],
    "category": ["category", "theme", "asset_class", "segment"],
    "issuer": ["issuer", "amc", "fund_house", "provider"],
    "aum_cr": ["aum_cr", "aum", "aum_inr_cr", "assets_cr"],
    "expense_ratio": ["expense_ratio", "expense", "expense_ratio_pct", "ter"],
    "nav": ["nav", "latest_nav", "nav_inr"],
    "close_price": ["close_price", "price", "last_price", "ltp"],
    "one_year_return": ["one_year_return", "1y_return", "return_1y", "one_year_return_pct"],
    "three_year_return": ["three_year_return", "3y_return", "return_3y", "three_year_return_pct"],
    "volatility": ["volatility", "std_dev", "risk", "volatility_pct"],
    "tracking_error": ["tracking_error", "tracking_diff", "tracking_error_pct"],
}

NUMERIC_COLUMNS = [
    "aum_cr",
    "expense_ratio",
    "nav",
    "close_price",
    "one_year_return",
    "three_year_return",
    "volatility",
    "tracking_error",
]


@dataclass
class InstrumentRepository:
    dataset_path: Path

    def load(self) -> pd.DataFrame:
        return load_dataset(self.dataset_path)


def load_dataset(source: str | Path | BinaryIO) -> pd.DataFrame:
    df = pd.read_csv(source)
    normalized = _normalize_columns(df)
    normalized = _coerce_numeric(normalized)
    normalized["analysis_score"] = _build_score(normalized)
    return normalized


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    lowered = {column.lower().strip(): column for column in df.columns}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                rename_map[lowered[alias]] = canonical
                break

    renamed = df.rename(columns=rename_map).copy()

    for canonical in COLUMN_ALIASES:
        if canonical not in renamed.columns:
            renamed[canonical] = pd.NA

    subset = renamed[list(COLUMN_ALIASES.keys())].copy()
    subset["symbol"] = subset["symbol"].fillna("UNKNOWN").astype(str).str.upper()
    subset["fund_name"] = subset["fund_name"].fillna(subset["symbol"]).astype(str)
    subset["category"] = subset["category"].fillna("Unclassified").astype(str)
    subset["issuer"] = subset["issuer"].fillna("Unknown").astype(str)
    return subset


def _coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    for column in NUMERIC_COLUMNS:
        cleaned[column] = (
            cleaned[column]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("%", "", regex=False)
            .str.replace("₹", "", regex=False)
            .replace({"nan": pd.NA, "": pd.NA, "<NA>": pd.NA})
        )
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
    return cleaned


def _build_score(df: pd.DataFrame) -> pd.Series:
    components = pd.DataFrame(index=df.index)
    components["aum"] = _scale(df["aum_cr"], higher_is_better=True)
    components["expense"] = _scale(df["expense_ratio"], higher_is_better=False)
    components["return_1y"] = _scale(df["one_year_return"], higher_is_better=True)
    components["return_3y"] = _scale(df["three_year_return"], higher_is_better=True)
    components["volatility"] = _scale(df["volatility"], higher_is_better=False)
    components["tracking"] = _scale(df["tracking_error"], higher_is_better=False)

    weights = {
        "aum": 0.20,
        "expense": 0.20,
        "return_1y": 0.20,
        "return_3y": 0.20,
        "volatility": 0.10,
        "tracking": 0.10,
    }
    weighted = sum(components[column].fillna(0.5) * weight for column, weight in weights.items())
    return (weighted * 100).round(1)


def _scale(series: pd.Series, higher_is_better: bool) -> pd.Series:
    valid = series.dropna()
    if valid.empty:
        return pd.Series([pd.NA] * len(series), index=series.index, dtype="float64")

    minimum = valid.min()
    maximum = valid.max()
    if minimum == maximum:
        scaled = pd.Series(0.5, index=series.index)
    else:
        scaled = (series - minimum) / (maximum - minimum)

    return scaled if higher_is_better else 1 - scaled
