""""体验匿名演示"入口测试：核心逻辑无头验证 + UI 冒烟（离屏）。

core.demo.provision_demo_project 是 UI 按钮的完整后端路径：
创建项目 → 复制内置演示数据（只读副本）→ 按方向导入 → 合同导入。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from costguard.core import demo as demo_core
from costguard.platform import resources as platform_resources

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bundled_demo_dir_resolves_to_repo_examples_in_dev_mode() -> None:
    assert not platform_resources.is_frozen()
    demo_dir = platform_resources.bundled_demo_dir()
    assert demo_dir == REPO_ROOT / "examples" / "demo"
    assert (demo_dir / "manifest.json").is_file()


def test_provision_demo_project_end_to_end(tmp_path: Path) -> None:
    from costguard.core.models import project as project_model

    ws = tmp_path / "CostGuardProjects"
    info = demo_core.provision_demo_project(ws)
    assert info.name == "匿名演示项目"
    assert (Path(info.workspace_path) / "project.db").is_file()
    # 重新打开核对业务结果
    _info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        periods = conn.execute(
            "SELECT direction, COUNT(*) n FROM settlement_periods WHERE project_id=?"
            " GROUP BY direction", (info.project_id,)).fetchall()
        by_dir = {r["direction"]: r["n"] for r in periods}
        assert by_dir == {"upward": 3, "downward": 3}
        # 合同文档已导入
        ndocs = conn.execute(
            "SELECT COUNT(*) n FROM contract_docs WHERE project_id=?",
            (info.project_id,)).fetchone()["n"]
        assert ndocs == 1
        # 演示文件作为只读副本入库存放（ADR-005）
        n_files = conn.execute(
            "SELECT COUNT(*) n FROM source_files WHERE project_id=?",
            (info.project_id,)).fetchone()["n"]
        assert n_files == 4
    finally:
        conn.close()


def test_provision_demo_project_auto_suffix_on_duplicate(tmp_path: Path) -> None:
    demo_core.provision_demo_project(tmp_path)
    info2 = demo_core.provision_demo_project(tmp_path)
    assert info2.name == "匿名演示项目-2", "重名时必须另建新项目，不得覆盖"


def test_provision_demo_project_copies_files_readonly(tmp_path: Path) -> None:
    """演示源文件（仓库 examples/demo）在导入后字节零修改。"""
    from tests.unit.test_demo_data import DEMO_DIR, DEMO_FILES, _sha256

    before = {name: _sha256(DEMO_DIR / name) for name in DEMO_FILES}
    demo_core.provision_demo_project(tmp_path)
    after = {name: _sha256(DEMO_DIR / name) for name in DEMO_FILES}
    assert before == after, "演示源文件被修改（违反只读纪律）"


def _qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def test_main_window_smoke_demo_entry_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """离屏冒烟：演示入口按钮存在；项目列表只显示项目名（路径仅进 tooltip）。

    工作空间根被 monkeypatch 到临时目录——绝不触碰用户真实
    ~/Documents/CostGuardProjects。
    """
    _qapp()
    from PySide6.QtWidgets import QPushButton

    from costguard.core.models import project as project_model
    from costguard.ui.main_window import MainWindow

    ws = tmp_path / "CostGuardProjects"
    ws.mkdir(parents=True)
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        project_model.platform_paths, "default_workspace_root", lambda: ws
    )
    win = MainWindow()
    try:
        buttons = win.findChildren(QPushButton)
        assert any(b.text() == "体验匿名演示" for b in buttons), "缺少演示入口按钮"
        # 项目列表隐私：文本只含项目名，绝对路径仅进 tooltip
        project_model.create_project("路径隐私冒烟", ws)
        win.refresh_projects()
        texts = [win.project_list.item(i).text()
                 for i in range(win.project_list.count())]
        assert any(t == "路径隐私冒烟" for t in texts), texts
        assert not any("/" in t for t in texts), f"列表项不得显示路径：{texts}"
        tooltips = [win.project_list.item(i).toolTip()
                    for i in range(win.project_list.count())]
        assert any("路径隐私冒烟" in tt for tt in tooltips), tooltips
    finally:
        win.close()
        win.deleteLater()
