"""Windows 交付冒烟（也可在任意平台运行的部分用于自测）。

用法：
  python scripts/windows_smoke.py e2e
      无头端到端：新建项目 → 导入匿名演示数据（对上/对下+合同）→ 双向校核 →
      异常检测 → 匹配 → Excel/Word 导出，并断言方向隔离/缺失不填零等纪律。
  python scripts/windows_smoke.py installed <exe 路径>
      已安装应用冒烟：启动 → 进程存活 → 请求退出 → 结束（由调用方再执行卸载）。

本脚本只使用 examples/demo 合成数据，不触碰任何真实资料。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_e2e() -> int:
    from costguard.core import demo as demo_core
    from costguard.core.anomalies import engine as anomaly_engine
    from costguard.core.engine import crosscheck
    from costguard.core.export import excel_export
    from costguard.core.matching import matching
    from costguard.core.models import project as pm

    with tempfile.TemporaryDirectory(prefix="cg-e2e-") as td:
        ws = Path(td) / "CostGuardProjects"
        info = demo_core.provision_demo_project(ws)
        info, conn = pm.open_project(Path(info.workspace_path))
        pid = info.project_id
        try:
            # 双向校核（方向隔离）
            results = []
            for direction in ("downward", "upward"):
                results += crosscheck.run_crosscheck(conn, pid, [1, 2, 3], direction=direction)
            assert len(results) == 6, f"应校核 6 个期次，实际 {len(results)}"
            # 同期号双方向并存（方向隔离的直接证据）
            both = conn.execute(
                """SELECT COUNT(*) c FROM (SELECT period_no FROM settlement_periods
                   WHERE project_id=? GROUP BY period_no HAVING COUNT(DISTINCT direction)>1)""",
                (pid,)).fetchone()[0]
            assert both == 3, f"同期号双方向期次数应为 3，实际 {both}"
            # 异常与匹配
            findings = anomaly_engine.run_anomalies(conn, pid)
            assert findings, "演示数据必须检出异常"
            levels = {g.level for g in matching.match_items(conn, pid)}
            assert "confirmed" in levels and "probable" in levels
            # 缺失不填零：缺失单价行保持 NULL
            nulls = conn.execute(
                """SELECT COUNT(*) c FROM line_items li JOIN settlement_periods sp
                   ON sp.id=li.period_id WHERE sp.project_id=? AND li.flags_json
                   NOT LIKE '%"subtotal": true%' AND li.unit_price IS NULL""",
                (pid,)).fetchone()[0]
            assert nulls >= 2, f"缺失单价行应保持 NULL，实际 {nulls}"
            # Decimal 导出
            exports = Path(info.workspace_path) / "exports"
            xlsx = excel_export.export_workbook(conn, pid, exports)
            docx = excel_export.export_management_summary_docx(conn, pid, exports)
            assert xlsx.is_file() and xlsx.stat().st_size > 10_000
            assert docx.is_file() and docx.stat().st_size > 5_000
            print(f"E2E PASS：期次 6/校核 {len(results)}/异常 {len(findings)}/"
                  f"缺失保留 {nulls} 行/导出 {xlsx.name}+{docx.name}")
        finally:
            conn.close()
    return 0


def run_installed(exe: str) -> int:
    import os

    exe_path = Path(exe)
    if not exe_path.is_file():
        print(f"FAIL: 未找到已安装程序 {exe_path}", file=sys.stderr)
        return 1
    proc = subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent),
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                            if os.name == "nt" else 0)
    time.sleep(8)
    if proc.poll() is not None:
        print(f"FAIL: 安装后程序提前退出（rc={proc.returncode}）", file=sys.stderr)
        return 1
    print(f"已启动并常驻：{exe_path}（pid={proc.pid}）")
    proc.terminate()  # Windows = TerminateProcess；GUI 无未保存状态
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("已退出")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    if sys.argv[1] == "e2e":
        return run_e2e()
    if sys.argv[1] == "installed" and len(sys.argv) == 3:
        return run_installed(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
