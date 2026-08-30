"""生成 README/QUICKSTART 用界面截图（examples/screenshots/）。

方法声明：截图由真实运行的 CostGuard 程序渲染产生——本脚本启动真实 MainWindow
（WA_DontShowOnScreen：窗口完整布局但不投到屏幕），加载匿名演示项目并调用与
界面按钮完全相同的计算/导出 API，然后逐页 widget.grab() 截取真实控件画面。
不含任何图像软件伪造；固定 1440×900（Retina 下 2x 等比），可重复生成。

导出文件（Excel/Word）截图：用 LibreOffice 无头渲染真实导出的文件转 PDF，
再经 sips 转 PNG（渲染的是实际交付的导出文件内容）。
"""
from __future__ import annotations

import os
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
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _ = QApplication.instance() or QApplication([])

    # 受控工作空间（截图不触碰用户真实 ~/Documents）
    ws_root = Path(tempfile.mkdtemp(prefix="cg-screens-")) / "CostGuardProjects"

    from costguard.core import demo as demo_core
    from costguard.core.anomalies import engine as anomaly_engine
    from costguard.core.engine import crosscheck
    from costguard.core.export import excel_export
    from costguard.core.matching import matching
    from costguard.core.models import project as project_model
    from costguard.ui.main_window import MainWindow, NewProjectDialog

    print("准备演示项目（真实导入+计算）…")
    info = demo_core.provision_demo_project(ws_root)
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
    dlg = NewProjectDialog(win)
    dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
    dlg.show()
    _shot(dlg, "02-新建项目.png", size=None)
    dlg.close()

    # 打开演示项目，跑与界面按钮相同的真实计算
    info2, conn = project_model.open_project(Path(info.workspace_path))
    win._enter_workbench(info2, conn)
    page = win.stack.widget(1)
    pid = info2.project_id
    for direction in ("downward", "upward"):
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
