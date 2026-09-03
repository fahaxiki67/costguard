"""对上控制基准候选测试（阶段 C-2）。

宪章第六节：reference / control_candidate / settlement_result 三角色分层、
supersedes 显式替代、五态输出（PASS/FAIL/PENDING/INCOMPARABLE/
CONTROL_CONFLICT）。登记即候选；只有人工确认的基准参与比较；
维度不同不强行比较；超出只提示金额不作违规认定。
"""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jiadun.core import control_baselines as cb
from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    assert migrations.LATEST_SCHEMA_VERSION == 50
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t',1,'/t','2026')"
        ).lastrowid
    yield conn, pid, tmp_path
    conn.close()


def _register(
    conn,
    pid,
    *,
    title="终审报告-审定金额",
    amount="1000000.00",
    currency="CNY",
    tax_basis="含税总价",
    scope="本项目全部对上结算（含变更）",
    status=None,
    **kwargs,
):
    baseline_id = cb.register_baseline(
        conn, pid, title=title, amount=amount, currency=currency,
        tax_basis=tax_basis, scope_descriptor=scope, **kwargs,
    )
    if status is not None:
        cb.set_baseline_review(conn, pid, baseline_id, status, reason="测试流转")
    return baseline_id


SETTLE = dict(
    currency="CNY", tax_basis="含税总价", scope_descriptor="本项目全部对上结算（含变更）",
)


def _row(
    row_id,
    *,
    role=cb.ROLE_CONTROL_CANDIDATE,
    status=cb.REVIEW_CONFIRMED,
    amount="1000000.00",
    currency="CNY",
    tax_basis="含税总价",
    scope="本项目全部对上结算（含变更）",
    supersedes=None,
):
    return {
        "id": row_id, "role": role, "title": f"基准{row_id}", "amount": amount,
        "currency": currency, "tax_basis": tax_basis, "scope_descriptor": scope,
        "review_status": status, "supersedes_id": supersedes, "evidence_id": None,
    }


class TestRegistration:
    def test_register_creates_candidate_with_evidence(self, db):
        conn, pid, _ = db
        baseline_id = _register(conn, pid)
        row = conn.execute(
            "SELECT review_status, reviewed_at, amount, role FROM control_baselines WHERE id=?",
            (baseline_id,),
        ).fetchone()
        assert row["review_status"] == "candidate"
        assert row["reviewed_at"] is None
        assert row["amount"] == "1000000.00"
        assert row["role"] == "control_candidate"
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE kind='control_baseline_registered'"
        ).fetchone()
        assert ev["n"] == 1

    @pytest.mark.parametrize("bad", [0, "-5", "abc", None, 1.5, ""])
    def test_rejects_invalid_amounts(self, db, bad):
        conn, pid, _ = db
        with pytest.raises(ValueError):
            cb.register_baseline(
                conn, pid, title="t", amount=bad, scope_descriptor="范围",
            )

    def test_requires_title_and_scope(self, db):
        conn, pid, _ = db
        with pytest.raises(ValueError, match="基准名称"):
            cb.register_baseline(conn, pid, title="  ", amount="1", scope_descriptor="范围")
        with pytest.raises(ValueError, match="范围说明"):
            cb.register_baseline(conn, pid, title="t", amount="1", scope_descriptor=" ")

    def test_unknown_role(self, db):
        conn, pid, _ = db
        with pytest.raises(ValueError, match="角色"):
            cb.register_baseline(
                conn, pid, title="t", amount="1", scope_descriptor="范围", role="boss",
            )

    def test_supersedes_target_must_exist_in_project(self, db, tmp_path):
        conn, pid, _ = db
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            _register(conn, pid, supersedes_id=999)
        # 他项目的基准不可被本项目替代
        with conn:
            pid2 = conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t2',1,'/t2','2026')"
            ).lastrowid
        foreign = _register(conn, pid2)
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            _register(conn, pid, supersedes_id=foreign)

    def test_supersedes_requires_same_role_and_single_chain(self, db):
        conn, pid, _ = db
        first = _register(conn, pid)
        second = _register(conn, pid, title="终审报告-修订版", supersedes_id=first)
        with pytest.raises(ValueError, match="已被基准"):
            _register(conn, pid, title="终审报告-再修订", supersedes_id=first)
        with pytest.raises(ValueError, match="同角色"):
            cb.register_baseline(
                conn, pid, title="参考资料", amount="1", scope_descriptor="范围",
                role=cb.ROLE_REFERENCE, supersedes_id=second,
            )

    def test_file_must_belong_to_project(self, db):
        conn, pid, _ = db
        with pytest.raises(ValueError, match="不属于当前项目"):
            _register(conn, pid, file_id=12345)


class TestReviewLifecycle:
    def test_confirm_records_reviewer_and_evidence(self, db):
        conn, pid, _ = db
        baseline_id = _register(conn, pid)
        result = cb.set_baseline_review(
            conn, pid, baseline_id, "confirmed", reviewed_by="tester", reason="与终审报告核对一致",
        )
        assert result["before"] == "candidate" and result["after"] == "confirmed"
        row = conn.execute(
            "SELECT review_status, reviewed_by FROM control_baselines WHERE id=?", (baseline_id,)
        ).fetchone()
        assert row["review_status"] == "confirmed" and row["reviewed_by"] == "tester"
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE kind='control_baseline_review'"
        ).fetchone()
        assert ev["n"] == 1

    def test_overturn_requires_reason(self, db):
        conn, pid, _ = db
        baseline_id = _register(conn, pid, status="confirmed")
        with pytest.raises(ValueError, match="理由"):
            cb.set_baseline_review(conn, pid, baseline_id, "rejected")
        cb.set_baseline_review(conn, pid, baseline_id, "rejected", reason="报告版本作废")

    def test_review_updates_fields_with_audit(self, db):
        conn, pid, _ = db
        baseline_id = _register(conn, pid, currency="unknown", tax_basis="unknown")
        # unknown → 具体值属于补全，允许不带理由
        result = cb.set_baseline_review(
            conn, pid, baseline_id, "confirmed",
            updates={"currency": "CNY", "tax_basis": "含税总价"},
        )
        assert result["field_changes"]["currency"] == ["unknown", "CNY"]
        # 改写已声明值必须留理由
        with pytest.raises(ValueError, match="理由"):
            cb.set_baseline_review(
                conn, pid, baseline_id, "confirmed", updates={"tax_basis": "不含税"},
            )
        cb.set_baseline_review(
            conn, pid, baseline_id, "confirmed", reason="税口径按补充协议更正",
            updates={"tax_basis": "不含税"},
        )
        row = conn.execute(
            "SELECT currency, tax_basis FROM control_baselines WHERE id=?", (baseline_id,)
        ).fetchone()
        assert row["currency"] == "CNY" and row["tax_basis"] == "不含税"

    def test_amount_and_supersedes_immutable_via_review(self, db):
        conn, pid, _ = db
        baseline_id = _register(conn, pid)
        with pytest.raises(ValueError, match="不可通过复核修改"):
            cb.set_baseline_review(conn, pid, baseline_id, "confirmed", updates={"amount": "2"})
        with pytest.raises(ValueError, match="不可通过复核修改"):
            cb.set_baseline_review(conn, pid, baseline_id, "confirmed", updates={"supersedes_id": 1})

    def test_invalid_decision_and_foreign_baseline(self, db):
        conn, pid, _ = db
        with pytest.raises(ValueError, match="未知的确认决定"):
            cb.set_baseline_review(conn, pid, 1, "auto_confirm")
        baseline_id = _register(conn, pid)
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            cb.set_baseline_review(conn, pid, baseline_id + 999, "confirmed")


class TestEvaluation:
    def test_no_baselines_is_not_available(self):
        result = cb.evaluate_baselines([], cb.SettlementSide("100", **SETTLE))
        assert result.status == cb.CONTROL_NOT_AVAILABLE

    def test_reference_only_is_not_available_but_listed(self):
        result = cb.evaluate_baselines(
            [_row(1, role=cb.ROLE_REFERENCE), _row(2, role=cb.ROLE_SETTLEMENT_RESULT)],
            cb.SettlementSide("100", **SETTLE),
        )
        assert result.status == cb.CONTROL_NOT_AVAILABLE
        assert len(result.references) == 2
        assert not result.items

    def test_candidate_is_pending(self):
        result = cb.evaluate_baselines(
            [_row(1, status=cb.REVIEW_CANDIDATE)], cb.SettlementSide("100", **SETTLE),
        )
        assert result.status == cb.CONTROL_PENDING
        assert result.items[0]["status"] == cb.CONTROL_PENDING
        assert "未经人工确认" in result.items[0]["reasons"][0]

    def test_missing_settlement_amount_is_pending(self):
        result = cb.evaluate_baselines(
            [_row(1)], cb.SettlementSide(None, **SETTLE),
        )
        assert result.status == cb.CONTROL_PENDING
        assert "结算侧金额缺失" in result.items[0]["reasons"][0]

    def test_settlement_within_baseline_passes(self):
        result = cb.evaluate_baselines(
            [_row(1)], cb.SettlementSide("999999.99", **SETTLE),
        )
        assert result.status == cb.CONTROL_PASS
        assert "差额 0.01 元" in result.items[0]["message"]

    def test_settlement_equal_passes(self):
        result = cb.evaluate_baselines(
            [_row(1)], cb.SettlementSide("1000000.00", **SETTLE),
        )
        assert result.status == cb.CONTROL_PASS

    def test_settlement_exceeding_fails_with_exact_overage(self):
        result = cb.evaluate_baselines(
            [_row(1)], cb.SettlementSide("1000250.00", **SETTLE),
        )
        assert result.status == cb.CONTROL_FAIL
        assert "高 250.00 元" in result.items[0]["message"]
        # 只提示超出，不作违规/责任认定
        assert "不构成违规或责任认定" in result.items[0]["message"]

    @pytest.mark.parametrize(
        "baseline_kw,reason_part",
        [
            ({"currency": "unknown"}, "币种未声明（基准侧）"),
            ({"currency": "USD"}, "币种不同"),
            ({"tax_basis": "unknown"}, "税口径未声明（基准侧）"),
            ({"tax_basis": "不含税建安费"}, "税口径不同"),
            ({"scope": "仅主体结构"}, "范围不同"),
            ({"scope": ""}, "范围未声明（基准侧）"),
        ],
    )
    def test_dimension_mismatch_is_incomparable(self, baseline_kw, reason_part):
        row = _row(1, **baseline_kw)
        result = cb.evaluate_baselines([row], cb.SettlementSide("100", **SETTLE))
        assert result.status == cb.CONTROL_INCOMPARABLE
        assert any(reason_part in r for r in result.items[0]["reasons"])

    def test_settlement_side_unknown_dimension_is_incomparable(self):
        result = cb.evaluate_baselines(
            [_row(1)],
            cb.SettlementSide("100", currency="unknown", tax_basis="含税总价",
                              scope_descriptor="本项目全部对上结算（含变更）"),
        )
        assert result.status == cb.CONTROL_INCOMPARABLE
        assert any("币种未声明（结算侧）" in r for r in result.items[0]["reasons"])

    def test_agreeing_baselines_pass(self):
        result = cb.evaluate_baselines(
            [_row(1, amount="1000000.00"), _row(2, amount="1000000.0")],
            cb.SettlementSide("100", **SETTLE),
        )
        assert result.status == cb.CONTROL_PASS

    def test_conflicting_confirmed_baselines(self):
        result = cb.evaluate_baselines(
            [_row(1, amount="1000000.00"), _row(2, amount="980000.00")],
            cb.SettlementSide("100", **SETTLE),
        )
        assert result.status == cb.CONTROL_CONFLICT
        assert all(item["status"] == cb.CONTROL_CONFLICT for item in result.items)
        assert "金额冲突" in result.message

    def test_confirmed_supersedes_resolves_conflict(self):
        rows = [
            _row(1, amount="980000.00"),
            _row(2, amount="980000.00"),
            _row(3, amount="1000000.00", supersedes=1),
        ]
        result = cb.evaluate_baselines(rows, cb.SettlementSide("100", **SETTLE))
        # 基准 1 被确认的基准 3 替代退出；2 与 3 金额不同仍冲突
        assert result.status == cb.CONTROL_CONFLICT
        excluded_ids = {item["baseline_id"] for item in result.excluded}
        assert 1 in excluded_ids

        agree = [
            _row(1, amount="980000.00"),
            _row(2, amount="1000000.00", supersedes=1),
        ]
        result2 = cb.evaluate_baselines(agree, cb.SettlementSide("100", **SETTLE))
        assert result2.status == cb.CONTROL_PASS
        assert {item["baseline_id"] for item in result2.excluded} == {1}

    def test_pending_superseder_keeps_old_active_with_note(self):
        rows = [
            _row(1, amount="1000000.00"),
            _row(2, amount="980000.00", status=cb.REVIEW_CANDIDATE, supersedes=1),
        ]
        result = cb.evaluate_baselines(rows, cb.SettlementSide("1000500.00", **SETTLE))
        # 基准 1 仍参与（替代者未确认）并判定超出；基准 2 未确认 → pending
        assert result.status == cb.CONTROL_FAIL
        item1 = next(i for i in result.items if i["baseline_id"] == 1)
        assert any("未确认的替代版本" in r for r in item1["reasons"])
        assert item1["status"] == cb.CONTROL_FAIL

    def test_rejected_excluded_and_all_rejected_is_not_available(self):
        result = cb.evaluate_baselines(
            [_row(1, status=cb.REVIEW_REJECTED)], cb.SettlementSide("100", **SETTLE),
        )
        assert result.status == cb.CONTROL_NOT_AVAILABLE
        assert result.excluded[0]["reason"].startswith("人工拒绝")

    def test_fail_not_masked_by_incomparable(self):
        rows = [
            _row(1, amount="1000000.00"),
            _row(2, amount="500000.00", scope="仅安装工程"),
        ]
        result = cb.evaluate_baselines(rows, cb.SettlementSide("1000250.00", **SETTLE))
        assert result.status == cb.CONTROL_FAIL
        statuses = {item["baseline_id"]: item["status"] for item in result.items}
        assert statuses[1] == cb.CONTROL_FAIL
        assert statuses[2] == cb.CONTROL_INCOMPARABLE

    def test_deterministic_regardless_of_input_order(self):
        rows = [_row(1, amount="980000.00"), _row(2, amount="1000000.00", supersedes=1)]
        settle = cb.SettlementSide("100", **SETTLE)
        forward = cb.evaluate_baselines(list(reversed(rows)), settle).as_dict()
        backward = cb.evaluate_baselines(rows, settle).as_dict()
        assert forward == backward

    @given(
        baseline=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
        settlement=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
    )
    def test_single_confirmed_baseline_pass_fail_boundary(self, baseline, settlement):
        row = _row(1, amount=str(baseline))
        result = cb.evaluate_baselines([row], cb.SettlementSide(str(settlement), **SETTLE))
        expected = cb.CONTROL_PASS if settlement <= baseline else cb.CONTROL_FAIL
        assert result.status == expected, (baseline, settlement, result.as_dict())
        if settlement > baseline:
            assert f"高 {settlement - baseline} 元" in result.items[0]["message"]

    @given(
        amount_a=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2),
        amount_b=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2),
        settlement=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2),
    )
    def test_two_confirmed_statuses_are_total(self, amount_a, amount_b, settlement):
        rows = [_row(1, amount=str(amount_a)), _row(2, amount=str(amount_b))]
        result = cb.evaluate_baselines(rows, cb.SettlementSide(str(settlement), **SETTLE))
        if amount_a != amount_b:
            assert result.status == cb.CONTROL_CONFLICT
        else:
            expected = cb.CONTROL_PASS if settlement <= amount_a else cb.CONTROL_FAIL
            assert result.status == expected


class TestEvaluateProject:
    def test_project_evaluation_and_evidence(self, db):
        conn, pid, _ = db
        _register(conn, pid, status="confirmed")
        result = cb.evaluate_project_control(
            conn, pid, cb.SettlementSide("1000100.00", **SETTLE), record_evidence=True,
        )
        assert result.status == cb.CONTROL_FAIL
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE kind='control_baseline_evaluation'"
        ).fetchone()
        assert ev["n"] == 1

    def test_project_evaluation_without_evidence_by_default(self, db):
        conn, pid, _ = db
        _register(conn, pid, status="confirmed")
        result = cb.evaluate_project_control(conn, pid, cb.SettlementSide("100", **SETTLE))
        assert result.status == cb.CONTROL_PASS
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE kind='control_baseline_evaluation'"
        ).fetchone()
        assert ev["n"] == 0


class TestRunContractIntegration:
    def test_payload_carries_baselines_with_status(self, db):
        conn, pid, _ = db
        _register(conn, pid, title="终审A")
        payload = run_contract.ensure_run_contract(conn, pid)
        baselines = payload.components["control_baselines"]
        assert len(baselines) == 1
        assert baselines[0]["review_status"] == "candidate"
        assert baselines[0]["amount"] == "1000000.00"
        summary = payload.components["control_baseline_summary"]
        assert summary["roles"]["control_candidate"] == 1
        assert summary["review_status"]["candidate"] == 1

    def test_rejected_excluded_and_roles_summarized(self, db):
        conn, pid, _ = db
        rejected = _register(conn, pid, title="作废终审")
        cb.set_baseline_review(conn, pid, rejected, "rejected", reason="版本作废")
        _register(conn, pid, title="参考资料", amount="1", scope="范围",
                  role=cb.ROLE_REFERENCE)
        confirmed = _register(conn, pid, title="终审A")
        cb.set_baseline_review(conn, pid, confirmed, "confirmed")
        payload = run_contract.ensure_run_contract(conn, pid)
        baselines = payload.components["control_baselines"]
        titles = {item["title"] for item in baselines}
        assert "作废终审" not in titles
        summary = payload.components["control_baseline_summary"]
        assert summary["roles"] == {"control_candidate": 1, "reference": 1, "settlement_result": 0}
        assert summary["review_status"]["confirmed"] == 1
        assert summary["review_status"]["candidate"] == 1
        assert summary["review_status"]["rejected"] == 0  # 被拒基准不进入载荷
