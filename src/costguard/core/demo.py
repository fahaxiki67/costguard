"""匿名演示项目一键创建（"体验匿名演示"入口的核心逻辑）。

把安装包内置的合成演示数据复制进一个新项目并完成导入（ADR-005 只读副本），
让普通用户不接触文件对话框即可走完"导入→标准化→校核→异常→匹配→导出"闭环。
本模块不依赖 PySide6，可无头测试。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from costguard.core.engine import settlement_io
from costguard.core.models import project as project_model
from costguard.platform import resources as platform_resources

DEMO_PROJECT_BASE_NAME = "匿名演示项目"
_MAX_NAME_ATTEMPTS = 20


class DemoProvisionError(RuntimeError):
    """演示项目创建/导入失败（携带可向用户展示的原因）。"""


def provision_demo_project(workspace: Path | None = None) -> project_model.ProjectInfo:
    """创建"匿名演示项目"并导入内置合成演示数据。

    - 项目重名时自动追加 -2、-3 … 后缀（不覆盖既有项目）；
    - xlsx 按 manifest.json 记录的方向导入（对上/对下）；
    - 合同 docx 走合同模块（提取事实与风险提示）；
    - 演示文件只读复制进项目 originals/，源文件零触碰。
    """
    demo_dir = platform_resources.bundled_demo_dir()
    manifest = json.loads((demo_dir / "manifest.json").read_text(encoding="utf-8"))
    root = Path(workspace) if workspace else project_model.workspace_root()

    info: project_model.ProjectInfo | None = None
    for i in range(1, _MAX_NAME_ATTEMPTS + 1):
        name = DEMO_PROJECT_BASE_NAME if i == 1 else f"{DEMO_PROJECT_BASE_NAME}-{i}"
        try:
            info = project_model.create_project(name, root)
            break
        except project_model.ProjectError as exc:
            # 目录重名属于可预期的业务分支；技术细节不直接进入普通界面。
            if "already exists" not in str(exc):
                break
    if info is None:
        raise DemoProvisionError("无法创建演示项目，请更换工作空间或项目名称后重试")

    _info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        pdir = Path(info.workspace_path)
        failures: list[str] = []
        for entry in manifest["files"]:
            src = demo_dir / entry["file_name"]
            try:
                if entry["data_type"] == "xlsx":
                    settlement_io.import_settlement_file(
                        conn, info.project_id, pdir, src, direction=entry["direction"])
                elif entry["data_type"] == "docx":
                    from costguard.core.contracts import extract as contract_extract

                    contract_extract.import_contract(conn, info.project_id, pdir, src)
                else:
                    failures.append(f"{entry['file_name']}: 未知演示数据类型 {entry['data_type']}")
            except Exception:  # noqa: BLE001 — 逐文件兜底，失败不中断其余演示文件
                failures.append(f"{entry['file_name']}：导入失败，请检查演示资源完整性")
        if failures:
            raise DemoProvisionError("演示数据导入部分失败：\n" + "\n".join(failures))
        _assert_periods_seeded(conn, info.project_id)
    finally:
        conn.close()
    return info


def _assert_periods_seeded(conn: sqlite3.Connection, project_id: int) -> None:
    """防呆：演示项目导入完成后必须已有对上/对下期次，否则视为导入失败。"""
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN direction='upward' THEN 1 ELSE 0 END) AS up,
             SUM(CASE WHEN direction='downward' THEN 1 ELSE 0 END) AS down
           FROM settlement_periods WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    if not row or not row["up"] or not row["down"]:
        raise DemoProvisionError("演示数据导入后未形成对上/对下期次，请重新运行演示")
