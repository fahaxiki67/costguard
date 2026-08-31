"""匹配人工复核队列的 P2 排序试验。

这里仅按当前批次已有的置信档位和调用方提供的影响优先级排序，不训练模型、
不跨项目复用人工标签，也不改变 ``MatchGroup.level`` 或自动确认任何匹配。
排序结果必须仍由人工逐组复核。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from jiadun.core.matching.matching import MatchGroup

D = Decimal

# 数值越大表示越需要人工先看；同一档位内再按影响金额降序，金额缺失按未知
# 处理并排在同档位的已知影响之后，不把缺失金额伪装为 0。
UNCERTAINTY_PRIORITY = {
    "pending_data": 4,
    "incomparable": 4,
    "suspected": 3,
    "probable": 2,
    "confirmed": 1,
}


@dataclass(frozen=True)
class ReviewCandidate:
    """带有排序依据的只读候选，不包含确认或处置动作。"""

    group: MatchGroup
    uncertainty_priority: int
    impact_amount: D | None
    impact_known: bool

    @property
    def sort_key(self) -> tuple[int, int, Decimal, str]:
        # 未知影响金额固定排在同优先级的已知金额之后；不对未知值补零。
        return (
            -self.uncertainty_priority,
            0 if self.impact_known else 1,
            -(self.impact_amount or D(0)),
            self.group.group_key,
        )


def _impact(value: object) -> tuple[D | None, bool]:
    if value is None or value == "":
        return None, False
    try:
        return D(str(value)), True
    except Exception:
        return None, False


def rank_review_candidates(
    groups: list[MatchGroup] | tuple[MatchGroup, ...],
    *,
    impact_by_group: Mapping[str, object] | None = None,
    limit: int | None = None,
) -> list[ReviewCandidate]:
    """按不确定性和已知影响排序当前项目的匹配候选。

    ``impact_by_group`` 只接受调用方已核实或已明确标记的影响值；本函数不从
    数据库重新计算金额，也不把排序结果写入 ``matches``。未知或不可解析值
    保持未知状态。``limit`` 只截取展示队列，不改变候选全集。
    """
    if limit is not None and limit < 0:
        raise ValueError("limit 不能为负数")
    impacts = impact_by_group or {}
    candidates = []
    for group in groups:
        amount, known = _impact(impacts.get(group.group_key))
        candidates.append(ReviewCandidate(
            group=group,
            uncertainty_priority=UNCERTAINTY_PRIORITY.get(group.level, 4),
            impact_amount=amount,
            impact_known=known,
        ))
    candidates.sort(key=lambda candidate: candidate.sort_key)
    return candidates if limit is None else candidates[:limit]


__all__ = ["ReviewCandidate", "UNCERTAINTY_PRIORITY", "rank_review_candidates"]
