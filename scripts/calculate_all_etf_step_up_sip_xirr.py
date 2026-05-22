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

from etf_web.db import create_db_engine
from etf_web.sip_calculator import calculate_xirr


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "etfs"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    engine = create_db_engine()
    etfs = load_etfs(engine, args.provider)
    rows = []
    schedules = []
    for index, etf in enumerate(etfs.itertuples(index=False), start=1):
        prices = load_prices(engine, etf.symbol, args.provider)
        result, schedule = calculate_step_up_sip(
            symbol=etf.symbol,
            name=etf.name,
            category=etf.category,
            listing_date=etf.listing_date,
            prices=prices,
            monthly_amount=args.monthly_amount,
            step_up_pct=args.step_up_pct,
            min_months=args.min_months,
        )
        rows.append(result)
        if not schedule.empty:
            schedules.append(schedule)
        print(f"{index}/{len(etfs)} {etf.symbol}: {result['status']}", flush=True)

    summary = pd.DataFrame(rows)
    summary = summary.sort_values(
        ["xirr_pct", "final_value", "months"],
        ascending=[False, False, False],
        na_position="last",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = output_dir / f"all_listed_etfs_step_up_sip_xirr_{stamp}.csv"
    schedule_path = output_dir / f"all_listed_etfs_step_up_sip_schedule_{stamp}.csv"
    summary.to_csv(summary_path, index=False)
    if schedules:
        pd.concat(schedules, ignore_index=True).to_csv(schedule_path, index=False)
    else:
        pd.DataFrame().to_csv(schedule_path, index=False)

    ok = summary[summary["status"] == "OK"]
    print(f"ETFs evaluated: {len(summary)}")
    print(f"OK: {len(ok)}")
    print(f"Summary CSV: {summary_path}")
    print(f"Schedule CSV: {schedule_path}")
    if not ok.empty:
        print("Top 10 by XIRR:")
        print(
            ok[
                [
                    "symbol",
                    "name",
                    "category",
                    "start_date",
                    "end_date",
                    "months",
                    "total_contributed",
                    "final_value",
                    "xirr_pct",
                ]
            ]
            .head(10)
            .to_string(index=False)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate step-up SIP XIRR for all listed ETFs.")
    parser.add_argument("--monthly-amount", type=float, default=1000.0, help="Starting monthly SIP amount.")
    parser.add_argument("--step-up-pct", type=float, default=10.0, help="Annual SIP step-up percentage.")
    parser.add_argument("--min-months", type=int, default=2, help="Minimum monthly SIP rows required.")
    parser.add_argument("--provider", default="yfinance", help="Adjusted OHLC source provider.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory.")
    parser.add_argument("--database-url", help="ETF PostgreSQL SQLAlchemy URL. Overrides DATABASE_URL.")
    return parser.parse_args()


def load_etfs(engine, provider: str) -> pd.DataFrame:
    statement = text(
        """
        select
            m.symbol,
            m.name,
            m.category,
            m.listing_date,
            count(a.trade_date) as adjusted_rows,
            min(a.trade_date) as first_adjusted_date,
            max(a.trade_date) as last_adjusted_date
        from etf_master m
        left join etf_adjusted_ohlc a
          on a.symbol = m.symbol and a.source_provider = :provider
        where m.is_active = true
        group by m.symbol, m.name, m.category, m.listing_date
        order by m.symbol
        """
    )
    with engine.begin() as connection:
        return pd.read_sql(statement, connection, params={"provider": provider})


def load_prices(engine, symbol: str, provider: str) -> pd.DataFrame:
    statement = text(
        """
        select trade_date, open, close
        from etf_adjusted_ohlc
        where symbol = :symbol
          and source_provider = :provider
          and open is not null
          and close is not null
        order by trade_date
        """
    )
    with engine.begin() as connection:
        prices = pd.read_sql(statement, connection, params={"symbol": symbol, "provider": provider})
    if prices.empty:
        return prices
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices["open"] = pd.to_numeric(prices["open"], errors="coerce")
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    return prices[(prices["open"] > 0) & (prices["close"] > 0)].copy()


def calculate_step_up_sip(
    symbol: str,
    name: str,
    category: str,
    listing_date,
    prices: pd.DataFrame,
    monthly_amount: float,
    step_up_pct: float,
    min_months: int,
) -> tuple[dict[str, object], pd.DataFrame]:
    if prices.empty:
        return base_row(symbol, name, category, listing_date, "No adjusted OHLC rows"), pd.DataFrame()

    prices["month"] = prices["trade_date"].dt.to_period("M")
    first = prices.sort_values("trade_date").groupby("month", as_index=False).first()
    last = (
        prices.sort_values("trade_date")
        .groupby("month", as_index=False)
        .last()[["month", "trade_date", "close"]]
        .rename(columns={"trade_date": "month_close_date", "close": "month_close"})
    )
    monthly = first[["month", "trade_date", "open"]].merge(last, on="month")
    if len(monthly) < min_months:
        row = base_row(symbol, name, category, listing_date, f"Fewer than {min_months} months")
        row.update({"months": len(monthly)})
        return row, pd.DataFrame()

    cash = 0.0
    units = 0
    contribution = monthly_amount
    total_contributed = 0.0
    total_deployed = 0.0
    cash_flows = []
    schedule_rows = []

    for index, row in monthly.iterrows():
        if index > 0 and index % 12 == 0:
            contribution *= 1 + step_up_pct / 100.0
        buy_price = float(row["open"])
        cash += contribution
        total_contributed += contribution
        units_bought = int(cash // buy_price) if buy_price > 0 else 0
        deployed = units_bought * buy_price
        if units_bought:
            units += units_bought
            cash -= deployed
            total_deployed += deployed
        cash_flows.append((row["trade_date"].date(), -contribution))
        portfolio_value = units * float(row["month_close"]) + cash
        schedule_rows.append(
            {
                "symbol": symbol,
                "month": str(row["month"]),
                "sip_date": row["trade_date"].date(),
                "contribution": round(contribution, 2),
                "cumulative_contributed": round(total_contributed, 2),
                "buy_price": round(buy_price, 4),
                "units_bought": units_bought,
                "cumulative_units": units,
                "deployed": round(deployed, 2),
                "cumulative_deployed": round(total_deployed, 2),
                "cash_balance": round(cash, 2),
                "month_close_date": row["month_close_date"].date(),
                "month_close": round(float(row["month_close"]), 4),
                "portfolio_value": round(portfolio_value, 2),
            }
        )

    final_date = prices.iloc[-1]["trade_date"].date()
    final_close = float(prices.iloc[-1]["close"])
    final_value = units * final_close + cash
    cash_flows.append((final_date, final_value))
    xirr = calculate_xirr(cash_flows)
    gain = final_value - total_contributed
    result = base_row(symbol, name, category, listing_date, "OK")
    result.update(
        {
            "start_date": monthly.iloc[0]["trade_date"].date(),
            "end_date": final_date,
            "months": len(monthly),
            "monthly_amount": round(monthly_amount, 2),
            "annual_step_up_pct": round(step_up_pct, 2),
            "total_contributed": round(total_contributed, 2),
            "amount_deployed": round(total_deployed, 2),
            "residual_cash": round(cash, 2),
            "units": units,
            "final_close": round(final_close, 4),
            "final_value": round(final_value, 2),
            "gain": round(gain, 2),
            "absolute_return_pct": round((gain / total_contributed) * 100, 2) if total_contributed else None,
            "xirr_pct": None if xirr is None else round(xirr * 100, 2),
        }
    )
    return result, pd.DataFrame(schedule_rows)


def base_row(symbol: str, name: str, category: str, listing_date, status: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": name,
        "category": category,
        "listing_date": listing_date,
        "status": status,
        "start_date": None,
        "end_date": None,
        "months": 0,
        "monthly_amount": None,
        "annual_step_up_pct": None,
        "total_contributed": None,
        "amount_deployed": None,
        "residual_cash": None,
        "units": None,
        "final_close": None,
        "final_value": None,
        "gain": None,
        "absolute_return_pct": None,
        "xirr_pct": None,
    }


if __name__ == "__main__":
    main()
