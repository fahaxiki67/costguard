"""异常 → 源表格文件定位跳转（平台分层，Windows COM 优先）。

数据链：anomalies.message 含「Sheet『名』单元格(行,列)」→ raw_sheets →
parse_batches → source_files（original_path 优先，缺失时回退只读副本
stored_path）。打开前校验 SHA-256，内容已变更则只开所在文件夹。

纪律：只读打开（ReadOnly=True, UpdateLinks=0）；先尝试附着用户已开实例，
附着失败才自行启动，且只 Quit 自己启动的实例；打开的工作簿一律
Close(SaveChanges=False)；无 Office/WPS 时退化为 os.startfile 仅开文件并如实提示。
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


_SPREADSHEET_PROC_NAMES = ("excel.exe", "et.exe", "wps.exe", "ket.exe")


def _spreadsheet_process_pids() -> dict[str, set[int]] | None:
    """按进程名枚举表格程序 PID；无法枚举时返回 None（保守判定，绝不 Quit）。"""
    if os.name != "nt":
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
            text=True, check=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None
    pids: dict[str, set[int]] = {n: set() for n in _SPREADSHEET_PROC_NAMES}
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() in pids:
            try:
                pids[parts[0].lower()].add(int(parts[1]))
            except ValueError:
                continue
    return pids


def _decide_started_here(
    pids_before: dict[str, set[int]] | None,
    pids_after: dict[str, set[int]] | None,
    attached_ok: bool,
) -> bool:
    """只 Quit 自己从头启动的实例（fail-safe：证据不足一律不 Quit）。

    WPS(et.exe/wps.exe) 是单进程多会话：Dispatch 可能附着既有服务器，
    而 Quit 会终止整个进程、连带关掉用户会话。因此只有当
    “调用前无任何表格进程，调用后出现新进程”时才认定实例由本进程
    启动并允许 Quit；进程快照不可用时保守返回 False。
    """
    if pids_before is None or pids_after is None:
        return False
    if attached_ok:
        # ROT 附着成功说明实例早已存在（哪怕快照没看到），绝不 Quit
        return False
    if any(pids_before.values()):
        return False
    return any(pids_after.values())


def open_in_spreadsheet(
    target: JumpTarget,
    *,
    progids: tuple[str, ...] | None = None,
    opener=os.startfile if os.name == "nt" else None,
    platform: str | None = None,
    pid_snapshot=None,
) -> str:
    """打开并定位；返回 located/opened_only/hash_mismatch/jump_failed/unsupported_platform。

    opener 可注入（测试用）；progids 可注入（测试用空元组模拟无 COM）；
    pid_snapshot 可注入（测试用），默认按进程名枚举表格程序 PID。
    """
    if opener is None:
        opener = _os_open
    if pid_snapshot is None:
        pid_snapshot = _spreadsheet_process_pids
    if not target.file_path.exists():
        raise FileNotFoundError(str(target.file_path))
    # 哈希校验全平台一致：内容已变更就不做任何自动定位
    if target.sha256 and _file_sha256(target.file_path) != target.sha256:
        opener(target.file_path.parent)
        return "hash_mismatch"
    if (platform or os.name) != "nt":
        try:
            opener(target.file_path)
        except Exception:  # noqa: BLE001
            return "unsupported_platform"
        return "opened_only"
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        opener(target.file_path)
        return "opened_only"
    pids_before = pid_snapshot()
    app = None
    attached_ok = False
    for progid in (
        progids if progids is not None else _DEFAULT_PROGIDS
    ):
        try:
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        client = win32com.client
        # 先附着已开实例，绝不接管/关闭用户自己的会话
        attach = getattr(client, "GetActiveObject", None)
        if attach is not None:
            try:
                app = attach(progid)
                attached_ok = True
                break
            except Exception:  # noqa: BLE001
                app = None
        try:
            app = client.Dispatch(progid)
            break
        except Exception:  # noqa: BLE001
            app = None
    if app is None:
        opener(target.file_path)
        return "opened_only"
    started_here = _decide_started_here(
        pids_before, pid_snapshot(), attached_ok)
    result = "located"
    workbook = None
    worksheet = None
    try:
        workbook = app.Workbooks.Open(str(target.file_path), 0, True)
        worksheet = workbook.Worksheets(target.sheet_name)
        app.Goto(worksheet.Cells(target.row, target.col), True)
    except Exception:  # noqa: BLE001
        result = "jump_failed"
        try:
            opener(target.file_path)
        except Exception:  # noqa: BLE001
            pass
    finally:
        # 只关自己开的工作簿；只 Quit 自己启动的实例；COM 逐层释放。
        # 注意：绝不在此调用 pythoncom.CoUninitialize()——GUI 线程 STA 上
        # 还有应用/用户会话的其他存活 COM 代理，中途 CoUninitialize 会把
        # 整个公寓拆掉导致它们全部断连（WPS/Excel 实测复现）。保留一个
        # apartment 引用到进程退出是无害的。
        if workbook is not None:
            try:
                workbook.Close(False)
            except Exception:  # noqa: BLE001
                pass
        if started_here:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        worksheet = None
        workbook = None
        app = None
    return result
