"""工程量核对（quantity ledger）：上下文登记、人工确认与确定性台账。

角色定义（宪章 §一/§六、AGENTS.md 数据纪律）：
- ``settlement_periods.period_no`` 继续作为内部序号（按方向自增）；业务身份
  （合同键 / 单位名称 / 业务期号 / 资料类型 contract|settlement）保存在
  ``quantity_period_context``，每次登记都创建独立期次行，不同合同、不同
  文件不得未经确认挤进同一业务期；
- 上下文一律以 ``pending`` 落库；只有人工确认（原因必填，写 Evidence/Audit，
  Evidence 记为 human 事实并绑定刷新后的 Run Contract）才参与有效核量，
  旧资料不自动升级；混合来源的旧期次不能一次性确认归入单一合同；
- 替代关系（supersedes）必须显式声明且同项目/合同/期号/方向/资料类型，
  阻断循环与跨范围替代；修订版确认后旧版只退出有效核量，不删除行或证据；
- 数量、换算、累计全部由程序用 Decimal 确定性完成：同量纲固定倍率复用
  ``engine.quantities``；人工换算 = 原数量 × 人工系数 × 目标单位固定倍率
  （target_unit=kg 时必须再乘 0.001 折 t）；跨量纲必须人工确认，不猜
  厚度/密度；缺失值显式 pending，绝不补 0；
- 比较/控制的配对键使用完整的编码/名称命名空间 + 特征 + 标准量纲 + 工作
  口径；空工作口径不构成可比口径；同身份仍存在待确认或未登记上下文的
  数量明细时，不得输出 PASS，必须返回被排除明细与原因；
- 台账结果只报告差额与状态，不自动认定违规或责任。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

from jiadun.core.contracts import run_contract
from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.engine.quantities import feature_key, unit_basis
from jiadun.core.evidence import audit as audit_api
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.parsing.extract_items import is_non_detail_flags

# 期次上下文状态：登记即 pending；确认后 confirmed；被确认的修订版明确
# 替代后 superseded（仅退出有效核量，行与证据保留）。
PERIOD_PENDING = "pending"
PERIOD_CONFIRMED = "confirmed"
PERIOD_SUPERSEDED = "superseded"

DOC_KIND_CONTRACT = "contract"
DOC_KIND_SETTLEMENT = "settlement"
DOC_KINDS = (DOC_KIND_CONTRACT, DOC_KIND_SETTLEMENT)

MODE_INCREMENTAL = "incremental"
MODE_CUMULATIVE = "cumulative"
AMOUNT_MODES = (MODE_INCREMENTAL, MODE_CUMULATIVE)

BUILDING_PENDING = "pending"
BUILDING_CONFIRMED = "confirmed"
BUILDING_STATUSES = (BUILDING_PENDING, BUILDING_CONFIRMED)

DIRECTIONS = ("upward", "downward")

# 比较五态（与控制基准一致）：只报告差额，不认定违规/责任。
COMPARE_PASS = "PASS"
COMPARE_FAIL = "FAIL"
COMPARE_PENDING = "PENDING"
COMPARE_INCOMPARABLE = "INCOMPARABLE"
COMPARE_CONTROL_CONFLICT = "CONTROL_CONFLICT"
# 严重度排序：用于同一控制组保留两种基准时取整体状态。
_SEVERITY = {COMPARE_PASS: 0, COMPARE_PENDING: 1,
             COMPARE_INCOMPARABLE: 2, COMPARE_CONTROL_CONFLICT: 3,
             COMPARE_FAIL: 4}

ALL_BUILDINGS_KEY = "__all__"

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 楼栋 token 三类：并列（1、2号楼）、中文/数字区间（1-3号楼 / 一至三号楼）、
# 单栋（1号楼 / 1#楼 / 一号楼）。中文序数只支持 1..99；含百/千/零等不支持
# 位数的段必须整体拒绝（如"一百零一号楼"不得后缀误识为 1号楼）。
_PARALLEL_TOKEN_RE = re.compile(
    r"(\d+|[一二三四五六七八九十百千零]+)"
    r"(?:\s*[、，,和与]\s*(?:\d+|[一二三四五六七八九十百千零]+))+"
    r"\s*#?\s*号?\s*楼")
_RANGE_TOKEN_RE = re.compile(
    r"(\d+|[一二三四五六七八九十百千零]+)"
    r"\s*[-—~至]\s*(\d+|[一二三四五六七八九十百千零]+)"
    r"\s*#?\s*号?\s*楼")
_BUILDING_TOKEN_RE = re.compile(
    r"(\d+|[一二三四五六七八九十百千零]+)\s*#?\s*号?\s*楼")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _require_reason(reason: str | None, action: str) -> str:
    text = str(reason or "").strip()
    if not text:
        raise ValueError(f"人工操作「{action}」必须记录原因（原则 14）")
    return text


def _try_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return to_decimal(value)
    except (NotANumberError, TypeError, ValueError, InvalidOperation):
        return None


# ---------------------------------------------------------------- 上下文读取

def list_period_contexts(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """项目全部期次上下文（含方向/内部序号/来源文件）。"""
    rows = conn.execute(
        """SELECT qpc.id AS context_id, qpc.period_id, qpc.contract_key,
                  qpc.business_period_no, qpc.unit_name, qpc.doc_kind,
                  qpc.amount_mode, qpc.work_scope, qpc.building_scope,
                  qpc.building_status, qpc.status, qpc.supersedes_period_id,
                  qpc.created_at, qpc.confirmed_at, qpc.confirmed_by,
                  qpc.confirmed_reason,
                  sp.direction, sp.period_no AS internal_period_no,
                  sp.title, sp.source_file_id
           FROM quantity_period_context qpc
           JOIN settlement_periods sp ON sp.id=qpc.period_id
           WHERE qpc.project_id=? ORDER BY qpc.id""",
        (int(project_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def get_period_context(conn: sqlite3.Connection, project_id: int,
                       context_id: int) -> dict | None:
    for context in list_period_contexts(conn, project_id):
        if context["context_id"] == int(context_id):
            return context
    return None


def list_line_contexts(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, line_item_id, standard_key, building, work_scope,
                  convert_factor, target_unit, convert_basis, status,
                  created_at, confirmed_at, confirmed_by, confirmed_reason
           FROM quantity_line_context WHERE project_id=? ORDER BY id""",
        (int(project_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def _identity_of(context: dict) -> tuple:
    """业务身份：同项目内（方向，合同键，单位，资料类型，业务期号）。"""
    return (
        context["direction"],
        str(context["contract_key"] or ""),
        str(context["unit_name"] or ""),
        str(context["doc_kind"] or ""),
        int(context["business_period_no"]),
    )


def _identity_label(context: dict) -> str:
    kind_zh = "合同" if context["doc_kind"] == DOC_KIND_CONTRACT else "结算"
    direction_zh = {"upward": "对上", "downward": "对下"}.get(
        context["direction"], context["direction"])
    return (
        f"{direction_zh}·{kind_zh}·合同「{context['contract_key']}」"
        f"·单位「{context['unit_name'] or '未登记'}」·第{context['business_period_no']}期"
    )


# ---------------------------------------------------------------- 替代关系校验

def _context_by_period(conn: sqlite3.Connection, project_id: int,
                       period_id: int) -> dict | None:
    row = conn.execute(
        """SELECT qpc.id AS context_id, qpc.period_id, qpc.contract_key,
                  qpc.business_period_no, qpc.unit_name, qpc.doc_kind,
                  qpc.work_scope, qpc.supersedes_period_id, qpc.status,
                  sp.direction
           FROM quantity_period_context qpc
           JOIN settlement_periods sp ON sp.id=qpc.period_id
           WHERE qpc.period_id=? AND qpc.project_id=?""",
        (int(period_id), int(project_id)),
    ).fetchone()
    return dict(row) if row else None


def _validate_supersedes(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    supersedes_period_id: int,
    direction: str,
    contract_key: str,
    business_period_no: int,
    doc_kind: str,
    unit_name: str,
    work_scope: str,
    self_period_id: int | None = None,
) -> dict:
    """校验显式替代关系：同项目/合同/期号/方向/资料类型/范围，阻断循环。"""
    target = _context_by_period(conn, project_id, supersedes_period_id)
    if target is None:
        raise ValueError(
            f"替代目标期次 {supersedes_period_id} 不存在、不属于当前项目，"
            "或尚未登记数量上下文；请先为被替代版本登记上下文")
    mismatches = []
    if target["direction"] != direction:
        mismatches.append(f"方向 {target['direction']}≠{direction}")
    if str(target["contract_key"] or "") != contract_key:
        mismatches.append("合同键不同")
    if int(target["business_period_no"]) != int(business_period_no):
        mismatches.append(
            f"业务期号 {target['business_period_no']}≠{business_period_no}")
    if str(target["doc_kind"] or "") != doc_kind:
        mismatches.append("资料类型不同")
    if str(target["unit_name"] or "") != unit_name:
        mismatches.append("单位名称不同")
    if str(target["work_scope"] or "") != work_scope:
        mismatches.append("工作口径不同")
    if mismatches:
        raise ValueError(
            "跨范围替代被阻断（" + "；".join(mismatches)
            + "）：替代关系只允许同一项目、同一合同、同一业务期号、同一方向、"
            "同一资料类型、同一单位与工作口径内的修订")
    # 循环检测：沿目标的 supersedes 链回溯，任何回到自身的路径都拒绝。
    visited = {int(supersedes_period_id)}
    cursor_period = target["supersedes_period_id"]
    while cursor_period is not None:
        cursor_period = int(cursor_period)
        if self_period_id is not None and cursor_period == int(self_period_id):
            raise ValueError("替代关系形成循环，已阻断")
        if cursor_period in visited:
            raise ValueError("替代关系形成循环，已阻断")
        visited.add(cursor_period)
        nxt = _context_by_period(conn, project_id, cursor_period)
        cursor_period = nxt["supersedes_period_id"] if nxt else None
    return target


def _validate_context_fields(
    *,
    direction: str,
    contract_key: str,
    business_period_no,
    doc_kind: str,
    amount_mode: str,
    building_status: str,
) -> tuple[str, int]:
    if direction not in DIRECTIONS:
        raise ValueError(f"方向必须是 upward/downward，收到 {direction!r}")
    key = str(contract_key or "").strip()
    if not key:
        raise ValueError("合同键必填：同单位多合同必须靠合同键隔离")
    try:
        business = int(business_period_no)
    except (TypeError, ValueError) as exc:
        raise ValueError("业务期号必须是正整数") from exc
    if business < 1:
        raise ValueError("业务期号必须是正整数")
    if doc_kind not in DOC_KINDS:
        raise ValueError(f"资料类型必须是 contract/settlement，收到 {doc_kind!r}")
    if amount_mode not in AMOUNT_MODES:
        raise ValueError(
            f"计量模式必须是 incremental/cumulative，收到 {amount_mode!r}")
    if building_status not in BUILDING_STATUSES:
        raise ValueError(f"楼栋确认状态必须是 pending/confirmed，收到 {building_status!r}")
    return key, business


def _validate_source_file(conn: sqlite3.Connection, project_id: int,
                          source_file_id: int | None) -> None:
    if source_file_id is None:
        return
    row = conn.execute(
        "SELECT id FROM source_files WHERE id=? AND project_id=?",
        (int(source_file_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"source_file_id={source_file_id} 不属于当前项目，跨项目来源被阻断")


# ---------------------------------------------------------------- 人工操作收尾

def _finalize_human_operation(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    evidence_ids: list[int],
    audit_id: int | None,
) -> None:
    """把人工 Evidence 记为 human 事实并绑定刷新后的运行契约。

    仅在项目已有物化运行合同时刷新：上下文变化通过载荷改变签名，旧契约
    退出 current；随后把本操作的 Evidence/审计绑定到新契约。绑定字段不
    进入指纹（见 run_contract._human_confirmation_snapshot），不会自引用。
    """
    if not run_contract.has_materialized_contract(conn, project_id):
        return
    active = run_contract.ensure_run_contract(conn, project_id)
    for evidence_id in evidence_ids:
        conn.execute(
            "UPDATE evidence SET scope='human', run_signature=?, run_id=? WHERE id=?",
            (active.signature, active.run_id, int(evidence_id)),
        )
    if audit_id is not None:
        conn.execute(
            "UPDATE audit_log SET run_signature=?, run_id=? WHERE id=?",
            (active.signature, active.run_id, int(audit_id)),
        )


# ---------------------------------------------------------------- 期次上下文写入

def create_period_context(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    direction: str,
    contract_key: str,
    business_period_no: int,
    unit_name: str = "",
    doc_kind: str = DOC_KIND_SETTLEMENT,
    amount_mode: str = MODE_INCREMENTAL,
    work_scope: str = "",
    building_scope: str = "",
    building_status: str = BUILDING_PENDING,
    title: str | None = None,
    source_file_id: int | None = None,
    supersedes_period_id: int | None = None,
    actor: str = "user",
    reason: str,
    commit: bool = True,
) -> dict:
    """按显式业务身份创建独立期次（内部序号自增）并登记 pending 上下文。

    每次调用都创建新的 ``settlement_periods`` 行：不同合同/文件各自拿到
    独立期次，绝不复用或挤进已有业务期。旧 ``ensure_period`` 入口不受影响。
    """
    reason = _require_reason(reason, "登记数量期次上下文")
    contract_key, business_period_no = _validate_context_fields(
        direction=direction, contract_key=contract_key,
        business_period_no=business_period_no, doc_kind=doc_kind,
        amount_mode=amount_mode, building_status=building_status)
    _validate_source_file(conn, project_id, source_file_id)
    if supersedes_period_id is not None:
        _validate_supersedes(
            conn, project_id, supersedes_period_id=supersedes_period_id,
            direction=direction, contract_key=contract_key,
            business_period_no=business_period_no, doc_kind=doc_kind,
            unit_name=unit_name, work_scope=work_scope)
    from jiadun.core.engine.settlement_io import next_period_no

    internal_no = next_period_no(conn, int(project_id), direction)
    period_title = title or f"{contract_key}第{business_period_no}期"
    now = _now()

    def _write() -> dict:
        cur = conn.execute(
            """INSERT INTO settlement_periods(
                   project_id, period_no, title, source_file_id, direction, contract_party)
               VALUES (?,?,?,?,?,?)""",
            (int(project_id), internal_no, period_title, source_file_id,
             direction, unit_name),
        )
        period_id = int(cur.lastrowid)
        cur = conn.execute(
            """INSERT INTO quantity_period_context(
                   project_id, period_id, contract_key, business_period_no, unit_name,
                   doc_kind, amount_mode, work_scope, building_scope, building_status,
                   status, supersedes_period_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (int(project_id), period_id, contract_key, business_period_no,
             unit_name, doc_kind, amount_mode, work_scope, building_scope,
             building_status, supersedes_period_id, now),
        )
        context_id = int(cur.lastrowid)
        label = _identity_label({
            "direction": direction, "contract_key": contract_key,
            "business_period_no": business_period_no, "unit_name": unit_name,
            "doc_kind": doc_kind})
        evidence_id = evidence_api.add_evidence(
            conn, int(project_id), "quantity_period_context",
            f"登记数量期次上下文：{label}"
            + (f"（明确替代期次 {supersedes_period_id}）" if supersedes_period_id else ""),
            steps=[{
                "step": "登记数量期次上下文",
                "context_id": context_id,
                "period_id": period_id,
                "internal_period_no": internal_no,
                "direction": direction,
                "contract_key": contract_key,
                "business_period_no": business_period_no,
                "unit_name": unit_name,
                "doc_kind": doc_kind,
                "amount_mode": amount_mode,
                "work_scope": work_scope,
                "building_scope": building_scope,
                "supersedes_period_id": supersedes_period_id,
                "status": PERIOD_PENDING,
                "reason": reason,
            }],
            sources=[{"period_id": period_id, "source_file_id": source_file_id}],
            commit=False,
        )
        conn.execute(
            "UPDATE quantity_period_context SET evidence_id=? WHERE id=?",
            (evidence_id, context_id),
        )
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_period_context_create",
            f"quantity_period_context:{context_id}",
            None,
            {"period_id": period_id, "contract_key": contract_key,
             "business_period_no": business_period_no, "doc_kind": doc_kind,
             "status": PERIOD_PENDING, "supersedes_period_id": supersedes_period_id},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=[int(evidence_id)], audit_id=audit_id)
        return {"context_id": context_id, "period_id": period_id}

    if commit:
        with run_contract._transaction(conn, "quantity_period_context_create"):
            return _write()
    return _write()


def attach_period_context(
    conn: sqlite3.Connection,
    project_id: int,
    period_id: int,
    *,
    direction: str | None = None,
    contract_key: str,
    business_period_no: int,
    unit_name: str = "",
    doc_kind: str = DOC_KIND_SETTLEMENT,
    amount_mode: str = MODE_INCREMENTAL,
    work_scope: str = "",
    building_scope: str = "",
    building_status: str = BUILDING_PENDING,
    supersedes_period_id: int | None = None,
    actor: str = "user",
    reason: str,
    commit: bool = True,
) -> int:
    """为既有期次行补登上下文（旧资料迁移路径）：一律 pending，不自动确认。"""
    reason = _require_reason(reason, "补登数量期次上下文")
    row = conn.execute(
        """SELECT id, direction FROM settlement_periods
           WHERE id=? AND project_id=?""",
        (int(period_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"期次 {period_id} 不存在或不属于当前项目")
    existing = conn.execute(
        "SELECT id FROM quantity_period_context WHERE period_id=?",
        (int(period_id),),
    ).fetchone()
    if existing:
        raise ValueError(f"期次 {period_id} 已登记数量上下文（#{existing['id']}），不得重复登记")
    period_direction = direction or row["direction"]
    contract_key, business_period_no = _validate_context_fields(
        direction=period_direction, contract_key=contract_key,
        business_period_no=business_period_no, doc_kind=doc_kind,
        amount_mode=amount_mode, building_status=building_status)
    if supersedes_period_id is not None:
        _validate_supersedes(
            conn, project_id, supersedes_period_id=supersedes_period_id,
            direction=period_direction, contract_key=contract_key,
            business_period_no=business_period_no, doc_kind=doc_kind,
            unit_name=unit_name, work_scope=work_scope)
    now = _now()

    def _write() -> int:
        cur = conn.execute(
            """INSERT INTO quantity_period_context(
                   project_id, period_id, contract_key, business_period_no, unit_name,
                   doc_kind, amount_mode, work_scope, building_scope, building_status,
                   status, supersedes_period_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (int(project_id), int(period_id), contract_key, business_period_no,
             unit_name, doc_kind, amount_mode, work_scope, building_scope,
             building_status, supersedes_period_id, now),
        )
        context_id = int(cur.lastrowid)
        evidence_id = evidence_api.add_evidence(
            conn, int(project_id), "quantity_period_context",
            f"为既有期次 {period_id} 补登数量上下文（pending，不自动确认）",
            steps=[{
                "step": "补登数量期次上下文",
                "context_id": context_id,
                "period_id": int(period_id),
                "contract_key": contract_key,
                "business_period_no": business_period_no,
                "doc_kind": doc_kind,
                "status": PERIOD_PENDING,
                "reason": reason,
            }],
            sources=[{"period_id": int(period_id)}],
            commit=False,
        )
        conn.execute(
            "UPDATE quantity_period_context SET evidence_id=? WHERE id=?",
            (evidence_id, context_id),
        )
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_period_context_attach",
            f"quantity_period_context:{context_id}",
            None,
            {"period_id": int(period_id), "status": PERIOD_PENDING,
             "contract_key": contract_key},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=[int(evidence_id)], audit_id=audit_id)
        return context_id

    if commit:
        with run_contract._transaction(conn, "quantity_period_context_attach"):
            return _write()
    return _write()


def _period_source_file_ids(conn: sqlite3.Connection, period_id: int) -> set[int]:
    rows = conn.execute(
        """SELECT DISTINCT sf.id AS file_id
           FROM line_items li
           LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE li.period_id=?""",
        (int(period_id),),
    ).fetchall()
    return {int(r["file_id"]) for r in rows if r["file_id"] is not None}


def confirm_period_context(
    conn: sqlite3.Connection,
    project_id: int,
    context_id: int,
    *,
    actor: str = "user",
    reason: str,
) -> dict:
    """人工确认期次上下文；修订版确认后旧版只退出有效核量。"""
    reason = _require_reason(reason, "确认数量期次上下文")
    context = get_period_context(conn, project_id, context_id)
    if context is None:
        raise ValueError(f"数量上下文不存在或不属于当前项目：id={context_id}")
    if context["status"] == PERIOD_SUPERSEDED:
        raise ValueError("该上下文已被确认的修订版明确替代，不能再确认")
    # 混合来源的旧期次不能一次确认全部归入单一合同：明细来自多个文件时
    # 必须按文件拆分期次分别登记确认。
    source_file_ids = _period_source_file_ids(conn, context["period_id"])
    if len(source_file_ids) > 1:
        raise ValueError(
            f"期次 {context['period_id']} 的明细来自 {len(source_file_ids)} 个不同"
            "来源文件（混合来源），不能一次性确认归入单一合同；请按文件拆分"
            "期次后分别登记与确认")
    # 同身份已确认版本且无替代关系：不得挤进同一期（须声明明确替代）。
    for other in list_period_contexts(conn, project_id):
        if other["context_id"] == context["context_id"]:
            continue
        if other["status"] != PERIOD_CONFIRMED:
            continue
        if _identity_of(other) != _identity_of(context):
            continue
        related = (
            other["supersedes_period_id"] == context["period_id"]
            or context["supersedes_period_id"] == other["period_id"]
        )
        if not related:
            raise ValueError(
                f"同一业务身份（{_identity_label(context)}）已存在已确认版本："
                "不同文件不得未经确认挤进同一期；如为修订版，请登记时显式声明"
                " supersedes_period_id 替代关系")
    now = _now()
    superseded_ids: list[dict] = []

    def _write() -> dict:
        conn.execute(
            """UPDATE quantity_period_context
               SET status=?, confirmed_at=?, confirmed_by=?, confirmed_reason=?
               WHERE id=?""",
            (PERIOD_CONFIRMED, now, actor, reason, int(context_id)),
        )
        evidence_ids = [evidence_api.add_evidence(
            conn, int(project_id), "quantity_period_context_confirm",
            f"确认数量期次上下文 #{context_id}（{_identity_label(context)}）",
            steps=[{
                "step": "人工确认数量期次上下文",
                "context_id": int(context_id),
                "period_id": context["period_id"],
                "reason": reason,
            }],
            sources=[{"period_id": context["period_id"]}],
            commit=False,
        )]
        if context["supersedes_period_id"] is not None:
            target = _context_by_period(
                conn, project_id, context["supersedes_period_id"])
            if target is not None and target["status"] == PERIOD_CONFIRMED:
                conn.execute(
                    "UPDATE quantity_period_context SET status=?, updated_at=? WHERE id=?",
                    (PERIOD_SUPERSEDED, now, target["context_id"]),
                )
                superseded_ids.append(target)
                evidence_ids.append(evidence_api.add_evidence(
                    conn, int(project_id), "quantity_period_supersede",
                    f"期次 {target['period_id']} 被确认的修订版（期次 "
                    f"{context['period_id']}）明确替代：旧版退出有效核量，"
                    "行、上下文与证据保留不删除",
                    steps=[{
                        "step": "修订版确认后旧版退出有效核量",
                        "superseded_context_id": target["context_id"],
                        "superseded_period_id": target["period_id"],
                        "revision_context_id": int(context_id),
                        "revision_period_id": context["period_id"],
                        "reason": reason,
                    }],
                    sources=[{"period_id": target["period_id"]}],
                    commit=False,
                ))
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_period_context_confirm",
            f"quantity_period_context:{context_id}",
            {"status": context["status"]},
            {"status": PERIOD_CONFIRMED,
             "superseded": [t["context_id"] for t in superseded_ids]},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=evidence_ids, audit_id=audit_id)
        return {
            "context_id": int(context_id),
            "status": PERIOD_CONFIRMED,
            "superseded_context_ids": [t["context_id"] for t in superseded_ids],
        }

    with run_contract._transaction(conn, "quantity_period_context_confirm"):
        return _write()


def set_period_context_supersedes(
    conn: sqlite3.Connection,
    project_id: int,
    context_id: int,
    supersedes_period_id: int | None,
    *,
    actor: str = "user",
    reason: str,
) -> None:
    """人工修正替代关系（仍需通过范围/循环校验，写审计）。"""
    reason = _require_reason(reason, "修改数量上下文替代关系")
    context = get_period_context(conn, project_id, context_id)
    if context is None:
        raise ValueError(f"数量上下文不存在或不属于当前项目：id={context_id}")
    if supersedes_period_id is not None:
        _validate_supersedes(
            conn, project_id, supersedes_period_id=supersedes_period_id,
            direction=context["direction"], contract_key=context["contract_key"],
            business_period_no=context["business_period_no"],
            doc_kind=context["doc_kind"], unit_name=context["unit_name"],
            work_scope=context["work_scope"],
            self_period_id=context["period_id"])
    with run_contract._transaction(conn, "quantity_period_supersedes_update"):
        conn.execute(
            "UPDATE quantity_period_context SET supersedes_period_id=?, updated_at=? WHERE id=?",
            (supersedes_period_id, _now(), int(context_id)),
        )
        evidence_id = evidence_api.add_evidence(
            conn, int(project_id), "quantity_period_supersede",
            f"数量上下文 #{context_id} 替代关系改为 "
            f"{supersedes_period_id if supersedes_period_id else '（无）'}",
            steps=[{
                "step": "人工修改替代关系",
                "context_id": int(context_id),
                "supersedes_period_id": supersedes_period_id,
                "reason": reason,
            }],
            sources=[{"period_id": context["period_id"]}],
            commit=False,
        )
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_period_supersedes_update",
            f"quantity_period_context:{context_id}",
            {"supersedes_period_id": context["supersedes_period_id"]},
            {"supersedes_period_id": supersedes_period_id},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=[int(evidence_id)], audit_id=audit_id)


# ---------------------------------------------------------------- 明细上下文

def _line_project(conn: sqlite3.Connection, project_id: int,
                  line_item_id: int) -> int:
    row = conn.execute(
        """SELECT sp.project_id AS pid FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           WHERE li.id=?""",
        (int(line_item_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"明细行 {line_item_id} 不存在")
    if int(row["pid"]) != int(project_id):
        raise ValueError(
            f"明细行 {line_item_id} 不属于当前项目（跨项目成员被阻断）")
    return int(row["pid"])


def set_line_context(
    conn: sqlite3.Connection,
    project_id: int,
    line_item_id: int,
    *,
    standard_key: str | None = None,
    building: str | None = None,
    work_scope: str | None = None,
    convert_factor: str | None = None,
    target_unit: str | None = None,
    convert_basis: str | None = None,
    actor: str = "user",
    reason: str,
    commit: bool = True,
) -> int:
    """登记/更新明细行显式上下文；参数变化后必须重新人工确认才可计算。"""
    reason = _require_reason(reason, "登记数量明细上下文")
    _line_project(conn, project_id, line_item_id)
    has_convert = convert_factor is not None or target_unit is not None
    if has_convert:
        if not (convert_factor or "").strip() or not (target_unit or "").strip() \
                or not (convert_basis or "").strip():
            raise ValueError(
                "跨量纲换算必须同时提供换算系数、目标单位和文字依据；"
                "不得只给部分参数")
        factor_value = _try_decimal(str(convert_factor).strip())
        if factor_value is None or factor_value <= 0:
            raise ValueError(f"换算系数不是正数：{convert_factor!r}")
    now = _now()

    def _write() -> int:
        existing = conn.execute(
            "SELECT id FROM quantity_line_context WHERE line_item_id=?",
            (int(line_item_id),),
        ).fetchone()
        if existing:
            line_ctx_id = int(existing["id"])
            before = conn.execute(
                "SELECT * FROM quantity_line_context WHERE id=?",
                (line_ctx_id,),
            ).fetchone()
            conn.execute(
                """UPDATE quantity_line_context
                   SET standard_key=?, building=?, work_scope=?, convert_factor=?,
                       target_unit=?, convert_basis=?, status='pending',
                       updated_at=?, confirmed_at=NULL, confirmed_by=NULL,
                       confirmed_reason=''
                   WHERE id=?""",
                (standard_key, building, work_scope,
                 str(convert_factor).strip() if convert_factor else None,
                 str(target_unit).strip() if target_unit else None,
                 convert_basis, now, line_ctx_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO quantity_line_context(
                       project_id, line_item_id, standard_key, building, work_scope,
                       convert_factor, target_unit, convert_basis, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?, 'pending', ?)""",
                (int(project_id), int(line_item_id), standard_key, building,
                 work_scope,
                 str(convert_factor).strip() if convert_factor else None,
                 str(target_unit).strip() if target_unit else None,
                 convert_basis, now),
            )
            line_ctx_id = int(cur.lastrowid)
            before = None
        evidence_id = evidence_api.add_evidence(
            conn, int(project_id), "quantity_line_context",
            f"登记明细行 {line_item_id} 数量上下文（pending，确认前参数不参与计算）",
            steps=[{
                "step": "登记数量明细上下文",
                "line_context_id": line_ctx_id,
                "line_item_id": int(line_item_id),
                "standard_key": standard_key,
                "building": building,
                "work_scope": work_scope,
                "convert_factor": str(convert_factor).strip() if convert_factor else None,
                "target_unit": target_unit,
                "convert_basis": convert_basis,
                "status": "pending",
                "reason": reason,
            }],
            sources=[{"line_item_id": int(line_item_id)}],
            commit=False,
        )
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_line_context_set",
            f"quantity_line_context:{line_ctx_id}",
            dict(before) if before else None,
            {"standard_key": standard_key, "building": building,
             "work_scope": work_scope, "status": "pending"},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=[int(evidence_id)], audit_id=audit_id)
        return line_ctx_id

    if commit:
        with run_contract._transaction(conn, "quantity_line_context_set"):
            return _write()
    return _write()


def confirm_line_context(
    conn: sqlite3.Connection,
    project_id: int,
    line_item_id: int,
    *,
    actor: str = "user",
    reason: str,
) -> dict:
    """确认明细行上下文；换算参数自此才可作为计算依据。"""
    reason = _require_reason(reason, "确认数量明细上下文")
    _line_project(conn, project_id, line_item_id)
    row = conn.execute(
        "SELECT id, status, convert_factor, target_unit, convert_basis"
        " FROM quantity_line_context WHERE line_item_id=? AND project_id=?",
        (int(line_item_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"明细行 {line_item_id} 尚未登记数量上下文")
    if (row["convert_factor"] or row["target_unit"]) and not (
            row["convert_factor"] and row["target_unit"] and row["convert_basis"]):
        raise ValueError("换算参数不完整（系数/目标单位/依据），不能确认")
    now = _now()
    with run_contract._transaction(conn, "quantity_line_context_confirm"):
        conn.execute(
            """UPDATE quantity_line_context
               SET status='confirmed', confirmed_at=?, confirmed_by=?, confirmed_reason=?
               WHERE id=?""",
            (now, actor, reason, int(row["id"])),
        )
        evidence_id = evidence_api.add_evidence(
            conn, int(project_id), "quantity_line_context_confirm",
            f"确认明细行 {line_item_id} 数量上下文",
            steps=[{
                "step": "人工确认数量明细上下文",
                "line_context_id": int(row["id"]),
                "line_item_id": int(line_item_id),
                "reason": reason,
            }],
            sources=[{"line_item_id": int(line_item_id)}],
            commit=False,
        )
        audit_id = audit_api.record_audit(
            conn, int(project_id), actor, "quantity_line_context_confirm",
            f"quantity_line_context:{row['id']}",
            {"status": row["status"]}, {"status": "confirmed"},
            reason, commit=False,
        )
        _finalize_human_operation(
            conn, int(project_id), evidence_ids=[int(evidence_id)], audit_id=audit_id)
    return {"line_context_id": int(row["id"]), "status": "confirmed"}


# ---------------------------------------------------------------- 楼栋候选

def _cn_token_to_int(token: str) -> int | None:
    """有限中文序数解析：1..99；含百/千/零或其他未知字符整体拒绝。

    "十一"→11、"二十"→20、"二十一"→21、"十"→10；不支持"一百零一"
    等百位写法（返回 None，调用方跳过该 token，绝不后缀误识）。
    """
    if token.isdigit():
        return int(token)
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", token):
        tens, ones = token.split("十")
        return _CN_DIGITS.get(tens, 1) * 10 + _CN_DIGITS.get(ones, 0)
    return _CN_DIGITS.get(token)


def _parallel_building_tokens(text: str) -> list[str]:
    """并列楼栋段（1、2号楼 / 一和三号楼）：返回组合 token，任一段无法
    解析则整段拒绝（完整拒绝，不出部分结果）。"""
    out: list[str] = []
    for match in _PARALLEL_TOKEN_RE.finditer(text):
        segments = re.split(r"\s*[、，,和与]\s*", match.group(0))
        segments = [
            re.sub(r"\s*#?\s*号?\s*楼\s*$", "", seg) for seg in segments]
        numbers = [_cn_token_to_int(seg) for seg in segments]
        if all(n is not None and n > 0 for n in numbers):
            out.append("、".join(str(n) for n in numbers) + "号楼")
    return out


def _building_tokens(text: str) -> list[str]:
    """从文本提取规范化楼栋标记：1号楼 / 一号楼 / 1#楼 / 1-3号楼 / 1、2号楼。

    中文序数支持 1..99（十一=11、二十=20）；中文区间（一至三号楼）与并列
    （1、2号楼）一律产出组合 token，不拆单栋；不支持位数的中文段（如
    "一百零一"）整体拒绝，不产出任何 token。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    tokens: list[str] = []
    stripped = normalized
    parallel_spans = [m.span() for m in _PARALLEL_TOKEN_RE.finditer(stripped)]
    for start, end in parallel_spans:
        tokens.extend(_parallel_building_tokens(normalized[start:end]))
    for start, end in reversed(parallel_spans):
        stripped = stripped[:start] + " " + stripped[end:]
    for match in _RANGE_TOKEN_RE.finditer(stripped):
        lo = _cn_token_to_int(match.group(1))
        hi = _cn_token_to_int(match.group(2))
        if lo is not None and hi is not None and lo > 0 and hi > 0:
            tokens.append(f"{lo}-{hi}号楼")
    stripped = _RANGE_TOKEN_RE.sub(" ", stripped)
    for match in _BUILDING_TOKEN_RE.finditer(stripped):
        number = _cn_token_to_int(match.group(1))
        if number is not None and number > 0:
            tokens.append(f"{number}号楼")
    return tokens


def suggest_building_candidates(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """按行生成可追溯楼栋候选（只读，不改原事实）。

    来源字段：行名称/项目特征/清单编码 + 工作表名 + 原文件名，全部复用
    ``_building_tokens`` 同一 token 规则。候选按 (line_item_id, 楼栋) 组织，
    便于 UI 按行展示；同一行内不同字段给出不同单栋楼栋时标记
    ``conflict``（如行写 1号楼、Sheet 写 2号楼）——不同行的不同楼栋不是
    冲突。组合范围（如 1-3号楼）不生成单栋候选，由台账按组合范围处理。
    """
    rows = conn.execute(
        """SELECT li.id AS line_item_id, li.period_id, li.code, li.name, li.feature,
                  li.flags_json, rs.id AS sheet_id, rs.sheet_name, sf.id AS file_id,
                  sf.original_name
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE sp.project_id=? ORDER BY li.id""",
        (int(project_id),),
    ).fetchall()
    candidates: dict[tuple[int, str], dict] = {}
    for row in rows:
        flags = json.loads(row["flags_json"] or "{}")
        row_source_fields = _row_building_source_fields(
            row["code"], row["name"], row["feature"],
            row["sheet_name"], row["original_name"])
        row_singles = {
            token for _f, _t, tokens in row_source_fields
            for token in tokens if _is_single_building_scope(token)
        }
        row_conflict = len(row_singles) > 1 or bool(row_singles and any(
            not _is_single_building_scope(token)
            for _field, _text, tokens in row_source_fields for token in tokens))
        for field, text, tokens in row_source_fields:
            for token in tokens:
                if not _is_single_building_scope(token):
                    continue  # 组合范围不作为单栋候选
                entry = candidates.setdefault((int(row["line_item_id"]), token), {
                    "period_id": int(row["period_id"]),
                    "line_item_id": int(row["line_item_id"]),
                    "building": token,
                    "sources": [],
                    "conflict": row_conflict,
                })
                entry["sources"].append({
                    "line_item_id": int(row["line_item_id"]),
                    "field": field,
                    "text": text,
                    "row": flags.get("row"),
                    "file": row["original_name"],
                    "sheet": row["sheet_name"],
                })
    return sorted(candidates.values(),
                  key=lambda c: (c["period_id"], c["line_item_id"], c["building"]))


_BUILDING_SOURCE_FIELD_ZH = {
    "row-name": "行名称",
    "row-feature": "项目特征",
    "row-code": "清单编码",
    "sheet": "工作表",
    "file": "来源文件名",
}


def _row_building_source_fields(code, name, feature, sheet_name, original_name):
    """收集一行的楼栋 token 来源：(字段, 原文, tokens)，字段顺序稳定。"""
    out: list[tuple[str, str, list[str]]] = []
    for field, text in (
        ("row-name", name),
        ("row-feature", feature),
        ("row-code", code),
        ("sheet", sheet_name),
        ("file", original_name),
    ):
        text = str(text or "").strip()
        tokens = _building_tokens(text)
        if tokens:
            out.append((field, text, tokens))
    return out


def _derive_rule_building(source_fields):
    """从行来源推导规则楼栋（只读）：返回 (building, status, basis)。

    - 单栋 token 恰一个且无组合范围 → rule_accepted（明确且一致）；
    - 单栋 token 多个 → 来源冲突，pending 并解释矛盾字段；
    - 组合范围（如 1-3号楼）→ 组合值本身作为 building（不摊分），状态
      combined_scope；组合与单栋并存时保守 pending（范围声明优先）；
    - 无任何 token → pending（未确认）。
    """
    singles: list[str] = []
    singles_sources: dict[str, tuple[str, str]] = {}
    combined: list[tuple[str, str, str]] = []
    for field, text, tokens in source_fields:
        for token in tokens:
            if _is_single_building_scope(token):
                if token not in singles:
                    singles.append(token)
                singles_sources.setdefault(token, (field, text))
            else:
                combined.append((token, field, text))
    if combined and singles:
        token, field, text = combined[0]
        s_field, s_text = singles_sources[singles[0]]
        return None, None, (
            f"来源并存需人工确认：{_BUILDING_SOURCE_FIELD_ZH[s_field]}"
            f"「{s_text}」识别 {singles[0]}，"
            f"{_BUILDING_SOURCE_FIELD_ZH[field]}「{text}」为组合范围 {token}，"
            "不摊分到单栋")
    if len({token for token, _field, _text in combined}) > 1:
        return None, None, "组合楼栋来源冲突，待人工确认：" + "；".join(
            f"{_BUILDING_SOURCE_FIELD_ZH[field]}「{text}」识别 {token}"
            for token, field, text in combined)
    if combined:
        token, field, text = combined[0]
        return token, "combined_scope", (
            f"规则自动：{_BUILDING_SOURCE_FIELD_ZH[field]}「{text}」"
            f"识别组合范围 {token}（不摊分到单栋）")
    if len(singles) == 1:
        field, text = singles_sources[singles[0]]
        return singles[0], "rule_accepted", (
            f"规则自动：{_BUILDING_SOURCE_FIELD_ZH[field]}「{text}」"
            f"识别 {singles[0]}")
    if len(singles) > 1:
        first_field, first_text = singles_sources[singles[0]]
        second_field, second_text = singles_sources[singles[1]]
        return None, None, (
            f"来源冲突：{_BUILDING_SOURCE_FIELD_ZH[first_field]}"
            f"「{first_text}」识别 {singles[0]}，"
            f"{_BUILDING_SOURCE_FIELD_ZH[second_field]}"
            f"「{second_text}」识别 {singles[1]}，待人工确认")
    return None, None, "未确认（仅计入全项目）"


# ---------------------------------------------------------------- 工程量台账

def _sheet_meta_map(conn: sqlite3.Connection, project_id: int) -> dict[int, dict]:
    rows = conn.execute(
        """SELECT rs.id AS sheet_id, rs.sheet_name, sf.id AS file_id,
                  sf.original_name
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=?""",
        (int(project_id),),
    ).fetchall()
    return {int(r["sheet_id"]): dict(r) for r in rows}


# 已验证标准量纲族首单位（unit_basis 对 kg/cm/mm 等折算到 t/m）。
_KNOWN_STANDARD_UNITS = frozenset({"t", "m", "m2", "m3"})


def _line_identity(code: str, name: str, feature: str,
                   line_ctx: dict | None, base_unit: str) -> tuple:
    """行级标准身份（items/同合同比较/跨方向控制共享路径）。

    优先级：人工标准键 > 描述规则身份 > 来源编码身份 > 来源名称身份。

    描述规则（rule_accepted）：完整名称 + 非空明确特征 + 已验证标准量纲
    完全一致时，跨来源编码（甚至无编码）归同对象——身份为
    ``("auto:desc", 名称键, 名称键, 特征键, 标准量纲)``，5 元形状与
    ``auto:code`` 对齐（identity[3]=特征、identity[4]=量纲），保证所有
    caller 索引一致。同码异名、跨特征、跨量纲、空特征、未知量纲一律
    保守保留编码/名称命名空间身份，不得混组；人工标准键始终优先。
    """
    if line_ctx and line_ctx.get("standard_key"):
        return ("manual", str(line_ctx["standard_key"]).strip())
    code_text = str(code or "").strip()
    name_key = feature_key(name)
    feature_id = feature_key(feature)
    if name_key and feature_id and base_unit in _KNOWN_STANDARD_UNITS:
        return ("auto:desc", name_key, name_key, feature_id, base_unit)
    if code_text:
        return ("auto:code", code_text, name_key, feature_id, base_unit)
    return ("auto:name", name_key, feature_id, base_unit)


def _identity_meta(identity: tuple) -> tuple[str, str]:
    """身份状态与依据（不冒记人工确认）：供来源追溯与导出。"""
    kind = identity[0]
    if kind == "manual":
        return "manual_standard_key", f"人工标准键「{identity[1]}」（人工优先）"
    if kind == "auto:desc":
        return "rule_accepted_desc", (
            f"描述规则自动接受：名称「{identity[1]}」+ 特征「{identity[3]}」"
            f"+ 标准量纲 {identity[4]} 完整一致")
    if kind == "auto:code":
        return "source_code_identity", (
            f"来源编码「{identity[1]}」身份（完整描述条件不满足，保守不归并）")
    return "source_name_identity", (
        "来源名称身份（完整描述条件不满足，保守不归并）")


def _project_desc(identity: tuple) -> tuple | None:
    """把身份投影为描述身份 (名称键, 特征键, 量纲)；条件不完整返回 None。"""
    identity = tuple(identity)
    kind = identity[0]
    if kind == "manual":
        return None
    if kind == "auto:desc":
        name_key, feature_id, base_unit = identity[1], identity[3], identity[4]
    elif kind == "auto:code":
        name_key, feature_id, base_unit = identity[2], identity[3], identity[4]
    else:
        name_key, feature_id, base_unit = identity[1], identity[2], identity[3]
    if not name_key or not feature_id or base_unit not in _KNOWN_STANDARD_UNITS:
        return None
    return (name_key, feature_id, base_unit)


_SINGLE_SCOPE_SEPARATORS = ("-", "—", "~", "、", ",", "，", "/", "／", "+")


def _is_single_building_scope(scope: str) -> bool:
    """单栋范围判定：含区间/并列分隔符的都视为多栋范围。"""
    return bool(scope) and not any(ch in scope for ch in _SINGLE_SCOPE_SEPARATORS)


def _collect_pending_quantity_rows(
    conn: sqlite3.Connection,
    project_id: int,
    confirmed_line_contexts: dict[int, dict],
) -> list[dict]:
    """收集「可辨认但尚未进入有效核量」的全部业务明细，供比较/控制拦截。

    缺量不等于零：数量缺失/不可解析的待确认行意味着未来确认后的数量
    未知，不得因当前无数字就视为"不改变数量结论"。仍只排除小计/合计/
    层级与扣款行；每行按与台账一致的规则计算标准身份（人工确认换算用
    目标标准单位），未知单位/特征保留空值交由 :func:`_coverage_hits`
    做保守候选匹配。
    """
    rows = conn.execute(
        """SELECT li.id, li.period_id, li.code, li.name, li.feature, li.unit,
                  li.quantity, li.flags_json, qpc.work_scope
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           LEFT JOIN quantity_period_context qpc
                  ON qpc.period_id=sp.id AND qpc.project_id=sp.project_id
           WHERE sp.project_id=?
             AND (qpc.id IS NULL OR qpc.status='pending')""",
        (int(project_id),),
    ).fetchall()
    entries: list[dict] = []
    for row in rows:
        if is_non_detail_flags(row["flags_json"]):
            continue
        flags = json.loads(row["flags_json"] or "{}")
        if flags.get("deduction"):
            continue
        line_ctx = confirmed_line_contexts.get(int(row["id"]))
        base = unit_basis(row["unit"])
        base_unit = base[0] if base else ""
        if line_ctx and line_ctx["convert_factor"] and line_ctx["target_unit"]:
            converted = unit_basis(str(line_ctx["target_unit"]))
            base_unit = converted[0] if converted else str(line_ctx["target_unit"]).strip()
        identity = _line_identity(
            row["code"], row["name"], row["feature"], line_ctx, base_unit)
        entries.append({
            "scope": (line_ctx.get("work_scope") if line_ctx else None)
                     or row["work_scope"] or None,
            "identity": identity,
            "code": str(row["code"] or "").strip(),
            "name_key": feature_key(row["name"]),
            "feature": feature_key(row["feature"]),
            "base": base_unit,
            "line_item_id": int(row["id"]),
            "period_id": int(row["period_id"]),
            "quantity": row["quantity"],
            "unit": row["unit"],
            "name": row["name"],
            "reason": "期次数量上下文待确认或未登记",
        })
    return entries


def _identity_may_match(control: tuple, control_code: str, control_name_key: str,
                        entry: dict) -> bool:
    """保守判定待确认行是否可能属于该控制对象（描述身份共享路径）。

    - 双方均为人工标准键：键相同即命中；
    - 任一方为人工键：待确认行保留的原始编码/名称与控制成员编码或
      归一化名称存在文本关联即命中（人工键可能覆盖同一对象）；
    - 双方均为自动身份：先投影描述身份——双方都完整（名称+特征+已验证
      量纲）时相等即同对象、不等即不同对象；任一方缺字段（缺特征/未知
      量纲/纯名称身份）保守回退文本关联，绝不因信息不完整而漏拦；
    - 特征/量纲仅在双方都明确且不同时排除。
    """
    control = tuple(control)
    entry_identity = tuple(entry["identity"])
    if control[0] == "manual" and entry_identity[0] == "manual":
        return entry_identity[1] == control[1]
    if control[0] == "manual" or entry_identity[0] == "manual":
        other_code = entry.get("code")
        other_name = entry.get("name_key")
        if other_code and other_code == control_code:
            return True
        if other_name and other_name == control_name_key:
            return True
        return False
    control_desc = _project_desc(control)
    entry_desc = _project_desc(entry_identity)
    if control_desc is not None and entry_desc is not None:
        return control_desc == entry_desc
    # 任一方描述条件不完整：保守文本关联。
    other_code = entry.get("code")
    other_name = entry.get("name_key")
    control_key = control[1] if len(control) > 1 else ""
    if other_code and other_code in {control_key, control_code, control_name_key}:
        return True
    if other_name and other_name in {control_key, control_code, control_name_key}:
        return True
    # 同命名空间键不同时的跨命名空间文本关联（编码↔名称）。
    if entry_identity[0] == control[0] and entry_identity[1] != control[1]:
        if entry_identity[0] == "auto:code" and other_name \
                and other_name == control_name_key:
            return True
        if entry_identity[0] == "auto:name" and other_code \
                and other_code == control_code:
            return True
        return False
    control_feature = control[3] if len(control) > 3 else ""
    if entry.get("feature") and control_feature \
            and entry["feature"] != control_feature:
        return False
    control_base = control[-1]
    if entry.get("base") and control_base and entry["base"] != control_base:
        return False
    return False


def _coverage_hits(
    coverage: list[dict],
    work_scope: str,
    identity: tuple,
    code: str = "",
    name_key: str = "",
    source_codes: list[str] | None = None,
) -> list[dict]:
    """待确认明细与该控制对象的保守候选交集（含行来源）。"""
    control_code = code or (identity[1] if len(identity) > 1 else "")
    control_name_key = name_key or (
        identity[2] if identity[0] in ("auto:code", "auto:desc")
        and len(identity) > 2
        else identity[1] if identity[0] == "auto:name" else "")
    hits = []
    for entry in coverage:
        if entry["scope"] is not None and entry["scope"] != (work_scope or None):
            continue
        if any(_identity_may_match(identity, member_code, control_name_key, entry)
               for member_code in (source_codes or [control_code])):
            hits.append(entry)
    return hits


def build_quantity_ledger(conn: sqlite3.Connection, project_id: int) -> dict:
    """构建同一份可序列化工程量台账（UI / Excel / Word 共用）。

    只读、确定性：不写库、不改原始行。输出：
    - ``items``：upward/downward × 合同/已结算 × 多合同/单位分项数量，
      含逐行来源（文件/Sheet/行/line_item_id）、原数量/单位/换算系数/
      标准量、有效期次与业务期号；
    - ``comparisons``：同方向内合同 vs 已结算比较（完整身份配对：编码/
      名称命名空间 + 特征 + 量纲 + 口径）；
    - ``quantity_controls``：跨方向控制——对上单合同基准（合同量上限与
      对上已结算两种基准同组保留）vs 对下 A/B/C 多合同同标准项/特征/口径
      合并累计（contributions 列出各合同各单位数量），不要求上下游合同键
      相同；
    - 缺失/不可比/未确认/存在待确认数量明细时显式 PENDING/INCOMPARABLE，
      绝不补 0 或输出 PASS；结论只报差额，不认定违规。
    """
    project_id = int(project_id)
    contexts = list_period_contexts(conn, project_id)
    unregistered_periods = [dict(row) for row in conn.execute(
        """SELECT sp.id AS period_id, sp.period_no AS internal_period_no,
                  sp.direction, sp.title, sf.original_name AS source_file,
                  COUNT(li.id) AS detail_count
           FROM settlement_periods sp
           LEFT JOIN quantity_period_context qpc ON qpc.period_id=sp.id
           LEFT JOIN source_files sf ON sf.id=sp.source_file_id
           LEFT JOIN line_items li ON li.period_id=sp.id
           WHERE sp.project_id=? AND qpc.id IS NULL
           GROUP BY sp.id ORDER BY sp.id""", (project_id,)
    )]
    pending_contexts = [
        {k: c[k] for k in (
            "context_id", "period_id", "direction", "contract_key",
            "business_period_no", "unit_name", "doc_kind", "amount_mode",
            "work_scope", "status")}
        for c in contexts if c["status"] == PERIOD_PENDING
    ]
    superseded_contexts = [
        {"context_id": c["context_id"], "period_id": c["period_id"],
         "contract_key": c["contract_key"],
         "business_period_no": c["business_period_no"]}
        for c in contexts if c["status"] == PERIOD_SUPERSEDED
    ]
    confirmed = [c for c in contexts if c["status"] == PERIOD_CONFIRMED]
    superseded_period_ids = {
        int(c["supersedes_period_id"]) for c in confirmed
        if c["supersedes_period_id"] is not None
    }
    effective = [
        c for c in confirmed if int(c["period_id"]) not in superseded_period_ids
    ]
    # 两个有效同身份版本：不取最新导入，整体标记 CONTROL_CONFLICT。
    by_identity: dict[tuple, list[dict]] = {}
    for context in effective:
        by_identity.setdefault(_identity_of(context), []).append(context)
    identity_conflicts = []
    conflict_period_ids: set[int] = set()
    for identity, members in by_identity.items():
        if len(members) > 1:
            identity_conflicts.append({
                "direction": identity[0],
                "contract_key": identity[1],
                "unit_name": identity[2],
                "doc_kind": identity[3],
                "business_period_no": identity[4],
                "context_ids": [m["context_id"] for m in members],
                "status": COMPARE_CONTROL_CONFLICT,
                "reason": "同一业务身份存在多个有效已确认版本且无替代关系，"
                          "不得随意取最新导入；请人工裁决替代关系",
            })
            conflict_period_ids.update(int(m["period_id"]) for m in members)
    effective_unique = [
        c for c in effective if int(c["period_id"]) not in conflict_period_ids
    ]

    # 口径（方向+合同+单位+资料类型+工作口径）内的计量模式必须唯一。
    scope_modes: dict[tuple, set[str]] = {}
    for context in effective_unique:
        scope_modes.setdefault(
            (context["direction"], context["contract_key"], context["unit_name"],
             context["doc_kind"], context["work_scope"]),
            set(),
        ).add(context["amount_mode"])
    scope_latest: dict[tuple, int] = {}
    for context in effective_unique:
        if context["amount_mode"] != MODE_CUMULATIVE:
            continue
        key = (context["direction"], context["contract_key"], context["unit_name"],
               context["doc_kind"], context["work_scope"])
        business_no = int(context["business_period_no"])
        if key not in scope_latest or business_no > scope_latest[key]:
            scope_latest[key] = business_no

    line_contexts = {
        int(r["line_item_id"]): dict(r)
        for r in conn.execute(
            """SELECT line_item_id, standard_key, building, work_scope,
                      convert_factor, target_unit, convert_basis
               FROM quantity_line_context WHERE project_id=? AND status='confirmed'""",
            (project_id,),
        ).fetchall()
    }
    sheet_meta = _sheet_meta_map(conn, project_id)
    coverage = _collect_pending_quantity_rows(
        conn, project_id, line_contexts)

    rows = conn.execute(
        """SELECT li.id, li.period_id, li.sheet_id, li.code, li.name, li.feature,
                  li.unit, li.quantity, li.flags_json,
                  sp.direction, sp.period_no AS internal_period_no,
                  qpc.contract_key, qpc.business_period_no, qpc.unit_name,
                  qpc.doc_kind, qpc.amount_mode, qpc.work_scope AS period_work_scope,
                  qpc.building_scope, qpc.building_status
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           JOIN quantity_period_context qpc ON qpc.period_id=sp.id
           WHERE sp.project_id=? AND qpc.status='confirmed'
           ORDER BY li.id""",
        (project_id,),
    ).fetchall()

    groups: dict[tuple, dict] = {}
    notes: list[str] = []
    skipped_non_detail = 0
    skipped_deduction = 0
    unconfirmed_building_rows = 0

    for row in rows:
        if int(row["period_id"]) in conflict_period_ids:
            continue
        flags = json.loads(row["flags_json"] or "{}")
        if is_non_detail_flags(row["flags_json"]):
            skipped_non_detail += 1
            continue
        if flags.get("deduction"):
            skipped_deduction += 1
            continue
        line_ctx = line_contexts.get(int(row["id"]))
        work_scope = (
            (line_ctx["work_scope"] if line_ctx and line_ctx["work_scope"] else None)
            or row["period_work_scope"] or ""
        )
        sheet_info = sheet_meta.get(int(row["sheet_id"])) if row["sheet_id"] else None
        # 楼栋推导（只读）：人工行级 > 人工期次范围 > 规则自动 > 待确认。
        # 规则自动复用同一 token 规则覆盖行字段与 Sheet/文件名，来源矛盾
        # 或组合/并存保持待确认；人工确认始终优先覆盖自动候选。
        building: str | None = None
        building_status: str | None = None
        building_basis: str | None = None
        if line_ctx and line_ctx["building"]:
            building = str(line_ctx["building"]).strip()
            building_status = "line_confirmed"
            building_basis = "行级人工确认"
        elif row["building_status"] == BUILDING_CONFIRMED and row["building_scope"]:
            building = str(row["building_scope"]).strip()
            building_status = "period_confirmed"
            building_basis = "期次确认范围"
        else:
            rule_building, rule_status, rule_basis = _derive_rule_building(
                _row_building_source_fields(
                    row["code"], row["name"], row["feature"],
                    sheet_info["sheet_name"] if sheet_info else None,
                    sheet_info["original_name"] if sheet_info else None))
            building = rule_building
            building_status = rule_status
            building_basis = rule_basis
            if building is None:
                unconfirmed_building_rows += 1
        # 数量与换算：确定性 Decimal；缺失/未知不补值。
        problem: str | None = None
        quantity = _try_decimal(row["quantity"])
        if quantity is None:
            problem = "数量缺失或不可解析（不得补 0）"
        total_factor: Decimal | None = None
        target_unit = ""
        human_factor: str | None = None
        source_basis = unit_basis(row["unit"])
        if line_ctx and line_ctx["convert_factor"] and line_ctx["target_unit"]:
            # 人工换算：原数量 × 人工系数 × 目标单位固定倍率。
            # target_unit=kg 时 kg 的固定倍率 0.001 必须再乘（1×0.5kg=0.0005t）。
            human = _try_decimal(str(line_ctx["convert_factor"]))
            if human is None or human <= 0:
                problem = problem or "人工换算系数不可解析，不得猜系数"
            else:
                converted_basis = unit_basis(str(line_ctx["target_unit"]))
                if converted_basis:
                    target_unit = converted_basis[0]
                    total_factor = human * converted_basis[1]
                else:
                    target_unit = str(line_ctx["target_unit"]).strip()
                    total_factor = human
                human_factor = str(human)
        elif source_basis is not None:
            total_factor = source_basis[1]
            target_unit = source_basis[0]
        else:
            target_unit = str(row["unit"] or "").strip()
            problem = problem or "单位未知且无人工确认换算，不得猜系数"
        if quantity is not None and total_factor is not None:
            standard_quantity = quantity * total_factor
        else:
            standard_quantity = None
            if problem is None:
                problem = "换算不可用，数量待确认"
        identity = _line_identity(
            row["code"], row["name"], row["feature"], line_ctx, target_unit)
        group_key = (
            row["direction"], row["doc_kind"], row["contract_key"],
            row["unit_name"], work_scope, identity,
        )
        source = {
            "line_item_id": int(row["id"]),
            "file": sheet_info["original_name"] if sheet_info else None,
            "file_id": sheet_info["file_id"] if sheet_info else None,
            "sheet": sheet_info["sheet_name"] if sheet_info else None,
            "sheet_id": int(row["sheet_id"]) if row["sheet_id"] else None,
            "row": flags.get("row"),
            "period_id": int(row["period_id"]),
            "business_period_no": int(row["business_period_no"]),
            "internal_period_no": int(row["internal_period_no"]),
            "building": building,
            "building_status": building_status,
            "building_basis": building_basis,
            "original_quantity": row["quantity"],
            "original_unit": row["unit"],
            "original_code": str(row["code"] or "").strip(),
            "original_name": str(row["name"] or "").strip(),
            "original_feature": str(row["feature"] or "").strip(),
            "identity_status": _identity_meta(identity)[0],
            "identity_basis": _identity_meta(identity)[1],
            "factor": str(total_factor) if total_factor is not None else None,
            "human_convert_factor": human_factor,
            "standard_quantity": str(standard_quantity) if standard_quantity is not None else None,
            "target_unit": target_unit,
            "problem": problem,
        }
        group = groups.setdefault(group_key, {
            "direction": row["direction"],
            "doc_kind": row["doc_kind"],
            "contract_key": row["contract_key"],
            "unit_name": row["unit_name"],
            "work_scope": work_scope,
            "identity": identity,
            "target_units": set(),
            "feature_keys": set(),
            "display_name": str(row["name"] or "").strip(),
            "code": str(row["code"] or "").strip(),
            "source_codes": set(),
            "source_names": set(),
            "source_features": set(),
            "rows": [],
        })
        group["source_codes"].add(str(row["code"] or "").strip())
        group["source_names"].add(str(row["name"] or "").strip())
        group["source_features"].add(str(row["feature"] or "").strip())
        group["rows"].append({
            "source": source,
            "building": building,
            "standard_quantity": standard_quantity,
            "target_unit": target_unit,
            "business_period_no": int(row["business_period_no"]),
            "amount_mode": row["amount_mode"],
            "feature_key": feature_key(row["feature"]),
            "problem": problem,
        })
        if target_unit:
            group["target_units"].add(target_unit)
        group["feature_keys"].add(feature_key(row["feature"]))

    if skipped_non_detail:
        notes.append(f"已排除非明细行（小计/合计/层级）{skipped_non_detail} 行，不入数量累计")
    if skipped_deduction:
        notes.append(f"已排除扣款行 {skipped_deduction} 行（负向调整不参与正向数量）")
    if unconfirmed_building_rows:
        notes.append(
            f"{unconfirmed_building_rows} 行明细楼栋未确认：仅计入全项目合计，"
            "单栋结论保持待确认")

    # ---- 分项汇总（模式感知：增量累加 / 累计取最新有效业务期快照）----
    items: list[dict] = []
    for group_key in sorted(
            groups,
            key=lambda k: (k[0], k[1], k[2], k[4], str(k[5]))):
        group = groups[group_key]
        scope_key = (group["direction"], group["contract_key"], group["unit_name"],
                     group["doc_kind"], group["work_scope"])
        modes = scope_modes.get(scope_key, set())
        status = "ok"
        reasons: list[str] = []
        if len(modes) > 1:
            status = "pending"
            reasons.append(
                "同合同同口径下计量模式不一致（incremental/cumulative 混用），"
                "不得混算，待人工统一口径")
        mode = next(iter(modes)) if len(modes) == 1 else None
        counted_rows: list[dict] = []
        if mode == MODE_CUMULATIVE:
            latest = scope_latest.get(scope_key)
            counted_rows = [
                r for r in group["rows"]
                if r["business_period_no"] == latest
            ]
            if not counted_rows:
                status = "pending"
                reasons.append(
                    "最新有效业务期缺少该清单项：不得回填旧量或按 0 处理，待补资料")
        else:
            counted_rows = list(group["rows"])
        if len(group["target_units"]) > 1:
            status = "incomparable"
            reasons.append(
                "同一清单项内单位量纲不一致（"
                + "/".join(sorted(group["target_units"]))
                + "），跨量纲换算未全部人工确认，不得混算")
        if group["identity"][0] == "manual" and len(group["feature_keys"]) > 1:
            status = "incomparable"
            reasons.append(
                "人工标准键不得跨项目特征混算：同一标准键下存在多个特征，待人工复核")
        problems = [r["problem"] for r in counted_rows if r["problem"]]
        if problems:
            if status == "ok":
                status = "pending"
            reasons.append("；".join(dict.fromkeys(problems)))

        buildings: dict[str, dict] = {}
        building_keys = sorted({
            r["building"] for r in counted_rows if r["building"]
        })
        # 不可比（量纲/特征冲突）的组不得输出无意义的混合合计：数量置
        # None，明细与原因保留在 sources/reason 中供追溯。
        suppress_quantity = status == "incomparable"
        for building in building_keys:
            rows_b = [r for r in counted_rows if r["building"] == building]
            total, has_problem = _sum_or_none(rows_b)
            buildings[building] = {
                "quantity": (
                    str(total) if total is not None and not suppress_quantity
                    else None),
                "status": (
                    "ok" if total is not None and not has_problem
                    else ("incomparable" if status == "incomparable" else "pending")),
            }
        total_all, has_problem_all = _sum_or_none(counted_rows)
        buildings[ALL_BUILDINGS_KEY] = {
            "quantity": (
                str(total_all) if total_all is not None and not suppress_quantity
                else None),
            "status": (
                "ok" if total_all is not None and not has_problem_all
                else ("incomparable" if status == "incomparable" else "pending")),
        }
        for row_info in group["rows"]:
            counted = row_info in counted_rows
            row_info["source"]["counted"] = counted
            if not counted:
                row_info["source"]["note"] = (
                    "累计口径：仅最新有效业务期快照计入，此行为历史快照")
        single_scopes = sorted({
            r["building"] for r in counted_rows
            if r["building"] and _is_single_building_scope(r["building"])})
        multi_scopes = sorted({
            r["building"] for r in counted_rows
            if r["building"] and not _is_single_building_scope(r["building"])})
        items.append({
            "direction": group["direction"],
            "doc_kind": group["doc_kind"],
            "contract_key": group["contract_key"],
            "unit_name": group["unit_name"],
            "work_scope": group["work_scope"],
            "standard_key": group["identity"][1] if group["identity"][0] == "manual" else None,
            "standard_key_source": group["identity"][0],
            "identity": list(group["identity"]),
            "identity_status": _identity_meta(group["identity"])[0],
            "identity_basis": _identity_meta(group["identity"])[1],
            "source_codes": sorted(c for c in group["source_codes"] if c),
            "source_names": sorted(n for n in group["source_names"] if n),
            "source_features": sorted(f for f in group["source_features"] if f),
            "code": group["code"] or group["display_name"],
            "feature": (
                sorted(group["feature_keys"])[0]
                if len(group["feature_keys"]) == 1 else None),
            "display_name": group["display_name"],
            "standard_unit": (
                sorted(group["target_units"])[0]
                if len(group["target_units"]) == 1 else ""
            ),
            "mode": mode,
            "status": status,
            "reason": "；".join(reasons),
            "quantity": buildings.get(ALL_BUILDINGS_KEY, {}).get("quantity"),
            "buildings": buildings,
            "row_scope": {
                "single_buildings": single_scopes,
                "multi_building_scopes": multi_scopes,
                "unscoped_rows": sum(
                    1 for r in counted_rows if not r["building"]),
                "missing_quantity_rows": sum(
                    1 for r in counted_rows if r["standard_quantity"] is None),
            },
            "sources": [r["source"] for r in group["rows"]],
        })

    # ---- 同方向比较：合同 vs 已结算（完整身份配对 + 待确认覆盖）----
    comparisons: list[dict] = []
    pair_map: dict[tuple, dict[str, dict]] = {}
    for item in items:
        key = (
            item["direction"], item["contract_key"], item["unit_name"],
            item["work_scope"], tuple(item["identity"]),
        )
        pair_map.setdefault(key, {})[item["doc_kind"]] = item
    for key in sorted(pair_map, key=lambda k: (k[0], k[1], k[3], str(k[4]))):
        sides = pair_map[key]
        contract_item = sides.get(DOC_KIND_CONTRACT)
        settlement_item = sides.get(DOC_KIND_SETTLEMENT)
        building_set: set[str] = set()
        for side in (contract_item, settlement_item):
            if side:
                building_set.update(
                    b for b in side["buildings"] if b != ALL_BUILDINGS_KEY)
        for building in [None] + sorted(building_set):
            contract_quantity, contract_status = _side_quantity(
                contract_item, building)
            settlement_quantity, settlement_status = _side_quantity(
                settlement_item, building)
            delta: Decimal | None = None
            if contract_quantity is not None and settlement_quantity is not None \
                    and contract_item["standard_unit"] \
                    and settlement_item["standard_unit"] \
                    and contract_item["standard_unit"] != settlement_item["standard_unit"]:
                status = COMPARE_INCOMPARABLE
                reason = (
                    f"量纲不同：合同 {contract_item['standard_unit']}"
                    f" vs 已结算 {settlement_item['standard_unit']}，不得直接比较")
            elif contract_quantity is not None and settlement_quantity is not None:
                delta = settlement_quantity - contract_quantity
                if delta > 0:
                    status = COMPARE_FAIL
                    reason = (
                        f"已结算较合同基准多 {delta}"
                        f"{settlement_item['standard_unit'] or ''}"
                        "（仅提示差额，不构成违规或责任认定）")
                else:
                    status = COMPARE_PASS
                    reason = (
                        f"已结算未超合同基准（结余 {-delta}"
                        f"{settlement_item['standard_unit'] or ''}）")
            else:
                status, reason = _missing_status_reason(
                    contract_item, contract_status, settlement_item,
                    settlement_status, building)
            # 待确认/未登记上下文的数量明细：该身份的通过结论不可信。
            pair_item = contract_item or settlement_item
            hits = _coverage_hits(
                coverage, key[3], key[4],
                code=pair_item["code"] if pair_item else "",
                name_key=feature_key(pair_item["display_name"]) if pair_item else "",
                source_codes=sorted({c for side in sides.values()
                                     for c in side.get("source_codes", [])}))
            if hits and status == COMPARE_PASS:
                status = COMPARE_PENDING
                delta = None
                reason = (
                    f"存在 {len(hits)} 行待确认/未登记数量上下文的数量明细"
                    f"（行 ID：{', '.join(str(h['line_item_id']) for h in hits[:5])}"
                    "），通过结论与差额不可信；请先确认相关期次上下文")
            elif hits and status == COMPARE_FAIL:
                reason += (
                    f"；另有 {len(hits)} 行待确认/未登记数量明细未计入，"
                    "未确认数量可能改变最终差额")
            comparisons.append({
                "direction": key[0],
                "contract_key": key[1],
                "unit_name": key[2],
                "work_scope": key[3],
                "standard_key": (
                    key[4][1] if key[4][0] == "manual" else key[4][1]),
                "identity": list(key[4]),
                "code": key[4][1] if len(key[4]) == 2 else key[4][1],
                "standard_key_source": key[4][0],
                "standard_unit": (
                    (contract_item or settlement_item or {}).get("standard_unit", "")),
                "building": building,
                "contract_quantity": (
                    str(contract_quantity) if contract_quantity is not None else None),
                "settlement_quantity": (
                    str(settlement_quantity) if settlement_quantity is not None else None),
                "delta": str(delta) if delta is not None else None,
                "status": status,
                "reason": reason,
                "excluded_details": hits,
            })

    # ---- 跨方向控制：对上基准（合同量 + 对上已结算）vs 对下多合同累计 ----
    quantity_controls = _build_quantity_controls(items, coverage)

    counts = {state: 0 for state in (
        COMPARE_PASS, COMPARE_FAIL, COMPARE_PENDING,
        COMPARE_INCOMPARABLE, COMPARE_CONTROL_CONFLICT)}
    for comparison in comparisons:
        counts[comparison["status"]] = counts.get(comparison["status"], 0) + 1
    status_counts = {
        "items": len(items), "comparisons": len(comparisons),
        "quantity_controls": len(quantity_controls), **counts}
    control_counts = {f"controls_{state}": 0 for state in counts}
    for control in quantity_controls:
        key = f"controls_{control['status']}"
        control_counts[key] = control_counts.get(key, 0) + 1
    status_counts.update(control_counts)
    current = run_contract.get_current_contract(conn, project_id)
    return {
        "project_id": project_id,
        "generated_at": _now(),
        "run_signature": current.signature if current else None,
        "schema_version": _schema_version(conn),
        "status_counts": status_counts,
        "pending_contexts": pending_contexts,
        "unregistered_periods": unregistered_periods,
        "superseded_contexts": superseded_contexts,
        "identity_conflicts": identity_conflicts,
        "items": items,
        "comparisons": comparisons,
        "quantity_controls": quantity_controls,
        "notes": notes,
    }


def _build_quantity_controls(
    items: list[dict],
    coverage: dict[tuple, list[dict]],
) -> list[dict]:
    """对上基准 vs 对下多合同累计：同标准项/特征/口径合并，双基准同组保留。

    对下侧跨合同（A/B/C）合并到同一控制组，contributions 列出各合同/单位
    的数量；不要求上下游合同键相同——配对只看工作口径 + 标准身份。

    描述归并（rule_accepted 层）：跨来源编码不同（甚至一方无编码）的行，
    若完整名称、非空特征、标准量纲与工作口径完全一致，按描述身份归入同一
    控制组（身份标记 ``auto:desc``，``source_codes`` 保留各原编码来源）；
    同码异名、跨特征、跨量纲、跨口径、空特征、人工标准键一律不归并。
    """
    # 身份已在 _line_identity 共享路径统一（auto:desc 描述规则），items/
    # 同合同比较/跨方向控制使用同一 identity，无展示层二次归并。
    controls: dict[tuple, dict[str, list[dict]]] = {}
    for item in items:
        identity = tuple(item["identity"])
        key = (item["work_scope"], identity)
        controls.setdefault(key, {}).setdefault(
            (item["direction"], item["doc_kind"]), []).append(item)
    results: list[dict] = []
    for key in sorted(controls, key=lambda k: (k[0], str(k[1]))):
        work_scope, identity = key
        sides = controls[key]
        members = [item for group in sides.values() for item in group]
        source_codes = sorted({
            code for item in members
            for code in (item.get("source_codes") or {item.get("code")})
            if code})
        upward_contract_items = sides.get(("upward", DOC_KIND_CONTRACT), [])
        upward_settlement_items = sides.get(("upward", DOC_KIND_SETTLEMENT), [])
        downward_items = sides.get(("downward", DOC_KIND_SETTLEMENT), [])
        downward_contract_items = sides.get(("downward", DOC_KIND_CONTRACT), [])
        display = next(
            (side[0]["display_name"] for side in (
                upward_contract_items, upward_settlement_items,
                downward_items, downward_contract_items) if side),
            "")
        code = next(
            (side[0]["code"] for side in (
                upward_contract_items, upward_settlement_items,
                downward_items, downward_contract_items) if side),
            "")
        standard_unit = next(
            (side[0]["standard_unit"] for side in (
                upward_contract_items, upward_settlement_items,
                downward_items, downward_contract_items)
             if side and side[0]["standard_unit"]), "")
        unit_conflict = any(
            item["standard_unit"] and item["standard_unit"] != standard_unit
            for group in (upward_contract_items, upward_settlement_items,
                          downward_items, downward_contract_items)
            for item in group)
        building_set: set[str] = set()
        for group in (upward_contract_items, upward_settlement_items,
                      downward_items, downward_contract_items):
            for item in group:
                building_set.update(
                    b for b in item["buildings"] if b != ALL_BUILDINGS_KEY)
        for building in [None] + sorted(building_set):
            cap_qty, cap_status, cap_missing, cap_contrib = _merge_side_quantity(
                upward_contract_items, building)
            up_settled_qty, up_settled_status, up_settled_missing, up_settled_contrib = (
                _merge_side_quantity(upward_settlement_items, building))
            down_qty, down_status, down_missing, _down_contrib = _merge_side_quantity(
                downward_items, building)
            down_contract_qty, down_contract_status, down_contract_missing, down_contract_contrib = (
                _merge_side_quantity(downward_contract_items, building))
            contributions = []
            for item in sorted(
                    downward_items + downward_contract_items,
                    key=lambda i: (i["contract_key"], i["unit_name"])):
                entry = item["buildings"].get(
                    building if building is not None else ALL_BUILDINGS_KEY)
                if entry is None or entry["quantity"] is None:
                    continue
                contributions.append({
                    "direction": item["direction"],
                    "doc_kind": item["doc_kind"],
                    "contract_key": item["contract_key"],
                    "unit_name": item["unit_name"],
                    "quantity": entry["quantity"],
                })
            reasons: list[str] = []
            hits = _coverage_hits(
                coverage, work_scope, identity, code=code,
                name_key=feature_key(display), source_codes=source_codes)

            def _guarded(baseline_qty, baseline_status, baseline_missing,
                         baseline_label, baseline_contributors, *,
                         work_scope=work_scope, down_qty=down_qty, down_status=down_status,
                         down_missing=down_missing, unit_conflict=unit_conflict,
                         standard_unit=standard_unit, hits=hits):
                """非 ok 前置拦截 + 空口径/量纲冲突屏蔽数值比较。"""
                if not work_scope:
                    return COMPARE_PENDING, None, (
                        "工作计量口径未登记：空口径不构成可比口径，待人工确认")
                status, delta, reason = _compare_baselines(
                    baseline_qty,
                    baseline_status,
                    baseline_missing,
                    down_qty,
                    down_status,
                    down_missing,
                    baseline_label,
                    baseline_contributors,
                )
                if status in (COMPARE_PASS, COMPARE_FAIL) and unit_conflict:
                    return COMPARE_INCOMPARABLE, None, (
                        "上下游/各分包标准量纲不一致，不得直接比较")
                # 待确认/未登记上下文的数量明细：逐基准阻断 PASS。即使总体
                # 已因另一基准缺失先变 PENDING，本基准的 PASS 与精确差额
                # 也不得保留；真实 FAIL 保留为异常线索，但追加覆盖缺口说明。
                if hits and status == COMPARE_PASS:
                    return COMPARE_PENDING, None, (
                        f"存在 {len(hits)} 行待确认/未登记数量上下文的数量明细"
                        f"（行 ID：{', '.join(str(h['line_item_id']) for h in hits[:5])}"
                        f"），{baseline_label}的通过结论与差额不可信，待确认")
                if hits and status == COMPARE_FAIL:
                    return COMPARE_FAIL, delta, (
                        f"对下累计较{baseline_label}多 {delta}"
                        f"{standard_unit or ''}"
                        "（仅提示差额，不构成违规或责任认定）；"
                        f"另有 {len(hits)} 行待确认/未登记数量明细未计入，"
                        "未确认数量可能改变最终差额")
                if status == COMPARE_PASS:
                    reason = (
                        f"对下累计未超{baseline_label}（结余 {-delta}"
                        f"{standard_unit or ''}）")
                elif status == COMPARE_FAIL:
                    reason = (
                        f"对下累计较{baseline_label}多 {delta}"
                        f"{standard_unit or ''}"
                        "（仅提示差额，不构成违规或责任认定）")
                return status, delta, reason

            status_cap, delta_cap, reason_cap = _guarded(
                cap_qty, cap_status, cap_missing, "对上合同基准", cap_contrib)
            status_settled, delta_settled, reason_settled = _guarded(
                up_settled_qty, up_settled_status, up_settled_missing,
                "对上已结算量", up_settled_contrib)
            for text in (reason_cap, reason_settled):
                if text and text not in reasons:
                    reasons.append(text)
            # 总体状态基于本组实际已提供的有效基准汇总（至少一种即可形成
            # 结论）：对上可以只给合同清单或只给结算清单，不要求两种同时
            # 提供。实际提供但缺量/待确认的基准不得忽略；两种都未提供才
            # 整体 PENDING；FAIL 优先暴露。
            compared = []
            if cap_status != "absent":
                compared.append(("upward_contract", status_cap))
            if up_settled_status != "absent":
                compared.append(("upward_settlement", status_settled))
            if compared:
                overall = max(
                    (s for _, s in compared),
                    key=lambda s: _SEVERITY.get(s, 0))
            else:
                overall = COMPARE_PENDING
                if "缺少对上合同基准（待补资料或登记/确认对上清单）" not in reasons:
                    reasons.append("缺少对上合同基准与对上已结算量：本控制组无任何"
                                   "有效基准，待补资料")
            results.append({
                "work_scope": work_scope,
                "standard_key": identity[1] if identity[0] == "manual" else None,
                "identity": list(identity),
                "identity_source": key[1][0],
                "source_codes": source_codes,
                "code": code,
                "display_name": display,
                "standard_unit": standard_unit,
                "building": building,
                "compared_baselines": [name for name, _s in compared],
                "baseline_statuses": {
                    "upward_contract": status_cap,
                    "upward_settlement": status_settled,
                },
                "baselines": {
                    "upward_contract": {
                        "quantity": str(cap_qty) if cap_qty is not None else None,
                        "status": cap_status,
                        "contributors": cap_contrib,
                    },
                    "upward_settlement": {
                        "quantity": (
                            str(up_settled_qty) if up_settled_qty is not None else None),
                        "status": up_settled_status,
                        "contributors": up_settled_contrib,
                    },
                    "downward_contract": {
                        "quantity": (
                            str(down_contract_qty)
                            if down_contract_qty is not None else None),
                        "status": down_contract_status,
                        "contributors": down_contract_contrib,
                    },
                },
                "downstream_quantity": str(down_qty) if down_qty is not None else None,
                "downstream_status": down_status,
                "contributions": contributions,
                "missing_contributions": cap_missing + up_settled_missing + down_missing,
                "delta_vs_contract": (
                    str(delta_cap) if delta_cap is not None else None),
                "status_vs_contract": status_cap,
                "delta_vs_settlement": (
                    str(delta_settled) if delta_settled is not None else None),
                "status_vs_settlement": status_settled,
                "status": overall,
                "reason": "；".join(reasons),
                "excluded_details": hits,
            })
    return results


def _schema_version(conn: sqlite3.Connection) -> int:
    from jiadun.core.db import migrations

    try:
        return migrations.current_version(conn)
    except sqlite3.Error:
        return -1


def _sum_or_none(rows: list[dict]) -> tuple[Decimal | None, bool]:
    total: Decimal | None = None
    has_problem = False
    for row_info in rows:
        if row_info["problem"]:
            has_problem = True
        if row_info["standard_quantity"] is None:
            has_problem = True
            continue
        total = (
            row_info["standard_quantity"] if total is None
            else total + row_info["standard_quantity"]
        )
    return total, has_problem


def _side_quantity(item: dict | None, building: str | None):
    """读取某一侧（合同/已结算）在指定楼栋的数量与状态。"""
    if item is None:
        return None, "absent"
    key = building if building is not None else ALL_BUILDINGS_KEY
    entry = item["buildings"].get(key)
    if entry is None:
        return None, "absent"
    if entry["quantity"] is None or item["status"] != "ok":
        return None, item["status"] if entry["quantity"] is not None else "pending"
    return Decimal(entry["quantity"]), "ok"


def _merge_side_quantity(items: list[dict], building: str | None):
    """把同侧多个分项（多合同/单位）在指定楼栋的数量合并。

    排除规则（fail-closed）：
    - 该分项已明确归属**其他单栋**的完整资料 → 允许排除；
    - 未分配楼栋、多栋范围、缺量的分项 → 不得排除，标记待确认并返回
      缺失贡献明细（A 60 有效 + B 缺量不得输出 60 的通过结论）；
    - 基准侧同一楼栋出现多个贡献分项 → 默认不叠加（疑似重复合同清单），
      返回 stacking 标记，需人工去重/选择范围。

    返回 (total, status, missing, contributors)；total 为 None 时 status
    说明原因。contributors 是向该范围贡献了数量的分项数：对下侧多合同
    合并是设计内行为；对基准侧由调用方判定 stacking（不得默认叠加）。
    """
    if not items:
        return None, "absent", [], 0
    key = building if building is not None else ALL_BUILDINGS_KEY
    total: Decimal | None = None
    status = "ok"
    found = False
    contributors = 0
    missing: list[dict] = []
    for item in items:
        entry = item["buildings"].get(key)
        scope = item.get("row_scope") or {}
        quantity_text = entry["quantity"] if entry else None
        if quantity_text is not None:
            found = True
            contributors += 1
            total = (
                Decimal(quantity_text) if total is None
                else total + Decimal(quantity_text))
            if item["status"] == "incomparable":
                status = "incomparable"
            elif item["status"] != "ok" and status != "incomparable":
                status = "pending"
            continue
        # 该分项在此楼栋没有数量：判断是否允许合法排除。
        scope = item.get("row_scope") or {}
        if building is not None:
            # 只有已明确归属其他单栋、且无未分配/多栋/缺量行的完整资料
            # 才能排除；未知楼栋资料不得在单栋结论中被静默跳过。
            already_scoped_elsewhere = bool(
                scope.get("single_buildings")
                and building not in scope.get("single_buildings", [])
                and not scope.get("multi_building_scopes")
                and not scope.get("unscoped_rows"))
            if already_scoped_elsewhere and not scope.get("missing_quantity_rows"):
                continue
        missing.append({
            "direction": item["direction"],
            "doc_kind": item["doc_kind"],
            "contract_key": item["contract_key"],
            "unit_name": item["unit_name"],
            "reason": (
                "数量缺失或不可解析" if scope.get("missing_quantity_rows")
                else "楼栋未分配或多栋范围，不能排除出该楼栋结论"
                if (scope.get("unscoped_rows") or scope.get("multi_building_scopes"))
                else "该楼栋无此分项数量"),
        })
        if status != "incomparable":
            status = "pending"
    if not found:
        if missing:
            return None, "pending", missing, contributors
        return None, "absent", missing, contributors
    return total, status, missing, contributors


def _compare_baselines(
    baseline_qty,
    baseline_status,
    baseline_missing,
    down_qty,
    down_status,
    down_missing,
    baseline_label,
    baseline_contributors,
):
    """单一基准 vs 对下累计：先拦非 ok 状态，再比数值；PASS 逐基准屏蔽。"""
    if baseline_status == "absent":
        return COMPARE_PENDING, None, (
            f"缺少{baseline_label}（待补资料或登记/确认对上清单）")
    if baseline_contributors > 1:
        return COMPARE_PENDING, None, (
            f"{baseline_label}存在 {baseline_contributors} 个上游合同/单位分项"
            "向同一范围贡献数量：不得默认叠加重复合同清单，须人工明确范围"
            "去重或选择")
    if baseline_status == "incomparable":
        return COMPARE_INCOMPARABLE, None, f"{baseline_label}数量不可比"
    if baseline_status != "ok" or baseline_missing:
        reasons = "；".join(m["reason"] for m in baseline_missing[:3])
        return COMPARE_PENDING, None, (
            f"{baseline_label}存在未确认数量（{reasons}），不得用于通过结论")
    if down_status == "absent":
        return COMPARE_PENDING, None, "对下数量缺失，待补资料"
    if down_status == "incomparable":
        return COMPARE_INCOMPARABLE, None, "对下数量不可比"
    if down_status != "ok" or down_missing:
        reasons = "；".join(m["reason"] for m in down_missing[:3])
        return COMPARE_PENDING, None, (
            f"对下存在未确认数量贡献（{reasons}），不得输出通过结论")
    delta = down_qty - baseline_qty
    if delta > 0:
        return COMPARE_FAIL, delta, None
    return COMPARE_PASS, delta, None


def _missing_status_reason(
    contract_item, contract_status, settlement_item, settlement_status, building,
):
    scope_text = "全项目" if building is None else building
    if contract_item is None:
        return COMPARE_PENDING, f"{scope_text}缺少合同数量基准，待补资料或登记合同清单"
    if settlement_item is None:
        return COMPARE_PENDING, f"{scope_text}缺少已结算数量，待补资料"
    for label, status, item in (
        ("合同", contract_status, contract_item),
        ("已结算", settlement_status, settlement_item),
    ):
        if status == "absent":
            if label == "合同":
                return COMPARE_PENDING, (
                    f"{scope_text}缺少分栋合同基准（合同侧未按楼栋拆分或未确认），"
                    "楼栋结论保持待确认")
            return COMPARE_PENDING, (
                f"{scope_text}缺少分栋已结算数量，楼栋结论保持待确认")
        if status == "incomparable":
            return COMPARE_INCOMPARABLE, f"{label}侧数量不可比：{item['reason']}"
    return COMPARE_PENDING, f"{scope_text}数量未确认，待人工处理"
