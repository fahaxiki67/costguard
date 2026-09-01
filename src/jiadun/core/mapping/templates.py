"""字段映射模板库（P1-03）。

模板只保存经过人工确认的映射及其来源，推荐接口是只读的。推荐结果不能
直接写入 ``table_headers`` 或 ``line_items``；用户必须在界面核对后重新走
现有人工确认入口，确保映射、范围、操作人和原因继续留在 Evidence/Audit。
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence.finding import stable_fingerprint

SUPPORTED_SCOPES = frozenset({"sheet", "project", "global"})
MAPPING_FIELDS = frozenset({
    "code", "name", "feature", "unit", "quantity", "unit_price", "amount", "tax_rate",
})
_WS_RE = re.compile(r"[\s\u00a0\u3000]+")
_PUNCT_RE = re.compile(r"[，。；：、（）()【】\[\]{}<>《》／/\\_-]+")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        decoded = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return decoded


def _normalise_label(value: Any) -> str:
    """用于比较表头的稳定文本；不覆盖或改写原始表头快照。"""
    text = "" if value is None else str(value)
    text = _WS_RE.sub("", text).strip().casefold()
    return _PUNCT_RE.sub("", text)


def _validate_scope(scope: str, project_id: int | None, sheet_id: int) -> tuple[str, str]:
    scope = str(scope).strip().lower()
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(f"不支持的模板作用域: {scope!r}")
    if scope == "sheet":
        if project_id is None:
            raise ValueError("本 Sheet 模板必须绑定项目")
        return scope, f"sheet:{int(sheet_id)}"
    if scope == "project":
        if project_id is None:
            raise ValueError("本项目模板必须绑定项目")
        return scope, f"project:{int(project_id)}"
    return scope, "global"


def _validate_mapping(col_map: dict[str, Any], n_cols: int) -> dict[str, int]:
    if not isinstance(col_map, dict):
        raise ValueError("列映射必须是对象")
    unknown = set(col_map) - MAPPING_FIELDS
    if unknown:
        raise ValueError(f"列映射包含不支持字段: {sorted(unknown)}")
    cleaned: dict[str, int] = {}
    for field, value in col_map.items():
        if not isinstance(field, str) or not isinstance(value, int):
            raise ValueError("列映射字段和列号必须分别为文本和整数")
        if value < 1 or value > int(n_cols):
            raise ValueError(f"列映射列号越界（有效范围 1..{n_cols}）")
        cleaned[field] = int(value)
    if "name" not in cleaned:
        raise ValueError("列映射必须包含名称列")
    if "amount" not in cleaned and not {"quantity", "unit_price"} <= cleaned.keys():
        raise ValueError("金额口径不完整：需合价，或同时提供工程量和单价")
    if len(cleaned.values()) != len(set(cleaned.values())):
        raise ValueError("列映射存在冲突：同一列被映射到多个字段")
    return cleaned


def _sheet_context(conn: sqlite3.Connection, project_id: int | None, sheet_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT rs.id AS sheet_id, rs.sheet_name, rs.n_rows, rs.n_cols,
                  rs.batch_id, pb.file_id, sf.project_id, sf.original_name,
                  sf.sha256, th.header_row_lo AS detected_header_lo,
                  th.header_row_hi AS detected_header_hi,
                  th.data_row_start AS detected_data_start,
                  th.data_row_end AS detected_data_end
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           LEFT JOIN table_headers th ON th.sheet_id=rs.id
           WHERE rs.id=?""",
        (int(sheet_id),),
    ).fetchone()
    if row is None or (project_id is not None and int(row["project_id"]) != int(project_id)):
        raise ValueError(f"sheet {sheet_id} 不属于指定项目")
    return dict(row)


def _header_snapshot(
    conn: sqlite3.Connection,
    sheet_id: int,
    header_range: tuple[int, int],
    n_cols: int,
) -> tuple[list[dict[str, Any]], str]:
    lo, hi = header_range
    if not isinstance(lo, int) or not isinstance(hi, int) or not (1 <= lo <= hi):
        raise ValueError("表头范围必须为正整数且首行不大于末行")
    rows = conn.execute(
        """SELECT row, col, raw_value, cached_value, is_formula
           FROM raw_cells WHERE sheet_id=? AND row BETWEEN ? AND ?
           ORDER BY row, col""",
        (int(sheet_id), lo, hi),
    ).fetchall()
    labels: list[dict[str, Any]] = []
    normalised: list[dict[str, Any]] = []
    for row in rows:
        value = row["cached_value"] if row["is_formula"] and row["cached_value"] is not None else row["raw_value"]
        if value is None or not str(value).strip():
            continue
        col = int(row["col"])
        if col < 1 or col > int(n_cols):
            continue
        original = str(value)
        labels.append({"row": int(row["row"]), "col": col, "value": original})
        normalised.append({
            "row_offset": int(row["row"]) - lo,
            "col": col,
            "value": _normalise_label(original),
        })
    if not normalised:
        raise ValueError("指定表头范围没有可保存的非空表头")
    signature = stable_fingerprint({
        "height": hi - lo + 1,
        "labels": normalised,
    })
    return labels, signature


@dataclass(frozen=True)
class MappingTemplate:
    template_id: int
    project_id: int | None
    scope: str
    scope_key: str
    template_name: str
    version: int
    header_signature: str
    header_labels: list[dict[str, Any]]
    col_map: dict[str, int]
    header_row_lo: int
    header_row_hi: int
    data_row_start: int | None
    data_row_end: int | None
    source_file_id: int | None
    source_sheet_id: int
    source_reference: dict[str, Any]
    created_by: str
    created_at: str
    note: str
    evidence_id: int | None


@dataclass(frozen=True)
class MappingTemplateRecommendation:
    template: MappingTemplate
    score: Decimal
    exact_header_match: bool
    scope_priority: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template.template_id,
            "template_name": self.template.template_name,
            "version": self.template.version,
            "scope": self.template.scope,
            "scope_key": self.template.scope_key,
            "score": str(self.score),
            "exact_header_match": self.exact_header_match,
            "scope_priority": self.scope_priority,
            "reason": self.reason,
            "col_map": dict(self.template.col_map),
            "source_reference": dict(self.template.source_reference),
            "created_by": self.template.created_by,
            "created_at": self.template.created_at,
            "evidence_id": self.template.evidence_id,
            "requires_manual_confirmation": True,
        }


def _row_to_template(row: sqlite3.Row) -> MappingTemplate:
    return MappingTemplate(
        template_id=int(row["id"]),
        project_id=int(row["project_id"]) if row["project_id"] is not None else None,
        scope=row["scope"],
        scope_key=row["scope_key"],
        template_name=row["template_name"],
        version=int(row["version"]),
        header_signature=row["header_signature"],
        header_labels=_loads(row["header_labels_json"], []),
        col_map=_loads(row["col_map_json"], {}),
        header_row_lo=int(row["header_row_lo"]),
        header_row_hi=int(row["header_row_hi"]),
        data_row_start=int(row["data_row_start"]) if row["data_row_start"] is not None else None,
        data_row_end=int(row["data_row_end"]) if row["data_row_end"] is not None else None,
        source_file_id=int(row["source_file_id"]) if row["source_file_id"] is not None else None,
        source_sheet_id=int(row["source_sheet_id"]),
        source_reference=_loads(row["source_reference_json"], {}),
        created_by=row["created_by"],
        created_at=row["created_at"],
        note=row["note"],
        evidence_id=int(row["evidence_id"]) if row["evidence_id"] is not None else None,
    )


def save_mapping_template(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    *,
    scope: str,
    template_name: str,
    col_map: dict[str, Any],
    header_range: tuple[int, int],
    data_range: tuple[int, int] | None = None,
    created_by: str,
    reason: str,
    note: str = "",
) -> MappingTemplate:
    """保存人工确认模板；不会修改当前 Sheet 映射或自动套用模板。"""
    if not str(template_name).strip():
        raise ValueError("模板名称不能为空")
    if not str(created_by).strip():
        raise ValueError("模板创建人不能为空")
    if not str(reason).strip():
        raise audit_log.AuditReasonRequiredError("保存映射模板必须记录原因")
    ctx = _sheet_context(conn, project_id, sheet_id)
    cleaned_map = _validate_mapping(col_map, int(ctx["n_cols"] or 0))
    lo, hi = header_range
    if hi > int(ctx["n_rows"] or 0):
        raise ValueError("表头范围超过工作表行数")
    if data_range is not None:
        data_start, data_end = data_range
        if not (isinstance(data_start, int) and isinstance(data_end, int)
                and hi < data_start <= data_end <= int(ctx["n_rows"] or 0)):
            raise ValueError("数据行范围无效：必须在表头后且不超过工作表行数")
    else:
        data_start = data_end = None
    scope, scope_key = _validate_scope(scope, project_id, sheet_id)
    labels, signature = _header_snapshot(conn, sheet_id, (lo, hi), int(ctx["n_cols"] or 0))
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM mapping_templates "
        "WHERE scope=? AND scope_key=? AND template_name=?",
        (scope, scope_key, str(template_name).strip()),
    ).fetchone()
    version = int(row["v"] or 0) + 1
    current = run_contract.get_current_contract(conn, project_id)
    source_reference = {
        "file_id": int(ctx["file_id"]),
        "original_name": ctx["original_name"],
        "file_sha256": ctx["sha256"],
        "sheet_id": int(sheet_id),
        "sheet_name": ctx["sheet_name"],
        "header_range": [lo, hi],
        "data_range": [data_start, data_end] if data_range is not None else None,
        "header_signature": signature,
        "requires_manual_confirmation": True,
    }
    created_at = _now()
    with run_contract._transaction(conn, "save_mapping_template"):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "mapping_template",
            f"字段映射模板「{str(template_name).strip()}」v{version}：仅作为人工复核候选",
            steps=[{
                "step": "人工保存字段映射模板",
                "scope": scope,
                "scope_key": scope_key,
                "version": version,
                "col_map": cleaned_map,
                "header_range": [lo, hi],
                "data_range": [data_start, data_end] if data_range is not None else None,
                "reason": reason.strip(),
            }],
            sources=[source_reference],
            commit=False,
            run_signature=current.signature if current else None,
            run_id=current.run_id if current else None,
            scope="human",
        )
        cur = conn.execute(
            """INSERT INTO mapping_templates(
                   project_id, scope, scope_key, template_name, version,
                   header_signature, header_labels_json, col_map_json,
                   header_row_lo, header_row_hi, data_row_start, data_row_end,
                   source_file_id, source_sheet_id, source_reference_json,
                   created_by, created_at, note, evidence_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id if scope != "global" else None,
                scope,
                scope_key,
                str(template_name).strip(),
                version,
                signature,
                _json(labels),
                _json(cleaned_map),
                lo,
                hi,
                data_start,
                data_end,
                int(ctx["file_id"]),
                int(sheet_id),
                _json(source_reference),
                str(created_by).strip(),
                created_at,
                str(note or "").strip(),
                evidence_id,
            ),
        )
        template_id = int(cur.lastrowid)
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            str(created_by).strip(),
            "save_mapping_template",
            f"mapping_template:{template_id}",
            None,
            {
                "template_id": template_id,
                "scope": scope,
                "scope_key": scope_key,
                "template_name": str(template_name).strip(),
                "version": version,
                "col_map": cleaned_map,
                "evidence_id": evidence_id,
            },
            reason,
            commit=False,
            run_id=current.run_id if current else None,
            run_signature=current.signature if current else None,
        )
        if current is not None:
            final_contract = run_contract.ensure_run_contract(conn, project_id)
            if final_contract.run_id != current.run_id:
                # 模板和审计内容已进入新合同；运行身份字段只是绑定信息，
                # 在最终合同确定后重绑，避免下一次读取又制造一个伪变化。
                conn.execute(
                    """UPDATE evidence SET run_signature=?, run_id=?
                       WHERE id=? AND project_id=?""",
                    (final_contract.signature, final_contract.run_id, evidence_id, project_id),
                )
                conn.execute(
                    """UPDATE audit_log SET run_signature=?, run_id=?
                       WHERE id=? AND project_id=?""",
                    (final_contract.signature, final_contract.run_id, audit_id, project_id),
                )
    saved = conn.execute("SELECT * FROM mapping_templates WHERE id=?", (template_id,)).fetchone()
    return _row_to_template(saved)


def _header_for_recommendation(
    conn: sqlite3.Connection, project_id: int, sheet_id: int
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    ctx = _sheet_context(conn, project_id, sheet_id)
    lo = ctx["detected_header_lo"]
    hi = ctx["detected_header_hi"]
    if lo is None or hi is None:
        raise ValueError("该工作表没有可用于推荐的已识别表头")
    labels, signature = _header_snapshot(conn, sheet_id, (int(lo), int(hi)), int(ctx["n_cols"] or 0))
    return ctx, labels, signature


def _template_similarity(current_labels: list[dict[str, Any]], template_labels: list[dict[str, Any]]) -> Decimal:
    current_tokens = Counter(_normalise_label(item.get("value")) for item in current_labels)
    template_tokens = Counter(_normalise_label(item.get("value")) for item in template_labels)
    current_tokens += Counter()
    template_tokens += Counter()
    if not current_tokens or not template_tokens:
        return Decimal("0")
    intersection = sum((current_tokens & template_tokens).values())
    union = sum((current_tokens | template_tokens).values())
    return (Decimal(intersection) / Decimal(union)).quantize(Decimal("0.0001"))


def recommend_mapping_templates(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    *,
    limit: int = 10,
    minimum_score: Decimal | str = Decimal("0.35"),
) -> list[MappingTemplateRecommendation]:
    """按表头相似度返回候选，纯读取且明确要求人工再次确认。"""
    if int(limit) < 1:
        return []
    ctx, labels, signature = _header_for_recommendation(conn, project_id, sheet_id)
    threshold = Decimal(str(minimum_score))
    rows = conn.execute(
        """SELECT * FROM mapping_templates
           WHERE (scope='global' AND project_id IS NULL)
              OR (scope='project' AND project_id=?)
              OR (scope='sheet' AND project_id=? AND scope_key=?)
           ORDER BY id DESC""",
        (int(project_id), int(project_id), f"sheet:{int(sheet_id)}"),
    ).fetchall()
    recommendations: list[MappingTemplateRecommendation] = []
    current_map_row = conn.execute(
        "SELECT col_map_json FROM table_headers WHERE sheet_id=? ORDER BY id DESC LIMIT 1",
        (int(sheet_id),),
    ).fetchone()
    current_map = _loads(current_map_row["col_map_json"], {}) if current_map_row else {}
    for row in rows:
        template = _row_to_template(row)
        exact = template.header_signature == signature
        header_score = Decimal("1") if exact else _template_similarity(labels, template.header_labels)
        candidate_map = template.col_map
        common_fields = len(set(current_map).intersection(candidate_map)) if current_map else 0
        map_denominator = max(len(set(current_map)), len(set(candidate_map)), 1)
        mapping_score = Decimal(common_fields) / Decimal(map_denominator)
        scope_priority = {"sheet": 3, "project": 2, "global": 1}[template.scope]
        score = (header_score * Decimal("0.75") + mapping_score * Decimal("0.25")).quantize(Decimal("0.0001"))
        if score < threshold:
            continue
        if exact:
            reason = "表头签名完全一致；仍需人工核对列位、数据范围和适用范围"
        elif template.scope == "sheet":
            reason = "本 Sheet 历史表头高度相似；仅供人工核对，不自动套用"
        else:
            reason = "表头与映射字段相似；仅供人工核对，不自动套用"
        recommendations.append(MappingTemplateRecommendation(
            template=template,
            score=score,
            exact_header_match=exact,
            scope_priority=scope_priority,
            reason=reason,
        ))
    recommendations.sort(
        key=lambda item: (
            item.exact_header_match,
            item.score,
            item.scope_priority,
            item.template.created_at,
            item.template.template_id,
        ),
        reverse=True,
    )
    return recommendations[: int(limit)]
