"""清单匹配引擎（Phase 5 核心）。

五档置信度（最高优先级原则 9）：
  confirmed   确认匹配：编码一致且名称归一化一致，或命中用户别名库
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
from costguard.core.evidence import audit as audit_log
from costguard.core.evidence import evidence as evidence_api

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


def load_aliases(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    """alias_text(归一化) -> canonical_key"""
    rows = conn.execute(
        "SELECT alias_text, canonical_key FROM item_aliases WHERE project_id=?", (project_id,)
    ).fetchall()
    return {normalize_name(r["alias_text"]): r["canonical_key"] for r in rows}


def _canonical_of(code: str, name: str) -> str:
    return f"code:{code}" if code else f"name:{name}"


def match_items(conn: sqlite3.Connection, project_id: int) -> list[MatchGroup]:
    """对项目全部明细行做五档归组（不修改行数据，只产出匹配建议）。"""
    rows = conn.execute(
        """SELECT li.id, li.code, li.name, li.unit, sp.period_no AS pno FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? ORDER BY li.id""",
        (project_id,),
    ).fetchall()
    aliases = load_aliases(conn, project_id)

    # ---- 逐行归到候选键 ----
    exact: dict[str, list[int]] = {}  # canonical_key -> ids（编码精确）
    name_exact: dict[str, list[int]] = {}
    for r in rows:
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


def save_matches(conn: sqlite3.Connection, project_id: int, groups: list[MatchGroup]) -> int:
    with conn:
        conn.execute("DELETE FROM matches WHERE project_id=? AND status='pending'", (project_id,))
        n = 0
        for g in groups:
            conn.execute(
                """INSERT INTO matches(project_id, group_key, item_ids_json, level, method, score, status)
                   VALUES (?,?,?,?,?,?,'pending')""",
                (project_id, g.group_key, json.dumps(g.item_ids), g.level, g.method, g.score),
            )
            n += 1
    return n


# ---- 人工复核 API ----

def confirm_match(
    conn: sqlite3.Connection,
    project_id: int,
    match_id: int,
    actor: str,
    reason: str,
    alias_name: str | None = None,
) -> None:
    """确认一个匹配。可选地把它固化为项目别名映射（保存依据=reason）。"""
    row = conn.execute("SELECT * FROM matches WHERE id=? AND project_id=?", (match_id, project_id)).fetchone()
    if not row:
        raise ValueError(f"match {match_id} not found")
    with conn:
        conn.execute(
            "UPDATE matches SET status='confirmed', reviewed_by=?, review_note=?, level=? WHERE id=?",
            (actor, reason, CONFIRMED, match_id),
        )
    if alias_name and row["level"] == SUSPECTED:
        # 疑似 → 确认后可沉淀为别名（原则 9/14：保存依据与确认记录）
        canonical = row["group_key"]
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO item_aliases(project_id, canonical_key, alias_text, mapping_basis,
                   confirmed_by, confirmed_at) VALUES (?,?,?,?,?,?)""",
                (project_id, canonical, alias_name, f"人工确认 match#{match_id}: {reason}",
                 actor, datetime.now().isoformat(timespec="seconds")),
            )
    evidence_api.add_evidence(
        conn, project_id, "match_confirmation",
        f"匹配组 {row['group_key']} 由 {actor} 确认为{CONFIRMED}：{reason}",
        steps=[{"step": "人工复核", "actor": actor, "reason": reason}],
        sources=[{"match_id": match_id, "items": json.loads(row["item_ids_json"])}],
    )
    audit_log.record_audit(
        conn, project_id, actor, "confirm_match", f"match:{match_id}",
        {"level": row["level"]}, {"level": CONFIRMED, "alias": alias_name}, reason,
    )


def override_match(
    conn: sqlite3.Connection,
    project_id: int,
    match_id: int,
    new_level: str,
    actor: str,
    reason: str,
) -> None:
    """用户随时可修正 AI 的匹配结果（原则 14）。"""
    if new_level not in LEVEL_SCORES:
        raise ValueError(f"invalid level: {new_level}")
    row = conn.execute("SELECT level FROM matches WHERE id=? AND project_id=?", (match_id, project_id)).fetchone()
    if not row:
        raise ValueError(f"match {match_id} not found")
    with conn:
        conn.execute("UPDATE matches SET level=?, status='reviewed', reviewed_by=?, review_note=? WHERE id=?",
                     (new_level, actor, reason, match_id))
    evidence_api.add_evidence(
        conn, project_id, "match_override",
        f"匹配组 #{match_id} 级别 {row['level']} → {new_level}（{actor}）",
        steps=[{"step": "人工修正", "reason": reason}],
        sources=[{"match_id": match_id}],
    )
    audit_log.record_audit(
        conn, project_id, actor, "override_match", f"match:{match_id}",
        {"level": row["level"]}, {"level": new_level}, reason,
    )
