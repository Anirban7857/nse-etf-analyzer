from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class SipInputs:
    mode: str
    monthly_amount: float
    annual_step_up_pct: float
    debt_annual_return_pct: float
    start_month: str
    end_month: str


@dataclass(frozen=True)
class DrawdownSlab:
    drawdown_pct: float
    allocation_pct: float


def load_monthly_data(csv_path: Path) -> pd.DataFrame:
    dataset = pd.read_csv(csv_path)
    dataset = _normalize_monthly_columns(dataset, csv_path)
    dataset["trade_date"] = pd.to_datetime(dataset["trade_date"])
    dataset["month"] = dataset["month"].astype(str)
    if "symbol" not in dataset.columns:
        dataset["symbol"] = csv_path.name.split("_")[0].upper()
    numeric_columns = ["open", "high", "low", "close", "volume", "change_pct"]
    for column in numeric_columns:
        dataset[column] = pd.to_numeric(dataset[column], errors="coerce")
    return dataset.sort_values("trade_date").reset_index(drop=True)


load_goldbees_monthly_data = load_monthly_data


def _normalize_monthly_columns(dataset: pd.DataFrame, csv_path: Path) -> pd.DataFrame:
    if {"trade_date", "month", "open", "high", "low", "close"}.issubset(dataset.columns):
        return dataset.copy()

    columns = {column.lower().strip(): column for column in dataset.columns}
    if not {"date", "price", "open", "high", "low"}.issubset(columns):
        return dataset.copy()

    normalized = pd.DataFrame()
    normalized["trade_date"] = pd.to_datetime(dataset[columns["date"]], dayfirst=True)
    normalized["month"] = normalized["trade_date"].dt.strftime("%Y-%m")
    normalized["symbol"] = _symbol_from_path(csv_path)
    normalized["open"] = _parse_number_series(dataset[columns["open"]])
    normalized["high"] = _parse_number_series(dataset[columns["high"]])
    normalized["low"] = _parse_number_series(dataset[columns["low"]])
    normalized["close"] = _parse_number_series(dataset[columns["price"]])
    _rescale_outlier_price_rows(normalized)
    normalized["volume"] = _parse_volume_series(dataset[columns["vol."]]) if "vol." in columns else 0
    normalized["change_pct"] = (
        _parse_number_series(dataset[columns["change %"]].astype(str).str.replace("%", "", regex=False))
        if "change %" in columns
        else 0
    )
    normalized["source"] = csv_path.name
    return normalized


def _parse_number_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")


def _parse_volume_series(series: pd.Series) -> pd.Series:
    values = series.astype(str).str.replace(",", "", regex=False).str.strip().str.upper()
    multipliers = values.str.extract(r"([KMB])$", expand=False).map({"K": 1_000, "M": 1_000_000, "B": 1_000_000_000})
    numbers = pd.to_numeric(values.str.replace(r"[KMB]$", "", regex=True), errors="coerce")
    return numbers * multipliers.fillna(1)


def _rescale_outlier_price_rows(dataset: pd.DataFrame) -> None:
    price_columns = ["open", "high", "low", "close"]
    prices = dataset[price_columns].apply(pd.to_numeric, errors="coerce")
    median_close = prices["close"].median()
    if pd.isna(median_close) or median_close <= 0:
        return

    outlier_rows = prices[price_columns].gt(median_close * 50).all(axis=1)
    if outlier_rows.any():
        dataset.loc[outlier_rows, price_columns] = prices.loc[outlier_rows, price_columns] / 100.0


def _symbol_from_path(csv_path: Path) -> str:
    name = csv_path.stem.lower()
    if "jbes" in name or "junior" in name:
        return "JUNIORBEES"
    if "gbes" in name or "gold" in name:
        return "GOLDBEES"
    return csv_path.name.split("_")[0].upper()


def available_months(dataset: pd.DataFrame) -> list[str]:
    return dataset["month"].dropna().astype(str).tolist()


def default_inputs(dataset: pd.DataFrame) -> SipInputs:
    months = available_months(dataset)
    if not months:
        raise ValueError("No monthly data available for SIP calculator.")

    start_index = max(0, len(months) - 60)
    return SipInputs(
        mode="sip",
        monthly_amount=5000.0,
        annual_step_up_pct=10.0,
        debt_annual_return_pct=7.0,
        start_month=months[start_index],
        end_month=months[-1],
    )


def parse_inputs(values: dict[str, str], dataset: pd.DataFrame) -> SipInputs:
    defaults = default_inputs(dataset)
    months = set(available_months(dataset))

    mode = values.get("sip_mode", defaults.mode).strip().lower()
    if mode not in {"sip", "step_up_sip"}:
        mode = defaults.mode

    monthly_amount = parse_float(values.get("sip_amount"), defaults.monthly_amount)
    annual_step_up_pct = parse_float(values.get("sip_step_up"), defaults.annual_step_up_pct)
    debt_annual_return_pct = parse_float(values.get("sip_debt_return"), defaults.debt_annual_return_pct)
    start_month = values.get("sip_start_month", defaults.start_month).strip() or defaults.start_month
    end_month = values.get("sip_end_month", defaults.end_month).strip() or defaults.end_month

    if start_month not in months:
        start_month = defaults.start_month
    if end_month not in months:
        end_month = defaults.end_month

    return SipInputs(
        mode=mode,
        monthly_amount=max(monthly_amount, 0.0),
        annual_step_up_pct=max(annual_step_up_pct, 0.0),
        debt_annual_return_pct=max(debt_annual_return_pct, 0.0),
        start_month=start_month,
        end_month=end_month,
    )


def default_drawdown_slabs() -> list[DrawdownSlab]:
    return [DrawdownSlab(float(drawdown), float(min(drawdown + 10, 100))) for drawdown in range(0, 100, 10)]


def parse_drawdown_slabs(values: object) -> list[DrawdownSlab]:
    raw_drawdowns = _get_values(values, "slab_drawdown")
    raw_allocations = _get_values(values, "slab_allocation")
    slabs: dict[float, DrawdownSlab] = {}

    for raw_drawdown, raw_allocation in zip(raw_drawdowns, raw_allocations):
        try:
            drawdown_pct = float(str(raw_drawdown).replace(",", "").strip())
            allocation_pct = float(str(raw_allocation).replace(",", "").strip())
        except ValueError:
            continue
        if drawdown_pct < 0 or allocation_pct < 0:
            continue
        drawdown_pct = min(drawdown_pct, 99.999)
        allocation_pct = min(allocation_pct, 100.0)
        slabs[drawdown_pct] = DrawdownSlab(drawdown_pct, allocation_pct)

    return sorted(slabs.values(), key=lambda slab: slab.drawdown_pct) or default_drawdown_slabs()


def calculate_sip(dataset: pd.DataFrame, inputs: SipInputs) -> dict[str, object]:
    if inputs.monthly_amount <= 0:
        raise ValueError("Investment amount must be greater than zero.")

    subset = dataset[(dataset["month"] >= inputs.start_month) & (dataset["month"] <= inputs.end_month)].copy()
    if subset.empty:
        raise ValueError("No monthly data available for the selected range.")

    subset = subset.sort_values("trade_date").reset_index(drop=True)
    if subset.iloc[0]["month"] != inputs.start_month or subset.iloc[-1]["month"] != inputs.end_month:
        raise ValueError("Selected month range is not fully available in the dataset.")

    schedule: list[dict[str, object]] = []
    cash_flows: list[tuple[date, float]] = []
    total_invested = 0.0
    total_units = 0.0
    current_amount = inputs.monthly_amount

    for index, row in subset.iterrows():
        if inputs.mode == "step_up_sip" and index > 0 and index % 12 == 0:
            current_amount = current_amount * (1 + inputs.annual_step_up_pct / 100.0)

        buy_price = float(row["open"])
        units = 0 if pd.isna(buy_price) or buy_price <= 0 else int(current_amount // buy_price)
        investment = units * buy_price
        total_invested += investment
        total_units += units
        if investment > 0:
            cash_flows.append((_to_date(row["trade_date"]), -investment))

        schedule.append(
            {
                "month": row["month"],
                "investment": round(investment, 2),
                "cumulative_invested": round(total_invested, 2),
                "buy_price": round(buy_price, 2),
                "units_bought": units,
                "cumulative_units": total_units,
                "portfolio_value_at_close": round(total_units * float(row["close"]), 2),
            }
        )

    final_close = float(subset.iloc[-1]["close"])
    final_value = total_units * final_close
    cash_flows.append((_to_date(subset.iloc[-1]["trade_date"]), final_value))
    gain = final_value - total_invested
    absolute_return_pct = 0.0 if total_invested == 0 else (gain / total_invested) * 100
    xirr = calculate_xirr(cash_flows)

    return {
        "symbol": _dataset_symbol(subset),
        "mode": inputs.mode,
        "start_month": inputs.start_month,
        "end_month": inputs.end_month,
        "months": len(schedule),
        "monthly_amount": round(inputs.monthly_amount, 2),
        "annual_step_up_pct": round(inputs.annual_step_up_pct, 2),
        "debt_annual_return_pct": round(inputs.debt_annual_return_pct, 2),
        "total_contributed": round(total_invested, 2),
        "total_invested": round(total_invested, 2),
        "total_units": total_units,
        "final_close": round(final_close, 2),
        "final_value": round(final_value, 2),
        "gain": round(gain, 2),
        "absolute_return_pct": round(absolute_return_pct, 2),
        "xirr_pct": None if xirr is None else round(xirr * 100, 2),
        "schedule": schedule,
    }


def calculate_drawdown_switch(
    dataset: pd.DataFrame,
    inputs: SipInputs,
    prior_history: pd.DataFrame | None = None,
    slabs: list[DrawdownSlab] | None = None,
) -> dict[str, object]:
    slabs = sorted(slabs or default_drawdown_slabs(), key=lambda slab: slab.drawdown_pct)
    debt_balance_ref = [0.0]
    total_units_ref = [0]
    cash_flows: list[tuple[date, float]] = []
    total_contributed = 0.0
    total_invested = 0.0
    current_contribution = inputs.monthly_amount
    schedule: list[dict[str, object]] = []
    monthly_rows: list[dict[str, object]] = []

    first_date = _to_date(dataset.iloc[0]["trade_date"])
    all_time_high = _starting_all_time_high(dataset, prior_history)
    contribution_months = 0
    last_contribution_month = ""
    previous_trade_date: date | None = None

    for index, row in dataset.iterrows():
        trade_date = _to_date(row["trade_date"])
        month = str(row["month"])
        debt_start = debt_balance_ref[0]
        elapsed_days = 0 if previous_trade_date is None else max(0, (trade_date - previous_trade_date).days)
        debt_interest = _accrue_debt_interest(
            debt_balance_ref,
            inputs.debt_annual_return_pct,
            elapsed_days,
        )
        row_contribution = 0.0

        if month != last_contribution_month:
            if contribution_months > 0 and contribution_months % 12 == 0:
                current_contribution *= 1 + inputs.annual_step_up_pct / 100.0
            debt_balance_ref[0] += current_contribution
            row_contribution = current_contribution
            total_contributed += current_contribution
            cash_flows.append((trade_date, -current_contribution))
            contribution_months += 1
            last_contribution_month = month

        open_price = float(row["open"])
        high_price = float(row["high"])
        low_price = float(row["low"])
        close_price = float(row["close"])
        month_invested = 0.0
        all_time_high_start = all_time_high

        if pd.isna(open_price) or open_price <= 0:
            continue

        all_time_high = max(all_time_high, open_price)
        investment = _buy_to_drawdown_target(
            schedule=schedule,
            trade_date=trade_date,
            month=month,
            trigger="open",
            price=open_price,
            drawdown_pct=_drawdown_pct(all_time_high, open_price),
            slabs=slabs,
            debt_balance_ref=debt_balance_ref,
            total_units_ref=total_units_ref,
            cumulative_invested=total_invested,
        )
        total_invested += investment
        month_invested += investment
        if investment > 0:
            _annotate_latest_drawdown_buy(
                schedule=schedule,
                cumulative_contributed=total_contributed,
                cumulative_invested=total_invested,
                cash_flows=cash_flows,
                valuation_date=trade_date,
            )

        if not pd.isna(low_price) and low_price > 0 and low_price < open_price:
            open_drawdown = _drawdown_pct(all_time_high, open_price)
            low_drawdown = _drawdown_pct(all_time_high, low_price)

            for slab in _crossed_slabs(slabs, open_drawdown, low_drawdown):
                threshold_price = all_time_high * (1 - slab.drawdown_pct / 100.0)
                investment = _buy_to_drawdown_target(
                    schedule=schedule,
                    trade_date=trade_date,
                    month=month,
                    trigger=f"{slab.drawdown_pct:g}% fall",
                    price=threshold_price,
                    drawdown_pct=slab.drawdown_pct,
                    slabs=slabs,
                    debt_balance_ref=debt_balance_ref,
                    total_units_ref=total_units_ref,
                    cumulative_invested=total_invested,
                )
                total_invested += investment
                month_invested += investment
                if investment > 0:
                    _annotate_latest_drawdown_buy(
                        schedule=schedule,
                        cumulative_contributed=total_contributed,
                        cumulative_invested=total_invested,
                        cash_flows=cash_flows,
                        valuation_date=trade_date,
                    )

            investment = _buy_to_drawdown_target(
                schedule=schedule,
                trade_date=trade_date,
                month=month,
                trigger="low",
                price=low_price,
                drawdown_pct=low_drawdown,
                slabs=slabs,
                debt_balance_ref=debt_balance_ref,
                total_units_ref=total_units_ref,
                cumulative_invested=total_invested,
            )
            total_invested += investment
            month_invested += investment
            if investment > 0:
                _annotate_latest_drawdown_buy(
                    schedule=schedule,
                    cumulative_contributed=total_contributed,
                    cumulative_invested=total_invested,
                    cash_flows=cash_flows,
                    valuation_date=trade_date,
                )

        if not pd.isna(high_price) and high_price > 0:
            all_time_high = max(all_time_high, high_price)

        monthly_rows.append(
            {
                "month": month,
                "trade_date": trade_date,
                "days_since_previous_price": elapsed_days,
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "ath_start": round(all_time_high_start, 2),
                "ath_end": round(all_time_high, 2),
                "open_drawdown_pct": round(_drawdown_pct(max(all_time_high_start, open_price), open_price), 2),
                "low_drawdown_pct": round(_drawdown_pct(max(all_time_high_start, open_price), low_price), 2),
                "target_allocation_pct": round(
                    _target_allocation_pct(_drawdown_pct(max(all_time_high_start, open_price), low_price), slabs),
                    2,
                ),
                "monthly_contribution": round(row_contribution, 2),
                "debt_start": round(debt_start, 2),
                "debt_interest": round(debt_interest, 2),
                "goldbees_invested": round(month_invested, 2),
                "debt_end": round(debt_balance_ref[0], 2),
                "units_end": total_units_ref[0],
                "gold_value_at_close": round(total_units_ref[0] * close_price, 2),
                "portfolio_value_at_close": round(debt_balance_ref[0] + total_units_ref[0] * close_price, 2),
            }
        )
        previous_trade_date = trade_date

    final_close = float(dataset.iloc[-1]["close"])
    debt_balance = debt_balance_ref[0]
    total_units = total_units_ref[0]
    gold_value = total_units * final_close
    final_value = debt_balance + gold_value
    gain = final_value - total_contributed
    absolute_return_pct = 0.0 if total_contributed == 0 else (gain / total_contributed) * 100
    cash_flows.append((_to_date(dataset.iloc[-1]["trade_date"]), final_value))
    xirr = calculate_xirr(cash_flows)

    return {
        "symbol": _dataset_symbol(dataset),
        "mode": inputs.mode,
        "start_month": inputs.start_month,
        "end_month": inputs.end_month,
        "months": len(dataset),
        "monthly_amount": round(inputs.monthly_amount, 2),
        "annual_step_up_pct": round(inputs.annual_step_up_pct, 2),
        "debt_annual_return_pct": round(inputs.debt_annual_return_pct, 2),
        "total_contributed": round(total_contributed, 2),
        "total_invested": round(total_invested, 2),
        "debt_value": round(debt_balance, 2),
        "gold_value": round(gold_value, 2),
        "total_units": total_units,
        "final_close": round(final_close, 2),
        "final_value": round(final_value, 2),
        "gain": round(gain, 2),
        "absolute_return_pct": round(absolute_return_pct, 2),
        "xirr_pct": None if xirr is None else round(xirr * 100, 2),
        "slabs": [
            {"drawdown_pct": round(slab.drawdown_pct, 2), "allocation_pct": round(slab.allocation_pct, 2)}
            for slab in slabs
        ],
        "schedule": schedule,
        "monthly_rows": monthly_rows,
    }


def _buy_to_drawdown_target(
    schedule: list[dict[str, object]],
    trade_date: date,
    month: str,
    trigger: str,
    price: float,
    drawdown_pct: float,
    slabs: list[DrawdownSlab],
    debt_balance_ref: list[float],
    total_units_ref: list[float],
    cumulative_invested: float,
) -> float:
    debt_balance = debt_balance_ref[0]
    total_units = total_units_ref[0]
    target_allocation_pct = _target_allocation_pct(drawdown_pct, slabs)
    allocation_base = debt_balance + cumulative_invested
    target_gold_value = allocation_base * target_allocation_pct / 100.0
    target_investment = min(debt_balance, max(0.0, target_gold_value - cumulative_invested))

    if target_investment <= 0 or price <= 0:
        return 0.0

    units = int(target_investment // price)
    if units <= 0:
        return 0.0

    investment = units * price
    debt_balance -= investment
    total_units += units
    debt_balance_ref[0] = debt_balance
    total_units_ref[0] = total_units
    schedule.append(
        {
            "trade_date": trade_date,
            "month": month,
            "trigger": trigger,
            "investment": round(investment, 2),
            "buy_price": round(price, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "units_bought": units,
            "cumulative_units": total_units,
            "debt_balance": round(debt_balance, 2),
            "target_allocation_pct": round(target_allocation_pct, 2),
            "gold_value": round(total_units * price, 2),
            "portfolio_value": round(debt_balance + total_units * price, 2),
        }
    )
    return investment


def _annotate_latest_drawdown_buy(
    schedule: list[dict[str, object]],
    cumulative_contributed: float,
    cumulative_invested: float,
    cash_flows: list[tuple[date, float]],
    valuation_date: date,
) -> None:
    if not schedule:
        return

    latest = schedule[-1]
    latest["cumulative_contributed"] = round(cumulative_contributed, 2)
    latest["cumulative_invested"] = round(cumulative_invested, 2)
    portfolio_value = float(latest["portfolio_value"])
    xirr = calculate_xirr([*cash_flows, (valuation_date, portfolio_value)])
    latest["xirr_pct"] = None if xirr is None else round(xirr * 100, 2)


def _accrue_debt_interest(
    debt_balance_ref: list[float],
    annual_return_pct: float,
    elapsed_days: int,
) -> float:
    if elapsed_days <= 0 or debt_balance_ref[0] <= 0 or annual_return_pct <= 0:
        return 0.0

    starting_balance = debt_balance_ref[0]
    debt_balance_ref[0] *= (1 + annual_return_pct / 100.0) ** (elapsed_days / 365.0)
    return debt_balance_ref[0] - starting_balance


def _drawdown_pct(all_time_high: float, price: float) -> float:
    if all_time_high <= 0:
        return 0.0
    return max(0.0, ((all_time_high - price) / all_time_high) * 100.0)


def _target_allocation_pct(drawdown_pct: float, slabs: list[DrawdownSlab]) -> float:
    target_allocation = 0.0
    for slab in slabs:
        if drawdown_pct + 0.000001 >= slab.drawdown_pct:
            target_allocation = slab.allocation_pct
        else:
            break
    return min(100.0, target_allocation)


def _crossed_slabs(
    slabs: list[DrawdownSlab],
    open_drawdown: float,
    low_drawdown: float,
) -> list[DrawdownSlab]:
    return [
        slab
        for slab in slabs
        if slab.drawdown_pct > open_drawdown + 0.000001 and slab.drawdown_pct <= low_drawdown + 0.000001
    ]


def _starting_all_time_high(dataset: pd.DataFrame, prior_history: pd.DataFrame | None) -> float:
    price_columns = ["open", "high", "close"]
    if prior_history is not None and not prior_history.empty:
        prior_prices = prior_history[price_columns].apply(pd.to_numeric, errors="coerce")
        prior_high = prior_prices.max().max()
        if not pd.isna(prior_high) and prior_high > 0:
            return float(prior_high)

    first_open = float(dataset.iloc[0]["open"])
    if pd.isna(first_open) or first_open <= 0:
        first_prices = dataset[price_columns].apply(pd.to_numeric, errors="coerce")
        first_high = first_prices.max().max()
        return 0.0 if pd.isna(first_high) else float(first_high)
    return first_open


def calculate_xirr(cash_flows: list[tuple[date, float]]) -> float | None:
    if len(cash_flows) < 2:
        return None
    if not any(amount < 0 for _, amount in cash_flows) or not any(amount > 0 for _, amount in cash_flows):
        return None

    start_date = min(flow_date for flow_date, _ in cash_flows)

    def net_present_value(rate: float) -> float:
        return sum(
            amount / ((1 + rate) ** ((flow_date - start_date).days / 365.0))
            for flow_date, amount in cash_flows
        )

    lower = -0.999999
    upper = 1.0
    lower_value = net_present_value(lower)
    upper_value = net_present_value(upper)

    while lower_value * upper_value > 0 and upper < 1000:
        upper *= 2
        upper_value = net_present_value(upper)

    if lower_value * upper_value > 0:
        return None

    for _ in range(100):
        midpoint = (lower + upper) / 2
        midpoint_value = net_present_value(midpoint)
        if abs(midpoint_value) < 0.000001:
            return midpoint
        if lower_value * midpoint_value <= 0:
            upper = midpoint
            upper_value = midpoint_value
        else:
            lower = midpoint
            lower_value = midpoint_value

    return (lower + upper) / 2


def parse_float(raw_value: str | None, fallback: float) -> float:
    if raw_value is None:
        return fallback
    try:
        return float(str(raw_value).replace(",", "").strip())
    except ValueError:
        return fallback


def _get_values(values: object, key: str) -> list[str]:
    if hasattr(values, "getlist"):
        return list(values.getlist(key))  # type: ignore[attr-defined]
    if isinstance(values, dict):
        raw_value = values.get(key)
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            return [str(item) for item in raw_value]
        return [str(raw_value)]
    return []


def _to_date(value: object) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.to_datetime(value).date()


def _dataset_symbol(dataset: pd.DataFrame) -> str:
    if "symbol" not in dataset.columns or dataset.empty:
        return "ETF"
    symbols = dataset["symbol"].dropna().astype(str)
    if symbols.empty:
        return "ETF"
    return symbols.iloc[0].upper()
