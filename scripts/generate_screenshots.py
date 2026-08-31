"""生成 README/QUICKSTART 用界面截图（examples/screenshots/）。

方法声明：截图由真实运行的价盾（Jiadun）程序渲染产生——本脚本启动真实 MainWindow
（WA_DontShowOnScreen：窗口完整布局但不投到屏幕），加载匿名演示项目并调用与
界面按钮完全相同的计算/导出 API，然后逐页 widget.grab() 截取真实控件画面。
不含任何图像软件伪造；固定 1440×900（Retina 下 2x 等比），可重复生成视觉样本。
脚本固定演示输入中的时间和路径；截图可按布局和像素内容复现，但 PNG 编码器、
QuickLook 缩略图元数据及系统字体渲染不承诺字节级哈希一致。

导出文件（Excel/Word）截图：用 macOS QuickLook（qlmanage）渲染真实导出的文件
缩略图（渲染的是实际交付的导出文件内容）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "examples" / "screenshots"
WINDOW_SIZE = (1440, 900)


def _shot(widget, name: str, size: tuple[int, int] | None = WINDOW_SIZE) -> Path:
    pix = widget.grab()
    if size is not None and (pix.width(), pix.height()) != size:
        pix = pix.scaled(size[0], size[1])
        if (pix.width(), pix.height()) != size:  # 等比缩放后可能有偏差
            return _save_resized(pix, name, size)
    path = OUT_DIR / name
    pix.save(str(path), "PNG")
    print(f"  {name}: {path.stat().st_size // 1024} KB")
    return path


def _save_resized(pix, name: str, size: tuple[int, int]) -> Path:
    from PySide6.QtCore import QSize

    fixed = pix.scaled(QSize(*size))
    path = OUT_DIR / name
    fixed.save(str(path), "PNG")
    print(f"  {name}: {path.stat().st_size // 1024} KB（等比适配）")
    return path


def _render_export_preview(export_file: Path, name: str) -> Path | None:
    """macOS QuickLook（qlmanage）渲染真实导出文件 → PNG 缩略图。"""
    out = OUT_DIR / name
    subprocess.run(
        ["qlmanage", "-t", "-s", "1440", "-o", str(OUT_DIR), str(export_file)],
        check=True, capture_output=True, timeout=120)
    produced = OUT_DIR / (export_file.name + ".png")
    if not produced.is_file():
        print(f"  跳过 {name}：QuickLook 未产出缩略图")
        return None
    produced.replace(out)
    print(f"  {name}: {out.stat().st_size // 1024} KB（QuickLook 渲染导出文件）")
    return out


def main() -> int:
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _ = QApplication.instance() or QApplication([])

    # 受控工作空间（截图不触碰用户真实 ~/Documents）
    ws_root = Path(tempfile.mkdtemp(prefix="jiadun-screens-")) / "JiadunProjects"

    # 先重定向 platform.paths，再导入会在模块级读取 settings_file() 的项目模型；
    # 这样连配置目录的创建也只发生在临时目录，不会在截图任务中触碰真实用户设置。
    from jiadun.platform import paths as platform_paths

    platform_paths.config_dir = lambda: ws_root.parent / "Jiadun"
    platform_paths.legacy_config_dir = lambda: ws_root.parent / "CostGuard"
    platform_paths.legacy_settings_file = lambda: ws_root.parent / "legacy-settings.json"
    platform_paths.legacy_workspace_root = lambda: ws_root.parent / "CostGuardProjects"

    from jiadun.core import demo as demo_core
    from jiadun.core.anomalies import engine as anomaly_engine
    from jiadun.core.engine import aggregate, crosscheck
    from jiadun.core.export import excel_export
    from jiadun.core.matching import matching
    from jiadun.core.models import project as project_model
    from jiadun.ui import main_window as main_window_module
    from jiadun.ui.main_window import MainWindow, NewProjectDialog

    # 截图生成是可再生测试任务，配置和 QSettings 也必须隔离在临时目录，
    # 防止 provision_demo_project/窗口几何把本机用户设置写入真实位置。
    isolated_settings = ws_root.parent / "settings.json"
    project_model._SETTINGS_FILE = isolated_settings
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(ws_root.parent / "qt-settings"))

    # Native QSettings 的后端在不同 macOS 版本上可能忽略临时路径并保留
    # 上一次“最近打开”值。截图是受控的文档资产，因此在本脚本内使用内存
    # 设置对象，既固定时间字段，也避免把测试状态写入用户的真实偏好。
    class _DeterministicSettings:
        def __init__(self):
            self._values: dict[str, object] = {}

        def value(self, key: str, default=None):
            return self._values.get(key, default)

        def setValue(self, key: str, value) -> None:  # noqa: N802 - QSettings API
            self._values[key] = value

        def sync(self) -> None:
            return None

    deterministic_settings = _DeterministicSettings()
    main_window_module._settings = lambda: deterministic_settings
    main_window_module._legacy_settings = lambda: deterministic_settings

    print("准备演示项目（真实导入+计算）…")
    info = demo_core.provision_demo_project(ws_root)
    # 合成项目由 create_project 使用当前时间；截图输入必须固定，否则项目列表
    # 每次运行都会出现不同的创建时间。这里只改受控临时项目库，不触碰用户资料。
    with sqlite3.connect(Path(info.workspace_path) / "project.db") as db:
        db.execute(
            "UPDATE projects SET created_at=? WHERE id=?",
            ("2026-01-01T00:00:00", info.project_id),
        )
        db.commit()
    # 让主窗口的项目列表读受控工作空间（不触碰用户真实 Documents，截图也不泄露路径）
    # 项目列表会扫描"已登记工作空间"（settings.json），因此两层都要隔离：
    # workspace_roots 决定列表扫描范围（不得触到用户真实目录），
    # workspace_root 决定新建项目默认落点。
    project_model.workspace_roots = lambda: [ws_root]
    project_model.workspace_root = lambda: ws_root
    win = MainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.show()
    win.resize(*WINDOW_SIZE)

    # 项目列表页（演示项目已列出；列表只显示项目名，不泄露路径）
    win.stack.setCurrentIndex(0)
    win.refresh_projects()
    _shot(win, "01-项目列表.png")

    # 新建项目对话框
    # 对话框需要显示工作空间，但绝不应把随机临时目录写入跟踪截图；只在
    # 构造期间提供固定展示值，项目实际写入位置仍是上面的受控临时空间。
    workspace_root_resolver = project_model.workspace_root
    project_model.workspace_root = lambda: Path("JiadunProjects")
    dlg = NewProjectDialog(win)
    project_model.workspace_root = workspace_root_resolver
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    dlg.show()
    _shot(dlg, "02-新建项目.png", size=None)
    dlg.close()

    # 打开演示项目，跑与界面按钮相同的真实计算
    info2, conn = project_model.open_project(Path(info.workspace_path))
    win._enter_workbench(info2, conn)
    page = win.stack.widget(1)
    pid = info2.project_id
    # 演示项目导入后先建立可持久化的期次累计控制值；交互式“项目校核”按钮
    # 依赖该层数据，截图必须覆盖真实可复现的完整业务路径。
    for direction in ("downward", "upward"):
        aggs = aggregate.aggregate_project(conn, pid, direction=direction)
        aggregate.persist_period_totals(conn, pid, aggs)
        crosscheck.run_crosscheck(conn, pid, [1, 2, 3], direction=direction)
    anomaly_engine.run_anomalies(conn, pid)
    matching.save_matches(conn, pid, matching.match_items(conn, pid))
    page.refresh_all()

    for idx, name in ((0, "03-期次概览.png"), (1, "04-清单明细.png"),
                      (2, "05-异常检测.png"), (3, "06-匹配复核.png"),
                      (4, "07-成果导出.png")):
        page.tabs.setCurrentIndex(idx)
        _shot(page, name)

    # 真实导出（与导出按钮相同 API），渲染导出文件内容
    print("导出并渲染真实导出文件…")
    exports = Path(info2.workspace_path) / "exports"
    xlsx = excel_export.export_workbook(conn, pid, exports)
    docx = excel_export.export_management_summary_docx(conn, pid, exports)
    win._conn.close()
    # 08：审核底稿是导出工作簿的末张工作表；QuickLook 只渲染首张，
    # 故抽取真实导出中的「审核底稿」表做单表内容预览（导出文件本身不动）。
    import openpyxl

    wb = openpyxl.load_workbook(xlsx)
    for sheet_name in list(wb.sheetnames):
        if sheet_name != "审核底稿":
            del wb[sheet_name]
    single = exports / "preview-审核底稿.xlsx"
    wb.save(single)
    wb.close()
    _render_export_preview(single, "08-导出-Excel审核底稿.png")
    single.unlink()
    _render_export_preview(docx, "09-导出-Word管理层摘要.png")
    conn.close()

    print(f"\n完成：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
