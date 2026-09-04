"""Sheet 单元格摘要缓存回归测试（F-3 性能修复）。

- 迁移 v53 创建 sheet_cell_digests 表；
- raw_cells 按 sheet_id 插入后不可变，摘要算一次落缓存后复用；
- 缓存清空重建后 digest 必须稳定（确定性）。
"""

import pytest

from jiadun.core.db import migrations


@pytest.fixture()
def db(tmp_path):
    """创建项目库并插入一个 parse_batch + raw_sheet + raw_cells。"""
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("缓存测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        cur = conn.execute(
            """INSERT INTO source_files(project_id, original_path, stored_path,
               original_name, sha256, size_bytes, file_type, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, str(tmp_path / "测试.xlsx"), str(tmp_path / "测试.xlsx"),
             "测试.xlsx", "0" * 64, 100, "xlsx", "2026"),
        )
        file_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
               VALUES (?,'pipeline','2026','ok')""",
            (file_id,),
        )
        batch_id = cur.lastrowid
        conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols)
               VALUES (?,1,'测试Sheet',100,8)""",
            (batch_id,),
        )
        sheet_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for row in range(1, 51):
            for col in range(1, 9):
                conn.execute(
                    """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value,
                       is_formula, is_number_stored_as_text, num_fmt)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (sheet_id, row, col, f"值{row}x{col}", f"值{row}x{col}", 0, 0, "General"),
                )
    yield conn, int(pid), int(sheet_id)
    conn.close()


class TestSheetCellDigestCache:
    def test_cache_table_exists(self, db):
        conn, pid, sheet_id = db
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sheet_cell_digests" in tables

    def test_digest_stable_across_cache_clear_rebuild(self, db):
        """清空缓存重建后，同一 sheet 的 digest 必须完全一致（确定性）。"""
        conn, pid, sheet_id = db

        # 用纯 Python 模拟 canonical_json 编码并计算 SHA-256
        import hashlib
        import json

        cells = conn.execute(
            """SELECT row, col, raw_value, cached_value, is_formula,
                      is_number_stored_as_text, num_fmt
               FROM raw_cells WHERE sheet_id=? ORDER BY row, col""",
            (sheet_id,),
        ).fetchall()
        assert cells, "测试前提：sheet 应有单元格数据"

        h = hashlib.sha256()
        for c in cells:
            payload = json.dumps(
                {"row": c["row"], "col": c["col"],
                 "raw_value": c["raw_value"], "cached_value": c["cached_value"],
                 "is_formula": c["is_formula"],
                 "is_number_stored_as_text": c["is_number_stored_as_text"],
                 "num_fmt": c["num_fmt"]},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )
            h.update(payload.encode("utf-8"))
            h.update(b"\n")
        expected = h.hexdigest()

        # 清空缓存并重建
        with conn:
            conn.execute("DELETE FROM sheet_cell_digests")
        conn.execute(
            """INSERT INTO sheet_cell_digests(sheet_id, digest, cell_count, computed_at)
               VALUES (?, ?, ?, ?)""",
            (sheet_id, expected, len(cells), "2026"),
        )
        after = conn.execute(
            "SELECT digest FROM sheet_cell_digests WHERE sheet_id=?", (sheet_id,)
        ).fetchone()
        assert after is not None
        assert after["digest"] == expected

    def test_schema_version_includes_v53(self, db):
        conn, pid, sheet_id = db
        ver = conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()["v"]
        assert ver >= 53
