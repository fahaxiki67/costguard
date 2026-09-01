"""异常规则目录与项目级启停配置。

规则函数仍然是确定性执行单元；本模块只描述规则的适用场景、证据要求和
配置元数据。启停变更写入项目库并进入下一次 Run Contract，不能静默影响
当前运行，也不能把关闭规则解释为“未发现问题”。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jiadun.core.anomalies import rules as rule_module
from jiadun.core.contracts import run_contract
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api

RULE_CONFIG_VERSION = "rule-catalog-v1"
RULE_SCENARIO_ZH = {
    "general": "通用",
    "upward": "对上",
    "downward": "对下",
    "subcontract": "分包",
    "material": "材料",
    "process_measurement": "过程计量",
    "final_settlement": "最终结算",
    "contract_compliance": "合同合规",
}


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    name_zh: str
    scenario: str
    severity: str
    trigger_condition: str
    evidence_requirements: str
    impact_algorithm: str
    limitations: str
    suggested_review: str
    allow_disable: bool = True
    version: str = RULE_CONFIG_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name_zh": self.name_zh,
            "scenario": self.scenario,
            "scenario_zh": RULE_SCENARIO_ZH.get(self.scenario, self.scenario),
            "severity": self.severity,
            "trigger_condition": self.trigger_condition,
            "evidence_requirements": self.evidence_requirements,
            "impact_algorithm": self.impact_algorithm,
            "limitations": self.limitations,
            "suggested_review": self.suggested_review,
            "allow_disable": self.allow_disable,
            "version": self.version,
        }


_NAMES = {
    "rule_qty_price_amount": "工程量×单价与合价核对",
    "rule_negative_qty": "负数工程量",
    "rule_round_amounts": "大额整数金额",
    "rule_unparsed_numbers": "数值无法解析",
    "rule_orphan_rows": "无名称数值行",
    "rule_subtotal_vs_details": "小计与明细汇总差异",
    "rule_duplicates": "重复明细",
    "rule_same_code_diff_name": "同编码不同名称",
    "rule_same_name_diff_code": "同名称不同编码",
    "rule_unit_changed": "计量单位变化",
    "rule_price_changed": "单价变化",
    "rule_tax_changed": "税率变化",
    "rule_tax_mode_mixed": "计税口径混用",
    "rule_qty_spike": "工程量突变",
    "rule_suspected_duplicate_settlement": "疑似重复结算",
    "rule_hidden_cells": "隐藏行列",
    "rule_formula_issues": "公式或缓存异常",
    "rule_text_numbers_in_value_cols": "数值列文本数字",
    "rule_merged_cells_in_data": "数据区合并单元格",
    "rule_missing_key_fields": "关键字段缺失",
    "rule_needs_review_headers": "表头待人工复核",
}

_SCENARIOS = {
    "rule_qty_price_amount": "general",
    "rule_negative_qty": "general",
    "rule_round_amounts": "final_settlement",
    "rule_unparsed_numbers": "general",
    "rule_orphan_rows": "general",
    "rule_subtotal_vs_details": "final_settlement",
    "rule_duplicates": "general",
    "rule_same_code_diff_name": "general",
    "rule_same_name_diff_code": "general",
    "rule_unit_changed": "process_measurement",
    "rule_price_changed": "general",
    "rule_tax_changed": "contract_compliance",
    "rule_tax_mode_mixed": "contract_compliance",
    "rule_qty_spike": "process_measurement",
    "rule_suspected_duplicate_settlement": "final_settlement",
    "rule_hidden_cells": "general",
    "rule_formula_issues": "general",
    "rule_text_numbers_in_value_cols": "general",
    "rule_merged_cells_in_data": "general",
    "rule_missing_key_fields": "general",
    "rule_needs_review_headers": "general",
}

_SEVERITIES = {
    "rule_qty_price_amount": "high",
    "rule_negative_qty": "high",
    "rule_round_amounts": "low",
    "rule_unparsed_numbers": "high",
    "rule_orphan_rows": "medium",
    "rule_subtotal_vs_details": "high",
    "rule_duplicates": "high",
    "rule_same_code_diff_name": "medium",
    "rule_same_name_diff_code": "medium",
    "rule_unit_changed": "medium",
    "rule_price_changed": "medium",
    "rule_tax_changed": "medium",
    "rule_tax_mode_mixed": "high",
    "rule_qty_spike": "medium",
    "rule_suspected_duplicate_settlement": "high",
    "rule_hidden_cells": "medium",
    "rule_formula_issues": "high",
    "rule_text_numbers_in_value_cols": "medium",
    "rule_merged_cells_in_data": "medium",
    "rule_missing_key_fields": "medium",
    "rule_needs_review_headers": "medium",
}


def _definition(rule_id: str) -> RuleDefinition:
    name = _NAMES.get(rule_id, rule_id)
    severity = _SEVERITIES.get(rule_id, "medium")
    scenario = _SCENARIOS.get(rule_id, "general")
    return RuleDefinition(
        rule_id=rule_id,
        name_zh=name,
        scenario=scenario,
        severity=severity,
        trigger_condition=f"确定性规则 {rule_id} 返回至少一项 Finding",
        evidence_requirements="必须保留原始文件、Sheet、行列或期次来源及计算过程",
        impact_algorithm="仅统计规则自身可复算的影响；缺失值不补零，不自动调平",
        limitations="规则发现不等于业务责任或正式结算结论，需人工回查原始证据",
        suggested_review="回查原始 Sheet、字段映射、数量、单价、金额和适用口径",
        allow_disable=rule_id != "rule_needs_review_headers",
    )


RULE_CATALOG: dict[str, RuleDefinition] = {
    function.__name__: _definition(function.__name__)
    for function in rule_module.ALL_RULES
}


def catalog_entries() -> list[RuleDefinition]:
    return [RULE_CATALOG[function.__name__] for function in rule_module.ALL_RULES]


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return data


def current_configurations(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, dict[str, Any]]:
    """读取每条规则最新项目配置；未配置项按目录默认启用。"""
    result = {
        definition.rule_id: {
            **definition.as_dict(),
            "enabled": True,
            "config_version": 0,
            "disabled_reason": None,
            "configured_by": None,
            "configured_at": None,
        }
        for definition in catalog_entries()
    }
    try:
        rows = conn.execute(
            """SELECT c.* FROM rule_configurations c
               JOIN (
                   SELECT rule_id, MAX(config_version) AS version
                   FROM rule_configurations WHERE project_id=? GROUP BY rule_id
               ) latest ON latest.rule_id=c.rule_id AND latest.version=c.config_version
               WHERE c.project_id=?""",
            (int(project_id), int(project_id)),
        ).fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        definition = RULE_CATALOG.get(row["rule_id"])
        if definition is None:
            continue
        result[row["rule_id"]] = {
            **definition.as_dict(),
            "enabled": bool(row["enabled"]),
            "config_version": int(row["config_version"]),
            "disabled_reason": row["disabled_reason"],
            "configured_by": row["actor"],
            "configured_at": row["created_at"],
            "configuration_id": int(row["id"]),
            "evidence_id": row["evidence_id"],
        }
    return result


def rule_config_snapshot(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    configurations = current_configurations(conn, project_id)
    ordered = [configurations[definition.rule_id] for definition in catalog_entries()]
    return {
        "version": RULE_CONFIG_VERSION,
        "rule_ids": [item["rule_id"] for item in ordered],
        "enabled_rule_ids": [item["rule_id"] for item in ordered if item["enabled"]],
        "disabled_rule_ids": [item["rule_id"] for item in ordered if not item["enabled"]],
        "configurations": ordered,
    }


def enabled_rule_functions(
    conn: sqlite3.Connection, project_id: int
) -> list[Callable]:
    configurations = current_configurations(conn, project_id)
    return [
        function for function in rule_module.ALL_RULES
        if configurations[function.__name__]["enabled"]
    ]


def set_rule_enabled(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: str,
    enabled: bool,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """追加一版规则配置并让配置变化进入新的 Run Contract。"""
    definition = RULE_CATALOG.get(str(rule_id))
    if definition is None:
        raise ValueError(f"未知规则: {rule_id!r}")
    if not str(actor).strip():
        raise ValueError("规则配置必须记录操作人")
    if not str(reason).strip():
        raise audit_log.AuditReasonRequiredError("规则配置变更必须记录原因")
    if not enabled and not definition.allow_disable:
        raise ValueError(f"规则「{definition.name_zh}」不允许关闭，以保持安全闸门")
    current_state = current_configurations(conn, project_id)[definition.rule_id]
    enabled = bool(enabled)
    if current_state["enabled"] == enabled:
        raise ValueError("规则已经处于指定启停状态，无需重复配置")
    old_contract = run_contract.get_current_contract(conn, project_id)
    version_row = conn.execute(
        "SELECT COALESCE(MAX(config_version), 0) AS v FROM rule_configurations "
        "WHERE project_id=? AND rule_id=?",
        (int(project_id), definition.rule_id),
    ).fetchone()
    version = int(version_row["v"] or 0) + 1
    now = datetime.now().isoformat(timespec="seconds")
    config_payload = {
        **definition.as_dict(),
        "enabled": enabled,
        "config_version": version,
        "disabled_reason": None if enabled else str(reason).strip(),
        "configured_by": str(actor).strip(),
        "configured_at": now,
    }
    with run_contract._transaction(conn, "set_rule_enabled"):
        # 先写配置变更审计，再生成新合同；审计快照本身属于 Run Contract
        # 的人工确认输入，确保新合同包含此次操作，而不会在下一次调用时
        # 因审计刚写入又无声地产生第三个运行。
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            str(actor).strip(),
            "set_rule_enabled",
            f"rule:{definition.rule_id}",
            {
                "enabled": current_state["enabled"],
                "config_version": current_state["config_version"],
                "run_id": old_contract.run_id if old_contract else None,
            },
            {
                "enabled": enabled,
                "config_version": version,
                "rule": definition.as_dict(),
            },
            reason,
            commit=False,
            run_id=old_contract.run_id if old_contract else None,
            run_signature=old_contract.signature if old_contract else None,
        )
        conn.execute(
            """INSERT INTO rule_configurations(
                   project_id, rule_id, enabled, config_version,
                   category, name_zh, scope, severity,
                   trigger_condition, evidence_requirements, impact_algorithm,
                   limitations, suggested_review, allow_disable, version,
                   disabled_reason, actor, created_at, metadata_json, audit_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), definition.rule_id, int(enabled), version,
                definition.scenario, definition.name_zh, "project", definition.severity,
                definition.trigger_condition, definition.evidence_requirements,
                definition.impact_algorithm, definition.limitations,
                definition.suggested_review, int(definition.allow_disable), definition.version,
                config_payload["disabled_reason"], str(actor).strip(), now,
                json.dumps(config_payload, ensure_ascii=False, sort_keys=True), audit_id,
            ),
        )
        current_contract = run_contract.ensure_run_contract(conn, project_id)
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "rule_configuration",
            f"规则「{definition.name_zh}」已{'启用' if enabled else '停用'}，配置 v{version}",
            steps=[{
                "step": "人工修改规则配置",
                "rule_id": definition.rule_id,
                "enabled": enabled,
                "config_version": version,
                "reason": str(reason).strip(),
            }],
            sources=[{
                "rule_id": definition.rule_id,
                "catalog_version": definition.version,
                "audit_id": audit_id,
            }],
            commit=False,
            run_signature=current_contract.signature,
            run_id=current_contract.run_id,
            scope="human",
        )
        # 审计内容已经在生成新合同前写入，最终合同确定后再绑定运行身份；
        # ``human_confirmation_snapshot`` 不包含这两个定位字段，因此不会
        # 产生“绑定一次又自我失效”的循环。
        conn.execute(
            """UPDATE audit_log SET run_id=?, run_signature=?
               WHERE id=? AND project_id=?""",
            (current_contract.run_id, current_contract.signature, audit_id, project_id),
        )
    config_payload["configuration_id"] = int(conn.execute(
        "SELECT id FROM rule_configurations WHERE project_id=? AND rule_id=? AND config_version=?",
        (int(project_id), definition.rule_id, version),
    ).fetchone()[0])
    config_payload["evidence_id"] = evidence_id
    config_payload["audit_id"] = audit_id
    config_payload["run_id"] = current_contract.run_id
    config_payload["run_signature"] = current_contract.signature
    return config_payload
