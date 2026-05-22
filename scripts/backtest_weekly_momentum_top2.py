from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import bindparam, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_web.market_data_db import create_market_engine


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "backtests"


@dataclass
class Position:
    symbol: str
    shares: int


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = create_market_engine()
    symbols = load_universe_symbols(engine, args.index_name, args.as_of_date)
    prices = load_prices(
        engine,
        symbols,
        start_date - timedelta(days=args.lookback_days + 14),
        end_date,
        args.price_source,
        args.adjusted_provider,
    )
    if prices.empty:
        raise ValueError("No price data found for selected universe and date range.")

    first_trade_date = first_trading_day_on_or_after(prices, start_date)
    signal_start_date = previous_trading_day_before(prices, first_trade_date) if first_trade_date else start_date
    daily_ranks = build_daily_ranks(prices, signal_start_date or start_date, end_date, args.lookback_days, args.top_n)
    equity, trades, decisions = run_strategy(
        prices=prices,
        daily_ranks=daily_ranks,
        initial_capital=args.initial_capital,
        start_date=start_date,
        end_date=end_date,
        top_n=args.top_n,
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.index_name.lower().replace(" ", "_") if args.index_name else "all_index_constituents"
    strategy_name = f"top{args.top_n}"
    equity_path = output_dir / f"{prefix}_daily_momentum_{strategy_name}_equity_{stamp}.csv"
    trades_path = output_dir / f"{prefix}_daily_momentum_{strategy_name}_trades_{stamp}.csv"
    decisions_path = output_dir / f"{prefix}_daily_momentum_{strategy_name}_decisions_{stamp}.csv"

    equity.to_csv(equity_path, index=False)
    pd.DataFrame(trades).to_csv(trades_path, index=False)
    pd.DataFrame(decisions).to_csv(decisions_path, index=False)

    final_value = float(equity.iloc[-1]["portfolio_value"]) if not equity.empty else args.initial_capital
    total_return_pct = ((final_value / args.initial_capital) - 1) * 100
    print(f"Universe symbols: {len(symbols)}")
    print(f"Top N: {args.top_n}")
    print(f"Rank dates: {daily_ranks['rank_date'].nunique()}")
    print(f"Trades: {len(trades)}")
    print(f"Initial capital: {args.initial_capital:.2f}")
    print(f"Final value: {final_value:.2f}")
    print(f"Total return %: {total_return_pct:.2f}")
    print(f"Equity CSV: {equity_path}")
    print(f"Trades CSV: {trades_path}")
    print(f"Decisions CSV: {decisions_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest daily momentum: every trading-day close ranks index constituents by trailing "
            "one-year return, buys top two on the next trading-day open, and rebalances only when "
            "a new stock becomes rank 1."
        )
    )
    parser.add_argument("--start", default="2017-01-01", help="Strategy start date in YYYY-MM-DD format.")
    parser.add_argument("--end", default=date.today().isoformat(), help="Strategy end date in YYYY-MM-DD format.")
    parser.add_argument("--index-name", help="Optional index name. Defaults to all symbols in index_constituents.")
    parser.add_argument("--as-of-date", help="Optional constituent as-of date. Defaults to latest available.")
    parser.add_argument("--lookback-days", type=int, default=365, help="Trailing return lookback in calendar days.")
    parser.add_argument("--initial-capital", type=float, default=100000.0, help="Starting cash.")
    parser.add_argument("--top-n", type=int, default=2, help="Number of top-ranked stocks to hold.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory for CSV files.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    parser.add_argument(
        "--price-source",
        choices=["bhavcopy", "adjusted"],
        default="bhavcopy",
        help="Use raw NSE bhavcopy prices or corporate-action-adjusted prices.",
    )
    parser.add_argument(
        "--adjusted-provider",
        default="yfinance",
        help="Provider name to read from stock_adjusted_prices when --price-source adjusted.",
    )
    return parser.parse_args()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_universe_symbols(engine, index_name: str | None, as_of_date: str | None) -> list[str]:
    conditions = []
    params: dict[str, object] = {}
    if index_name:
        conditions.append("index_name = :index_name")
        params["index_name"] = index_name.upper()
    if as_of_date:
        conditions.append("as_of_date = :as_of_date")
        params["as_of_date"] = parse_date(as_of_date)
    else:
        conditions.append("as_of_date = (select max(as_of_date) from index_constituents)")

    where_clause = " and ".join(conditions)
    statement = text(f"select distinct symbol from index_constituents where {where_clause} order by symbol")
    with engine.begin() as connection:
        symbols = [row[0] for row in connection.execute(statement, params).fetchall()]

    if not symbols:
        raise ValueError("No symbols found in index_constituents for the selected filters.")
    return symbols


def load_prices(
    engine,
    symbols: list[str],
    start_date: date,
    end_date: date,
    price_source: str = "bhavcopy",
    adjusted_provider: str = "yfinance",
) -> pd.DataFrame:
    if price_source == "adjusted":
        return load_adjusted_prices(engine, symbols, start_date, end_date, adjusted_provider)
    return load_bhavcopy_prices(engine, symbols, start_date, end_date)


def load_bhavcopy_prices(engine, symbols: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    statement = (
        text(
            """
            select trade_date, symbol, open_price, close_price
            from bhavcopy_prices
            where series = 'EQ'
              and symbol in :symbols
              and trade_date between :start_date and :end_date
              and open_price is not null
              and close_price is not null
            order by symbol, trade_date
            """
        )
        .bindparams(bindparam("symbols", expanding=True))
    )
    with engine.begin() as connection:
        prices = pd.read_sql(
            statement,
            connection,
            params={"symbols": symbols, "start_date": start_date, "end_date": end_date},
        )
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices["open_price"] = pd.to_numeric(prices["open_price"])
    prices["close_price"] = pd.to_numeric(prices["close_price"])
    return prices.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def load_adjusted_prices(
    engine,
    symbols: list[str],
    start_date: date,
    end_date: date,
    adjusted_provider: str,
) -> pd.DataFrame:
    statement = (
        text(
            """
            select trade_date, symbol, open_price, close_price
            from stock_adjusted_prices
            where source_provider = :source_provider
              and symbol in :symbols
              and trade_date between :start_date and :end_date
              and open_price is not null
              and close_price is not null
            order by symbol, trade_date
            """
        )
        .bindparams(bindparam("symbols", expanding=True))
    )
    with engine.begin() as connection:
        prices = pd.read_sql(
            statement,
            connection,
            params={
                "source_provider": adjusted_provider,
                "symbols": symbols,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices["open_price"] = pd.to_numeric(prices["open_price"])
    prices["close_price"] = pd.to_numeric(prices["close_price"])
    return prices.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def build_daily_ranks(
    prices: pd.DataFrame,
    start_date: date,
    end_date: date,
    lookback_days: int,
    top_n: int,
) -> pd.DataFrame:
    trading_days = sorted(prices["trade_date"].dt.normalize().unique())
    check_dates = [trade_date for trade_date in trading_days if start_date <= trade_date.date() <= end_date]
    schedule = pd.DataFrame({"check_date": check_dates})
    schedule["lookback_cutoff"] = schedule["check_date"] - pd.to_timedelta(lookback_days, unit="D")

    pieces = []
    for symbol, symbol_prices in prices.groupby("symbol", sort=False):
        history = symbol_prices[["trade_date", "close_price"]].sort_values("trade_date")
        current = schedule[["check_date"]].merge(
            history,
            left_on="check_date",
            right_on="trade_date",
            how="left",
        ).rename(columns={"trade_date": "rank_date", "close_price": "rank_close"})
        previous = pd.merge_asof(
            schedule[["check_date", "lookback_cutoff"]].sort_values("lookback_cutoff"),
            history,
            left_on="lookback_cutoff",
            right_on="trade_date",
            direction="backward",
        ).rename(columns={"trade_date": "lookback_date", "close_price": "lookback_close"})
        frame = current[["check_date", "rank_date", "rank_close"]].copy()
        frame["lookback_date"] = previous["lookback_date"]
        frame["lookback_close"] = previous["lookback_close"]
        frame["symbol"] = symbol
        frame = frame.dropna(subset=["rank_date", "lookback_date", "rank_close", "lookback_close"])
        frame = frame[frame["lookback_close"] > 0]
        frame["return_1y_pct"] = ((frame["rank_close"] / frame["lookback_close"]) - 1) * 100
        pieces.append(frame)

    ranks = pd.concat(pieces, ignore_index=True)
    ranks = ranks.sort_values(["check_date", "return_1y_pct", "symbol"], ascending=[True, False, True])
    ranks["rank"] = ranks.groupby("check_date").cumcount() + 1
    return ranks[ranks["rank"] <= max(top_n, 10)].reset_index(drop=True)


def run_strategy(
    prices: pd.DataFrame,
    daily_ranks: pd.DataFrame,
    initial_capital: float,
    start_date: date,
    end_date: date,
    top_n: int,
) -> tuple[pd.DataFrame, list[dict[str, object]], list[dict[str, object]]]:
    all_trading_days = sorted(prices["trade_date"].dt.date.unique())
    trading_days = [trade_date for trade_date in all_trading_days if start_date <= trade_date <= end_date]
    open_lookup = {
        (row.symbol, row.trade_date.date()): float(row.open_price)
        for row in prices.itertuples(index=False)
    }
    close_updates_by_date: dict[date, list[tuple[str, float]]] = {}
    for row in prices.itertuples(index=False):
        close_updates_by_date.setdefault(row.trade_date.date(), []).append((row.symbol, float(row.close_price)))

    top_two_by_check_date = {
        check_date.date(): frame.sort_values("rank").head(top_n).copy()
        for check_date, frame in daily_ranks.groupby("check_date")
    }

    events_by_date: dict[date, list[list[str]]] = {}
    current_rank_one: str | None = None
    decisions: list[dict[str, object]] = []
    for check_date, top_stocks in top_two_by_check_date.items():
        if len(top_stocks) < top_n:
            continue
        rank_one = str(top_stocks.iloc[0]["symbol"])
        targets = [str(row["symbol"]) for _, row in top_stocks.iterrows()]
        execution_date = next_trading_day_after(all_trading_days, check_date)
        if execution_date is None:
            break
        if execution_date < start_date or execution_date > end_date:
            continue

        should_rebalance = current_rank_one is None or rank_one != current_rank_one
        decisions.append(
            {
                "check_date": check_date,
                "rank_date": top_stocks.iloc[0]["rank_date"].date(),
                "execution_date": execution_date,
                "rank_1": rank_one,
                "rank_1_return_1y_pct": round(float(top_stocks.iloc[0]["return_1y_pct"]), 4),
                "targets": ",".join(targets),
                "action": "rebalance" if should_rebalance else "hold",
            }
        )
        if not should_rebalance:
            continue

        events_by_date.setdefault(execution_date, []).append(targets)
        current_rank_one = rank_one

    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[dict[str, object]] = []
    equity_rows = []
    last_close: dict[str, float] = {}
    for trade_date in trading_days:
        for targets in events_by_date.get(trade_date, []):
            cash += sell_positions_not_in_targets(positions, set(targets), open_lookup, trade_date, trades)
            cash = buy_equal_weight_positions(
                cash=cash,
                positions=positions,
                targets=targets,
                open_lookup=open_lookup,
                execution_date=trade_date,
                trades=trades,
            )

        for symbol, close_price in close_updates_by_date.get(trade_date, []):
            last_close[symbol] = close_price
        stock_value = sum(position.shares * last_close.get(symbol, 0.0) for symbol, position in positions.items())
        equity_rows.append(
            {
                "trade_date": trade_date,
                "cash": round(cash, 2),
                "stock_value": round(stock_value, 2),
                "portfolio_value": round(cash + stock_value, 2),
                "holdings": ",".join(sorted(positions)),
            }
        )

    return pd.DataFrame(equity_rows), trades, decisions


def sell_positions_not_in_targets(
    positions: dict[str, Position],
    targets: set[str],
    open_lookup: dict[tuple[str, date], float],
    execution_date: date,
    trades: list[dict[str, object]],
) -> float:
    proceeds = 0.0
    for symbol, position in list(positions.items()):
        if symbol in targets:
            continue
        price = open_lookup.get((symbol, execution_date))
        if price is None:
            continue
        value = position.shares * price
        proceeds += value
        trades.append(
            {
                "trade_date": execution_date,
                "symbol": symbol,
                "side": "SELL",
                "shares": position.shares,
                "price": round(price, 4),
                "value": round(value, 2),
            }
        )
        del positions[symbol]
    return proceeds


def buy_equal_weight_positions(
    cash: float,
    positions: dict[str, Position],
    targets: list[str],
    open_lookup: dict[tuple[str, date], float],
    execution_date: date,
    trades: list[dict[str, object]],
) -> float:
    missing_targets = [symbol for symbol in targets if symbol not in positions]
    available_targets = [symbol for symbol in missing_targets if open_lookup.get((symbol, execution_date), 0) > 0]
    if not available_targets:
        return cash

    allocation = cash / len(available_targets)
    for symbol in available_targets:
        price = open_lookup[(symbol, execution_date)]
        shares = int(allocation // price)
        if shares <= 0:
            continue
        value = shares * price
        cash -= value
        positions[symbol] = Position(symbol=symbol, shares=shares)
        trades.append(
            {
                "trade_date": execution_date,
                "symbol": symbol,
                "side": "BUY",
                "shares": shares,
                "price": round(price, 4),
                "value": round(value, 2),
            }
        )
    return cash


def next_trading_day_after(trading_days: list[date], check_date: date) -> date | None:
    for trading_day in trading_days:
        if trading_day > check_date:
            return trading_day
    return None


def first_trading_day_on_or_after(prices: pd.DataFrame, target_date: date) -> date | None:
    for trade_date in sorted(prices["trade_date"].dt.date.unique()):
        if trade_date >= target_date:
            return trade_date
    return None


def previous_trading_day_before(prices: pd.DataFrame, target_date: date | None) -> date | None:
    if target_date is None:
        return None
    previous_dates = [trade_date for trade_date in sorted(prices["trade_date"].dt.date.unique()) if trade_date < target_date]
    return previous_dates[-1] if previous_dates else None


if __name__ == "__main__":
    main()
