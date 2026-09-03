"""对上控制基准候选：角色分层、确认生命周期与确定性比较（宪章第六节）。

纪律：
- 必须正式区分 reference / control_candidate / settlement_result 三种角色；
  只有 ``control_candidate`` 参与上限比较，reference 与 settlement_result
  仅作为信息陈列，不产生 PASS/FAIL 结论。
- 不使用“最新日期优先 / 最大金额优先 / 最新文件覆盖旧文件”之类的粗暴规则；
  基准替代必须显式声明 supersedes，且一个基准至多被一条新版本替代。
- 比较维度（币种、税口径、范围）任一不同或未声明 → INCOMPARABLE，绝不强行
  比较；两个有效基准金额冲突且无替代关系 → CONTROL_CONFLICT。
- 登记与复核沿用阶段 C-1 的生命周期：登记即 candidate，只有人工确认
  （confirmed）的基准参与比较；推翻 confirmed/rejected 必须填写理由。
- 超出控制基准只提示“结算结果较已确认控制基准高 X 元”，不认定违规、
  责任人或违规金额。
- 金额一律 Decimal；结算侧金额与口径由调用方显式提供，本模块不猜测、
  不从文件名或期次数值推断任何口径。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.engine.money import to_decimal
from jiadun.core.evidence import evidence as evidence_api

# ---- 角色（宪章第六节：必须正式区分三种角色）----

ROLE_CONTROL_CANDIDATE = "control_candidate"
ROLE_REFERENCE = "reference"
ROLE_SETTLEMENT_RESULT = "settlement_result"
BASELINE_ROLES = (ROLE_CONTROL_CANDIDATE, ROLE_REFERENCE, ROLE_SETTLEMENT_RESULT)

# ---- 确认生命周期（与阶段 C-1 合同事实同一套词表）----

REVIEW_CANDIDATE = "candidate"
REVIEW_CONFIRMED = "confirmed"
REVIEW_REJECTED = "rejected"
REVIEW_NEEDS_REVIEW = "needs_review"
_REVIEW_DECISIONS = (REVIEW_CONFIRMED, REVIEW_REJECTED, REVIEW_NEEDS_REVIEW, REVIEW_CANDIDATE)

# ---- 比较状态（宪章第六节）----

CONTROL_PASS = "pass"
CONTROL_FAIL = "fail"
CONTROL_PENDING = "pending"
CONTROL_INCOMPARABLE = "incomparable"
CONTROL_CONFLICT = "control_conflict"
CONTROL_NOT_AVAILABLE = "not_available"
# 汇总状态的优先级：基准自身无法确立 > 已有确定发现 > 不可比 > 未确认 >
# 通过。fail 高于 incomparable 是为了让“确定超出”不被“另一条基准不可比”
# 掩盖；全部逐条明细仍保留在 items 中，不做任何静默丢弃。
_STATUS_PRECEDENCE = (
    CONTROL_CONFLICT, CONTROL_FAIL, CONTROL_INCOMPARABLE, CONTROL_PENDING, CONTROL_PASS,
)

_UNKNOWN = "unknown"
# 允许随复核一并人工修正的字段；金额与替代关系一经登记不可改，
# 修正金额必须登记新基准并声明 supersedes，保证历史不可篡改。
_REVIEW_UPDATABLE_FIELDS = ("currency", "tax_basis", "scope_descriptor", "effective_period", "version")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_text(value: object, field_name: str, *, required: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise ValueError(f"{field_name} 不能为空：控制基准的关键属性必须显式声明，不得留白")
    return text


def _clean_optional_vocab(value: object) -> str:
    """币种/税口径允许留白，但留白一律归一为 unknown（fail-closed，可比较性检查会拦下）。"""
    text = str(value if value is not None else "").strip()
    return text or _UNKNOWN


def _require_positive_amount(value: object) -> Decimal:
    if isinstance(value, float):
        raise ValueError("金额不接受 float：请传入 Decimal 或字符串，避免二进制浮点误差")
    try:
        amount = to_decimal(value)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"控制基准金额无法解析为 Decimal：{value!r}") from exc
    if not amount.is_finite():
        raise ValueError("控制基准金额必须是有限数")
    if amount <= 0:
        raise ValueError("控制基准金额必须大于 0（不得以 0 或负数充当上限）")
    return amount


def _baseline_by_id(conn: sqlite3.Connection, project_id: int, baseline_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM control_baselines WHERE id=? AND project_id=?",
        (int(baseline_id), int(project_id)),
    ).fetchone()


def register_baseline(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    title: str,
    amount: Decimal | str,
    currency: str = _UNKNOWN,
    tax_basis: str = _UNKNOWN,
    scope_descriptor: str,
    effective_period: str = "",
    version: int = 1,
    role: str = ROLE_CONTROL_CANDIDATE,
    supersedes_id: int | None = None,
    priority: int = 0,
    file_id: int | None = None,
    detail: str = "",
    created_by: str = "user",
) -> int:
    """登记一条控制基准候选（初始一律 candidate，不自动 confirmed）。

    金额必须为正的有限 Decimal；范围必须显式声明——范围、税口径、币种
    未声明的基准无法通过可比较性检查，登记时可以暂缺，但比较时会被
    判为 INCOMPARABLE，绝不默认可比。
    """
    role = _clean_text(role, "角色")
    if role not in BASELINE_ROLES:
        raise ValueError(f"未知的控制基准角色：{role!r}（允许：{', '.join(BASELINE_ROLES)}）")
    title_text = _clean_text(title, "基准名称", required=True)
    scope_text = _clean_text(scope_descriptor, "范围说明", required=True)
    amount_value = _require_positive_amount(amount)
    try:
        version_number = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"版本必须是整数：{version!r}") from exc
    if version_number < 1:
        raise ValueError("版本必须 >= 1")
    supersedes_value: int | None = None
    if supersedes_id is not None:
        supersedes_value = int(supersedes_id)
        if supersedes_value == 0:
            raise ValueError("supersedes_id 必须指向已存在的基准，或留空表示无替代关系")
        target = _baseline_by_id(conn, project_id, supersedes_value)
        if target is None:
            raise ValueError(f"被替代的基准不存在或不属于当前项目：{supersedes_value}")
        if target["role"] != role:
            raise ValueError(
                f"替代关系必须同角色：{role!r} 不能替代 {target['role']!r}"
            )
        taken = conn.execute(
            "SELECT id FROM control_baselines WHERE supersedes_id=?", (supersedes_value,)
        ).fetchone()
        if taken is not None:
            raise ValueError(
                f"基准 {supersedes_value} 已被基准 {taken['id']} 替代；"
                "如需再修正，请替代最新版本，保持替代链单一"
            )
    if file_id is not None:
        source = conn.execute(
            "SELECT id FROM source_files WHERE id=? AND project_id=?",
            (int(file_id), int(project_id)),
        ).fetchone()
        if source is None:
            raise ValueError("基准来源文件不属于当前项目，拒绝登记")
    now = _now()
    with conn:
        cur = conn.execute(
            """INSERT INTO control_baselines(
                   project_id, role, title, file_id, amount, currency, tax_basis,
                   scope_descriptor, effective_period, version, supersedes_id,
                   priority, review_status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), role, title_text,
                int(file_id) if file_id is not None else None,
                str(amount_value), _clean_optional_vocab(currency),
                _clean_optional_vocab(tax_basis), scope_text,
                _clean_text(effective_period, "生效期间"), version_number,
                supersedes_value, int(priority), REVIEW_CANDIDATE, now,
            ),
        )
        baseline_id = int(cur.lastrowid)
        evidence_api.add_evidence(
            conn,
            project_id,
            "control_baseline_registered",
            f"登记控制基准候选：{title_text}（金额 {amount_value}，角色 {role}）",
            steps=[{
                "step": "人工登记控制基准候选",
                "baseline_id": baseline_id,
                "title": title_text,
                "role": role,
                "amount": str(amount_value),
                "currency": _clean_optional_vocab(currency),
                "tax_basis": _clean_optional_vocab(tax_basis),
                "scope_descriptor": scope_text,
                "supersedes_id": supersedes_value,
                "created_by": created_by,
                "detail": _clean_text(detail, "备注"),
            }],
            sources=[{
                "baseline_id": baseline_id,
                "title": title_text,
                "file_id": int(file_id) if file_id is not None else None,
            }],
            scope="human",
            commit=False,
        )
    run_contract.ensure_run_contract(conn, project_id)
    return baseline_id


def list_baselines(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    role: str | None = None,
    review_status: str | None = None,
) -> list[dict[str, Any]]:
    """按 id 顺序返回控制基准台账（含确认状态与来源文件名）。"""
    sql = """SELECT cb.*, sf.original_name AS source_file_name
             FROM control_baselines cb
             LEFT JOIN source_files sf ON sf.id=cb.file_id
             WHERE cb.project_id=?"""
    params: list[Any] = [int(project_id)]
    if role is not None:
        sql += " AND cb.role=?"
        params.append(role)
    if review_status is not None:
        sql += " AND cb.review_status=?"
        params.append(review_status)
    sql += " ORDER BY cb.id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def set_baseline_review(
    conn: sqlite3.Connection,
    project_id: int,
    baseline_id: int,
    decision: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """人工确认/拒绝/标记待复核一条控制基准；推翻既有结论必须留理由。

    币种、税口径、范围、生效期间与版本号允许在复核时一并人工修正——
    这正是“确认范围、税口径、变更和版本”的入口；金额与 supersedes
    关系不可修改，修正金额必须登记新基准并声明替代关系。所有字段变化
    与状态流转都写入 ``control_baseline_review`` 审计 Evidence。
    """
    if decision not in _REVIEW_DECISIONS:
        raise ValueError(f"未知的确认决定：{decision!r}")
    row = _baseline_by_id(conn, project_id, baseline_id)
    if row is None:
        raise ValueError(f"控制基准不存在或不属于当前项目：baseline_id={baseline_id}")
    updates = dict(updates or {})
    unknown_keys = set(updates) - set(_REVIEW_UPDATABLE_FIELDS)
    if unknown_keys:
        raise ValueError(
            f"不可通过复核修改的字段：{sorted(unknown_keys)}；"
            f"允许：{list(_REVIEW_UPDATABLE_FIELDS)}（金额与替代关系不可修改）"
        )
    before = {
        "currency": row["currency"] or _UNKNOWN,
        "tax_basis": row["tax_basis"] or _UNKNOWN,
        "scope_descriptor": row["scope_descriptor"] or "",
        "effective_period": row["effective_period"] or "",
        "version": int(row["version"]),
    }
    after = dict(before)
    for key, value in updates.items():
        if key == "version":
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"版本必须是整数：{value!r}") from exc
            if number < 1:
                raise ValueError("版本必须 >= 1")
            after[key] = number
        elif key in ("currency", "tax_basis"):
            after[key] = _clean_optional_vocab(value)
        else:
            after[key] = _clean_text(value, key)
    # 修正已有明确值（非空、非 unknown）必须给出理由；补 unknown → 具体值
    # 属于补全，不强制理由但同样留痕。
    changed = {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    status_before = row["review_status"] or REVIEW_CANDIDATE
    overturning = status_before in (REVIEW_CONFIRMED, REVIEW_REJECTED) and decision != status_before
    overwriting_known = any(
        key in ("currency", "tax_basis") and before[key] != _UNKNOWN
        or key == "scope_descriptor" and before[key] != ""
        for key in changed
    )
    if (overturning or overwriting_known) and not (reason or "").strip():
        raise ValueError(
            "推翻既有确认结论或改写已声明的币种/税口径/范围必须填写理由"
        )
    now = _now()
    with conn:
        if changed:
            conn.execute(
                """UPDATE control_baselines
                   SET currency=?, tax_basis=?, scope_descriptor=?, effective_period=?, version=?
                   WHERE id=?""",
                (
                    after["currency"], after["tax_basis"], after["scope_descriptor"],
                    after["effective_period"], after["version"], int(baseline_id),
                ),
            )
        conn.execute(
            """UPDATE control_baselines
               SET review_status=?, reviewed_at=?, reviewed_by=?, review_reason=?
               WHERE id=?""",
            (decision, now, reviewed_by, (reason or "").strip(), int(baseline_id)),
        )
        evidence_api.add_evidence(
            conn,
            project_id,
            "control_baseline_review",
            f"控制基准《{row['title']}》：{status_before} → {decision}"
            + (f"（{(reason or '').strip()}）" if (reason or "").strip() else ""),
            steps=[{
                "step": "人工复核控制基准",
                "baseline_id": int(baseline_id),
                "title": row["title"],
                "before": status_before,
                "after": decision,
                "field_changes": {key: {"before": old, "after": new} for key, (old, new) in changed.items()},
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{"baseline_id": int(baseline_id), "title": row["title"]}],
            scope="human",
            commit=False,
        )
    run_contract.ensure_run_contract(conn, project_id)
    return {
        "baseline_id": int(baseline_id),
        "before": status_before,
        "after": decision,
        "field_changes": {key: list(pair) for key, pair in changed.items()},
        "reviewed_at": now,
        "reviewed_by": reviewed_by,
    }


# ---- 确定性比较引擎 ----


@dataclass(frozen=True)
class SettlementSide:
    """比较的结算侧。金额与口径由调用方显式提供；本模块不推断任何口径。

    ``amount`` 允许为 None（结算侧尚未形成时），此时比较只能给出
    PENDING，不得与任何基准比较大小。
    """

    amount: Decimal | str | None
    currency: str = _UNKNOWN
    tax_basis: str = _UNKNOWN
    scope_descriptor: str = ""

    def normalized_amount(self) -> Decimal | None:
        if self.amount is None:
            return None
        if isinstance(self.amount, float):
            raise ValueError("结算侧金额不接受 float：请传入 Decimal 或字符串")
        return to_decimal(self.amount)


@dataclass
class ControlComparison:
    """一次控制基准比较的完整结果：总体状态 + 逐条明细，不丢弃任何基准。"""

    status: str
    message: str
    items: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    settlement: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "items": list(self.items),
            "excluded": list(self.excluded),
            "references": list(self.references),
            "settlement": dict(self.settlement),
        }


def _norm_vocab(value: object) -> str:
    text = str(value if value is not None else "").strip().casefold()
    return text or _UNKNOWN


def _parse_row_amount(row: dict[str, Any]) -> Decimal:
    try:
        amount = to_decimal(row.get("amount"))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(
            f"控制基准 #{row.get('id')} 的存储金额无法解析为 Decimal：{row.get('amount')!r}"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"控制基准 #{row.get('id')} 的存储金额非法：{row.get('amount')!r}")
    return amount


def _comparability_reasons(baseline: dict[str, Any], settlement: SettlementSide) -> list[str]:
    """逐维度判定可比较性；返回原因列表，空列表表示可比。

    币种/税口径/范围任一未声明（unknown/空）或不同 → INCOMPARABLE。
    范围采用逐字匹配：自由文本范围的语义相同与否无法确定性判定，
    文本不同就必须显式人工对齐，绝不默认同一范围。
    """
    reasons: list[str] = []
    base_currency = _norm_vocab(baseline.get("currency"))
    settle_currency = _norm_vocab(settlement.currency)
    if base_currency == _UNKNOWN or settle_currency == _UNKNOWN:
        whose = []
        if base_currency == _UNKNOWN:
            whose.append("基准侧")
        if settle_currency == _UNKNOWN:
            whose.append("结算侧")
        reasons.append(f"币种未声明（{'、'.join(whose)}），不可比较")
    elif base_currency != settle_currency:
        reasons.append(
            f"币种不同：基准 {baseline.get('currency')} vs 结算 {settlement.currency}"
        )
    base_tax = _norm_vocab(baseline.get("tax_basis"))
    settle_tax = _norm_vocab(settlement.tax_basis)
    if base_tax == _UNKNOWN or settle_tax == _UNKNOWN:
        whose = []
        if base_tax == _UNKNOWN:
            whose.append("基准侧")
        if settle_tax == _UNKNOWN:
            whose.append("结算侧")
        reasons.append(f"税口径未声明（{'、'.join(whose)}），不可比较")
    elif base_tax != settle_tax:
        reasons.append(
            f"税口径不同：基准 {baseline.get('tax_basis')} vs 结算 {settlement.tax_basis}"
        )
    base_scope = str(baseline.get("scope_descriptor") or "").strip()
    settle_scope = str(settlement.scope_descriptor or "").strip()
    if not base_scope or not settle_scope:
        whose = []
        if not base_scope:
            whose.append("基准侧")
        if not settle_scope:
            whose.append("结算侧")
        reasons.append(f"范围未声明（{'、'.join(whose)}），不可比较")
    elif base_scope != settle_scope:
        reasons.append(f"范围不同：基准「{base_scope}」 vs 结算「{settle_scope}」")
    return reasons


def evaluate_baselines(
    baseline_rows: list[dict[str, Any]],
    settlement: SettlementSide,
) -> ControlComparison:
    """确定性比较引擎：不查库、不读时钟，同输入必得同输出。

    规则（宪章第六节）：
    1. 只有 ``control_candidate`` 角色产生判定；reference /
       settlement_result 只陈列。
    2. 人工拒绝（rejected）的基准退出比较，进入 excluded 留痕。
    3. 被 confirmed 基准显式替代（supersedes）的基准退出比较；
       替代者尚是候选时，被替代基准仍参与，并标注存在待确认替代版本。
    4. 未确认（candidate / needs_review）→ PENDING。
    5. 币种/税口径/范围不同或未声明 → INCOMPARABLE。
    6. 可比的 confirmed 基准金额不一致且无替代关系 → CONTROL_CONFLICT。
    7. 结算侧金额缺失 → PENDING；超出 → FAIL（仅提示超出金额，不作违规
       认定）；未超出 → PASS。
    """
    rows = sorted(baseline_rows, key=lambda r: int(r.get("id") or 0))
    by_id = {int(r.get("id") or 0): r for r in rows}
    # 替代关系图：被替代基准 id → 替代者 id（唯一索引保证至多一条）。
    superseder_of: dict[int, int] = {}
    for row in rows:
        target = row.get("supersedes_id")
        if target is not None:
            superseder_of[int(target)] = int(row.get("id") or 0)

    def _superseded_by_confirmed(baseline_id: int) -> int | None:
        # 沿替代链找到第一个 confirmed 替代者；防御环状或跨项目脏数据。
        seen: set[int] = set()
        current = baseline_id
        while current in superseder_of and current not in seen:
            seen.add(current)
            successor = superseder_of[current]
            successor_row = by_id.get(successor)
            if successor_row is None:
                return None
            if str(successor_row.get("review_status") or REVIEW_CANDIDATE) == REVIEW_CONFIRMED:
                return successor
            current = successor
        return None

    items: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    comparable: list[tuple[dict[str, Any], Decimal]] = []

    for row in rows:
        baseline_id = int(row.get("id") or 0)
        status_value = str(row.get("review_status") or REVIEW_CANDIDATE)
        role = str(row.get("role") or ROLE_CONTROL_CANDIDATE)
        head = {
            "baseline_id": baseline_id,
            "title": row.get("title"),
            "role": role,
            "amount": row.get("amount"),
            "review_status": status_value,
            "evidence_id": row.get("evidence_id"),
        }
        if role != ROLE_CONTROL_CANDIDATE:
            references.append({**head, "note": "非控制候选角色，仅陈列，不参与上限比较"})
            continue
        if status_value == REVIEW_REJECTED:
            excluded.append({**head, "reason": "人工拒绝，退出控制比较"})
            continue
        replaced_by = _superseded_by_confirmed(baseline_id)
        if replaced_by is not None:
            excluded.append({
                **head,
                "reason": f"已被确认的基准 #{replaced_by} 替代（supersedes），退出控制比较",
            })
            continue
        item: dict[str, Any] = {**head, "status": CONTROL_PENDING, "message": "", "reasons": []}
        pending_note = ""
        if baseline_id in superseder_of:
            successor_status = by_id[superseder_of[baseline_id]].get("review_status") or REVIEW_CANDIDATE
            if successor_status != REVIEW_CONFIRMED:
                pending_note = (
                    f"存在未确认的替代版本 #{superseder_of[baseline_id]}（{successor_status}），"
                    "替代确认前本基准仍参与比较"
                )
        if status_value != REVIEW_CONFIRMED:
            item["reasons"].append(
                f"基准为 {status_value}，未经人工确认不得作为已确认控制基准"
            )
            if pending_note:
                item["reasons"].append(pending_note)
            items.append(item)
            continue
        reasons = _comparability_reasons(row, settlement)
        if reasons:
            item["status"] = CONTROL_INCOMPARABLE
            item["reasons"] = reasons + ([pending_note] if pending_note else [])
            item["message"] = "维度不可比或存在未确认替代，不强行比较"
            items.append(item)
            continue
        # 存在待确认替代版本只是提示，不构成不可比原因。
        item["reasons"] = [pending_note] if pending_note else []
        comparable.append((item, _parse_row_amount(row)))
        items.append(item)

    settlement_amount = settlement.normalized_amount()

    if comparable and settlement_amount is None:
        for item, _amount in comparable:
            item["status"] = CONTROL_PENDING
            item["reasons"].append("结算侧金额缺失，无法比较大小")
            item["message"] = "待结算侧金额确认后重比"
    elif comparable:
        amounts = {amount for _item, amount in comparable}
        if len(amounts) > 1:
            detail = "、".join(
                f"#{item['baseline_id']}={amount}" for item, amount in comparable
            )
            for item, _amount in comparable:
                item["status"] = CONTROL_CONFLICT
                item["reasons"].append(
                    f"已确认基准金额互相冲突（{detail}）且无替代关系，控制基准无法确立"
                )
                item["message"] = "控制基准冲突，需人工确立唯一有效基准或补充替代关系"
        else:
            baseline_amount = next(iter(amounts))
            for item, _amount in comparable:
                if settlement_amount > baseline_amount:
                    overage = settlement_amount - baseline_amount
                    item["status"] = CONTROL_FAIL
                    item["message"] = (
                        f"结算结果较已确认控制基准高 {overage} 元"
                        f"（基准 {baseline_amount}，结算 {settlement_amount}）——"
                        "仅提示超出，不构成违规或责任认定"
                    )
                else:
                    margin = baseline_amount - settlement_amount
                    item["status"] = CONTROL_PASS
                    item["message"] = (
                        f"结算结果未超过已确认控制基准（基准 {baseline_amount}，"
                        f"结算 {settlement_amount}，差额 {margin} 元）"
                    )

    candidate_ids = [
        int(r.get("id") or 0) for r in rows
        if str(r.get("role") or ROLE_CONTROL_CANDIDATE) == ROLE_CONTROL_CANDIDATE
    ]
    if not candidate_ids or not items:
        status = CONTROL_NOT_AVAILABLE
        message = "没有可参与比较的控制基准候选" if not candidate_ids else "控制基准候选全部被拒绝或已被确认版本替代"
    else:
        present = {item["status"] for item in items}
        status = next((code for code in _STATUS_PRECEDENCE if code in present), CONTROL_NOT_AVAILABLE)
        message = _overall_message(status, items, settlement_amount)
    return ControlComparison(
        status=status,
        message=message,
        items=items,
        excluded=excluded,
        references=references,
        settlement={
            "amount": str(settlement_amount) if settlement_amount is not None else None,
            "currency": str(settlement.currency or "").strip() or _UNKNOWN,
            "tax_basis": str(settlement.tax_basis or "").strip() or _UNKNOWN,
            "scope_descriptor": str(settlement.scope_descriptor or "").strip(),
        },
    )


def _overall_message(
    status: str, items: list[dict[str, Any]], settlement_amount: Decimal | None
) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = "、".join(f"{code}×{counts[code]}" for code in _STATUS_PRECEDENCE if code in counts)
    if status == CONTROL_CONFLICT:
        return f"已确认控制基准金额冲突（{summary}）：需人工确立唯一有效基准或补充替代关系"
    if status == CONTROL_FAIL:
        first_fail = next(item for item in items if item["status"] == CONTROL_FAIL)
        return f"{first_fail['message']}（逐条结果：{summary}）"
    if status == CONTROL_INCOMPARABLE:
        first = next(item for item in items if item["status"] == CONTROL_INCOMPARABLE)
        return f"存在不可比基准（{first['reasons'][0] if first['reasons'] else ''}；逐条结果：{summary}）"
    if status == CONTROL_PENDING:
        return (
            "控制基准尚未确认或结算侧金额缺失，不能形成控制比较结论"
            f"（逐条结果：{summary}）"
        )
    return (
        f"结算结果未超过已确认控制基准（逐条结果：{summary}）"
        if settlement_amount is not None
        else "结算侧金额缺失"
    )


def evaluate_project_control(
    conn: sqlite3.Connection,
    project_id: int,
    settlement: SettlementSide,
    *,
    record_evidence: bool = False,
) -> ControlComparison:
    """按当前台账执行一次确定性比较；可选写入审计 Evidence。

    结算侧金额与口径必须由调用方显式给出（口径选择属于业务决策，见
    ROADMAP 阶段 C 的接线项），本函数不做任何推断。
    """
    rows = list_baselines(conn, project_id)
    comparison = evaluate_baselines(rows, settlement)
    if record_evidence and rows:
        current = run_contract.get_current_contract(conn, project_id)
        evidence_api.add_evidence(
            conn,
            project_id,
            "control_baseline_evaluation",
            f"控制基准比较：{comparison.status}——{comparison.message}",
            steps=[{
                "step": "确定性控制基准比较",
                "status": comparison.status,
                "settlement": comparison.settlement,
                "items": comparison.items,
                "excluded": comparison.excluded,
                "references": comparison.references,
            }],
            sources=[
                {"baseline_id": item["baseline_id"], "title": item["title"]}
                for item in comparison.items + comparison.excluded + comparison.references
            ],
            run_signature=current.signature if current else None,
        )
    return comparison


__all__ = [
    "BASELINE_ROLES", "ControlComparison", "ROLE_CONTROL_CANDIDATE",
    "ROLE_REFERENCE", "ROLE_SETTLEMENT_RESULT", "REVIEW_CANDIDATE",
    "REVIEW_CONFIRMED", "REVIEW_REJECTED", "REVIEW_NEEDS_REVIEW",
    "SettlementSide", "CONTROL_CONFLICT", "CONTROL_FAIL",
    "CONTROL_INCOMPARABLE", "CONTROL_NOT_AVAILABLE", "CONTROL_PASS",
    "CONTROL_PENDING", "evaluate_baselines", "evaluate_project_control",
    "list_baselines", "register_baseline", "set_baseline_review",
]
