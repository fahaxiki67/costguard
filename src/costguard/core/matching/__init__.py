"""匹配相关的候选生成与完整性门控辅助 API。"""

from costguard.core.matching.key_integrity import (
    BLOCKED_BY_DUPLICATE,
    DEFAULT_RULE_VERSION,
    DUPLICATE,
    EMPTY,
    KEY_RULE_VERSION,
    LEFT_ONLY,
    RIGHT_ONLY,
    CompositeKeyRules,
    KeyIntegrityResult,
    KeyIntegrityRules,
    KeyNormalizationRules,
    classify_composite_keys,
    normalize_composite_key,
    normalize_key_value,
)
from costguard.core.matching.review_queue import (
    UNCERTAINTY_PRIORITY,
    ReviewCandidate,
    rank_review_candidates,
)

__all__ = [
    "BLOCKED_BY_DUPLICATE",
    "CompositeKeyRules",
    "DEFAULT_RULE_VERSION",
    "DUPLICATE",
    "EMPTY",
    "KEY_RULE_VERSION",
    "LEFT_ONLY",
    "RIGHT_ONLY",
    "KeyIntegrityResult",
    "KeyIntegrityRules",
    "KeyNormalizationRules",
    "classify_composite_keys",
    "normalize_composite_key",
    "normalize_key_value",
    "ReviewCandidate",
    "UNCERTAINTY_PRIORITY",
    "rank_review_candidates",
]
