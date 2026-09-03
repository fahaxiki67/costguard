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
import ntpath
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jiadun.platform.secure_fs import SecureDirectory

BACKUP_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_DB_NAME = "project.db"
# 项目目录内随时间可再生的内容不进备份包
_SKIP_DIRS = {"exports", "backups", ".git"}
_SKIP_FILES = {"project.db-shm", "project.db-wal", "project.db-journal"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    *(f"COM{suffix}" for suffix in ("¹", "²", "³")),
    *(f"LPT{suffix}" for suffix in ("¹", "²", "³")),
})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# ZIP 中心目录本身仍需由 zipfile 读取，但在任何解压/完整读取前限制声明的
# 条目数和解压大小，避免恶意压缩包以 ZIP bomb 或超大 manifest 耗尽资源。
_MAX_BACKUP_ENTRIES = 100_000
_MAX_BACKUP_ENTRY_BYTES = 4 * 1024**3
_MAX_BACKUP_TOTAL_BYTES = 8 * 1024**3
_MAX_MANIFEST_BYTES = 8 * 1024**2
_MAX_WINDOWS_COMPONENT_CHARS = 255
_MAX_ARCHIVE_PATH_CHARS = 32_767


@dataclass(frozen=True)
class _CreatedPath:
    """本次调用创建的文件系统对象及其创建时身份。"""

    path: Path
    st_dev: int
    st_ino: int
    expected_sha256: str | None = None
    expected_size: int | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_stat(path: Path) -> os.stat_result | None:
    """读取路径自身的 lstat；无法判断时直接拒绝，不能降级为跟随路径。"""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"无法安全检查路径，拒绝继续：{path}") from exc


def _is_reparse_point(path: Path, st: os.stat_result | None = None) -> bool:
    """识别 symlink、Windows junction 及其他 reparse point。"""
    if st is None:
        st = _path_stat(path)
    if st is None:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    # Python 3.12 在 Windows 提供 Path.is_junction；使用 getattr 保持 core
    # 在 macOS/Linux 可导入，调用失败时仍按不安全处理。
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError as exc:
            raise ValueError(f"无法判断 junction，拒绝继续：{path}") from exc
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_path(path: Path, *, require_directory: bool = False) -> None:
    """确认已有路径链没有 symlink/junction/reparse point。

    只检查 lexical path 链本身，不以 ``resolve`` 代替检查；否则把越界链接
    解析到外部后，调用方可能误以为路径已经安全。缺失的尾部允许由本次调用
    逐级创建，任何无法检查的状态都 fail-closed。
    """
    absolute = Path(path).absolute()
    missing_seen = False
    chain = list(reversed((absolute, *absolute.parents)))
    for current in chain:
        st = _path_stat(current)
        if st is None:
            missing_seen = True
            continue
        if missing_seen:
            # 正常文件系统不会出现“父项缺失而子项存在”；出现时不再猜测。
            raise ValueError(f"路径链状态不一致，拒绝继续：{path}")
        if _is_reparse_point(current, st):
            raise ValueError(f"路径包含 symlink/junction/reparse point，拒绝继续：{path}")
        if current != absolute and not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"路径中间项不是目录：{current}")
        if current == absolute and require_directory and not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"恢复目标不是目录：{current}")


def _remember_created_path(
    path: Path,
    st: os.stat_result,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> _CreatedPath:
    """记录对象身份；无法得到稳定 inode 时宁可让失败现场保留。"""
    return _CreatedPath(
        path=Path(path),
        st_dev=int(st.st_dev),
        st_ino=int(st.st_ino),
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )


def _ensure_directory(path: Path, created_dirs: list[_CreatedPath]) -> None:
    """逐级创建目录并记录所有本次新建项，绝不穿过 reparse point。"""
    path = Path(path).absolute()
    _assert_safe_path(path)
    if _path_stat(path) is not None:
        _assert_safe_path(path, require_directory=True)
        return

    missing: list[Path] = []
    cursor = path
    while True:
        st = _path_stat(cursor)
        if st is None:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise ValueError(f"找不到安全的目录根：{path}")
            cursor = parent
            continue
        if _is_reparse_point(cursor, st):
            raise ValueError(f"目录链包含 symlink/junction/reparse point，拒绝继续：{path}")
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"路径中间项不是目录：{cursor}")
        break

    for directory in reversed(missing):
        _assert_safe_path(directory.parent, require_directory=True)
        try:
            directory.mkdir()
        except FileExistsError:
            # 竞争创建也必须重新检查，不能默认“已经存在”就是普通目录。
            _assert_safe_path(directory, require_directory=True)
        else:
            created = _path_stat(directory)
            if created is None or not stat.S_ISDIR(created.st_mode):
                raise ValueError(f"新建目录无法安全确认，拒绝继续：{directory}")
            created_dirs.append(_remember_created_path(directory, created))
            _assert_safe_path(directory, require_directory=True)


def _cleanup_created_paths(
    created_files: list[_CreatedPath], created_dirs: list[_CreatedPath]
) -> None:
    """只清理本次成功创建且仍是普通文件/空目录的路径。

    不使用递归删除：即使外部进程在失败期间改变了现场，也不跟随链接、
    不删除已有目录，并且无法确认安全时宁可留下现场供人工复核。
    """
    for created in reversed(created_files):
        path = created.path
        try:
            _assert_safe_path(path.parent, require_directory=True)
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, NotADirectoryError):
            continue
        try:
            unsafe = _is_reparse_point(path, st)
        except Exception:  # noqa: BLE001 - 无法确认时保留现场
            continue
        if (
            created.st_ino == 0
            or int(st.st_dev) != created.st_dev
            or int(st.st_ino) != created.st_ino
            or unsafe
            or not stat.S_ISREG(st.st_mode)
            or (
                created.expected_size is not None
                and int(st.st_size) != created.expected_size
            )
        ):
            continue
        if created.expected_sha256 is not None:
            try:
                if _sha256(path) != created.expected_sha256:
                    continue
            except Exception:  # noqa: BLE001 - 无法复核时保留现场
                continue
        try:
            path.unlink()
        except OSError:
            pass
    for created in reversed(created_dirs):
        path = created.path
        try:
            _assert_safe_path(path.parent, require_directory=True)
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, NotADirectoryError):
            continue
        try:
            unsafe = _is_reparse_point(path, st)
        except Exception:  # noqa: BLE001 - 无法确认时保留现场
            continue
        if (
            created.st_ino == 0
            or int(st.st_dev) != created.st_dev
            or int(st.st_ino) != created.st_ino
            or unsafe
            or not stat.S_ISDIR(st.st_mode)
        ):
            continue
        try:
            path.rmdir()
        except OSError:
            # 只允许删除空目录；有未知内容时保留现场。
            pass


def _record_secure_created_directory(
    path: Path,
    created_dirs: list[_CreatedPath],
    bucket: list[_CreatedPath] | None = None,
) -> _CreatedPath:
    """记录由 ``SecureDirectory`` 创建的目录，供保守清理使用。"""
    path = Path(path).absolute()
    st = _path_stat(path)
    if st is None or not stat.S_ISDIR(st.st_mode) or _is_reparse_point(path, st):
        raise ValueError(f"安全新建目录无法确认，拒绝继续：{path}")
    created = _remember_created_path(path, st)
    created_dirs.append(created)
    if bucket is not None:
        bucket.append(created)
    return created


def _secure_child_directory(
    parent: SecureDirectory,
    name: str,
    *,
    created_dirs: list[_CreatedPath],
    bucket: list[_CreatedPath] | None = None,
    require_new: bool = False,
    label: str,
) -> SecureDirectory:
    """通过已持有的父目录句柄打开一个子目录。"""
    child, created = parent.child(name, create=True)
    if require_new and not created:
        child.close()
        raise FileExistsError(f"{label}已存在，拒绝继续：{parent.path / name}")
    try:
        if created:
            _record_secure_created_directory(child.path, created_dirs, bucket)
    except BaseException:
        child.close()
        raise
    return child


def _close_secure_directories(guards: list[SecureDirectory]) -> None:
    """逆序关闭目录句柄；清理时不因单个关闭错误跳过其余句柄。"""
    for guard in reversed(guards):
        try:
            guard.close()
        except OSError:
            pass


def _validate_windows_component(value: str, *, field: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"{field} 不是合法名称")
    if len(value) > _MAX_WINDOWS_COMPONENT_CHARS:
        raise ValueError(f"{field} 超过 Windows 单段路径长度上限")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} 含 Windows 非法控制字符")
    if any(char in _WINDOWS_FORBIDDEN_CHARS for char in value):
        raise ValueError(f"{field} 含 Windows 非法字符")
    if value[-1] in {".", " "}:
        raise ValueError(f"{field} 不能以点号或空格结尾")
    # Windows 会把设备名识别应用到扩展名前的部分，并忽略该部分
    # 末尾的空格/点；例如 ``CON .txt`` 仍不能作为普通文件名。
    stem = value.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{field} 使用 Windows 保留设备名")


def _validate_project_dir_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("manifest.project_dir_name 必须是字符串")
    _validate_windows_component(value, field="manifest.project_dir_name")
    return value


def _validate_archive_relative_path(value: object) -> str:
    """严格验证归档内相对路径；不做清洗、不替换、不截断。"""
    if not isinstance(value, str) or not value:
        raise ValueError("ZIP/manifest 路径必须是非空字符串")
    if len(value) > _MAX_ARCHIVE_PATH_CHARS:
        raise ValueError("ZIP/manifest 路径超过安全长度上限")
    if "\x00" in value:
        raise ValueError("ZIP/manifest 路径含 NUL，拒绝恢复")
    if "\\" in value:
        raise ValueError("ZIP/manifest 路径含反斜杠，拒绝恢复")
    if value.startswith("/") or ntpath.splitdrive(value)[0]:
        raise ValueError(f"ZIP/manifest 路径不是相对路径，拒绝恢复：{value}")
    parts = value.split("/")
    if any(not part for part in parts):
        raise ValueError(f"ZIP/manifest 路径含空路径段，拒绝恢复：{value}")
    for part in parts:
        _validate_windows_component(part, field="ZIP/manifest 路径段")
    return value


def _collision_key(value: str) -> str:
    # Windows/macOS 常见文件系统对大小写/Unicode 规范化不敏感；在所有平台
    # 一致拒绝潜在碰撞，避免同一备份在目标平台落到同一个最终路径。
    return unicodedata.normalize("NFC", value).casefold()


def _validate_manifest(manifest: object) -> tuple[str, dict[str, dict]]:
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    version = manifest.get("backup_format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("manifest.backup_format_version 非法")
    project_dir_name = _validate_project_dir_name(manifest.get("project_dir_name"))
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("manifest.entries 必须是非空数组")
    if len(raw_entries) > _MAX_BACKUP_ENTRIES:
        raise ValueError("manifest.entries 数量超过安全上限")

    entry_map: dict[str, dict] = {}
    collision_keys: set[str] = {_collision_key(_MANIFEST_NAME)}
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"manifest.entries[{index}] 不是对象")
        path = _validate_archive_relative_path(entry.get("path"))
        if _collision_key(path) == _collision_key(_MANIFEST_NAME):
            raise ValueError("manifest.entries 不得包含 manifest.json")
        key = _collision_key(path)
        if key in collision_keys:
            raise ValueError(f"manifest.entries 存在重复或大小写碰撞路径：{path}")
        collision_keys.add(key)
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError(f"manifest.entries[{index}].sha256 非法")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"manifest.entries[{index}].size 非法")
        entry_map[path] = {"path": path, "sha256": sha256.lower(), "size": size}
    if _DB_NAME not in entry_map:
        raise ValueError("备份包缺少 project.db，拒绝恢复")
    return project_dir_name, entry_map


def _manifest_json(data: bytes, zip_path: Path) -> dict:
    class _DuplicateManifestKeyError(ValueError):
        pass

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateManifestKeyError(f"manifest JSON 存在重复键：{key}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except _DuplicateManifestKeyError as exc:
        raise ValueError(str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"manifest.json 无法安全解析：{zip_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("manifest.json 必须是 JSON 对象")
    return value


def _zip_info_is_unsafe(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir() or file_type == stat.S_IFDIR:
        return True
    # 0 表示没有 Unix 类型信息；有类型信息时只接受普通文件，拒绝
    # symlink、FIFO、设备等特殊条目。
    return bool(file_type and file_type != stat.S_IFREG)


def _validate_zipfile(
    zf: zipfile.ZipFile, zip_path: Path
) -> tuple[dict, dict[str, dict], dict[str, zipfile.ZipInfo]]:
    infos = zf.infolist()
    if len(infos) > _MAX_BACKUP_ENTRIES:
        raise ValueError("备份包条目数量超过安全上限，拒绝恢复")
    manifest_infos = [info for info in infos if info.filename == _MANIFEST_NAME]
    if len(manifest_infos) != 1:
        raise ValueError(f"备份包必须恰好包含一个 {_MANIFEST_NAME}：{zip_path}")
    if manifest_infos[0].file_size > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest.json 超过安全大小上限，拒绝恢复")

    zip_entries: dict[str, zipfile.ZipInfo] = {}
    collision_keys: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        if not isinstance(info.filename, str):
            raise ValueError("ZIP 条目名称不是字符串，拒绝恢复")
        if info.file_size < 0 or info.file_size > _MAX_BACKUP_ENTRY_BYTES:
            raise ValueError(f"ZIP 条目声明大小超过安全上限，拒绝恢复：{info.filename}")
        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_BACKUP_TOTAL_BYTES:
            raise ValueError("备份包声明的解压总大小超过安全上限，拒绝恢复")
        if _zip_info_is_unsafe(info):
            raise ValueError(f"ZIP 包含目录或 symlink/特殊文件条目，拒绝恢复：{info.filename}")
        if info.filename == _MANIFEST_NAME:
            path = _MANIFEST_NAME
        else:
            path = _validate_archive_relative_path(info.filename)
            if _collision_key(path) == _collision_key(_MANIFEST_NAME):
                raise ValueError("ZIP 条目与 manifest.json 路径碰撞，拒绝恢复")
        key = _collision_key(path)
        if key in collision_keys:
            raise ValueError(f"ZIP 存在重复或大小写碰撞文件名，拒绝恢复：{info.filename}")
        collision_keys.add(key)
        if path != _MANIFEST_NAME:
            zip_entries[path] = info

    manifest = _manifest_json(zf.read(manifest_infos[0]), zip_path)
    project_dir_name, entry_map = _validate_manifest(manifest)
    if manifest["backup_format_version"] > BACKUP_FORMAT_VERSION:
        raise ValueError("备份包格式版本比当前软件新，拒绝继续（请升级软件）")
    del project_dir_name  # 由调用方再次取值，保持 manifest 原始结构不被改写
    manifest_paths = set(entry_map)
    zip_paths = set(zip_entries)
    if manifest_paths != zip_paths:
        raise ValueError(
            "manifest 与 ZIP 内容不一致，拒绝恢复 "
            f"（manifest 文件 {len(manifest_paths)} 个，ZIP 文件 {len(zip_paths)} 个）"
        )
    for path, info in zip_entries.items():
        if info.file_size != entry_map[path]["size"]:
            raise ValueError(f"manifest 与 ZIP 文件大小不一致，拒绝恢复：{path}")
    return manifest, entry_map, zip_entries


def _assert_within(root: Path, candidate: Path) -> None:
    root_resolved = Path(root).resolve(strict=True)
    candidate_resolved = Path(candidate).resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"恢复路径越界，拒绝恢复：{candidate}") from exc


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
    before = _path_stat(db_path)
    if before is None or not stat.S_ISREG(before.st_mode) or _is_reparse_point(db_path, before):
        raise ValueError(f"备份源数据库已不是安全的普通文件：{db_path}")
    _assert_safe_path(db_path.parent, require_directory=True)
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
        after = _path_stat(db_path)
        if (
            after is None
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
        ):
            raise ValueError(f"备份源数据库在快照期间发生变化，拒绝继续：{db_path}")
    finally:
        src.close()


def _collect_project_files(pdir: Path) -> list[Path]:
    pdir = Path(pdir).absolute()
    _assert_safe_path(pdir, require_directory=True)
    files: list[Path] = []

    def visit(directory: Path) -> None:
        _assert_safe_path(directory, require_directory=True)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ValueError(f"无法安全读取项目目录，拒绝备份：{directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"无法安全读取项目项，拒绝备份：{path}") from exc
            if _is_reparse_point(path, st):
                raise ValueError(f"项目包含 symlink/junction/reparse point，拒绝备份：{path}")
            if stat.S_ISDIR(st.st_mode):
                if entry.name in _SKIP_DIRS:
                    continue
                visit(path)
                continue
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"项目包含非普通文件，拒绝备份：{path}")
            relative = path.relative_to(pdir).as_posix()
            _validate_archive_relative_path(relative)
            if relative in _SKIP_FILES:
                continue
            if relative == _MANIFEST_NAME:
                raise ValueError(
                    "项目根目录保留文件名 manifest.json，拒绝生成含义冲突的备份"
                )
            files.append(path)

    visit(pdir)
    return files


def _write_regular_file_to_zip(
    zf: zipfile.ZipFile, source: Path, arcname: str
) -> tuple[str, int]:
    """从同一个只读句柄复制并计算清单，避免扫描后再次跟随替换路径。"""
    _assert_safe_path(source.parent, require_directory=True)
    before = _path_stat(source)
    if before is None or not stat.S_ISREG(before.st_mode) or _is_reparse_point(source, before):
        raise ValueError(f"备份源文件已不是安全的普通文件：{source}")
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src:
        opened = os.fstat(src.fileno())
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ValueError(f"备份源文件在读取前发生变化，拒绝继续：{source}")
        with zf.open(arcname, "w", force_zip64=True) as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
    after = _path_stat(source)
    _assert_safe_path(source.parent, require_directory=True)
    if (
        after is None
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != size
    ):
        raise ValueError(f"备份源文件在读取期间发生变化，拒绝继续：{source}")
    return digest.hexdigest(), size


def _copy_limited(src, dst, limit: int) -> int:
    """复制时同时执行实际解压字节预算，避免只相信 ZIP 头部声明。"""
    written = 0
    for chunk in iter(lambda: src.read(1 << 20), b""):
        written += len(chunk)
        if written > limit:
            raise ValueError("ZIP 实际解压大小超过 manifest 安全上限")
        dst.write(chunk)
    return written


def _secure_file_digest(parent: SecureDirectory, name: str) -> tuple[int, str]:
    """通过父目录句柄读取并计算普通文件的大小和 SHA-256。"""
    digest = hashlib.sha256()
    with parent.open_file(name) as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"安全校验目标不是普通文件：{parent.path / name}")
        size = 0
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _assert_secure_file_matches(
    parent: SecureDirectory,
    name: str,
    relative_path: str,
    expected_size: int,
    expected_sha256: str,
    *,
    final: bool = False,
) -> None:
    size, digest = _secure_file_digest(parent, name)
    label = "恢复最终" if final else "恢复"
    if size != expected_size:
        raise ValueError(f"{label}大小不符：{relative_path}")
    if digest != expected_sha256:
        raise ValueError(
            f"{label}校验失败 {relative_path}: {digest} != {expected_sha256}"
        )


def backup_project(pdir: Path, dest_dir: Path | None = None) -> Path:
    """把项目打包为 zip；失败时只清理本次调用新建的目录。"""
    created_dirs: list[_CreatedPath] = []
    try:
        return _backup_project_impl(pdir, dest_dir, created_dirs)
    except BaseException:
        # 目标目录可能是在快照、遍历或 ZIP 写入阶段才失败；此时也必须
        # 回收本次新建的空目录，但不能碰调用前已经存在的用户目录。
        _cleanup_created_paths([], created_dirs)
        raise


def _backup_project_impl(
    pdir: Path,
    dest_dir: Path | None,
    created_dirs: list[_CreatedPath],
) -> Path:
    """备份实现；``created_dirs`` 由外层统一负责失败清理。"""
    pdir = Path(pdir).absolute()
    db = pdir / _DB_NAME
    _assert_safe_path(pdir, require_directory=True)
    _validate_project_dir_name(pdir.name)
    db_stat = _path_stat(db)
    if db_stat is None or not stat.S_ISREG(db_stat.st_mode) or _is_reparse_point(db, db_stat):
        raise FileNotFoundError(f"not a Jiadun project (missing project.db): {pdir}")
    dest_dir = Path(dest_dir) if dest_dir else pdir / "backups"
    dest_dir = dest_dir.absolute()
    _ensure_directory(dest_dir, created_dirs)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    with tempfile.TemporaryDirectory(prefix="jiadun_backup_") as td:
        # 系统临时目录可能位于 symlink 之后（macOS /var→/private/var）；
        # 先解析到真实路径，避免自身的 fail-closed 链检查误拒操作系统前缀。
        tdir = Path(td).resolve()
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
        if _path_stat(out_path) is not None:
            raise FileExistsError(f"备份目标已存在，拒绝覆盖：{out_path}")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{out_path.name}.", suffix=".partial", dir=dest_dir
        )
        tmp_out = Path(temp_name)
        tmp_stat = os.fstat(fd)
        entries: list[dict] = []
        try:
            with os.fdopen(fd, "w+b") as temp_handle:
                with zipfile.ZipFile(
                    temp_handle, "w", zipfile.ZIP_DEFLATED
                ) as zf:
                    for src, arcname in staged:
                        archive_path = arcname.replace("\\", "/")
                        digest, size = _write_regular_file_to_zip(
                            zf, src, archive_path
                        )
                        entries.append({
                            "path": archive_path,
                            "sha256": digest,
                            "size": size,
                        })
                    manifest = {
                        "backup_format_version": BACKUP_FORMAT_VERSION,
                        "project_dir_name": pdir.name,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "entries": entries,
                    }
                    zf.writestr(
                        _MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                    )
            # mkstemp 的句柄仍绑定到本次创建的对象；硬链接提交不覆盖任何
            # 已存在目标，失败时也不会把既有用户文件当作临时文件清理。
            current_tmp = _path_stat(tmp_out)
            if (
                current_tmp is None
                or current_tmp.st_dev != tmp_stat.st_dev
                or current_tmp.st_ino != tmp_stat.st_ino
            ):
                raise ValueError("备份临时文件身份发生变化，拒绝提交")
            try:
                os.link(tmp_out, out_path)
            except FileExistsError as exc:
                raise FileExistsError(f"备份目标已存在，拒绝覆盖：{out_path}") from exc
            tmp_out.unlink()
        except BaseException:
            try:
                current_tmp = _path_stat(tmp_out)
                if (
                    current_tmp is not None
                    and current_tmp.st_dev == tmp_stat.st_dev
                    and current_tmp.st_ino == tmp_stat.st_ino
                ):
                    tmp_out.unlink()
            except OSError:
                pass
            raise
    return out_path


def read_backup_manifest(zip_path: Path) -> dict:
    """读取并完整验证清单与 ZIP 内容，不返回未经校验的 manifest。"""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        manifest, _entry_map, _zip_entries = _validate_zipfile(zf, zip_path)
        return manifest


def restore_project(zip_path: Path, target_parent: Path) -> Path:
    """恢复备份到 target_parent/<项目名>；返回项目目录。fail-closed。"""
    zip_path = Path(zip_path)
    target_parent = Path(target_parent).absolute()
    created_files: list[_CreatedPath] = []
    created_dirs: list[_CreatedPath] = []
    staging_files: list[_CreatedPath] = []
    staging_dirs: list[_CreatedPath] = []
    guards: list[SecureDirectory] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            manifest, entry_map, zip_entries = _validate_zipfile(zf, zip_path)
            if int(manifest["backup_format_version"]) > BACKUP_FORMAT_VERSION:
                raise ValueError("备份包格式版本比当前软件新，拒绝恢复（请升级软件）")
            project_dir_name = _validate_project_dir_name(manifest["project_dir_name"])

            # 从文件系统根开始逐级获取目录句柄；以后所有恢复写入均通过
            # 这些句柄完成，不再使用“检查后再按字符串路径 mkdir/open”的
            # TOCTOU 模式。Windows 目录句柄不共享删除，POSIX 使用 *at +
            # O_NOFOLLOW，目录链被替换时直接失败而不是跟随到外部。
            target_guard, target_created, target_guards = SecureDirectory.open_or_create(
                target_parent
            )
            guards.extend(target_guards)
            for path in target_created:
                _record_secure_created_directory(path, created_dirs)

            pdir = target_parent / project_dir_name
            _assert_safe_path(pdir)
            pdir_stat = _path_stat(pdir)
            if pdir_stat is not None:
                _assert_safe_path(pdir, require_directory=True)
                # 备份恢复是“新建项目”操作；即使已有目录为空也不写入，
                # 避免把用户目录与本次半成品混在一起，且保证失败时无需触碰它。
                raise FileExistsError(f"恢复目标已存在，拒绝覆盖：{pdir}")
            # 在目标父目录内创建本次调用独占的随机 staging 根。所有解压、
            # 哈希、SQLite 检查和打开验证都先在该根内完成，成功后整体改名，
            # 避免用户看到半成品，也把失败清理范围限制为本次新建对象。
            staging_name = f".jiadun_restore_{secrets.token_hex(16)}"
            staging_guard = _secure_child_directory(
                target_guard,
                staging_name,
                created_dirs=created_dirs,
                bucket=staging_dirs,
                require_new=True,
                label="恢复 staging 目录",
            )
            guards.append(staging_guard)
            root = target_parent / staging_name
            stage_dirs: dict[tuple[str, ...], SecureDirectory] = {(): staging_guard}

            for relative_path, info in zip_entries.items():
                entry = entry_map[relative_path]
                parts = tuple(relative_path.split("/"))
                parent_parts = parts[:-1]
                parent_guard = stage_dirs.get(parent_parts)
                if parent_guard is None:
                    for index in range(1, len(parts)):
                        key = parts[:index]
                        if key in stage_dirs:
                            continue
                        parent = stage_dirs[key[:-1]]
                        child = _secure_child_directory(
                            parent,
                            key[-1],
                            created_dirs=created_dirs,
                            bucket=staging_dirs,
                            require_new=True,
                            label="恢复 staging 子目录",
                        )
                        stage_dirs[key] = child
                        guards.append(child)
                    parent_guard = stage_dirs[parent_parts]

                name = parts[-1]
                dest = root.joinpath(*parts)
                _assert_within(root, dest)
                # 目录句柄已锁定到 staging 内的直接父目录；独占创建不会
                # 跟随同名 symlink，也不会覆盖竞争中出现的文件。
                with zf.open(info) as src, parent_guard.create_file(name) as out:
                    created = os.fstat(out.fileno())
                    if not stat.S_ISREG(created.st_mode):
                        raise ValueError(f"恢复目标不是普通文件，拒绝继续：{dest}")
                    created_path = _remember_created_path(
                        dest,
                        created,
                        expected_sha256=entry["sha256"],
                        expected_size=entry["size"],
                    )
                    created_files.append(created_path)
                    staging_files.append(created_path)
                    written = _copy_limited(src, out, entry["size"])
                    if written != entry["size"]:
                        raise ValueError(f"恢复实际大小不符：{relative_path}")
                _assert_secure_file_matches(
                    parent_guard,
                    name,
                    relative_path,
                    entry["size"],
                    entry["sha256"],
                )
        # 恢复后立即可用性验证
        conn = sqlite3.connect(root / _DB_NAME)
        try:
            rep = integrity_check(conn)
            if not rep.ok:
                raise RuntimeError(f"恢复后的数据库完整性检查失败：{rep.integrity}")
        finally:
            conn.close()
        from jiadun.core.models.project import open_project as _open

        _info, open_conn = _open(root)  # 能打开（含迁移路径检查）才算恢复成功
        open_conn.close()
        # 不使用“exists 后 rename”：POSIX 的 rename 可能替换竞争中出现的
        # 空目录。这里通过目标父目录句柄独占创建最终目录，再逐文件以
        # secure create_file 复制已验证 staging 内容；目标在任一步出现都
        # 只能导致拒绝，不能覆盖。
        _assert_safe_path(target_parent, require_directory=True)
        _assert_safe_path(pdir)
        _assert_within(target_parent, pdir)
        final_root_guard = _secure_child_directory(
            target_guard,
            project_dir_name,
            created_dirs=created_dirs,
            require_new=True,
            label="恢复目标在提交前出现",
        )
        guards.append(final_root_guard)
        final_dirs: dict[tuple[str, ...], SecureDirectory] = {(): final_root_guard}

        for relative_path, entry in entry_map.items():
            parts = tuple(relative_path.split("/"))
            parent_parts = parts[:-1]
            source_parent = stage_dirs[parent_parts]
            final_parent = final_dirs.get(parent_parts)
            if final_parent is None:
                for index in range(1, len(parts)):
                    key = parts[:index]
                    if key in final_dirs:
                        continue
                    parent = final_dirs[key[:-1]]
                    child = _secure_child_directory(
                        parent,
                        key[-1],
                        created_dirs=created_dirs,
                        require_new=True,
                        label="恢复最终子目录",
                    )
                    final_dirs[key] = child
                    guards.append(child)
                final_parent = final_dirs[parent_parts]

            name = parts[-1]
            source = root.joinpath(*parts)
            dest = pdir.joinpath(*parts)
            _assert_within(root, source)
            _assert_within(pdir, dest)
            with source_parent.open_file(name) as src, final_parent.create_file(name) as out:
                created = os.fstat(out.fileno())
                if not stat.S_ISREG(created.st_mode):
                    raise ValueError(f"恢复最终目标不是普通文件，拒绝继续：{dest}")
                created_files.append(
                    _remember_created_path(
                        dest,
                        created,
                        expected_sha256=entry["sha256"],
                        expected_size=entry["size"],
                    )
                )
                written = _copy_limited(src, out, entry["size"])
                if written != entry["size"]:
                    raise ValueError(f"恢复最终实际大小不符：{relative_path}")
            _assert_secure_file_matches(
                final_parent,
                name,
                relative_path,
                entry["size"],
                entry["sha256"],
                final=True,
            )

        # staging 只在所有最终文件成功复制后清理；如果外部进程改变了
        # staging 文件，内容/身份校验会让清理保留现场，不误删。
        _close_secure_directories(guards)
        _cleanup_created_paths(staging_files, staging_dirs)
        return pdir.absolute()
    except BaseException:
        _close_secure_directories(guards)
        _cleanup_created_paths(created_files, created_dirs)
        raise


def verify_backup(zip_path: Path) -> dict:
    """只读校验备份包：清单 + 每个文件 SHA-256 + 内嵌库 integrity。"""
    zip_path = Path(zip_path)
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="jiadun_bverify_") as td:
        with zipfile.ZipFile(zip_path) as zf:
            manifest, entry_map, zip_entries = _validate_zipfile(zf, zip_path)
            for relative_path, info in zip_entries.items():
                entry = entry_map[relative_path]
                digest = hashlib.sha256()
                with zf.open(info) as src:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    bad.append(f"{relative_path}: sha256 不符")
        db_extract = Path(td).resolve() / _DB_NAME
        with zipfile.ZipFile(zip_path) as zf:
            _manifest, _entry_map, zip_entries = _validate_zipfile(zf, zip_path)
            db_info = zip_entries[_DB_NAME]
            with zf.open(db_info) as src, open(db_extract, "xb") as out:
                written = _copy_limited(src, out, db_info.file_size)
            if written != db_info.file_size:
                raise ValueError("备份 project.db 实际大小不符")
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
