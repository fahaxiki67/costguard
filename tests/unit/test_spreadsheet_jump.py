"""异常定位跳转的单测：链路解析、单元格引用、降级行为（不依赖真实 Office）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from jiadun.platform import spreadsheet_jump as sj


def test_col_letter():
    assert sj._col_letter(1) == "A"
    assert sj._col_letter(26) == "Z"
    assert sj._col_letter(27) == "AA"
    assert sj._col_letter(6) == "F"


def test_cell_ref_format(tmp_path: Path):
    target = sj.JumpTarget(
        file_path=tmp_path / "台账.xlsx", sheet_name="材料调差", row=3, col=6
    )
    assert sj.cell_ref(target) == "台账.xlsx::材料调差!F3"


def test_jump_target_from_message_chain(tmp_path: Path):
    """anomaly.message 的「Sheet『x』单元格(r,c)」+ subject 链 → JumpTarget。"""
    from jiadun.core import demo as demo_core
    from jiadun.core.anomalies import engine as anomaly_engine
    from jiadun.core.models import project as pm

    ws = tmp_path / "ws"
    info = demo_core.provision_demo_project(ws)
    info, conn = pm.open_project(Path(info.workspace_path))
    try:
        anomaly_engine.run_anomalies(conn, info.project_id)
        row = conn.execute(
            "SELECT id FROM anomalies WHERE rule_id='formula_no_cache' LIMIT 1"
        ).fetchone()
        if row is None:  # demo 公式缓存形态变化时跳过
            pytest.skip("demo 数据未产生 formula_no_cache 异常")
        target = sj.jump_target_for_anomaly(conn, row["id"])
        assert target is not None
        assert target.row >= 1 and target.col >= 1
        assert target.sheet_name
        assert target.file_path.exists()
        if target.sha256:
            assert sj._file_sha256(target.file_path) == target.sha256
        # 无定位信息的异常 → None
        plain = conn.execute(
            "SELECT id FROM anomalies WHERE message NOT LIKE '%单元格(%' LIMIT 1"
        ).fetchone()
        if plain is not None:
            assert sj.jump_target_for_anomaly(conn, plain["id"]) is None
    finally:
        conn.close()


def test_open_in_spreadsheet_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "a.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    target = sj.JumpTarget(file_path=f, sheet_name="S", row=1, col=1)
    opened: list[Path] = []

    def fake_opener(p: Path) -> None:
        opened.append(p)

    # 无 COM（空 progids）→ 仅打开文件
    out = sj.open_in_spreadsheet(target, progids=(), opener=fake_opener)
    assert out == "opened_only" and opened == [f]

    # 哈希不符 → 只开文件夹
    target2 = sj.JumpTarget(file_path=f, sheet_name="S", row=1, col=1, sha256="deadbeef")
    out = sj.open_in_spreadsheet(target2, opener=fake_opener)
    assert out == "hash_mismatch" and opened[-1] == f.parent

    # 文件缺失 → FileNotFoundError
    missing = sj.JumpTarget(file_path=tmp_path / "无.xlsx", sheet_name="S", row=1, col=1)
    with pytest.raises(FileNotFoundError):
        sj.open_in_spreadsheet(missing, opener=fake_opener)

    # 非 Windows 平台 → opened_only
    monkeypatch.setattr(sj.os, "name", "linux")
    out = sj.open_in_spreadsheet(target, opener=fake_opener)
    assert out == "opened_only"


def test_located_with_fake_com(tmp_path: Path):
    """注入假 COM：命中 progid 并 Goto 成功 → located。"""
    f = tmp_path / "b.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    target = sj.JumpTarget(file_path=f, sheet_name="S", row=2, col=5)

    calls: list[str] = []

    class FakeCells:
        def __init__(self, row, col):
            self.row, self.col = row, col

    class FakeSheet:
        def __init__(self, name):
            self.name = name

        def Cells(self, row, col):  # noqa: N802
            return FakeCells(row, col)

    class FakeBook:
        def __init__(self, name):
            self.name = name

        def Worksheets(self, name):  # noqa: N802
            return FakeSheet(name)

    class FakeApp:
        def __init__(self, progid):
            calls.append(progid)

        @property
        def Workbooks(self):  # noqa: N802
            return self

        def Open(self, path, update_links, read_only):  # noqa: N802
            assert read_only is True
            return FakeBook(path)

        def Goto(self, cell, scroll):  # noqa: N802
            calls.append(f"Goto:{cell.row},{cell.col}")

    import sys
    import types

    fake = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda progid: FakeApp(progid)
    fake.client = client
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "win32com", fake)
    monkey.setitem(sys.modules, "win32com.client", client)
    monkey.setitem(sys.modules, "pythoncom", pythoncom)
    try:
        out = sj.open_in_spreadsheet(
            target, progids=("Excel.Application",), opener=lambda p: None
        )
    finally:
        monkey.undo()
    assert out == "located"
    assert calls[0] == "Excel.Application"
    assert calls[-1] == "Goto:2,5"
