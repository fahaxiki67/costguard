"""异常 → 源表格文件定位跳转（平台分层，Windows COM 优先）。

数据链：anomalies.message 含「Sheet『名』单元格(行,列)」→ raw_sheets →
parse_batches → source_files（original_path 优先，缺失时回退只读副本
stored_path）。打开前校验 SHA-256，内容已变更则只开所在文件夹。

纪律：只读打开（ReadOnly=True, UpdateLinks=0）；先尝试附着用户已开实例，
附着失败才自行启动，且只 Quit 自己启动的实例；只有本次调用自己打开的
工作簿才允许 Close(SaveChanges=False)，已由用户打开的工作簿绝不关闭；
无法确认所有权时宁可不关闭；无 Office/WPS 时退化为 os.startfile 仅开文件并如实提示。
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


def _normalise_workbook_path(value: object) -> str | None:
    """把 COM 返回的工作簿路径转成仅用于比较的 Windows 路径键。"""
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(raw)))
    except (OSError, ValueError):
        return None


def _com_attribute(value: object, name: str) -> object | None:
    try:
        result = getattr(value, name)
    except Exception:  # noqa: BLE001 - COM 属性可能因保护/断连而失败
        return None
    return result() if callable(result) and name in {"FullName", "Path", "Name"} else result


def _com_object_identity(value: object) -> tuple[str, int] | None:
    """读取 COM 对象的稳定身份；无法读取时返回 None。"""
    # 测试替身可显式提供身份 token；真实 pywin32 对象走 IUnknown。
    try:
        marker = value._jiadun_com_identity
    except Exception:  # noqa: BLE001
        marker = None
    if marker is not None:
        return ("explicit", id(marker))
    try:
        ole_object = value._oleobj_
        get_iunknown = ole_object.GetIUnknown
        identity = int(get_iunknown())
    except Exception:  # noqa: BLE001 - COM 代理可能不暴露底层身份
        return None
    return ("IUnknown", identity) if identity else None


def _same_com_object(left: object, right: object) -> bool | None:
    if left is right:
        return True
    left_id = _com_object_identity(left)
    right_id = _com_object_identity(right)
    if left_id is None or right_id is None:
        return None
    return left_id == right_id


def _safe_process_snapshot(snapshot) -> dict[str, set[int]] | None:
    """进程快照仅作保守的 app 所有权证据；异常/格式不明即视为未知。"""
    try:
        value = snapshot()
    except Exception:  # noqa: BLE001 - tasklist/权限/COM 环境均可能失败
        return None
    if value is None or not isinstance(value, dict):
        return None
    result: dict[str, set[int]] = {}
    try:
        for name, pids in value.items():
            if not isinstance(pids, (set, frozenset, list, tuple)):
                return None
            result[str(name).lower()] = {int(pid) for pid in pids}
    except (TypeError, ValueError):
        return None
    return result


def _workbook_path_values(workbook: object) -> list[str]:
    values: list[str] = []
    full_name = _com_attribute(workbook, "FullName")
    if isinstance(full_name, str) and full_name:
        values.append(full_name)
    book_path = _com_attribute(workbook, "Path")
    book_name = _com_attribute(workbook, "Name")
    if book_path and book_name:
        values.append(os.path.join(str(book_path), str(book_name)))
    return values


def _workbook_match_target(workbook: object, target_path: Path) -> bool | None:
    """匹配 Workbook 与目标文件；无法取得可靠身份时返回 None。"""
    target_key = _normalise_workbook_path(target_path)
    if target_key is None:
        return None
    values = _workbook_path_values(workbook)
    if not values:
        return None
    uncertain = False
    for value in values:
        key = _normalise_workbook_path(value)
        if key == target_key:
            return True
        if key is None:
            uncertain = True
            continue
        # 映射盘/UNC、8.3 短名、junction 和其他 Windows 等价路径不能
        # 只靠字符串判断；samefile 失败时必须按未知处理，不能按“不相同”处理。
        try:
            if os.path.samefile(value, target_path):
                return True
        except (OSError, ValueError):
            uncertain = True
    return None if uncertain else False


def _iter_workbooks(collection: object) -> tuple[list[object], bool]:
    """返回工作簿快照及是否完整可读；不完整时 ownership 必须按未知处理。"""
    try:
        count = int(collection.Count)
    except Exception:  # noqa: BLE001
        try:
            return list(collection), True
        except Exception:  # noqa: BLE001
            return [], False
    if count < 0:
        return [], False
    items: list[object] = []
    for index in range(1, count + 1):
        try:
            item = collection.Item(index)
        except Exception:  # noqa: BLE001
            try:
                item = collection(index)
            except Exception:  # noqa: BLE001
                return [], False
        items.append(item)
    return items, True


def _target_workbook_open_state(app: object, target_path: Path) -> bool | None:
    """判断目标 Workbook 在调用前是否已打开；None 表示证据不足。"""
    workbook, known = _find_target_workbook(app, target_path)
    if workbook is not None:
        return True
    return False if known else None


def _workbook_inventory(app: object) -> tuple[list[object], bool]:
    try:
        collection = app.Workbooks
    except Exception:  # noqa: BLE001
        return [], False
    return _iter_workbooks(collection)


def _find_target_in_snapshot(
    workbooks: list[object], complete: bool, target_path: Path
) -> tuple[object | None, bool]:
    if not complete:
        return None, False
    unknown = False
    for workbook in workbooks:
        match = _workbook_match_target(workbook, target_path)
        if match is True:
            return workbook, True
        if match is None:
            # 集合虽可遍历，但有一个 Workbook 无法识别时，不能排除它就是目标。
            unknown = True
    return None, not unknown


def _find_target_workbook(app: object, target_path: Path) -> tuple[object | None, bool]:
    """返回已打开目标 Workbook；第二项表示集合与每个路径均可确认。"""
    workbooks, complete = _workbook_inventory(app)
    return _find_target_in_snapshot(workbooks, complete, target_path)


def _workbook_opened_by_call(
    workbook: object, previous_workbooks: list[object]
) -> bool | None:
    """仅在 COM 身份证明为新对象时认定 Workbook 属于本次调用。"""
    if any(workbook is previous for previous in previous_workbooks):
        return False
    current_id = _com_object_identity(workbook)
    if current_id is None:
        return None
    unknown_previous = False
    for previous in previous_workbooks:
        relation = _same_com_object(previous, workbook)
        if relation is True:
            return False
        if relation is None:
            unknown_previous = True
    if unknown_previous:
        return None
    return True


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
    app = None
    app_owned_by_jiadun = False
    pids_before = _safe_process_snapshot(pid_snapshot)
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
                break
            except Exception:  # noqa: BLE001
                app = None
        # Dispatch 可能附着到刚刚启动的用户进程；只有 DispatchEx 明确
        # 创建独立本地实例，才允许本次调用在结束时 Quit。
        dispatch_ex = getattr(client, "DispatchEx", None)
        if callable(dispatch_ex):
            try:
                app = dispatch_ex(progid)
                pids_after = _safe_process_snapshot(pid_snapshot)
                app_owned_by_jiadun = _decide_started_here(
                    pids_before, pids_after, attached_ok=False
                )
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
    result = "located"
    workbook = None
    worksheet = None
    previous_workbooks: list[object] = []
    workbook_inventory_known = False
    # True 只在“调用前集合可完整读取且目标不在其中”时赋予；False 覆盖
    # 用户已打开、集合不可读或任何证据不足的情形（fail-safe）。
    workbook_owned_by_jiadun = False
    try:
        previous_workbooks, workbook_inventory_known = _workbook_inventory(app)
        existing_workbook, ownership_known = _find_target_in_snapshot(
            previous_workbooks, workbook_inventory_known, target.file_path
        )
        if existing_workbook is not None:
            # 用户已经打开目标文件时直接定位现有对象，不再再次 Open，避免
            # Excel/WPS 返回第二个只读 Workbook 后无法安全判断其所有权。
            workbook = existing_workbook
        elif not ownership_known:
            raise RuntimeError("无法确认目标 Workbook 所有权，拒绝通过 COM 打开")
        else:
            workbook = app.Workbooks.Open(str(target.file_path), 0, True)
            workbook_owned_by_jiadun = (
                _workbook_opened_by_call(workbook, previous_workbooks) is True
            )
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
        if workbook is not None and workbook_owned_by_jiadun:
            try:
                workbook.Close(False)
            except Exception:  # noqa: BLE001
                pass
        if app_owned_by_jiadun and (workbook is None or workbook_owned_by_jiadun):
            try:
                # 即使 app 由 DispatchEx 创建，也要在 Quit 前确认当前集合
                # 可完整读取且已经没有任何 Workbook，避免关闭调用期间被
                # 用户放入该实例的文档。未知状态一律不 Quit。
                current_workbooks, current_known = _workbook_inventory(app)
                if current_known and not current_workbooks:
                    app.Quit()
            except Exception:  # noqa: BLE001
                pass
        worksheet = None
        workbook = None
        app = None
    return result
