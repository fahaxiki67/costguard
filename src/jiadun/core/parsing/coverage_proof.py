"""Sheet 有效区域与逐行分类证明。

该模块把解析器看到的数据区变成可复算的只读事实。它不修改原始网格，金额
始终使用 ``Decimal``；缺失金额不会转成零。A、B 当前使用同一确定性抽取
器时，证明明确记录为非独立，不能仅凭两条路径相等升级为完整校核。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from jiadun.core.engine.money import NotANumberError, money_mul, to_decimal
from jiadun.core.parsing.header_detect import (
    HeaderDetection,
    build_anchor_map,
    data_rows_range,
    detect_duplicate_header_rows,
    detect_pre_header_rows,
    is_grand_total_row,
    is_subtotal_row,
)

D = Decimal

ROW_CLASSES = (
    "blank",
    "detail",
    "subtotal",
    "grand_total",
    "title",
    "note",
    "tail_note",
    "orphan_numeric",
    "parse_failed",
    "pending_review",
    "unrecognized",
)


@dataclass(frozen=True)
class RowClassification:
    row_number: int
    class_code: str
    reason_code: str
    source_range: dict[str, int]
    raw_values: dict[str, str]
    calculated_amount: str | None = None
    effective_amount: str | None = None
    participates_in_a: bool = False
    participates_in_b: bool = False
    participates_in_c: bool = False
    is_pending: bool = False
    is_parse_failed: bool = False
    evidence_id: int | None = None


@dataclass(frozen=True)
class SheetCoverageProof:
    raw_row_start: int
    raw_row_end: int
    raw_col_start: int
    raw_col_end: int
    raw_data_row_count: int
    classified_row_count: int
    counts: dict[str, int]
    raw_amount_total: str | None
    detail_amount_total: str | None
    business_rows_used: int
    proof_status: str
    proof_reason: list[str] = field(default_factory=list)
    ab_row_set_status: str = "same_row_set"
    ab_row_set_hash: str | None = None
    ab_independence_level: str = "shared_extractor"
    duplicate_header_rows: list[int] = field(default_factory=list)
    pre_header_nonempty_rows: list[int] = field(default_factory=list)
    pre_header_suspect_rows: list[int] = field(default_factory=list)
    unresolved_candidate_count: int = 0
    risk_flags: list[str] = field(default_factory=list)


def _text_at(cells: dict[tuple[int, int], str], anchors: dict, row: int, col: int | None) -> str:
    if col is None:
        return ""
    key = anchors.get((row, col), (row, col))
    return str(cells.get(key, "") or "").strip()


def _row_source_range(row: int, columns: list[int]) -> dict[str, int]:
    return {
        "row_start": row,
        "row_end": row,
        "col_start": min(columns) if columns else 1,
        "col_end": max(columns) if columns else 1,
    }


def _safe_decimal(raw: str) -> D | None:
    if not raw:
        return None
    try:
        return to_decimal(raw)
    except NotANumberError:
        return None


def _is_note(text: str) -> bool:
    compact = "".join((text or "").split())
    return compact.startswith(("备注", "说明", "注：", "注:")) or compact in {"注", "说明"}


def build_sheet_coverage_proof(
    cells: dict[tuple[int, int], str],
    det: HeaderDetection,
    max_row: int,
    *,
    merged_ranges: list[str] | None = None,
    data_range: tuple[int, int] | None = None,
    hidden_rows: list[int] | None = None,
    hidden_cols: list[int] | None = None,
    manual_range_confirmed: bool = False,
) -> tuple[SheetCoverageProof, list[RowClassification]]:
    """从原始网格生成证明草稿和逐行分类，不写数据库。

    隐藏行/列默认会使范围保持 ``unproven``。只有用户在人工确认入口
    明确给出数据范围时，才把“范围已确认”作为当前证明条件；隐藏结构
    仍写入 ``proof_reason``，不被静默抹掉。
    """
    anchors = build_anchor_map(merged_ranges or [])
    start, end = data_range if data_range is not None else data_rows_range(cells, det, max_row)
    columns = sorted(set(det.col_map.values()))
    all_columns = sorted({col for (_row, col) in cells})
    col_start = min(all_columns or columns or [1])
    col_end = max(all_columns or columns or [1])
    duplicate_header_rows = detect_duplicate_header_rows(
        cells, det, max_row, col_end, merged_ranges or []
    )
    pre_header_nonempty_rows, pre_header_suspect_rows = detect_pre_header_rows(
        cells, det, col_end
    )
    unresolved_candidate_count = int(getattr(det, "unresolved_candidate_count", 0) or 0)
    risk_flags = list(getattr(det, "risk_flags", []) or [])
    if duplicate_header_rows and "duplicate_header_rows" not in risk_flags:
        risk_flags.append("duplicate_header_rows")
    if pre_header_suspect_rows and "pre_header_business_rows" not in risk_flags:
        risk_flags.append("pre_header_business_rows")
    if unresolved_candidate_count > 1 and "multiple_header_candidates" not in risk_flags:
        risk_flags.append("multiple_header_candidates")
    classifications: list[RowClassification] = []
    numeric_fields = ("quantity", "unit_price", "amount")

    for row in range(start, end + 1):
        row_texts = {
            field: _text_at(cells, anchors, row, col)
            for field, col in det.col_map.items()
        }
        raw_row_has_data = any(
            _text_at(cells, anchors, row, col) for col in range(col_start, col_end + 1)
        )
        source_range = _row_source_range(row, columns or [col_start, col_end])
        if not raw_row_has_data:
            classifications.append(
                RowClassification(row, "blank", "all_cells_empty", source_range, {})
            )
            continue

        name = row_texts.get("name", "")
        code = row_texts.get("code", "")
        numeric_raw = {field: row_texts.get(field, "") for field in numeric_fields}
        numeric_values = {field: _safe_decimal(value) for field, value in numeric_raw.items()}
        parse_failed = any(value and numeric_values[field] is None for field, value in numeric_raw.items())
        lead_text = "".join(
            _text_at(cells, anchors, row, col)
            for col in range(1, (min(columns) if columns else col_start) + 1)
            if _text_at(cells, anchors, row, col)
        )

        if row in duplicate_header_rows:
            class_code = "pending_review"
            reason = "duplicate_header_row"
        elif is_subtotal_row(name, lead_text):
            class_code = "grand_total" if is_grand_total_row(name, lead_text) else "subtotal"
            reason = "grand_total_label" if class_code == "grand_total" else "subtotal_label"
        elif not code and name and not any(numeric_raw.values()):
            class_code = "note" if _is_note(name) else "title"
            reason = "note_label" if class_code == "note" else "name_only_row"
        elif not code and not name and any(numeric_raw.values()):
            class_code = "orphan_numeric"
            reason = "numeric_without_code_or_name"
        elif parse_failed:
            class_code = "parse_failed"
            reason = "numeric_value_unparseable"
        elif det.needs_review:
            class_code = "pending_review"
            reason = "header_mapping_needs_review"
        elif not any(row_texts.get(field, "") for field in ("code", "name", *numeric_fields)):
            class_code = "unrecognized"
            reason = "mapped_fields_empty"
        else:
            class_code = "detail"
            reason = "mapped_detail_row"

        raw_values = {field: value for field, value in row_texts.items() if value}
        calculated: D | None = None
        if numeric_values["quantity"] is not None and numeric_values["unit_price"] is not None:
            calculated = money_mul(numeric_values["quantity"], numeric_values["unit_price"])
        raw_amount = numeric_values["amount"]
        # 数值字段存在但解析失败时保留候选计算过程，不把该行当作可用
        # 业务金额；人工补证前不能用其它字段静默绕过解析失败。
        effective = None if parse_failed else (raw_amount if raw_amount is not None else calculated)
        participates = class_code == "detail" and effective is not None
        classifications.append(
            RowClassification(
                row_number=row,
                class_code=class_code,
                reason_code=reason,
                source_range=source_range,
                raw_values=raw_values,
                calculated_amount=str(calculated) if calculated is not None else None,
                effective_amount=str(effective) if effective is not None else None,
                participates_in_a=participates,
                participates_in_b=participates,
                is_pending=class_code == "pending_review",
                is_parse_failed=class_code == "parse_failed",
            )
        )

    # 与 extract_items 一致：明细区末尾连续孤立数值是尾注，但在证明中保留
    # 它们的原始行号和分类，不再静默丢弃。
    tail_start = len(classifications)
    while tail_start and classifications[tail_start - 1].class_code == "orphan_numeric":
        tail_start -= 1
    if tail_start < len(classifications):
        classifications = [
            *classifications[:tail_start],
            *[
                RowClassification(
                    row_number=item.row_number,
                    class_code="tail_note",
                    reason_code="trailing_orphan_numeric",
                    source_range=item.source_range,
                    raw_values=item.raw_values,
                    calculated_amount=item.calculated_amount,
                    effective_amount=item.effective_amount,
                    is_pending=True,
                )
                for item in classifications[tail_start:]
            ],
        ]

    counts = {key: 0 for key in ROW_CLASSES}
    for item in classifications:
        counts[item.class_code] = counts.get(item.class_code, 0) + 1
    # 统一走 ``_safe_decimal``，不能因为原始单元格包含千分位/货币符号而
    # 在证明计算中绕过业务 Decimal 入口；无法解析的金额保留在行分类中，
    # 不纳入金额合计，也不被当作 0。
    raw_amount_values: list[D] = []
    for item in classifications:
        if item.class_code != "detail":
            continue
        value = _safe_decimal(item.raw_values.get("amount", ""))
        if value is not None:
            raw_amount_values.append(value)
    raw_amount_total = sum(raw_amount_values, D("0")) if raw_amount_values else None
    detail_amount_total = sum(
        (D(item.effective_amount) for item in classifications
         if item.class_code == "detail" and item.effective_amount is not None),
        D(0),
    )
    used_rows = [item.row_number for item in classifications if item.participates_in_a]
    row_set_hash = hashlib.sha256(
        json.dumps(used_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest() if used_rows else None

    # 先收集所有未解决条件，再按最保守的级别合并。人工确认只能解除
    # “隐藏行/列导致的可见性未知”，不能覆盖重复表头、前置业务行、解析
    # 失败或其它未解决行；否则同一张表会同时出现“证明完整”和“仍有结构
    # 风险”的矛盾状态。
    reasons: list[str] = []
    if end < start:
        reasons.append("no_data_range")
    if hidden_rows or hidden_cols:
        reasons.append(
            "hidden_rows_or_columns_manually_confirmed"
            if manual_range_confirmed
            else "hidden_rows_or_columns"
        )
    if pre_header_suspect_rows:
        reasons.append("pre_header_business_rows")
    if unresolved_candidate_count > 1:
        reasons.append("multiple_header_candidates")
    if duplicate_header_rows:
        reasons.append("duplicate_header_rows")
    if det.needs_review:
        reasons.append("header_mapping_needs_review")
    if any(
        counts.get(key, 0)
        for key in (
            "parse_failed",
            "pending_review",
            "unrecognized",
            "orphan_numeric",
            "tail_note",
        )
    ):
        reasons.append("unresolved_rows_present")

    unproven_reasons = {
        "no_data_range",
        "hidden_rows_or_columns",
        "pre_header_business_rows",
        "multiple_header_candidates",
        "header_mapping_needs_review",
    }
    partial_reasons = {"duplicate_header_rows", "unresolved_rows_present"}
    if any(reason in unproven_reasons for reason in reasons):
        proof_status = "unproven"
    elif any(reason in partial_reasons for reason in reasons):
        proof_status = "partial"
    else:
        proof_status = "complete"

    proof = SheetCoverageProof(
        raw_row_start=start,
        raw_row_end=end,
        raw_col_start=col_start,
        raw_col_end=col_end,
        raw_data_row_count=max(0, end - start + 1),
        classified_row_count=len(classifications),
        counts=counts,
        # “明细存在但原始合价全缺失”必须保持 None，不能把初始化的 0
        # 序列化成看似真实的原始控制金额。
        raw_amount_total=str(raw_amount_total) if raw_amount_total is not None else None,
        detail_amount_total=str(detail_amount_total) if used_rows else None,
        business_rows_used=len(used_rows),
        proof_status=proof_status,
        proof_reason=sorted(set(reasons)),
        ab_row_set_hash=row_set_hash,
        duplicate_header_rows=duplicate_header_rows,
        pre_header_nonempty_rows=pre_header_nonempty_rows,
        pre_header_suspect_rows=pre_header_suspect_rows,
        unresolved_candidate_count=unresolved_candidate_count,
        risk_flags=sorted(set(risk_flags)),
    )
    return proof, classifications


def persist_sheet_coverage_proof(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    file_id: int | None,
    batch_id: int | None,
    sheet_id: int,
    period_id: int | None,
    direction: str | None,
    proof: SheetCoverageProof,
    rows: list[RowClassification],
    run_signature: str | None = None,
    run_id: str | None = None,
    c_control_status: str = "not_assessed",
    c_control_value: str | None = None,
    c_control_source: dict[str, Any] | None = None,
    c_control_evidence_id: int | None = None,
    evidence_id: int | None = None,
    commit: bool = False,
) -> int:
    """写入不可变证明和逐行分类；旧证明不覆盖。"""
    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO sheet_coverage_proofs(
                   project_id, run_signature, run_id, file_id, batch_id, sheet_id, period_id,
                   direction, raw_row_start, raw_row_end, raw_col_start, raw_col_end,
                   raw_data_row_count, classified_row_count, classified_detail_rows,
                   excluded_subtotal_rows, excluded_title_rows, excluded_note_rows,
                   excluded_blank_rows, excluded_tail_note_rows, excluded_orphan_numeric_rows,
                   excluded_parse_failed_rows, pending_rows, unrecognized_rows,
                   business_rows_used, raw_amount_total, detail_amount_total, proof_status,
                   proof_reason, c_control_status, c_control_value, c_control_source_json,
                   c_control_evidence_id, ab_row_set_status, ab_row_set_hash,
                   ab_independence_level, evidence_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, run_signature, run_id, file_id, batch_id, sheet_id, period_id,
                direction or "unknown", proof.raw_row_start, proof.raw_row_end,
                proof.raw_col_start, proof.raw_col_end, proof.raw_data_row_count,
                proof.classified_row_count, proof.counts.get("detail", 0),
                proof.counts.get("subtotal", 0) + proof.counts.get("grand_total", 0),
                proof.counts.get("title", 0), proof.counts.get("note", 0),
                proof.counts.get("blank", 0), proof.counts.get("tail_note", 0),
                proof.counts.get("orphan_numeric", 0), proof.counts.get("parse_failed", 0),
                proof.counts.get("pending_review", 0) + proof.counts.get("tail_note", 0),
                proof.counts.get("unrecognized", 0), proof.business_rows_used,
                proof.raw_amount_total, proof.detail_amount_total, proof.proof_status,
                json.dumps(proof.proof_reason, ensure_ascii=False), c_control_status,
                c_control_value, json.dumps(c_control_source or {}, ensure_ascii=False),
                c_control_evidence_id, proof.ab_row_set_status, proof.ab_row_set_hash,
                proof.ab_independence_level, evidence_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        proof_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO row_classifications(
                   proof_id, project_id, sheet_id, row_number, class_code, reason_code,
                   source_range_json, raw_values_json, calculated_amount, effective_amount,
                   participates_in_a, participates_in_b, participates_in_c, is_pending,
                   is_parse_failed, evidence_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    proof_id, project_id, sheet_id, item.row_number, item.class_code,
                    item.reason_code, json.dumps(item.source_range, ensure_ascii=False),
                    json.dumps(item.raw_values, ensure_ascii=False), item.calculated_amount,
                    item.effective_amount, int(item.participates_in_a), int(item.participates_in_b),
                    int(item.participates_in_c), int(item.is_pending), int(item.is_parse_failed),
                    item.evidence_id, datetime.now().isoformat(timespec="seconds"),
                )
                for item in rows
            ],
        )
        return proof_id

    if commit:
        with conn:
            return _insert()
    return _insert()
