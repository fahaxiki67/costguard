"""方向隔离缺陷回归（监督指令一）。

先红后绿：本文件最初在缺陷实现下运行必须失败——
ensure_period 只按 project_id+period_no 查找，同期号的 upward/downward
复用同一 period_id；crosscheck 同期号混算上下游数据。
"""
import json

import pytest

from costguard.core.db import migrations
from costguard.core.engine import crosscheck
from costguard.core.engine.settlement_io import ensure_period, next_period_no


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
            " VALUES ('方向隔离', 1, '/t', '2026')"
        ).lastrowid
    yield conn, pid
    conn.close()


def add_item(conn, period_id, code, name, qty, amount, flags=None):
    with conn:
        conn.execute(
            "INSERT INTO line_items(period_id, code, name, unit, quantity, amount, flags_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (period_id, code, name, "m3", qty, amount, json.dumps(flags or {})),
        )


class TestEnsurePeriodDirectionIsolation:
    def test_same_period_no_different_directions_two_ids(self, db):
        """同项目、同期号，upward 与 downward 必须是两个期次。"""
        conn, pid = db
        up_id = ensure_period(conn, pid, 1, "对上第1期", None, direction="upward")
        down_id = ensure_period(conn, pid, 1, "对下第1期", None, direction="downward")
        assert up_id != down_id, "同 period_no 的两个方向必须得到不同 period_id"

    def test_rows_do_not_overwrite_across_directions(self, db):
        """两个方向的标题、方向字段、明细互不覆盖。"""
        conn, pid = db
        up_id = ensure_period(conn, pid, 1, "对上第1期", None, direction="upward")
        down_id = ensure_period(conn, pid, 1, "对下第1期", None, direction="downward")
        add_item(conn, up_id, "K1", "对上清单", "10", "100")
        add_item(conn, down_id, "K1", "对下清单", "20", "200")
        up = conn.execute("SELECT title, direction FROM settlement_periods WHERE id=?", (up_id,)).fetchone()
        down = conn.execute("SELECT title, direction FROM settlement_periods WHERE id=?", (down_id,)).fetchone()
        assert up["title"] == "对上第1期" and up["direction"] == "upward"
        assert down["title"] == "对下第1期" and down["direction"] == "downward"
        n_up = conn.execute("SELECT COUNT(*) c FROM line_items WHERE period_id=?", (up_id,)).fetchone()["c"]
        n_down = conn.execute("SELECT COUNT(*) c FROM line_items WHERE period_id=?", (down_id,)).fetchone()["c"]
        assert (n_up, n_down) == (1, 1), "明细不得跨方向覆盖"

    def test_ensure_period_idempotent_within_direction(self, db):
        """同方向同期号重复调用必须复用同一期次（重复导入幂等）。"""
        conn, pid = db
        a = ensure_period(conn, pid, 1, "对上第1期", None, direction="upward")
        b = ensure_period(conn, pid, 1, "对上第1期（重复）", None, direction="upward")
        assert a == b

    def test_next_period_no_independent_per_direction(self, db):
        """期号递增语义确认：对上已有第1期时，对下第1期仍允许创建（编号序列按方向独立）。"""
        conn, pid = db
        ensure_period(conn, pid, 1, "对上第1期", None, direction="upward")
        # 全项目口径的 next 仍是 2（用于未标记导入的兜底）
        assert next_period_no(conn, pid) == 2
        # 但对下方向创建第1期必须成功，不得被对上的期号挡住
        down_id = ensure_period(conn, pid, 1, "对下第1期", None, direction="downward")
        assert down_id


class TestCrosscheckPeriodLocking:
    """反例精确断言（监督指令一.6）：up=100，down=200，全量才允许 300。"""

    @pytest.fixture()
    def mixed(self, db):
        conn, pid = db
        up_id = ensure_period(conn, pid, 1, "对上第1期", None, direction="upward")
        down_id = ensure_period(conn, pid, 1, "对下第1期", None, direction="downward")
        add_item(conn, up_id, "K1", "同一清单", "10", "100")
        add_item(conn, down_id, "K1", "同一清单", "20", "200")
        return conn, pid, up_id, down_id

    def test_check_period_by_id_isolated(self, mixed):
        conn, pid, up_id, down_id = mixed
        up = crosscheck.check_period(conn, period_id=up_id)
        down = crosscheck.check_period(conn, period_id=down_id)
        assert up.path_a_total == 100, f"upward A 路径必须恰为 100，得到 {up.path_a_total}"
        assert down.path_a_total == 200, f"downward A 路径必须恰为 200，得到 {down.path_a_total}"

    def test_by_no_requires_direction_on_ambiguity(self, mixed):
        """同期号存在两个方向时，不带 direction 的兼容入口必须明确拒绝。"""
        conn, pid, *_ = mixed
        with pytest.raises(crosscheck.AmbiguousPeriodError):
            crosscheck.check_period_by_no(conn, pid, 1)  # 未声明 direction

    def test_by_no_with_direction(self, mixed):
        conn, pid, *_ = mixed
        up = crosscheck.check_period_by_no(conn, pid, 1, direction="upward")
        assert up.path_a_total == 100
        down = crosscheck.check_period_by_no(conn, pid, 1, direction="downward")
        assert down.path_a_total == 200

    def test_b_path_and_c_path_same_period_id(self, mixed):
        """B 路径（原始网格重算）与 C 校验也必须锁定同一 period_id。"""
        conn, pid, up_id, down_id = mixed
        # B 路径需要 raw_sheets 关联期次：构造最小网格
        with conn:
            cur = conn.execute(
                "INSERT INTO source_files(project_id, original_path, stored_path, original_name,"
                " sha256, size_bytes, file_type, imported_at)"
                " VALUES (?, 't', 't', 't.xlsx', 'a'*64, 1, 'xlsx', '2026')", (pid,))
            fid = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO parse_batches(file_id, parser, parsed_at, status) VALUES (?,'t','2026','ok')",
                (fid,))
            batch = cur.lastrowid
            for pid_target, amount in ((up_id, "100"), (down_id, "200")):
                cur = conn.execute(
                    "INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols,"
                    " merged_ranges_json, hidden_rows_json, hidden_cols_json, period_id)"
                    " VALUES (?,0,'g',3,4,'[]','[]','[]',?)", (batch, pid_target))
                sid = cur.lastrowid
                cells = [
                    (1, 1, "清单编码", "清单编码", 0), (1, 2, "清单名称", "清单名称", 0),
                    (1, 3, "工程量", "工程量", 0), (1, 4, "合价", "合价", 0),
                    (2, 1, "K1", "K1", 0), (2, 2, "同一清单", "同一清单", 0),
                    (2, 3, amount, amount, 0), (2, 4, amount, amount, 0),
                ]
                conn.executemany(
                    "INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value, is_formula)"
                    " VALUES (?,?,?,?,?,?)",
                    [(sid, r, c, v, v, 0) for (r, c, v, _cv, _f) in cells],
                )
        up = crosscheck.check_period(conn, period_id=up_id)
        down = crosscheck.check_period(conn, period_id=down_id)
        assert up.path_b_total == 100 and up.path_a_total == 100
        assert down.path_b_total == 200 and down.path_a_total == 200
        assert up.status == "match" and down.status == "match"

    def test_period_totals_writeback_locks_period_id(self, mixed):
        """歧义期号必须明确拒绝；按方向分别校核后，回写必须挂在各自期次上。"""
        conn, pid, up_id, down_id = mixed
        # 未声明 direction → 明确拒绝（不静默混算）
        with pytest.raises(crosscheck.AmbiguousPeriodError):
            crosscheck.run_crosscheck(conn, pid, [1], direction=None)
        # 先落 period_totals（模拟累计已跑），再按方向校核回写
        for target in (up_id, down_id):
            with conn:
                conn.execute(
                    "INSERT INTO period_totals(project_id, period_id, item_key, amount_sum)"
                    " VALUES (?,?,?,?)", (pid, target, "code:K1",
                                          "100" if target == up_id else "200"))
        crosscheck.run_crosscheck(conn, pid, [1], direction="upward")
        crosscheck.run_crosscheck(conn, pid, [1], direction="downward")
        rows = conn.execute(
            """SELECT pt.cross_check_status, pt.evidence_id, sp.direction FROM period_totals pt
               JOIN settlement_periods sp ON sp.id = pt.period_id WHERE pt.project_id=?""",
            (pid,),
        ).fetchall()
        # 本测试焦点：回写必须各自锁定 period_id（status 值由校核本身决定，
        # 此处无网格 → incomplete，但两行必须分别回写、互不覆盖）
        by_dir = {r["direction"]: r["cross_check_status"] for r in rows}
        assert set(by_dir) == {"upward", "downward"}
        assert all(v in ("match", "diff", "incomplete") for v in by_dir.values())
        # 证据记录按 period_id 分开，来源里带方向
        evs = conn.execute(
            """SELECT e.sources_json FROM evidence e WHERE e.project_id=? AND e.kind='cross_check'""",
            (pid,),
        ).fetchall()
        dirs = {json.loads(e["sources_json"])["direction"] for e in evs}
        assert dirs == {"upward", "downward"}
