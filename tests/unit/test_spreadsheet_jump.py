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
    out = sj.open_in_spreadsheet(target, progids=(), opener=fake_opener, platform="nt")
    assert out == "opened_only" and opened == [f]

    # 哈希不符 → 只开文件夹
    target2 = sj.JumpTarget(file_path=f, sheet_name="S", row=1, col=1, sha256="deadbeef")
    out = sj.open_in_spreadsheet(target2, opener=fake_opener, platform="nt")
    assert out == "hash_mismatch" and opened[-1] == f.parent

    # 文件缺失 → FileNotFoundError
    missing = sj.JumpTarget(file_path=tmp_path / "无.xlsx", sheet_name="S", row=1, col=1)
    with pytest.raises(FileNotFoundError):
        sj.open_in_spreadsheet(missing, opener=fake_opener)

    # 非 Windows 平台 → opened_only
    out = sj.open_in_spreadsheet(target, opener=fake_opener, platform="darwin")
    assert out == "opened_only"


def test_located_with_fake_com(tmp_path: Path):
    """注入假 COM：命中 progid 并 Goto 成功 → located。"""
    f = tmp_path / "b.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    target = sj.JumpTarget(file_path=f, sheet_name="S", row=2, col=5)

    calls: list[str] = []
    quits: list[int] = []

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
            self._jiadun_com_identity = object()

        def Worksheets(self, name):  # noqa: N802
            return FakeSheet(name)

        def Close(self, save):  # noqa: N802
            calls.append(f"Close:{save}")

    class FakeApp:
        def __init__(self, progid):
            calls.append(progid)

        @property
        def Workbooks(self):  # noqa: N802
            return self

        @property
        def Count(self):  # noqa: N802
            return 0

        def Open(self, path, update_links, read_only):  # noqa: N802
            assert read_only is True
            return FakeBook(path)

        def Goto(self, cell, scroll):  # noqa: N802
            calls.append(f"Goto:{cell.row},{cell.col}")

        def Quit(self):  # noqa: N802
            quits.append(1)

    import sys
    import types

    fake = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda progid: FakeApp(progid)
    fake.client = client
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "win32com", fake)
    monkey.setitem(sys.modules, "win32com.client", client)
    monkey.setitem(sys.modules, "pythoncom", pythoncom)
    # 进程快照：调用前就有表格进程（用户已开）→ 绝不允许 Quit
    snapshot = lambda: {"excel.exe": {111}, "et.exe": set(), "wps.exe": set()}  # noqa: E731
    try:
        out = sj.open_in_spreadsheet(
            target, progids=("Excel.Application",), opener=lambda p: None,
            platform="nt", pid_snapshot=snapshot)
    finally:
        monkey.undo()
    assert out == "located"
    assert calls[0] == "Excel.Application"
    assert "Goto:2,5" in calls
    assert "Close:False" in calls  # 自己开的工作簿要关
    assert quits == []             # 用户会话在 → 绝不 Quit


def test_quit_only_when_started_from_nothing(tmp_path: Path):
    """只有明确创建独立实例（DispatchEx）时才允许 Quit。"""
    f = tmp_path / "c.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    target = sj.JumpTarget(file_path=f, sheet_name="S", row=1, col=1)
    quits: list[int] = []
    closes: list[int] = []

    class FakeSheet:
        def Cells(self, row, col):  # noqa: N802
            return (row, col)

    class FakeBook:
        def __init__(self):
            self._jiadun_com_identity = object()

        def Worksheets(self, name):  # noqa: N802
            return FakeSheet()

        def Close(self, save):  # noqa: N802
            closes.append(save)

    class FakeApp:
        @property
        def Workbooks(self):  # noqa: N802
            return self

        @property
        def Count(self):  # noqa: N802
            return 0

        def Open(self, path, u, ro):  # noqa: N802
            return FakeBook()

        def Goto(self, cell, scroll):  # noqa: N802
            pass

        def Quit(self):  # noqa: N802
            quits.append(1)

    import sys
    import types

    fake = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda progid: FakeApp()
    client.DispatchEx = lambda progid: FakeApp()
    fake.client = client
    pythoncom = types.ModuleType("pythoncom")
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "win32com", fake)
    monkey.setitem(sys.modules, "win32com.client", client)
    monkey.setitem(sys.modules, "pythoncom", pythoncom)
    state = {"n": 0}

    def snapshot():
        state["n"] += 1
        return {"excel.exe": set(), "et.exe": {100} if state["n"] >= 2 else set(),
                "wps.exe": set()}

    try:
        out = sj.open_in_spreadsheet(
            target, progids=("Excel.Application",), opener=lambda p: None,
            platform="nt", pid_snapshot=snapshot)
    finally:
        monkey.undo()
    assert out == "located"
    assert closes == [False]
    assert quits == [1]


def test_no_snapshot_never_quits(tmp_path: Path):
    """进程快照不可用（None）→ 保守：即使新启动也绝不 Quit。"""
    f = tmp_path / "d.xlsx"
    f.write_bytes(b"PK\x03\x04fake")
    target = sj.JumpTarget(file_path=f, sheet_name="S", row=1, col=1)
    quits: list[int] = []

    class FakeSheet:
        def Cells(self, row, col):  # noqa: N802
            return (row, col)

    class FakeBook:
        def __init__(self):
            self._jiadun_com_identity = object()

        def Worksheets(self, name):  # noqa: N802
            return FakeSheet()

        def Close(self, save):  # noqa: N802
            pass

    class FakeApp:
        @property
        def Workbooks(self):  # noqa: N802
            return self

        @property
        def Count(self):  # noqa: N802
            return 0

        def Open(self, path, u, ro):  # noqa: N802
            return FakeBook()

        def Goto(self, cell, scroll):  # noqa: N802
            pass

        def Quit(self):  # noqa: N802
            quits.append(1)

    import sys
    import types

    fake = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda progid: FakeApp()
    fake.client = client
    pythoncom = types.ModuleType("pythoncom")
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "win32com", fake)
    monkey.setitem(sys.modules, "win32com.client", client)
    monkey.setitem(sys.modules, "pythoncom", pythoncom)
    try:
        out = sj.open_in_spreadsheet(
            target, progids=("Excel.Application",), opener=lambda p: None,
            platform="nt", pid_snapshot=lambda: None)
    finally:
        monkey.undo()
    assert out == "located"
    assert quits == []


class _WorkbookCollection:
    def __init__(self, books):
        self.books = list(books)

    @property
    def Count(self):  # noqa: N802
        return len(self.books)

    def Item(self, index):  # noqa: N802
        return self.books[index - 1]


class _WorkbookSheet:
    def Cells(self, row, col):  # noqa: N802
        return row, col


class _OwnedBook:
    def __init__(self, path: Path, closes: list[bool], *, fail_sheet: bool = False):
        self.FullName = str(path)
        self.Path = str(path.parent)
        self.Name = path.name
        self._jiadun_com_identity = object()
        self._closes = closes
        self._fail_sheet = fail_sheet

    def Worksheets(self, _name):  # noqa: N802
        if self._fail_sheet:
            raise RuntimeError("sheet access failed")
        return _WorkbookSheet()

    def Close(self, save):  # noqa: N802
        self._closes.append(save)


class _OwnedApp:
    def __init__(self, books, opens, closes, quits, *, fail_sheet: bool = False):
        self.Workbooks = _WorkbookCollection(books)
        self._opens = opens
        self._closes = closes
        self._quits = quits
        self._fail_sheet = fail_sheet
        self.Workbooks.Open = self.Open

    def Open(self, path, _update_links, _read_only):  # noqa: N802
        book = _OwnedBook(Path(path), self._closes, fail_sheet=self._fail_sheet)
        self._opens.append(book)
        return book

    def Goto(self, _cell, _scroll):  # noqa: N802
        return None

    def Quit(self):  # noqa: N802
        self._quits.append(True)


def _install_fake_com(
    monkeypatch: pytest.MonkeyPatch, app, *, active=None, dispatch_ex=False
):
    import sys
    import types

    fake = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda _progid: app
    if dispatch_ex:
        client.DispatchEx = lambda _progid: app
    if active is not None:
        client.GetActiveObject = lambda _progid: active
    else:
        def no_active(_progid):
            raise RuntimeError("no active object")

        client.GetActiveObject = no_active
    fake.client = client
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    monkeypatch.setitem(sys.modules, "win32com", fake)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)


def _user_process_snapshot():
    return {"excel.exe": {111}, "et.exe": set(), "wps.exe": set()}


def test_existing_user_instance_with_target_workbook_is_not_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "用户已打开.xlsx"
    target_file.write_bytes(b"placeholder")
    opens: list[_OwnedBook] = []
    closes: list[bool] = []
    app = _OwnedApp([], opens, closes, [])
    existing = _OwnedBook(target_file, closes)
    app.Workbooks = _WorkbookCollection([existing])
    app.Workbooks.Open = app.Open
    _install_fake_com(monkeypatch, app, active=app)

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=_user_process_snapshot,
    )

    assert out == "located"
    assert opens == []
    assert closes == []
    assert app._quits == []


def test_existing_user_instance_without_target_workbook_closes_only_our_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "用户实例未打开目标.xlsx"
    target_file.write_bytes(b"placeholder")
    opens: list[_OwnedBook] = []
    closes: list[bool] = []
    app = _OwnedApp([], opens, closes, [])
    _install_fake_com(monkeypatch, app, active=app)

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=_user_process_snapshot,
    )

    assert out == "located"
    assert len(opens) == 1
    assert closes == [False]
    assert app._quits == []


def test_new_spreadsheet_instance_closes_book_and_quits_our_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "程序新建实例.xlsx"
    target_file.write_bytes(b"placeholder")
    opens: list[_OwnedBook] = []
    closes: list[bool] = []
    quits: list[bool] = []
    app = _OwnedApp([], opens, closes, quits)
    _install_fake_com(monkeypatch, app, active=None, dispatch_ex=True)
    state = {"calls": 0}

    def snapshot():
        state["calls"] += 1
        return {"excel.exe": set() if state["calls"] == 1 else {222}, "et.exe": set(), "wps.exe": set()}

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=snapshot,
    )

    assert out == "located"
    assert closes == [False]
    assert quits == [True]


def test_dispatch_ex_with_existing_spreadsheet_process_never_quits_user_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "已有进程中的目标.xlsx"
    target_file.write_bytes(b"placeholder")
    closes: list[bool] = []
    quits: list[bool] = []
    app = _OwnedApp([], [], closes, quits)
    _install_fake_com(monkeypatch, app, active=None, dispatch_ex=True)
    state = {"calls": 0}

    def snapshot():
        state["calls"] += 1
        if state["calls"] == 1:
            return {"excel.exe": {111}, "et.exe": set(), "wps.exe": set()}
        return {"excel.exe": {111, 222}, "et.exe": set(), "wps.exe": set()}

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=snapshot,
    )

    assert out == "located"
    assert closes == [False]
    assert quits == []


def test_unidentified_opened_workbook_is_not_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "无法确认所有权.xlsx"
    target_file.write_bytes(b"placeholder")
    closes: list[bool] = []

    class UnidentifiedBook:
        def Worksheets(self, _name):  # noqa: N802
            return _WorkbookSheet()

        def Close(self, save):  # noqa: N802
            closes.append(save)

    class AppWithUnidentifiedOpen(_OwnedApp):
        def Open(self, _path, _update_links, _read_only):  # noqa: N802
            return UnidentifiedBook()

    app = AppWithUnidentifiedOpen([], [], closes, [])
    _install_fake_com(monkeypatch, app, active=app)

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=_user_process_snapshot,
    )

    assert out == "located"
    assert closes == []


def test_unknown_workbook_ownership_never_closes_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target_file = tmp_path / "所有权未知.xlsx"
    target_file.write_bytes(b"placeholder")
    closes: list[bool] = []
    quits: list[bool] = []

    class UninspectableApp(_OwnedApp):
        def __init__(self):
            super().__init__([], [], closes, quits, fail_sheet=True)
            self.Workbooks = self

        def Open(self, path, _update_links, _read_only):  # noqa: N802
            return _OwnedBook(Path(path), closes, fail_sheet=True)

        def __iter__(self):
            raise RuntimeError("workbook collection unavailable")

    app = UninspectableApp()
    _install_fake_com(monkeypatch, app, active=app)

    out = sj.open_in_spreadsheet(
        sj.JumpTarget(target_file, "S", 1, 1),
        progids=("Excel.Application",),
        opener=lambda _path: None,
        platform="nt",
        pid_snapshot=_user_process_snapshot,
    )

    assert out == "jump_failed"
    assert closes == []
    assert quits == []


def test_decide_started_here_table():
    empty = {"excel.exe": set(), "et.exe": set(), "wps.exe": set()}
    occupied = {"excel.exe": {1}, "et.exe": set(), "wps.exe": set()}
    started = {"excel.exe": {2}, "et.exe": set(), "wps.exe": set()}
    # 无人使用 + 出现新进程 → 是我们启动的
    assert sj._decide_started_here(empty, started, attached_ok=False) is True
    # 无人使用但附着成功 → 不 Quit（附着失败才会 Dispatch）
    assert sj._decide_started_here(empty, started, attached_ok=True) is False
    # 已有用户进程 → 永不 Quit
    assert sj._decide_started_here(occupied, occupied, attached_ok=False) is False
    # 快照不可用 → 永不 Quit
    assert sj._decide_started_here(None, None, attached_ok=False) is False
