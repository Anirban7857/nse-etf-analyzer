from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import html


def write_drawdown_workbook(path: Path, result: dict[str, object]) -> None:
    symbol = str(result.get("symbol", "ETF")).upper()
    summary_rows = [
        ["Metric", "Value"],
        ["Mode", "Drawdown switch"],
        ["Symbol", symbol],
        ["Start month", result["start_month"]],
        ["End month", result["end_month"]],
        ["Months", result["months"]],
        ["Starting monthly contribution", result["monthly_amount"]],
        ["Annual step-up %", result["annual_step_up_pct"]],
        ["Ultra-short annual return %", result["debt_annual_return_pct"]],
        ["Total contributed", result["total_contributed"]],
        [f"{symbol} invested", result["total_invested"]],
        ["Ultra-short balance", result["debt_value"]],
        [f"{symbol} value", result["gold_value"]],
        ["Portfolio value", result["final_value"]],
        ["Gain / Loss", result["gain"]],
        ["Absolute return %", result["absolute_return_pct"]],
        ["XIRR %", result["xirr_pct"] if result["xirr_pct"] is not None else "N/A"],
        ["Total units", result["total_units"]],
        ["Ending close", result["final_close"]],
    ]

    monthly_headers = [
        "Month",
        "Trade Date",
        "Days Since Previous Price",
        "Open",
        "High",
        "Low",
        "Close",
        "ATH Start",
        "ATH End",
        "Open Drawdown %",
        "Low Drawdown %",
        "Target Allocation %",
        "Monthly Contribution",
        "Ultra-short Start",
        "Ultra-short Interest",
        f"{symbol} Invested",
        "Ultra-short End",
        "Units End",
        f"{symbol} Value At Close",
        "Portfolio Value At Close",
    ]
    monthly_keys = [
        "month",
        "trade_date",
        "days_since_previous_price",
        "open",
        "high",
        "low",
        "close",
        "ath_start",
        "ath_end",
        "open_drawdown_pct",
        "low_drawdown_pct",
        "target_allocation_pct",
        "monthly_contribution",
        "debt_start",
        "debt_interest",
        "goldbees_invested",
        "debt_end",
        "units_end",
        "gold_value_at_close",
        "portfolio_value_at_close",
    ]
    monthly_rows = [monthly_headers] + [
        [row.get(key, "") for key in monthly_keys]
        for row in result.get("monthly_rows", [])
    ]

    buy_headers = [
        "Trade Date",
        "Month",
        "Buy Trigger",
        "Investment",
        "Cumulative Total Invested",
        f"Cumulative {symbol} Invested",
        "Buy Price",
        "Drawdown %",
        "Units Bought",
        "Cumulative Units",
        "Target Allocation %",
        "Ultra-short Balance",
        f"{symbol} Value",
        "Portfolio Value",
        "XIRR % On Total Value",
    ]
    buy_keys = [
        "trade_date",
        "month",
        "trigger",
        "investment",
        "cumulative_contributed",
        "cumulative_invested",
        "buy_price",
        "drawdown_pct",
        "units_bought",
        "cumulative_units",
        "target_allocation_pct",
        "debt_balance",
        "gold_value",
        "portfolio_value",
        "xirr_pct",
    ]
    buy_rows = [buy_headers] + [
        [row.get(key, "") for key in buy_keys]
        for row in result.get("schedule", [])
    ]

    slab_rows = [["Drawdown Trigger %", "Target Allocation %"]] + [
        [row.get("drawdown_pct", ""), row.get("allocation_pct", "")]
        for row in result.get("slabs", [])
    ]

    sheets = [
        ("Summary", summary_rows),
        ("Slabs", slab_rows),
        ("Monthly Ledger", monthly_rows),
        ("Buy Transactions", buy_rows),
    ]
    _write_xlsx(path, sheets)


def _write_xlsx(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        workbook.writestr("_rels/.rels", _root_rels_xml())
        workbook.writestr("xl/workbook.xml", _workbook_xml([name for name, _ in sheets]))
        workbook.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets)))
        workbook.writestr("xl/styles.xml", _styles_xml())
        for index, (_, rows) in enumerate(sheets, start=1):
            workbook.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))


def _sheet_xml(rows: list[list[object]]) -> str:
    columns_xml = _columns_xml(rows)
    auto_filter_xml = _auto_filter_xml(rows)
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cells.append(_cell_xml(_cell_ref(column_index, row_index), value))
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{columns_xml}"
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        f"{auto_filter_xml}"
        '</worksheet>'
    )


def _columns_xml(rows: list[list[object]]) -> str:
    if not rows:
        return ""

    max_columns = max(len(row) for row in rows)
    columns = []
    for column_index in range(max_columns):
        max_length = 0
        for row in rows:
            if column_index >= len(row):
                continue
            value = row[column_index]
            if value is None:
                continue
            if isinstance(value, (date, datetime)):
                text = value.isoformat()
            else:
                text = str(value)
            max_length = max(max_length, len(text))

        width = min(max(max_length + 2, 10), 36)
        excel_column = column_index + 1
        columns.append(f'<col min="{excel_column}" max="{excel_column}" width="{width}" customWidth="1"/>')

    return f'<cols>{"".join(columns)}</cols>'


def _auto_filter_xml(rows: list[list[object]]) -> str:
    if not rows:
        return ""

    max_columns = max(len(row) for row in rows)
    if max_columns == 0:
        return ""

    last_cell = _cell_ref(max_columns, max(1, len(rows)))
    return f'<autoFilter ref="A1:{last_cell}"/>'


def _cell_xml(ref: str, value: object) -> str:
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    else:
        text = str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t>{html.escape(text)}</t></is></c>'


def _cell_ref(column_index: int, row_index: int) -> str:
    letters = ""
    while column_index:
        column_index, remainder = divmod(column_index - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_index}"


def _content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheet_overrides}"
        '</Types>'
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{html.escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets>"
        '</workbook>'
    )


def _workbook_rels_xml(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    styles_rel_id = sheet_count + 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{styles_rel_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '</Relationships>'
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )
