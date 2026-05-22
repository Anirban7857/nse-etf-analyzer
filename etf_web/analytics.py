from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ETFAnalytics:
    dataset: pd.DataFrame

    def apply_filters(self, query: str = "", category: str = "", issuer: str = "") -> pd.DataFrame:
        filtered = self.dataset.copy()

        if query:
            query_lower = query.lower()
            mask = filtered["symbol"].str.lower().str.contains(query_lower) | filtered["fund_name"].str.lower().str.contains(query_lower)
            filtered = filtered[mask]

        if category:
            filtered = filtered[filtered["category"] == category]

        if issuer:
            filtered = filtered[filtered["issuer"] == issuer]

        return filtered.sort_values(["analysis_score", "aum_cr"], ascending=[False, False], na_position="last")

    def distinct_values(self, column: str) -> list[str]:
        values = self.dataset[column]
        return sorted(value for value in values.dropna().astype(str).unique() if value and value != "nan")

    def summary(self, df: pd.DataFrame) -> dict[str, str]:
        total_aum = df["aum_cr"].sum(min_count=1)
        average_expense = df["expense_ratio"].mean()
        average_return = df["one_year_return"].mean()
        average_score = df["analysis_score"].mean()

        return {
            "count": str(len(df)),
            "total_aum": self._format_number(total_aum, prefix="Rs ", suffix=" Cr"),
            "avg_expense": self._format_number(average_expense, suffix="%"),
            "avg_return": self._format_number(average_return, suffix="%"),
            "avg_score": self._format_number(average_score),
        }

    def breakdown(self, df: pd.DataFrame, column: str) -> list[dict[str, str]]:
        if df.empty:
            return []

        counts = df.groupby(column, dropna=False).size().sort_values(ascending=False)
        total = counts.sum()
        return [
            {"label": str(label), "count": int(count), "share": f"{(count / total) * 100:.1f}%"}
            for label, count in counts.items()
        ]

    def top_ranked(self, df: pd.DataFrame, column: str, ascending: bool) -> list[dict[str, str]]:
        subset = (
            df[df[column] != ""]
            .dropna(subset=[column])
            .sort_values(column, ascending=ascending)
            .head(5)
        )
        return self.records(subset)

    def records(self, df: pd.DataFrame) -> list[dict[str, str]]:
        records = []
        for row in df.to_dict(orient="records"):
            records.append(
                {
                    "symbol": row["symbol"],
                    "fund_name": row["fund_name"],
                    "category": row["category"],
                    "issuer": row["issuer"],
                    "aum_cr": self._format_number(row["aum_cr"], prefix="Rs ", suffix=" Cr"),
                    "expense_ratio": self._format_number(row["expense_ratio"], suffix="%"),
                    "nav": self._format_number(row["nav"], prefix="Rs "),
                    "close_price": self._format_number(row["close_price"], prefix="Rs "),
                    "one_year_return": self._format_number(row["one_year_return"], suffix="%"),
                    "three_year_return": self._format_number(row["three_year_return"], suffix="%"),
                    "volatility": self._format_number(row["volatility"], suffix="%"),
                    "tracking_error": self._format_number(row["tracking_error"], suffix="%"),
                    "analysis_score": self._format_number(row["analysis_score"]),
                }
            )
        return records

    @staticmethod
    def _format_number(value: object, prefix: str = "", suffix: str = "") -> str:
        if value == "" or pd.isna(value):
            return "N/A"
        return f"{prefix}{float(value):,.2f}{suffix}"
