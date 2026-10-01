"""工程量核对（quantity ledger）反例测试。

先写反例再实现：单位守恒、合同/单位/业务期号隔离、累计快照、修订替代、
楼栋范围、口径隔离、缺失不补 0、跨项目阻断、Run Contract 失效、v56 迁移。
所有断言只依赖 quantity_control 的公开 API 与数据库事实，不依赖实现细节。
"""
import json
from decimal import Decimal

import pytest

from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations
from jiadun.core.engine import quantity_control as qc

D = Decimal


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "project.db"
    migrations.migrate(path, tmp_path / "backups")
    conn = migrations.connect(path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name,schema_version,workspace_path,created_at) VALUES (?,?,?,?)",
            ("工程量核对", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(pid)
    conn.close()


@pytest.fixture()
def db2(tmp_path):
    path = tmp_path / "other.db"
    migrations.migrate(path, tmp_path / "backups2")
    conn = migrations.connect(path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name,schema_version,workspace_path,created_at) VALUES (?,?,?,?)",
            ("另一个项目", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(pid)
    conn.close()


def ctx(conn, pid, *, direction="downward", contract_key="C-A", business=1,
        unit="某劳务公司", doc_kind="settlement", mode="incremental",
        work_scope="浇筑劳务", building="", building_status="pending",
        supersedes=None, file_id=None):
    return qc.create_period_context(
        conn, pid, direction=direction, contract_key=contract_key,
        business_period_no=business, unit_name=unit, doc_kind=doc_kind,
        amount_mode=mode, work_scope=work_scope, building_scope=building,
        building_status=building_status, supersedes_period_id=supersedes,
        source_file_id=file_id, actor="tester", reason="测试登记上下文")


def line(conn, period_id, *, code="AA001", name="C30混凝土", feature="",
         unit="m3", qty="100", row=None):
    flags = json.dumps({"row": row}) if row is not None else "{}"
    return conn.execute(
        "INSERT INTO line_items(period_id,sheet_id,code,name,feature,unit,quantity,flags_json)"
        " VALUES (?,NULL,?,?,?,?,?,?)",
        (period_id, code, name, feature, unit, qty, flags)).lastrowid


def confirm_ctx(conn, pid, context_id, reason="与原件核对一致"):
    return qc.confirm_period_context(conn, pid, context_id, actor="tester", reason=reason)


def line_building(conn, pid, line_id, building, reason="楼栋按明细确认"):
    qc.set_line_context(conn, pid, line_id, building=building, actor="tester", reason=reason)
    qc.confirm_line_context(conn, pid, line_id, actor="tester", reason=reason)


def line_scope(conn, pid, line_id, work_scope, reason="行级工作口径确认"):
    qc.set_line_context(conn, pid, line_id, work_scope=work_scope,
                        actor="tester", reason=reason)
    qc.confirm_line_context(conn, pid, line_id, actor="tester", reason=reason)


def make_file(conn, pid, name):
    return conn.execute(
        """INSERT INTO source_files(project_id, original_path, stored_path,
           original_name, sha256, size_bytes, file_type, imported_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (pid, f'/f/{name}', f'/s/{name}', name, name, 1, 'xlsx',
         '2026')).lastrowid


def item_of(ledger, *, direction="downward", doc_kind="settlement",
            contract_key="C-A", work_scope="浇筑劳务", code="AA001"):
    for it in ledger["items"]:
        if (it["direction"] == direction and it["doc_kind"] == doc_kind
                and it["contract_key"] == contract_key and it["work_scope"] == work_scope
                and it.get("code") == code):
            return it
    return None


def comparison_of(ledger, *, direction="downward", contract_key="C-A",
                  work_scope="浇筑劳务", code="AA001", building=None):
    for cmp in ledger["comparisons"]:
        if (cmp["direction"] == direction and cmp["contract_key"] == contract_key
                and cmp["work_scope"] == work_scope and cmp.get("code") == code
                and cmp["building"] == building):
            return cmp
    return None


def control_of(ledger, *, work_scope, building=None, code="AA001"):
    for ctl in ledger["quantity_controls"]:
        if (ctl["work_scope"] == work_scope and ctl["building"] == building
                and ctl.get("code") == code):
            return ctl
    return None


# ---------------------------------------------------------------- 单位与求和守恒

class TestUnitConservation:
    def test_one_t_plus_500_kg_is_1_5_t(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], unit="吨", qty="1")
        line(conn, cid["period_id"], unit="千克", qty="500")
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        it = item_of(ledger)
        assert it is not None
        assert D(it["quantity"]) == D("1.5")
        assert it["standard_unit"] == "t"
        # 原始行不被改写
        units = [r["unit"] for r in conn.execute(
            "SELECT unit FROM line_items ORDER BY id").fetchall()]
        assert units == ["吨", "千克"]

    def test_two_60_rows_sum_120_not_deduplicated(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], qty="60")
        line(conn, cid["period_id"], qty="60")
        confirm_ctx(conn, pid, cid["context_id"])
        it = item_of(qc.build_quantity_ledger(conn, pid))
        assert D(it["quantity"]) == D("120")
        assert len(it["sources"]) == 2

    def test_missing_quantity_is_pending_not_zero(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], qty="100")
        line(conn, cid["period_id"], qty=None)
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        it = item_of(ledger)
        # 缺失行不得补 0，也不得让缺失行参与合计；组状态必须显式 pending
        assert it["status"] != "ok"
        assert "缺失" in it["reason"]
        assert D(it["quantity"]) == D("100")  # 仅有效行合计，缺失行不折算为 0
        assert D(it["sources"][0]["standard_quantity"]) == D("100")
        assert it["sources"][1]["standard_quantity"] is None
        assert not any(s["standard_quantity"] == "0" for s in it["sources"])

    def test_kg_target_multiplier_applies_fixed_scale(self, db):
        # 人工换算 target_unit=kg：1 × 0.5（→kg）× 0.001（kg→t）= 0.0005 t
        conn, pid = db
        cid = ctx(conn, pid)
        a = line(conn, cid["period_id"], name="钢材", unit="批", qty="1")
        qc.set_line_context(conn, pid, a, standard_key="STD-钢材",
                            convert_factor="0.5", target_unit="kg",
                            convert_basis="1批=0.5kg，询价单人工确认",
                            actor="t", reason="跨量纲换算")
        qc.confirm_line_context(conn, pid, a, actor="t", reason="依据充分")
        confirm_ctx(conn, pid, cid["context_id"])
        it = next(x for x in qc.build_quantity_ledger(conn, pid)["items"]
                  if x["standard_key"] == "STD-钢材")
        assert D(it["quantity"]) == D("0.0005")
        assert it["standard_unit"] == "t"

    def test_cross_dimension_never_auto_converted(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        a = line(conn, cid["period_id"], name="墙面装修", unit="m2", qty="100")
        b = line(conn, cid["period_id"], name="墙面装修", unit="m3", qty="10")
        qc.set_line_context(conn, pid, a, standard_key="STD-墙面",
                            actor="t", reason="同一清单项")
        qc.set_line_context(conn, pid, b, standard_key="STD-墙面",
                            actor="t", reason="同一清单项")
        qc.confirm_line_context(conn, pid, a, actor="t", reason="核对")
        qc.confirm_line_context(conn, pid, b, actor="t", reason="核对")
        confirm_ctx(conn, pid, cid["context_id"])
        it = item_of(qc.build_quantity_ledger(conn, pid))
        assert it["quantity"] is None
        assert it["status"] == "incomparable"

    def test_confirmed_conversion_unifies_unknown_unit(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        a = line(conn, cid["period_id"], name="土方", unit="m3", qty="100")
        b = line(conn, cid["period_id"], name="土方", unit="方", qty="100")
        qc.set_line_context(conn, pid, a, standard_key="STD-土方",
                            actor="t", reason="统一标准键")
        qc.set_line_context(conn, pid, b, standard_key="STD-土方",
                            convert_factor="1", target_unit="m3",
                            convert_basis="1方=1m3，行业计量惯例，人工确认",
                            actor="t", reason="统一土方计量单位")
        qc.confirm_line_context(conn, pid, a, actor="t", reason="核对标准键")
        qc.confirm_line_context(conn, pid, b, actor="t", reason="依据充分")
        confirm_ctx(conn, pid, cid["context_id"])
        it = next(x for x in qc.build_quantity_ledger(conn, pid)["items"]
                  if x["standard_key"] == "STD-土方")
        assert D(it["quantity"]) == D("200")
        assert it["standard_unit"] == "m3"


# ---------------------------------------------------------------- 合同/单位/期号隔离

class TestContractIsolation:
    def test_abc_each_own_business_period_1(self, db):
        conn, pid = db
        ids = []
        for key in ("C-A", "C-B", "C-C"):
            cid = ctx(conn, pid, contract_key=key, unit="同一分包单位")
            line(conn, cid["period_id"], qty="10")
            confirm_ctx(conn, pid, cid["context_id"])
            ids.append(cid)
        # 三个独立期次行，互不复用
        period_ids = [c["period_id"] for c in ids]
        assert len(set(period_ids)) == 3
        ledger = qc.build_quantity_ledger(conn, pid)
        for key in ("C-A", "C-B", "C-C"):
            it = item_of(ledger, contract_key=key)
            assert it is not None
            assert D(it["quantity"]) == D("10")

    def test_same_unit_different_contract_not_merged(self, db):
        conn, pid = db
        for key, qty in (("C-A", "100"), ("C-B", "50")):
            cid = ctx(conn, pid, contract_key=key)
            line(conn, cid["period_id"], qty=qty)
            confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        assert D(item_of(ledger, contract_key="C-A")["quantity"]) == D("100")
        assert D(item_of(ledger, contract_key="C-B")["quantity"]) == D("50")

    def test_second_file_same_identity_stays_pending(self, db):
        conn, pid = db
        first = ctx(conn, pid, file_id=make_file(conn, pid, "第一次申报.xlsx"))
        line(conn, first["period_id"], qty="100")
        confirm_ctx(conn, pid, first["context_id"])
        second = ctx(conn, pid, file_id=make_file(conn, pid, "第二次申报.xlsx"))
        line(conn, second["period_id"], qty="70")
        # 未声明替代关系前，第二份不得确认挤进同一期
        with pytest.raises(ValueError, match="替代"):
            confirm_ctx(conn, pid, second["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        it = item_of(ledger)
        assert D(it["quantity"]) == D("100")
        assert any(p["context_id"] == second["context_id"] for p in ledger["pending_contexts"])

    def test_two_confirmed_same_identity_is_control_conflict(self, db):
        conn, pid = db
        first = ctx(conn, pid)
        line(conn, first["period_id"], qty="100")
        confirm_ctx(conn, pid, first["context_id"])
        second = ctx(conn, pid)
        line(conn, second["period_id"], qty="70")
        # 外部 SQL 绕过确认闸门后的兜底：两个有效同身份版本不得取最新导入
        conn.execute(
            "UPDATE quantity_period_context SET status='confirmed' WHERE id=?",
            (second["context_id"],))
        ledger = qc.build_quantity_ledger(conn, pid)
        assert ledger["identity_conflicts"]
        it = item_of(ledger)
        assert it is None or it["quantity"] is None


# ---------------------------------------------------------------- 模式：累计与修订

class TestCumulativeAndRevision:
    def test_cumulative_takes_latest_snapshot_only(self, db):
        conn, pid = db
        p1 = ctx(conn, pid, business=1, mode="cumulative")
        line(conn, p1["period_id"], qty="100")
        confirm_ctx(conn, pid, p1["context_id"])
        p2 = ctx(conn, pid, business=2, mode="cumulative")
        line(conn, p2["period_id"], qty="180")
        confirm_ctx(conn, pid, p2["context_id"])
        it = item_of(qc.build_quantity_ledger(conn, pid))
        assert D(it["quantity"]) == D("180")
        counted = [s for s in it["sources"] if s["counted"]]
        assert len(counted) == 1 and D(counted[0]["standard_quantity"]) == D("180")

    def test_cumulative_missing_item_not_backfilled(self, db):
        conn, pid = db
        p1 = ctx(conn, pid, business=1, mode="cumulative")
        line(conn, p1["period_id"], name="混凝土", code="CON", qty="100")
        line(conn, p1["period_id"], name="钢筋", code="REB", qty="50")
        confirm_ctx(conn, pid, p1["context_id"])
        p2 = ctx(conn, pid, business=2, mode="cumulative")
        line(conn, p2["period_id"], name="混凝土", code="CON", qty="180")
        confirm_ctx(conn, pid, p2["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        concrete = item_of(ledger, code="CON")
        assert D(concrete["quantity"]) == D("180")
        rebar = item_of(ledger, code="REB")
        assert rebar["quantity"] is None
        assert rebar["status"] != "ok"
        assert any("回填" in w or "缺少" in w for w in [rebar["reason"]])

    def test_revision_supersedes_keeps_old_rows_and_evidence(self, db):
        conn, pid = db
        original = ctx(conn, pid)
        line(conn, original["period_id"], qty="100")
        confirm_ctx(conn, pid, original["context_id"])
        evid_before = conn.execute(
            "SELECT COUNT(*) FROM evidence").fetchone()[0]
        revision = ctx(conn, pid, supersedes=original["period_id"])
        line(conn, revision["period_id"], qty="120")
        confirm_ctx(conn, pid, revision["context_id"], reason="审定修订版替代原申报")
        # 旧版退出有效核量但不删除行与证据
        contexts = {c["context_id"]: c for c in qc.list_period_contexts(conn, pid)}
        assert contexts[original["context_id"]]["status"] == "superseded"
        assert contexts[revision["context_id"]]["status"] == "confirmed"
        assert conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] >= evid_before
        it = item_of(qc.build_quantity_ledger(conn, pid))
        assert D(it["quantity"]) == D("120")

    def test_mixed_mode_within_contract_is_pending(self, db):
        conn, pid = db
        p1 = ctx(conn, pid, business=1, mode="incremental")
        line(conn, p1["period_id"], qty="100")
        confirm_ctx(conn, pid, p1["context_id"])
        p2 = ctx(conn, pid, business=2, mode="cumulative")
        line(conn, p2["period_id"], qty="180")
        confirm_ctx(conn, pid, p2["context_id"])
        it = item_of(qc.build_quantity_ledger(conn, pid))
        assert it["status"] != "ok"
        assert "口径" in it["reason"] or "模式" in it["reason"]


# ---------------------------------------------------------------- 楼栋与口径

class TestBuildingAndScope:
    def test_full_project_and_building_delta(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope="商品混凝土供应")
        c1 = line(conn, contract["period_id"], qty="1000")
        c2 = line(conn, contract["period_id"], qty="800")
        line_building(conn, pid, c1, "1号楼")
        line_building(conn, pid, c2, "2号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        settled = ctx(conn, pid, doc_kind="settlement", direction="upward",
                      work_scope="商品混凝土供应")
        s1 = line(conn, settled["period_id"], qty="1100")
        s2 = line(conn, settled["period_id"], qty="600")
        line_building(conn, pid, s1, "1号楼")
        line_building(conn, pid, s2, "2号楼")
        confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        # 全项目：合同 1800 vs 已结算 1700
        all_cmp = comparison_of(ledger, direction="upward",
                                work_scope="商品混凝土供应", building=None)
        assert D(all_cmp["contract_quantity"]) == D("1800")
        assert D(all_cmp["settlement_quantity"]) == D("1700")
        assert D(all_cmp["delta"]) == D("-100")
        # 1号楼：合同 1000 vs 已结算 1100，超 100
        b1 = comparison_of(ledger, direction="upward",
                           work_scope="商品混凝土供应", building="1号楼")
        assert D(b1["contract_quantity"]) == D("1000")
        assert D(b1["settlement_quantity"]) == D("1100")
        assert D(b1["delta"]) == D("100")
        assert b1["status"] == "FAIL"
        b2 = comparison_of(ledger, direction="upward",
                           work_scope="商品混凝土供应", building="2号楼")
        assert b2["status"] == "PASS"
        # 合同侧分项的楼栋拆分
        con_it = item_of(ledger, direction="upward", doc_kind="contract",
                         work_scope="商品混凝土供应")
        assert D(con_it["buildings"]["1号楼"]["quantity"]) == D("1000")
        assert D(con_it["buildings"]["__all__"]["quantity"]) == D("1800")

    def test_missing_building_baseline_is_pending(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward")
        line(conn, contract["period_id"], qty="1000")  # 无楼栋信息
        confirm_ctx(conn, pid, contract["context_id"])
        settled = ctx(conn, pid, doc_kind="settlement", direction="upward")
        s1 = line(conn, settled["period_id"], qty="1100")
        line_building(conn, pid, s1, "1号楼")
        confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = comparison_of(ledger, direction="upward", building="1号楼")
        assert b1["contract_quantity"] is None
        assert b1["status"] == "PENDING"
        all_cmp = comparison_of(ledger, direction="upward", building=None)
        assert D(all_cmp["delta"]) == D("100")

    def test_multi_building_scope_never_copied_or_averaged(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward")
        c1 = line(conn, contract["period_id"], qty="1000")
        line_building(conn, pid, c1, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        settled = ctx(conn, pid, doc_kind="settlement", direction="upward",
                      building="1-3号楼", building_status="confirmed")
        line(conn, settled["period_id"], qty="1800")
        confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = comparison_of(ledger, direction="upward", building="1号楼")
        # 不得把 1-3号楼合计复制/平均到 1号楼
        assert b1["settlement_quantity"] is None
        assert b1["status"] == "PENDING"
        all_cmp = comparison_of(ledger, direction="upward", building=None)
        assert D(all_cmp["settlement_quantity"]) == D("1800")
        # "1-3号楼" 是独立楼栋键
        b13 = comparison_of(ledger, direction="upward", building="1-3号楼")
        assert D(b13["settlement_quantity"]) == D("1800")

    def test_material_and_labor_scopes_never_summed(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope="材料供应")
        line(conn, contract["period_id"], qty="100")
        confirm_ctx(conn, pid, contract["context_id"])
        # 同一份对下结算单内：材料行用期次口径，劳务行用行级确认口径
        settled = ctx(conn, pid, doc_kind="settlement", direction="upward",
                      work_scope="材料供应")
        line(conn, settled["period_id"], qty="100")
        labor_row = line(conn, settled["period_id"], qty="100")
        line_scope(conn, pid, labor_row, "浇筑劳务")
        confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        assert D(item_of(ledger, direction="upward", doc_kind="settlement",
                         work_scope="材料供应")["quantity"]) == D("100")
        labor = item_of(ledger, direction="upward", doc_kind="settlement",
                        work_scope="浇筑劳务")
        assert D(labor["quantity"]) == D("100")
        mat_cmp = comparison_of(ledger, direction="upward", work_scope="材料供应")
        assert mat_cmp["status"] == "PASS"
        labor_cmp = comparison_of(ledger, direction="upward", work_scope="浇筑劳务")
        assert labor_cmp["status"] == "PENDING"  # 合同侧无劳务基准
        # 不存在 200 的混合项
        assert all(D(it["quantity"]) != D("200") for it in ledger["items"] if it["quantity"])

    def test_building_candidates_traceable(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], name="1号楼混凝土", qty="10", row=5)
        line(conn, cid["period_id"], name="2号楼混凝土", qty="20", row=6)
        candidates = qc.suggest_building_candidates(conn, pid)
        tokens = {c["building"] for c in candidates}
        assert {"1号楼", "2号楼"} <= tokens
        one = next(c for c in candidates if c["building"] == "1号楼")
        assert one["sources"] and one["sources"][0]["row"] == 5
        assert one["sources"][0]["line_item_id"] > 0


# ---------------------------------------------------------------- 人工操作纪律

class TestHumanDiscipline:
    def test_create_and_confirm_require_reason(self, db):
        conn, pid = db
        with pytest.raises(ValueError, match="原因"):
            qc.create_period_context(
                conn, pid, direction="downward", contract_key="C-A",
                business_period_no=1, actor="t", reason="  ")
        cid = ctx(conn, pid)
        with pytest.raises(ValueError, match="原因"):
            qc.confirm_period_context(conn, pid, cid["context_id"], actor="t", reason="")

    def test_context_writes_audit_and_evidence(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        confirm_ctx(conn, pid, cid["context_id"])
        actions = {r["action"] for r in conn.execute(
            "SELECT action FROM audit_log WHERE project_id=?", (pid,)).fetchall()}
        assert any("quantity" in a for a in actions)
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE project_id=?", (pid,)).fetchone()[0] >= 2

    def test_legacy_attach_is_pending_until_confirmed(self, db):
        conn, pid = db
        with conn:
            legacy_pid = conn.execute(
                "INSERT INTO settlement_periods(project_id,period_no,title,direction)"
                " VALUES (?,?,?,'downward')", (pid, 1, "旧资料第1期")).lastrowid
        line(conn, legacy_pid, qty="100")
        context_id = qc.attach_period_context(
            conn, pid, legacy_pid, contract_key="C-A", business_period_no=1,
            unit_name="某劳务公司", work_scope="浇筑劳务",
            actor="t", reason="补登旧资料上下文")
        contexts = {c["context_id"]: c for c in qc.list_period_contexts(conn, pid)}
        assert contexts[context_id]["status"] == "pending"
        ledger = qc.build_quantity_ledger(conn, pid)
        assert item_of(ledger) is None  # 未确认不参与有效核量
        qc.confirm_period_context(conn, pid, context_id, actor="t", reason="核对原件")
        assert D(item_of(qc.build_quantity_ledger(conn, pid))["quantity"]) == D("100")

    def test_cross_project_member_and_supersedes_blocked(self, db):
        # 同一数据库中建两个项目：跨项目校验针对 project 边界，而非碰巧
        # 同号的两份库文件。
        conn, pid = db
        with conn:
            pid_b = conn.execute(
                "INSERT INTO projects(name,schema_schema_dummy,workspace_path,created_at)"
                " VALUES ('B项目',1,'/b','2026')").lastrowid if False else conn.execute(
                "INSERT INTO projects(name,schema_version,workspace_path,created_at)"
                " VALUES ('B项目',?,?,?)",
                (migrations.LATEST_SCHEMA_VERSION, '/b', '2026')).lastrowid
        foreign = ctx(conn, pid_b)
        with conn:
            foreign_line = line(conn, foreign["period_id"], qty="1")
        with pytest.raises(ValueError, match="项目"):
            qc.set_line_context(conn, pid, foreign_line, building="1号楼",
                                actor="t", reason="跨项目")
        with pytest.raises(ValueError, match="项目"):
            qc.create_period_context(
                conn, pid, direction="downward", contract_key="C-A",
                business_period_no=1, supersedes_period_id=foreign["period_id"],
                actor="t", reason="跨项目替代")

    def test_source_file_must_belong_to_project(self, db):
        conn, pid = db
        with conn:
            other_pid = conn.execute(
                "INSERT INTO projects(name,schema_version,workspace_path,created_at)"
                " VALUES ('邻项目',?,?,?)",
                (migrations.LATEST_SCHEMA_VERSION, '/other', '2026')).lastrowid
            with conn:
                foreign_file = conn.execute(
                    """INSERT INTO source_files(project_id, original_path, stored_path,
                       original_name, sha256, size_bytes, file_type, imported_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (other_pid, '/x', '/y', 'foreign.xlsx', 'deadbeef', 1,
                     'xlsx', '2026')).lastrowid
        with pytest.raises(ValueError, match="项目"):
            ctx(conn, pid, file_id=foreign_file)

    def test_mixed_source_period_cannot_confirm_into_single_contract(self, db):
        conn, pid = db
        with conn:
            legacy = conn.execute(
                "INSERT INTO settlement_periods(project_id,period_no,title,direction)"
                " VALUES (?,?,?,'downward')", (pid, 1, "混合来源旧期次")).lastrowid
            for sha in ("aa", "bb"):
                with conn:
                    file_id = conn.execute(
                        """INSERT INTO source_files(project_id, original_path, stored_path,
                           original_name, sha256, size_bytes, file_type, imported_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (pid, f'/f{sha}', f'/s{sha}', f'{sha}.xlsx', sha, 1,
                         'xlsx', '2026')).lastrowid
                    batch = conn.execute(
                        "INSERT INTO parse_batches(file_id, parser, parsed_at, status)"
                        " VALUES (?,?,?,?)", (file_id, 'xlsx', '2026', 'ok')).lastrowid
                    sheet = conn.execute(
                        "INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,"
                        " n_rows, n_cols) VALUES (?,?,?,?,?)",
                        (batch, 0, 'S', 2, 2)).lastrowid
                    conn.execute(
                        "INSERT INTO line_items(period_id, sheet_id, name, unit,"
                        " quantity) VALUES (?,?,?,?,?)",
                        (legacy, sheet, '混合明细', 'm3', '10'))
        context_id = qc.attach_period_context(
            conn, pid, legacy, contract_key="C-A", business_period_no=1,
            work_scope="浇筑劳务", actor="t", reason="补登")
        with pytest.raises(ValueError, match="混合来源"):
            confirm_ctx(conn, pid, context_id)

    def test_supersedes_scope_and_cycle_blocked(self, db):
        conn, pid = db
        c1 = ctx(conn, pid)
        other_scope = ctx(conn, pid, contract_key="C-B")
        with pytest.raises(ValueError, match="范围|口径"):
            ctx(conn, pid, supersedes=other_scope["period_id"])
        wrong_period = ctx(conn, pid, business=2)
        with pytest.raises(ValueError, match="期号"):
            ctx(conn, pid, supersedes=wrong_period["period_id"])
        c2 = ctx(conn, pid, supersedes=c1["period_id"])
        c3 = ctx(conn, pid, supersedes=c2["period_id"])
        with pytest.raises(ValueError, match="循环"):
            qc.set_period_context_supersedes(
                conn, pid, c1["context_id"], c3["period_id"],
                actor="t", reason="构造循环")

    def test_unconfirmed_line_params_not_used(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        a = line(conn, cid["period_id"], name="土方", unit="m3", qty="100")
        b = line(conn, cid["period_id"], name="土方", unit="方", qty="100")
        qc.set_line_context(conn, pid, a, standard_key="STD-土方",
                            actor="t", reason="登记")
        qc.set_line_context(conn, pid, b, standard_key="STD-土方",
                            convert_factor="1", target_unit="m3",
                            convert_basis="1方=1m3", actor="t", reason="登记")
        confirm_ctx(conn, pid, cid["context_id"])
        # 行上下文未确认：标准键与换算参数都不可用——不得出现 200 m3 的合并项
        ledger = qc.build_quantity_ledger(conn, pid)
        assert all(it["standard_key"] != "STD-土方" for it in ledger["items"])
        assert all(not (it["standard_unit"] == "m3" and it["quantity"]
                        and D(it["quantity"]) == D("200"))
                   for it in ledger["items"])

    def test_manual_key_does_not_mix_features(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        a = line(conn, cid["period_id"], name="钢筋", feature="HRB400", qty="10")
        b = line(conn, cid["period_id"], name="钢筋", feature="HPB300", qty="20")
        for target in (a, b):
            qc.set_line_context(conn, pid, target, standard_key="STD-钢筋",
                                actor="t", reason="同清单")
            qc.confirm_line_context(conn, pid, target, actor="t", reason="核对")
        confirm_ctx(conn, pid, cid["context_id"])
        it = next(x for x in qc.build_quantity_ledger(conn, pid)["items"]
                  if x["standard_key"] == "STD-钢筋")
        assert it["quantity"] is None
        assert it["status"] == "incomparable"

    def test_auto_mapping_requires_exact_identity(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], code="AA001", feature="C30", qty="10")
        line(conn, cid["period_id"], code="AA001", feature="C35", qty="20")
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        assert len(ledger["items"]) == 2
        assert all(D(it["quantity"]) in (D("10"), D("20")) for it in ledger["items"])


# ---------------------------------------------------------------- Run Contract 失效

class TestRunContractInvalidation:
    def test_quantity_payload_in_components(self, db):
        conn, pid = db
        ctx(conn, pid)
        comps = run_contract.build_run_contract_components(conn, pid)
        assert comps["quantity_context"]["available"] is True
        assert len(comps["quantity_context"]["quantity_contexts"]) == 1

    def test_direct_context_change_invalidates_signature(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        first = run_contract.ensure_run_contract(conn, pid)
        # 外部 SQL 直接改上下文（无审计路径）也必须使旧运行失效
        conn.execute(
            "UPDATE quantity_period_context SET status='confirmed', confirmed_reason='外部SQL'"
            " WHERE id=?", (cid["context_id"],))
        second = run_contract.ensure_run_contract(conn, pid)
        assert second.signature != first.signature
        assert first.invalidated_at is not None or run_contract.get_current_contract(
            conn, pid).run_id == second.run_id


# ---------------------------------------------------------------- 台账可序列化

class TestLedgerContract:
    def test_result_is_json_serializable(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], unit="吨", qty="1")
        confirm_ctx(conn, pid, cid["context_id"])
        contract = ctx(conn, pid, doc_kind="contract")
        line(conn, contract["period_id"], qty="2")
        confirm_ctx(conn, pid, contract["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        dumped = json.dumps(ledger, ensure_ascii=False)
        assert "quantity_controls" in dumped
        assert isinstance(ledger["items"], list)
        assert isinstance(ledger["comparisons"], list)
        assert isinstance(ledger["quantity_controls"], list)
        assert ledger["project_id"] == pid

    def test_no_pass_when_missing_or_unconfirmed(self, db):
        conn, pid = db
        # 只有结算、无合同基准：不得输出 PASS
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], qty="100")
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        cmp_all = comparison_of(ledger, building=None)
        assert cmp_all["status"] == "PENDING"
        # 完全未确认：无任何比较
        ctx(conn, pid, contract_key="C-X")
        ledger2 = qc.build_quantity_ledger(conn, pid)
        assert all(c["status"] != "PASS" for c in ledger2["comparisons"])


# ---------------------------------------------------------------- 跨方向控制：对上 vs 对下多合同

class TestCrossDirectionControls:
    """核心验收：上游单合同基准 vs 下游 A/B/C 多合同同标准项合并累计。"""

    SCOPE = "商品混凝土供应"

    def _setup(self, conn, pid, *, downstream):
        """上游合同 1号楼1000/2号楼800；downstream=[(合同,楼栋,量)]。

        每个下游合同只建一个期次上下文（同一合同的多个楼栋行归属同一
        业务期），避免把同一业务身份拆成多个版本。
        """
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        for building, qty in (("1号楼", "1000"), ("2号楼", "800")):
            row = line(conn, contract["period_id"], qty=qty)
            line_building(conn, pid, row, building)
        confirm_ctx(conn, pid, contract["context_id"])
        by_contract: dict[str, dict] = {}
        for key, building, qty in downstream:
            settled = by_contract.get(key)
            if settled is None:
                settled = by_contract[key] = ctx(
                    conn, pid, contract_key=key, direction="downward",
                    work_scope=self.SCOPE)
            row = line(conn, settled["period_id"], qty=qty)
            line_building(conn, pid, row, building)
        for settled in by_contract.values():
            confirm_ctx(conn, pid, settled["context_id"])
        return qc.build_quantity_ledger(conn, pid)

    def test_upstream_vs_downstream_abc(self, db):
        conn, pid = db
        ledger = self._setup(conn, pid, downstream=[
            ("A", "1号楼", "600"), ("A", "2号楼", "200"),
            ("B", "1号楼", "500"), ("B", "2号楼", "300"),
            ("C", "2号楼", "100"),
        ])
        # 1号楼：对下 600+500=1100 > 对上合同 1000 → 超 100
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        assert D(b1["baselines"]["upward_contract"]["quantity"]) == D("1000")
        assert D(b1["downstream_quantity"]) == D("1100")
        assert D(b1["delta_vs_contract"]) == D("100")
        assert b1["status_vs_contract"] == "FAIL"
        assert b1["status"] == "FAIL"
        contributions = {c["contract_key"]: D(c["quantity"])
                         for c in b1["contributions"]}
        assert contributions == {"A": D("600"), "B": D("500")}
        # 全项目：1700 ≤ 1800 → 不超
        all_ctl = control_of(ledger, work_scope=self.SCOPE, building=None)
        assert D(all_ctl["baselines"]["upward_contract"]["quantity"]) == D("1800")
        assert D(all_ctl["downstream_quantity"]) == D("1700")
        assert D(all_ctl["delta_vs_contract"]) == D("-100")
        assert all_ctl["status_vs_contract"] == "PASS"
        # 2号楼：600 ≤ 800
        b2 = control_of(ledger, work_scope=self.SCOPE, building="2号楼")
        assert b2["status_vs_contract"] == "PASS"
        # 同组保留两种基准；总体基于实际提供的合同基准（已结算未提供）
        assert "upward_settlement" in all_ctl["baselines"]
        assert all_ctl["status_vs_settlement"] == "PENDING"
        assert all_ctl["compared_baselines"] == ["upward_contract"]
        assert all_ctl["status"] == "PASS"

    def test_pending_downstream_blocks_pass(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        b_pending = ctx(conn, pid, contract_key="B", direction="downward",
                        work_scope=self.SCOPE)   # 未确认：另一分包 60
        line(conn, b_pending["period_id"], qty="60")
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        assert D(b1["downstream_quantity"]) == D("60")
        assert b1["status_vs_contract"] == "PENDING"
        assert b1["excluded_details"]
        assert "不可信" in b1["reason"] or "不得输出通过结论" in b1["reason"]

    def test_confirmed_missing_quantity_blocks_pass(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        b = ctx(conn, pid, contract_key="B", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, b["period_id"], qty=None)   # 已确认但缺量
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, b["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        # A 60 有效 + B 缺量已确认：不得跳过 B 输出 60/100 的通过结论
        assert b1["status_vs_contract"] == "PENDING"
        assert any("数量缺失" in m["reason"]
                   for m in b1["missing_contributions"])
        assert b1["status"] == "PENDING"

    def test_unknown_building_not_skipped_from_building_conclusion(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        b = ctx(conn, pid, contract_key="B", direction="downward",
                work_scope=self.SCOPE)   # B 数量楼栋未知
        line(conn, b["period_id"], qty="60")
        confirm_ctx(conn, pid, b["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        assert b1["status_vs_contract"] == "PENDING"
        assert any("楼栋" in m["reason"] for m in b1["missing_contributions"])

    def test_complete_120_over_100_fails(self, db):
        conn, pid = db
        # 上游基准 100；对下 A60+B60 全部确认 → 120 > 100 → FAIL
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        for key, qty in (("A", "60"), ("B", "60")):
            settled = ctx(conn, pid, contract_key=key, direction="downward",
                          work_scope=self.SCOPE)
            row = line(conn, settled["period_id"], qty=qty)
            line_building(conn, pid, row, "1号楼")
            confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        assert D(b1["downstream_quantity"]) == D("120")
        assert D(b1["delta_vs_contract"]) == D("20")
        assert b1["status_vs_contract"] == "FAIL"

    @pytest.mark.parametrize("missing", ["quantity", "unit"])
    def test_incomplete_pending_row_blocks_pass(self, db, missing):
        """真实反例（quantity-ledger-incomplete-coverage-probe.json）：

        上游 U 合同 100 + U 结算 100 确认，A 60 确认，B 同 C1混凝土/C30/
        材料口径但上下文 pending。B.quantity=None 或 B.unit=None 时旧实现
        全 PASS——缺量不等于零、缺单位不能凭精确 tuple 漏拦。
        """
        conn, pid = db
        contract = ctx(conn, pid, contract_key="U", doc_kind="contract",
                       direction="upward", work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], name="C1混凝土",
                   feature="C30", unit="m3", qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        settled_up = ctx(conn, pid, contract_key="U", doc_kind="settlement",
                         direction="upward", work_scope=self.SCOPE)
        row = line(conn, settled_up["period_id"], name="C1混凝土",
                   feature="C30", unit="m3", qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, settled_up["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], name="C1混凝土", feature="C30",
                   unit="m3", qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        b_pending = ctx(conn, pid, contract_key="B", direction="downward",
                        work_scope=self.SCOPE)
        kwargs = {"name": "C1混凝土", "feature": "C30", "unit": "m3", "qty": "60"}
        if missing == "quantity":
            kwargs["qty"] = None
        else:
            kwargs["unit"] = None
        b_line = line(conn, b_pending["period_id"], **kwargs)
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼",
                        code="AA001")
        assert b1 is not None
        assert b1["status_vs_contract"] == "PENDING", b1["reason"]
        assert b1["status_vs_settlement"] == "PENDING"
        assert b1["status"] == "PENDING"
        # 拦截必须携带行来源，供人工复核定位
        assert b1["excluded_details"]
        assert any(h["line_item_id"] == b_line
                   for h in b1["excluded_details"])

    def test_coverage_hits_mask_pass_per_baseline(self, db):
        """真实反例（../quantity-ledger-coverage-probe.json）：

        上游 U 合同 100、U 结算 100 均确认；下游 A 60 确认、B 60 上下文
        pending（同 C1 混凝土 C30 m3 材料）。原缺陷：status_vs_contract/
        status_vs_settlement 均输出 PASS、只有 overall=PENDING。修复后
        覆盖缺口必须逐基准阻断 PASS，不得保留未知覆盖下的精确差额结论。
        """
        conn, pid = db
        contract = ctx(conn, pid, contract_key="U", doc_kind="contract",
                       direction="upward", work_scope=self.SCOPE)
        row = line(conn, contract["period_id"], name="C1混凝土",
                   feature="C30", unit="m3", qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        settled_up = ctx(conn, pid, contract_key="U", doc_kind="settlement",
                         direction="upward", work_scope=self.SCOPE)
        row = line(conn, settled_up["period_id"], name="C1混凝土",
                   feature="C30", unit="m3", qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, settled_up["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], name="C1混凝土", feature="C30",
                   unit="m3", qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        b_pending = ctx(conn, pid, contract_key="B", direction="downward",
                        work_scope=self.SCOPE)   # 60 待确认
        line(conn, b_pending["period_id"], name="C1混凝土", feature="C30",
             unit="m3", qty="60")
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼",
                        code="AA001")
        assert b1 is not None
        assert b1["status_vs_contract"] == "PENDING"
        assert b1["status_vs_settlement"] == "PENDING"
        assert b1["status"] == "PENDING"
        assert b1["excluded_details"]

    def test_overall_pass_by_contract_when_settlement_baseline_missing(self, db):
        """总体基于实际提供的基准汇总：只有合同量时按合同量得出结论。

        合同 1800、下游 1700 完整；对上已结算未提供 → 该栏保持 PENDING，
        但 overall 依据 compared_baselines=[upward_contract] 输出按合同未超。
        """
        conn, pid = db
        ledger = self._setup(conn, pid, downstream=[
            ("A", "1号楼", "600"), ("A", "2号楼", "200"),
            ("B", "1号楼", "500"), ("B", "2号楼", "300"),
            ("C", "2号楼", "100"),
        ])
        all_ctl = control_of(ledger, work_scope=self.SCOPE, building=None)
        assert all_ctl["compared_baselines"] == ["upward_contract"]
        assert all_ctl["status_vs_contract"] == "PASS"
        assert all_ctl["status_vs_settlement"] == "PENDING"   # 栏位状态保留
        assert all_ctl["status"] == "PASS"                    # 总体按合同未超
        assert "无任何" not in all_ctl["reason"]

    def test_overall_by_settlement_baseline_only(self, db):
        """只提供对上已结算基准：总体依据它汇总；合同量栏留待补。"""
        conn, pid = db
        settled_up = ctx(conn, pid, contract_key="U", doc_kind="settlement",
                         direction="upward", work_scope=self.SCOPE)
        row = line(conn, settled_up["period_id"], qty="1600")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, settled_up["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="1700")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        all_ctl = control_of(ledger, work_scope=self.SCOPE, building=None)
        assert all_ctl["compared_baselines"] == ["upward_settlement"]
        assert D(all_ctl["delta_vs_settlement"]) == D("100")
        assert all_ctl["status_vs_settlement"] == "FAIL"
        assert all_ctl["status_vs_contract"] == "PENDING"
        assert all_ctl["status"] == "FAIL"   # FAIL 优先暴露

    def test_no_baseline_at_all_is_pending(self, db):
        """两个基准都未提供：整体 PENDING，不伪造结论。"""
        conn, pid = db
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        all_ctl = control_of(ledger, work_scope=self.SCOPE, building=None)
        assert all_ctl["compared_baselines"] == []
        assert all_ctl["status"] == "PENDING"

    def test_duplicate_upstream_baselines_no_default_stacking(self, db):
        conn, pid = db
        for key in ("U1", "U2"):   # 两个上游合同同标准项同楼栋
            contract = ctx(conn, pid, contract_key=key, doc_kind="contract",
                           direction="upward", work_scope=self.SCOPE)
            row = line(conn, contract["period_id"], qty="100")
            line_building(conn, pid, row, "1号楼")
            confirm_ctx(conn, pid, contract["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward",
                work_scope=self.SCOPE)
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope=self.SCOPE, building="1号楼")
        assert b1["status_vs_contract"] == "PENDING"
        assert "不得默认叠加" in b1["reason"]

    def test_empty_work_scope_not_comparable(self, db):
        conn, pid = db
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope="")
        row = line(conn, contract["period_id"], qty="100")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, contract["context_id"])
        a = ctx(conn, pid, contract_key="A", direction="downward", work_scope="")
        row = line(conn, a["period_id"], qty="60")
        line_building(conn, pid, row, "1号楼")
        confirm_ctx(conn, pid, a["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        b1 = control_of(ledger, work_scope="", building="1号楼")
        assert b1["status_vs_contract"] == "PENDING"
        assert "口径" in b1["reason"]

    def test_pairing_distinguishes_features(self, db):
        conn, pid = db
        # 同码异特征：C30 与 C35 的比较互不覆盖
        contract = ctx(conn, pid, doc_kind="contract", direction="upward",
                       work_scope=self.SCOPE)
        line(conn, contract["period_id"], feature="C30", qty="100")
        line(conn, contract["period_id"], feature="C35", qty="50")
        confirm_ctx(conn, pid, contract["context_id"])
        settled = ctx(conn, pid, doc_kind="settlement", direction="upward",
                      work_scope=self.SCOPE)
        line(conn, settled["period_id"], feature="C30", qty="110")
        line(conn, settled["period_id"], feature="C35", qty="40")
        confirm_ctx(conn, pid, settled["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        c30 = [c for c in ledger["comparisons"]
               if c["identity"][3] == "c30" and c["building"] is None]
        c35 = [c for c in ledger["comparisons"]
               if c["identity"][3] == "c35" and c["building"] is None]
        assert len(c30) == 1 and len(c35) == 1
        assert D(c30[0]["settlement_quantity"]) == D("110")
        assert D(c35[0]["settlement_quantity"]) == D("40")

    def test_auto_identity_separates_code_and_name_namespaces(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], code="X1", name="甲项", qty="10")
        line(conn, cid["period_id"], code="", name="X1", qty="20")  # 名称恰为X1
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        assert len(ledger["items"]) == 2  # 编码命名空间 ≠ 名称命名空间
        quantities = sorted(D(it["quantity"]) for it in ledger["items"])
        assert quantities == [D("10"), D("20")]

    def test_same_code_different_names_not_auto_merged(self, db):
        conn, pid = db
        cid = ctx(conn, pid)
        line(conn, cid["period_id"], code="X1", name="甲项", qty="10")
        line(conn, cid["period_id"], code="X1", name="乙项", qty="20")
        confirm_ctx(conn, pid, cid["context_id"])
        ledger = qc.build_quantity_ledger(conn, pid)
        assert len(ledger["items"]) == 2


# ---------------------------------------------------------------- settlement_io 入口

class TestSettlementIOEntry:
    def test_ensure_period_for_contract_creates_independent_periods(self, db):
        from jiadun.core.engine import settlement_io

        conn, pid = db
        a = settlement_io.ensure_period_for_contract(
            conn, pid, direction="downward", contract_key="C-A",
            business_period_no=1, actor="t", reason="合同A第1期")
        b = settlement_io.ensure_period_for_contract(
            conn, pid, direction="downward", contract_key="C-B",
            business_period_no=1, actor="t", reason="合同B第1期")
        assert a != b
        rows = conn.execute(
            "SELECT period_no FROM settlement_periods WHERE id IN (?,?)"
            " ORDER BY period_no", (a, b)).fetchall()
        assert rows[0]["period_no"] != rows[1]["period_no"]  # 内部序号各自独立
        contexts = {c["period_id"]: c for c in qc.list_period_contexts(conn, pid)}
        assert contexts[a]["status"] == "pending"
        assert contexts[a]["contract_key"] == "C-A"

    def test_original_ensure_period_still_works(self, db):
        from jiadun.core.engine import settlement_io

        conn, pid = db
        period_id = settlement_io.ensure_period(
            conn, pid, 1, "兼容旧入口", None, direction="downward")
        assert period_id > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quantity_period_context").fetchone()[0] == 0


# ---------------------------------------------------------------- v56 迁移

class TestV56Migration:
    def _make_v55_db(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        migrations.migrate(db_path, tmp_path / "backups")
        conn = migrations.connect(db_path)
        with conn:
            conn.execute("DELETE FROM schema_migrations WHERE version=56")
            conn.execute("DROP TABLE IF EXISTS quantity_period_context")
            conn.execute("DROP TABLE IF EXISTS quantity_line_context")
            conn.execute("DROP INDEX IF EXISTS idx_qpc_project")
            conn.execute("DROP INDEX IF EXISTS idx_qlc_project")
            conn.execute("DROP INDEX IF EXISTS idx_qpc_contract")
            conn.execute(
                "UPDATE projects SET schema_version=55")
        backups_before = len(list((tmp_path / "backups").glob("*.db")))
        return db_path, backups_before

    def test_migration_preserves_data_and_creates_backup(self, tmp_path):
        db_path, backups_before = self._make_v55_db(tmp_path)
        conn = migrations.connect(db_path)
        with conn:
            pid = conn.execute(
                "INSERT INTO projects(name,schema_version,workspace_path,created_at)"
                " VALUES ('旧库','55','/x','2026')").lastrowid
            period = conn.execute(
                "INSERT INTO settlement_periods(project_id,period_no,title,direction)"
                " VALUES (?,1,'旧期次','downward')", (pid,)).lastrowid
            conn.execute(
                "INSERT INTO line_items(period_id,name,unit,quantity) VALUES (?,?,?,?)",
                (period, "旧明细", "m3", "100"))
            conn.execute(
                "INSERT INTO evidence(project_id,kind,summary,steps_json,sources_json,created_at)"
                " VALUES (?, 'x', 'y', '[]', '[]', '2026')", (pid,))
        conn.close()

        version = migrations.migrate(db_path, tmp_path / "backups")
        assert version == migrations.LATEST_SCHEMA_VERSION
        backups = list((tmp_path / "backups").glob("*.db"))
        assert len(backups) == backups_before + 1  # 升级前自动备份
        conn = migrations.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "quantity_period_context" in tables
        assert "quantity_line_context" in tables
        assert conn.execute(
            "SELECT COUNT(*) FROM line_items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        conn.close()

    def test_new_tables_default_pending(self, tmp_path):
        db_path, _ = self._make_v55_db(tmp_path)
        migrations.migrate(db_path, tmp_path / "backups")
        conn = migrations.connect(db_path)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(quantity_period_context)").fetchall()}
        assert {"status", "contract_key", "business_period_no", "supersedes_period_id",
                "doc_kind", "amount_mode", "work_scope", "building_scope"} <= cols
        # 新表为空时不影响既有读取
        assert conn.execute(
            "SELECT COUNT(*) FROM quantity_period_context").fetchone()[0] == 0
        conn.close()
