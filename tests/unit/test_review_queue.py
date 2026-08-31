"""匹配复核队列排序试验测试。"""

from decimal import Decimal

from jiadun.core.matching.matching import (
    CONFIRMED,
    INCOMPARABLE,
    PROBABLE,
    SUSPECTED,
    MatchGroup,
)
from jiadun.core.matching.review_queue import rank_review_candidates


def _group(key: str, level: str) -> MatchGroup:
    return MatchGroup(group_key=key, level=level, method="test", score=0.5)


def test_review_queue_prioritizes_uncertainty_then_known_impact_without_confirmation():
    groups = [
        _group("confirmed", CONFIRMED),
        _group("probable-big", PROBABLE),
        _group("suspected-small", SUSPECTED),
        _group("incomparable", INCOMPARABLE),
        _group("suspected-big", SUSPECTED),
    ]
    ranked = rank_review_candidates(
        groups,
        impact_by_group={"probable-big": "900", "suspected-big": Decimal("100")},
    )
    assert [candidate.group.group_key for candidate in ranked] == [
        "incomparable", "suspected-big", "suspected-small", "probable-big", "confirmed",
    ]
    assert all(candidate.group.level != "confirmed" or candidate.group.group_key == "confirmed"
               for candidate in ranked)
    assert ranked[1].impact_amount == Decimal("100")


def test_review_queue_keeps_unknown_impact_unknown_and_supports_display_limit():
    groups = [_group("known", PROBABLE), _group("unknown", PROBABLE)]
    ranked = rank_review_candidates(
        groups,
        impact_by_group={"known": "12.50", "unknown": None},
        limit=1,
    )
    assert len(ranked) == 1
    assert ranked[0].group.group_key == "known"
    full = rank_review_candidates(groups, impact_by_group={"unknown": "bad"})
    assert full[-1].impact_amount is None
    assert full[-1].impact_known is False
