"""有界、fail-closed 的 Excel 公式反向索引。

本模块只做一件事：从已经取得的公式文本中建立“被引用单元格 -> 公式单元格”
的有限反向索引。它不计算公式，也不尝试复现 Excel 的动态引用语义。

设计约束：

* 只接受 A1 单元格引用和有明确边界的有限区间；``$`` 绝对引用只影响 Excel
  的复制语义，不影响依赖关系，因此按同一个单元格处理。
* 同表、跨表以及带引号/Excel 转义单引号的工作表名都可解析。
* 外部工作簿、定义名、结构化引用、3D 引用、整行/整列、INDIRECT/OFFSET、
  数组/共享公式、解析失败和容量超限均不会静默变成依赖边。对应公式会变成
  ``opaque`` 或 ``incomplete``，图也会保留非 complete 状态。
* 区间只有在展开前通过数量上限检查后才会加入反向索引；不为超限区间猜测
  “可能影响”的单元格。
* 源文件 hash/version 是图的输入快照。没有可比较的当前指纹也不视为已验证，
  需要调用方重新构建图后才能恢复 complete。

不引入新的第三方依赖。``openpyxl`` 是项目已有的 Excel 解析依赖，仅使用其
公式 tokenizer；本模块不修改工作簿、数据库、导出层或公式计算算法。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class FormulaStatus(StrEnum):
    """公式或图的可用状态。

    ``opaque`` 表示公式中发现了明确但本模块不解释的语义；``incomplete`` 表示
    解析失败、输入不完整、指纹无法验证或达到边界。两者都不能当作完整图使用。
    """

    COMPLETE = "complete"
    OPAQUE = "opaque"
    INCOMPLETE = "incomplete"


Status = Literal["complete", "opaque", "incomplete"]


class FormulaGraphLimits:
    """公式图的显式资源边界。

    ``max_nodes``、``max_references`` 和 ``max_cells`` 是常用别名，便于调用方
    直接按“图节点/引用表达式/单公式引用单元格”表达限制。所有边界都必须为
    正整数；不接受 ``None`` 代表无限，避免调用方意外关闭 fail-closed 防线。
    """

    def __init__(
        self,
        *,
        max_formula_length: int = 8_192,
        max_formula_tokens: int = 2_048,
        max_reference_expressions: int = 256,
        max_range_cells: int = 4_096,
        max_total_reference_cells: int = 8_192,
        max_formulas: int = 10_000,
        max_edges: int = 100_000,
        max_impact_nodes: int = 1_000,
        max_impact_depth: int = 32,
        # aliases kept as keyword-only options; a non-None alias overrides the
        # corresponding canonical option so callers cannot silently get a
        # different bound than the one they requested.
        max_nodes: int | None = None,
        max_references: int | None = None,
        max_cells: int | None = None,
    ) -> None:
        if max_nodes is not None:
            max_formulas = max_nodes
        if max_references is not None:
            max_reference_expressions = max_references
        if max_cells is not None:
            max_total_reference_cells = max_cells

        values = {
            "max_formula_length": max_formula_length,
            "max_formula_tokens": max_formula_tokens,
            "max_reference_expressions": max_reference_expressions,
            "max_range_cells": max_range_cells,
            "max_total_reference_cells": max_total_reference_cells,
            "max_formulas": max_formulas,
            "max_edges": max_edges,
            "max_impact_nodes": max_impact_nodes,
            "max_impact_depth": max_impact_depth,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in values.items():
            setattr(self, name, value)

    @property
    def max_nodes(self) -> int:
        return self.max_formulas

    @property
    def max_references(self) -> int:
        return self.max_reference_expressions

    @property
    def max_cells(self) -> int:
        return self.max_total_reference_cells

    def __repr__(self) -> str:
        fields = (
            "max_formula_length",
            "max_formula_tokens",
            "max_reference_expressions",
            "max_range_cells",
            "max_total_reference_cells",
            "max_formulas",
            "max_edges",
            "max_impact_nodes",
            "max_impact_depth",
        )
        body = ", ".join(f"{name}={getattr(self, name)!r}" for name in fields)
        return f"FormulaGraphLimits({body})"


GraphLimits = FormulaGraphLimits


def _normalise_source_file(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _normalise_sheet_name(value: Any) -> str:
    if value is None:
        raise ValueError("sheet name is required")
    text = str(value).strip()
    if not text:
        raise ValueError("sheet name is empty")
    if text.startswith("'") or text.endswith("'"):
        decoded, reason = _decode_sheet_name(text)
        if reason:
            raise ValueError(reason)
        return decoded
    return text


def _decode_sheet_name(value: str) -> tuple[str, str | None]:
    """解码 Excel 的 ``'Sheet''Name'`` 工作表写法。"""
    text = value.strip()
    if not text.startswith("'"):
        return text, None
    if len(text) < 2 or not text.endswith("'"):
        return "", "unterminated_quoted_sheet_name"
    inner = text[1:-1]
    result: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "'":
            if index + 1 < len(inner) and inner[index + 1] == "'":
                result.append("'")
                index += 2
                continue
            return "", "invalid_quoted_sheet_name"
        result.append(char)
        index += 1
    if not result:
        return "", "empty_quoted_sheet_name"
    return "".join(result), None


def _column_name(column: int) -> str:
    result: list[str] = []
    value = column
    while value:
        value, remainder = divmod(value - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


def _source_key(source_file: str | None) -> str:
    return source_file if source_file is not None else "<default>"


@dataclass(frozen=True, slots=True)
class CellReference:
    """一个已经解析并验证过的 A1 单元格地址。"""

    sheet: str
    row: int
    column: int
    source_file: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sheet", _normalise_sheet_name(self.sheet))
        object.__setattr__(self, "source_file", _normalise_source_file(self.source_file))
        if isinstance(self.row, bool) or not isinstance(self.row, int) or not 1 <= self.row <= 1_048_576:
            raise ValueError("row must be in Excel's 1..1048576 range")
        if isinstance(self.column, bool) or not isinstance(self.column, int) or not 1 <= self.column <= 16_384:
            raise ValueError("column must be in Excel's 1..16384 range")

    @property
    def coordinate(self) -> str:
        return f"{_column_name(self.column)}{self.row}"

    @property
    def a1(self) -> str:
        return self.coordinate

    @property
    def cell(self) -> str:
        return self.coordinate

    @property
    def sheet_name(self) -> str:
        return self.sheet

    @property
    def address(self) -> str:
        return f"{self.sheet}!{self.coordinate}"

    @property
    def qualified_address(self) -> str:
        return f"{_quote_sheet_name(self.sheet)}!{self.coordinate}"

    @property
    def key(self) -> str:
        return f"{_source_key(self.source_file)}::{self.sheet.casefold()}!{self.coordinate}"

    def __str__(self) -> str:
        return self.address


CellAddress = CellReference


@dataclass(frozen=True, slots=True)
class CellRange:
    """一个有限矩形区间；端点已经去除绝对引用标记。"""

    sheet: str
    start_row: int
    start_column: int
    end_row: int
    end_column: int
    source_file: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sheet", _normalise_sheet_name(self.sheet))
        object.__setattr__(self, "source_file", _normalise_source_file(self.source_file))
        start_row, end_row = self.start_row, self.end_row
        start_column, end_column = self.start_column, self.end_column
        if start_row > end_row:
            start_row, end_row = end_row, start_row
        if start_column > end_column:
            start_column, end_column = end_column, start_column
        object.__setattr__(self, "start_row", start_row)
        object.__setattr__(self, "end_row", end_row)
        object.__setattr__(self, "start_column", start_column)
        object.__setattr__(self, "end_column", end_column)
        for name in ("start_row", "end_row"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_048_576:
                raise ValueError(f"{name} must be in Excel's 1..1048576 range")
        for name in ("start_column", "end_column"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16_384:
                raise ValueError(f"{name} must be in Excel's 1..16384 range")

    @property
    def start(self) -> CellReference:
        return CellReference(self.sheet, self.start_row, self.start_column, self.source_file)

    @property
    def end(self) -> CellReference:
        return CellReference(self.sheet, self.end_row, self.end_column, self.source_file)

    @property
    def start_cell(self) -> CellReference:
        return self.start

    @property
    def end_cell(self) -> CellReference:
        return self.end

    @property
    def row_count(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def column_count(self) -> int:
        return self.end_column - self.start_column + 1

    @property
    def cell_count(self) -> int:
        return self.row_count * self.column_count

    @property
    def coordinate(self) -> str:
        start = self.start.coordinate
        end = self.end.coordinate
        return start if start == end else f"{start}:{end}"

    @property
    def a1(self) -> str:
        return self.coordinate

    @property
    def address(self) -> str:
        return f"{self.sheet}!{self.coordinate}"

    @property
    def qualified_address(self) -> str:
        return f"{_quote_sheet_name(self.sheet)}!{self.coordinate}"

    def iter_cells(self) -> Iterator[CellReference]:
        for row in range(self.start_row, self.end_row + 1):
            for column in range(self.start_column, self.end_column + 1):
                yield CellReference(self.sheet, row, column, self.source_file)

    @property
    def cells(self) -> tuple[CellReference, ...]:
        return tuple(self.iter_cells())

    def __str__(self) -> str:
        return self.address


FormulaReference = CellRange
Reference = CellRange


@dataclass(frozen=True, slots=True)
class SourceFileFingerprint:
    """图构建时看到的一个源文件指纹。"""

    source_file: str | None = None
    sha256: str | None = None
    version: str | int | None = None

    @property
    def file_hash(self) -> str | None:
        return self.sha256

    @property
    def source_file_hash(self) -> str | None:
        return self.sha256

    @property
    def source_file_version(self) -> str | int | None:
        return self.version


@dataclass(frozen=True, slots=True, init=False)
class FormulaCell:
    """待加入图的一个公式单元格。

    构造函数同时接受 ``coordinate=`` 和常见别名 ``cell=``，也支持以 row/column
    给出目标位置；这样从原始网格记录转换时不必先修改原始数据结构。
    """

    sheet: str
    coordinate: str
    formula: str
    source_file: str | None
    source_file_hash: str | None
    source_file_version: str | int | None
    is_array: bool
    is_shared: bool

    def __init__(
        self,
        sheet: str,
        coordinate: str | None = None,
        formula: str | None = None,
        *,
        cell: str | None = None,
        value: str | None = None,
        row: int | None = None,
        column: int | None = None,
        source_file: str | None = None,
        source_file_hash: str | None = None,
        source_file_version: str | int | None = None,
        source_hash: str | None = None,
        version: str | int | None = None,
        is_array: bool = False,
        array_formula: bool | None = None,
        is_shared: bool = False,
        shared_formula: bool | None = None,
    ) -> None:
        coordinate = coordinate or cell
        if coordinate is None and row is not None and column is not None:
            coordinate = f"{_column_name(column)}{row}"
        if coordinate is None:
            raise ValueError("formula cell coordinate is required")
        if formula is None:
            formula = value
        if formula is None:
            raise ValueError("formula text is required")
        if array_formula is not None:
            is_array = array_formula
        if shared_formula is not None:
            is_shared = shared_formula
        if source_hash is not None:
            source_file_hash = source_hash
        if version is not None:
            source_file_version = version
        object.__setattr__(self, "sheet", _normalise_sheet_name(sheet))
        object.__setattr__(self, "coordinate", str(coordinate))
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "source_file", _normalise_source_file(source_file))
        object.__setattr__(self, "source_file_hash", source_file_hash)
        object.__setattr__(self, "source_file_version", source_file_version)
        object.__setattr__(self, "is_array", bool(is_array))
        object.__setattr__(self, "is_shared", bool(is_shared))

    @property
    def cell(self) -> str:
        return self.coordinate

    @property
    def address(self) -> str:
        return f"{self.sheet}!{self.coordinate}"


FormulaRecord = FormulaCell
FormulaInput = FormulaCell


@dataclass(frozen=True, slots=True)
class FormulaParseResult:
    """一个公式的静态解析结果。

    ``ranges`` 保留原始有限区间语义；``references`` 是通过区间展开得到的
    单元格依赖，供反向索引使用。只要状态不是 complete，两者都为空，确保
    不会把部分解析结果误用成完整依赖集合。
    """

    status: FormulaStatus
    references: tuple[CellReference, ...] = ()
    ranges: tuple[CellRange, ...] = ()
    reasons: tuple[str, ...] = ()
    error: str | None = None

    @property
    def cells(self) -> tuple[CellReference, ...]:
        return self.references

    @property
    def dependencies(self) -> tuple[CellReference, ...]:
        return self.references

    @property
    def range_references(self) -> tuple[CellRange, ...]:
        return self.ranges

    @property
    def complete(self) -> bool:
        return self.status == FormulaStatus.COMPLETE

    @property
    def is_complete(self) -> bool:
        return self.complete

    @property
    def opaque(self) -> bool:
        return self.status == FormulaStatus.OPAQUE

    @property
    def incomplete(self) -> bool:
        return self.status == FormulaStatus.INCOMPLETE

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


@dataclass(frozen=True, slots=True)
class FormulaNode:
    address: CellReference
    formula: str
    analysis: FormulaParseResult
    source_file_hash: str | None = None
    source_file_version: str | int | None = None

    @property
    def cell(self) -> CellReference:
        return self.address

    @property
    def sheet(self) -> str:
        return self.address.sheet

    @property
    def coordinate(self) -> str:
        return self.address.coordinate

    @property
    def references(self) -> tuple[CellReference, ...]:
        return self.analysis.references

    @property
    def dependencies(self) -> tuple[CellReference, ...]:
        return self.analysis.references

    @property
    def ranges(self) -> tuple[CellRange, ...]:
        return self.analysis.ranges

    @property
    def status(self) -> FormulaStatus:
        return self.analysis.status

    @property
    def opaque(self) -> bool:
        return self.analysis.opaque

    @property
    def incomplete(self) -> bool:
        return self.analysis.incomplete


@dataclass(frozen=True, slots=True)
class ImpactResult:
    source: CellReference
    dependents: tuple[CellReference, ...]
    status: FormulaStatus
    reasons: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def nodes(self) -> tuple[CellReference, ...]:
        return self.dependents

    @property
    def complete(self) -> bool:
        return self.status == FormulaStatus.COMPLETE and not self.truncated

    @property
    def is_complete(self) -> bool:
        return self.complete


_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]+)$")
_WHOLE_COLUMN_RE = re.compile(r"^\$?[A-Za-z]{1,3}(?:\s*:\s*\$?[A-Za-z]{1,3})$")
_WHOLE_ROW_RE = re.compile(r"^\$?[0-9]+(?:\s*:\s*\$?[0-9]+)$")


def _quote_sheet_name(sheet: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", sheet):
        return sheet
    return "'" + sheet.replace("'", "''") + "'"


def _split_outside(text: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif char == delimiter and not quoted:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _split_qualified(value: str) -> tuple[str | None, str, str | None]:
    parts = _split_outside(value, "!")
    if len(parts) == 1:
        return None, parts[0], None
    if len(parts) != 2:
        reason = "three_dimensional_reference" if ":" in value else "malformed_sheet_qualifier"
        return None, "", reason
    qualifier = parts[0].strip()
    remainder = parts[1].strip()
    if not qualifier or not remainder:
        return None, "", "malformed_sheet_qualifier"
    if "[" in qualifier or "]" in qualifier or "[" in remainder or "]" in remainder:
        return None, "", "external_link_or_structured_reference"
    qualifier_parts = _split_outside(qualifier, ":")
    if len(qualifier_parts) > 1:
        return None, "", "three_dimensional_reference"
    sheet, reason = _decode_sheet_name(qualifier)
    if reason:
        return None, "", reason
    if not sheet:
        return None, "", "malformed_sheet_qualifier"
    if "'" in qualifier and not qualifier.startswith("'"):
        return None, "", "invalid_quoted_sheet_name"
    # Excel sheet names cannot contain ``:``. When it survives quote
    # decoding it denotes the quoted form of a 3D qualifier such as
    # ``'Jan:Mar'!A1``.
    if ":" in sheet:
        return None, "", "three_dimensional_reference"
    return sheet, remainder, None


def _parse_cell_endpoint(
    value: str,
    *,
    default_sheet: str,
    source_file: str | None,
) -> tuple[CellReference | None, bool, str | None]:
    sheet, endpoint, reason = _split_qualified(value.strip())
    if reason:
        return None, False, reason
    explicit_sheet = sheet is not None
    try:
        sheet_name = _normalise_sheet_name(sheet if sheet is not None else default_sheet)
    except ValueError as exc:
        return None, explicit_sheet, str(exc)
    endpoint = endpoint.strip()
    if "[" in endpoint or "]" in endpoint:
        return None, explicit_sheet, "external_link_or_structured_reference"
    if _WHOLE_COLUMN_RE.fullmatch(endpoint) or _WHOLE_ROW_RE.fullmatch(endpoint):
        return None, explicit_sheet, "whole_column_or_row_reference"
    match = _CELL_RE.fullmatch(endpoint)
    if not match:
        return None, explicit_sheet, "defined_name_or_invalid_reference"
    letters, row_text = match.groups()
    column = 0
    for char in letters.upper():
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(row_text)
    if not 1 <= row <= 1_048_576 or not 1 <= column <= 16_384:
        return None, explicit_sheet, "cell_reference_out_of_bounds"
    return CellReference(sheet_name, row, column, source_file), explicit_sheet, None


def _parse_reference_expression(
    value: str,
    *,
    current_sheet: str,
    source_file: str | None,
) -> tuple[CellRange | None, str | None]:
    text = value.strip()
    if not text:
        return None, "empty_reference"
    if "[" in text or "]" in text:
        # This deliberately covers both [Book.xlsx] links and Table[Column]
        # structured references. Neither has a finite A1 interpretation here.
        return None, "external_link_or_structured_reference"
    if _has_unclosed_single_quote(text):
        return None, "unclosed_quoted_sheet_name"
    colon_parts = _split_outside(text, ":")
    repeated_qualified_range = len(colon_parts) == 2 and all("!" in part for part in colon_parts)
    if not repeated_qualified_range:
        _, qualified_endpoint, qualified_reason = _split_qualified(text)
        if qualified_reason:
            return None, qualified_reason
        if _WHOLE_COLUMN_RE.fullmatch(qualified_endpoint.strip()) or _WHOLE_ROW_RE.fullmatch(
            qualified_endpoint.strip()
        ):
            return None, "whole_column_or_row_reference"
    if len(colon_parts) > 2:
        return None, "three_dimensional_reference"
    if len(colon_parts) == 1:
        endpoint, _, reason = _parse_cell_endpoint(
            colon_parts[0], default_sheet=current_sheet, source_file=source_file
        )
        if reason:
            return None, reason
        assert endpoint is not None
        return CellRange(
            endpoint.sheet,
            endpoint.row,
            endpoint.column,
            endpoint.row,
            endpoint.column,
            source_file,
        ), None

    left, right = colon_parts
    left_endpoint, left_explicit, reason = _parse_cell_endpoint(
        left, default_sheet=current_sheet, source_file=source_file
    )
    if reason:
        # Sheet1:Sheet3!A1 lands here as a 3D reference; expose that specific
        # reason instead of treating the sheet name as an ordinary defined name.
        if "!" in text:
            return None, "three_dimensional_reference"
        return None, reason
    assert left_endpoint is not None
    right_endpoint, right_explicit, reason = _parse_cell_endpoint(
        right, default_sheet=left_endpoint.sheet, source_file=source_file
    )
    if reason:
        return None, reason
    assert right_endpoint is not None
    if (left_explicit or right_explicit) and left_endpoint.sheet.casefold() != right_endpoint.sheet.casefold():
        return None, "three_dimensional_reference"
    return CellRange(
        left_endpoint.sheet,
        left_endpoint.row,
        left_endpoint.column,
        right_endpoint.row,
        right_endpoint.column,
        source_file,
    ), None


def _contains_outside_double_quotes(text: str, character: str) -> bool:
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            quoted = not quoted
        elif char == character and not quoted:
            return True
        index += 1
    return False


def _has_unclosed_double_quote(text: str) -> bool:
    quoted = False
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        if quoted and index + 1 < len(text) and text[index + 1] == '"':
            index += 2
            continue
        quoted = not quoted
        index += 1
    return quoted


def _has_unclosed_single_quote(text: str) -> bool:
    """判断工作表名中的单引号是否成对（``''`` 是 Excel 转义）。"""
    quoted = False
    index = 0
    while index < len(text):
        if text[index] != "'":
            index += 1
            continue
        if quoted and index + 1 < len(text) and text[index + 1] == "'":
            index += 2
            continue
        quoted = not quoted
        index += 1
    return quoted


def _formula_result(
    status: FormulaStatus,
    *,
    reasons: Iterable[str] = (),
    error: str | None = None,
    references: Iterable[CellReference] = (),
    ranges: Iterable[CellRange] = (),
) -> FormulaParseResult:
    # Stable order makes evidence snapshots and tests deterministic.
    stable_reasons = tuple(dict.fromkeys(str(reason) for reason in reasons if reason))
    if status != FormulaStatus.COMPLETE:
        # A non-complete formula is intentionally not partially indexed.
        return FormulaParseResult(status, (), (), stable_reasons, error)
    stable_ranges = tuple(ranges)
    unique_refs: dict[str, CellReference] = {}
    for reference in references:
        unique_refs.setdefault(reference.key, reference)
    return FormulaParseResult(
        status,
        tuple(unique_refs.values()),
        stable_ranges,
        stable_reasons,
        error,
    )


def parse_formula(
    formula: str,
    current_sheet: str = "Sheet1",
    *,
    sheet: str | None = None,
    sheet_name: str | None = None,
    source_file: str | None = None,
    limits: FormulaGraphLimits | None = None,
    is_array: bool = False,
    array_formula: bool | None = None,
    is_shared: bool = False,
    shared_formula: bool | None = None,
) -> FormulaParseResult:
    """静态解析一条公式并返回有限、可追溯的依赖。

    结果为 ``opaque`` 或 ``incomplete`` 时，``references`` 和 ``ranges`` 均为空。
    这是一项刻意的 fail-closed 约束：调用方必须先检查 ``complete``，不能把一条
    只解析出部分 token 的公式当成完整依赖图。
    """
    limits = limits or FormulaGraphLimits()
    if sheet is not None:
        current_sheet = sheet
    if sheet_name is not None:
        current_sheet = sheet_name
    try:
        current_sheet = _normalise_sheet_name(current_sheet)
    except ValueError as exc:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("invalid_sheet_name",), error=str(exc))
    source_file = _normalise_source_file(source_file)
    if array_formula is not None:
        is_array = bool(array_formula)
    if shared_formula is not None:
        is_shared = bool(shared_formula)
    if not isinstance(formula, str):
        return _formula_result(
            FormulaStatus.INCOMPLETE,
            reasons=("parse_failed", "formula_text_not_string"),
            error="formula text must be a string",
        )
    if len(formula) > limits.max_formula_length:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("limit_formula_length",))
    text = formula.strip()
    if not text.startswith("="):
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("invalid_formula", "missing_equals"))
    if _has_unclosed_double_quote(text):
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", "unclosed_text_literal"))
    if is_array or is_shared or "{" in text or "}" in text:
        reason = "shared_formula" if is_shared else "array_formula"
        return _formula_result(FormulaStatus.OPAQUE, reasons=(reason,))
    if "[" in text or "]" in text:
        return _formula_result(FormulaStatus.OPAQUE, reasons=("external_link_or_structured_reference",))
    if _contains_outside_double_quotes(text, "#"):
        # Includes dynamic spill references (A1#) and formula error syntax. We
        # keep the entire formula opaque; no attempt is made to interpret #REF!.
        return _formula_result(FormulaStatus.OPAQUE, reasons=("spill_or_error_reference",))

    try:
        from openpyxl.formula import Tokenizer

        tokens = Tokenizer(text).items
    except Exception as exc:  # tokenizer raises more than one exception type for malformed formulas
        return _formula_result(
            FormulaStatus.INCOMPLETE,
            reasons=("parse_failed",),
            error=f"{type(exc).__name__}: {exc}",
        )
    if len(tokens) > limits.max_formula_tokens:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("limit_formula_tokens",))
    if not tokens:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", "empty_formula"))

    depth = 0
    expressions: list[str] = []
    opaque_reasons: list[str] = []
    significant: list[Any] = []
    for token in tokens:
        token_type = getattr(token, "type", "")
        subtype = getattr(token, "subtype", "")
        value = str(getattr(token, "value", ""))
        if token_type in {"FUNC", "PAREN"}:
            if subtype == "OPEN":
                depth += 1
                if token_type == "FUNC" and value[:-1].strip().upper() in {"INDIRECT", "OFFSET"}:
                    opaque_reasons.append("indirect_or_offset")
            elif subtype == "CLOSE":
                depth -= 1
                if depth < 0:
                    return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", "unbalanced_parentheses"))
            else:
                opaque_reasons.append("unknown_function_token")
            significant.append(token)
            continue
        if token_type == "ARRAY":
            opaque_reasons.append("array_formula")
            significant.append(token)
            continue
        if token_type == "OPERAND":
            if subtype == "RANGE":
                expressions.append(value)
            elif subtype == "ERROR":
                opaque_reasons.append("formula_error")
            elif subtype not in {"NUMBER", "TEXT", "LOGICAL"}:
                opaque_reasons.append("unknown_operand")
            significant.append(token)
            continue
        if token_type == "SEP" and subtype == "ROW":
            opaque_reasons.append("array_row_separator")
        significant.append(token)

    if depth != 0:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", "unbalanced_parentheses"))
    non_whitespace = [
        token for token in significant if getattr(token, "type", "") != "WHITE-SPACE"
    ]
    if non_whitespace and getattr(non_whitespace[-1], "type", "") == "OPERATOR-INFIX":
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", "trailing_operator"))
    for previous, current in zip(non_whitespace, non_whitespace[1:], strict=False):
        if getattr(previous, "type", "") == "OPERAND" and getattr(current, "type", "") == "OPERAND":
            # Excel uses whitespace between operands for intersection. It is
            # not the same dependency semantics as a union, so do not guess.
            opaque_reasons.append("range_intersection_or_implicit_operator")
            break
    if opaque_reasons:
        return _formula_result(FormulaStatus.OPAQUE, reasons=opaque_reasons)
    if len(expressions) > limits.max_reference_expressions:
        return _formula_result(FormulaStatus.INCOMPLETE, reasons=("limit_reference_expressions",))

    ranges: list[CellRange] = []
    references: list[CellReference] = []
    total_cells = 0
    for expression in expressions:
        parsed, reason = _parse_reference_expression(
            expression,
            current_sheet=current_sheet,
            source_file=source_file,
        )
        if reason:
            if reason == "external_link_or_structured_reference":
                return _formula_result(FormulaStatus.OPAQUE, reasons=(reason,))
            if reason == "three_dimensional_reference":
                return _formula_result(FormulaStatus.OPAQUE, reasons=(reason,))
            if reason == "whole_column_or_row_reference":
                return _formula_result(FormulaStatus.OPAQUE, reasons=(reason,))
            if reason == "defined_name_or_invalid_reference":
                return _formula_result(FormulaStatus.OPAQUE, reasons=("defined_name_or_invalid_reference",))
            return _formula_result(FormulaStatus.INCOMPLETE, reasons=("parse_failed", reason))
        assert parsed is not None
        if parsed.cell_count > limits.max_range_cells:
            return _formula_result(FormulaStatus.INCOMPLETE, reasons=("limit_range_cells",))
        total_cells += parsed.cell_count
        if total_cells > limits.max_total_reference_cells:
            return _formula_result(FormulaStatus.INCOMPLETE, reasons=("limit_total_reference_cells",))
        ranges.append(parsed)
        references.extend(parsed.iter_cells())
    return _formula_result(FormulaStatus.COMPLETE, references=references, ranges=ranges)


def parse_formula_references(
    formula: str,
    current_sheet: str = "Sheet1",
    *,
    sheet: str | None = None,
    sheet_name: str | None = None,
    **kwargs: Any,
) -> FormulaParseResult:
    """``parse_formula`` 的语义别名，保留更直观的调用名。"""
    return parse_formula(
        formula,
        current_sheet,
        sheet=sheet,
        sheet_name=sheet_name,
        **kwargs,
    )


def _coerce_fingerprint(
    source_file: str | None,
    value: Any,
) -> SourceFileFingerprint:
    if isinstance(value, SourceFileFingerprint):
        return SourceFileFingerprint(
            source_file if source_file is not None else value.source_file,
            value.sha256,
            value.version,
        )
    if isinstance(value, Mapping):
        source = value.get("source_file", value.get("file", source_file))
        digest = value.get("sha256", value.get("hash", value.get("source_file_hash")))
        version = value.get("version", value.get("source_file_version"))
        return SourceFileFingerprint(_normalise_source_file(source), digest, version)
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            return SourceFileFingerprint(source_file, value[0], value[1])
        if len(value) == 1:
            return SourceFileFingerprint(source_file, value[0], None)
    if value is None:
        return SourceFileFingerprint(source_file, None, None)
    return SourceFileFingerprint(source_file, str(value), None)


def _merge_fingerprint(
    existing: SourceFileFingerprint | None,
    incoming: SourceFileFingerprint,
) -> tuple[SourceFileFingerprint, bool]:
    if existing is None:
        return incoming, False
    if existing.sha256 is not None and incoming.sha256 is not None and existing.sha256 != incoming.sha256:
        return existing, True
    if existing.version is not None and incoming.version is not None and existing.version != incoming.version:
        return existing, True
    return SourceFileFingerprint(
        existing.source_file,
        existing.sha256 if existing.sha256 is not None else incoming.sha256,
        existing.version if existing.version is not None else incoming.version,
    ), False


def _parse_target_address(
    value: Any,
    *,
    default_sheet: str | None,
    default_source_file: str | None,
) -> CellReference:
    if isinstance(value, CellReference):
        return value
    if isinstance(value, FormulaNode):
        return value.address
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            sheet, coordinate = value
            return _parse_target_address(
                f"{sheet}!{coordinate}",
                default_sheet=default_sheet,
                default_source_file=default_source_file,
            )
        if len(value) == 3:
            source, sheet, coordinate = value
            return _parse_target_address(
                f"{sheet}!{coordinate}",
                default_sheet=sheet,
                default_source_file=_normalise_source_file(source),
            )
    if not isinstance(value, str):
        raise ValueError("cell address must be a CellReference or string")
    text = value.strip()
    source_file = default_source_file
    if "::" in text:
        source_text, text = text.split("::", 1)
        source_file = None if source_text == "<default>" else source_text
    sheet, endpoint, reason = _split_qualified(text)
    if reason:
        raise ValueError(reason)
    if sheet is None:
        if default_sheet is None:
            raise ValueError("sheet name is required for an unqualified cell address")
        sheet = default_sheet
    parsed, _, reason = _parse_cell_endpoint(
        f"{sheet}!{endpoint}",
        default_sheet=sheet,
        source_file=source_file,
    )
    if reason or parsed is None:
        raise ValueError(reason or "invalid_cell_address")
    return parsed


def _coerce_formula_cell(
    item: Any,
    *,
    default_source_file: str | None,
) -> FormulaCell:
    if isinstance(item, FormulaCell):
        if item.source_file is None and default_source_file is not None:
            return FormulaCell(
                item.sheet,
                item.coordinate,
                item.formula,
                source_file=default_source_file,
                source_file_hash=item.source_file_hash,
                source_file_version=item.source_file_version,
                is_array=item.is_array,
                is_shared=item.is_shared,
            )
        return item
    if isinstance(item, Mapping):
        formula = item.get("formula", item.get("value"))
        sheet = item.get("sheet", item.get("sheet_name", item.get("worksheet")))
        coordinate = item.get("coordinate", item.get("cell", item.get("address")))
        row = item.get("row")
        column = item.get("column", item.get("col"))
        if (sheet is None or coordinate is None) and isinstance(item.get("address"), str):
            target = _parse_target_address(
                item["address"],
                default_sheet=None,
                default_source_file=default_source_file,
            )
            sheet = target.sheet
            coordinate = target.coordinate
            if default_source_file is None:
                default_source_file = target.source_file
        formula_kind = str(item.get("formula_type", item.get("formula_kind", ""))).strip().lower()
        return FormulaCell(
            sheet,
            coordinate,
            formula,
            row=row,
            column=column,
            source_file=item.get("source_file", default_source_file),
            source_file_hash=item.get("source_file_hash", item.get("source_hash", item.get("sha256"))),
            source_file_version=item.get("source_file_version", item.get("version")),
            is_array=bool(item.get("is_array", item.get("array_formula", False)))
            or formula_kind == "array",
            is_shared=bool(item.get("is_shared", item.get("shared_formula", False)))
            or formula_kind == "shared",
        )
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        if len(item) == 3:
            sheet, coordinate, formula = item
            return FormulaCell(sheet, coordinate, formula, source_file=default_source_file)
        if len(item) == 4:
            source_file, sheet, coordinate, formula = item
            return FormulaCell(sheet, coordinate, formula, source_file=source_file)
    raise ValueError("formula input must provide sheet, coordinate and formula")


def _iter_formula_cells(
    formulas: Any,
    *,
    default_source_file: str | None,
) -> Iterator[FormulaCell]:
    if hasattr(formulas, "worksheets"):
        for worksheet in formulas.worksheets:
            formula_attributes = getattr(worksheet, "formula_attributes", {}) or {}
            for row in worksheet.iter_rows():
                for cell in row:
                    value = cell.value
                    data_type = getattr(cell, "data_type", None)
                    is_formula = data_type == "f" or (isinstance(value, str) and value.startswith("="))
                    if not is_formula:
                        continue
                    attributes = formula_attributes.get(cell.coordinate, {})
                    if not isinstance(attributes, Mapping):
                        attributes = {}
                    formula_type = str(attributes.get("t", "")).strip().lower()
                    is_array = bool(getattr(cell, "is_array", False)) or formula_type == "array"
                    is_shared = bool(getattr(cell, "is_shared", False)) or formula_type == "shared"
                    if not isinstance(value, str):
                        value = getattr(value, "text", value)
                        is_array = True
                    yield FormulaCell(
                        worksheet.title,
                        cell.coordinate,
                        value,
                        source_file=default_source_file,
                        is_array=is_array,
                        is_shared=is_shared,
                    )
        return
    if isinstance(formulas, Mapping):
        # One record mapping is accepted in addition to the convenient
        # {"Sheet!B1": "=A1"} mapping.
        if "formula" in formulas or ("value" in formulas and ("sheet" in formulas or "sheet_name" in formulas)):
            yield _coerce_formula_cell(formulas, default_source_file=default_source_file)
            return
        for target, formula in formulas.items():
            if isinstance(formula, Mapping):
                merged = dict(formula)
                merged.setdefault("address", target)
                yield _coerce_formula_cell(merged, default_source_file=default_source_file)
            else:
                address = _parse_target_address(
                    target,
                    default_sheet=None,
                    default_source_file=default_source_file,
                )
                yield FormulaCell(
                    address.sheet,
                    address.coordinate,
                    formula,
                    source_file=address.source_file,
                )
        return
    if formulas is None:
        return
    for item in formulas:
        yield _coerce_formula_cell(item, default_source_file=default_source_file)


class FormulaGraph:
    """公式节点与单元格反向索引。

    图允许读取已知的安全边，但 ``complete``/``status`` 是使用结果前的强制
    门控信号：图为 opaque/incomplete 或源文件已失效时，调用方不得把已知边当作
    全量影响结论。
    """

    def __init__(
        self,
        formulas: Any = None,
        *,
        limits: FormulaGraphLimits | None = None,
        source_file: str | None = None,
        source_file_hash: str | None = None,
        source_file_version: str | int | None = None,
        source_files: Mapping[Any, Any] | None = None,
        max_nodes: int | None = None,
        max_edges: int | None = None,
        max_range_cells: int | None = None,
        max_formula_length: int | None = None,
        max_formula_tokens: int | None = None,
        max_reference_expressions: int | None = None,
        max_total_reference_cells: int | None = None,
        max_impact_nodes: int | None = None,
        max_impact_depth: int | None = None,
    ) -> None:
        if limits is None:
            direct_limits: dict[str, int] = {}
            for name, value in (
                ("max_nodes", max_nodes),
                ("max_edges", max_edges),
                ("max_range_cells", max_range_cells),
                ("max_formula_length", max_formula_length),
                ("max_formula_tokens", max_formula_tokens),
                ("max_reference_expressions", max_reference_expressions),
                ("max_total_reference_cells", max_total_reference_cells),
                ("max_impact_nodes", max_impact_nodes),
                ("max_impact_depth", max_impact_depth),
            ):
                if value is not None:
                    direct_limits[name] = value
            limits = FormulaGraphLimits(**direct_limits)
        elif any(
            value is not None
            for value in (
                max_nodes,
                max_edges,
                max_range_cells,
                max_formula_length,
                max_formula_tokens,
                max_reference_expressions,
                max_total_reference_cells,
                max_impact_nodes,
                max_impact_depth,
            )
        ):
            raise ValueError("pass either limits or direct limit overrides, not both")
        self.limits = limits
        self._nodes: dict[str, FormulaNode] = {}
        self._reverse: dict[str, set[CellReference]] = {}
        self._reference_objects: dict[str, CellReference] = {}
        self._edge_count = 0
        self._fingerprints: dict[str, SourceFileFingerprint] = {}
        self._status_reasons: list[str] = []
        self._invalidated = False
        self._invalidation_reason: str | None = None
        self._processed_inputs = 0
        self._default_source_file = _normalise_source_file(source_file)
        if source_files:
            for key, value in source_files.items():
                key_text = _normalise_source_file(key)
                self._register_fingerprint(_coerce_fingerprint(key_text, value))
        if source_file_hash is not None or source_file_version is not None:
            self._register_fingerprint(
                SourceFileFingerprint(self._default_source_file, source_file_hash, source_file_version)
            )
        if formulas is not None:
            self._build(formulas)

    @classmethod
    def build(cls, formulas: Any = None, **kwargs: Any) -> FormulaGraph:
        return cls(formulas, **kwargs)

    @classmethod
    def from_formulas(cls, formulas: Any = None, **kwargs: Any) -> FormulaGraph:
        return cls(formulas, **kwargs)

    @classmethod
    def from_workbook(cls, workbook: Any, **kwargs: Any) -> FormulaGraph:
        return cls(workbook, **kwargs)

    def add_formula(
        self,
        sheet_or_record: Any,
        coordinate: str | None = None,
        formula: str | None = None,
        *,
        source_file: str | None = None,
        source_file_hash: str | None = None,
        source_file_version: str | int | None = None,
        is_array: bool = False,
        is_shared: bool = False,
    ) -> FormulaNode | None:
        """向现有图追加一个公式；仍受同一组节点/边上限约束。

        追加接口只作为小批量解析的便利入口，遇到重复地址或超限不会覆盖已有
        节点，而是保留原节点并让图保持非 complete 状态。
        """
        try:
            if coordinate is None and formula is None:
                record = sheet_or_record
            else:
                record = FormulaCell(
                    sheet_or_record,
                    coordinate,
                    formula,
                    source_file=source_file,
                    source_file_hash=source_file_hash,
                    source_file_version=source_file_version,
                    is_array=is_array,
                    is_shared=is_shared,
                )
            self._build([record])
            if isinstance(record, FormulaCell):
                return self.get_node(record.address)
            return None
        except Exception:
            self._add_reason("formula_input_parse_failed")
            return None

    add_formula_cell = add_formula

    def _add_reason(self, reason: str) -> None:
        if reason and reason not in self._status_reasons:
            self._status_reasons.append(reason)

    def _register_fingerprint(self, incoming: SourceFileFingerprint) -> None:
        key = _source_key(incoming.source_file)
        existing, conflict = _merge_fingerprint(self._fingerprints.get(key), incoming)
        self._fingerprints[key] = existing
        if conflict:
            self._add_reason("conflicting_source_file_fingerprint")

    def _build(self, formulas: Any) -> None:
        try:
            iterator = _iter_formula_cells(
                formulas,
                default_source_file=self._default_source_file,
            )
            for item in iterator:
                if self._processed_inputs >= self.limits.max_formulas:
                    self._add_reason("limit_formula_nodes")
                    break
                self._processed_inputs += 1
                try:
                    record = _coerce_formula_cell(
                        item,
                        default_source_file=self._default_source_file,
                    )
                    effective_source = record.source_file or self._default_source_file
                    if record.source_file_hash is not None or record.source_file_version is not None:
                        self._register_fingerprint(
                            SourceFileFingerprint(
                                effective_source,
                                record.source_file_hash,
                                record.source_file_version,
                            )
                        )
                    address = CellReference(
                        record.sheet,
                        *_coordinate_to_row_col(record.coordinate),
                        effective_source,
                    )
                except Exception:
                    self._add_reason("formula_input_parse_failed")
                    continue
                if address.key in self._nodes:
                    self._add_reason("duplicate_formula_cell")
                    continue
                analysis = parse_formula(
                    record.formula,
                    record.sheet,
                    source_file=effective_source,
                    limits=self.limits,
                    is_array=record.is_array,
                    is_shared=record.is_shared,
                )
                fingerprint = self._fingerprints.get(_source_key(effective_source))
                node = FormulaNode(
                    address,
                    record.formula,
                    analysis,
                    record.source_file_hash
                    if record.source_file_hash is not None
                    else (fingerprint.sha256 if fingerprint is not None else None),
                    record.source_file_version
                    if record.source_file_version is not None
                    else (fingerprint.version if fingerprint is not None else None),
                )
                self._nodes[address.key] = node
                if analysis.status == FormulaStatus.INCOMPLETE:
                    self._add_reason("incomplete_formula")
                    self._status_reasons.extend(
                        reason for reason in analysis.reasons if reason not in self._status_reasons
                    )
                elif analysis.status == FormulaStatus.OPAQUE:
                    self._add_reason("opaque_formula")
                    self._status_reasons.extend(
                        reason for reason in analysis.reasons if reason not in self._status_reasons
                    )
                if not analysis.complete:
                    continue
                if self._edge_count + len(analysis.references) > self.limits.max_edges:
                    self._add_reason("limit_graph_edges")
                    continue
                for reference in analysis.references:
                    self._reverse.setdefault(reference.key, set()).add(address)
                    self._reference_objects.setdefault(reference.key, reference)
                    self._edge_count += 1
        except Exception:
            # An arbitrary iterable or workbook adapter must never make the
            # partially built index appear complete. Keep already captured
            # nodes, but close the graph explicitly.
            self._add_reason("formula_input_parse_failed")

    @property
    def nodes(self) -> tuple[FormulaNode, ...]:
        return tuple(sorted(self._nodes.values(), key=lambda node: _cell_sort_key(node.address)))

    @property
    def formula_nodes(self) -> tuple[FormulaNode, ...]:
        return self.nodes

    @property
    def edges(self) -> tuple[tuple[CellReference, CellReference], ...]:
        pairs = [
            (self._reference_objects[reference_key], dependent)
            for reference_key, dependents in self._reverse.items()
            for dependent in dependents
        ]
        return tuple(sorted(pairs, key=lambda pair: (_cell_sort_key(pair[0]), _cell_sort_key(pair[1]))))

    def _reference_from_key(self, key: str) -> tuple[CellReference, ...]:
        reference = self._reference_objects.get(key)
        if reference is not None:
            return (reference,)
        for node in self._nodes.values():
            if node.address.key == key:
                return (node.address,)
        return ()

    @property
    def reverse_index(self) -> dict[CellReference, frozenset[CellReference]]:
        result: dict[CellReference, frozenset[CellReference]] = {}
        for key, dependents in self._reverse.items():
            references = self._reference_from_key(key)
            if not references:
                continue
            result[references[0]] = frozenset(dependents)
        return result

    @property
    def index(self) -> dict[CellReference, frozenset[CellReference]]:
        return self.reverse_index

    @property
    def opaque_nodes(self) -> tuple[FormulaNode, ...]:
        return tuple(node for node in self.nodes if node.opaque)

    @property
    def incomplete_nodes(self) -> tuple[FormulaNode, ...]:
        return tuple(node for node in self.nodes if node.incomplete)

    @property
    def source_files(self) -> dict[str, SourceFileFingerprint]:
        return dict(self._fingerprints)

    @property
    def fingerprints(self) -> dict[str, SourceFileFingerprint]:
        return self.source_files

    @property
    def source_file_hash(self) -> str | None:
        values = {fingerprint.sha256 for fingerprint in self._fingerprints.values()}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def source_file_version(self) -> str | int | None:
        values = {fingerprint.version for fingerprint in self._fingerprints.values()}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def status(self) -> FormulaStatus:
        if self._invalidated:
            return FormulaStatus.INCOMPLETE
        if any(reason in {"formula_input_parse_failed", "limit_formula_nodes", "limit_graph_edges"}
               or reason == "duplicate_formula_cell"
               or reason.startswith("limit_")
               or reason.startswith("incomplete")
               or reason.startswith("conflicting")
               for reason in self._status_reasons):
            return FormulaStatus.INCOMPLETE
        if self._status_reasons:
            return FormulaStatus.OPAQUE
        return FormulaStatus.COMPLETE

    @property
    def complete(self) -> bool:
        return self.status == FormulaStatus.COMPLETE

    @property
    def is_complete(self) -> bool:
        return self.complete

    @property
    def opaque(self) -> bool:
        return self.status == FormulaStatus.OPAQUE

    @property
    def incomplete(self) -> bool:
        return self.status == FormulaStatus.INCOMPLETE

    @property
    def reasons(self) -> tuple[str, ...]:
        if self._invalidated and self._invalidation_reason:
            return tuple(dict.fromkeys((*self._status_reasons, self._invalidation_reason)))
        return tuple(self._status_reasons)

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    @property
    def valid(self) -> bool:
        return not self._invalidated

    @property
    def invalidation_reason(self) -> str | None:
        return self._invalidation_reason

    def invalidate(self, reason: str = "source_file_invalidated") -> None:
        self._invalidated = True
        self._invalidation_reason = reason

    def _supplied_fingerprints(
        self,
        *,
        source_file: str | None,
        source_file_hash: str | None,
        source_file_version: str | int | None,
        source_files: Mapping[Any, Any] | None,
    ) -> dict[str, SourceFileFingerprint]:
        supplied: dict[str, SourceFileFingerprint] = {}
        if source_files is not None:
            for key, value in source_files.items():
                fingerprint = _coerce_fingerprint(_normalise_source_file(key), value)
                supplied[_source_key(fingerprint.source_file)] = fingerprint
        if source_file_hash is not None or source_file_version is not None:
            key = _normalise_source_file(source_file)
            if key is None and len(self._fingerprints) == 1:
                existing = next(iter(self._fingerprints.values()))
                key = existing.source_file
            supplied[_source_key(key)] = SourceFileFingerprint(key, source_file_hash, source_file_version)
        return supplied

    def is_valid_for(
        self,
        source_file_hash: str | None = None,
        source_file_version: str | int | None = None,
        *,
        source_file: str | None = None,
        source_files: Mapping[Any, Any] | None = None,
    ) -> bool:
        """判断图是否仍对应调用方提供的源文件指纹。

        无参数调用只反映显式 ``invalidate`` 状态。提供了当前指纹后，要求图中
        每个构建时指纹都能被一一核对；缺少 hash/version 也返回 False。
        """
        if self._invalidated:
            return False
        supplied = self._supplied_fingerprints(
            source_file=source_file,
            source_file_hash=source_file_hash,
            source_file_version=source_file_version,
            source_files=source_files,
        )
        if not supplied:
            return True
        if not self._fingerprints:
            return False
        if set(supplied) != set(self._fingerprints):
            return False
        for key, expected in self._fingerprints.items():
            actual = supplied[key]
            if expected.sha256 is not None and (
                actual.sha256 is None or expected.sha256 != actual.sha256
            ):
                return False
            if expected.version is not None and actual.version != expected.version:
                return False
            if expected.sha256 is None and expected.version is None:
                return False
        return True

    def invalidate_if_changed(
        self,
        source_file_hash: str | None = None,
        source_file_version: str | int | None = None,
        *,
        source_file: str | None = None,
        source_files: Mapping[Any, Any] | None = None,
    ) -> bool:
        """指纹不一致或不可比较时使图失效；返回本次是否发现失效。"""
        if self.is_valid_for(
            source_file=source_file,
            source_file_hash=source_file_hash,
            source_file_version=source_file_version,
            source_files=source_files,
        ):
            return False
        self.invalidate("source_file_hash_or_version_changed")
        return True

    def invalidate_if_file_changed(
        self,
        path: str | Path,
        *,
        source_file: str | None = None,
        source_file_version: str | int | None = None,
    ) -> bool:
        """读取文件当前 hash 并执行失效检查；文件不可读也 fail-closed。"""
        file_path = Path(path)
        if source_file is not None:
            source_key = source_file
        elif len(self._fingerprints) == 1:
            source_key = next(iter(self._fingerprints.values())).source_file
        else:
            source_key = str(file_path)
        try:
            digest = sha256_file(file_path)
        except OSError:
            self.invalidate("source_file_unavailable")
            return True
        return self.invalidate_if_changed(
            source_file_hash=digest,
            source_file_version=source_file_version,
            source_file=source_key,
        )

    def get_node(self, address: Any) -> FormulaNode | None:
        try:
            target = _parse_target_address(
                address,
                default_sheet=self._single_sheet_name(),
                default_source_file=self._lookup_default_source_file(),
            )
        except (TypeError, ValueError):
            return None
        target = self._resolve_lookup_target(target)
        return self._nodes.get(target.key)

    def _single_sheet_name(self) -> str | None:
        names = {node.address.sheet for node in self._nodes.values()}
        return next(iter(names)) if len(names) == 1 else None

    def _lookup_default_source_file(self) -> str | None:
        if len(self._fingerprints) == 1:
            return next(iter(self._fingerprints.values())).source_file
        sources = {node.address.source_file for node in self._nodes.values()}
        return next(iter(sources)) if len(sources) == 1 else self._default_source_file

    def _resolve_lookup_target(self, target: CellReference) -> CellReference:
        default_source = self._lookup_default_source_file()
        if target.source_file is None and default_source is not None:
            return CellReference(target.sheet, target.row, target.column, default_source)
        return target

    def _lookup_key(self, address: Any) -> tuple[str | None, str | None]:
        try:
            target = _parse_target_address(
                address,
                default_sheet=self._single_sheet_name(),
                default_source_file=self._lookup_default_source_file(),
            )
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        target = self._resolve_lookup_target(target)
        return target.key, None

    def get_dependents(self, address: Any) -> frozenset[CellReference]:
        """返回直接依赖于 ``address`` 的公式节点。

        这是一个安全边集合，不代表全量结论；当图不是 complete 时，调用方必须
        同时检查 ``graph.status`` 或使用 ``impact`` 返回的状态。
        """
        key, _ = self._lookup_key(address)
        if key is None:
            return frozenset()
        return frozenset(self._reverse.get(key, set()))

    def dependents_of(self, address: Any) -> frozenset[CellReference]:
        return self.get_dependents(address)

    def lookup(self, address: Any) -> frozenset[CellReference]:
        return self.get_dependents(address)

    # 让 ``build_reverse_index`` 的返回值也能按只读 mapping 使用，同时保留
    # ``status``/``reasons``/``invalidate_if_changed`` 等图级门控信息。
    def __getitem__(self, address: Any) -> frozenset[CellReference]:
        return self.get_dependents(address)

    def __contains__(self, address: Any) -> bool:
        key, _ = self._lookup_key(address)
        return key is not None and key in self._reverse

    def __len__(self) -> int:
        return len(self._reverse)

    def items(self):
        return self.reverse_index.items()

    def keys(self):
        return self.reverse_index.keys()

    def values(self):
        return self.reverse_index.values()

    def get(self, address: Any, default: Any = None):
        return self.get_dependents(address) if address in self else default

    def __iter__(self):
        return iter(self.reverse_index)

    def impact(
        self,
        address: Any,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> ImpactResult:
        """沿反向边取得有限传递影响，并显式报告截断/不完整状态。"""
        default_sheet = self._single_sheet_name()
        default_source = self._lookup_default_source_file()
        try:
            source = _parse_target_address(
                address,
                default_sheet=default_sheet,
                default_source_file=default_source,
            )
            source = self._resolve_lookup_target(source)
        except (TypeError, ValueError) as exc:
            fallback = CellReference(default_sheet or "<invalid>", 1, 1, default_source)
            return ImpactResult(
                fallback,
                (),
                FormulaStatus.INCOMPLETE,
                ("invalid_lookup_address", str(exc)),
            )
        depth_limit = self.limits.max_impact_depth if max_depth is None else max_depth
        node_limit = self.limits.max_impact_nodes if max_nodes is None else max_nodes
        if isinstance(depth_limit, bool) or not isinstance(depth_limit, int) or depth_limit <= 0:
            return ImpactResult(source, (), FormulaStatus.INCOMPLETE, ("invalid_impact_depth",))
        if isinstance(node_limit, bool) or not isinstance(node_limit, int) or node_limit <= 0:
            return ImpactResult(source, (), FormulaStatus.INCOMPLETE, ("invalid_impact_nodes",))
        queue: list[tuple[str, int]] = [(source.key, 0)]
        seen: set[str] = {source.key}
        result: list[CellReference] = []
        reasons: list[str] = list(self.reasons)
        truncated = False
        while queue:
            current_key, depth = queue.pop(0)
            dependents = sorted(self._reverse.get(current_key, set()), key=_cell_sort_key)
            if depth >= depth_limit and dependents:
                truncated = True
                reasons.append("limit_impact_depth")
                continue
            for dependent in dependents:
                if dependent.key in seen:
                    continue
                if len(result) >= node_limit:
                    truncated = True
                    reasons.append("limit_impact_nodes")
                    break
                seen.add(dependent.key)
                result.append(dependent)
                if dependent.key in self._nodes:
                    queue.append((dependent.key, depth + 1))
            if truncated and len(result) >= node_limit:
                break
        if truncated:
            reasons.append("impact_truncated")
        status = FormulaStatus.COMPLETE
        if self._invalidated or any(
            reason.startswith("limit_")
            or reason.startswith("incomplete")
            or reason.startswith("conflicting")
            or reason in {"formula_input_parse_failed", "duplicate_formula_cell", "impact_truncated"}
            for reason in reasons
        ):
            status = FormulaStatus.INCOMPLETE
        elif reasons:
            status = FormulaStatus.OPAQUE
        return ImpactResult(source, tuple(result), status, tuple(dict.fromkeys(reasons)), truncated)

    def get_impact(self, address: Any, **kwargs: Any) -> ImpactResult:
        return self.impact(address, **kwargs)


def _coordinate_to_row_col(coordinate: Any) -> tuple[int, int]:
    if not isinstance(coordinate, str):
        raise ValueError("formula cell coordinate must be a string")
    match = _CELL_RE.fullmatch(coordinate.strip())
    if not match:
        raise ValueError("formula cell coordinate must be a single A1 cell")
    letters, row_text = match.groups()
    column = 0
    for char in letters.upper():
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(row_text)
    if not 1 <= row <= 1_048_576 or not 1 <= column <= 16_384:
        raise ValueError("formula cell coordinate is out of bounds")
    return row, column


def _cell_sort_key(cell: CellReference) -> tuple[str, str, int, int]:
    return (_source_key(cell.source_file), cell.sheet.casefold(), cell.row, cell.column)


def build_formula_graph(
    formulas: Any = None,
    *,
    formula_cells: Any = None,
    limits: FormulaGraphLimits | None = None,
    source_file: str | None = None,
    source_file_hash: str | None = None,
    source_file_version: str | int | None = None,
    source_files: Mapping[Any, Any] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    max_range_cells: int | None = None,
    max_formula_length: int | None = None,
    max_formula_tokens: int | None = None,
    max_reference_expressions: int | None = None,
    max_total_reference_cells: int | None = None,
    max_impact_nodes: int | None = None,
    max_impact_depth: int | None = None,
) -> FormulaGraph:
    """构建有界公式图。

    ``formulas`` 可为 ``FormulaCell`` 可迭代对象、``{address: formula}`` 映射，
    或 openpyxl workbook。输入格式错误会使图进入 incomplete，而不会返回一个
    看似完整的空图。
    """
    if formulas is None and formula_cells is not None:
        formulas = formula_cells
    return FormulaGraph(
        formulas,
        limits=limits,
        source_file=source_file,
        source_file_hash=source_file_hash,
        source_file_version=source_file_version,
        source_files=source_files,
        max_nodes=max_nodes,
        max_edges=max_edges,
        max_range_cells=max_range_cells,
        max_formula_length=max_formula_length,
        max_formula_tokens=max_formula_tokens,
        max_reference_expressions=max_reference_expressions,
        max_total_reference_cells=max_total_reference_cells,
        max_impact_nodes=max_impact_nodes,
        max_impact_depth=max_impact_depth,
    )


def build_reverse_index(*args: Any, **kwargs: Any) -> FormulaGraph:
    """构建公式图的兼容入口；索引通过返回对象的 ``reverse_index`` 读取。"""
    return build_formula_graph(*args, **kwargs)


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """按块计算源文件 SHA-256，不修改文件。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


source_file_hash = sha256_file
compute_source_file_hash = sha256_file
compute_file_hash = sha256_file
hash_source_file = sha256_file


__all__ = [
    "CellAddress",
    "CellRange",
    "CellReference",
    "FormulaCell",
    "FormulaGraph",
    "FormulaGraphLimits",
    "FormulaInput",
    "FormulaNode",
    "FormulaParseResult",
    "FormulaRecord",
    "FormulaReference",
    "FormulaStatus",
    "GraphLimits",
    "ImpactResult",
    "Reference",
    "SourceFileFingerprint",
    "build_formula_graph",
    "build_reverse_index",
    "compute_file_hash",
    "compute_source_file_hash",
    "hash_source_file",
    "parse_formula",
    "parse_formula_references",
    "sha256_file",
    "source_file_hash",
]
