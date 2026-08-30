"""第二读取器的隔离试验接口（P1，默认关闭）。

主读取器和业务导入路径不依赖本模块。调用方只有在明确打开试验开关并提供
两个独立读取器时才会执行比较；任一差异只形成差异报告，不自动选择有利结果，
也不把试验结果写入正式导入、校核或结论表。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".csv"}
UNSUPPORTED_SUFFIXES = {".xlsb", ".ods"}


@dataclass(frozen=True)
class SheetSnapshot:
    name: str
    visible: bool | None
    row_count: int | None
    date_count: int | None
    null_count: int | None
    merged_ranges: tuple[str, ...] = ()
    control_totals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReaderSnapshot:
    reader_name: str
    source_path: str
    file_type: str
    sheets: tuple[SheetSnapshot, ...]
    limitations: tuple[str, ...] = ()

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)


@dataclass(frozen=True)
class ReaderTrialResult:
    status: str
    enabled: bool
    source_path: str
    primary: ReaderSnapshot | None = None
    secondary: ReaderSnapshot | None = None
    differences: tuple[dict[str, Any], ...] = ()
    limitations: tuple[str, ...] = ()
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.status == "complete"
            and not self.differences
            and not self.limitations
            and not (self.primary and self.primary.limitations)
            and not (self.secondary and self.secondary.limitations)
        )

    def as_dict(self) -> dict[str, Any]:
        def encode(snapshot: ReaderSnapshot | None) -> dict[str, Any] | None:
            if snapshot is None:
                return None
            return {
                "reader_name": snapshot.reader_name,
                "source_path": snapshot.source_path,
                "file_type": snapshot.file_type,
                "sheets": [
                    {
                        "name": sheet.name,
                        "visible": sheet.visible,
                        "row_count": sheet.row_count,
                        "date_count": sheet.date_count,
                        "null_count": sheet.null_count,
                        "merged_ranges": list(sheet.merged_ranges),
                        "control_totals": list(sheet.control_totals),
                    }
                    for sheet in snapshot.sheets
                ],
                "limitations": list(snapshot.limitations),
            }

        return {
            "status": self.status,
            "enabled": self.enabled,
            "source_path": self.source_path,
            "primary": encode(self.primary),
            "secondary": encode(self.secondary),
            "differences": [dict(item) for item in self.differences],
            "limitations": list(self.limitations),
            "error": self.error,
        }


def trial_scope(path: str | Path) -> tuple[bool, str]:
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return True, "supported"
    if suffix in UNSUPPORTED_SUFFIXES:
        return False, f"{suffix} 暂不纳入第二读取器试验"
    return False, f"{suffix or 'unknown'} 格式不在当前试验范围"


def _normalise_snapshot(snapshot: ReaderSnapshot) -> ReaderSnapshot:
    return ReaderSnapshot(
        reader_name=str(snapshot.reader_name),
        source_path=str(snapshot.source_path),
        file_type=str(snapshot.file_type).lower(),
        sheets=tuple(snapshot.sheets),
        limitations=tuple(str(item) for item in snapshot.limitations),
    )


def compare_reader_snapshots(
    primary: ReaderSnapshot,
    secondary: ReaderSnapshot,
) -> tuple[dict[str, Any], ...]:
    """比较两条读取路径的结构摘要；返回差异而不裁决。"""
    left = _normalise_snapshot(primary)
    right = _normalise_snapshot(secondary)
    differences: list[dict[str, Any]] = []

    if left.sheet_count != right.sheet_count:
        differences.append({
            "field": "sheet_count",
            "primary": left.sheet_count,
            "secondary": right.sheet_count,
        })
    max_count = max(left.sheet_count, right.sheet_count)
    for index in range(max_count):
        l_sheet = left.sheets[index] if index < left.sheet_count else None
        r_sheet = right.sheets[index] if index < right.sheet_count else None
        if l_sheet is None or r_sheet is None:
            differences.append({
                "field": f"sheet[{index}]",
                "primary": l_sheet.name if l_sheet else None,
                "secondary": r_sheet.name if r_sheet else None,
            })
            continue
        for field_name in (
            "name", "visible", "row_count", "date_count", "null_count",
            "merged_ranges", "control_totals",
        ):
            left_value = getattr(l_sheet, field_name)
            right_value = getattr(r_sheet, field_name)
            if left_value != right_value:
                differences.append({
                    "field": f"sheet[{index}].{field_name}",
                    "primary": left_value,
                    "secondary": right_value,
                })
    return tuple(differences)


def run_isolated_reader_trial(
    source_path: str | Path,
    primary_reader: Callable[[Path], ReaderSnapshot],
    secondary_reader: Callable[[Path], ReaderSnapshot] | None = None,
    *,
    enabled: bool = False,
) -> ReaderTrialResult:
    """执行一次不写正式库的第二读取器比较；默认返回 ``disabled``。"""
    path = Path(source_path)
    if not enabled:
        return ReaderTrialResult(
            status="disabled",
            enabled=False,
            source_path=str(path),
            limitations=("第二读取器试验默认关闭，未影响主读取路径。",),
        )
    in_scope, scope_note = trial_scope(path)
    if not in_scope:
        return ReaderTrialResult(
            status="incomplete",
            enabled=True,
            source_path=str(path),
            limitations=(scope_note,),
        )
    if secondary_reader is None:
        return ReaderTrialResult(
            status="incomplete",
            enabled=True,
            source_path=str(path),
            limitations=("未提供独立第二读取器，试验保持关闭。",),
        )
    try:
        primary = _normalise_snapshot(primary_reader(path))
        secondary = _normalise_snapshot(secondary_reader(path))
    except Exception as exc:
        return ReaderTrialResult(
            status="failed",
            enabled=True,
            source_path=str(path),
            limitations=("读取器执行失败，未自动选择任何结果。",),
            error=f"{type(exc).__name__}: {exc}",
        )
    differences = compare_reader_snapshots(primary, secondary)
    limitations = tuple(dict.fromkeys((*primary.limitations, *secondary.limitations)))
    return ReaderTrialResult(
        status="complete",
        enabled=True,
        source_path=str(path),
        primary=primary,
        secondary=secondary,
        differences=differences,
        limitations=limitations,
    )


__all__ = [
    "ReaderSnapshot",
    "ReaderTrialResult",
    "SheetSnapshot",
    "SUPPORTED_SUFFIXES",
    "UNSUPPORTED_SUFFIXES",
    "compare_reader_snapshots",
    "run_isolated_reader_trial",
    "trial_scope",
]
