"""权威批次清单与到件完整性评估。

文件登记本身只能证明“收到过哪些文件”，不能证明“应到资料是否齐全”。本
模块要求上游明确提供 manifest，并把缺件、哈希不符、歧义和未绑定分开记录；
不会根据当前收到的文件反向生成“应到清单”。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from costguard.core.contracts import run_contract
from costguard.core.evidence import audit as audit_log
from costguard.core.evidence import evidence as evidence_api

MANIFEST_VERSION = "1"
NOT_AVAILABLE = "not_available"
COMPLETE = "complete"
INCOMPLETE = "incomplete"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class ManifestEntrySpec:
    logical_key: str
    expected_name: str | None = None
    expected_sha256: str | None = None
    expected_period_no: int | None = None
    expected_direction: str | None = None
    expected_sheet_name: str | None = None
    required: bool = True
    note: str = ""
    source_reference: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _spec(value: ManifestEntrySpec | dict[str, Any]) -> ManifestEntrySpec:
    if isinstance(value, ManifestEntrySpec):
        return value
    return ManifestEntrySpec(
        logical_key=str(value.get("logical_key") or "").strip(),
        expected_name=value.get("expected_name"),
        expected_sha256=value.get("expected_sha256"),
        expected_period_no=(
            int(value["expected_period_no"])
            if value.get("expected_period_no") is not None else None
        ),
        expected_direction=value.get("expected_direction"),
        expected_sheet_name=value.get("expected_sheet_name"),
        required=bool(value.get("required", True)),
        note=str(value.get("note") or ""),
        source_reference=value.get("source_reference") or {},
    )


def create_manifest(
    conn: sqlite3.Connection,
    project_id: int,
    manifest_key: str,
    entries: list[ManifestEntrySpec | dict[str, Any]],
    *,
    source: str = "",
    declared_by: str = "",
    control_hash: str | None = None,
    note: str = "",
    version: str = MANIFEST_VERSION,
) -> int:
    """创建不可覆盖的权威清单；同一 ``manifest_key`` 不能静默替换。"""
    key = str(manifest_key or "").strip()
    if not key:
        raise ValueError("manifest_key 不能为空")
    specs = [_spec(item) for item in entries]
    if not specs:
        raise ValueError("权威批次清单至少应有一项")
    keys = [item.logical_key for item in specs]
    if any(not key for key in keys):
        raise ValueError("manifest entry logical_key 不能为空")
    if len(keys) != len(set(keys)):
        raise ValueError("manifest entry logical_key 不得重复")
    had_materialized_contract = run_contract.has_materialized_contract(conn, project_id)

    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO import_manifests(
                   project_id, manifest_key, source, declared_by, declared_at,
                   control_hash, status, note, version)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (project_id, key, source, declared_by, _now(), control_hash,
             "not_assessed", note, version),
        )
        manifest_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO import_manifest_entries(
                   manifest_id, logical_key, expected_name, expected_sha256,
                   expected_period_no, expected_direction, expected_sheet_name,
                   required, state, note, source_reference_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                manifest_id, item.logical_key, item.expected_name, item.expected_sha256,
                item.expected_period_no, item.expected_direction, item.expected_sheet_name,
                1 if item.required else 0, "missing", item.note,
                json.dumps(item.source_reference or {}, ensure_ascii=False, default=str),
            ) for item in specs],
        )
        return manifest_id

    with run_contract._transaction(conn, "create_manifest"):
        manifest_id = _insert()
        # 权威清单本身是运行范围的一部分。已有计算结果的项目不能在清单
        # 写入后继续沿用旧签名；放在同一事务内，契约切换失败时清单也回滚。
        if had_materialized_contract:
            run_contract.ensure_run_contract(conn, project_id)
        return manifest_id


def current_manifest(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM import_manifests
           WHERE project_id=? ORDER BY declared_at DESC, id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def _source_rows(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, original_name, sha256, size_bytes, file_type
           FROM source_files WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()


def _semantic_state(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    file_id: int,
) -> tuple[str, str]:
    """验证清单声明的期次、方向、Sheet 与解析事实，而非只验文件身份。"""
    expected_period = entry["expected_period_no"]
    expected_direction = (entry["expected_direction"] or "").strip() or None
    expected_sheet = (entry["expected_sheet_name"] or "").strip() or None
    if expected_period is None and expected_direction is None and expected_sheet is None:
        return "present", "文件身份已按权威绑定确认到件"

    facts = conn.execute(
        """SELECT rs.id AS sheet_id, rs.sheet_name, rs.period_id,
                         pb.status AS batch_status, th.id AS header_id,
                         th.needs_review, sp.period_no, sp.direction
                  FROM raw_sheets rs
                  JOIN parse_batches pb ON pb.id=rs.batch_id
                  LEFT JOIN table_headers th ON th.sheet_id=rs.id
                  LEFT JOIN settlement_periods sp ON sp.id=rs.period_id
                 WHERE pb.file_id=?
                 ORDER BY rs.id""",
        (file_id,),
    ).fetchall()
    if not facts:
        return "mismatch", "文件已到件，但没有可核对的解析工作表事实"

    candidates = list(facts)
    if expected_sheet is not None:
        candidates = [row for row in candidates if row["sheet_name"] == expected_sheet]
        if not candidates:
            return "mismatch", f"实际 Sheet 与清单不符：要求「{expected_sheet}」"
    if expected_period is not None:
        candidates = [row for row in candidates if row["period_no"] == int(expected_period)]
        if not candidates:
            return "mismatch", f"实际期次与清单不符：要求第{expected_period}期"
    if expected_direction is not None:
        candidates = [
            row for row in candidates
            if (row["direction"] or "unknown") == expected_direction
        ]
        if not candidates:
            return "mismatch", f"实际方向与清单不符：要求「{expected_direction}」"
    if len(candidates) != 1:
        return "ambiguous", "清单语义对应多个解析工作表，未自动选择"
    fact = candidates[0]
    if fact["batch_status"] != "ok":
        return "mismatch", f"解析批次状态为 {fact['batch_status']!r}，不能证明清单语义"
    if expected_period is not None or expected_direction is not None:
        if fact["period_id"] is None:
            return "mismatch", "工作表尚未绑定结算期次，不能证明期次/方向"
    if fact["header_id"] is None or fact["needs_review"]:
        return "mismatch", "工作表表头不存在或仍待人工复核，不能证明清单语义"
    return "present", "文件身份及期次、方向、Sheet、表头语义均已核对"


def _entry_state(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    source_rows: list[sqlite3.Row],
) -> tuple[str, int | None, str]:
    """仅按显式哈希或人工绑定判断到件，不以文件名自动替代权威绑定。"""
    expected_sha = (entry["expected_sha256"] or "").strip().lower() or None
    expected_name = (entry["expected_name"] or "").strip() or None
    bound_id = entry["received_file_id"]
    by_id = {int(row["id"]): row for row in source_rows}
    if bound_id is not None:
        row = by_id.get(int(bound_id))
        if row is None:
            return "mismatch", None, "已绑定的文件不属于当前项目或已不存在"
        if expected_sha and (row["sha256"] or "").lower() != expected_sha:
            return "mismatch", int(row["id"]), "已绑定文件 SHA-256 与权威清单不符"
        if expected_name and row["original_name"] != expected_name:
            return "mismatch", int(row["id"]), "已绑定文件名与权威清单不符"
        state, note = _semantic_state(conn, entry, int(row["id"]))
        return state, int(row["id"]), note
    if expected_sha:
        matches = [row for row in source_rows if (row["sha256"] or "").lower() == expected_sha]
        if len(matches) == 1:
            if expected_name and matches[0]["original_name"] != expected_name:
                return "mismatch", int(matches[0]["id"]), "SHA-256 命中但文件名与清单不符"
            state, note = _semantic_state(conn, entry, int(matches[0]["id"]))
            return state, int(matches[0]["id"]), note
        if len(matches) > 1:
            return "ambiguous", None, "同一权威 SHA-256 对应多个收到文件"
        same_name = [row for row in source_rows if expected_name and row["original_name"] == expected_name]
        if same_name:
            return "mismatch", int(same_name[0]["id"]), "文件名命中但 SHA-256 未命中"
        return "missing", None, "未收到权威 SHA-256 对应文件"
    if expected_name:
        same_name = [row for row in source_rows if row["original_name"] == expected_name]
        if len(same_name) > 1:
            return "ambiguous", None, "同名收到文件超过一个，未自动选择"
        if same_name:
            return "unbound", int(same_name[0]["id"]), "文件名命中，但缺少权威 SHA-256 或人工绑定"
        return "missing", None, "未收到清单指定文件名"
    return "unbound", None, "清单未提供可核对的文件名或 SHA-256"


def assess_manifest(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """评估当前权威清单；不从已收到文件反推缺失项。"""
    manifest = current_manifest(conn, project_id)
    if not manifest:
        return {
            "status": NOT_AVAILABLE,
            "manifest_id": None,
            "manifest_key": None,
            "authoritative": False,
            "entries": [],
            "required_total": 0,
            "present": 0,
            "missing": 0,
            "mismatch": 0,
            "ambiguous": 0,
            "unbound": 0,
            "optional_missing": 0,
        }
    source_rows = _source_rows(conn, project_id)
    entries = conn.execute(
        "SELECT * FROM import_manifest_entries WHERE manifest_id=? ORDER BY id",
        (manifest["id"],),
    ).fetchall()
    result_entries: list[dict[str, Any]] = []
    state_counts = {"present": 0, "missing": 0, "mismatch": 0, "ambiguous": 0, "unbound": 0}
    optional_missing = 0
    binding_changed = False
    with run_contract._transaction(conn, "assess_manifest"):
        for entry in entries:
            state, file_id, note = _entry_state(conn, entry, source_rows)
            if file_id != entry["received_file_id"]:
                binding_changed = True
            conn.execute(
                "UPDATE import_manifest_entries SET state=?, received_file_id=?, note=? WHERE id=?",
                (state, file_id, note, entry["id"]),
            )
            state_counts[state] = state_counts.get(state, 0) + 1
            if not entry["required"] and state != "present":
                optional_missing += 1
            result_entries.append({
                "id": int(entry["id"]),
                "logical_key": entry["logical_key"],
                "expected_name": entry["expected_name"],
                "expected_sha256": entry["expected_sha256"],
                "expected_period_no": entry["expected_period_no"],
                "expected_direction": entry["expected_direction"],
                "expected_sheet_name": entry["expected_sheet_name"],
                "required": bool(entry["required"]),
                "received_file_id": file_id,
                "state": state,
                "note": note,
            })
        required_states = [
            item["state"] for item in result_entries if item["required"]
        ]
        if any(state == "mismatch" for state in required_states):
            status = MISMATCH
        elif required_states and all(state == "present" for state in required_states):
            status = COMPLETE
        else:
            status = INCOMPLETE
        conn.execute(
            "UPDATE import_manifests SET status=? WHERE id=?",
            (status, manifest["id"]),
        )
    # 哈希唯一命中属于可复核的输入绑定，变化后需立刻让旧成果失效；仅状态
    # 文案变化不会触发新契约。首次尚未物化结果的项目不额外创建契约。
    if binding_changed and run_contract.get_current_contract(conn, project_id):
        run_contract.ensure_run_contract(conn, project_id)
    return {
        "status": status,
        "manifest_id": int(manifest["id"]),
        "manifest_key": manifest["manifest_key"],
        "authoritative": True,
        "source": manifest["source"],
        "declared_by": manifest["declared_by"],
        "declared_at": manifest["declared_at"],
        "control_hash": manifest["control_hash"],
        "version": manifest["version"],
        "entries": result_entries,
        "required_total": sum(1 for item in result_entries if item["required"]),
        **state_counts,
        "optional_missing": optional_missing,
    }


def bind_manifest_entry(
    conn: sqlite3.Connection,
    project_id: int,
    entry_id: int,
    file_id: int,
    *,
    actor: str,
    reason: str,
) -> None:
    """人工绑定收到文件，Evidence 与 Audit 与状态更新同一事务提交。"""
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工绑定批次文件必须记录原因")
    row = conn.execute(
        """SELECT e.*, m.project_id FROM import_manifest_entries e
           JOIN import_manifests m ON m.id=e.manifest_id
           WHERE e.id=? AND m.project_id=?""",
        (entry_id, project_id),
    ).fetchone()
    source = conn.execute(
        "SELECT * FROM source_files WHERE id=? AND project_id=?", (file_id, project_id)
    ).fetchone()
    if not row or not source:
        raise ValueError("manifest entry 或 source file 不存在，无法绑定")
    with run_contract._transaction(conn, "bind_manifest_entry"):
        # 先写入绑定，再依据新的权威范围生成签名；Evidence 必须绑定新签名，
        # 否则人工绑定完成后会被误归入旧运行。
        conn.execute(
            """UPDATE import_manifest_entries
               SET received_file_id=?, state='present', note=? WHERE id=?""",
            (file_id, "人工绑定处理中", entry_id),
        )
        signature = run_contract.ensure_run_contract(conn, project_id).signature
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "manifest_binding",
            "人工绑定权威批次清单与收到文件。",
            steps=[{
                "entry_id": entry_id,
                "file_id": file_id,
                "logical_key": row["logical_key"],
                "reason": reason.strip(),
            }],
            sources=[{
                "file": source["original_name"],
                "sha256": source["sha256"],
                "file_id": file_id,
            }],
            commit=False,
            run_signature=signature,
        )
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            actor,
            "bind_manifest_entry",
            f"manifest_entry:{entry_id}",
            {"received_file_id": row["received_file_id"]},
            {"received_file_id": file_id, "evidence_id": evidence_id},
            reason,
            commit=False,
        )
        conn.execute(
            """UPDATE import_manifest_entries
               SET note=? WHERE id=?""",
            (f"人工绑定；Evidence ID {evidence_id}；Audit ID {audit_id}", entry_id),
        )


def manifest_summary(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    return assess_manifest(conn, project_id)
