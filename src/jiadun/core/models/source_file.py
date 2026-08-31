"""原始文件导入（ADR-005）：只读副本 + SHA256 登记。

纪律：
- 导入是副本写入的唯一入口；原文件绝不修改；
- originals/ 内副本设为只读；
- 同项目内相同 SHA256 的文件不重复复制（登记复用）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


def import_file(conn: sqlite3.Connection, project_id: int, project_dir: Path, src: Path) -> SourceFile:
    """复制 src 到项目 originals/ 并登记。返回登记信息。"""
    src = Path(src)
    if not src.is_file():
        raise SourceFileError(f"source file not found: {src}")
    suffix = src.suffix.lower()
    ftype = FILE_TYPE_BY_SUFFIX.get(suffix)
    if ftype is None:
        raise SourceFileError(f"unsupported file type: {suffix}")

    digest = sha256_of(src)
    size = src.stat().st_size

    originals = Path(project_dir) / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    stored = originals / f"{digest}{suffix}"

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
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                block = fin.read(1 << 20)
                if not block:
                    break
                fout.write(block)
        os.chmod(tmp, 0o444)  # 副本只读
        tmp.rename(stored)
    os.chmod(stored, 0o444) if os.name != "nt" else None

    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            """INSERT INTO source_files
               (project_id, original_path, stored_path, original_name, sha256, size_bytes, file_type, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, str(src), str(stored), src.name, digest, size, ftype, now),
        )
        file_id = cur.lastrowid
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
