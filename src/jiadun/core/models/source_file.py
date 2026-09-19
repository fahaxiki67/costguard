"""原始文件导入（ADR-005）：只读副本 + SHA256 登记。

纪律：
- 导入是副本写入的唯一入口；原文件绝不修改；
- originals/ 内副本设为只读；
- 同项目内相同 SHA256 的文件不重复复制（登记复用）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# hexdigest() 的形态契约（64 位小写十六进制）；originals/ 副本文件名以此为唯一寻址。
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

FILE_TYPE_BY_SUFFIX = {
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xls",
    ".csv": "csv",
    ".txt": "txt",
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
}


class SourceFileError(Exception):
    pass


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class SourceFile:
    file_id: int
    original_path: str
    stored_path: str
    original_name: str
    sha256: str
    file_type: str
    size_bytes: int


def import_file(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    *,
    commit: bool = True,
) -> SourceFile:
    """复制 src 到项目 originals/ 并登记。返回登记信息。

    ``commit=False`` 供结算导入的外层事务使用；副本文件本身仍按只读资产
    写入，数据库登记会随调用方事务一起提交或回滚。

    边界纪律：输入必须是已存在的常规文件（symlink 解析到最终目标后校验）；
    副本与 .importing 中间文件一律只允许落在 ``originals`` 根目录内，
    写入前对 resolve 后的路径做包含校验。
    """
    src_resolved = Path(src).resolve()
    if not src_resolved.is_file():
        raise SourceFileError(f"source file not found: {src}")
    suffix = src_resolved.suffix.lower()
    ftype = FILE_TYPE_BY_SUFFIX.get(suffix)
    if ftype is None:
        raise SourceFileError(f"unsupported file type: {suffix}")

    digest = sha256_of(src_resolved)
    # originals/ 以「sha256 + 后缀」寻址；入库前校验摘要形态，保证副本
    # 文件名永远是受限的十六进制标识（防哈希函数/格式漂移带坏存储布局）。
    if not _SHA256_HEX_RE.fullmatch(digest):
        raise SourceFileError(f"invalid sha256 digest: {digest!r}")
    size = src_resolved.stat().st_size

    originals = Path(project_dir) / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    originals_root = originals.resolve()
    # hexdigest 已通过 _SHA256_HEX_RE 校验，pathlib 组合即可，不做字符串插值。
    stored = originals.joinpath(digest).with_suffix(suffix)
    if stored.resolve().parent != originals_root:
        raise SourceFileError(
            f"副本路径越出 originals 根目录：{stored}（根目录 {originals_root}）"
        )

    row = conn.execute(
        "SELECT id, stored_path FROM source_files WHERE project_id=? AND sha256=?",
        (project_id, digest),
    ).fetchone()
    if row:  # 同一文件重复导入：复用已有副本
        return SourceFile(
            row["id"],
            _current_original(conn, row["id"]),
            row["stored_path"], src.name, digest, ftype, size,
        )

    if not stored.exists():  # 不同项目目录或首见文件：复制
        tmp = stored.with_suffix(suffix + ".importing")
        if tmp.resolve().parent != originals_root:
            raise SourceFileError(
                f"中间文件路径越出 originals 根目录：{tmp}（根目录 {originals_root}）"
            )
        with src_resolved.open("rb") as fin, tmp.open("wb") as fout:
            while True:
                block = fin.read(1 << 20)
                if not block:
                    break
                fout.write(block)
        os.chmod(tmp, 0o444)  # 副本只读
        tmp.rename(stored)
    os.chmod(stored, 0o444) if os.name != "nt" else None

    now = datetime.now().isoformat(timespec="seconds")
    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO source_files
               (project_id, original_path, stored_path, original_name, sha256, size_bytes, file_type, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, str(src), str(stored), src.name, digest, size, ftype, now),
        )
        return int(cur.lastrowid)
    if not commit:
        file_id = _insert()
    elif conn.in_transaction:
        # 不提交调用方已经打开的外层事务；导入编排会在同一事务中登记
        # source_files，并在后续物化失败时整体回滚数据库记录。
        # SAVEPOINT 用常量名与常量语句：登记是单层调用，语句文本不拼接。
        conn.execute("SAVEPOINT source_file_insert")
        try:
            file_id = _insert()
        except Exception:
            conn.execute("ROLLBACK TO source_file_insert")
            conn.execute("RELEASE source_file_insert")
            raise
        else:
            conn.execute("RELEASE source_file_insert")
    else:
        with conn:
            file_id = _insert()
    return SourceFile(file_id, str(src), str(stored), src.name, digest, ftype, size)


def _current_original(conn: sqlite3.Connection, file_id: int) -> str:
    row = conn.execute("SELECT original_path FROM source_files WHERE id=?", (file_id,)).fetchone()
    return row["original_path"]


def list_files(conn: sqlite3.Connection, project_id: int) -> list[SourceFile]:
    rows = conn.execute(
        "SELECT id, original_path, stored_path, original_name, sha256, file_type, size_bytes "
        "FROM source_files WHERE project_id=? ORDER BY imported_at, id",
        (project_id,),
    ).fetchall()
    return [
        SourceFile(r["id"], r["original_path"], r["stored_path"], r["original_name"], r["sha256"], r["file_type"], r["size_bytes"])
        for r in rows
    ]
