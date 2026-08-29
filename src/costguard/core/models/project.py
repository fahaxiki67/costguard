"""项目生命周期：创建/打开/列出。

工作空间目录（ADR-003）：
<workspace>/<project_name>/
├── project.db  ├── originals/  ├── exports/  └── backups/
"""
from __future__ import annotations

import json
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
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def workspace_root() -> Path:
    root = load_settings().get("workspace_root")
    return Path(root) if root else platform_paths.default_workspace_root()


def set_workspace_root(path: Path) -> None:
    settings = load_settings()
    settings["workspace_root"] = str(path)
    save_settings(settings)


def _project_dir(root: Path, name: str) -> Path:
    safe = name.strip()
    if not safe:
        raise ProjectError("project name is empty")
    for ch in r'\/:*?"<>|':
        safe = safe.replace(ch, "_")
    return root / safe


def list_projects() -> list[ProjectInfo]:
    """扫描工作空间根目录下所有有效项目。"""
    root = workspace_root()
    result: list[ProjectInfo] = []
    if not root.exists():
        return result
    for child in sorted(root.iterdir()):
        db = child / "project.db"
        if not child.is_dir() or not db.exists():
            continue
        try:
            conn = migrations.connect(db)
            row = conn.execute(
                "SELECT id, name, schema_version, workspace_path, created_at FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            continue
        if row:
            result.append(
                ProjectInfo(row["id"], row["name"], row["workspace_path"], row["schema_version"], row["created_at"])
            )
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
        ProjectInfo(row["id"], row["name"], row["workspace_path"], row["schema_version"], row["created_at"]),
        conn,
    )
