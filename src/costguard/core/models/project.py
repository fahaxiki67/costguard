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

from costguard.core.db import migrations
from costguard.platform import paths as platform_paths

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
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


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
    """用于跨设置项去重；不要求目录已经存在。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def workspace_roots() -> list[Path]:
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

    candidates = [
        Path(configured) if isinstance(configured, str) and configured else None,
        platform_paths.default_workspace_root(),
        *(Path(item) for item in known if isinstance(item, str) and item),
    ]
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
    return workspace_roots()[0]


def remember_workspace(path: Path, *, make_default: bool = False) -> None:
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
    if make_default or not settings.get("workspace_root"):
        settings["workspace_root"] = str(root)
    save_settings(settings)


def set_workspace_root(path: Path) -> None:
    remember_workspace(path, make_default=True)


def _project_dir(root: Path, name: str) -> Path:
    safe = name.strip()
    if not safe:
        raise ProjectError("project name is empty")
    for ch in r'\/:*?"<>|':
        safe = safe.replace(ch, "_")
    return root / safe


def _read_project_info(project_dir: Path) -> ProjectInfo | None:
    """只读获取项目概要；列表刷新不得触发迁移或写入用户数据库。"""
    db = project_dir / "project.db"
    try:
        uri = f"{db.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, name, schema_version, workspace_path, created_at "
                "FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
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


def list_projects() -> list[ProjectInfo]:
    """扫描默认及已登记工作空间下的全部有效项目。"""
    result: list[ProjectInfo] = []
    seen: set[str] = set()
    for root in workspace_roots():
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


def create_project(name: str, workspace: Path | None = None) -> ProjectInfo:
    """创建新项目：目录 + 空库 + 迁移到最新版本。不覆盖已存在项目。"""
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
    return ProjectInfo(pid, name.strip(), str(pdir), migrations.LATEST_SCHEMA_VERSION, now)


def open_project(pdir: Path) -> tuple[ProjectInfo, sqlite3.Connection]:
    """打开既有项目，必要时执行迁移。返回 (info, conn)；conn 由调用方关闭。"""
    db = Path(pdir) / "project.db"
    if not db.exists():
        raise ProjectError(f"not a CostGuard project (missing project.db): {pdir}")
    migrations.migrate(db, Path(pdir) / "backups")
    conn = migrations.connect(db)
    row = conn.execute(
        "SELECT id, name, schema_version, workspace_path, created_at FROM projects ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        raise ProjectError(f"project.db has no project record: {pdir}")
    return (
        ProjectInfo(row["id"], row["name"], str(Path(pdir)), row["schema_version"], row["created_at"]),
        conn,
    )
