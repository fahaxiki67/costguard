"""项目备份/恢复/完整性检查单测（P3）。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
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


def _archive_members(path: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(path) as zf:
        return [(copy.copy(info), zf.read(info)) for info in zf.infolist()]


def _write_archive(path: Path, members: list[tuple[zipfile.ZipInfo, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, data in members:
            zf.writestr(info, data)


def _manifest_member(members: list[tuple[zipfile.ZipInfo, bytes]]) -> tuple[int, dict]:
    for index, (info, data) in enumerate(members):
        if info.filename == br._MANIFEST_NAME:
            return index, json.loads(data.decode("utf-8"))
    raise AssertionError("测试备份缺少 manifest")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_manifest_entry(manifest: dict, path: str, data: bytes) -> dict:
    manifest["entries"].append({
        "path": path,
        "sha256": _sha256_bytes(data),
        "size": len(data),
    })
    return manifest


def _set_manifest(members: list[tuple[zipfile.ZipInfo, bytes]], manifest: dict) -> None:
    index, _old = _manifest_member(members)
    info, _data = members[index]
    members[index] = (info, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))


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


def test_backup_restore_handles_chinese_and_space_paths(tmp_path: Path):
    """Windows 常见中文、空格和括号路径只影响测试副本，不改变安全边界。"""
    source_root = tmp_path / "中文 项目资料 (copy)"
    info = demo_core.provision_demo_project(source_root)
    project_dir = Path(info.workspace_path)
    backup_dir = tmp_path / "备份 输出 (独立)"
    restore_parent = tmp_path / "恢复 目录 中文 (isolated)"

    archive = br.backup_project(project_dir, backup_dir)
    restored = br.restore_project(archive, restore_parent)

    assert restored.parent == restore_parent
    assert restored.name == project_dir.name
    assert br.verify_backup(archive)["ok"]
    assert (restored / br._DB_NAME).is_file()


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


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape.txt",
        r"..\escape.txt",
        "/escape.txt",
        r"C:\escape.txt",
        r"\\server\share\escape.txt",
    ],
)
def test_restore_rejects_unsafe_zip_paths_without_creating_target(
    demo_project: Path, tmp_path: Path, unsafe_name: str
):
    """ZIP 内路径必须原样为安全相对路径，不能靠 Path/zipfile 清洗。"""
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    payload = next(data for info, data in members if info.filename == br._DB_NAME)
    extra = zipfile.ZipInfo(unsafe_name)
    extra.create_system = 3
    members.insert(0, (extra, payload[:1]))
    bad = tmp_path / "unsafe.zip"
    _write_archive(bad, members)

    target_parent = tmp_path / "restore-unsafe"
    with pytest.raises(ValueError):
        br.restore_project(bad, target_parent)
    assert not target_parent.exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    "project_dir_name",
    [
        "", ".", "..", "../outside", r"..\outside", r"C:\outside",
        r"\\server\share", "CON", "CON .txt", "COM¹", "LPT³", "name.",
        "name ",
    ],
)
def test_restore_rejects_illegal_project_dir_name(
    demo_project: Path, tmp_path: Path, project_dir_name: str
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    _index, manifest = _manifest_member(members)
    manifest["project_dir_name"] = project_dir_name
    _set_manifest(members, manifest)
    bad = tmp_path / "bad-project-name.zip"
    _write_archive(bad, members)

    target_parent = tmp_path / "restore-project-name"
    with pytest.raises(ValueError):
        br.restore_project(bad, target_parent)
    assert not target_parent.exists()


def test_restore_rejects_duplicate_zip_file_name(demo_project: Path, tmp_path: Path):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    info, data = next((info, data) for info, data in members if info.filename != br._MANIFEST_NAME)
    duplicate = copy.copy(info)
    members.insert(1, (duplicate, data))
    bad = tmp_path / "duplicate-zip-name.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="重复"):
        br.restore_project(bad, tmp_path / "restore-duplicate")


def test_restore_rejects_duplicate_manifest_path(demo_project: Path, tmp_path: Path):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    _index, manifest = _manifest_member(members)
    manifest["entries"].append(copy.deepcopy(manifest["entries"][0]))
    _set_manifest(members, manifest)
    bad = tmp_path / "duplicate-manifest-path.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="重复"):
        br.restore_project(bad, tmp_path / "restore-duplicate-manifest")


def test_restore_rejects_duplicate_manifest_json_key(
    demo_project: Path, tmp_path: Path
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    index, manifest = _manifest_member(members)
    info, data = members[index]
    encoded_name = json.dumps(manifest["project_dir_name"], ensure_ascii=False)
    needle = f'"project_dir_name": {encoded_name}'
    raw = data.decode("utf-8")
    assert needle in raw
    raw = raw.replace(needle, f"{needle}, {needle}", 1)
    members[index] = (info, raw.encode("utf-8"))
    bad = tmp_path / "duplicate-manifest-json-key.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="重复键"):
        br.read_backup_manifest(bad)


def test_restore_rejects_declared_zip_resource_limit(
    demo_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """声明的超大条目必须在解压前拒绝，不能把 ZIP bomb 写入目标。"""
    source = br.backup_project(demo_project, tmp_path / "backups")
    monkeypatch.setattr(br, "_MAX_BACKUP_ENTRY_BYTES", 1)

    target_parent = tmp_path / "restore-resource-limit"
    with pytest.raises(ValueError, match="安全上限"):
        br.restore_project(source, target_parent)
    assert not target_parent.exists()


def test_restore_and_verify_reject_future_backup_version(
    demo_project: Path, tmp_path: Path
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    _index, manifest = _manifest_member(members)
    manifest["backup_format_version"] = br.BACKUP_FORMAT_VERSION + 1
    _set_manifest(members, manifest)
    bad = tmp_path / "future-version.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="格式版本"):
        br.read_backup_manifest(bad)
    with pytest.raises(ValueError, match="格式版本"):
        br.verify_backup(bad)
    with pytest.raises(ValueError, match="格式版本"):
        br.restore_project(bad, tmp_path / "restore-future")


def test_backup_rejects_reserved_root_manifest(
    demo_project: Path, tmp_path: Path
):
    (demo_project / br._MANIFEST_NAME).write_text("用户文件", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest.json"):
        br.backup_project(demo_project, tmp_path / "backups")
    assert not list((tmp_path / "backups").glob("*.zip"))


def test_backup_failure_cleans_only_new_destination_directories(
    demo_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    destination = tmp_path / "new" / "backup-output"

    def fail_after_destination_is_created(_project_dir: Path):
        raise RuntimeError("synthetic source enumeration failure")

    monkeypatch.setattr(br, "_collect_project_files", fail_after_destination_is_created)
    with pytest.raises(RuntimeError, match="source enumeration"):
        br.backup_project(demo_project, destination)

    assert not destination.exists()
    assert not (tmp_path / "new").exists()


def test_backup_rejects_reparse_destination_without_writing_outside(
    demo_project: Path, tmp_path: Path
):
    if not hasattr(os, "symlink"):
        pytest.skip("当前平台不支持 symlink")
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    linked = tmp_path / "linked-backups"
    try:
        os.symlink(outside, linked, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"创建测试 symlink 需要平台权限：{exc}")

    with pytest.raises(ValueError, match="symlink"):
        br.backup_project(demo_project, linked)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("mismatch_kind", ["manifest_extra", "zip_extra", "manifest_missing"])
def test_restore_rejects_manifest_zip_content_mismatch(
    demo_project: Path, tmp_path: Path, mismatch_kind: str
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    _index, manifest = _manifest_member(members)
    if mismatch_kind == "manifest_extra":
        _add_manifest_entry(manifest, "not-in-zip.txt", b"not in zip")
        _set_manifest(members, manifest)
    elif mismatch_kind == "manifest_missing":
        removable = next(entry for entry in manifest["entries"] if entry["path"] != br._DB_NAME)
        manifest["entries"].remove(removable)
        _set_manifest(members, manifest)
    else:
        extra = zipfile.ZipInfo("not-in-manifest.txt")
        extra.create_system = 3
        members.insert(0, (extra, b"not in manifest"))
    bad = tmp_path / f"mismatch-{mismatch_kind}.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="manifest 与 ZIP 内容不一致"):
        br.restore_project(bad, tmp_path / f"restore-{mismatch_kind}")


def test_restore_rejects_zip_symlink_entry(demo_project: Path, tmp_path: Path):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    link = zipfile.ZipInfo("outside-link.txt")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    members.insert(0, (link, b"outside.txt"))
    bad = tmp_path / "zip-symlink.zip"
    _write_archive(bad, members)

    with pytest.raises(ValueError, match="symlink"):
        br.restore_project(bad, tmp_path / "restore-zip-symlink")


def test_restore_rejects_symlink_target_parent_without_touching_outside(
    demo_project: Path, tmp_path: Path
):
    if not hasattr(os, "symlink"):
        pytest.skip("当前平台不支持 symlink")
    source = br.backup_project(demo_project, tmp_path / "backups")
    outside = tmp_path / "outside"
    outside.mkdir()
    target_parent = tmp_path / "restore-link"
    try:
        os.symlink(outside, target_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"创建测试 symlink 需要平台权限：{exc}")

    with pytest.raises(ValueError, match="symlink"):
        br.restore_project(source, target_parent)
    assert not (outside / demo_project.name).exists()


def test_restore_failure_keeps_preexisting_empty_project_directory(
    demo_project: Path, tmp_path: Path
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    members = _archive_members(source)
    for index, (info, data) in enumerate(members):
        if info.filename not in {br._MANIFEST_NAME, br._DB_NAME}:
            altered = bytes([data[0] ^ 0x01]) + data[1:]
            members[index] = (info, altered)
            break
    else:  # pragma: no cover - demo backup always contains an original file
        raise AssertionError("测试备份没有可篡改的 originals 文件")
    bad = tmp_path / "hash-mismatch-after-write.zip"
    _write_archive(bad, members)

    target_parent = tmp_path / "restore-existing-empty"
    pdir = target_parent / demo_project.name
    pdir.mkdir(parents=True)
    with pytest.raises(FileExistsError, match="恢复目标已存在"):
        br.restore_project(bad, target_parent)
    assert pdir.is_dir()
    assert list(pdir.iterdir()) == []


def test_restore_commit_refuses_directory_created_after_initial_check(
    demo_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """最终提交必须独占 mkdir，不能用检查后的 rename 替换竞争目录。"""
    source = br.backup_project(demo_project, tmp_path / "backups")
    target_parent = tmp_path / "restore-commit-race"
    pdir = target_parent / demo_project.name
    original_assert_within = br._assert_within
    injected = {"done": False}

    def create_target_after_check(root: Path, candidate: Path) -> None:
        original_assert_within(root, candidate)
        if root == target_parent and candidate == pdir and not injected["done"]:
            pdir.mkdir(parents=True)
            injected["done"] = True

    monkeypatch.setattr(br, "_assert_within", create_target_after_check)
    with pytest.raises(FileExistsError, match="提交前出现"):
        br.restore_project(source, target_parent)

    assert injected["done"]
    assert pdir.is_dir()
    assert list(pdir.iterdir()) == []


def test_restore_cleanup_does_not_delete_replaced_user_file(
    demo_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    target_parent = tmp_path / "restore-replaced"
    replaced = {"path": None}
    original_digest = br._secure_file_digest

    def replace_then_fail(parent: br.SecureDirectory, name: str) -> tuple[int, str]:
        size, digest = original_digest(parent, name)
        if name != br._DB_NAME and replaced["path"] is None:
            path = parent.path / name
            path.write_bytes(b"X" * size)
            replaced["path"] = path
            return size, "0" * 64
        return size, digest

    monkeypatch.setattr(br, "_secure_file_digest", replace_then_fail)
    with pytest.raises(ValueError, match="恢复校验失败"):
        br.restore_project(source, target_parent)
    assert replaced["path"] is not None
    assert replaced["path"].read_bytes() == b"X" * replaced["path"].stat().st_size


def test_restore_cleanup_keeps_same_inode_user_edit(
    demo_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = br.backup_project(demo_project, tmp_path / "backups")
    target_parent = tmp_path / "restore-same-inode-edit"
    original_digest = br._secure_file_digest
    edited: dict[str, Path | None] = {"path": None}

    def edit_then_fail(parent: br.SecureDirectory, name: str) -> tuple[int, str]:
        size, digest = original_digest(parent, name)
        if name != br._DB_NAME and edited["path"] is None:
            path = parent.path / name
            with path.open("ab") as fh:
                fh.write(b"user edit during failed restore")
            edited["path"] = path
            return size, "0" * 64
        return size, digest

    monkeypatch.setattr(br, "_secure_file_digest", edit_then_fail)
    with pytest.raises(ValueError, match="恢复校验失败"):
        br.restore_project(source, target_parent)

    assert edited["path"] is not None
    assert edited["path"].read_bytes().endswith(b"user edit during failed restore")


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
