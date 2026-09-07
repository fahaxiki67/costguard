"""税口径模型测试（任务书任务 C1-C6）。"""

import pytest

from jiadun.core.db import migrations
from jiadun.core.engine.tax_basis import (
    detect_tax_basis,
    set_sheet_tax_basis,
    tax_basis_comparable,
)


@pytest.fixture()
def project_db(tmp_path):
    import hashlib

    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    # 源文件实体与真实哈希都要落盘：Run Contract 构建会核验存储副本身份
    src = tmp_path / "结算表.xlsx"
    src.write_bytes(b"tax basis fixture")
    real_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("税口径测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        conn.execute(
            """INSERT INTO source_files(project_id, original_name, original_path,
               stored_path, file_type, size_bytes, sha256, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "结算表.xlsx", str(src), str(src), "xlsx",
             src.stat().st_size, real_sha, "2026"),
        )
        fid = conn.execute("SELECT MAX(id) FROM source_files").fetchone()[0]
        cur = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json)
               VALUES (?,?,?,?,?)""",
            (fid, "pipeline", "2026-09-07T23:00:00", "ok", "{}"),
        )
        batch_id = cur.lastrowid
        conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
               n_rows, n_cols, sheet_status, sheet_status_reason)
               VALUES (?,?,?,?,?,?,?)""",
            (batch_id, 1, "分部分项清单", 50, 8, "pending", "待确认"),
        )
    yield conn, int(pid)
    conn.close()


class TestDetectTaxBasis:
    """C3/C6：表头明确文本识别——1/2/3/4/5/7 号场景的识别层。"""

    def test_explicit_excluded(self):
        basis, reason = detect_tax_basis("编码 名称 不含税单价 合价")
        assert basis == "excluded"
        assert "不含税" in reason

    def test_explicit_included(self):
        basis, reason = detect_tax_basis("编码 名称 含税单价 合价")
        assert basis == "included"

    def test_price_tax_total(self):
        basis, reason = detect_tax_basis("名称 金额 价税合计")
        assert basis == "included"
        assert "价税合计" in reason

    def test_tax_amount_column_only_registers_fact(self):
        basis, reason = detect_tax_basis("编码 名称 工程量 单价 合价 税金")
        # C1/C5：独立税金列只登记事实，不得推断口径
        assert basis == "unknown"
        assert "税金" in reason

    def test_no_tax_info(self):
        basis, reason = detect_tax_basis("编码 名称 工程量 单价 合价")
        assert basis == "unknown"
        assert "无任何税信息" in reason

    def test_tax_rate_number_is_not_basis(self):
        # C5：税率 9% 不是口径依据
        basis, reason = detect_tax_basis("编码 名称 单价 合价 税率 9%")
        assert basis == "unknown"

    def test_conflicting_markers(self):
        basis, reason = detect_tax_basis("不含税单价 含税合价")
        assert basis == "unknown"
        assert "冲突" in reason


class TestComparable:
    """C4：口径不一致/未确认不得直接比差额。"""

    def test_same_basis_ok(self):
        ok, reason = tax_basis_comparable("included", "included")
        assert ok

    def test_mismatch_incomparable(self):
        ok, reason = tax_basis_comparable("included", "excluded")
        assert not ok
        assert "INCOMPARABLE" in reason

    def test_unknown_pending(self):
        ok, reason = tax_basis_comparable("unknown", "included")
        assert not ok
        assert "PENDING" in reason


class TestHumanOverride:
    """C6-9：人工改判——理由必填、Evidence、机器不覆盖。"""

    def test_override_requires_reason(self, project_db):
        conn, pid = project_db
        sheet_id = conn.execute("SELECT id FROM raw_sheets").fetchone()["id"]
        with pytest.raises(ValueError, match="理由"):
            set_sheet_tax_basis(conn, pid, int(sheet_id), "excluded")
        with pytest.raises(ValueError, match="未知的税口径"):
            set_sheet_tax_basis(conn, pid, int(sheet_id), "vat_free", reason="x")

    def test_override_writes_evidence_and_row(self, project_db):
        conn, pid = project_db
        sheet_id = int(conn.execute("SELECT id FROM raw_sheets").fetchone()["id"])
        result = set_sheet_tax_basis(
            conn, pid, sheet_id, "excluded", reason="表头写明不含税单价，与合同核对一致"
        )
        assert result["after"] == "excluded"
        row = conn.execute(
            """SELECT tax_basis, tax_basis_source, tax_basis_reason
               FROM raw_sheets WHERE id=?""", (sheet_id,)
        ).fetchone()
        assert row["tax_basis"] == "excluded"
        assert row["tax_basis_source"] == "human"
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert "sheet_tax_basis" in kinds

    def test_foreign_sheet_rejected(self, project_db):
        conn, pid = project_db
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            set_sheet_tax_basis(conn, pid, 999999, "included", reason="x")


class TestRunContractInvalidation:
    """C6-10：税口径变化使旧 Run 失效（签名变化）。"""

    def test_basis_change_invalidates_run(self, project_db):
        from jiadun.core.contracts import run_contract

        conn, pid = project_db
        sheet_id = int(conn.execute("SELECT id FROM raw_sheets").fetchone()["id"])
        first = run_contract.ensure_run_contract(conn, pid)
        set_sheet_tax_basis(conn, pid, sheet_id, "included", reason="合同约定含税")
        second = run_contract.ensure_run_contract(conn, pid)
        assert first.signature != second.signature
        old_row = conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE run_id=?",
            (first.run_id,),
        ).fetchone()
        assert old_row["invalidated_at"] is not None


class TestCarryForwardIncludesTaxBasis:
    """重解析结转应携带人工税口径（与 sheet_status/list_kind 同规则）。"""

    def test_tax_basis_carried(self, project_db, tmp_path):
        from jiadun.core.engine import sheet_inventory

        conn, pid = project_db
        sheet_id = int(conn.execute("SELECT id FROM raw_sheets").fetchone()["id"])
        set_sheet_tax_basis(conn, pid, sheet_id, "excluded", reason="表头注明不含税")
        fid = int(conn.execute(
            "SELECT file_id FROM parse_batches").fetchone()["file_id"])
        with conn:
            cur = conn.execute(
                """INSERT INTO parse_batches(file_id, parser, parsed_at, status,
                   stats_json) VALUES (?,?,?,?,?)""",
                (fid, "pipeline", "2026-09-07T23:30:00", "ok", "{}"),
            )
            new_batch = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                   n_rows, n_cols) VALUES (?,?,?,?,?)""",
                (new_batch, 1, "分部分项清单", 50, 8),
            )
        summary = sheet_inventory.carry_forward_sheet_decisions(
            conn, pid, fid, new_batch
        )
        # 旧 Sheet 无 raw_cells、新 Sheet 也无 → 空摘要一致 → 结转
        assert summary["carried"] == 1
        row = conn.execute(
            """SELECT tax_basis, tax_basis_source FROM raw_sheets
               WHERE batch_id=?""", (new_batch,),
        ).fetchone()
        assert row["tax_basis"] == "excluded"
        assert row["tax_basis_source"] == "human"
