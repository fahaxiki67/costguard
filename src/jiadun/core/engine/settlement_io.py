"""结算文件导入编排：解析 → 表头识别 → 期次归属 → 行抽取 → 落库。

一条管线供 UI / CLI / 测试共用。报告每个 sheet 的处理结论，绝不静默丢弃。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from jiadun.core.contracts import run_contract
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.models.source_file import SourceFile, import_file
from jiadun.core.parsing import coverage_proof, excel_parser, extract_items
from jiadun.core.parsing.header_detect import HeaderDetection, detect_header


@dataclass
class SheetReport:
    sheet_name: str
    status: str  # 'parsed' | 'no_header' | 'duplicate_header' | 'needs_review'
    n_items: int = 0
    n_subtotal: int = 0
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    # ``status`` 保留导入过程兼容值；``state_code`` 是对外统一四级 Sheet
    # 状态（confirmed/pending/non_business/parse_failed），避免把“已解析”
    # 误当成“已确认”。
    state_code: str = "pending"


@dataclass
class ImportReport:
    file_id: int
    batch_id: int | None
    period_no: int
    period_id: int
    status: str  # 'ok' | 'partial' | 'failed'
    sheets: list[SheetReport] = field(default_factory=list)
    message: str = ""
    needs_manual_review: bool = False
    # 任务书 B4：重解析人工决策结转统计（carried/skipped_* 计数）
    carry_forward: dict = field(default_factory=dict)


_PERIOD_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*期")

# sheet 级写入闸门（监督第九轮）：
# - det.needs_review 一律不写 canonical（歧义/低置信未经人工确认不得进入结算模型）；
# - sheet 名/document 语义门控：汇总/核销/台账类 sheet 即使强表头也需角色确认；
# - 无行数护栏（600 行合法大结算必须解析，判断不按大小）。
SUMMARY_LIKE_PATTERN = re.compile(r"汇总|核销|台账|summary|reconciliation|ledger", re.IGNORECASE)

# 待人工确认工作表的唯一口径。已确认“仅作证据”的工作表仍保留在
# raw_sheets/audit_log/evidence 中，但不应继续阻塞项目校核状态。
PENDING_SHEETS_SQL = """
SELECT rs.id AS sheet_id, rs.sheet_name, rs.n_cols, rs.n_rows, sf.original_name,
       rs.sheet_status, rs.sheet_status_reason,
       th.col_map_json, th.header_row_lo, th.header_row_hi, th.needs_review,
       th.data_row_start, th.data_row_end, th.data_range_status, th.data_range_method
FROM raw_sheets rs
JOIN parse_batches pb ON pb.id = rs.batch_id
JOIN source_files sf ON sf.id = pb.file_id
LEFT JOIN table_headers th ON th.sheet_id = rs.id
WHERE sf.project_id=? AND rs.sheet_status='pending'
  AND pb.id=(
      SELECT latest.id FROM parse_batches latest
      WHERE latest.file_id=pb.file_id
      ORDER BY latest.parsed_at DESC, latest.id DESC LIMIT 1
  )
ORDER BY sf.id, rs.id"""


def set_sheet_status(
    conn: sqlite3.Connection,
    sheet_id: int,
    status: str,
    *,
    reason: str = "",
    actor: str | None = None,
) -> None:
    """持久化 Sheet 四级状态；状态码只允许来自统一状态合同。"""
    from jiadun.core.reporting.state import SHEET_STATE_CODES

    if status not in SHEET_STATE_CODES:
        raise ValueError(f"不支持的 Sheet 状态: {status!r}")
    from datetime import datetime

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """UPDATE raw_sheets
           SET sheet_status=?, sheet_status_reason=?, sheet_status_updated_at=?,
               sheet_status_actor=?
           WHERE id=?""",
        (status, reason.strip(), now, actor, int(sheet_id)),
    )


def pending_sheet_count(conn: sqlite3.Connection, project_id: int) -> int:
    """返回当前真正需要人工处理的工作表数量。

    仅存证角色确认不进入结算模型，但它已经完成了人工决策，不能继续被
    当作“待确认”计数；所有 UI、校核和导出调用同一函数，避免口径漂移。
    """
    row = conn.execute(
        """SELECT COUNT(*) AS c
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=? AND rs.sheet_status='pending'
             AND pb.id=(
                 SELECT latest.id FROM parse_batches latest
                 WHERE latest.file_id=pb.file_id
                 ORDER BY latest.parsed_at DESC, latest.id DESC LIMIT 1
             )""",
        (project_id,),
    ).fetchone()
    return int(row["c"] if row else 0)


def _persist_coverage_proof(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    file_id: int,
    batch_id: int,
    sheet_id: int,
    period_id: int | None,
    direction: str,
    proof: coverage_proof.SheetCoverageProof,
    rows: list[coverage_proof.RowClassification],
    run_signature: str | None = None,
    run_id: str | None = None,
) -> int:
    """保存 Sheet 覆盖证明及其 Evidence；证明失败不得静默转为成功。"""
    evidence_id = evidence_api.add_evidence(
        conn,
        project_id,
        "sheet_coverage_proof",
        f"Sheet 覆盖证明：数据区第{proof.raw_row_start}至{proof.raw_row_end}行，"
        f"分类{proof.classified_row_count}行，参与累计{proof.business_rows_used}行，状态{proof.proof_status}",
        steps=[
            {
                "step": "逐行分类",
                "raw_data_row_count": proof.raw_data_row_count,
                "classified_row_count": proof.classified_row_count,
                "counts": proof.counts,
                "proof_status": proof.proof_status,
                "proof_reason": proof.proof_reason,
                "ab_row_set_status": proof.ab_row_set_status,
                "ab_independence_level": proof.ab_independence_level,
            }
        ],
        sources=[
            {
                "file_id": file_id,
                "batch_id": batch_id,
                "sheet_id": sheet_id,
                "period_id": period_id,
                "direction": direction,
                "location": (
                    f"行{proof.raw_row_start}-行{proof.raw_row_end}，"
                    f"列{proof.raw_col_start}-列{proof.raw_col_end}"
                ),
            }
        ],
        commit=False,
        run_signature=run_signature,
        run_id=run_id,
        scope="current" if (run_signature or run_id) else "source",
    )
    return coverage_proof.persist_sheet_coverage_proof(
        conn,
        project_id=project_id,
        file_id=file_id,
        batch_id=batch_id,
        sheet_id=sheet_id,
        period_id=period_id,
        direction=direction,
        proof=proof,
        rows=rows,
        run_signature=run_signature,
        run_id=run_id,
        evidence_id=evidence_id,
        commit=False,
    )


def _rebind_coverage_proofs_for_run(
    conn: sqlite3.Connection,
    project_id: int,
    previous: run_contract.RunContract | None,
    current: run_contract.RunContract,
    *,
    reason: str,
) -> int:
    """把未受数据影响的旧覆盖证明重新固化到新运行。

    人工确认“非结算 Sheet”只改变项目门控事实，不改变结算 Sheet 的原始
    网格、行分类或金额。旧证明仍作为历史快照保留；这里追加一份逐字段相同、
    但绑定新 ``run_id`` 的当前证明，并为其生成新的当前 Evidence。若输入
    运行不存在或没有旧证明，返回 0，不通过任何默认值补齐。
    """
    if previous is None or previous.run_id == current.run_id:
        return 0
    # 运行契约的变化可能已经经历多次派生运行。例如先完成一次校核、再
    # 聚合、再确认非结算 Sheet 时，某些 Sheet 的最近证明属于更早的运行，
    # 而不一定属于 ``previous``。只取上一运行会让这些 Sheet 在当前运行
    # 丢失覆盖证明，导致 C 路径被错误降级为 ``unproven``。因此按每个
    # Sheet 取最新一份历史证明，并排除当前运行；证明本身是不可变快照，
    # 跨运行复用不会改变原始网格或逐行分类。
    rows = conn.execute(
        """SELECT p.*
           FROM sheet_coverage_proofs p
           WHERE p.project_id=?
             AND p.period_id IS NOT NULL
             AND NOT (p.run_signature=? AND p.run_id=?)
             AND p.id=(SELECT MAX(p2.id) FROM sheet_coverage_proofs p2
                       WHERE p2.project_id=p.project_id AND p2.sheet_id=p.sheet_id
                         AND p2.period_id IS NOT NULL)
           ORDER BY p.id""",
        (project_id, current.signature, current.run_id),
    ).fetchall()
    if not rows:
        return 0
    from datetime import datetime

    rebound = 0
    for row in rows:
        # 方向属于 Run Contract 输入，而不是 coverage 证明的原始网格事实。
        # 方向可能由人工确认或历史兼容脚本在上一次证明之后才被补齐；重绑
        # 时必须读取当前 period 的方向，不能把旧快照中的 ``unknown`` 再
        # 复制到当前运行，否则摘要的 period/sheet/方向证据闸门会误判为
        # 证据不一致。旧 proof 仍保持不可变并留作历史追溯。
        current_period = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=? AND project_id=?",
            (row["period_id"], project_id),
        ).fetchone()
        current_direction = (
            str(current_period["direction"] or "unknown")
            if current_period is not None else str(row["direction"] or "unknown")
        )
        old_evidence = conn.execute(
            "SELECT sources_json FROM evidence WHERE id=?",
            (row["evidence_id"],),
        ).fetchone()
        try:
            old_sources = json.loads(old_evidence["sources_json"] or "[]") if old_evidence else []
        except (TypeError, json.JSONDecodeError):
            old_sources = []
        if isinstance(old_sources, dict):
            old_sources = [old_sources]
        elif not isinstance(old_sources, list):
            old_sources = []
        # 旧证明的事实内容不变，但其运行身份已退出 current；先保留
        # Evidence 事件与历史原因，再追加同内容的新运行证明。这样直接读取
        # evidence 表、导出历史计数和 current_scope 三者口径一致。
        if row["evidence_id"] is not None:
            evidence_api.mark_historical(
                conn,
                project_id,
                [int(row["evidence_id"])],
                reason,
                commit=False,
            )
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "sheet_coverage_proof",
            f"Sheet 覆盖证明重新绑定当前运行：证明内容不变；{reason}",
            steps=[
                {
                    "step": "复用不可变覆盖证明",
                    "source_proof_id": int(row["id"]),
                    "source_run_id": row["run_id"],
                    "source_run_signature": row["run_signature"],
                    "reason": reason,
                }
            ],
            sources=[
                *old_sources,
                {
                    "source_proof_id": int(row["id"]),
                    "sheet_id": int(row["sheet_id"]),
                    "period_id": int(row["period_id"]),
                    "direction": current_direction,
                },
            ],
            commit=False,
            run_signature=current.signature,
            run_id=current.run_id,
            scope="current",
        )
        cur = conn.execute(
            """INSERT INTO sheet_coverage_proofs(
                   project_id, run_signature, run_id, file_id, batch_id, sheet_id,
                   period_id, direction, raw_row_start, raw_row_end, raw_col_start,
                   raw_col_end, raw_data_row_count, classified_row_count,
                   classified_detail_rows, excluded_subtotal_rows, excluded_title_rows,
                   excluded_note_rows, excluded_blank_rows, excluded_tail_note_rows,
                   excluded_orphan_numeric_rows, excluded_parse_failed_rows,
                   pending_rows, unrecognized_rows, business_rows_used, raw_amount_total,
                   detail_amount_total, proof_status, proof_reason, c_control_status,
                   c_control_value, c_control_source_json, c_control_evidence_id,
                   ab_row_set_status, ab_row_set_hash, ab_independence_level,
                   evidence_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id,
                current.signature,
                current.run_id,
                row["file_id"],
                row["batch_id"],
                row["sheet_id"],
                row["period_id"],
                current_direction,
                row["raw_row_start"],
                row["raw_row_end"],
                row["raw_col_start"],
                row["raw_col_end"],
                row["raw_data_row_count"],
                row["classified_row_count"],
                row["classified_detail_rows"],
                row["excluded_subtotal_rows"],
                row["excluded_title_rows"],
                row["excluded_note_rows"],
                row["excluded_blank_rows"],
                row["excluded_tail_note_rows"],
                row["excluded_orphan_numeric_rows"],
                row["excluded_parse_failed_rows"],
                row["pending_rows"],
                row["unrecognized_rows"],
                row["business_rows_used"],
                row["raw_amount_total"],
                row["detail_amount_total"],
                row["proof_status"],
                row["proof_reason"],
                row["c_control_status"],
                row["c_control_value"],
                row["c_control_source_json"],
                row["c_control_evidence_id"],
                row["ab_row_set_status"],
                row["ab_row_set_hash"],
                row["ab_independence_level"],
                evidence_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        new_proof_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO row_classifications(
                   proof_id, project_id, sheet_id, row_number, class_code, reason_code,
                   source_range_json, raw_values_json, calculated_amount, effective_amount,
                   participates_in_a, participates_in_b, participates_in_c, is_pending,
                   is_parse_failed, evidence_id, created_at)
               SELECT ?, project_id, sheet_id, row_number, class_code, reason_code,
                      source_range_json, raw_values_json, calculated_amount, effective_amount,
                      participates_in_a, participates_in_b, participates_in_c, is_pending,
                      is_parse_failed, evidence_id, created_at
                 FROM row_classifications WHERE proof_id=?""",
            (new_proof_id, int(row["id"])),
        )
        rebound += 1
    return rebound


def _validation_evidence_ids_before_invalidation(
    conn: sqlite3.Connection,
    project_id: int,
    period_ids: list[int],
    previous_contract: run_contract.RunContract | None,
) -> tuple[set[int], set[int]]:
    """读取失效化前仍属于旧当前运行的校核 Evidence。

    ``invalidate_crosscheck_results`` 会先刷新 Run Contract。若在刷新后再用
    ``current_scope`` 查询，旧结果已经不再命中当前 ``run_id``，其 Evidence
    ID 便会丢失，随后只能删除结果表而无法把旧证据标成历史。本函数因此在
    契约切换前按旧运行身份取快照，且只纳入结果表直接引用的 Evidence；原始
    行来源/C 控制证据仍属于 source，不被错误历史化。
    """
    if previous_contract is None:
        identity_sql = "run_id IS NULL AND run_signature IS NULL"
        identity_params: tuple[object, ...] = ()
    else:
        identity_sql = "run_id=? AND run_signature=?"
        identity_params = (previous_contract.run_id, previous_contract.signature)
    period_sql = ""
    period_params: tuple[object, ...] = ()
    if period_ids:
        placeholders = ",".join("?" for _ in period_ids)
        period_sql = f" AND period_id IN ({placeholders})"
        period_params = tuple(period_ids)
    result_evidence_ids: set[int] = set()
    proof_evidence_ids: set[int] = set()
    for table in ("crosscheck_results", "period_totals"):
        rows = conn.execute(
            f"""SELECT evidence_id FROM {table}
                WHERE project_id=? AND evidence_id IS NOT NULL
                  AND {identity_sql}{period_sql}""",
            (int(project_id), *identity_params, *period_params),
        ).fetchall()
        result_evidence_ids.update(int(row["evidence_id"]) for row in rows)
    rows = conn.execute(
        f"""SELECT evidence_id FROM sheet_coverage_proofs
            WHERE project_id=? AND evidence_id IS NOT NULL
              AND {identity_sql}{period_sql}""",
        (int(project_id), *identity_params, *period_params),
    ).fetchall()
    proof_evidence_ids.update(int(row["evidence_id"]) for row in rows)
    return result_evidence_ids, proof_evidence_ids


def invalidate_crosscheck_results(
    conn: sqlite3.Connection,
    project_id: int,
    period_ids: set[int] | list[int] | tuple[int, ...] | None = None,
) -> int:
    """在结算范围或人工门控发生变化后撤销旧的最新校核结果。

    证据历史仍保留在 ``evidence`` 表中；这里只清除可被工作台、导出和
    项目总览当作“最近校核”的结果，避免输入变更后继续显示旧的绿色结论。
    ``period_ids=None`` 表示项目级变更（新增文件、方向或待确认状态变化）。
    """
    # 必须在刷新契约前捕获旧运行身份。若先 ensure，再按 current_scope 查，
    # 旧结果会因新 run_id 立即退出读取面，Evidence ID 也随之丢失，无法写入
    # “历史结果——不参与当前结论”的明确边界。
    previous_contract = run_contract.get_current_contract(conn, project_id)
    # 首次导入只建立输入，不强行创建一个尚未运行的契约；已有运行结果的
    # 项目则立即计算新签名并把旧导出标为 stale。
    run_contract.ensure_if_materialized(conn, project_id)
    ids = sorted({int(pid) for pid in (period_ids or [])})
    old_result_evidence_ids, old_proof_evidence_ids = _validation_evidence_ids_before_invalidation(
        conn, project_id, ids, previous_contract
    )
    current_contract = run_contract.get_current_contract(conn, project_id)
    contract_changed = (
        (previous_contract is None and current_contract is not None)
        or (
            previous_contract is not None
            and current_contract is not None
            and previous_contract.run_id != current_contract.run_id
        )
    )
    old_evidence_ids = old_result_evidence_ids | (
        old_proof_evidence_ids if contract_changed else set()
    )
    with run_contract._transaction(conn, "invalidate_crosscheck"):
        evidence_api.mark_historical(
            conn,
            project_id,
            old_evidence_ids,
            "本次结算范围或人工门控发生变化，旧校核结果已移出当前读取面，需重新运行",
            commit=False,
        )
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"DELETE FROM crosscheck_results WHERE project_id=? AND period_id IN ({placeholders})",
                (project_id, *ids),
            )
            conn.execute(
                f"""UPDATE period_totals
                   SET cross_check_status='pending', cross_check_diff=NULL,
                       evidence_id=NULL, ab_status='pending', ab_diff=NULL,
                       control_status='not_available', control_diff=NULL,
                       verification_level='insufficient'
                   WHERE project_id=? AND period_id IN ({placeholders})""",
                (project_id, *ids),
            )
        else:
            cur = conn.execute(
                "DELETE FROM crosscheck_results WHERE project_id=?", (project_id,)
            )
            conn.execute(
                """UPDATE period_totals
                   SET cross_check_status='pending', cross_check_diff=NULL,
                       evidence_id=NULL, ab_status='pending', ab_diff=NULL,
                       control_status='not_available', control_diff=NULL,
                       verification_level='insufficient'
                   WHERE project_id=?""",
                (project_id,),
            )
    return int(cur.rowcount if cur.rowcount is not None else 0)


def set_project_direction(
    conn: sqlite3.Connection,
    project_id: int,
    period_id: int,
    direction: str,
    *,
    actor: str,
    reason: str,
) -> int:
    """原子修改期次方向，并同步失效化、证据和审计。

    UI 只能调用这个核心入口，不能先提交业务字段再补写审计。任何一步
    失败都会回滚方向、运行契约切换、旧结果失效和证据记录。
    """
    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    if direction not in {"upward", "downward", "unknown"}:
        raise ValueError(f"不支持的结算方向: {direction!r}")
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("标记期次方向必须记录原因（原则 14）")
    period = conn.execute(
        """SELECT id, period_no, title, direction FROM settlement_periods
           WHERE id=? AND project_id=?""",
        (period_id, project_id),
    ).fetchone()
    if not period:
        raise ValueError(f"period {period_id} 不属于 project {project_id}")
    old_direction = period["direction"] or "unknown"
    if old_direction == direction:
        return 0
    collision = conn.execute(
        """SELECT id FROM settlement_periods
           WHERE project_id=? AND period_no=? AND direction=? AND id<>?""",
        (project_id, period["period_no"], direction, period_id),
    ).fetchone()
    if collision:
        raise sqlite3.IntegrityError(
            f"第{period['period_no']}期在方向 {direction!r} 已存在期次"
        )

    before = {
        "period_id": int(period_id),
        "period_no": int(period["period_no"]),
        "title": period["title"],
        "direction": old_direction,
    }
    after = {**before, "direction": direction}
    previous_contract = run_contract.get_current_contract(conn, project_id)
    with run_contract._transaction(conn, "set_project_direction"):
        changed = conn.execute(
            """UPDATE settlement_periods SET direction=?
               WHERE id=? AND project_id=? AND direction=?""",
            (direction, period_id, project_id, old_direction),
        )
        if changed.rowcount != 1:
            raise RuntimeError("期次方向在操作期间发生变化，请刷新后重试")
        # 审计本身属于 Run Contract 输入，必须在契约刷新前写入快照；
        # 刷新后再把这条责任记录绑定到最终当前运行，避免“刚写审计又
        # 产生第三个运行”的自失效窗口。
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            actor,
            "set_direction",
            f"period:{period_id}",
            before,
            after,
            reason,
            commit=False,
            run_id=previous_contract.run_id if previous_contract else None,
            run_signature=previous_contract.signature if previous_contract else None,
        )
        # 方向属于运行范围；契约切换与旧校核结果失效必须处于同一事务。
        invalidate_crosscheck_results(conn, project_id)
        active_contract = run_contract.get_current_contract(conn, project_id)
        if active_contract is not None:
            # 方向修改不会改变原始网格、表头或逐行分类；但方向是当前
            # Evidence 的作用域，必须追加一份绑定新运行且携带新方向的
            # 不可变 coverage 快照。否则旧 ``unknown`` 方向证明会在严格
            # 摘要闸门中被当成跨方向证据，导致合法的重跑无法形成有条件
            # 结果。原 proof/Evidence 仍由失效化逻辑保留为历史记录。
            _rebind_coverage_proofs_for_run(
                conn,
                project_id,
                previous_contract,
                active_contract,
                reason="期次方向人工修正；原始网格和逐行覆盖分类未改变",
            )
        if active_contract is not None:
            conn.execute(
                "UPDATE audit_log SET run_id=?, run_signature=? WHERE id=?",
                (active_contract.run_id, active_contract.signature, audit_id),
            )
        evidence_api.add_evidence(
            conn,
            project_id,
            "direction_change",
            f"第{period['period_no']}期方向由 {old_direction} 改为 {direction}：{reason.strip()}",
            steps=[{"step": "人工标记方向", "actor": actor, "reason": reason.strip()}],
            sources=[{"period_id": int(period_id), "period_no": int(period["period_no"])}],
            commit=False,
            run_signature=active_contract.signature if active_contract else None,
            run_id=active_contract.run_id if active_contract else None,
            scope="human",
        )
    return 1


def _cn_to_int(s: str) -> int:
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s.isdigit():
        return int(s)
    return digits.get(s, 0)


def guess_period_no(path: Path) -> int | None:
    m = _PERIOD_RE.search(path.stem)
    return _cn_to_int(m.group(1)) if m else None


def load_sheet_grid(conn: sqlite3.Connection, sheet_id: int) -> tuple[dict, list[str], int, int]:
    rows = conn.execute(
        "SELECT row, col, raw_value, cached_value, is_formula FROM raw_cells WHERE sheet_id=?",
        (sheet_id,),
    ).fetchall()
    cells: dict[tuple[int, int], str] = {}
    for r in rows:
        text = r["cached_value"] if r["is_formula"] and r["cached_value"] is not None else r["raw_value"]
        if text:
            cells[(r["row"], r["col"])] = text
    meta = conn.execute(
        "SELECT sheet_name, n_rows, n_cols, merged_ranges_json FROM raw_sheets WHERE id=?",
        (sheet_id,),
    ).fetchone()
    merged = json.loads(meta["merged_ranges_json"])
    return cells, merged, meta["n_rows"], meta["n_cols"]


def _merge_bounds(range_text: str) -> tuple[int, int, int, int] | None:
    """解析 A1 合并范围，返回 (row_start, row_end, col_start, col_end)。"""
    match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", (range_text or "").replace("$", ""))
    if not match:
        return None
    from openpyxl.utils import column_index_from_string

    return (
        int(match.group(2)),
        int(match.group(4)),
        column_index_from_string(match.group(1)),
        column_index_from_string(match.group(3)),
    )


def _header_only_merged_ranges(merged_ranges: list[str], header_row_hi: int) -> list[str]:
    """只把表头区合并交给字段识别/抽取，禁止明细区锚点复制。"""
    out: list[str] = []
    for range_text in merged_ranges:
        bounds = _merge_bounds(range_text)
        if bounds is None or bounds[1] <= header_row_hi:
            out.append(range_text)
    return out


def _data_merged_ranges(
    merged_ranges: list[str], data_range: tuple[int, int],
) -> list[str]:
    """返回与物理数据行相交的合并范围，作为结构性复核风险。"""
    start, end = data_range
    out: list[str] = []
    for range_text in merged_ranges:
        bounds = _merge_bounds(range_text)
        if bounds is None:
            continue
        if bounds[0] <= end and bounds[1] >= start:
            out.append(range_text)
    return out


def _json_object(value: str | None, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _structural_gate_reasons(
    sheet_meta: sqlite3.Row,
    data_merged_ranges: list[str],
    *,
    manual_range_confirmed: bool = False,
) -> list[str]:
    """从不可变解析元数据生成导入层结构性证据缺口。"""
    reasons: list[str] = []
    hidden_rows = _json_object(sheet_meta["hidden_rows_json"], [])
    hidden_cols = _json_object(sheet_meta["hidden_cols_json"], [])
    if not isinstance(hidden_rows, list):
        hidden_rows = []
    if not isinstance(hidden_cols, list):
        hidden_cols = []
    if (hidden_rows or hidden_cols) and not manual_range_confirmed:
        reasons.append(
            "存在隐藏行/列，实际参与校核的取数范围需人工确认"
        )
    if str(sheet_meta["filter_state"] or "none") == "unknown":
        reasons.append("工作表/表格存在筛选条件，筛选影响的实际可见行无法从文件确定")
    formula_metadata = _json_object(sheet_meta["formula_metadata_json"], {})
    try:
        unverified = int(formula_metadata.get("unverified_count", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        unverified = 0
    limitations = formula_metadata.get("limitations", [])
    if formula_metadata.get("capability_limited") or limitations:
        if isinstance(limitations, str):
            limitations = [limitations]
        if not isinstance(limitations, list):
            limitations = []
        detail = "；".join(str(item) for item in limitations[:3])
        reasons.append(
            "解析器能力不足，无法证明公式/合并/筛选结构完整"
            + (f"（{detail}）" if detail else "")
        )
    if unverified:
        reasons.append(f"存在 {unverified} 个公式缓存未验证单元格，金额不得直接作为确定性依据")
    if data_merged_ranges:
        reasons.append(
            f"明细区存在合并单元格 {data_merged_ranges[:5]}，已禁止自动锚点复制，需人工确认"
        )
    return reasons


def next_period_no(conn: sqlite3.Connection, project_id: int,
                   direction: str = "unknown") -> int:
    """下一个可用期号——按方向独立递增（对上/对下各自有序）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(period_no), 0) AS m FROM settlement_periods"
        " WHERE project_id=? AND direction=?",
        (project_id, direction),
    ).fetchone()
    return int(row["m"]) + 1


def ensure_period(
    conn: sqlite3.Connection,
    project_id: int,
    period_no: int,
    title: str,
    source_file_id: int | None,
    direction: str = "unknown",
    contract_party: str = "",
    *,
    commit: bool = True,
) -> int:
    """按 (project_id, period_no, direction) 定位或创建期次。

    对上/对下各自拥有独立的期号序列：同一 period_no 的不同方向是两个期次，
    不得复用（方向隔离）。同方向同期号重复调用幂等复用（重复导入安全）。
    """
    row = conn.execute(
        "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=? AND direction=?",
        (project_id, period_no, direction),
    ).fetchone()
    if row:
        return int(row["id"])
    def _insert() -> int:
        cur = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, source_file_id, direction, contract_party)"
            " VALUES (?,?,?,?,?,?)",
            (project_id, period_no, title, source_file_id, direction, contract_party),
        )
        return int(cur.lastrowid)

    if commit:
        with run_contract._transaction(conn, "ensure_period"):
            return _insert()
    return _insert()


def _route_role_review(conn: sqlite3.Connection, project_id: int, file_id: int,
                       sheet_id: int, sheet_name: str,
                       confidence: float | None, oversized: bool,
                       reason: str | None = None,
                       *, commit: bool = True) -> None:
    """需角色审阅 sheet：留 evidence 候选与审计，不写 canonical tables。"""
    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    reason = reason or ("超长汇总/台账/核销型结构，疑似非结算清单"
                        if oversized else f"表头识别置信度不足（{confidence}）")
    set_sheet_status(conn, sheet_id, "pending", reason=reason, actor="system")
    evidence_api.add_evidence(
        conn, project_id, "sheet_role_candidate",
        f"Sheet「{sheet_name}」待人工角色确认：{reason}",
        steps=[{"step": "角色门控", "confidence": confidence, "reason": reason}],
        sources=[{"file_id": file_id, "sheet_id": sheet_id, "location": "整表",
                  "confidence": confidence}],
        commit=commit,
    )
    audit_log.record_audit(
        conn, project_id, "system", "route_role_review", f"sheet:{sheet_id}",
        None, {"sheet": sheet_name, "confidence": confidence, "oversized": oversized},
        f"角色审阅路由：{reason}；保留 raw 网格，未进入结算模型",
        commit=commit,
    )


def _validate_confirmed_col_map(col_map: dict, max_col: int) -> None:
    """校验人工确认列映射。

    支持两类真实结算结构：
    - 工程量计价：name + quantity + unit_price；
    - 金额计价/比例计价：name + amount（工程量可以真实缺失，绝不补 0）。
    """
    if "name" not in col_map:
        raise ValueError("确认列映射缺少必需字段: name")
    amount_path = "amount" in col_map
    quantity_price_path = "quantity" in col_map and "unit_price" in col_map
    if not amount_path and not quantity_price_path:
        raise ValueError(
            "确认列映射缺少必需的完整金额口径：需 amount，或同时提供 quantity + unit_price")
    cols = list(col_map.values())
    if len(cols) != len(set(cols)):
        raise ValueError("确认列映射存在冲突：同一列被映射到多个字段")
    if any(not isinstance(c, int) or c < 1 or c > max_col for c in cols):
        raise ValueError(f"确认列映射列号越界（有效范围 1..{max_col}）")


def confirm_sheet_role_and_extract(conn: sqlite3.Connection, project_id: int,
                                   sheet_id: int, actor: str, reason: str,
                                   direction: str = "unknown",
                                   confirmed_col_map: dict | None = None,
                                   confirmed_header_range: tuple[int, int] | None = None,
                                   confirmed_data_range: tuple[int, int] | None = None,
                                   period_no: int | None = None) -> int:
    """人工确认被门控 sheet 的角色为结算清单后，重放语义层抽取（ADR-008）。

    - reason 必填（原则 14）；
    - det.needs_review（歧义/低置信）时必须显式传 confirmed_col_map（人工真正
      选择列），未传即拒绝；无歧义 sheet（仅角色语义被挡）可仅确认角色；
    - 审计记录 old（检测到的映射）/new（人工确认的映射）；
    - 校验：必需字段、列号存在且不冲突。
    """
    from dataclasses import replace

    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工确认角色必须记录原因（原则 14）")
    cells, merged, n_rows, n_cols = load_sheet_grid(conn, sheet_id)
    meta = conn.execute(
        """SELECT rs.sheet_name, sf.id AS file_id, sf.original_name,
                  rs.hidden_rows_json, rs.hidden_cols_json,
                  rs.filter_state, rs.filter_conditions_json, rs.table_ranges_json,
                  rs.formula_metadata_json, rs.auto_filter_ref,
                  rs.merged_ranges_json
           FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (sheet_id, project_id),
    ).fetchone()
    if not meta:
        raise ValueError(f"sheet {sheet_id} 不属于 project {project_id}")
    sheet_name = meta["sheet_name"]
    det = detect_header(0, cells, merged, n_rows, n_cols)
    if det is None:
        if confirmed_col_map is None or confirmed_header_range is None:
            raise ValueError(
                "该 sheet 无可识别表头，需同时提供人工列映射与表头范围后才能按清单抽取")
        det = HeaderDetection(
            sheet_index=0,
            header_row_lo=confirmed_header_range[0],
            header_row_hi=confirmed_header_range[1],
            col_map=dict(confirmed_col_map),
            confidence=0.0,
            needs_review=True,
            notes=["表头由人工确认"],
        )

    if det.needs_review and confirmed_col_map is None:
        raise ValueError(
            "该 sheet 表头存在歧义（needs_review），必须显式传入人工确认的列映射 "
            "confirmed_col_map 后才能写入结算模型")

    used_map = dict(det.col_map)
    if confirmed_col_map is not None:
        _validate_confirmed_col_map(confirmed_col_map, n_cols)
        used_map = dict(confirmed_col_map)

    header_lo, header_hi = det.header_row_lo, det.header_row_hi
    if confirmed_header_range is not None:
        header_lo, header_hi = confirmed_header_range
        if not (isinstance(header_lo, int) and isinstance(header_hi, int)
                and 1 <= header_lo <= header_hi <= n_rows):
            raise ValueError(f"确认表头范围无效（有效行 1..{n_rows}）")
    if confirmed_data_range is not None:
        data_start, data_end = confirmed_data_range
        if not (isinstance(data_start, int) and isinstance(data_end, int)
                and header_hi < data_start <= data_end <= n_rows):
            raise ValueError(
                f"确认数据行范围无效：应在表头末行 {header_hi} 之后且不超过 {n_rows}")
        data_range = (data_start, data_end)
        data_range_status = "confirmed"
        data_range_method = "manual_confirmation"
    else:
        from jiadun.core.parsing.header_detect import data_rows_range

        data_range = data_rows_range(
            cells, replace(det, header_row_lo=header_lo, header_row_hi=header_hi), n_rows)
        data_range_status = "inferred" if data_range[1] >= data_range[0] else "unproven"
        data_range_method = "last_non_empty_row" if data_range_status == "inferred" else "no_data_rows"

    # 表头区合并可用于识别列标题；明细区合并不得自动把左上锚点值复制
    # 到其它物理行。人工确认仍保留合并风险元数据，后续可在 Evidence 中
    # 说明人工是否接受该结构。
    extraction_merged_ranges = _header_only_merged_ranges(merged, header_hi)
    data_merged_ranges = _data_merged_ranges(merged, data_range)
    structural_reasons = _structural_gate_reasons(
        meta,
        data_merged_ranges,
        manual_range_confirmed=confirmed_data_range is not None,
    )

    # 幂等/重复确认拒绝：已确认的 sheet 不能重复写入；自动识别但因
    # 结构性风险处于 pending 的 sheet 允许人工重新指定范围/映射，且
    # 会复用原期次并替换该 Sheet 的旧规范明细，避免重复累计。
    already = conn.execute(
        "SELECT period_id, sheet_status FROM raw_sheets WHERE id=?", (sheet_id,)).fetchone()
    existing_period_id = (
        int(already["period_id"])
        if already and already["period_id"] is not None else None
    )
    if existing_period_id is not None and str(already["sheet_status"] or "") != "pending":
        raise ValueError(
            f"该 sheet 已完成确认抽取（period_id={existing_period_id}），不得重复确认")

    # 先抽取（items 非空才建 period，避免失败留空期次）
    det_used = replace(det, header_row_lo=header_lo, header_row_hi=header_hi,
                       col_map=used_map, needs_review=False)
    items = extract_items.extract_items(
        cells, extraction_merged_ranges, det_used, n_rows, data_range=data_range
    )
    if not items:
        raise ValueError("确认后抽取 0 行：请核对人工列映射（未创建期次）")
    proof_draft, proof_rows = coverage_proof.build_sheet_coverage_proof(
        cells,
        det_used,
        n_rows,
        merged_ranges=extraction_merged_ranges,
        data_range=data_range,
        hidden_rows=json.loads(meta["hidden_rows_json"] or "[]"),
        hidden_cols=json.loads(meta["hidden_cols_json"] or "[]"),
        manual_range_confirmed=confirmed_data_range is not None,
    )
    previous_contract = run_contract.get_current_contract(conn, project_id)

    with run_contract._transaction(conn, "confirm_sheet_role_and_extract"):
        audit_log.record_audit(
            conn, project_id, actor, "confirm_sheet_role", f"sheet:{sheet_id}",
            {"role": "gated", "needs_review": det.needs_review,
             "mapping": {"detected": det.col_map}},
            {"role": "settlement", "confidence": det.confidence,
             "mapping": {"detected": det.col_map, "confirmed": used_map},
             "header_range": [header_lo, header_hi], "data_range": list(data_range),
             "period_no": period_no}, reason, commit=False,
            run_id=previous_contract.run_id if previous_contract else None,
            run_signature=previous_contract.signature if previous_contract else None)
        if existing_period_id is not None and period_no is None:
            period_id = existing_period_id
            pno = conn.execute(
                "SELECT period_no FROM settlement_periods WHERE id=?", (period_id,)
            ).fetchone()["period_no"]
        else:
            pno = period_no if period_no is not None else next_period_no(conn, project_id, direction)
            if not isinstance(pno, int) or pno < 1:
                raise ValueError("确认期次必须为正整数")
            period_id = ensure_period(
                conn, project_id, pno, f"{meta['original_name']}/{sheet_name}", meta["file_id"],
                direction=direction, commit=False)
        if existing_period_id is not None and period_id == existing_period_id:
            conn.execute(
                "DELETE FROM line_items WHERE period_id=? AND sheet_id=?",
                (period_id, sheet_id),
            )
        n = extract_items.persist_line_items(conn, period_id, sheet_id, items, commit=False)
        # 回写：sheet→期次关联 + 已确认列映射（needs_review 归零，保持证据链）
        conn.execute("UPDATE raw_sheets SET period_id=? WHERE id=?",
                     (period_id, sheet_id))
        set_sheet_status(
            conn,
            sheet_id,
            "pending" if structural_reasons else "confirmed",
            reason=(
                f"人工确认结算清单角色并抽取 {n} 行：{reason.strip()}；"
                "仍存在结构性证据缺口：" + "；".join(structural_reasons)
                if structural_reasons
                else f"人工确认结算清单角色并抽取 {n} 行：{reason.strip()}"
            ),
            actor=actor,
        )
        conn.execute("DELETE FROM table_headers WHERE sheet_id=?", (sheet_id,))
        conn.execute(
            """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
               col_map_json, confidence, needs_review, data_row_start, data_row_end,
               data_range_status, data_range_method, data_range_evidence_json)
               VALUES (?,?,?,?,?,0,?,?,?,?,?)""",
            (sheet_id, header_lo, header_hi, json.dumps(used_map), det.confidence,
             data_range[0], data_range[1], data_range_status, data_range_method,
             json.dumps({
                 "method": data_range_method,
                 "header_range": [header_lo, header_hi],
                 "data_range": list(data_range),
                 "actor": actor,
                 "hidden_rows": json.loads(meta["hidden_rows_json"] or "[]"),
                 "hidden_cols": json.loads(meta["hidden_cols_json"] or "[]"),
                 "visibility_risk": bool(meta["hidden_rows_json"] and meta["hidden_rows_json"] != "[]")
                                  or bool(meta["hidden_cols_json"] and meta["hidden_cols_json"] != "[]"),
                 "auto_filter_ref": meta["auto_filter_ref"],
                 "filter_state": meta["filter_state"] or "none",
                 "filter_conditions": _json_object(meta["filter_conditions_json"], []),
                 "table_ranges": _json_object(meta["table_ranges_json"], []),
                 "formula_metadata": _json_object(meta["formula_metadata_json"], {}),
                 "data_merged_ranges": data_merged_ranges,
                 "merge_anchor_copy": False,
                 "duplicate_header_rows": list(getattr(det, "duplicate_header_rows", []) or []),
                 "pre_header_nonempty_rows": list(getattr(det, "pre_header_nonempty_rows", []) or []),
                 "pre_header_suspect_rows": list(getattr(det, "pre_header_suspect_rows", []) or []),
                 "unresolved_candidate_count": int(getattr(det, "unresolved_candidate_count", 0) or 0),
                 "risk_flags": list(getattr(det, "risk_flags", []) or []),
                 "filter_reviewed_by": actor if meta["filter_state"] == "unknown" else None,
                 "merge_reviewed_by": actor if data_merged_ranges else None,
             }, ensure_ascii=False)))
        invalidate_crosscheck_results(conn, project_id)
        current_contract = run_contract.ensure_run_contract(conn, project_id)
        evidence_api.add_evidence(
            conn, project_id, "sheet_role_confirmed",
            f"Sheet「{sheet_name}」经人工确认为结算清单，重放抽取 {n} 行",
            steps=[{"step": "人工确认", "actor": actor, "reason": reason,
                    "mapping": {"detected": det.col_map, "confirmed": used_map},
                    "header_range": [header_lo, header_hi], "data_range": list(data_range),
                    "period_no": pno, "confidence": det.confidence}],
            sources=[{"sheet_id": sheet_id, "period_id": period_id, "n_items": n}],
            commit=False,
            run_signature=current_contract.signature,
            run_id=current_contract.run_id,
            scope="human",
        )
        _rebind_coverage_proofs_for_run(
            conn,
            project_id,
            previous_contract,
            current_contract,
            reason="人工确认结算 Sheet 并重放抽取；既有结算 Sheet 证明内容未改变",
        )
        _persist_coverage_proof(
            conn,
            project_id=project_id,
            file_id=int(meta["file_id"]),
            batch_id=int(conn.execute(
                "SELECT batch_id FROM raw_sheets WHERE id=?", (sheet_id,)
            ).fetchone()["batch_id"]),
            sheet_id=sheet_id,
            period_id=period_id,
            direction=direction,
            proof=proof_draft,
            rows=proof_rows,
            run_signature=current_contract.signature,
            run_id=current_contract.run_id,
        )
    return n


NON_SETTLEMENT_ROLES = {
    "non_settlement_form",
    "settlement_summary",
    "supporting_evidence",
    "contract_control",
    "other_non_settlement",
}


def confirm_sheet_non_settlement_role(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    actor: str,
    confirmed_role: str,
    reason: str,
) -> None:
    """人工确认被门控 sheet 仅作表单/汇总/控制证据，不进入结算模型。"""
    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工确认角色必须记录原因（原则 14）")
    if confirmed_role not in NON_SETTLEMENT_ROLES:
        raise ValueError(
            f"不支持的非结算角色 {confirmed_role!r}；允许值: {sorted(NON_SETTLEMENT_ROLES)}")
    meta = conn.execute(
        """SELECT rs.sheet_name, rs.period_id, sf.id AS file_id
           FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (sheet_id, project_id),
    ).fetchone()
    if not meta:
        raise ValueError(f"sheet {sheet_id} 不属于 project {project_id}")
    if meta["period_id"] is not None:
        raise ValueError("该 sheet 已进入结算模型，不能再确认成非结算角色")
    duplicate = conn.execute(
        """SELECT id FROM audit_log WHERE project_id=? AND target=?
           AND action='confirm_sheet_non_settlement_role' LIMIT 1""",
        (project_id, f"sheet:{sheet_id}"),
    ).fetchone()
    if duplicate:
        raise ValueError("该 sheet 的非结算角色已确认，不得重复确认")

    previous_contract = run_contract.get_current_contract(conn, project_id)
    with run_contract._transaction(conn, "confirm_sheet_non_settlement_role"):
        set_sheet_status(
            conn, sheet_id, "non_business",
            reason=f"人工确认为 {confirmed_role}，仅作证据：{reason.strip()}",
            actor=actor,
        )
        audit_log.record_audit(
            conn, project_id, actor, "confirm_sheet_non_settlement_role", f"sheet:{sheet_id}",
            {"role": "gated"}, {"role": confirmed_role}, reason, commit=False,
        )
        invalidate_crosscheck_results(conn, project_id)
        # 角色确认本身属于新的运行输入。先让失效化逻辑固定新契约，再把
        # 人工 Evidence 绑定到该契约，避免 human 证据落在旧/空运行上。
        current_contract = run_contract.ensure_run_contract(conn, project_id)
        evidence_api.add_evidence(
            conn, project_id, "sheet_role_confirmed",
            f"Sheet「{meta['sheet_name']}」经人工确认为 {confirmed_role}，仅作证据，不进入结算模型",
            steps=[{"step": "人工确认非结算角色", "actor": actor,
                    "role": confirmed_role, "reason": reason}],
            sources=[{"file_id": meta["file_id"], "sheet_id": sheet_id, "location": "整表"}],
            commit=False,
            run_signature=current_contract.signature,
            run_id=current_contract.run_id,
            scope="human",
        )
        _rebind_coverage_proofs_for_run(
            conn,
            project_id,
            previous_contract,
            current_contract,
            reason="人工确认非结算 Sheet 角色；结算 Sheet 原始网格和逐行分类未改变",
        )


def _route_form_sheet(conn: sqlite3.Connection, project_id: int, file_id: int,
                      sheet_id: int, sheet_name: str,
                      cells: dict[tuple[int, int], str]) -> None:
    """键值对表单 → 待人工确认的证据候选（仅通用 evidence 表 + 审计）。

    纪律（监督第六轮）：
    - 不写 settlement_period / line_items / period_totals（防污染结算模型）；
    - 不写 contract_docs / contract_facts（合同模块专用，避免虚假合同风险）；
    - 每条候选 = 一条 evidence（kind='form_field_candidate'，sources 含
      file_id/sheet_id/行列/原文，summary 明确待人工确认）+ audit 留痕；
    - 不新建数据库迁移。
    """
    import re

    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    kv_pairs = []
    max_col = max(c for _r, c in cells) if cells else 0
    for (row, col), text in sorted(cells.items()):
        t = (text or "").strip()
        m = re.match(r"^(.{2,24}?)[：:]\s*(.+)$", t)
        if m and not any(w in t for w in ("清单编码", "清单名称", "合价")):
            kv_pairs.append((m.group(1).strip(), m.group(2).strip(), row, col, t))
            continue
        # 键列 + 同行右侧第一个非空格作为值（间隔布局）。
        # 标签先去掉尾部冒号再匹配后缀——"申请单位名称："不能因冒号而漏掉。
        label = t.rstrip("：:")
        if t and len(label) <= 24 and not any(
            w in t for w in ("清单编码", "清单名称", "合价")
        ) and (label.endswith("名称") or label.endswith("行") or label.endswith("编号")
               or label.endswith("信息") or label.endswith("金额")):
            value_col, nxt = next(
                ((c2, cells.get((row, c2), "").strip())
                 for c2 in range(col + 1, max_col + 1)
                 if cells.get((row, c2), "").strip()),
                (None, ""))
            if value_col is not None and nxt:
                # 位置记真实值列（可反向定位到原格），不是 col+1
                kv_pairs.append((label, nxt, row, value_col, f"{t} {nxt}"))
    with run_contract._transaction(conn, "route_form_sheet"):
        set_sheet_status(
            conn, sheet_id, "pending",
            reason="检测为键值对表单，等待人工确认是否为非业务表",
            actor="system",
        )
        for key, value, row, col, quote in kv_pairs[:40]:
            evidence_api.add_evidence(
                conn, project_id, "form_field_candidate",
                f"表单字段候选（待人工确认）：{key} = {value[:60]}",
                steps=[{"step": "表单路由", "sheet": sheet_name, "status": "待人工确认"}],
                sources=[{"file_id": file_id, "sheet_id": sheet_id,
                          "location": f"行{row}列{col}", "quote": quote[:120]}],
                commit=False,
            )
        audit_log.record_audit(
            conn, project_id, "system", "route_non_settlement_form", f"sheet:{sheet_id}",
            None, {"sheet": sheet_name, "n_candidates": len(kv_pairs[:40])},
            "键值对表单路由：保留原 Sheet，字段候选存证据表待人工复核，未进入结算与合同模型",
            commit=False,
        )


def guess_period_no_from_text(text: str) -> int | None:
    m = _PERIOD_RE.search(text or "")
    return _cn_to_int(m.group(1)) if m else None


def _existing_import_report(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    direction: str,
    period_no: int | None,
) -> ImportReport | None:
    """查找同一 SHA 已经形成的规范导入，避免重复追加明细。

    ``source_files`` 按 SHA 复用只是原件层幂等；如果不检查这里，重复选择
    同一文件仍会新建 parse_batch/raw_sheet/line_items，A/B/C 便可能一起把
    金额放大。方向或显式期次发生变化时不猜测用户意图，直接要求通过明确
    的补充/版本入口处理，而不是静默写入旧期次。
    """
    batches = conn.execute(
        """SELECT pb.id, pb.status, pb.parsed_at
             FROM parse_batches pb
            WHERE pb.file_id=? ORDER BY pb.parsed_at DESC, pb.id DESC""",
        (file_id,),
    ).fetchall()
    for batch in batches:
        sheets = conn.execute(
            """SELECT rs.id, rs.sheet_name, rs.sheet_status, rs.period_id,
                      sp.period_no, sp.direction
                 FROM raw_sheets rs
                 LEFT JOIN settlement_periods sp ON sp.id=rs.period_id
                WHERE rs.batch_id=? ORDER BY rs.sheet_index, rs.id""",
            (batch["id"],),
        ).fetchall()
        # 只有已经写入 raw_sheets 的成功/部分批次才构成规范导入幂等边界；
        # 解析失败批次允许用户重新选择后重试，且失败事实仍保留。
        if not sheets:
            continue
        existing_directions = {
            str(row["direction"] or "unknown")
            for row in sheets if row["direction"] is not None
        }
        if existing_directions and direction not in existing_directions:
            raise ValueError(
                "同一原始文件已按其他方向导入；请使用明确的补充导入/版本入口，"
                "不要把同一文件静默追加到旧期次"
            )
        existing_periods = {
            int(row["period_no"]) for row in sheets if row["period_no"] is not None
        }
        if period_no is not None and existing_periods and period_no not in existing_periods:
            raise ValueError(
                "同一原始文件已绑定其他期次；请使用明确的版本或补充资料入口"
            )
        period_rows = [row for row in sheets if row["period_id"] is not None]
        item_count = 0
        reports: list[SheetReport] = []
        for row in sheets:
            if row["period_id"] is not None:
                count = conn.execute(
                    """SELECT COUNT(*) AS c FROM line_items
                         WHERE period_id=? AND sheet_id=?""",
                    (row["period_id"], row["id"]),
                ).fetchone()
                n_items = int(count["c"] or 0)
                item_count += n_items
            else:
                n_items = 0
            state = str(row["sheet_status"] or "pending")
            reports.append(
                SheetReport(
                    row["sheet_name"],
                    "parsed" if state == "confirmed" else "needs_review",
                    n_items=n_items,
                    state_code=state,
                    notes=["同一 SHA-256 原始文件已导入，本次选择未重复写入明细"],
                )
            )
        first_period = period_rows[0] if period_rows else None
        return ImportReport(
            file_id=file_id,
            batch_id=int(batch["id"]),
            period_no=int(first_period["period_no"]) if first_period else (period_no or 0),
            period_id=int(first_period["period_id"]) if first_period else -1,
            status="ok" if all(row["sheet_status"] == "confirmed" for row in sheets) else "partial",
            sheets=reports,
            message="same_source_already_imported",
            needs_manual_review=any(row["sheet_status"] != "confirmed" for row in sheets),
        )
    return None


def import_settlement_file(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    period_no: int | None = None,
    direction: str = "unknown",
    contract_party: str = "",
    document_category: str = "unclassified",
) -> ImportReport:
    """以单个外层事务执行文件物化，失败时只保留 failed 批次和 Evidence。"""
    src = Path(src)
    # 原始文件副本是可恢复的输入资产，先登记并提交；后续数据库物化失败
    # 时，副本仍可被同一 SHA 的下一次重试复用，不会留下无法解释的来源。
    sf = import_file(conn, project_id, project_dir, src, commit=True)
    from jiadun.core import document_intake

    document_intake.record_document(
        conn, project_id, sf.file_id, category=document_category,
        parse_status="processing", parser="settlement",
    )
    # PDF/Word/图片结算清单：表格解析尚不支持（fail-closed 转待人工处理并给出
    # 可行动指引），不得包装成莫名失败；原件保持只读，等待用户另存 Excel/CSV。
    if sf.file_type in {"pdf", "doc", "docx", "image"}:
        guidance = (
            f"{sf.file_type.upper()} 结算清单暂不支持自动表格解析：原件已只读保留。"
            "请将该文件另存为 Excel/CSV 后重新导入；或在资料中心把它重新分类为"
            "合同/审计报告等其他类别（PDF 合同与审计报告走逐页提取管线）。"
        )
        from jiadun.core import document_intake as _di

        with run_contract._transaction(conn, "persist_unsupported_settlement"):
            _di.mark_document_status(
                conn, project_id, sf.file_id,
                parse_status="needs_review", detail=guidance,
                parser="settlement", commit=False,
            )
            evidence_api.add_evidence(
                conn, project_id, "parse_failure",
                f"文件「{sf.original_name}」为 {sf.file_type.upper()} 格式，"
                "结算表格解析暂不支持，已登记为待人工处理",
                steps=[{
                    "step": "导入物化",
                    "status": "needs_review",
                    "reason": guidance,
                    "action": "另存为 Excel/CSV 后重新导入，或重新分类",
                }],
                sources=[{
                    "file_id": sf.file_id,
                    "location": "文件级导入管线",
                    "original_name": sf.original_name,
                }],
                commit=False,
                scope="source",
            )
        run_contract.ensure_if_materialized(conn, project_id)
        return ImportReport(
            sf.file_id,
            None,
            period_no or next_period_no(conn, project_id, direction),
            -1,
            "partial",
            sheets=[
                SheetReport(
                    sf.original_name,
                    "needs_review",
                    state_code="pending",
                    notes=[guidance],
                )
            ],
            message=guidance,
            needs_manual_review=True,
        )
    try:
        with run_contract._transaction(conn, "import_settlement_file"):
            report = _import_settlement_file(
                conn,
                project_id,
                project_dir,
                src,
                period_no=period_no,
                direction=direction,
                contract_party=contract_party,
                _source_file=sf,
            )
    except Exception as exc:
        # 任一物化阶段失败均不得把已写入的 raw/period/line_items 当作成功
        # 结果。外层事务已回滚；这里重新登记一个无 Sheet 的 failed 批次，
        # 保留可追溯错误并允许用户重新选择/重试。
        error = f"{type(exc).__name__}: {exc}"
        failed_result = excel_parser.ParseResult(
            parser="pipeline",
            status="failed",
            stats={"pipeline_error": error},
            error=error,
        )
        with run_contract._transaction(conn, "persist_import_failure"):
            failed_batch_id = excel_parser.persist_parse_result(
                conn, sf.file_id, failed_result, commit=False
            )
            evidence_api.add_evidence(
                conn,
                project_id,
                "parse_failure",
                f"文件「{sf.original_name}」导入物化失败：{error}",
                steps=[
                    {
                        "step": "导入物化",
                        "status": "failed",
                        "error": error,
                        "rollback": "raw/period/line_items 已回滚",
                    }
                ],
                sources=[
                    {
                        "file_id": sf.file_id,
                        "batch_id": failed_batch_id,
                        "location": "文件级导入管线",
                        "original_name": sf.original_name,
                    }
                ],
                commit=False,
                scope="source",
            )
        run_contract.ensure_if_materialized(conn, project_id)
        fallback = period_no or next_period_no(conn, project_id, direction)
        report = ImportReport(
            sf.file_id,
            failed_batch_id,
            fallback,
            -1,
            "failed",
            sheets=[
                SheetReport(
                    sf.original_name,
                    "parse_failed",
                    state_code="parse_failed",
                    notes=[error],
                )
            ],
            message=error,
        )
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="failed",
            detail=error, parser="settlement",
        )
        return report
    status = "parsed" if report.status == "ok" else "needs_review"
    document_intake.mark_document_status(
        conn, project_id, sf.file_id, parse_status=status,
        detail=report.message or "", parser="settlement",
    )
    return report


def _import_settlement_file(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    period_no: int | None = None,
    direction: str = "unknown",
    contract_party: str = "",
    *,
    _source_file: SourceFile | None = None,
) -> ImportReport:
    """导入并解析一个结算文件。

    期次归属（v2 模型）：一个工作簿可含多期。逐 Sheet 判定期号——
    Sheet 名或表前标题含"第N期"则用之；否则用文件名期号；再否则按递增编号。
    """
    src = Path(src)
    sf = _source_file or import_file(conn, project_id, project_dir, src, commit=False)
    existing_report = _existing_import_report(
        conn,
        project_id,
        sf.file_id,
        direction=direction,
        period_no=period_no,
    )
    if existing_report is not None:
        return existing_report
    file_period = period_no if period_no is not None else guess_period_no(src)
    used_increment = file_period is None

    result = excel_parser.parse_file(Path(sf.stored_path), sf.file_type)
    # 入口必须先独立盘点源工作簿的 Sheet 目录，再接受解析器的结果。
    # 对 xlsx/xlsm，缺失或不一致的源清单都意味着解析范围无法证明完整；
    # 即使剩余 Sheet 的 A/B/C 数值碰巧相等，也只能进入待人工复核。
    source_census_status = str(result.stats.get("source_census_status") or "")
    source_census_differences = result.stats.get("source_census_differences", [])
    if not isinstance(source_census_differences, list):
        source_census_differences = [str(source_census_differences)]
    source_scope_unproven = sf.file_type == "xlsx" and source_census_status != "complete"
    source_scope_reason = (
        "；".join(str(item) for item in source_census_differences if str(item).strip())
        if source_census_differences
        else "源文件 Sheet 清单缺失或无法与解析结果对照"
    )
    if source_scope_unproven:
        result.stats["source_scope_gate_reason"] = source_scope_reason
        if result.status == "ok":
            result.status = "partial"
    # ``partial`` 仍然保留原始网格供预览/人工映射，但所有结构性能力缺口
    # 必须沿导入链路落库并让工作表进入 pending；只有真正的 unsupported/
    # failed 才走“无可解析 Sheet”的失败分支。
    if result.status not in {"ok", "partial"}:
        # 文件登记已经发生；解析失败也必须形成持久化批次和来源 Evidence，
        # 否则重新打开项目后只剩 source_files，无法知道该文件为何没有进入
        # raw_sheets，也无法让统一 Sheet 状态机阻断项目级结论。
        with run_contract._transaction(conn, "persist_parse_failure"):
            batch_id = excel_parser.persist_parse_result(
                conn, sf.file_id, result, commit=False
            )
            evidence_api.add_evidence(
                conn,
                project_id,
                "parse_failure",
                f"文件「{sf.original_name}」解析失败：{result.error or result.status}",
                steps=[
                    {
                        "step": "文件解析",
                        "parser": result.parser,
                        "status": result.status,
                        "error": result.error,
                    }
                ],
                sources=[
                    {
                        "file_id": sf.file_id,
                        "batch_id": batch_id,
                        "location": "文件级解析",
                        "original_name": sf.original_name,
                    }
                ],
                commit=False,
                scope="source",
            )
        # 即使解析失败，已有项目的旧结果也不能继续作为当前输入下的成果。
        # 首次导入仍不创建空运行契约。
        run_contract.ensure_if_materialized(conn, project_id)
        fallback = file_period or next_period_no(conn, project_id, direction)
        return ImportReport(
            sf.file_id,
            batch_id,
            fallback,
            -1,
            "failed",
            sheets=[
                SheetReport(
                    sf.original_name,
                    "parse_failed",
                    state_code="parse_failed",
                    notes=[result.error or result.status],
                )
            ],
            message=result.error,
        )

    from jiadun.core.parsing.excel_parser import persist_parse_result

    try:
        batch_id = persist_parse_result(conn, sf.file_id, result, commit=False)
    except excel_parser.ParseResultValidationError as exc:
        # 解析器元数据（尤其是 Sheet 索引/网格坐标）不满足落库合同。
        # 这类错误必须在首个 INSERT 前转换为一个无 Sheet 的 failed 批次，
        # 保留失败 Evidence，同时绝不能把部分 raw_sheets/期次/明细留在库中。
        error = f"{type(exc).__name__}: {exc}"
        failed_result = excel_parser.ParseResult(
            parser=result.parser,
            status="failed",
            stats={**result.stats, "validation_error": error},
            error=error,
        )
        with run_contract._transaction(conn, "persist_parse_validation_failure"):
            failed_batch_id = persist_parse_result(
                conn, sf.file_id, failed_result, commit=False
            )
            evidence_api.add_evidence(
                conn,
                project_id,
                "parse_failure",
                f"文件「{sf.original_name}」解析结果元数据无效：{exc}",
                steps=[
                    {
                        "step": "解析结果结构校验",
                        "parser": result.parser,
                        "status": "failed",
                        "error": error,
                    }
                ],
                sources=[
                    {
                        "file_id": sf.file_id,
                        "batch_id": failed_batch_id,
                        "location": "文件级 Sheet 索引/网格元数据",
                        "original_name": sf.original_name,
                    }
                ],
                commit=False,
                scope="source",
            )
        run_contract.ensure_if_materialized(conn, project_id)
        fallback = file_period or next_period_no(conn, project_id, direction)
        return ImportReport(
            sf.file_id,
            failed_batch_id,
            fallback,
            -1,
            "failed",
            sheets=[
                SheetReport(
                    sf.original_name,
                    "parse_failed",
                    state_code="parse_failed",
                    notes=[error],
                )
            ],
            message=error,
        )

    if source_scope_unproven:
        evidence_api.add_evidence(
            conn,
            project_id,
            "source_sheet_scope_mismatch",
            f"文件「{sf.original_name}」源文件 Sheet 范围无法证明完整：{source_scope_reason}",
            steps=[
                {
                    "step": "源工作簿 Sheet 目录盘点",
                    "status": source_census_status or "unavailable",
                    "census": result.stats.get("source_census"),
                    "parsed_sheet_count": len(result.sheets),
                    "differences": source_census_differences,
                }
            ],
            sources=[
                {
                    "file_id": sf.file_id,
                    "batch_id": batch_id,
                    "location": "文件级 Sheet 范围",
                    "original_name": sf.original_name,
                }
            ],
            commit=False,
            scope="source",
        )

    parse_partial = result.status == "partial"
    report = ImportReport(sf.file_id, batch_id, file_period or 0, -1, "partial")
    if parse_partial:
        report.needs_manual_review = True
        report.message = "parser_capability_limited_needs_manual_review"
    parsed_any = False
    form_routed = False
    role_gated = False
    structural_gate = source_scope_unproven
    period_ids: set[int] = set()
    # 覆盖证明要绑定“整次导入完成后”的 Run Contract。导入过程中期次、
    # Sheet 和明细仍在逐步落库，若逐 Sheet 立即 ensure 契约，会把前半个
    # 文件错误地绑定到中间状态；先保留草稿，等输入范围稳定后统一写入。
    pending_coverage_proofs: list[dict[str, object]] = []
    # 文档级语义门控：文件名含汇总/核销/台账语义 → 整文件需角色审阅
    # （copy 文件名是用户/验收语境的真实命名，非 test_id 硬编码）
    doc_summary_like = bool(SUMMARY_LIKE_PATTERN.search(src.stem))
    for sheet in result.sheets:
        sheet_id = conn.execute(
            "SELECT id FROM raw_sheets WHERE batch_id=? AND sheet_index=?", (batch_id, sheet.sheet_index)
        ).fetchone()["id"]
        sheet_meta = conn.execute(
            """SELECT filter_state, filter_conditions_json, table_ranges_json,
                      formula_metadata_json, merged_ranges_json,
                      hidden_rows_json, hidden_cols_json
                 FROM raw_sheets WHERE id=?""",
            (sheet_id,),
        ).fetchone()
        cells, merged, n_rows, _ = load_sheet_grid(conn, sheet_id)
        det = detect_header(sheet.sheet_index, cells, merged, n_rows, sheet.n_cols)
        from jiadun.core.parsing.header_detect import detect_form_like

        # ---- 隐藏 Sheet 门控（基线核查 C4）----
        # 隐藏/深度隐藏工作表即使是规范清单表也不得自动确认写入结算模型：
        # 隐藏态常用于底稿/草稿/废弃口径，可见状态不参与业务判断。原始网格
        # 已入保真层（raw_sheets/raw_cells），数据零丢失；人工确认角色后可
        # 通过既有重解析入口参与。
        visible_state = str(getattr(sheet, "visible_state", "") or "").strip().lower()
        visibility_unknown = visible_state not in {"visible", "hidden", "veryhidden"}
        if visible_state in {"hidden", "veryhidden"} or visibility_unknown:
            visibility_reason = (
                f"隐藏工作表（{visible_state}）"
                if not visibility_unknown
                else f"工作表可见性未知（{sf.file_type}解析器未提供可见性）"
            )
            set_sheet_status(
                conn, sheet_id, "pending",
                reason=f"{visibility_reason}：未经人工确认角色前不写入结算模型；"
                       "请确认该 Sheet 是否参与对上/对下后再重新解析",
                actor="system",
            )
            report.sheets.append(SheetReport(
                sheet.sheet_name, "needs_review",
                notes=[f"检测到{visibility_reason}：为避免底稿/草稿数据混入结算，"
                       "已保留原始网格并转为待人工确认"],
                state_code="pending"))
            role_gated = True
            continue

        # ---- sheet 级写入闸门（监督第八轮）----
        form_level = detect_form_like(cells, merged)
        form_like = form_level == "strong" or (det is None and form_level is not None)
        if form_like:
            # 键值对表单结构优先于弱清单识别（含 det 非 None 的误命中页）
            _route_form_sheet(conn, project_id, sf.file_id, sheet_id, sheet.sheet_name, cells)
            report.sheets.append(SheetReport(
                sheet.sheet_name, "non_settlement_form",
                notes=["检测为键值对表单（非结算清单）：已按待人工复核的表单字段候选记录，"
                       "保留原 Sheet 与单元格；请人工确认后使用通用 evidence 人工复核入口"],
                state_code="pending"))
            form_routed = True
            continue
        # 语义门控：sheet 名含汇总/核销/台账 → 无论表头多强都需角色确认
        summary_like = bool(SUMMARY_LIKE_PATTERN.search(sheet.sheet_name))
        # needs_review 一律挡（监督第九轮）：歧义/低置信未经人工确认不得写 canonical
        needs_review_gate = det is not None and det.needs_review
        no_quantity_column = det is not None and "quantity" not in det.col_map
        if doc_summary_like or summary_like or needs_review_gate or no_quantity_column:
            reason = ("文档文件名含汇总/核销/台账语义，整文件需角色确认" if doc_summary_like else
                      "sheet 名含汇总/核销/台账语义" if summary_like else
                      "表头存在歧义或识别不可靠（needs_review）" if needs_review_gate else
                      "未识别到数量（计量）列，结算清单结构不完整")
            _route_role_review(conn, project_id, sf.file_id, sheet_id, sheet.sheet_name,
                               det.confidence if det else None, False, reason,
                               commit=False)
            report.sheets.append(SheetReport(
                sheet.sheet_name, "needs_role_review",
                notes=[f"{reason}：未经人工确认角色前不写入结算模型；"
                       "字段候选已存 evidence，请人工选择角色"
                       "（confirm_sheet_role_and_extract）"],
                state_code="pending"))
            role_gated = True
            continue
        if det is None:
            # 无法识别表头的 Sheet 仍属于待人工确认，不应被整体报告静默归为
            # “导入失败”或让项目状态看起来像没有待处理事项。
            set_sheet_status(
                conn, sheet_id, "pending",
                reason="未识别到可靠表头，等待人工指定角色、表头和字段映射",
                actor="system",
            )
            report.sheets.append(SheetReport(
                sheet.sheet_name, "no_header",
                notes=["未识别到可靠表头，保留原始网格，需人工指定角色、表头和字段映射"],
                state_code="pending",
            ))
            role_gated = True
            continue
        # 固化本次抽取实际使用的数据范围。旧实现只在校核时临时调用
        # data_rows_range，无法证明“导入时看到的范围”和“校核时回读的范围”
        # 一致；现在把推断方法与证据一并落库，后续可检测原始网格是否变化。
        from jiadun.core.parsing.header_detect import data_rows_range

        detected_data_range = data_rows_range(cells, det, n_rows)
        range_start, range_end = detected_data_range
        has_data_range = range_end >= range_start
        stored_start = range_start if has_data_range else None
        stored_end = range_end if has_data_range else None
        range_status = "inferred" if has_data_range else "unproven"
        range_method = "last_non_empty_row" if has_data_range else "no_data_rows"
        data_merged_ranges = _data_merged_ranges(merged, detected_data_range)
        extraction_merged_ranges = _header_only_merged_ranges(merged, det.header_row_hi)
        structural_reasons = _structural_gate_reasons(sheet_meta, data_merged_ranges)
        if source_scope_unproven:
            structural_reasons.insert(
                0,
                "源文件 Sheet 清单与解析结果不一致，当前工作表取数范围需人工确认："
                + source_scope_reason,
            )
        range_evidence = {
            "method": range_method,
            "header_range": [det.header_row_lo, det.header_row_hi],
            "data_range": [stored_start, stored_end],
            "max_row": n_rows,
            "basis": "表头之后最后一个非空行",
            "hidden_rows": list(sheet.hidden_rows),
            "hidden_cols": list(sheet.hidden_cols),
            "visibility_risk": bool(sheet.hidden_rows or sheet.hidden_cols),
            "auto_filter_ref": sheet.auto_filter_ref,
            "filter_state": sheet_meta["filter_state"],
            "filter_conditions": _json_object(sheet_meta["filter_conditions_json"], []),
            "table_ranges": _json_object(sheet_meta["table_ranges_json"], []),
            "formula_metadata": _json_object(sheet_meta["formula_metadata_json"], {}),
            "data_merged_ranges": data_merged_ranges,
            "merge_anchor_copy": False,
            "duplicate_header_rows": list(getattr(det, "duplicate_header_rows", []) or []),
            "pre_header_nonempty_rows": list(getattr(det, "pre_header_nonempty_rows", []) or []),
            "pre_header_suspect_rows": list(getattr(det, "pre_header_suspect_rows", []) or []),
            "unresolved_candidate_count": int(getattr(det, "unresolved_candidate_count", 0) or 0),
            "risk_flags": list(getattr(det, "risk_flags", []) or []),
        }
        proof_draft, proof_rows = coverage_proof.build_sheet_coverage_proof(
            cells,
            det,
            n_rows,
            merged_ranges=extraction_merged_ranges,
            data_range=detected_data_range,
            hidden_rows=list(sheet.hidden_rows),
            hidden_cols=list(sheet.hidden_cols),
        )
        # 数据区内部存在空白、标题或说明行时，自动识别无法证明这些行
        # 是安全排除还是漏掉了业务明细；保留逐行 Evidence，并把工作表
        # 置为 pending，等待人工确认范围。
        # 仅把明细/控制行之间的数据区内部空白、说明行视为未闭合范围。
        # 表头下方为多层表头预留的前置空行不属于业务数据区，不应让
        # 合法的 GB 表式被误报为部分导入；内部空白仍必须人工确认。
        detail_numbers = [
            item.row_number for item in proof_rows if item.class_code == "detail"
        ]
        control_numbers = [
            item.row_number
            for item in proof_rows
            if item.class_code in {"subtotal", "grand_total"}
        ]
        if detail_numbers:
            interior_lo = min(detail_numbers)
            interior_hi = max(control_numbers or detail_numbers)
            excluded_by_kind = {
                "空白": sum(
                    1 for item in proof_rows
                    if item.class_code == "blank"
                    and interior_lo < item.row_number < interior_hi
                ),
                "说明/备注": sum(
                    1 for item in proof_rows
                    if item.class_code == "note"
                    and interior_lo < item.row_number < interior_hi
                ),
            }
        else:
            excluded_by_kind = {"空白": 0, "说明/备注": 0}
        for label, count in excluded_by_kind.items():
            if count:
                structural_reasons.append(
                    f"数据区存在被排除的{label}行（{count} 行），实际范围需人工确认"
                )
        structural_reasons = list(dict.fromkeys(structural_reasons))
        skip_stats: dict = {}
        items = extract_items.extract_items(
            cells, extraction_merged_ranges, det, n_rows,
            data_range=detected_data_range, stats=skip_stats)
        skip_notes = []
        if skip_stats.get("title_rows"):
            skip_notes.append(
                f"跳过分部/章节标题行 {skip_stats['title_rows']} 行（非清单项，原文见保真层）")
        if skip_stats.get("tail_note_rows"):
            skip_notes.append(
                f"剔除明细区尾部尾注行 {skip_stats['tail_note_rows']} 行（原文见保真层）")
        if not items:
            notes = ["header-like but 0 data rows"] + skip_notes
            set_sheet_status(
                conn, sheet_id, "pending",
                reason="识别到表头但数据区没有可写入的明细，等待人工确认范围",
                actor="system",
            )
            pending_coverage_proofs.append({
                "file_id": sf.file_id,
                "batch_id": batch_id,
                "sheet_id": sheet_id,
                "period_id": None,
                "direction": direction,
                "proof": proof_draft,
                "rows": proof_rows,
            })
            report.sheets.append(
                SheetReport(sheet.sheet_name, "no_header", notes=notes, state_code="pending")
            )
            continue

        # ---- 逐 Sheet 期次判定 ----
        # 优先级：用户显式指定 > 文件名期号 > Sheet 名期号 > 递增。
        # 递增按导入 direction 独立（对上/对下各自有序）；文件名与 Sheet 名期号
        # 冲突时按文件名（导入语境更明确）并提示复核。
        sheet_pno = guess_period_no_from_text(sheet.sheet_name) or guess_period_no_from_text(
            " ".join(t for (r, _c), t in sorted(cells.items()) if r <= 5)
        )
        notes: list[str] = []
        if used_increment:
            pno = (next_period_no(conn, project_id, direction) if parsed_any
                   else (file_period or sheet_pno or next_period_no(conn, project_id, direction)))
        elif file_period is not None:
            pno = file_period
            if sheet_pno is not None and sheet_pno != file_period:
                notes.append(f"Sheet 名期号({sheet_pno})与文件名期号({file_period})不一致，按文件名处理，请复核")
        else:
            pno = sheet_pno if sheet_pno is not None else next_period_no(conn, project_id, direction)
        title = f"{src.stem}/{sheet.sheet_name}"
        period_id = ensure_period(
            conn,
            project_id,
            pno,
            title,
            sf.file_id,
            direction,
            contract_party,
            commit=False,
        )
        if structural_reasons:
            structural_gate = True
        conn.execute("UPDATE raw_sheets SET period_id=? WHERE id=?", (period_id, sheet_id))
        set_sheet_status(
            conn, sheet_id, "pending" if structural_reasons else "confirmed",
            reason=(
                "自动表头与数据范围已识别，但存在结构性证据缺口："
                + "；".join(structural_reasons)
                if structural_reasons
                else "自动表头与数据范围通过确定性规则识别并写入结算模型"
            ),
            actor="system",
        )
        conn.execute(
            """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi, col_map_json,
               confidence, needs_review, data_row_start, data_row_end,
               data_range_status, data_range_method, data_range_evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sheet_id, det.header_row_lo, det.header_row_hi,
             json.dumps(det.col_map), det.confidence, int(det.needs_review),
             stored_start, stored_end, range_status, range_method,
             json.dumps(range_evidence, ensure_ascii=False)),
        )
        period_ids.add(period_id)

        extract_items.persist_line_items(
            conn, period_id, sheet_id, items, commit=False
        )
        pending_coverage_proofs.append({
            "file_id": sf.file_id,
            "batch_id": batch_id,
            "sheet_id": sheet_id,
            "period_id": period_id,
            "direction": direction,
            "proof": proof_draft,
            "rows": proof_rows,
        })
        parsed_any = True
        status = "needs_review" if det.needs_review else "parsed"
        report.sheets.append(
            SheetReport(
                sheet.sheet_name, status,
                n_items=len([i for i in items if not i.flags.get("subtotal")]),
                n_subtotal=len([i for i in items if i.flags.get("subtotal")]),
                confidence=det.confidence,
                notes=det.notes + notes + skip_notes + structural_reasons,
                state_code="pending" if structural_reasons else "confirmed",
            )
        )
    report.period_id = next(iter(period_ids), -1)
    report.period_no = min(
        (conn.execute("SELECT period_no FROM settlement_periods WHERE id=?", (pid,)).fetchone()["period_no"]
         for pid in period_ids), default=0,
    )
    # 任务书 B4：重解析后按「同名 + 单元格摘要一致」结转上一批次的人工
    # 确认（sheet_status 与 list_kind）。人工确认优先于机器门控——结转
    # 会覆盖本批次机器写入的 pending；内容变化的 Sheet 保持待确认。
    from jiadun.core.engine.sheet_inventory import carry_forward_sheet_decisions

    report.carry_forward = carry_forward_sheet_decisions(
        conn, project_id, sf.file_id, batch_id, actor="system"
    )
    if report.carry_forward.get("carried"):
        # 结转后以数据库实际状态重算本文件剩余待确认数量，报告不得虚报
        # pending（全部结转成功时应如实回到 ok）。
        remaining_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM raw_sheets WHERE batch_id=? AND sheet_status='pending'",
            (batch_id,),
        ).fetchone()["c"]
        has_pending = remaining_pending > 0
    else:
        has_pending = role_gated or form_routed or structural_gate
    if parsed_any and not has_pending and not parse_partial:
        report.status = "ok"
    elif parsed_any:
        # 有 canonical 但仍有被挡 sheet：overall=partial，pending 不得被 full_pipeline 掩盖
        report.status = "partial"
        report.needs_manual_review = True
        report.message = (
            "parser_capability_limited_needs_manual_review"
            if parse_partial
            else "settlement_parsed_with_pending_role_review"
        )
    elif form_routed and not role_gated:
        report.status = "partial"
        report.needs_manual_review = True
        report.message = "non_settlement_form_needs_manual_review"
    elif role_gated or form_routed or source_scope_unproven:
        report.status = "partial"
        report.needs_manual_review = True
        report.message = (
            "non_settlement_spreadsheet_needs_role_review" if role_gated
            else "non_settlement_form_needs_manual_review" if form_routed
            else "source_sheet_scope_unproven"
        )
    else:
        report.status = "failed"
        report.message = "no sheets parsed"
    if parsed_any or role_gated or form_routed or source_scope_unproven:
        # 新导入既可能新增明细，也可能只新增待确认工作表；两者都会改变
        # 项目级校核前提，因此旧的最新结果必须重新计算。
        invalidate_crosscheck_results(conn, project_id)
        if pending_coverage_proofs:
            # 只有在整份文件的 Sheet/期次/明细已落库后才固定运行契约，
            # 覆盖证明、逐行分类和其 Evidence 共享同一 run_id/signature。
            active_contract = run_contract.ensure_run_contract(conn, project_id)
            with run_contract._transaction(conn, "persist_import_coverage_proofs"):
                for pending in pending_coverage_proofs:
                    _persist_coverage_proof(
                        conn,
                        project_id=project_id,
                        file_id=int(pending["file_id"]),
                        batch_id=int(pending["batch_id"]),
                        sheet_id=int(pending["sheet_id"]),
                        period_id=(
                            int(pending["period_id"])
                            if pending["period_id"] is not None else None
                        ),
                        direction=str(pending["direction"]),
                        proof=pending["proof"],
                        rows=pending["rows"],
                        run_signature=active_contract.signature,
                        run_id=active_contract.run_id,
                    )
    return report
