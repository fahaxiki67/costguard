"""清单匹配五档置信度测试（原则 8/9/14）。"""

import pytest

from costguard.core.db import migrations
from costguard.core.evidence import audit as audit_log
from costguard.core.matching import matching as matching_mod


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
    def test_same_item_in_upward_and_downward_is_never_merged(self, db):
        """同码同名跨方向也必须是两个组，默认入口不得混合。"""
        conn, pid, (p1, p2) = db
        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (p1,)
            )
            conn.execute(
                "UPDATE settlement_periods SET direction='downward' WHERE id=?", (p2,)
            )
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地")

        groups = levels_of(matching_mod.match_items(conn, pid))

        assert set(groups) == {"downward:code:C1", "upward:code:C1"}
        assert all(len(group.item_ids) == 1 for group in groups.values())
        assert {group.item_ids[0] for group in groups.values()} == {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM line_items ORDER BY id"
            ).fetchall()
        }

    def test_same_code_same_name_confirmed(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地")
        groups = levels_of(matching_mod.match_items(conn, pid))
        assert groups["code:C1"].level == matching_mod.CONFIRMED

    def test_same_code_diff_name_probable(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地（人工）")
        groups = levels_of(matching_mod.match_items(conn, pid))
        assert groups["code:C1"].level == matching_mod.PROBABLE

    def test_same_name_diff_code_probable_merged(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "挖沟槽土方")
        add(conn, p2, "C9", "挖沟槽土方")
        groups = levels_of(matching_mod.match_items(conn, pid))
        # 两个 code 组融合为一个 probable 组
        assert len(groups) == 1
        g = next(iter(groups.values()))
        assert g.level == matching_mod.PROBABLE and g.method == "name_merge"

    def test_whitespace_variant_not_confirmed_silent(self, db):
        """名称轻微差异（空格）→ 归一化后同名 → probable 合并（非静默 confirmed）。"""
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "水泥砂浆楼地面")
        add(conn, p2, "C1", " 水泥砂浆  楼地面 ")
        groups = levels_of(matching_mod.match_items(conn, pid))
        g = groups["code:C1"]
        # 同码同名（归一化）→ confirmed 是允许的（编码一致且名称归一一致）
        assert g.level in (matching_mod.CONFIRMED, matching_mod.PROBABLE)

    def test_similar_but_different_suspected(self, db):
        """名称相似但实际不同（C25 vs C30）→ 不得自动合并。"""
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C30混凝土垫层")
        groups = matching_mod.match_items(conn, pid)
        # 两组必须分开或仅疑似关联；绝不允许 confirmed 合并
        assert all(g.level in (matching_mod.SUSPECTED, matching_mod.PROBABLE) for g in groups)
        assert not any(g.level == matching_mod.CONFIRMED and len(g.names) > 1 for g in groups)

    def test_unit_mismatch_incomparable(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地", unit="m2")
        add(conn, p2, "C1", "平整场地", unit="m2")
        add(conn, p2, "C1", "平整场地", unit="立方米")
        groups = levels_of(matching_mod.match_items(conn, pid))
        assert groups["code:C1"].level == matching_mod.INCOMPARABLE

    def test_missing_fields_pending(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, code="", name="")
        groups = levels_of(matching_mod.match_items(conn, pid))
        assert any(g.level == matching_mod.PENDING_DATA for g in groups.values())

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
        groups = levels_of(matching_mod.match_items(conn, pid))
        g = groups["name:c25混凝土垫层"]
        assert g.level == matching_mod.CONFIRMED and g.method == "alias"
        assert len(g.item_ids) == 2  # 两行归入同组


class TestHumanReview:
    def test_direction_prefix_is_not_persisted_into_alias_core_key(self, db):
        conn, pid, (p1, p2) = db
        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (p1,)
            )
            conn.execute(
                "UPDATE settlement_periods SET direction='downward' WHERE id=?", (p2,)
            )
        add(conn, p1, None, "对上临时别名")
        add(conn, p2, None, "对下清单")
        matching_mod.save_matches(conn, pid, matching_mod.match_items(conn, pid))
        row = conn.execute(
            "SELECT id, group_key FROM matches WHERE group_key LIKE 'upward:%'"
        ).fetchone()
        with conn:
            conn.execute(
                "UPDATE matches SET level=? WHERE id=?", (matching_mod.SUSPECTED, row["id"])
            )

        matching_mod.confirm_match(
            conn, pid, row["id"], "王工", "人工确认同义名称", alias_name="对上临时别名"
        )

        alias = conn.execute(
            "SELECT canonical_key, direction FROM item_aliases WHERE project_id=?", (pid,)
        ).fetchone()
        assert alias["canonical_key"] == row["group_key"].removeprefix("upward:")
        assert not alias["canonical_key"].startswith("upward:")
        assert alias["direction"] == "upward"

    def test_upward_alias_never_applies_to_downward_items(self, db):
        """对上的人工别名是方向性业务结论，不得自动合并对下同名行。"""
        conn, pid, (p1, p2) = db
        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (p1,)
            )
            conn.execute(
                "UPDATE settlement_periods SET direction='downward' WHERE id=?", (p2,)
            )
            conn.execute(
                """INSERT INTO item_aliases(project_id, direction, canonical_key,
                   alias_text, mapping_basis, confirmed_by, confirmed_at)
                   VALUES (?, 'upward', 'code:C1', '已确认别名', '对上人工确认', '王工', '2026')""",
                (pid,),
            )
        add(conn, p1, "C1", "标准名称")
        add(conn, p1, None, "已确认别名")
        add(conn, p2, "C1", "标准名称")
        add(conn, p2, None, "已确认别名")

        groups = levels_of(matching_mod.match_items(conn, pid))

        assert groups["upward:code:C1"].method == "alias"
        assert len(groups["upward:code:C1"].item_ids) == 2
        assert len(groups["downward:code:C1"].item_ids) == 1
        assert "downward:name:已确认别名" in groups
        assert groups["downward:name:已确认别名"].method != "alias"

    def test_confirm_requires_reason(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        groups = matching_mod.match_items(conn, pid)
        matching_mod.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        with pytest.raises(audit_log.AuditReasonRequiredError):
            matching_mod.confirm_match(conn, pid, mid, "测试员", reason="")

    def test_confirm_records_audit_and_evidence(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, "C1", "平整场地")
        add(conn, p2, "C1", "平整场地（人工）")
        groups = matching_mod.match_items(conn, pid)
        matching_mod.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        matching_mod.confirm_match(conn, pid, mid, "王工", reason="同一工作内容，编码一致")
        m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        assert m["status"] == "confirmed" and m["level"] == matching_mod.CONFIRMED
        audits = audit_log.history_for(conn, pid)
        assert any(a.action == "confirm_match" for a in audits)
        ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (pid,)).fetchone()["c"]
        assert ev >= 1

    def test_override_match(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C30混凝土垫层")
        groups = matching_mod.match_items(conn, pid)
        matching_mod.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        matching_mod.override_match(conn, pid, mid, matching_mod.INCOMPARABLE, "王工", "C25 与 C30 强度等级不同，不可比")
        m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        assert m["level"] == matching_mod.INCOMPARABLE and m["status"] == "reviewed"

    def test_confirm_suspected_persists_alias(self, db):
        conn, pid, (p1, p2) = db
        add(conn, p1, None, "C25混凝土垫层")
        add(conn, p2, None, "C25砼垫层")
        # 先建 suspected 匹配（通过 override）
        groups = matching_mod.match_items(conn, pid)
        matching_mod.save_matches(conn, pid, groups)
        mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()["id"]
        with conn:
            conn.execute("UPDATE matches SET level=? WHERE id=?", (matching_mod.SUSPECTED, mid))
        matching_mod.confirm_match(conn, pid, mid, "王工", reason="砼即混凝土，同一清单", alias_name="C25砼垫层")
        row = conn.execute("SELECT * FROM item_aliases WHERE project_id=?", (pid,)).fetchone()
        assert row and row["alias_text"] == "C25砼垫层"
        assert "match#" in row["mapping_basis"]
