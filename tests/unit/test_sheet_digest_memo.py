"""Sheet 单元格摘要记忆化（只读窗口内跨门控复用）的行为回归。

背景（2026-09-06 profiling 实锤）：一次 Excel 导出触发约 22 次
Run Contract 门控 + 多次摘要构建，旧实现每次全量重扫 raw_cells
（10k 项目 441 万次 canonical_json、导出 144.7s）。优化为进程内
按连接记忆化，只读窗口（写指纹稳定）内复用，任何写入立即失效。

必须同时锁住两端：
- 性能端：只读窗口内第二次门控不再重算摘要；
- 准确端：任何写入（含绕过触发器直改 raw_cells 值）后摘要按当前
  数据重算，值级漂移保持可见（宪章 Fail-Closed）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations


@pytest.fixture()
def project_db(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES(?,?,?,?)""",
            ("摘要记忆化测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


def _import_sheet(conn, project_id: int, tmp_path: Path, rows: list[tuple]) -> int:
    """走真实结算导入路径落一个最小合成 Sheet，返回最新 sheet_id。

    必须用 settlement_io.import_settlement_file（含 Sheet 角色确认与
    明细抽取）——raw 持久化不产生 line_items，而明细摘要测试需要它们。
    """
    from jiadun.core.engine import settlement_io

    path = tmp_path / f"input-{abs(hash(tuple(rows))) % 10**8}.xlsx"
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期"
    ws.append(["清单编码", "清单名称", "项目特征", "计量单位", "工程量", "综合单价", "合价", "税率"])
    for code, amount in rows:
        ws.append([code, f"合成项{code}", "memo", "项", 1, amount, amount, None])
    wb.save(path)
    report = settlement_io.import_settlement_file(
        conn, project_id, tmp_path, path, period_no=1, direction="downward"
    )
    assert report.status == "ok", f"导入未完成：{report.status}"
    sheet = conn.execute(
        """SELECT rs.id FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=? ORDER BY rs.id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    return int(sheet["id"])


class _ComputeCounter:
    """包装 compute_sheet_cell_digest 统计真实重算次数。"""

    def __init__(self):
        self.calls: list[int] = []
        self._original = run_contract.compute_sheet_cell_digest

    def install(self, monkeypatch: pytest.MonkeyPatch):
        counter = self

        def counted(conn, sheet_id):
            counter.calls.append(int(sheet_id))
            return counter._original(conn, sheet_id)

        monkeypatch.setattr(run_contract, "compute_sheet_cell_digest", counted)


def test_read_window_reuses_digests_across_gate_calls(
    project_db, monkeypatch: pytest.MonkeyPatch
):
    conn, project_id, tmp_path = project_db
    _import_sheet(conn, project_id, tmp_path, [("A001", 100), ("A002", 200)])
    counter = _ComputeCounter()
    counter.install(monkeypatch)

    scope_first = run_contract._sheet_scope(conn, project_id)
    assert counter.calls, "首次构建必须真实计算摘要"
    first_calls = list(counter.calls)

    # 只读窗口内的第二次构建：同 Sheet 不得重算。
    scope_second = run_contract._sheet_scope(conn, project_id)
    assert counter.calls == first_calls
    assert scope_second == scope_first


def test_any_write_on_connection_invalidates_memo(
    project_db, monkeypatch: pytest.MonkeyPatch
):
    conn, project_id, tmp_path = project_db
    _import_sheet(conn, project_id, tmp_path, [("A001", 100)])
    run_contract._sheet_scope(conn, project_id)  # 填充记忆

    counter = _ComputeCounter()
    counter.install(monkeypatch)
    # 本连接发生一次合法写入（导入第二个文件）后，摘要必须按新数据重算。
    _import_sheet(conn, project_id, tmp_path, [("B001", 300)])
    run_contract._sheet_scope(conn, project_id)
    assert counter.calls, "写入后必须重新计算摘要（记忆失效）"


def test_value_drift_bypassing_triggers_stays_visible(
    project_db, monkeypatch: pytest.MonkeyPatch
):
    """绕过 raw_cells 不可变触发器直改值：摘要漂移必须可见（Fail-Closed）。"""
    conn, project_id, tmp_path = project_db
    sheet_id = _import_sheet(
        conn, project_id, tmp_path, [("A001", 100), ("A002", 200)]
    )
    before = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }

    conn.execute("DROP TRIGGER trg_raw_cells_immutable_update")
    with conn:
        conn.execute(
            "UPDATE raw_cells SET raw_value='伪造' WHERE sheet_id=? AND row=2 AND col=2",
            (sheet_id,),
        )

    after = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }
    assert after[sheet_id] != before[sheet_id], (
        "直改 raw_cells 值后摘要不变：值级漂移被记忆化静默掩盖"
    )


def test_separate_connections_do_not_share_stale_memos(project_db, tmp_path: Path):
    """另一连接的写入必须让本连接的摘要重算（data_version 失效路径）。"""
    conn, project_id, tmp_path = project_db
    sheet_id = _import_sheet(conn, project_id, tmp_path, [("A001", 100)])
    before = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }

    other = migrations.connect(tmp_path / "project.db")
    try:
        other.execute("DROP TRIGGER trg_raw_cells_immutable_update")
        with other:
            other.execute(
                "UPDATE raw_cells SET raw_value='外部改写' "
                "WHERE sheet_id=? AND row=2 AND col=2",
                (sheet_id,),
            )
    finally:
        other.close()

    after = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }
    assert after[sheet_id] != before[sheet_id], (
        "外部连接改写 raw_cells 后摘要不变：跨连接写入未触发失效"
    )


def test_line_item_digest_reuses_within_read_window(project_db):
    """明细摘要同窗口复用、写入后重算（同款指纹守卫）。"""
    conn, project_id, tmp_path = project_db
    _import_sheet(conn, project_id, tmp_path, [("A001", 100), ("A002", 200)])
    first = run_contract._line_item_digest(conn, project_id)
    assert first["line_item_count"] == 2

    # 只读窗口内再次求值：结果一致（纯函数性质）。
    second = run_contract._line_item_digest(conn, project_id)
    assert second == first

    # 合法写入（再导入一个文件）后摘要必须按新数据重算。
    _import_sheet(conn, project_id, tmp_path, [("B001", 300)])
    third = run_contract._line_item_digest(conn, project_id)
    assert third["line_item_count"] == 3
    assert third["line_items_sha256"] != first["line_items_sha256"]


def test_memo_capacity_guard_frees_stale_entries(
    project_db, monkeypatch: pytest.MonkeyPatch
):
    """容量上限触发整体清空：清空后结果仍与逐次重算一致（无正确性损失）。"""
    conn, project_id, tmp_path = project_db
    _import_sheet(conn, project_id, tmp_path, [("A001", 100)])
    first = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }
    # 人为塞满记忆表模拟长驻进程累积的已关闭连接条目。
    run_contract._READ_WINDOW_MEMO.clear()
    for index in range(run_contract._READ_WINDOW_MEMO_LIMIT + 1):
        run_contract._READ_WINDOW_MEMO[index] = {"fingerprint": None, "values": {}}
    second = {
        entry["sheet_id"]: entry["raw_cell_sha256"]
        for entry in run_contract._sheet_scope(conn, project_id)
    }
    assert second == first
