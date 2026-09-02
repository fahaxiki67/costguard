"""SQLite 破坏性测试（P3 验收工具）。

场景（全部只使用合成演示数据，可在任何平台运行）：
  kill_import     导入中途强杀进程 → 重开库必须完整、无半成功、可重跑
  kill_analysis   分析中途强杀进程 → 同上
  kill_export     导出中途强杀进程 → 同上
  db_locked       外部进程持有写锁 → 应用写入必须报错明确、不损坏库
  project_moved   项目目录整体移动 → 重开自动修复 originals 路径
  onedrive_conflict  同步冲突副本出现 → 打开不受影响、冲突文件不被误删
  old_version_upgrade  v0.1.16 基线建的项目 → 新版本打开自动迁移+备份+数据保全

用法：uv run python scripts/db_destructive_tests.py [--only 场景1,场景2]
输出 JSON（stdout）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PYTHON = sys.executable
KILL_SCRIPT_TEMPLATE = r'''
import sys, time
sys.path.insert(0, {src!r})
from pathlib import Path
from jiadun.core import demo as demo_core
from jiadun.core.engine import aggregate as agg_mod
from jiadun.core.export import excel_export
from jiadun.core.matching import matching
from jiadun.core.models import project as pm
from jiadun.core import analysis
from jiadun.core.anomalies import engine as anomaly_engine

ws = Path({ws!r})
info = demo_core.provision_demo_project(ws)
info, conn = pm.open_project(Path(info.workspace_path))
pid = info.project_id
mode = {mode!r}
print("ready", flush=True)
if mode == "import":
    # 真实导入路径反复执行：每个项目导入一次演示文件，循环建项目最贴近导入负载
    from jiadun.core import demo as demo_core
    for i in range(15):
        demo_core.provision_demo_project(ws)
elif mode == "analysis":
    aggs = agg_mod.aggregate_project(conn, pid, include_all_directions=True)
    agg_mod.persist_period_totals(conn, pid, aggs)
    for i in range(60):
        results = analysis.run_analysis(conn, pid)
elif mode == "export":
    aggs = agg_mod.aggregate_project(conn, pid, include_all_directions=True)
    agg_mod.persist_period_totals(conn, pid, aggs)
    exports = Path(info.workspace_path) / "exports"
    for i in range(80):
        excel_export.export_workbook(conn, pid, exports)
conn.close()
'''


def _spawn_and_kill(mode: str, ws: Path, delay: float) -> dict:
    script = KILL_SCRIPT_TEMPLATE.format(src=str(REPO_ROOT / "src"), ws=str(ws), mode=mode)
    script_path = ws / f"_kill_probe_{mode}.py"
    ws.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [PYTHON, str(script_path)], cwd=str(ws), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
    # 等 probe 打印 ready（库已建好并打开）后再延迟一点杀
    line = proc.stdout.readline().decode("utf-8", "replace").strip()
    if line != "ready":
        proc.kill()
        return {"ok": False, "error": f"probe 未就绪: {line}"}
    time.sleep(delay)
    if proc.poll() is not None:
        return {"ok": False, "error": f"probe 提前退出 rc={proc.returncode}"}
    if os.name == "nt":
        proc.kill()  # TerminateProcess，最接近强制断电/任务管理器结束
    else:
        os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=15)
    return {"ok": True, "killed_after_s": delay}


def _verify_reopen(ws: Path, *, expect_results_usable: bool) -> dict:
    from jiadun.core import backup_restore as br
    from jiadun.core.engine import aggregate as agg_mod
    from jiadun.core.models import project as pm

    # 找 workspace 根下任意项目目录（含 project.db 的最深两层）
    cands = sorted(p for p in ws.rglob("project.db"))
    if not cands:
        return {"integrity_ok": False, "integrity": "no project.db",
                "fk_violations": [], "rerun_ok": False,
                "rerun_error": "project.db not found", "half_baked": True,
                "period_total_directions": []}
    info = conn = None
    for attempt in range(5):
        try:
            info, conn = pm.open_project(cands[0].parent)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"integrity_ok": False, "integrity": f"reopen failed: {exc}",
                        "fk_violations": [], "rerun_ok": False,
                        "rerun_error": str(exc), "half_baked": True,
                        "period_total_directions": []}
            time.sleep(1.0)
    try:
        rep = br.integrity_check(conn)
        # 重跑前的既有状态：演示项目为双方向，出现且仅出现单方向 = 半成功残留
        dirs_before = [r[0] for r in conn.execute(
            """SELECT DISTINCT sp.direction FROM period_totals pt
               JOIN settlement_periods sp ON sp.id=pt.period_id
               WHERE pt.project_id=?""",
            (info.project_id,)).fetchall()]
        counts_before = conn.execute(
            "SELECT COUNT(*) FROM period_totals WHERE project_id=?",
            (info.project_id,)).fetchone()[0]
        half_baked = len(dirs_before) == 1 and counts_before > 0
        rerun_ok = True
        rerun_error = None
        try:
            aggs = agg_mod.aggregate_project(conn, info.project_id, include_all_directions=True)
            agg_mod.persist_period_totals(conn, info.project_id, aggs)
        except Exception as exc:  # noqa: BLE001
            rerun_ok, rerun_error = False, f"{type(exc).__name__}: {exc}"
        return {
            "integrity_ok": rep.ok,
            "integrity": rep.integrity,
            "fk_violations": rep.foreign_key_violations[:5],
            "rerun_ok": rerun_ok,
            "rerun_error": rerun_error,
            "pre_kill_directions": dirs_before,
            "pre_kill_rows": counts_before,
            "half_baked": half_baked,
        }
    finally:
        conn.close()


def scenario_kill(mode: str) -> dict:
    import tempfile

    with tempfile.TemporaryDirectory(prefix=f"cg_kill_{mode}_") as td:
        ws = Path(td) / "JiadunProjects-ws"
        killed = _spawn_and_kill(mode, ws, delay=random.uniform(0.4, 2.2))
        if not killed["ok"]:
            return {"scenario": f"kill_{mode}", "ok": False, **killed}
        verify = _verify_reopen(ws, expect_results_usable=False)
        ok = verify["integrity_ok"] and verify["rerun_ok"] and not verify["half_baked"]
        return {"scenario": f"kill_{mode}", "ok": ok, "killed": killed, **verify}


def scenario_db_locked() -> dict:
    import sqlite3
    import tempfile

    from jiadun.core import demo as demo_core
    from jiadun.core.models import project as pm

    with tempfile.TemporaryDirectory(prefix="cg_locked_") as td:
        ws = Path(td)
        info = demo_core.provision_demo_project(ws)
        pdir = Path(info.workspace_path)
        db = pdir / "project.db"
        # 外部连接持有 EXCLUSIVE 锁
        locker = sqlite3.connect(db)
        locker.execute("BEGIN EXCLUSIVE")
        lock_error = None
        try:
            try:
                conn = pm.open_project(pdir)[1]
                try:
                    conn.execute("BEGIN IMMEDIATE")
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                # 打开/写入被拒绝=正确行为（迁移和写入都必须让路）
                lock_error = f"{type(exc).__name__}: {exc}"
        finally:
            locker.rollback()
            locker.close()
        # 释放后再开必须正常
        from jiadun.core import backup_restore as br
        conn = pm.open_project(pdir)[1]
        try:
            rep = br.integrity_check(conn)
        finally:
            conn.close()
        return {
            "scenario": "db_locked",
            "ok": lock_error is not None and rep.ok,
            "locked_refused_as": lock_error,
            "integrity_after": rep.integrity,
        }


def scenario_project_moved() -> dict:
    import shutil
    import tempfile

    from jiadun.core import backup_restore as br
    from jiadun.core import demo as demo_core
    from jiadun.core.models import project as pm

    with tempfile.TemporaryDirectory(prefix="cg_moved_") as td:
        ws = Path(td) / "ws1"
        info = demo_core.provision_demo_project(ws)
        pdir = Path(info.workspace_path)
        conn0 = pm.open_project(pdir)[1]
        original_files = conn0.execute(
            "SELECT COUNT(*) FROM source_files").fetchone()[0]
        conn0.close()
        moved = Path(td) / " moved 「已移动」" / pdir.name
        moved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdir), str(moved))
        info2, conn = pm.open_project(moved)
        try:
            rows = conn.execute(
                "SELECT stored_path FROM source_files").fetchall()
            stored_ok = all(Path(r["stored_path"]).is_file() for r in rows)
            rep = br.integrity_check(conn)
        finally:
            conn.close()
        return {
            "scenario": "project_moved",
            "ok": stored_ok and rep.ok and original_files > 0,
            "source_files": original_files,
            "stored_paths_repaired": stored_ok,
            "integrity": rep.integrity,
        }


def scenario_onedrive_conflict() -> dict:
    import shutil
    import tempfile

    from jiadun.core import backup_restore as br
    from jiadun.core import demo as demo_core
    from jiadun.core.models import project as pm

    with tempfile.TemporaryDirectory(prefix="cg_conflict_") as td:
        ws = Path(td)
        info = demo_core.provision_demo_project(ws)
        pdir = Path(info.workspace_path)
        db = pdir / "project.db"
        # 模拟 OneDrive：产生冲突副本与临时文件
        conflict_db = pdir / "project-DESKTOP-ABC12-CONFLICT.db"
        shutil.copy2(db, conflict_db)
        (pdir / "project.db~$ syncing").write_bytes(b"tmp")
        info2, conn = pm.open_project(pdir)
        try:
            rep = br.integrity_check(conn)
            n_conflict = conflict_db.stat().st_size > 0
        finally:
            conn.close()
        conflict_still_there = conflict_db.is_file()
        return {
            "scenario": "onedrive_conflict",
            "ok": rep.ok and n_conflict and conflict_still_there,
            "integrity": rep.integrity,
            "conflict_file_preserved": conflict_still_there,
            "syncing_tmp_cleaned": not (pdir / "project.db~$ syncing").is_file(),
            "note": "软件不清理也不读取冲突副本；删除冲突文件是用户的决定",
        }


def scenario_old_version_upgrade() -> dict:
    """用 v0.1.16 基线代码建项目 → 当前代码打开：迁移+备份+数据保全。"""
    import shutil
    import subprocess
    import tempfile

    worktree = REPO_ROOT / "cg_runs" / "wt_v0116"
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "v0.1.16"],
        cwd=str(REPO_ROOT), check=True, capture_output=True)
    try:
        with tempfile.TemporaryDirectory(prefix="cg_upgrade_") as td:
            ws = Path(td)
            old_src = worktree / "src"
            env = {**os.environ, "PYTHONPATH": str(old_src)}
            probe = ws / "_old_create.py"
            probe.write_text(
                "import sys\n"
                f"sys.path.insert(0, r'{old_src}')\n"
                "from jiadun.core import demo as demo_core\n"
                "from jiadun.core.engine import aggregate as agg_mod\n"
                "from jiadun.core.models import project as pm\n"
                "from jiadun.core.anomalies import engine as anomaly_engine\n"
                "from jiadun.core.engine import crosscheck\n"
                "from jiadun.core.matching import matching\n"
                "info = demo_core.provision_demo_project(r'" + str(ws) + "')\n"
                "info, conn = pm.open_project(info.workspace_path)\n"
                "pid = info.project_id\n"
                "aggs = agg_mod.aggregate_project(conn, pid, include_all_directions=True)\n"
                "agg_mod.persist_period_totals(conn, pid, aggs)\n"
                "crosscheck.run_crosscheck(conn, pid, [1,2,3], direction='upward')\n"
                "anomaly_engine.run_anomalies(conn, pid)\n"
                "matching.match_items(conn, pid)\n"
                "n_files = conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]\n"
                "n_evidence = conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]\n"
                "n_rules = conn.execute('SELECT COUNT(*) FROM anomalies').fetchone()[0]\n"
                "n_matches = conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0]\n"
                "print('BASE', n_files, n_evidence, n_rules, n_matches)\n",
                encoding="utf-8")
            out = subprocess.run([PYTHON, str(probe)], capture_output=True,
                                 text=True, env=env, encoding="utf-8", errors="replace")
            base_line = next((ln for ln in out.stdout.splitlines() if ln.startswith("BASE")), None)
            if base_line is None:
                return {"scenario": "old_version_upgrade", "ok": False,
                        "error": (out.stderr or out.stdout)[-500:]}
            _, n_files, n_ev, n_rules, n_matches = base_line.split()
            # 现在用当前版本打开
            from jiadun.core import backup_restore as br
            from jiadun.core.db import migrations
            from jiadun.core.models import project as pm

            pdir = next(x.parent for x in sorted(ws.rglob("project.db")))
            info, conn = pm.open_project(pdir)
            try:
                rep = br.integrity_check(conn)
                now = {
                    "files": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
                    "evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                    "rules": conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],
                    "matches": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                    "schema": conn.execute(
                        "SELECT schema_version FROM projects LIMIT 1").fetchone()[0],
                }
                backups = list((pdir / "backups").glob("project_pre_migration_*.db"))
            finally:
                conn.close()
            ok = (
                now["files"] == int(n_files) and now["evidence"] == int(n_ev)
                and now["rules"] == int(n_rules) and now["matches"] == int(n_matches)
                and now["schema"] == migrations.LATEST_SCHEMA_VERSION
                and len(backups) >= 1 and rep.ok)
            return {
                "scenario": "old_version_upgrade",
                "ok": ok,
                "baseline(v0.1.16)": {"files": n_files, "evidence": n_ev,
                                      "rules": n_rules, "matches": n_matches},
                "after_upgrade": now,
                "integrity": rep.integrity,
                "pre_migration_backups": [b.name for b in backups],
            }
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=str(REPO_ROOT), check=False, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None)
    args = parser.parse_args()
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    scenarios = {
        "kill_import": lambda: scenario_kill("import"),
        "kill_analysis": lambda: scenario_kill("analysis"),
        "kill_export": lambda: scenario_kill("export"),
        "db_locked": scenario_db_locked,
        "project_moved": scenario_project_moved,
        "onedrive_conflict": scenario_onedrive_conflict,
        "old_version_upgrade": scenario_old_version_upgrade,
    }
    results = []
    for name, fn in scenarios.items():
        if only and name not in only:
            continue
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            r = {"scenario": name, "ok": False,
                 "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
        print(f"  {name}: {'PASS' if r.get('ok') else 'FAIL'}", file=sys.stderr)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
