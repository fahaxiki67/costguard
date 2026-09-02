"""异常 → 源表格文件定位跳转（平台分层，Windows COM 优先）。

数据链：anomalies.message 含「Sheet『名』单元格(行,列)」→ raw_sheets →
parse_batches → source_files（original_path 优先，缺失时回退只读副本
stored_path）。打开前校验 SHA-256，内容已变更则只开所在文件夹。

纪律：只读打开（ReadOnly=True, UpdateLinks=0）；不 Quit 用户已开的
表格程序；无 Office/WPS 时退化为 os.startfile 仅开文件并如实提示。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_CELL_RE = re.compile(r"单元格\((\d+),(\d+)\)")
_DEFAULT_PROGIDS = ("Excel.Application", "Ket.Application", "ET.Application")


@dataclass(frozen=True)
class JumpTarget:
    file_path: Path
    sheet_name: str
    row: int  # 1-based，已按异常记录的物理行号
    col: int  # 1-based
    sha256: str | None = None


def _col_letter(col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def cell_ref(target: JumpTarget) -> str:
    return f"{target.file_path.name}::{target.sheet_name}!{_col_letter(target.col)}{target.row}"


def jump_target_for_anomaly(conn: sqlite3.Connection, anomaly_id: int) -> JumpTarget | None:
    """由异常解析跳转目标；无 Sheet 单元格信息或链路断裂时返回 None。"""
    row = conn.execute(
        "SELECT subject_type, subject_id, message FROM anomalies WHERE id=?",
        (int(anomaly_id),),
    ).fetchone()
    if row is None or row["subject_type"] != "sheet":
        return None
    match = _CELL_RE.search(row["message"] or "")
    if match is None:
        return None
    sheet = conn.execute(
        "SELECT sheet_name, batch_id FROM raw_sheets WHERE id=?",
        (int(row["subject_id"]),),
    ).fetchone()
    if sheet is None:
        return None
    batch = conn.execute(
        "SELECT file_id FROM parse_batches WHERE id=?", (sheet["batch_id"],)
    ).fetchone()
    if batch is None:
        return None
    sf = conn.execute(
        "SELECT original_path, stored_path, sha256 FROM source_files WHERE id=?",
        (batch["file_id"],),
    ).fetchone()
    if sf is None:
        return None
    file_path = Path(sf["original_path"] or sf["stored_path"])
    if not file_path.exists():
        stored = Path(sf["stored_path"] or "")
        if stored.exists():
            file_path = stored  # 原件被移动时回退项目内只读副本
    return JumpTarget(
        file_path=file_path,
        sheet_name=sheet["sheet_name"],
        row=int(match.group(1)),
        col=int(match.group(2)),
        sha256=sf["sha256"],
    )


def _os_open(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    elif sys_platform_mac():
        import subprocess

        subprocess.run(["open", str(path)], check=False)  # noqa: S603,S607
    else:
        import subprocess

        subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603,S607


def sys_platform_mac() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_in_spreadsheet(
    target: JumpTarget,
    *,
    progids: tuple[str, ...] | None = None,
    opener=os.startfile if os.name == "nt" else None,
) -> str:
    """打开并定位；返回 located/opened_only/hash_mismatch/jump_failed/unsupported_platform。

    opener 可注入（测试用）；progids 可注入（测试用空元组模拟无 COM）。
    """
    if opener is None:
        opener = _os_open
    if os.name != "nt":
        try:
            opener(target.file_path)
        except Exception:  # noqa: BLE001
            return "unsupported_platform"
        return "opened_only"
    if not target.file_path.exists():
        raise FileNotFoundError(str(target.file_path))
    if target.sha256 and _file_sha256(target.file_path) != target.sha256:
        opener(target.file_path.parent)
        return "hash_mismatch"
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        opener(target.file_path)
        return "opened_only"
    app = None
    for progid in (
        progids if progids is not None else _DEFAULT_PROGIDS
    ):
        try:
            pythoncom.CoInitialize()
            app = win32com.client.Dispatch(progid)
            break
        except Exception:  # noqa: BLE001
            app = None
    if app is None:
        opener(target.file_path)
        return "opened_only"
    try:
        workbook = app.Workbooks.Open(str(target.file_path), 0, True)
        worksheet = workbook.Worksheets(target.sheet_name)
        app.Goto(worksheet.Cells(target.row, target.col), True)
        return "located"
    except Exception:  # noqa: BLE001
        try:
            opener(target.file_path)
        except Exception:  # noqa: BLE001
            pass
        return "jump_failed"
