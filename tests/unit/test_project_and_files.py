"""项目生命周期与工作空间纪律测试。"""
import os
import sqlite3
from pathlib import Path

import pytest

from costguard.core.db import migrations
from costguard.core.models import project as project_model
from costguard.core.models import source_file


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        project_model.platform_paths,
        "default_workspace_root",
        lambda: tmp_path / "ws",
    )
    return tmp_path / "ws"


class TestProject:
    def test_create_creates_layout(self, ws):
        info = project_model.create_project("测试项目A", ws)
        pdir = Path(info.workspace_path)
        assert (pdir / "project.db").exists()
        for sub in ("originals", "exports", "backups"):
            assert (pdir / sub).is_dir()
        assert info.schema_version == migrations.LATEST_SCHEMA_VERSION

    def test_create_refuses_nonempty_dir(self, ws):
        pdir = ws / "已有目录"
        pdir.mkdir(parents=True)
        (pdir / "x.txt").write_text("user data")
        with pytest.raises(project_model.ProjectError):
            project_model.create_project("已有目录", ws)
        assert (pdir / "x.txt").exists()  # 用户数据未动

    def test_name_sanitized(self, ws):
        info = project_model.create_project('a/b\\c:d*e?"<>|', ws)
        assert "/" not in Path(info.workspace_path).name

    def test_list_projects(self, ws):
        project_model.create_project("P1", ws)
        project_model.create_project("P2", ws)
        names = {p.name for p in project_model.list_projects()}
        assert names == {"P1", "P2"}

    def test_custom_workspace_persists_and_default_stays_visible(self, ws, tmp_path):
        project_model.create_project("默认空间项目", ws)
        custom = tmp_path / "用户自选空间"
        project_model.create_project("自选空间项目", custom)
        project_model.set_workspace_root(custom)

        # 每次调用都从磁盘重新读取 settings.json，等价于应用重启后的发现路径。
        assert project_model.workspace_root() == custom
        assert project_model.workspace_roots() == [custom, ws]
        assert {p.name for p in project_model.list_projects()} == {
            "默认空间项目",
            "自选空间项目",
        }

    def test_remember_workspace_is_idempotent(self, ws, tmp_path):
        custom = tmp_path / "custom"
        project_model.remember_workspace(custom)
        project_model.remember_workspace(custom)
        project_model.remember_workspace(ws)
        project_model.remember_workspace(custom, make_default=True)
        settings = project_model.load_settings()
        assert settings["workspace_root"] == str(custom)
        assert settings["known_workspaces"] == [str(custom), str(ws)]

    def test_list_uses_actual_path_after_project_directory_is_moved(self, ws, tmp_path):
        created = project_model.create_project("可移动项目", ws)
        moved_root = tmp_path / "moved"
        moved_root.mkdir()
        moved = moved_root / Path(created.workspace_path).name
        Path(created.workspace_path).rename(moved)
        project_model.set_workspace_root(moved_root)

        [listed] = [p for p in project_model.list_projects() if p.name == "可移动项目"]
        assert listed.workspace_path == str(moved)
        reopened, conn = project_model.open_project(moved)
        try:
            assert reopened.workspace_path == str(moved)
        finally:
            conn.close()

    def test_open_and_reopen(self, ws):
        created = project_model.create_project("重开项目", ws)
        info, conn = project_model.open_project(Path(created.workspace_path))
        try:
            assert info.name == "重开项目"
            assert info.project_id == created.project_id
        finally:
            conn.close()

    def test_open_rejects_non_project(self, ws):
        (ws / "notaproj").mkdir(parents=True)
        with pytest.raises(project_model.ProjectError):
            project_model.open_project(ws / "notaproj")


class TestSourceFileImport:
    @pytest.fixture()
    def proj(self, ws):
        return project_model.create_project("导入项目", ws)

    def test_import_copy_readonly_original_untouched(self, proj, tmp_path):
        src = tmp_path / "结算第1期.xlsx"
        src.write_bytes(b"original-bytes-01")
        before = src.read_bytes(), src.stat().st_mtime_ns
        info, conn = project_model.open_project(Path(proj.workspace_path))
        try:
            sf = source_file.import_file(conn, proj.project_id, Path(proj.workspace_path), src)
            stored = Path(sf.stored_path)
            assert stored.exists()
            assert stored.read_bytes() == b"original-bytes-01"
            # 副本只读
            assert not (os.access(stored, os.W_OK) and os.name != "nt"), "stored copy must be read-only"
            # 原文件字节与 mtime 未变
            assert (src.read_bytes(), src.stat().st_mtime_ns) == before
            assert sf.sha256 and len(sf.sha256) == 64
        finally:
            conn.close()

    def test_duplicate_import_reuses_copy(self, proj, tmp_path):
        src = tmp_path / "same.xlsx"
        src.write_bytes(b"dup-content")
        info, conn = project_model.open_project(Path(proj.workspace_path))
        try:
            a = source_file.import_file(conn, proj.project_id, Path(proj.workspace_path), src)
            b = source_file.import_file(conn, proj.project_id, Path(proj.workspace_path), src)
            assert a.file_id == b.file_id
            assert len(list(source_file.list_files(conn, proj.project_id))) == 1
        finally:
            conn.close()

    def test_rejects_unsupported_type(self, proj, tmp_path):
        src = tmp_path / "evil.exe"
        src.write_bytes(b"MZ")
        info, conn = project_model.open_project(Path(proj.workspace_path))
        try:
            with pytest.raises(source_file.SourceFileError):
                source_file.import_file(conn, proj.project_id, Path(proj.workspace_path), src)
        finally:
            conn.close()

    def test_rejects_missing_file(self, proj, tmp_path):
        info, conn = project_model.open_project(Path(proj.workspace_path))
        try:
            with pytest.raises(source_file.SourceFileError):
                source_file.import_file(conn, proj.project_id, Path(proj.workspace_path), tmp_path / "nope.xlsx")
        finally:
            conn.close()

    def test_foreign_keys_enforced(self, proj):
        conn = migrations.connect(Path(proj.workspace_path) / "project.db")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO evidence(project_id, kind, summary, created_at) VALUES (999,'k','s','t')")
        finally:
            conn.close()
