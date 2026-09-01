"""Excel 解析保真层（ADR-008 第一段）。

原则：
- 只"读"，不"解释"：逐格落库 raw_cells，保留公式/缓存值/文本数字/合并拓扑/隐藏行列；
- 永远只读只读副本（originals/），绝不写回；
- .xlsx 用 openpyxl 双读（公式视图 + 缓存值视图）；
- .xls 用 xlrd（无公式缓存与合并单元格信息，作为已知限制记录）；
- .csv 单文件即单表；
- pdf/docx/image 在本 Phase 只登记"解析器未实现"，不假装解析。
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jiadun.core.engine.money import NotANumberError, to_decimal

# 前多少行参与表头识别
HEADER_SCAN_ROWS = 15
SHEET_MAX_EMPTY_TAIL = 200


@dataclass
class CellRecord:
    row: int  # 1-based
    col: int  # 1-based
    raw_value: str | None
    cached_value: str | None
    is_formula: bool
    is_number_stored_as_text: bool
    num_fmt: str
    # 公式缓存是源文件的结构事实，不把缺失/错误缓存伪装成普通文本。
    # ``cached`` 表示文件里存在缓存值，但不代表已经由本程序重算。
    formula_cache_status: str = "not_formula"


@dataclass
class SheetRecord:
    sheet_index: int
    sheet_name: str
    n_rows: int
    n_cols: int
    merged_ranges: list[str] = field(default_factory=list)
    hidden_rows: list[int] = field(default_factory=list)
    hidden_cols: list[int] = field(default_factory=list)
    cells: list[CellRecord] = field(default_factory=list)
    # 筛选可见性由 Office 应用维护，openpyxl 只能读到范围/条件，不能证明
    # 当前文件中哪些行在用户最后一次筛选时可见；因此存在筛选元数据时默认
    # 记为 unknown，由导入/异常层保守门控。
    auto_filter_ref: str | None = None
    filter_conditions: list[dict] = field(default_factory=list)
    table_ranges: list[dict] = field(default_factory=list)
    filter_state: str = "none"
    formula_metadata: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    parser: str
    # ``partial`` 表示已保留可预览的原始网格，但解析器能力不足以证明
    # 结构完整（例如旧式 .xls 无法保留公式/合并拓扑）。调用方必须继续
    # 落库以便人工复核，不能把它当作 ``ok``。
    status: str  # 'ok' | 'partial' | 'unsupported' | 'failed'
    sheets: list[SheetRecord] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str = ""


def _num_stored_as_text(value) -> bool:
    """值是字符串、且看起来是数字（含千分位/¥/括号负数）。"""
    if not isinstance(value, str):
        return False
    try:
        to_decimal(value)
        return True
    except NotANumberError:
        return False


_OPAQUE_FORMULA_MARKERS = (
    "INDIRECT(", "OFFSET(", "[", "]", "TABLE(", "@",
)


def _formula_cache_status(formula: str | None, cached) -> str:
    """返回可供 fail-closed 规则消费的公式缓存状态。

    这里只做保守的结构识别，不尝试在 Python 中重算 Excel 公式。跨工作簿、
    动态引用、结构化引用等公式即使携带缓存值，也不能把该缓存当作本程序
    已验证的金额依据。
    """
    if not formula:
        return "not_formula"
    text = str(formula).strip()
    cached_text = _cached_str(cached)
    if cached_text is None:
        return "missing"
    if "#REF!" in text.upper() or cached_text.lstrip().startswith("#"):
        return "error"
    upper = text.upper()
    if any(marker in upper for marker in _OPAQUE_FORMULA_MARKERS):
        return "opaque"
    return "cached"


def _filter_object_conditions(auto_filter, source: str) -> list[dict]:
    """将 worksheet/table AutoFilter 的范围与条件转为稳定 JSON。

    openpyxl 的 FilterColumn 是 Serialisable 对象，直接 ``__dict__`` 会混入
    不稳定的对象表示；这里仅取可审计的列号、筛选值和条件属性。
    """
    if auto_filter is None:
        return []
    out: list[dict] = []
    ref = getattr(auto_filter, "ref", None)
    filter_columns = getattr(auto_filter, "filterColumn", None) or []
    for column in filter_columns:
        entry: dict[str, object] = {
            "source": source,
            "kind": "filter_column",
            "ref": str(ref) if ref else None,
            # OOXML colId is zero based;保留两个口径，避免后续证据把列号错位。
            "col_id": int(column.colId) if column.colId is not None else None,
            "column": (int(column.colId) + 1) if column.colId is not None else None,
        }
        filters = getattr(column, "filters", None)
        if filters is not None:
            values = getattr(filters, "filter", None) or []
            dates = getattr(filters, "dateGroupItem", None) or []
            entry["values"] = [str(value) for value in values]
            entry["blank"] = bool(getattr(filters, "blank", False))
            if dates:
                entry["date_group_items"] = [
                    {
                        key: getattr(item, key)
                        for key in (
                            "year", "month", "day", "hour", "minute", "second", "dateTimeGrouping"
                        )
                        if getattr(item, key, None) is not None
                    }
                    for item in dates
                ]
        custom = getattr(column, "customFilters", None)
        if custom is not None:
            entry["custom_filters"] = [
                {
                    "operator": getattr(item, "operator", None),
                    "value": getattr(item, "val", None),
                }
                for item in (getattr(custom, "customFilter", None) or [])
            ]
        dynamic = getattr(column, "dynamicFilter", None)
        if dynamic is not None:
            entry["dynamic_filter"] = {
                key: getattr(dynamic, key, None)
                for key in ("type", "val", "maxVal")
                if getattr(dynamic, key, None) is not None
            }
        top10 = getattr(column, "top10", None)
        if top10 is not None:
            entry["top10"] = {
                key: getattr(top10, key, None)
                for key in ("top", "percent", "val")
                if getattr(top10, key, None) is not None
            }
        out.append(entry)
    return out


def _sheet_filter_metadata(ws) -> tuple[str | None, list[dict], list[dict], str]:
    """抽取 worksheet 与表格级筛选的范围、条件及未知可见性状态。"""
    worksheet_filter = getattr(ws, "auto_filter", None)
    worksheet_ref = getattr(worksheet_filter, "ref", None)
    conditions = _filter_object_conditions(worksheet_filter, "worksheet.auto_filter")
    active_filter_condition = any(item.get("kind") == "filter_column" for item in conditions)
    if worksheet_ref and not conditions:
        conditions.append({
            "source": "worksheet.auto_filter",
            "kind": "range_only",
            "ref": str(worksheet_ref),
        })
    tables: list[dict] = []
    table_has_filter = False
    for table_name in getattr(ws, "tables", {}).keys():
        table = ws.tables[table_name]
        table_filter = getattr(table, "autoFilter", None)
        table_ref = getattr(table, "ref", None)
        table_filter_ref = getattr(table_filter, "ref", None) if table_filter is not None else None
        tables.append({
            "name": str(getattr(table, "displayName", None) or table_name),
            "ref": str(table_ref) if table_ref else None,
            "auto_filter_ref": str(table_filter_ref) if table_filter_ref else None,
        })
        if table_filter is not None:
            table_has_filter = True
            table_conditions = _filter_object_conditions(table_filter, "table.auto_filter")
            if table_filter_ref and not table_conditions:
                table_conditions.append({
                    "source": "table.auto_filter",
                    "kind": "range_only",
                    "ref": str(table_filter_ref),
                    "table": str(getattr(table, "displayName", None) or table_name),
                })
            for item in table_conditions:
                item.setdefault("table", str(getattr(table, "displayName", None) or table_name))
            conditions.extend(table_conditions)
            active_filter_condition = active_filter_condition or any(
                item.get("kind") == "filter_column" for item in table_conditions
            )
    if active_filter_condition:
        filter_state = "unknown"
    elif worksheet_ref or table_has_filter:
        # 只有范围/下拉按钮、没有实际条件时，文件内没有可见性变化需要猜测；
        # 范围仍完整保存，供上层报告和复核使用。
        filter_state = "range_only"
    else:
        filter_state = "none"
    return (
        str(worksheet_ref) if worksheet_ref else None,
        conditions,
        tables,
        filter_state,
    )


def parse_xlsx(path: Path) -> ParseResult:
    import openpyxl

    wb_formula = openpyxl.load_workbook(path, data_only=False, read_only=False)
    # 缓存值只用于逐格证据比对，不需要第二份可编辑工作簿。只读缓存必须
    # 与公式视图按行并行迭代；禁止对 ReadOnlyWorksheet 逐格调用 ``cell``，
    # 那会退化为大文件上的 O(n²) 扫描。
    wb_cached = openpyxl.load_workbook(path, data_only=True, read_only=True)
    result = ParseResult(parser="openpyxl", status="ok")
    stats = {
        "n_sheets": 0,
        "n_cells": 0,
        "n_formulas": 0,
        "n_formula_unverified": 0,
        "n_filter_sheets": 0,
        "n_text_numbers": 0,
        "error_cells": [],
    }

    try:
        for idx, ws in enumerate(wb_formula.worksheets):
            ws_c = wb_cached.worksheets[idx]
            auto_filter_ref, filter_conditions, table_ranges, filter_state = (
                _sheet_filter_metadata(ws)
            )
            sheet = SheetRecord(
                sheet_index=idx,
                sheet_name=ws.title,
                n_rows=ws.max_row or 0,
                n_cols=ws.max_column or 0,
                merged_ranges=[str(r) for r in ws.merged_cells.ranges],
                hidden_rows=sorted(r for r, dim in ws.row_dimensions.items() if dim.hidden),
                hidden_cols=sorted(
                    (c.column if isinstance(c, int) else openpyxl.utils.column_index_from_string(c))
                    for c, dim in ws.column_dimensions.items()
                    if dim.hidden
                ),
                auto_filter_ref=auto_filter_ref,
                filter_conditions=filter_conditions,
                table_ranges=table_ranges,
                filter_state=filter_state,
            )
            formula_metadata = {
                "formula_count": 0,
                "cached_count": 0,
                "uncached_count": 0,
                "error_count": 0,
                "opaque_count": 0,
                "unverified_count": 0,
                "unverified_cells": [],
            }
            for row, cached_row in zip(ws.iter_rows(), ws_c.iter_rows(), strict=False):
                for cell in row:
                    v = cell.value
                    if v is None and cell.number_format in ("General", ""):
                        continue
                    cached = (
                        cached_row[cell.column - 1].value
                        if cell.column <= len(cached_row) else None
                    )
                    is_formula = cell.data_type == "f"
                    formula_cache_status = _formula_cache_status(v if is_formula else None, cached)
                    rec = CellRecord(
                        row=cell.row,
                        col=cell.column,
                        raw_value=str(v) if v is not None else None,
                        cached_value=_cached_str(cached),
                        is_formula=is_formula,
                        is_number_stored_as_text=_num_stored_as_text(v),
                        num_fmt=cell.number_format or "",
                        formula_cache_status=formula_cache_status,
                    )
                    sheet.cells.append(rec)
                    if is_formula:
                        stats["n_formulas"] += 1
                        formula_metadata["formula_count"] += 1
                        if formula_cache_status == "cached":
                            formula_metadata["cached_count"] += 1
                        else:
                            formula_metadata["unverified_count"] += 1
                            metadata_key = (
                                "uncached_count" if formula_cache_status == "missing"
                                else f"{formula_cache_status}_count"
                            )
                            formula_metadata[metadata_key] += 1
                            formula_metadata["unverified_cells"].append({
                                "row": cell.row,
                                "col": cell.column,
                                "coordinate": cell.coordinate,
                                "formula": str(v),
                                "cached_value": _cached_str(cached),
                                "cache_status": formula_cache_status,
                            })
                            stats["n_formula_unverified"] += 1
                    if rec.is_number_stored_as_text:
                        stats["n_text_numbers"] += 1
                    if isinstance(v, str) and v.startswith("#"):
                        stats["error_cells"].append(f"{ws.title}!{cell.coordinate}:{v}")
            sheet.formula_metadata = formula_metadata
            if filter_state != "none":
                stats["n_filter_sheets"] += 1
            result.sheets.append(sheet)
            stats["n_sheets"] += 1
            stats["n_cells"] += len(sheet.cells)
    finally:
        wb_formula.close()
        wb_cached.close()
    result.stats = stats
    return result


def _cached_str(v) -> str | None:
    if v is None:
        return None
    return str(v)


def parse_xls(path: Path) -> ParseResult:
    import xlrd

    book = xlrd.open_workbook(str(path))
    # xlrd 2.x 可以读取单元格文本和值，但不会保留 OOXML 那样的公式
    # 缓存、合并单元格拓扑或筛选可见性。即使数值碰巧相等，也不能让
    # 下游把 .xls 当作结构完整的输入；保留网格预览，同时显式降级。
    limitations = [
        "xls: xlrd 不保留公式文本/缓存状态",
        "xls: xlrd 不保留合并单元格拓扑",
        "xls: 筛选后的实际可见行无法由解析器证明",
    ]
    result = ParseResult(parser="xlrd", status="partial")
    stats = {"n_sheets": 0, "n_cells": 0, "n_formulas": 0, "n_text_numbers": 0,
             "limitations": limitations, "capability_limited": True}
    for idx in range(book.nsheets):
        sh = book.sheet_by_index(idx)
        sheet = SheetRecord(
            sheet_index=idx,
            sheet_name=sh.name,
            n_rows=sh.nrows,
            n_cols=sh.ncols,
            hidden_rows=sorted(r for r, info in getattr(sh, "rowinfo_map", {}).items() if info.hidden),
            hidden_cols=sorted(c for c, info in getattr(sh, "colinfo_map", {}).items() if info.hidden),
            formula_metadata={
                "capability_limited": True,
                "limitations": limitations,
                "formula_support": "unavailable",
                "merged_range_support": "unavailable",
                "filter_visibility_support": "unavailable",
            },
        )
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == 0:  # EMPTY
                    continue
                text = _xls_cell_text(cell)
                sheet.cells.append(
                    CellRecord(
                        row=r + 1, col=c + 1,
                        raw_value=text, cached_value=text,
                        is_formula=False,
                        is_number_stored_as_text=_num_stored_as_text(text),
                        num_fmt="",
                    )
                )
        result.sheets.append(sheet)
        stats["n_sheets"] += 1
        stats["n_cells"] += len(sheet.cells)
    book.release_resources()
    result.stats = stats
    return result


def _xls_cell_text(cell) -> str:
    if cell.ctype == 2:  # NUMBER
        if float(cell.value).is_integer():
            return str(int(cell.value))
        return repr(cell.value)
    if cell.ctype == 3:  # DATE
        try:
            import xlrd

            dt = xlrd.xldate_as_datetime(cell.value, 0)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return str(cell.value)
    return str(cell.value)


def parse_csv(path: Path) -> ParseResult:
    result = ParseResult(parser="csv", status="ok")
    stats = {"n_sheets": 1, "n_cells": 0, "n_formulas": 0, "n_text_numbers": 0}
    sheet = SheetRecord(sheet_index=0, sheet_name=path.stem, n_rows=0, n_cols=0)
    n_rows = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            n_rows += 1
            for c, v in enumerate(row, start=1):
                if v == "":
                    continue
                sheet.cells.append(
                    CellRecord(row=n_rows, col=c, raw_value=v, cached_value=v,
                               is_formula=False, is_number_stored_as_text=_num_stored_as_text(v), num_fmt="")
                )
    sheet.n_rows = n_rows
    sheet.n_cols = max((rec.col for rec in sheet.cells), default=0)
    result.sheets = [sheet]
    stats["n_cells"] = len(sheet.cells)
    result.stats = stats
    return result


UNSUPPORTED = {"pdf", "doc", "docx", "image"}


def parse_file(path: Path, file_type: str) -> ParseResult:
    """按文件类型分发。未知类型/未实现类型返回明确状态。"""
    path = Path(path)
    if file_type in UNSUPPORTED:
        return ParseResult(parser="none", status="unsupported",
                           error=f"parser for '{file_type}' not implemented yet (planned Phase 6+)")
    if not path.exists():
        return ParseResult(parser="none", status="failed", error=f"file not found: {path}")
    try:
        if file_type == "xlsx":
            return parse_xlsx(path)
        if file_type == "xls":
            return parse_xls(path)
        if file_type == "csv":
            return parse_csv(path)
        if file_type in UNSUPPORTED:
            return ParseResult(parser="none", status="unsupported",
                               error=f"parser for '{file_type}' not implemented yet (planned Phase 6+)")
        return ParseResult(parser="none", status="failed", error=f"unknown file_type: {file_type}")
    except Exception as exc:  # 解析器级失败必须显式报告，不静默
        return ParseResult(parser="unknown", status="failed", error=f"{type(exc).__name__}: {exc}")


def persist_parse_result(
    conn: sqlite3.Connection,
    file_id: int,
    result: ParseResult,
    *,
    commit: bool = True,
) -> int:
    """把解析结果落库。返回 batch_id。

    ``failed``/``unsupported`` 解析批次也必须落库；调用方可以传
    ``commit=False``，把批次和对应的 Evidence 放在同一个外层事务内。
    """
    now = datetime.now().isoformat(timespec="seconds")
    stats = dict(result.stats)
    if result.error:
        # 解析错误是输入证据的一部分；不要只放在内存 ImportReport 里，
        # 否则项目重新打开后无法解释为何该文件没有 Sheet。
        stats["error"] = result.error

    def _insert() -> int:
        cur = conn.execute(
            "INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json) VALUES (?,?,?,?,?)",
            (file_id, result.parser, now, result.status, _dumps(stats)),
        )
        batch_id = cur.lastrowid
        for sheet in result.sheets:
            cur = conn.execute(
                """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols,
                   merged_ranges_json, hidden_rows_json, hidden_cols_json,
                   auto_filter_ref, filter_conditions_json, table_ranges_json,
                   filter_state, formula_metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, sheet.sheet_index, sheet.sheet_name, sheet.n_rows, sheet.n_cols,
                 _dumps(sheet.merged_ranges), _dumps(sheet.hidden_rows), _dumps(sheet.hidden_cols),
                 sheet.auto_filter_ref, _dumps(sheet.filter_conditions), _dumps(sheet.table_ranges),
                 sheet.filter_state, _dumps(sheet.formula_metadata)),
            )
            sheet_id = cur.lastrowid
            conn.executemany(
                """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value,
                   is_formula, is_number_stored_as_text, num_fmt, formula_cache_status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (sheet_id, c.row, c.col, c.raw_value, c.cached_value,
                     int(c.is_formula), int(c.is_number_stored_as_text), c.num_fmt,
                     c.formula_cache_status)
                    for c in sheet.cells
                ],
            )
        return int(batch_id)

    if commit:
        with conn:
            return _insert()
    return _insert()


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
