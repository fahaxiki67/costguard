"""清单匹配引擎（Phase 5 核心）。

五档置信度（最高优先级原则 9）：
  confirmed   规则完全匹配候选：编码一致且名称归一化一致，或命中用户别名库；
              数据库人工状态仍为 pending，需人工复核后才变为 confirmed
  probable    高概率匹配：编码一致但名称不同（同码异名），或名称归一化一致但编码不同
  suspected   疑似匹配：仅语义相似（rapidfuzz ≥ SIM_SUSPECTED），必须人工确认
  incomparable 不可比：单位不一致（归一化后）或口径冲突，禁止合并
  pending_data  待补资料：缺失编码与名称等关键字段

纪律：
- 不允许因为名称相似就直接合并（原则 8/9）：suspected 永远停在复核队列；
- 用户别名映射必须保存依据与确认记录（item_aliases + audit_log）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz

from costguard.core.anomalies.rules import _norm_unit
from costguard.core.contracts import run_contract
from costguard.core.evidence import audit as audit_log
from costguard.core.evidence import evidence as evidence_api
from costguard.core.matching.key_integrity import classify_composite_keys

# 置信度档位
CONFIRMED = "confirmed"
PROBABLE = "probable"
SUSPECTED = "suspected"
INCOMPARABLE = "incomparable"
PENDING_DATA = "pending_data"

LEVEL_SCORES = {CONFIRMED: 1.0, PROBABLE: 0.8, SUSPECTED: 0.5, INCOMPARABLE: 0.0, PENDING_DATA: 0.0}

SIM_CONFIRMED = 97.0   # 名称相似度达到此值且单位一致才可进入"确认"候选（仍需编码或用户确认）
SIM_SUSPECTED = 85.0   # 低于此值不产生候选

_STRIP_RE = re.compile(r"[\s（）()【】\[\]｛｝{}、，,。.：:；;\-—_－]+")


def normalize_name(name: str | None) -> str:
    """名称归一化：去空白/括号/标点，全角转半角，小写。"""
    if not name:
        return ""
    s = name.strip().lower()
    table = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    s = s.translate(table)
    return _STRIP_RE.sub("", s)


@dataclass
class MatchGroup:
    group_key: str
    level: str
    method: str
    score: float
    item_ids: list[int] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def load_aliases(
    conn: sqlite3.Connection, project_id: int, direction: str
) -> dict[str, str]:
    """读取一个明确方向的别名；项目级查询不得跨方向复用人工结论。"""
    rows = conn.execute(
        """SELECT alias_text, canonical_key FROM item_aliases
           WHERE project_id=? AND direction=?""",
        (project_id, direction),
    ).fetchall()
    return {normalize_name(r["alias_text"]): r["canonical_key"] for r in rows}


def _canonical_of(code: str, name: str) -> str:
    return f"code:{code}" if code else f"name:{name}"


def _unscoped_group_key(group_key: str) -> str:
    """匹配范围前缀只用于隔离展示，不写进可复用的别名核心键。"""
    prefix, separator, remainder = group_key.partition(":")
    if separator and prefix in {"upward", "downward", "unknown"}:
        return remainder
    return group_key


def _duplicate_composite_ids(rows: list[sqlite3.Row]) -> set[int]:
    """找出同一结算期内完整复合键重复的行，供自动匹配门控。

    同一编码在不同期次各出现一次是正常的跨期对照，不属于重复；因此先按
    ``period_id`` 分侧检查 ``code + unit``。同码异单位仍交给既有“不可比”判定，
    不会被这个门控误吞。
    """
    blocked: set[int] = set()
    period_rows: dict[int, list[dict]] = {}
    for row in rows:
        period_rows.setdefault(int(row["period_id"]), []).append({
            "id": int(row["id"]),
            "code": row["code"],
            "unit": row["unit"],
        })
    for side in period_rows.values():
        integrity = classify_composite_keys(side, [], ("code", "unit"))
        blocked.update(int(row["id"]) for row in integrity.duplicate_left)
    return blocked


def _match_items_for_direction(
    conn: sqlite3.Connection, project_id: int, direction: str
) -> list[MatchGroup]:
    """只在一个明确方向内做五档归组。"""
    rows = conn.execute(
        """SELECT li.id, li.period_id, li.code, li.name, li.unit, sp.period_no AS pno,
                  COALESCE(sp.direction, 'unknown') AS direction
           FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? AND COALESCE(sp.direction, 'unknown')=?
           ORDER BY li.id""",
        (project_id, direction),
    ).fetchall()
    aliases = load_aliases(conn, project_id, direction)
    blocked_duplicate_ids = _duplicate_composite_ids(rows)
    eligible_rows = [row for row in rows if int(row["id"]) not in blocked_duplicate_ids]

    # ---- 逐行归到候选键 ----
    exact: dict[str, list[int]] = {}  # canonical_key -> ids（编码精确）
    name_exact: dict[str, list[int]] = {}
    for r in eligible_rows:
        name = (r["name"] or "").strip()
        if r["code"]:
            exact.setdefault(r["code"], []).append(r["id"])
        if name:
            name_exact.setdefault(normalize_name(name), []).append(r["id"])

    groups: dict[str, MatchGroup] = {}

    def group_of(key: str) -> MatchGroup:
        return groups.setdefault(
            key, MatchGroup(group_key=key, level=CONFIRMED, method="exact", score=1.0)
        )

    # 编码精确组
    for code, ids in exact.items():
        g = group_of(f"code:{code}")
        g.item_ids = ids
        g.method = "code_exact"
        g.codes.add(code)

    # ---- 填充组的名称/单位（后续判定与融合都依赖 names）----
    all_rows = {r["id"]: r for r in rows}
    for g in groups.values():
        for iid in g.item_ids:
            r = all_rows[iid]
            g.names.add((r["name"] or "").strip())
            g.codes.add((r["code"] or "").strip())
            g.units.add(_norm_unit(r["unit"]))

    # 同名异码融合：名称归一化相同的编码组合并为一个高概率组
    code_groups = [g for g in groups.values() if g.group_key.startswith("code:")]
    by_name: dict[str, list[MatchGroup]] = {}
    for g in code_groups:
        for n in g.names:
            nm = normalize_name(n)
            if not nm:
                continue
            bucket = by_name.setdefault(nm, [])
            if g not in bucket:  # 同组多个名称变体只登记一次
                bucket.append(g)
    merged_keys: set[str] = set()
    for gs in by_name.values():
        if len(gs) > 1:
            base = gs[0]
            for other in gs[1:]:
                base.item_ids.extend(other.item_ids)
                base.names |= other.names
                base.codes |= other.codes
                base.units |= other.units
                merged_keys.add(other.group_key)
            base.level = PROBABLE
            base.method = "name_merge"
            base.score = 0.8
            base.notes.append(f"同名异码 {sorted(base.codes)}，已合并为高概率组，请复核")

    # 无编码行：名称精确 / 别名库 → 独立组或并入同名编码组
    assigned_ids = {iid for g in groups.values() for iid in g.item_ids}
    for nm, ids in name_exact.items():
        ids = [i for i in ids if i not in assigned_ids]
        if not ids:
            continue
        aliased_key = aliases.get(nm)
        target = None
        if aliased_key and aliased_key in groups:
            target = groups[aliased_key]
        else:
            # 并入名称归一化相同的编码组（若唯一）
            hosts = by_name.get(nm, [])
            if len(hosts) == 1:
                target = hosts[0]
        if target is not None:
            target.item_ids.extend(ids)
            for iid in ids:
                r = all_rows[iid]
                target.names.add((r["name"] or "").strip())
                target.units.add(_norm_unit(r["unit"]))
            if aliased_key:
                # 别名命中并入已有组 → 组升级为确认（依据已存别名库）
                target.level = CONFIRMED
                target.method = "alias"
                target.score = 1.0
            target.notes.append(f"{len(ids)} 行无编码，按名称归并")
            assigned_ids.update(ids)
            continue
        key = aliased_key or f"name:{nm}"
        g = group_of(key)
        g.item_ids = ids
        g.method = "alias" if aliased_key else "name_exact"
        g.score = 1.0 if aliased_key else 0.8
        g.level = CONFIRMED if aliased_key else PROBABLE
        for iid in ids:
            r = all_rows[iid]
            g.names.add((r["name"] or "").strip())
            g.units.add(_norm_unit(r["unit"]))
        assigned_ids.update(ids)

    # 归组失败且无法通过相似度处理的行 → 待补资料组
    blocked_rows = [all_rows[i] for i in all_rows if i in blocked_duplicate_ids]
    if blocked_rows:
        g = group_of("pending:blocked_by_duplicate")
        g.item_ids = [r["id"] for r in blocked_rows]
        g.level = PENDING_DATA
        g.method = "key_integrity"
        g.score = 0.0
        g.notes.append(
            "复合键（编码+单位）在同一结算期内重复，已阻断自动匹配；请人工拆分或确认"
        )
        for r in blocked_rows:
            g.names.add((r["name"] or "").strip())
            g.codes.add((r["code"] or "").strip())
            g.units.add(_norm_unit(r["unit"]))
        assigned_ids.update(r["id"] for r in blocked_rows)
    orphans = [all_rows[i] for i in all_rows if i not in assigned_ids]
    if orphans:
        g = MatchGroup(group_key="pending:orphan", level=PENDING_DATA, method="none", score=0.0,
                       item_ids=[r["id"] for r in orphans],
                       notes=["缺失名称/编码（待补资料）"])
        for r in orphans:
            g.names.add((r["name"] or "").strip())
        groups["pending:orphan"] = g

    # ---- 降级判定 ----
    result: list[MatchGroup] = []
    for key, g in groups.items():
        if key in merged_keys:
            continue
        if len(g.names) > 1 and key.startswith("code:"):
            g.level = PROBABLE
            g.notes.append("同码异名，请核实")
        if len(g.codes) > 1 and key.startswith("code:"):
            g.level = PROBABLE
            g.notes.append("同组出现多个编码")
        if len(g.units) > 1:
            g.level = INCOMPARABLE
            g.notes.append(f"单位不一致 {sorted(g.units)}：不可比，禁止合并")
        if key != "pending:orphan" and (any(not n for n in g.names) or not g.names):
            g.level = PENDING_DATA
            g.notes.append("缺失名称/编码（待补资料）")
        result.append(g)

    # ---- 疑似匹配：无编码且名称互不相同的组之间做语义相似 ----
    named_groups = [g for g in result if g.level != INCOMPARABLE and g.names and not g.codes]
    used = set()
    name_list = [(g, next(iter(g.names))) for g in named_groups]
    for i, (ga, na) in enumerate(name_list):
        if ga.group_key in used:
            continue
        for gb, nb in name_list[i + 1:]:
            if gb.group_key in used:
                continue
            score = fuzz.token_set_ratio(normalize_name(na), normalize_name(nb))
            if score >= SIM_CONFIRMED:
                ga.item_ids.extend(gb.item_ids)
                ga.names |= gb.names
                ga.level = PROBABLE
                ga.method = "fuzzy_name"
                ga.score = score / 100
                ga.notes.append(f"名称相似 {score:.0f}%，合并为高概率组，请复核")
                used.add(gb.group_key)
                break
            if score >= SIM_SUSPECTED and ga.level != PROBABLE:
                ga.level = SUSPECTED
                ga.score = score / 100
                ga.notes.append(f"与「{nb}」相似 {score:.0f}%，疑似匹配待人工确认")
    return sorted(result, key=lambda g: g.group_key)


def match_items(
    conn: sqlite3.Connection, project_id: int, direction: str | None = None
) -> list[MatchGroup]:
    """对项目明细做五档归组，默认也严格隔离对上、对下和未标记方向。

    同一编码或名称出现在不同方向时不得自动归入同一匹配组。显式传入
    ``direction`` 可只处理一个方向；未传入时逐方向独立运行，并把方向写进
    ``group_key``，确保保存后的人工复核记录仍可辨认范围。
    """
    if direction is not None:
        return _match_items_for_direction(conn, project_id, direction)

    directions = [
        r["direction"]
        for r in conn.execute(
            """SELECT DISTINCT COALESCE(direction, 'unknown') AS direction
               FROM settlement_periods WHERE project_id=? ORDER BY direction""",
            (project_id,),
        ).fetchall()
    ]
    if not directions:
        return []
    if len(directions) == 1:
        return _match_items_for_direction(conn, project_id, directions[0])

    result: list[MatchGroup] = []
    for current_direction in directions:
        for group in _match_items_for_direction(conn, project_id, current_direction):
            group.group_key = f"{current_direction}:{group.group_key}"
            group.notes.append(f"匹配范围：{current_direction}")
            result.append(group)
    return sorted(result, key=lambda g: g.group_key)


def save_matches(conn: sqlite3.Connection, project_id: int, groups: list[MatchGroup]) -> int:
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    with run_contract._transaction(conn, "save_matches"):
        # 当前候选是可重建缓存；人工已处理记录不删除，旧签名记录由当前
        # 读取面排除并保留作历史。
        conn.execute(
            "DELETE FROM matches WHERE project_id=? AND status='pending' AND run_signature=?",
            (project_id, active_contract.signature),
        )
        n = 0
        for g in groups:
            conn.execute(
                """INSERT INTO matches(
                       project_id, group_key, item_ids_json, level, method, score,
                       status, run_signature)
                   VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (project_id, g.group_key, json.dumps(g.item_ids), g.level, g.method,
                 g.score, active_contract.signature),
            )
            n += 1
    return n


# ---- 人工复核 API ----


def _current_match_row(
    conn: sqlite3.Connection, project_id: int, match_id: int
) -> sqlite3.Row | None:
    """只读取当前运行契约下的匹配行，禁止人工 API 触碰历史候选。"""
    scope, scope_params = run_contract.current_scope(conn, project_id, "m")
    return conn.execute(
        f"SELECT m.* FROM matches m WHERE m.id=? AND m.project_id=? AND {scope}",
        (match_id, project_id, *scope_params),
    ).fetchone()

def confirm_match(
    conn: sqlite3.Connection,
    project_id: int,
    match_id: int,
    actor: str,
    reason: str,
    alias_name: str | None = None,
) -> None:
    """确认一个匹配。可选地把它固化为项目别名映射（保存依据=reason）。"""
    # 先校验人工判断原因，再进行任何状态、别名、证据或审计写入。
    # UI 已有必填门控，核心 API 也必须保持同一边界，避免空原因导致部分写入。
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工确认匹配必须记录原因（原则 14）")
    run_contract.require_current_results_available(conn, project_id, operation="确认匹配")
    row = _current_match_row(conn, project_id, match_id)
    if not row:
        raise ValueError(f"match {match_id} not found")
    # 状态、别名、证据和审计必须同一事务提交。任何一环失败都回滚整次人工
    # 操作，避免“匹配已确认但证据/审计缺失”的不可追溯状态。
    active_signature = run_contract.current_run_signature(conn, project_id, ensure=True)
    if row["run_signature"] != active_signature:
        raise ValueError("该匹配结果已因输入或配置变化失效，请重新运行匹配后再确认")
    with run_contract._transaction(conn, "confirm_match"):
        # UI 对话框或调用方外层事务期间可能发生运行级失效；实际写入前
        # 必须再次检查可用性、当前 scope 和签名，不能复用入口时的快照。
        run_contract.require_current_results_available(conn, project_id, operation="确认匹配")
        live_row = _current_match_row(conn, project_id, match_id)
        if not live_row or live_row["run_signature"] != active_signature:
            raise run_contract.CurrentResultsUnavailableError(
                "确认匹配不可用：匹配结果已不在当前运行范围，请刷新后重试。"
            )
        item_ids = json.loads(live_row["item_ids_json"])
        if not item_ids:
            raise ValueError(f"match {match_id} has no line items")
        placeholders = ",".join("?" for _ in item_ids)
        directions = {
            r["direction"] or "unknown"
            for r in conn.execute(
                f"""SELECT DISTINCT COALESCE(sp.direction, 'unknown') AS direction
                    FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                    WHERE li.id IN ({placeholders})""",  # noqa: S608 - placeholders only
                item_ids,
            ).fetchall()
        }
        if len(directions) != 1:
            raise ValueError(
                f"match {match_id} spans {sorted(directions)}; direction-isolated confirmation required"
            )
        direction = next(iter(directions))
        cur = conn.execute(
            """UPDATE matches SET status='confirmed', reviewed_by=?, review_note=?, level=?
               WHERE id=? AND project_id=? AND run_signature=?""",
            (actor, reason, CONFIRMED, match_id, project_id, active_signature),
        )
        if cur.rowcount != 1:
            raise run_contract.CurrentResultsUnavailableError(
                "确认匹配不可用：匹配结果已发生变化，请刷新后重试。"
            )
        if alias_name and live_row["level"] == SUSPECTED:
            # 疑似 → 确认后可沉淀为别名（原则 9/14：保存依据与确认记录）
            canonical = _unscoped_group_key(live_row["group_key"])
            conn.execute(
                """INSERT OR IGNORE INTO item_aliases(project_id, direction, canonical_key,
                   alias_text, mapping_basis, confirmed_by, confirmed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (project_id, direction, canonical, alias_name,
                 f"人工确认 match#{match_id}: {reason}",
                 actor, datetime.now().isoformat(timespec="seconds")),
            )
        evidence_api.add_evidence(
            conn, project_id, "match_confirmation",
            f"匹配组 {live_row['group_key']} 由 {actor} 确认为{CONFIRMED}：{reason}",
            steps=[{"step": "人工复核", "actor": actor, "reason": reason}],
            sources=[{"match_id": match_id, "direction": direction, "items": item_ids}],
            commit=False,
            run_signature=active_signature,
        )
        audit_log.record_audit(
            conn, project_id, actor, "confirm_match", f"match:{match_id}",
            {"level": live_row["level"]},
            {"level": CONFIRMED, "alias": alias_name, "direction": direction}, reason,
            commit=False,
        )
    # 别名是匹配配置的一部分。确认后立即生成新契约，使后续运行不能误用
    # 变更前的候选；本次人工记录仍保留在旧签名历史中。
    if alias_name and live_row["level"] == SUSPECTED:
        run_contract.ensure_run_contract(conn, project_id)


def override_match(
    conn: sqlite3.Connection,
    project_id: int,
    match_id: int,
    new_level: str,
    actor: str,
    reason: str,
) -> None:
    """用户随时可修正 AI 的匹配结果（原则 14）。"""
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工修正匹配必须记录原因（原则 14）")
    if new_level not in LEVEL_SCORES:
        raise ValueError(f"invalid level: {new_level}")
    run_contract.require_current_results_available(conn, project_id, operation="修正匹配")
    row = _current_match_row(conn, project_id, match_id)
    if not row:
        raise ValueError(f"match {match_id} not found")
    # 同确认操作：业务状态、证据和人工审计必须全成或全回滚。
    active_signature = run_contract.current_run_signature(conn, project_id, ensure=True)
    if row["run_signature"] != active_signature:
        raise ValueError("该匹配结果已因输入或配置变化失效，请重新运行匹配后再修正")
    with run_contract._transaction(conn, "override_match"):
        run_contract.require_current_results_available(conn, project_id, operation="修正匹配")
        live_row = _current_match_row(conn, project_id, match_id)
        if not live_row or live_row["run_signature"] != active_signature:
            raise run_contract.CurrentResultsUnavailableError(
                "修正匹配不可用：匹配结果已不在当前运行范围，请刷新后重试。"
            )
        cur = conn.execute(
            """UPDATE matches SET level=?, status='reviewed', reviewed_by=?, review_note=?
               WHERE id=? AND project_id=? AND run_signature=?""",
            (new_level, actor, reason, match_id, project_id, active_signature),
        )
        if cur.rowcount != 1:
            raise run_contract.CurrentResultsUnavailableError(
                "修正匹配不可用：匹配结果已发生变化，请刷新后重试。"
            )
        evidence_api.add_evidence(
            conn, project_id, "match_override",
            f"匹配组 #{match_id} 级别 {live_row['level']} → {new_level}（{actor}）",
            steps=[{"step": "人工修正", "reason": reason}],
            sources=[{"match_id": match_id}],
            commit=False,
            run_signature=active_signature,
        )
        audit_log.record_audit(
            conn, project_id, actor, "override_match", f"match:{match_id}",
            {"level": live_row["level"]}, {"level": new_level}, reason,
            commit=False,
        )
