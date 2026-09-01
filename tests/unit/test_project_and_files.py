"""项目生命周期与工作空间纪律测试。"""
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.db import migrations
from jiadun.core.models import project as project_model
from jiadun.core.models import source_file


def _file_state(path: Path) -> tuple[int, int, str]:
    """独立记录文件大小、mtime 和 SHA-256，用于证明扫描未写源目录。"""
    data = path.read_bytes()
    return path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest()


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

    def test_list_projects_creates_no_sqlite_sidecars(self, ws):
        info = project_model.create_project("严格只读列表", ws)
        pdir = Path(info.workspace_path)
        db = pdir / "project.db"
        before_entries = {p.name for p in pdir.iterdir()}
        before_db = _file_state(db)

        assert [p.name for p in project_model.list_projects()] == ["严格只读列表"]

        assert {p.name for p in pdir.iterdir()} == before_entries
        assert _file_state(db) == before_db
        assert not (pdir / "project.db-wal").exists()
        assert not (pdir / "project.db-shm").exists()

    def test_list_projects_reads_committed_active_wal_without_touching_source(self, ws):
        pdir = ws / "active-wal"
        pdir.mkdir(parents=True)
        db = pdir / "project.db"
        writer = sqlite3.connect(db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                """CREATE TABLE projects(
                   id INTEGER PRIMARY KEY, name TEXT, schema_version INTEGER,
                   workspace_path TEXT, created_at TEXT)"""
            )
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            writer.execute(
                "INSERT INTO projects VALUES (1,'WAL内项目',5,?,'2026')",
                (str(pdir),),
            )
            writer.commit()
            before = {
                p.name: _file_state(p)
                for p in pdir.iterdir()
                if p.is_file()
            }

            assert [p.name for p in project_model.list_projects()] == ["WAL内项目"]
            assert {
                p.name: _file_state(p)
                for p in pdir.iterdir()
                if p.is_file()
            } == before
        finally:
            writer.close()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 目录权限语义测试")
    def test_list_projects_discovers_database_in_readonly_directory(self, ws):
        info = project_model.create_project("只读介质项目", ws)
        pdir = Path(info.workspace_path)
        old_mode = pdir.stat().st_mode
        pdir.chmod(0o555)
        try:
            assert [p.name for p in project_model.list_projects()] == ["只读介质项目"]
            assert not (pdir / "project.db-wal").exists()
            assert not (pdir / "project.db-shm").exists()
        finally:
            pdir.chmod(old_mode)

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

    def test_corrupted_settings_recovers_with_backup(self, ws):
        project_model._SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        project_model._SETTINGS_FILE.write_text("{损坏的 json!!", encoding="utf-8")

        # 损坏设置不得让列表瘫痪：按默认空间恢复，且原文件留档可查
        assert project_model.load_settings() == {}
        assert [p.name for p in project_model.list_projects()] == []

        backups = list(project_model._SETTINGS_FILE.parent.glob("settings.json.corrupt-*"))
        assert len(backups) == 1
        assert "损坏的 json" in backups[0].read_text(encoding="utf-8")

        # 恢复后可正常保存新设置
        project_model.remember_workspace(ws)
        assert project_model.workspace_root() == ws

    def test_settings_holding_non_object_json_is_treated_as_corrupt(self, ws):
        project_model._SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        project_model._SETTINGS_FILE.write_text('["不是对象"]', encoding="utf-8")
        assert project_model.load_settings() == {}
        assert list(project_model._SETTINGS_FILE.parent.glob("settings.json.corrupt-*"))

    def test_symlinked_workspace_root_lists_each_project_once(self, ws, tmp_path):
        project_model.create_project("链接去重项目", ws)
        link_root = tmp_path / "link-root"
        try:
            link_root.symlink_to(ws, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"当前环境无法创建符号链接（Windows 需管理员或开发者模式）：{exc}")
        project_model.remember_workspace(link_root)

        names = [p.name for p in project_model.list_projects()]
        assert names.count("链接去重项目") == 1

    def test_core_create_and_open_persist_workspace_without_ui(self, ws, tmp_path):
        custom = tmp_path / "核心入口自选空间"
        info = project_model.create_project("核心持久化项目", custom)
        # 不经过任何 set_workspace_root/remember_workspace 调用，设置须已落盘
        settings = project_model.load_settings()
        assert str(custom) in settings.get("known_workspaces", [])

        _info2, conn = project_model.open_project(Path(info.workspace_path))
        conn.close()
        settings = project_model.load_settings()
        assert str(custom) in settings.get("known_workspaces", [])

    def test_moved_project_repairs_originals_stored_path(self, ws, tmp_path):
        import shutil

        import openpyxl

        from jiadun.core.models import source_file

        info = project_model.create_project("移动后副本恢复", ws)
        pdir = Path(info.workspace_path)
        _info, conn = project_model.open_project(pdir)
        try:
            src = tmp_path / "mini.xlsx"
            wb = openpyxl.Workbook()
            sheet = wb.active
            sheet.append(["清单编码", "清单名称", "计量单位", "工程量", "综合单价", "合价"])
            sheet.append(["010101001001", "平整场地", "m2", 10, 8.5, 85])
            wb.save(src)
            sf = source_file.import_file(conn, info.project_id, pdir, src)
            old_stored = sf.stored_path
            assert Path(old_stored).is_file()
        finally:
            conn.close()

        moved = tmp_path / "移动后位置"
        shutil.move(str(pdir), str(moved))
        assert not Path(old_stored).exists()

        _info2, conn2 = project_model.open_project(moved)
        try:
            row = conn2.execute(
                "SELECT stored_path FROM source_files WHERE id=?", (sf.file_id,)
            ).fetchone()
            repaired = Path(row["stored_path"])
            assert repaired.is_file(), "打开后 originals 副本路径必须恢复可用"
            assert repaired.parent == moved / "originals"
        finally:
            conn2.close()

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


class TestSchemaCopyMigration:
    """旧版库（schema v3）携带真实数据升级到最新版的副本迁移验证。

    对应 CHANGELOG 0.1.4 的声明：迁移前后记录数与导入副本哈希逐一保持。
    """

    def _build_v3_database(self, db, pdir) -> None:
        from jiadun.core.db import migrations

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            for version, scripts in migrations.MIGRATIONS:
                if version > 3:
                    break
                for sql in scripts:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, "2026-01-01T00:00:00"),
                )
            now = "2026-01-01T00:00:00"
            conn.execute(
                "INSERT INTO projects(id, name, schema_version, workspace_path, created_at)"
                " VALUES (1, '旧版项目', 3, ?, ?)",
                (str(pdir), now),
            )
            fake_hash = "a" * 64
            conn.execute(
                """INSERT INTO source_files(id, project_id, original_path, stored_path,
                   original_name, sha256, size_bytes, file_type, imported_at)
                   VALUES (1, 1, '/somewhere/旧.xlsx', ?, '旧.xlsx', ?, 123, 'xlsx', ?)""",
                (str(pdir / "originals" / (fake_hash + ".xlsx")), fake_hash, now),
            )
            conn.execute(
                """INSERT INTO settlement_periods(id, project_id, period_no, title,
                   source_file_id, direction, contract_party, note)
                   VALUES (1, 1, 1, '旧期次', 1, 'upward', '', NULL)"""
            )
            conn.execute(
                """INSERT INTO line_items(id, period_id, sheet_id, code, name, feature, unit,
                   quantity, unit_price, amount, tax_rate, flags_json)
                   VALUES (1, 1, NULL, '0101', '平整场地', '', 'm2', '10', '8.5', '85', '0.09', '{}')"""
            )
            conn.execute(
                """INSERT INTO item_aliases(id, project_id, canonical_key, alias_text,
                   mapping_basis, confirmed_by, confirmed_at)
                   VALUES (1, 1, 'code:0101', '平整场地', 'manual', 'user', ?)""",
                (now,),
            )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)"
                " ON CONFLICT(version) DO NOTHING",
                (3, now),
            )
            conn.commit()
        finally:
            conn.close()

    def test_v3_database_upgrades_to_latest_with_data_intact(self, tmp_path):
        from jiadun.core.db import migrations

        pdir = tmp_path / "旧版项目"
        (pdir / "originals").mkdir(parents=True)
        db = pdir / "project.db"
        self._build_v3_database(db, pdir)
        copies = pdir / "originals" / ("a" * 64 + ".xlsx")
        copies.write_bytes(b"synthetic bytes")

        final = migrations.migrate(db, pdir / "backups")
        assert final == migrations.LATEST_SCHEMA_VERSION

        info, conn = project_model.open_project(pdir)
        try:
            assert info.schema_version == migrations.LATEST_SCHEMA_VERSION
            counts = {
                "projects": 1, "source_files": 1, "settlement_periods": 1,
                "line_items": 1, "item_aliases": 1,
            }
            for table, expected in counts.items():
                n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                assert n == expected, f"{table} 记录数在迁移后变化"
            row = conn.execute(
                "SELECT stored_path, sha256 FROM source_files WHERE id=1"
            ).fetchone()
            assert row["sha256"] == "a" * 64, "副本哈希必须原样保留"
            assert Path(row["stored_path"]).is_file(), "迁移后副本路径必须仍可用"
            # v5 语义：旧别名迁移到 unknown 方向，须重新确认后才能用于对上/对下
            alias = conn.execute("SELECT direction FROM item_aliases WHERE id=1").fetchone()
            assert alias is not None and alias["direction"] == "unknown"
        finally:
            conn.close()
