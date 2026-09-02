"""项目备份/恢复/完整性检查单测（P3）。"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from jiadun.core import backup_restore as br
from jiadun.core import demo as demo_core
from jiadun.core.models import project as pm


@pytest.fixture()
def demo_project(tmp_path: Path) -> Path:
    info = demo_core.provision_demo_project(tmp_path / "ws")
    return Path(info.workspace_path)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("source_files", "raw_sheets", "raw_cells", "line_items", "evidence")
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables
    }


def test_integrity_check_healthy_and_broken(tmp_path: Path):
    db = tmp_path / "ok.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER PRIMARY KEY, y TEXT)")
    conn.execute("INSERT INTO t(y) VALUES ('值')")
    conn.commit()
    rep = br.integrity_check(conn)
    assert rep.ok and rep.integrity == "ok" and rep.tables >= 1
    # 外键违规检测：孤儿行（pragma off 时插入，foreign_key_check 仍能查出）
    conn.execute("CREATE TABLE p(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE c(pid INTEGER REFERENCES p(id))")
    conn.execute("INSERT INTO c VALUES (999)")  # 孤儿行
    conn.commit()
    rep2 = br.integrity_check(conn)
    assert not rep2.ok and rep2.foreign_key_violations
    conn.close()
    # 损坏库：写垃圾字节
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    bad_conn = sqlite3.connect(bad)
    rep3 = br.integrity_check(bad_conn)
    bad_conn.close()
    assert not rep3.ok


def test_backup_verify_restore_roundtrip(demo_project: Path, tmp_path: Path):
    info, conn = pm.open_project(demo_project)
    before = _counts(conn)
    conn.close()
    assert before["source_files"] > 0

    out = br.backup_project(demo_project, tmp_path)
    assert out.is_file() and out.suffix == ".zip"
    v = br.verify_backup(out)
    assert v["ok"] and v["integrity"] == "ok"
    assert v["entries"] >= 2  # db + originals

    restored = br.restore_project(out, tmp_path / "restore")
    info2, conn2 = pm.open_project(restored)
    after = _counts(conn2)
    conn2.close()
    assert after == before
    # originals 内容逐字节一致
    for src in (demo_project / "originals").iterdir():
        dst = restored / "originals" / src.name
        assert dst.read_bytes() == src.read_bytes()


def test_restore_refuses_overwrite_and_tamper(demo_project: Path, tmp_path: Path):
    out = br.backup_project(demo_project, tmp_path)
    (tmp_path / "restore" / demo_project.name).mkdir(parents=True)
    (tmp_path / "restore" / demo_project.name / "keep.txt").write_text("用户已有内容")
    with pytest.raises(FileExistsError):
        br.restore_project(out, tmp_path / "restore")

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith((".docx", ".xlsx")):
                data += b"tampered"
            zout.writestr(item, data)
    with pytest.raises(ValueError):
        br.restore_project(bad, tmp_path / "restore-bad")
    # 失败后不留半成品
    assert not (tmp_path / "restore-bad" / demo_project.name).exists()


def test_backup_snapshot_with_open_wal_connection(demo_project: Path, tmp_path: Path):
    """连接持有未 checkpoint 的 WAL 数据时备份，快照必须包含已提交数据。"""
    info, conn = pm.open_project(demo_project)
    n_before = conn.execute("SELECT COUNT(*) FROM raw_cells").fetchone()[0]
    out = br.backup_project(demo_project, tmp_path)
    # 连接仍开着 → 备份仍应成功且数据完整
    conn.close()
    with zipfile.ZipFile(out) as zf:
        zf.extract("project.db", tmp_path / "snap")
    snap = sqlite3.connect(tmp_path / "snap" / "project.db")
    n_snap = snap.execute("SELECT COUNT(*) FROM raw_cells").fetchone()[0]
    snap.close()
    assert n_snap == n_before
    assert br.verify_backup(out)["ok"]
