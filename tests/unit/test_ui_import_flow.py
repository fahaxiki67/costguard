"""实际使用入口回归：空白首页、资料文件/文件夹导入、拖拽与字体。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


def test_scan_import_paths_recurses_and_deduplicates_supported_files(tmp_path: Path):
    from jiadun.ui.file_selection import scan_import_paths

    folder = tmp_path / "资料"
    nested = folder / "子目录"
    nested.mkdir(parents=True)
    xlsx = folder / "第1期.xlsx"
    docx = nested / "合同.docx"
    ignored = nested / "说明.jpg"
    temporary = nested / "~$打开中的表.xlsx"
    for path in (xlsx, docx, ignored, temporary):
        path.write_bytes(b"x")

    selection = scan_import_paths([folder, xlsx])

    assert [path.name for path in selection.files] == ["合同.docx", "第1期.xlsx"]
    assert [path.name for path in selection.skipped] == ["说明.jpg"]


def test_scan_import_paths_rejects_symlink_roots_files_and_broken_links(
    tmp_path: Path,
):
    from jiadun.ui.file_selection import scan_import_paths

    root = tmp_path / "资料"
    nested = root / "子目录"
    outside = tmp_path / "目录外"
    nested.mkdir(parents=True)
    outside.mkdir()
    inside = nested / "内部.xlsx"
    inside.write_bytes(b"xlsx")
    outside_file = outside / "目录外.xlsx"
    outside_file.write_bytes(b"xlsx")
    file_link = root / "文件链接.xlsx"
    dir_link = nested / "目录链接"
    root_link = tmp_path / "资料目录链接"
    broken = root / "断开链接.xlsx"
    try:
        file_link.symlink_to(outside_file)
        dir_link.symlink_to(outside, target_is_directory=True)
        root_link.symlink_to(root, target_is_directory=True)
        broken.symlink_to(tmp_path / "不存在.xlsx")
    except OSError as exc:
        pytest.skip(f"当前环境无法创建符号链接（Windows 需管理员或开发者模式）：{exc}")

    selection = scan_import_paths([root, root_link, file_link, broken])

    assert selection.files == (inside,)
    skipped = {path: reason for path, reason in selection.skipped_reasons}
    assert {file_link, dir_link, root_link, broken} <= set(skipped)
    assert all("符号链接" in skipped[path] for path in (file_link, dir_link, root_link, broken))


def test_scan_import_paths_rejects_empty_paths_without_scanning_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from jiadun.ui.file_selection import scan_import_paths

    monkeypatch.chdir(tmp_path)
    unexpected = tmp_path / "不应被扫描.xlsx"
    unexpected.write_bytes(b"xlsx")
    selected = tmp_path / "明确选择.xlsx"
    selected.write_bytes(b"xlsx")

    empty = scan_import_paths(["", "   "])
    mixed = scan_import_paths([selected, ""])

    assert empty.files == ()
    assert {reason for _path, reason in empty.skipped_reasons} == {"空路径未导入"}
    assert mixed.files == (selected,)
    assert unexpected not in mixed.files


def test_drop_zone_ignores_empty_local_file_url():
    from PySide6.QtCore import QMimeData, QUrl

    from jiadun.ui.file_selection import FileDropZone

    mime = QMimeData()
    mime.setUrls([QUrl("file://")])

    assert FileDropZone.paths_from_mime(mime) == []


def test_workbench_parse_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from jiadun.core.models import project as project_model
    from jiadun.ui.workbench import WorkbenchPage

    QApplication.instance() or QApplication([])
    info = project_model.create_project("损坏文件导入", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    bad_file = tmp_path / "损坏结算表.xlsx"
    bad_file.write_bytes(b"not-a-zip-workbook")
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text, *_args, **_kwargs: messages.append(text),
    )

    page = WorkbenchPage(conn, info, info.workspace_path, on_back=lambda: None)
    try:
        page.import_paths([bad_file])
        assert messages
        assert "失败 1 个文件" in messages[-1]
        assert "成功导入 1 个文件" not in messages[-1]
        assert "损坏结算表.xlsx" in messages[-1]
    finally:
        conn.close()
        page.deleteLater()


def test_project_card_summary_failure_falls_back_to_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from jiadun.core.models import project as project_model
    from jiadun.ui import main_window

    info = project_model.create_project("摘要异常项目", tmp_path / "ws")

    def raise_summary(*_args, **_kwargs):
        raise RuntimeError("summary probe failed")

    monkeypatch.setattr(main_window, "build_project_summary", raise_summary)

    snapshot = main_window.MainWindow._project_snapshot(info)

    assert snapshot["project_status"] == "不可形成项目结论"
    assert snapshot["latest"] == "不可形成项目结论"


def test_main_window_initial_view_does_not_scan_implicit_known_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from PySide6.QtWidgets import QApplication

    from jiadun.core.models import project as project_model
    from jiadun.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    default_root = tmp_path / "JiadunProjects"
    legacy_root = tmp_path / "CostGuardProjects"
    remembered = tmp_path / "pytest-temporary-workspace"
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        project_model.platform_paths, "legacy_settings_file", lambda: tmp_path / "legacy-settings.json"
    )
    monkeypatch.setattr(project_model.platform_paths, "default_workspace_root", lambda: default_root)
    monkeypatch.setattr(project_model.platform_paths, "legacy_workspace_root", lambda: legacy_root)
    project_model.create_project("不应自动出现", remembered)

    win = MainWindow()
    try:
        win.show()
        app.processEvents()
        assert win.project_list.count() == 0
        assert win.empty_drop_zone.isVisible()
    finally:
        win.close()


def test_main_window_exposes_source_file_folder_and_drop_actions():
    from PySide6.QtWidgets import QApplication, QPushButton

    from jiadun.ui.file_selection import FileDropZone
    from jiadun.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    try:
        buttons = {button.text() for button in win.findChildren(QPushButton)}
        assert "导入资料文件…" in buttons
        assert "导入资料文件夹…" in buttons
        assert win.findChild(FileDropZone) is not None
    finally:
        win.close()


def test_theme_sets_one_platform_available_application_font():
    from PySide6.QtWidgets import QApplication

    from jiadun.ui import theme

    app = QApplication.instance() or QApplication([])
    family = theme.preferred_font_family()
    theme.apply_theme(app)

    assert family
    assert app.font().family() == family


def test_workbench_import_paths_accepts_folder_and_routes_settlement_and_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import openpyxl
    from PySide6.QtWidgets import QApplication, QMessageBox

    from jiadun.core.models import project as project_model
    from jiadun.ui.workbench import WorkbenchPage

    QApplication.instance() or QApplication([])
    info = project_model.create_project("资料导入路由", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    folder = tmp_path / "待导入资料"
    folder.mkdir()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["清单编码", "清单名称", "计量单位", "工程量", "综合单价", "合价"])
    sheet.append(["0101", "平整场地", "m2", 2, 3, 6])
    book.save(folder / "第1期.xlsx")
    (folder / "合同.txt").write_text("合同金额：1000元\n工期：30天", encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

    page = WorkbenchPage(conn, info, info.workspace_path, on_back=lambda: None)
    try:
        page.import_paths([folder])
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM source_files WHERE project_id=?", (info.project_id,)
        ).fetchone()["n"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM contract_docs WHERE project_id=?", (info.project_id,)
        ).fetchone()["n"] == 1
    finally:
        conn.close()
        page.deleteLater()


def test_main_window_source_file_flow_creates_project_and_imports_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import openpyxl
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    from jiadun.core.models import project as project_model
    from jiadun.ui import main_window

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        project_model.platform_paths, "legacy_settings_file", lambda: tmp_path / "legacy-settings.json"
    )
    monkeypatch.setattr(
        project_model.platform_paths, "default_workspace_root", lambda: tmp_path / "workspace"
    )
    monkeypatch.setattr(
        project_model.platform_paths, "legacy_workspace_root", lambda: tmp_path / "legacy"
    )
    source = tmp_path / "单个结算文件.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["清单编码", "清单名称", "计量单位", "工程量", "综合单价", "合价"])
    sheet.append(["0101", "平整场地", "m2", 2, 3, 6])
    book.save(source)

    class FakeDialog:
        def __init__(self, parent=None):
            self.name_edit = type("NameEdit", (), {"setText": lambda _self, _text: None})()

        def exec(self):
            return QDialog.Accepted

        def values(self):
            return "拖拽创建项目", tmp_path / "workspace"

    monkeypatch.setattr(main_window, "NewProjectDialog", FakeDialog)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

    win = main_window.MainWindow()
    try:
        win._handle_source_paths([source])
        assert win.stack.currentIndex() == main_window.PAGE_WORKBENCH
        page = win.stack.widget(main_window.PAGE_WORKBENCH)
        assert page.conn.execute(
            "SELECT COUNT(*) AS n FROM source_files WHERE project_id=?",
            (page.project.project_id,),
        ).fetchone()["n"] == 1
    finally:
        win.close()
