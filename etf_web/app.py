from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_from_directory
from sqlalchemy import text

from .analytics import ETFAnalytics
from .drawdown_excel import write_drawdown_workbook
from .exports import export_csv_bundle
from .market_data_db import create_market_engine
from .repository import InstrumentRepository, load_dataset
from .sip_calculator import (
    available_months,
    calculate_drawdown_switch,
    calculate_sip,
    load_monthly_data,
    parse_drawdown_slabs,
    parse_inputs,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    app.config["SECRET_KEY"] = "nse-etf-analyzer"

    default_dataset = Path(__file__).resolve().parent.parent / "data" / "nse_etfs_sample.csv"
    export_dir = Path(__file__).resolve().parent.parent / "data" / "generated"
    repository = InstrumentRepository(default_dataset)

    @app.route("/", methods=["GET", "POST"])
    def index():
        file_storage = request.files.get("dataset")
        source_name = "Bundled ETF sample dataset"
        error_message = ""
        generation_message = ""
        sip_error_message = ""
        sip_result = None
        drawdown_summary = None
        strategy_comparison = None
        strategy_series = None
        sip_form = None
        sip_months: list[str] = []
        sip_symbols: list[str] = []
        selected_sip_symbol = ""
        drawdown_slabs = parse_drawdown_slabs(request.values)

        try:
            if request.method == "POST" and file_storage and file_storage.filename:
                dataset = load_dataset(file_storage)
                source_name = file_storage.filename
            else:
                dataset = repository.load()
        except Exception:
            dataset = repository.load()
            error_message = "The uploaded CSV could not be parsed. The bundled ETF sample dataset is shown instead."

        exports = export_csv_bundle(dataset, export_dir)
        generation_message = (
            "CSV exports were generated for this run. "
            "The daily OHLC file is a current-day snapshot scaffold built from the dataset's close price, "
            "not exchange historical OHLC."
        )

        monthly_paths = _monthly_data_paths(export_dir)
        daily_paths = _daily_data_paths(export_dir)
        sip_symbols = sorted(monthly_paths)
        requested_symbol = request.values.get("sip_symbol", "GOLDBEES").strip().upper()
        if requested_symbol not in monthly_paths and sip_symbols:
            requested_symbol = sip_symbols[0]
        selected_sip_symbol = requested_symbol

        if selected_sip_symbol in monthly_paths:
            monthly_data = load_monthly_data(monthly_paths[selected_sip_symbol])
            sip_months = available_months(monthly_data)
            sip_inputs = parse_inputs(request.values, monthly_data)
            sip_form = {
                "symbol": selected_sip_symbol,
                "mode": sip_inputs.mode,
                "monthly_amount": f"{sip_inputs.monthly_amount:.2f}",
                "annual_step_up_pct": f"{sip_inputs.annual_step_up_pct:.2f}",
                "debt_annual_return_pct": f"{sip_inputs.debt_annual_return_pct:.2f}",
                "start_month": sip_inputs.start_month,
                "end_month": sip_inputs.end_month,
            }
            try:
                sip_result = calculate_sip(monthly_data, sip_inputs)
                strategy_comparison = _calculate_strategy_comparison(
                    export_dir,
                    selected_sip_symbol,
                    sip_inputs,
                    drawdown_slabs,
                )
                strategy_series = strategy_comparison["series"]
                if request.values.get("calculate_drawdown_summary") == "1":
                    drawdown_summary = _calculate_drawdown_result(
                        export_dir,
                        selected_sip_symbol,
                        sip_inputs,
                        drawdown_slabs,
                    )
            except ValueError as exc:
                sip_error_message = str(exc)
        else:
            sip_error_message = (
                "Monthly ETF data file is missing. "
                "Add files like data/generated/goldbees_monthly_ohlc_investing.csv or "
                "data/generated/juniorbees_monthly_ohlc_investing.csv to use the SIP calculator."
            )

        filters = {
            "query": request.values.get("query", "").strip(),
            "category": request.values.get("category", "").strip(),
            "issuer": request.values.get("issuer", "").strip(),
        }

        analytics = ETFAnalytics(dataset)
        filtered = analytics.apply_filters(**filters)

        context = {
            "source_name": source_name,
            "error_message": error_message,
            "filters": filters,
            "summary": analytics.summary(filtered),
            "records": analytics.records(filtered),
            "category_breakdown": analytics.breakdown(filtered, "category"),
            "issuer_breakdown": analytics.breakdown(filtered, "issuer"),
            "top_expense": analytics.top_ranked(filtered, "expense_ratio", ascending=True),
            "top_return": analytics.top_ranked(filtered, "one_year_return", ascending=False),
            "top_score": analytics.top_ranked(filtered, "analysis_score", ascending=False),
            "categories": analytics.distinct_values("category"),
            "issuers": analytics.distinct_values("issuer"),
            "generation_message": generation_message,
            "generated_master_name": exports.master_csv_path.name,
            "generated_daily_name": exports.daily_csv_path.name,
            "sip_error_message": sip_error_message,
            "sip_result": sip_result,
            "drawdown_summary": drawdown_summary,
            "strategy_comparison": strategy_comparison["summary"] if strategy_comparison else None,
            "strategy_series": strategy_series,
            "sip_form": sip_form,
            "sip_months": sip_months,
            "sip_symbols": sip_symbols,
            "selected_sip_symbol": selected_sip_symbol,
            "drawdown_slabs": drawdown_slabs,
        }
        return render_template("index.html", **context)

    @app.route("/drawdown-report")
    def download_drawdown_report():
        monthly_paths = _monthly_data_paths(export_dir)
        daily_paths = _daily_data_paths(export_dir)
        selected_symbol = request.values.get("sip_symbol", "GOLDBEES").strip().upper()
        if selected_symbol not in monthly_paths:
            raise ValueError("Selected ETF monthly data is not available.")

        monthly_data = load_monthly_data(monthly_paths[selected_symbol])
        sip_inputs = parse_inputs(request.values, monthly_data)
        drawdown_result = _calculate_drawdown_result(
            export_dir,
            selected_symbol,
            sip_inputs,
            parse_drawdown_slabs(request.values),
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{selected_symbol.lower()}_drawdown_switch_{timestamp}.generated.xlsx"
        write_drawdown_workbook(export_dir / filename, drawdown_result)
        return send_from_directory(export_dir, filename, as_attachment=True)

    @app.route("/generated/<path:filename>")
    def download_generated(filename: str):
        return send_from_directory(export_dir, filename, as_attachment=True)

    @app.route("/stock-candles")
    def stock_candles():
        symbol = request.values.get("symbol", "RELIANCE").strip().upper() or "RELIANCE"
        timeframe = request.values.get("timeframe", "daily").strip().lower()
        candles = _load_stock_candles(symbol, timeframe)
        return jsonify({"symbol": symbol, "timeframe": timeframe, "candles": candles})

    @app.route("/strategy-comparison-data")
    def strategy_comparison_data():
        backtest_dir = export_dir / "backtests"
        return jsonify(_load_total_market_strategy_comparisons(backtest_dir))

    @app.route("/strategy-equity-series")
    def strategy_equity_series():
        backtest_dir = export_dir / "backtests"
        return jsonify(_load_total_market_strategy_equity_series(backtest_dir))

    return app


def _calculate_drawdown_result(export_dir: Path, selected_symbol: str, sip_inputs, drawdown_slabs):
    monthly_paths = _monthly_data_paths(export_dir)
    daily_paths = _daily_data_paths(export_dir)
    drawdown_inputs = replace(sip_inputs, mode="drawdown_switch")
    drawdown_data = load_monthly_data(daily_paths.get(selected_symbol, monthly_paths[selected_symbol]))
    drawdown_subset = drawdown_data[
        (drawdown_data["month"] >= drawdown_inputs.start_month)
        & (drawdown_data["month"] <= drawdown_inputs.end_month)
    ].copy()
    if drawdown_subset.empty:
        raise ValueError("No drawdown data available for the selected range.")

    prior_history = drawdown_data[
        drawdown_data["trade_date"] < drawdown_subset.iloc[0]["trade_date"]
    ]
    return calculate_drawdown_switch(
        drawdown_subset,
        drawdown_inputs,
        prior_history,
        drawdown_slabs,
    )


def _calculate_strategy_comparison(export_dir: Path, selected_symbol: str, sip_inputs, drawdown_slabs):
    monthly_paths = _monthly_data_paths(export_dir)
    monthly_data = load_monthly_data(monthly_paths[selected_symbol])
    sip_result = calculate_sip(monthly_data, replace(sip_inputs, mode="sip"))
    step_up_result = calculate_sip(monthly_data, replace(sip_inputs, mode="step_up_sip"))
    drawdown_result = _calculate_drawdown_result(export_dir, selected_symbol, sip_inputs, drawdown_slabs)

    return {
        "summary": [
            _comparison_row("SIP", sip_result, sip_result["total_invested"]),
            _comparison_row("Step-up SIP", step_up_result, step_up_result["total_invested"]),
            _comparison_row("Drawdown", drawdown_result, drawdown_result["total_contributed"]),
        ],
        "series": _comparison_series(sip_result, step_up_result, drawdown_result),
    }


def _comparison_row(label: str, result: dict[str, object], invested: object) -> dict[str, object]:
    final_value = float(result["final_value"])
    invested_value = float(invested)
    gain = final_value - invested_value
    return {
        "label": label,
        "invested": round(invested_value, 2),
        "final_value": round(final_value, 2),
        "gain": round(gain, 2),
        "absolute_return_pct": round((gain / invested_value) * 100, 2) if invested_value else 0.0,
        "xirr_pct": result.get("xirr_pct"),
    }


def _comparison_series(
    sip_result: dict[str, object],
    step_up_result: dict[str, object],
    drawdown_result: dict[str, object],
) -> list[dict[str, object]]:
    sip_by_month = {
        row["month"]: row
        for row in sip_result.get("schedule", [])
    }
    step_up_by_month = {
        row["month"]: row
        for row in step_up_result.get("schedule", [])
    }
    drawdown_by_month: dict[str, dict[str, object]] = {}
    for row in drawdown_result.get("monthly_rows", []):
        drawdown_by_month[row["month"]] = row

    months = sorted(set(sip_by_month) & set(step_up_by_month) & set(drawdown_by_month))
    return [
        {
            "month": month,
            "amount_invested": step_up_by_month[month].get("cumulative_invested", 0),
            "sip_value": sip_by_month[month].get("portfolio_value_at_close", 0),
            "step_up_value": step_up_by_month[month].get("portfolio_value_at_close", 0),
            "drawdown_value": drawdown_by_month[month].get("portfolio_value_at_close", 0),
        }
        for month in months
    ]


def _monthly_data_paths(export_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in export_dir.glob("*_monthly_ohlc_investing.csv"):
        symbol = path.name.split("_")[0].upper()
        paths[symbol] = path
    for path in export_dir.glob("*.csv"):
        lower_name = path.name.lower()
        if "jbes" in lower_name or "junior" in lower_name:
            paths.setdefault("JUNIORBEES", path)
    return paths


def _daily_data_paths(export_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in export_dir.glob("*.csv"):
        lower_name = path.name.lower()
        if "daily" not in lower_name:
            continue
        if "jbes" in lower_name or "junior" in lower_name:
            paths["JUNIORBEES"] = path
        elif "gbes" in lower_name or "gold" in lower_name:
            paths["GOLDBEES"] = path
    return paths


def _load_stock_candles(symbol: str, timeframe: str) -> list[dict[str, object]]:
    prices = _load_stock_prices_from_database(symbol) if _should_use_market_database() else pd.DataFrame()
    if prices.empty:
        prices = _load_stock_prices_from_yfinance(symbol)
    if prices.empty:
        return []

    candles = _resample_stock_prices(prices, timeframe)
    if timeframe == "daily":
        candles = candles.tail(260)
    elif timeframe == "weekly":
        candles = candles.tail(156)
    elif timeframe == "monthly":
        candles = candles.tail(120)
    elif timeframe == "yearly":
        candles = candles.tail(30)

    return [
        {
            "date": row.trade_date.date().isoformat(),
            "open": round(float(row.open_price), 2),
            "high": round(float(row.high_price), 2),
            "low": round(float(row.low_price), 2),
            "close": round(float(row.close_price), 2),
        }
        for row in candles.itertuples(index=False)
    ]


def _should_use_market_database() -> bool:
    return bool(os.environ.get("MARKET_DATABASE_URL") or os.environ.get("DATABASE_URL"))


def _load_stock_prices_from_database(symbol: str) -> pd.DataFrame:
    try:
        engine = create_market_engine()
        with engine.begin() as connection:
            adjusted = pd.read_sql(
                text(
                    """
                    select trade_date, open_price, high_price, low_price, close_price
                    from stock_adjusted_prices
                    where symbol = :symbol
                      and source_provider = 'yfinance'
                      and open_price is not null
                      and high_price is not null
                      and low_price is not null
                      and close_price is not null
                    order by trade_date
                    """
                ),
                connection,
                params={"symbol": symbol},
            )
            if not adjusted.empty:
                return _normalize_stock_price_frame(adjusted)

            bhavcopy = pd.read_sql(
                text(
                    """
                    select trade_date, open_price, high_price, low_price, close_price
                    from bhavcopy_prices
                    where symbol = :symbol
                      and series = 'EQ'
                      and open_price is not null
                      and high_price is not null
                      and low_price is not null
                      and close_price is not null
                    order by trade_date
                    """
                ),
                connection,
                params={"symbol": symbol},
            )
            return _normalize_stock_price_frame(bhavcopy)
    except Exception:
        return pd.DataFrame()


def _load_stock_prices_from_yfinance(symbol: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()

    try:
        raw = yf.download(
            f"{symbol}.NS",
            period="max",
            interval="1d",
            auto_adjust=True,
            progress=False,
            actions=False,
            threads=False,
        )
    except Exception:
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    frame = raw.reset_index().rename(
        columns={
            "Date": "trade_date",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
        }
    )
    return _normalize_stock_price_frame(frame[["trade_date", "open_price", "high_price", "low_price", "close_price"]])


def _normalize_stock_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        return prices
    normalized = prices.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    for column in ["open_price", "high_price", "low_price", "close_price"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=["trade_date", "open_price", "high_price", "low_price", "close_price"])
    return normalized.sort_values("trade_date").reset_index(drop=True)


def _resample_stock_prices(prices: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe == "daily":
        return prices

    frequency = {
        "weekly": "W-FRI",
        "monthly": "ME",
        "yearly": "YE",
    }.get(timeframe, "D")
    if frequency == "D":
        return prices

    indexed = prices.set_index("trade_date").sort_index()
    resampled = indexed.resample(frequency).agg(
        open_price=("open_price", "first"),
        high_price=("high_price", "max"),
        low_price=("low_price", "min"),
        close_price=("close_price", "last"),
    )
    return resampled.dropna().reset_index()


def _load_total_market_strategy_comparisons(backtest_dir: Path) -> dict[str, object]:
    lookbacks = [
        ("1m", "Trailing 1 month", "nifty_total_market_top1_to_top10_trailing_1_month_*"),
        ("3m", "Trailing 3 months", "nifty_total_market_top1_to_top10_trailing_3_month_*"),
        ("6m", "Trailing 6 months", "nifty_total_market_top1_to_top10_trailing_6_month_*"),
        ("1y", "Trailing 1 year", "nifty_total_market_top1_to_top10_20260520_234821"),
    ]
    payload = {"lookbacks": [], "series": {}}
    for key, label, pattern in lookbacks:
        folder = _latest_strategy_folder(backtest_dir, pattern)
        if folder is None:
            continue
        summary_path = folder / "comparison" / "nifty_total_market_top1_to_top10_cagr_summary.csv"
        if not summary_path.exists():
            continue
        try:
            summary = pd.read_csv(summary_path)
        except Exception:
            continue
        rows = []
        for row in summary.to_dict(orient="records"):
            rows.append(
                {
                    "top_n": int(row["top_n"]),
                    "final_value": round(float(row["final_value"]), 2),
                    "total_return_pct": round(float(row["total_return_pct"]), 2),
                    "cagr_pct": round(float(row["cagr_pct"]), 2),
                    "trades": int(row["trades"]),
                    "ending_holdings": str(row.get("ending_holdings", "")),
                }
            )
        if rows:
            payload["lookbacks"].append({"key": key, "label": label})
            payload["series"][key] = rows
    return payload


def _load_total_market_strategy_equity_series(backtest_dir: Path) -> dict[str, object]:
    lookbacks = [
        ("1m", "Trailing 1 month", "nifty_total_market_top1_to_top10_trailing_1_month_*"),
        ("3m", "Trailing 3 months", "nifty_total_market_top1_to_top10_trailing_3_month_*"),
        ("6m", "Trailing 6 months", "nifty_total_market_top1_to_top10_trailing_6_month_*"),
        ("1y", "Trailing 1 year", "nifty_total_market_top1_to_top10_20260520_234821"),
    ]
    payload = {"lookbacks": [], "series": {}}
    initial_capital = 100000.0
    for key, label, pattern in lookbacks:
        folder = _latest_strategy_folder(backtest_dir, pattern)
        if folder is None:
            continue
        lookback_series = []
        for top_n in range(1, 11):
            equity_path = folder / f"top_{top_n}" / f"nifty_total_market_top_{top_n}_equity.csv"
            if not equity_path.exists():
                continue
            try:
                equity = pd.read_csv(equity_path)
            except Exception:
                continue
            if equity.empty or "trade_date" not in equity or "portfolio_value" not in equity:
                continue
            equity["trade_date"] = pd.to_datetime(equity["trade_date"], errors="coerce")
            equity["portfolio_value"] = pd.to_numeric(equity["portfolio_value"], errors="coerce")
            equity = equity.dropna(subset=["trade_date", "portfolio_value"]).sort_values("trade_date")
            if equity.empty:
                continue
            sampled = _sample_month_end_equity(equity)
            points = [
                {
                    "date": row.trade_date.date().isoformat(),
                    "portfolio_value": round(float(row.portfolio_value), 2),
                    "return_pct": round(((float(row.portfolio_value) / initial_capital) - 1) * 100, 2),
                }
                for row in sampled.itertuples(index=False)
            ]
            lookback_series.append(
                {
                    "key": f"{key}_top_{top_n}",
                    "lookback_key": key,
                    "lookback_label": label,
                    "top_n": top_n,
                    "label": f"{label} Top {top_n}",
                    "points": points,
                }
            )
        if lookback_series:
            payload["lookbacks"].append({"key": key, "label": label})
            payload["series"][key] = lookback_series
    return payload


def _sample_month_end_equity(equity: pd.DataFrame) -> pd.DataFrame:
    monthly = (
        equity.assign(month=equity["trade_date"].dt.to_period("M"))
        .groupby("month", as_index=False)
        .last()[["trade_date", "portfolio_value"]]
    )
    first = equity[["trade_date", "portfolio_value"]].head(1)
    last = equity[["trade_date", "portfolio_value"]].tail(1)
    return (
        pd.concat([first, monthly, last], ignore_index=True)
        .drop_duplicates(subset=["trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def _latest_strategy_folder(root: Path, pattern: str) -> Path | None:
    matches = [path for path in root.glob(pattern) if path.is_dir()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)
