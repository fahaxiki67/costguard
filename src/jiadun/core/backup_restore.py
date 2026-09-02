"""项目备份与恢复（P3 生产化）。

纪律：
- project.db 通过 SQLite backup API 取一致性快照（WAL 也安全），不直接拷贝热库；
- 备份包为 zip + manifest.json（文件清单 + SHA-256），恢复前逐文件校验；
- 恢复目标已存在同名项目时拒绝（不覆盖用户数据，fail-closed）；
- 恢复后立即做 integrity check + 打开验证，失败即清理半成品目录。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BACKUP_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_DB_NAME = "project.db"
# 项目目录内随时间可再生的内容不进备份包
_SKIP_DIRS = {"exports", "backups", ".git"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    integrity: str          # PRAGMA integrity_check 首行（ok 或错误描述）
    foreign_key_violations: list[str]
    tables: int


def integrity_check(conn: sqlite3.Connection) -> IntegrityReport:
    """PRAGMA integrity_check + foreign_key_check（只读，不改库）。

    损坏库（文件头损坏、页校验失败等）可能让 PRAGMA 本身抛
    DatabaseError，此时如实报告损坏而不是把异常抛给调用方。
    """
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        first = rows[0][0] if rows else "empty"
    except sqlite3.DatabaseError as exc:
        return IntegrityReport(
            ok=False, integrity=f"integrity_check failed: {exc}",
            foreign_key_violations=[], tables=0)
    try:
        violations = [
            f"{r[0]}: {r[1]}" if len(r) > 1 and r[1] is not None else str(r[0])
            for r in conn.execute("PRAGMA foreign_key_check").fetchall()
        ]
    except sqlite3.DatabaseError as exc:
        violations = [f"foreign_key_check failed: {exc}"]
    try:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    except sqlite3.DatabaseError:
        tables = 0
    return IntegrityReport(
        ok=(first == "ok" and not violations),
        integrity=str(first),
        foreign_key_violations=violations,
        tables=int(tables),
    )


def _snapshot_db(db_path: Path, dest: Path) -> None:
    """用 SQLite backup API 写一致性副本（源库可处于 WAL/被连接状态）。"""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
            # 快照自检：备份库自身必须完整
            snap = sqlite3.connect(dest)
            try:
                row = snap.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise RuntimeError(f"backup snapshot integrity failed: {row}")
            finally:
                snap.close()
        finally:
            dst.close()
    finally:
        src.close()


def _collect_project_files(pdir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(pdir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(pdir)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        files.append(path)
    return files


def backup_project(pdir: Path, dest_dir: Path | None = None) -> Path:
    """把项目打包为 zip（含 manifest），返回备份文件路径。

    dest_dir 缺省 = 项目目录 backups/ 下。备份失败不留半成品。
    """
    pdir = Path(pdir)
    db = pdir / _DB_NAME
    if not db.is_file():
        raise FileNotFoundError(f"not a Jiadun project (missing project.db): {pdir}")
    dest_dir = Path(dest_dir) if dest_dir else pdir / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory(prefix="jiadun_backup_") as td:
        tdir = Path(td)
        staged: list[tuple[Path, str]] = []  # (文件, 归档内相对路径)
        # DB 一致性快照
        snap = tdir / _DB_NAME
        _snapshot_db(db, snap)
        staged.append((snap, _DB_NAME))
        # originals（只读副本）等其余文件原样打包
        for path in _collect_project_files(pdir):
            if path == db:
                continue
            staged.append((path, str(path.relative_to(pdir))))

        out_path = dest_dir / f"project_backup_{pdir.name}_{stamp}.zip"
        tmp_out = out_path.with_suffix(".zip.partial")
        entries: list[dict] = []
        try:
            with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf:
                for src, arcname in staged:
                    zf.write(src, arcname)
                    entries.append({
                        "path": arcname.replace("\\", "/"),
                        "sha256": _sha256(src),
                        "size": src.stat().st_size,
                    })
            manifest = {
                "backup_format_version": BACKUP_FORMAT_VERSION,
                "project_dir_name": pdir.name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "entries": entries,
            }
            with zipfile.ZipFile(tmp_out, "a", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(_MANIFEST_NAME,
                            json.dumps(manifest, ensure_ascii=False, indent=2))
            tmp_out.replace(out_path)
        except BaseException:
            if tmp_out.exists():
                tmp_out.unlink()
            raise
    return out_path


def read_backup_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if _MANIFEST_NAME not in names:
            raise ValueError(f"备份包缺少 {_MANIFEST_NAME}：{zip_path}")
        return json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))


def restore_project(zip_path: Path, target_parent: Path) -> Path:
    """恢复备份到 target_parent/<项目名>；返回项目目录。fail-closed。"""
    zip_path = Path(zip_path)
    target_parent = Path(target_parent)
    manifest = read_backup_manifest(zip_path)
    if int(manifest.get("backup_format_version", -1)) > BACKUP_FORMAT_VERSION:
        raise ValueError("备份包格式版本比当前软件新，拒绝恢复（请升级软件）")
    entry_map = {e["path"]: e for e in manifest["entries"]}
    if _DB_NAME not in entry_map:
        raise ValueError("备份包缺少 project.db，拒绝恢复")

    pdir = target_parent / manifest.get("project_dir_name", "restored_project")
    if pdir.exists() and any(pdir.iterdir()):
        raise FileExistsError(f"恢复目标已存在且非空，拒绝覆盖：{pdir}")
    pdir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.filename == _MANIFEST_NAME or info.is_dir():
                    continue
                entry = entry_map.get(info.filename)
                if entry is None:
                    raise ValueError(f"备份包内出现清单外文件：{info.filename}")
                dest = pdir / info.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                actual = _sha256(dest)
                if actual != entry["sha256"]:
                    raise ValueError(
                        f"恢复校验失败 {info.filename}: {actual} != {entry['sha256']}")
                if dest.stat().st_size != entry["size"]:
                    raise ValueError(f"恢复大小不符：{info.filename}")
        # 恢复后立即可用性验证
        snap_dir = pdir
        conn = sqlite3.connect(snap_dir / _DB_NAME)
        try:
            rep = integrity_check(conn)
            if not rep.ok:
                raise RuntimeError(f"恢复后的数据库完整性检查失败：{rep.integrity}")
        finally:
            conn.close()
        from jiadun.core.models.project import open_project as _open

        _info, open_conn = _open(snap_dir)  # 能打开（含迁移路径检查）才算恢复成功
        open_conn.close()
    except BaseException:
        shutil.rmtree(pdir, ignore_errors=True)
        raise
    return pdir


def verify_backup(zip_path: Path) -> dict:
    """只读校验备份包：清单 + 每个文件 SHA-256 + 内嵌库 integrity。"""
    manifest = read_backup_manifest(zip_path)
    entry_map = {e["path"]: e for e in manifest["entries"]}
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="jiadun_bverify_") as td:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.filename == _MANIFEST_NAME or info.is_dir():
                    continue
                entry = entry_map.get(info.filename)
                if entry is None:
                    bad.append(f"{info.filename}: 清单外文件")
                    continue
                data = zf.read(info)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    bad.append(f"{info.filename}: sha256 不符")
        db_extract = Path(td) / _DB_NAME
        with zipfile.ZipFile(zip_path) as zf:
            zf.extract(_DB_NAME, td)
        conn = sqlite3.connect(db_extract)
        try:
            rep = integrity_check(conn)
        finally:
            conn.close()
    return {
        "ok": not bad and rep.ok,
        "bad_entries": bad,
        "integrity": rep.integrity,
        "entries": len(manifest["entries"]),
    }
