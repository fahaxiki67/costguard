"""经营合规问题台账（P6）：人工评估字段与生命周期状态。

纪律：
- 台账字段（金额影响/责任单位/责任事项/处理意见/状态）只能由人工经本模块
  写入，且必须带操作者与原因，写入即产生审计事件；
- 台账按 (project_id, fingerprint) 定位：anomalies.id 每次分析运行都会
  重建，fingerprint 才是跨运行稳定的问题身份（与 finding_lifecycle 一致）；
- 金额影响为 TEXT 承载的精确十进制（Decimal 校验后保存），绝不引入浮点；
- 规则引擎（anomalies engine）永远不会写台账字段——事实差异与风险判断分离；
- ledger_status 全集：pending_confirmation（待确认）/ confirmed（已确认）/
  resolved（已解决）/ not_applicable（不适用）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from jiadun.core.evidence import audit as audit_log

LEDGER_STATUSES = (
    "pending_confirmation",
    "confirmed",
    "resolved",
    "not_applicable",
)

LEDGER_STATUS_ZH = {
    "pending_confirmation": "待确认",
    "confirmed": "已确认",
    "resolved": "已解决",
    "not_applicable": "不适用",
}

_UPDATABLE = ("amount_impact", "responsible_unit", "responsible_matter",
              "handling_opinion", "ledger_status")


@dataclass(frozen=True)
class LedgerEntry:
    fingerprint: str
    amount_impact: str | None
    responsible_unit: str | None
    responsible_matter: str | None
    handling_opinion: str | None
    ledger_status: str
    updated_by: str
    updated_at: str


def _resolve_fingerprint(
    conn: sqlite3.Connection, project_id: int, anomaly_id: int
) -> str:
    """anomalies.id 跨运行会重建，写入前把当前 id 解析为稳定 fingerprint。"""
    row = conn.execute(
        "SELECT fingerprint FROM anomalies WHERE id=? AND project_id=?",
        (int(anomaly_id), int(project_id)),
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise ValueError(
            f"异常不存在或缺少 fingerprint: anomaly_id={anomaly_id}")
    return str(row["fingerprint"])


def _validate_amount(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"金额影响必须是可解析的十进制值，收到: {value!r}") from exc
    return str(value).strip()


def _row_to_entry(row) -> LedgerEntry:
    return LedgerEntry(
        fingerprint=row["fingerprint"],
        amount_impact=row["amount_impact"],
        responsible_unit=row["responsible_unit"],
        responsible_matter=row["responsible_matter"],
        handling_opinion=row["handling_opinion"],
        ledger_status=row["ledger_status"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def update_finding_ledger(
    conn: sqlite3.Connection,
    project_id: int,
    anomaly_id: int | None = None,
    *,
    actor: str,
    reason: str,
    fingerprint: str | None = None,
    amount_impact: str | None = None,
    responsible_unit: str | None = None,
    responsible_matter: str | None = None,
    handling_opinion: str | None = None,
    ledger_status: str | None = None,
) -> LedgerEntry:
    """人工更新一条异常的台账字段（upsert），返回更新后的台账记录。

    定位方式二选一：fingerprint（跨运行稳定身份）或 anomaly_id（写入时
    解析为 fingerprint）。未传的字段保持原值；ledger_status 必须在全集内。
    金额影响做 Decimal 可解析校验。写入产生审计事件。
    """
    if not actor or not actor.strip():
        raise ValueError("台账更新必须记录操作者")
    if not reason or not reason.strip():
        raise ValueError("台账更新必须记录原因（留痕纪律）")
    if ledger_status is not None and ledger_status not in LEDGER_STATUSES:
        raise ValueError(f"无效台账状态: {ledger_status!r}（全集 {LEDGER_STATUSES}）")
    if fingerprint is None:
        if anomaly_id is None:
            raise ValueError("必须提供 fingerprint 或 anomaly_id 之一")
        fingerprint = _resolve_fingerprint(conn, project_id, int(anomaly_id))
    now = datetime.now().isoformat(timespec="seconds")
    fields = {
        "amount_impact": _validate_amount(amount_impact),
        "responsible_unit": responsible_unit,
        "responsible_matter": responsible_matter,
        "handling_opinion": handling_opinion,
        "ledger_status": ledger_status,
    }
    with conn:
        existing = conn.execute(
            "SELECT id FROM finding_ledger WHERE project_id=? AND fingerprint=?",
            (int(project_id), fingerprint),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO finding_ledger(
                       project_id, fingerprint, amount_impact, responsible_unit,
                       responsible_matter, handling_opinion, ledger_status,
                       updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(project_id), fingerprint, fields["amount_impact"],
                 fields["responsible_unit"], fields["responsible_matter"],
                 fields["handling_opinion"],
                 fields["ledger_status"] or "pending_confirmation",
                 actor.strip(), now),
            )
        else:
            sets, params = [], []
            for key in _UPDATABLE:
                if fields[key] is not None:
                    sets.append(f"{key}=?")
                    params.append(fields[key])
            if sets:
                sets += ["updated_by=?", "updated_at=?"]
                params += [actor.strip(), now, int(project_id), fingerprint]
                conn.execute(
                    f"UPDATE finding_ledger SET {', '.join(sets)} "
                    "WHERE project_id=? AND fingerprint=?",
                    params,
                )
        audit_log.record_audit(
            conn,
            project_id=int(project_id),
            actor=actor.strip(),
            action="finding_ledger_updated",
            target=f"finding:{str(fingerprint)[:24]}",
            before=None,
            after={k: v for k, v in fields.items() if v is not None},
            reason=reason.strip(),
        )
    return get_finding_ledger_by_fingerprint(conn, project_id, fingerprint)


def get_finding_ledger_by_fingerprint(
    conn: sqlite3.Connection, project_id: int, fingerprint: str
) -> LedgerEntry | None:
    row = conn.execute(
        """SELECT fingerprint, amount_impact, responsible_unit, responsible_matter,
                  handling_opinion, ledger_status, updated_by, updated_at
           FROM finding_ledger WHERE project_id=? AND fingerprint=?""",
        (int(project_id), str(fingerprint)),
    ).fetchone()
    return _row_to_entry(row) if row is not None else None


def get_finding_ledger(
    conn: sqlite3.Connection, project_id: int, anomaly_id: int
) -> LedgerEntry | None:
    """按当前运行里的异常 id 查台账（内部解析为 fingerprint）。"""
    fp = _resolve_fingerprint(conn, project_id, int(anomaly_id))
    return get_finding_ledger_by_fingerprint(conn, project_id, fp)


def list_finding_ledger(
    conn: sqlite3.Connection, project_id: int
) -> list[LedgerEntry]:
    rows = conn.execute(
        """SELECT fingerprint, amount_impact, responsible_unit, responsible_matter,
                  handling_opinion, ledger_status, updated_by, updated_at
           FROM finding_ledger WHERE project_id=? ORDER BY fingerprint""",
        (int(project_id),),
    ).fetchall()
    return [_row_to_entry(r) for r in rows]
