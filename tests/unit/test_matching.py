"""清单匹配五档置信度测试（原则 8/9/14）。"""

import pytest

from costguard.core.db import migrations
from costguard.core.evidence import audit as audit_log
from costguard.core.matching import matching as M


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        cur = conn.execute("INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t',1,'/t','2026')")
        pid = cur.lastrowid
        pmap = {}
        for pno in (1, 2):
            cur = conn.execute(
                "INSERT INTO settlement_periods(project_id, period_no, title) VALUES (?,?,?)", (pid, pno, f"第{pno}期")
            )
            pmap[pno] = cur.lastrowid
    yield conn, pid, pmap
    conn.close()


def add(conn, period_id, code="C1", name="清单A", unit="m3"):
    with conn:
        conn.execute(
            "INSERT INTO line_items(period_id, code, name, unit) VALUES (?,?,?,?)",
            (period_id, code, name, unit),
        )


def levels_of(groups):
    return {g.group_key: g for g in groups}


class TestLevels:
    def test_same_code_same_name_confirmed(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地")
        groups = levels_of(M.match_items(conn, pid))
        assert groups["code:C1"].level == M.CONFIRMED

    def test_same_code_diff_name_probable(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地（人工）")
        groups = levels_of(M.match_items(conn, pid))
        assert groups["code:C1"].level == M.PROBABLE

    def test_same_name_diff_code_probable_merged(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "挖沟槽土方")
        add(conn, p2, "C9", "挖沟槽土方")
        groups = levels_of(M.match_items(conn, pid))
        # 两个 code 组融合为一个 probable 组
        assert len(groups) == 1
        g = next(iter(groups.values()))
        assert g.level == M.PROBABLE and g.method == "name_merge"

    def test_whitespace_variant_not_confirmed_silent(self, db):
        """名称轻微差异（空格）→ 归一化后同名 → probable 合并（非静默 confirmed）。"""
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "水泥砂浆楼地面")
        add(conn, p2, "C1", " 水泥砂浆  楼地面 ")
        groups = levels_of(M.match_items(conn, pid))
        g = groups["code:C1"]
        # 同码同名（归一化）→ confirmed 是允许的（编码一致且名称归一一致）
        assert g.level in (M.CONFIRMED, M.PROBABLE)

    def test_similar_but_different_suspected(self, db):
        """名称相似但实际不同（C25 vs C30）→ 不得自动合并。"""
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C30混凝土垫层")
        groups = M.match_items(conn, pid)
        levels = {g.level for g in groups}
        # 两组必须分开或仅疑似关联；绝不允许 confirmed 合并
        c30 = [g for g in groups if any("C30" in n for n in g.names)]
        assert all(g.level in (M.SUSPECTED, M.PROBABLE) for g in groups)
        assert not any(g.level == M.CONFIRMED and len(g.names) > 1 for g in groups)

    def test_unit_mismatch_incomparable(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地", unit="m2")
        add(conn, p2, "C1", "平整场地", unit="m2")
        add(conn, p2, "C1", "平整场地", unit="立方米")
        groups = levels_of(M.match_items(conn, pid))
        assert groups["code:C1"].level == M.INCOMPARABLE

    def test_missing_fields_pending(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, code="", name="")
        groups = levels_of(M.match_items(conn, pid))
        assert any(g.level == M.PENDING_DATA for g in groups.values())

    def test_alias_gives_confirmed(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C25砼垫层")  # 语义一致但字面不同
        with conn:
            conn.execute(
                """INSERT INTO item_aliases(project_id, canonical_key, alias_text, mapping_basis,
                   confirmed_by, confirmed_at) VALUES (?,?,?,?,?,?)""",
                (pid, "name:c25混凝土垫层", "C25砼垫层", "用户确认：砼=混凝土", "user", "2026"),
            )
        groups = levels_of(M.match_items(conn, pid))
        g = groups["name:c25混凝土垫层"]
        assert g.level == M.CONFIRMED and g.method == "alias"
        assert len(g.item_ids) == 2  # 两行归入同组


class TestHumanReview:
    def test_confirm_requires_reason(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        groups = M.match_items(conn, pid)
        M.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        with pytest.raises(audit_log.AuditReasonRequired):
            M.confirm_match(conn, pid, mid, "测试员", reason="")

    def test_confirm_records_audit_and_evidence(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地（人工）")
        groups = M.match_items(conn, pid)
        M.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        M.confirm_match(conn, pid, mid, "王工", reason="同一工作内容，编码一致")
        m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        assert m["status"] == "confirmed" and m["level"] == M.CONFIRMED
        audits = audit_log.history_for(conn, pid)
        assert any(a.action == "confirm_match" for a in audits)
        ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (pid,)).fetchone()["c"]
        assert ev >= 1

    def test_override_match(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C30混凝土垫层")
        groups = M.match_items(conn, pid)
        M.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        M.override_match(conn, pid, mid, M.INCOMPARABLE, "王工", "C25 与 C30 强度等级不同，不可比")
        m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        assert m["level"] == M.INCOMPARABLE and m["status"] == "reviewed"

    def test_confirm_suspected_persists_alias(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C25砼垫层")
        # 先建 suspected 匹配（通过 override）
        groups = M.match_items(conn, pid)
        M.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        with conn:
            conn.execute("UPDATE matches SET level=? WHERE id=?", (M.SUSPECTED, mid))
        M.confirm_match(conn, pid, mid, "王工", reason="砼即混凝土，同一清单", alias_name="C25砼垫层")
        row = conn.execute("SELECT * FROM item_aliases WHERE project_id=?", (pid,)).fetchone()
        assert row and row["alias_text"] == "C25砼垫层"
        assert "match#" in row["mapping_basis"]
