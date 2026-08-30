"""统一的 Finding 数据结构与稳定序列化工具。

异常规则只负责发现问题；本模块负责把规则输出整理成可重跑、可追溯的
Finding。``finding_id``/``fingerprint`` 不包含时间戳，因此同一输入重复运行
不会因为生成时间变化而产生无法归并的对象。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=lambda item: str(item))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """返回跨运行稳定的 JSON；Decimal 以字符串保存，避免浮点漂移。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _raw_values(details: dict[str, Any]) -> dict[str, Any]:
    explicit = details.get("raw_values")
    if isinstance(explicit, dict):
        return explicit
    selected = {
        key: value
        for key, value in details.items()
        if key.startswith("raw_") or key in {"value", "actual", "old", "new", "qty", "price", "amount"}
    }
    # 即使某条旧规则尚未显式拆出 raw 字段，也不能丢掉它的观察依据。
    return selected or {"details": details}


@dataclass
class Finding:
    """规则发现的统一对象。

    前六个字段保留旧规则的构造顺序；扩展字段全部有默认值，保证旧规则和
    外部脚本仍可用 ``Finding(rule, severity, subject_type, subject_id, message,
    details)`` 创建对象。
    """

    rule_id: str
    severity: str
    subject_type: str
    subject_id: int
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    finding_id: str | None = None
    fingerprint: str | None = None
    confidence: str = "medium"
    detection_mode: str = "automated"
    raw_values: dict[str, Any] = field(default_factory=dict)
    normalized_values: dict[str, Any] = field(default_factory=dict)
    impact: str = ""
    limitations: list[str] = field(default_factory=list)
    recommendation: str = ""
    suppression_reason: str | None = None

    def __post_init__(self) -> None:
        self.details = dict(self.details or {})
        if not self.raw_values:
            self.raw_values = _raw_values(self.details)
        if not self.normalized_values:
            self.normalized_values = {
                "rule_id": self.rule_id,
                "subject_type": self.subject_type,
                "subject_id": self.subject_id,
            }
        if self.confidence == "medium" and self.details.get("confidence") is not None:
            self.confidence = str(self.details["confidence"])
        elif not self.confidence:
            self.confidence = str(self.details.get("confidence") or "medium")
        if self.detection_mode == "automated" and self.details.get("detection_mode") is not None:
            self.detection_mode = str(self.details["detection_mode"])
        elif not self.detection_mode:
            self.detection_mode = str(self.details.get("detection_mode") or "automated")
        if not self.impact:
            self.impact = str(
                self.details.get("impact")
                or {
                    "high": "可能影响金额正确性或审核结论",
                    "medium": "需要人工复核，暂不形成确定性结论",
                    "low": "提示潜在数据质量或口径问题",
                    "info": "提供补充审核线索",
                }.get(self.severity, "需要人工复核")
            )
        if not self.limitations:
            listed = self.details.get("limitations")
            self.limitations = list(listed) if isinstance(listed, list) else [
                "自动发现不替代人工复核；需回查原始文件、工作表、行列和原始值"
            ]
        if not self.recommendation:
            self.recommendation = str(
                self.details.get("recommendation")
                or f"按规则 {self.rule_id} 回查来源证据并记录处理结果"
            )
        if self.suppression_reason is None and self.details.get("suppression_reason"):
            self.suppression_reason = str(self.details["suppression_reason"])

        payload = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "message": self.message,
            "details": self.details,
            "raw_values": self.raw_values,
            "normalized_values": self.normalized_values,
            "confidence": self.confidence,
            "detection_mode": self.detection_mode,
        }
        self.fingerprint = self.fingerprint or stable_fingerprint(payload)
        self.finding_id = self.finding_id or f"finding:{self.fingerprint[:24]}"

    def as_record(self) -> dict[str, Any]:
        """给数据库/导出层使用的稳定字段快照。"""
        return {
            "finding_id": self.finding_id,
            "fingerprint": self.fingerprint,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "message": self.message,
            "details": self.details,
            "confidence": self.confidence,
            "detection_mode": self.detection_mode,
            "raw_values": self.raw_values,
            "normalized_values": self.normalized_values,
            "impact": self.impact,
            "limitations": self.limitations,
            "recommendation": self.recommendation,
            "suppression_reason": self.suppression_reason,
        }
