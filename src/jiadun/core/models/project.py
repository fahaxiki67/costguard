"""项目生命周期：创建/打开/列出。

工作空间目录（ADR-003）：
<workspace>/<project_name>/
├── project.db  ├── originals/  ├── exports/  └── backups/
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jiadun.core.db import migrations
from jiadun.platform import paths as platform_paths

_SETTINGS_FILE = platform_paths.settings_file()


class ProjectError(Exception):
    pass


@dataclass(frozen=True)
class ProjectInfo:
    project_id: int
    name: str
    workspace_path: str
    schema_version: int
    created_at: str

    @property
    def path(self) -> Path:
        return Path(self.workspace_path)


def load_settings() -> dict:
    """读取当前设置，缺少时只读回退到旧版 CostGuard 设置。"""
    candidates = [_SETTINGS_FILE]
    legacy_settings = platform_paths.legacy_settings_file()
    if legacy_settings != _SETTINGS_FILE:
        candidates.append(legacy_settings)

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 当前设置损坏时可归档以便恢复；legacy 文件严格只读，不能因发现
            # 新版本而改名、覆盖或删除旧设置。
            if path == _SETTINGS_FILE:
                _archive_corrupt_settings(path)
            # 当前文件损坏不应遮蔽仍然可用的 legacy 设置；继续检查候选。
            continue
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（如被写成数组/字符串）同样按损坏处理
            if path == _SETTINGS_FILE:
                _archive_corrupt_settings(path)
            continue
        return data
    return {}


def _archive_corrupt_settings(path: Path) -> None:
    """损坏的设置不静默丢弃：改名留档供用户检查，程序按默认状态恢复。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path.rename(path.with_name(f"{path.name}.corrupt-{stamp}"))
    except OSError:
        pass  # 留档失败也不得阻断启动；下次保存会用原子写覆盖


def _legacy_settings_are_active() -> bool:
    """判断当前是否正在使用 legacy 设置作为只读发现来源。"""
    return not _SETTINGS_FILE.exists() and platform_paths.legacy_settings_file().is_file()


def save_settings(settings: dict) -> None:
    """原子写入全局设置，避免应用中断留下半截 JSON。"""
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_FILE.with_name(f".{_SETTINGS_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, _SETTINGS_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()


def _path_key(path: Path) -> str:
    """用于跨设置项/扫描去重；realpath 归一，符号链接根目录与真实目录同键。

    realpath 对不存在的路径也尽力解析（逐级消解符号链接），因此去重不要求
    目录已经存在。
    """
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def workspace_roots(*, include_known: bool = True, include_legacy: bool = True) -> list[Path]:
    """返回需要扫描的全部工作空间，首项是当前默认空间。

    旧版本只保存一个 ``workspace_root``，并且 UI 创建/打开自定义项目时没有
    调用保存函数。新格式保留当前空间及历史空间；默认目录始终参与扫描，避免
    用户切换工作空间后另一批项目从列表中消失。
    """
    settings = load_settings()
    configured = settings.get("workspace_root")
    known = settings.get("known_workspaces", [])
    if not isinstance(known, list):
        known = []

    default_root = platform_paths.default_workspace_root()
    configured_root = Path(configured) if isinstance(configured, str) and configured else None
    # ``workspace_root`` historically也可能由 core.create_project 的隐式登记
    # 写入。启动页的窄范围扫描只采用用户明确选择/确认过的根目录；完整兼容
    # 扫描（include_known=True）仍保留旧值，用户可通过“打开已有项目”恢复。
    configured_explicit = settings.get("workspace_root_explicit") is True
    known_roots = [Path(item) for item in known if isinstance(item, str) and item]
    if _legacy_settings_are_active():
        # legacy: 旧设置仅用于发现历史项目；新建项目默认落在 JiadunProjects，
        # 防止首次启动时把新数据写入旧 CostGuard 空间。
        candidates = [default_root]
        if configured_explicit:
            candidates.insert(0, configured_root)
        if include_known:
            candidates.extend(known_roots)
    else:
        candidates = [default_root]
        if configured_explicit:
            candidates.insert(0, configured_root)
        elif include_known:
            # 旧设置兼容：完整历史扫描仍可看到原配置空间；启动窄扫描不自动带入。
            candidates.insert(0, configured_root)
        if include_known:
            candidates.extend(known_roots)
    # legacy: 旧 CostGuardProjects 只读发现；创建和设置始终写入用户选定的
    # 新工作空间，不自动搬迁旧项目。
    legacy_root = platform_paths.legacy_workspace_root()
    if include_legacy and legacy_root.is_dir():
        candidates.append(legacy_root)
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        root = candidate.expanduser()
        key = _path_key(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def workspace_root() -> Path:
    return workspace_roots(include_known=True, include_legacy=True)[0]


def remember_workspace(
    path: Path,
    *,
    make_default: bool = False,
    set_default_if_missing: bool = True,
    explicit: bool = False,
) -> None:
    """持久登记用户选择的工作空间，不移动也不修改其中的工程数据。"""
    root = Path(path).expanduser()
    settings = load_settings()
    known = settings.get("known_workspaces", [])
    if not isinstance(known, list):
        known = []

    values = [str(root), *(item for item in known if isinstance(item, str) and item)]
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _path_key(Path(value))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)

    settings["known_workspaces"] = deduped
    if make_default or (set_default_if_missing and not settings.get("workspace_root")):
        settings["workspace_root"] = str(root)
    if make_default or explicit:
        settings["workspace_root_explicit"] = True
    save_settings(settings)


def set_workspace_root(path: Path) -> None:
    remember_workspace(path, make_default=True, explicit=True)


def _project_dir(root: Path, name: str) -> Path:
    safe = name.strip()
    if not safe:
        raise ProjectError("project name is empty")
    for ch in r'\/:*?"<>|':
        safe = safe.replace(ch, "_")
    return root / safe


def _query_project_row(conn_uri: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(conn_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, name, schema_version, workspace_path, created_at "
            "FROM projects ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def _read_project_info_via_copy(project_dir: Path) -> ProjectInfo | None:
    """库有活动 WAL 时：把 project.db 与 -wal 复制到临时目录后读副本。

    已提交的 WAL 数据在副本上回放可见；源目录零写入、字节与 mtime 不变。
    复制撞上写入瞬间可能得到撕裂副本——此时打开报错并按"本次不可见"处理，
    项目列表是提示性扫描，真正打开仍走 open_project 的完整纪律。
    """
    import shutil
    import tempfile

    db = project_dir / "project.db"
    with tempfile.TemporaryDirectory(prefix="cg-probe-") as td:
        tmp_db = Path(td) / "project.db"
        shutil.copy2(db, tmp_db)
        wal = db.with_name(db.name + "-wal")
        if wal.is_file():
            shutil.copy2(wal, tmp_db.with_name(tmp_db.name + "-wal"))
        row = _query_project_row(tmp_db.resolve().as_uri())
    if not row:
        return None
    return ProjectInfo(
        row["id"], row["name"], str(project_dir), row["schema_version"], row["created_at"]
    )


def _read_project_info(project_dir: Path) -> ProjectInfo | None:
    """只读获取项目概要；列表刷新不得触发迁移或写入用户数据库。

    - 无活动 WAL（正常关闭后 -wal 不存在或为空）：``immutable=1`` 打开——
      SQLite 保证零写入、不创建 -shm/-wal 边车，只读目录（如刻录介质）可用；
    - 有活动 WAL（应用正在运行或异常退出未检查点）：读临时副本（见上）。
    """
    db = project_dir / "project.db"
    if not db.is_file():
        return None
    wal = db.with_name(db.name + "-wal")
    try:
        if wal.is_file() and wal.stat().st_size > 0:
            return _read_project_info_via_copy(project_dir)
        row = _query_project_row(f"{db.resolve().as_uri()}?immutable=1&mode=ro")
    except (OSError, sqlite3.Error):
        return None
    if not row:
        return None
    # 数据库内的 workspace_path 可能因项目目录被移动而过期；实际打开路径以本次
    # 扫描到的目录为准，数据库内容保持原样，待用户打开时再按正常迁移纪律处理。
    return ProjectInfo(
        row["id"],
        row["name"],
        str(project_dir),
        row["schema_version"],
        row["created_at"],
    )


def list_projects(
    *, include_known: bool = True, include_legacy: bool = True
) -> list[ProjectInfo]:
    """扫描指定范围内的有效项目。

    默认兼容旧版行为，扫描默认、已配置、已登记和旧版工作空间。UI 启动
    页可传 ``include_known=False, include_legacy=False``，只显示当前默认/
    已配置工作空间，避免把历史临时目录隐式带入首页。
    """
    result: list[ProjectInfo] = []
    seen: set[str] = set()
    for root in workspace_roots(
        include_known=include_known, include_legacy=include_legacy
    ):
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            db = child / "project.db"
            if not child.is_dir() or not db.is_file():
                continue
            key = _path_key(db)
            if key in seen:
                continue
            info = _read_project_info(child)
            if info is not None:
                seen.add(key)
                result.append(info)
    return result


def _remember_workspace_quietly(path: Path) -> None:
    """核心入口的持久登记：配置不可写时不阻断项目操作（UI 层另有提示）。"""
    try:
        # core 导入/测试可能使用临时目录；只登记为可供显式恢复的历史空间，
        # 不把它悄悄设为启动页当前空间。
        remember_workspace(path, set_default_if_missing=False)
    except OSError:
        pass


def create_project(name: str, workspace: Path | None = None) -> ProjectInfo:
    """创建新项目：目录 + 空库 + 迁移到最新版本。不覆盖已存在项目。

    无论从 UI、脚本还是验收 runner 调用，工作空间都会持久登记——否则自定义
    空间里的项目会在应用重启后从列表消失（v0.1.2 回归缺陷）。
    """
    root = Path(workspace) if workspace else workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    pdir = _project_dir(root, name)
    if pdir.exists() and any(pdir.iterdir()):
        raise ProjectError(f"project directory already exists and is not empty: {pdir}")
    for sub in ("originals", "exports", "backups"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    db = pdir / "project.db"
    migrations.migrate(db, pdir / "backups")
    conn = migrations.connect(db)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with conn:
            conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES (?,?,?,?)",
                (name.strip(), migrations.LATEST_SCHEMA_VERSION, str(pdir), now),
            )
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    _remember_workspace_quietly(root)
    return ProjectInfo(pid, name.strip(), str(pdir), migrations.LATEST_SCHEMA_VERSION, now)


def _repair_source_paths(conn: sqlite3.Connection, pdir: Path) -> None:
    """项目目录被移动后，把 source_files 里的 originals 绝对路径恢复到当前位置。

    只在用户显式打开项目时执行（打开本身即写库）。副本文件名是 sha256+后缀，
    全局唯一，按文件名在新 originals/ 下精确回填；originals 内容零触碰，
    original_path（用户原件的历史记录）保持原样。
    """
    originals = pdir / "originals"
    if not originals.is_dir():
        return
    rows = conn.execute("SELECT id, stored_path FROM source_files").fetchall()
    for row in rows:
        stored = Path(row["stored_path"])
        if stored.is_file():
            continue
        candidate = originals / stored.name
        if candidate.is_file():
            with conn:
                conn.execute(
                    "UPDATE source_files SET stored_path=? WHERE id=?",
                    (str(candidate), row["id"]),
                )


def open_project(pdir: Path) -> tuple[ProjectInfo, sqlite3.Connection]:
    """打开既有项目，必要时执行迁移。返回 (info, conn)；conn 由调用方关闭。"""
    pdir = Path(pdir)
    db = pdir / "project.db"
    if not db.exists():
        raise ProjectError(f"not a Jiadun project (missing project.db): {pdir}")
    migrations.migrate(db, pdir / "backups")
    conn = migrations.connect(db)
    row = conn.execute(
        "SELECT id, name, schema_version, workspace_path, created_at FROM projects ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        raise ProjectError(f"project.db has no project record: {pdir}")
    _repair_source_paths(conn, pdir)
    _remember_workspace_quietly(pdir.parent)
    return (
        ProjectInfo(row["id"], row["name"], str(pdir), row["schema_version"], row["created_at"]),
        conn,
    )
