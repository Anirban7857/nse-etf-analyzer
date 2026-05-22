from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "generated" / "backtests"


def main() -> None:
    args = parse_args()
    if args.database_url:
        os.environ["MARKET_DATABASE_URL"] = args.database_url

    rows = []
    for top_n in range(args.min_top_n, args.max_top_n + 1):
        print(f"Running top {top_n}...", flush=True)
        output = run_backtest(args, top_n)
        parsed = parse_backtest_output(output)
        equity_path = Path(parsed["Equity CSV"])
        trades_path = Path(parsed["Trades CSV"])
        decisions_path = Path(parsed["Decisions CSV"])
        equity = pd.read_csv(equity_path)
        final = equity.iloc[-1]
        cagr = calculate_cagr(
            initial_capital=args.initial_capital,
            final_value=float(parsed["Final value"]),
            start_date=parse_date(args.first_execution_date),
            end_date=pd.to_datetime(final["trade_date"]).date(),
        )
        rows.append(
            {
                "top_n": top_n,
                "universe_symbols": int(parsed["Universe symbols"]),
                "rank_dates": int(parsed["Rank dates"]),
                "trades": int(parsed["Trades"]),
                "initial_capital": round(args.initial_capital, 2),
                "final_date": final["trade_date"],
                "cash": round(float(final["cash"]), 2),
                "stock_value": round(float(final["stock_value"]), 2),
                "final_value": round(float(parsed["Final value"]), 2),
                "total_return_pct": round(float(parsed["Total return %"]), 2),
                "cagr_pct": round(cagr * 100, 2),
                "ending_holdings": final["holdings"],
                "equity_csv": str(equity_path),
                "trades_csv": str(trades_path),
                "decisions_csv": str(decisions_path),
            }
        )
        if args.organized_output_root:
            copy_strategy_files(
                root=Path(args.organized_output_root),
                top_n=top_n,
                equity_path=equity_path,
                trades_path=trades_path,
                decisions_path=decisions_path,
            )

    comparison = pd.DataFrame(rows).sort_values("top_n")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"total_market_momentum_top1_to_top10_cagr_comparison_{stamp}.csv"
    comparison.to_csv(output_path, index=False)
    if args.organized_output_root:
        comparison_dir = Path(args.organized_output_root) / "comparison"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        comparison.to_csv(comparison_dir / "nifty_total_market_top1_to_top10_cagr_comparison.csv", index=False)
        comparison[
            ["top_n", "final_value", "total_return_pct", "cagr_pct", "trades", "ending_holdings"]
        ].to_csv(comparison_dir / "nifty_total_market_top1_to_top10_cagr_summary.csv", index=False)
    print(f"Comparison CSV: {output_path}")
    print(
        comparison[
            [
                "top_n",
                "final_value",
                "total_return_pct",
                "cagr_pct",
                "trades",
                "ending_holdings",
            ]
        ].to_string(index=False)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare total-market momentum top-N strategies.")
    parser.add_argument("--start", default="2017-01-01", help="Backtest start date.")
    parser.add_argument("--end", default=date.today().isoformat(), help="Backtest end date.")
    parser.add_argument("--first-execution-date", default="2017-01-03", help="Date used for CAGR period.")
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--min-top-n", type=int, default=1)
    parser.add_argument("--max-top-n", type=int, default=10)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--organized-output-root", help="Optional root folder to copy top-N CSV packs into.")
    parser.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL. Overrides MARKET_DATABASE_URL.")
    return parser.parse_args()


def run_backtest(args: argparse.Namespace, top_n: int) -> str:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "backtest_weekly_momentum_top2.py"),
        "--price-source",
        "adjusted",
        "--start",
        args.start,
        "--end",
        args.end,
        "--initial-capital",
        str(args.initial_capital),
        "--lookback-days",
        str(args.lookback_days),
        "--top-n",
        str(top_n),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env=os.environ.copy(),
    )
    print(completed.stdout, flush=True)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, flush=True)
    return completed.stdout


def parse_backtest_output(output: str) -> dict[str, str]:
    parsed = {}
    for line in output.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            parsed[key.strip()] = value.strip()
    required = ["Universe symbols", "Rank dates", "Trades", "Final value", "Total return %", "Equity CSV", "Trades CSV", "Decisions CSV"]
    missing = [key for key in required if key not in parsed]
    if missing:
        raise ValueError(f"Missing expected output keys: {missing}")
    return parsed


def calculate_cagr(initial_capital: float, final_value: float, start_date: date, end_date: date) -> float:
    years = (end_date - start_date).days / 365.25
    return (final_value / initial_capital) ** (1 / years) - 1


def copy_strategy_files(
    root: Path,
    top_n: int,
    equity_path: Path,
    trades_path: Path,
    decisions_path: Path,
) -> None:
    target = root / f"top_{top_n}"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(equity_path, target / f"nifty_total_market_top_{top_n}_equity.csv")
    shutil.copy2(trades_path, target / f"nifty_total_market_top_{top_n}_trades.csv")
    shutil.copy2(decisions_path, target / f"nifty_total_market_top_{top_n}_decisions.csv")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


if __name__ == "__main__":
    main()
