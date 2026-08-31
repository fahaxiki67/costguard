"""匹配前复合键完整性检查。

本模块只做确定性的键标准化、完整性分类和安全候选索引，不执行模糊匹配，
也不把左右记录连接成笛卡尔积。调用方可以先用结果中的分类门控匹配流程，
再把 ``matched_pairs`` 交给后续的一对一精确匹配。

规则边界：

* 字符串键默认只做首尾 ``trim``，不改变大小写和内部字符；
* ``None``、空字符串、全空格字符串、NaN 和规则声明的业务缺失标记均为缺失；
* 复合键任一组成字段缺失，整行进入空键类别；
* 同一侧重复键的全部原始记录进入重复类别，不参与安全匹配；
* 仅当某键在左右两侧均恰好出现一次时，才产生一对安全精确候选。

输入记录不会被修改，返回的分类记录保留输入记录的全部字段。因此，调用方
只要在输入中带上 ``source_file_id``、``source_sheet``、``source_row``、
``source_cell``、``id`` 等字段，分类结果就会原样保留这些来源定位信息。
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from math import isnan
from numbers import Real
from typing import Any

# 规则版本是结果可追溯的一部分。新增或改变缺失标记、标准化方式时，应使用
# 新版本，而不是静默改变同一版本的含义。
DEFAULT_RULE_VERSION = "composite-key-v1"
KEY_RULE_VERSION = DEFAULT_RULE_VERSION

EMPTY = "empty"
DUPLICATE = "duplicate"
LEFT_ONLY = "left_only"
RIGHT_ONLY = "right_only"
BLOCKED_BY_DUPLICATE = "blocked_by_duplicate"

type RowLike = Any
type CompositeKey = tuple[Any, ...]


@dataclass(frozen=True)
class KeyNormalizationRules:
    """复合键标准化规则。

    ``missing_tokens`` 用于声明本业务版本的缺失文本，例如 ``"待补资料"``。
    默认不把任何普通文本（除空字符串外）当作缺失，避免把合法业务键误判为
    缺失。``missing_values`` 适用于少量非字符串哨兵值；``None`` 始终缺失。
    """

    version: str = DEFAULT_RULE_VERSION
    trim: bool = True
    missing_tokens: tuple[str, ...] = ()
    missing_values: tuple[Any, ...] = ()
    strip_chars: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("复合键规则必须有非空 version")
        if self.strip_chars is not None and not isinstance(self.strip_chars, str):
            raise TypeError("strip_chars 必须是字符串或 None")

        tokens = self.missing_tokens
        if isinstance(tokens, str):
            tokens = (tokens,)
        else:
            tokens = tuple(tokens)
        if any(not isinstance(token, str) for token in tokens):
            raise TypeError("missing_tokens 只能包含字符串")
        if self.trim:
            tokens = tuple(self._trim_text(token) for token in tokens)
        object.__setattr__(self, "missing_tokens", tokens)

        values = self.missing_values
        if isinstance(values, tuple):
            values = tuple(values)
        else:
            values = tuple(values)
        object.__setattr__(self, "missing_values", values)

    @property
    def trim_strings(self) -> bool:
        """``trim`` 的可读别名，便于调用方显式表达规则含义。"""

        return self.trim

    @property
    def missing_markers(self) -> tuple[str, ...]:
        """业务缺失文本的可读别名。"""

        return self.missing_tokens

    def _trim_text(self, value: str) -> str:
        if self.strip_chars is None:
            return value.strip()
        return value.strip(self.strip_chars)

    def normalize(self, value: Any) -> Any | None:
        """按版本化规则标准化一个键组成字段。

        返回 ``None`` 表示缺失。数值 ``0``、布尔 ``False`` 和其他合法非空值
        不会被当作缺失；NaN 则明确按缺失处理。
        """

        if _is_missing_scalar(value):
            return None

        if isinstance(value, str):
            normalized = self._trim_text(value) if self.trim else value
            # 空字符串无论规则是否启用 trim，均为缺失。trim=False 只保留
            # 非空字符串的原始首尾空格，不改变空值定义。
            if not normalized or normalized in self.missing_tokens:
                return None
            return normalized

        if _equals_any(value, self.missing_values):
            return None
        return value


# 语义相同的别名，降低主代理接入时的命名适配成本。
CompositeKeyRules = KeyNormalizationRules
KeyIntegrityRules = KeyNormalizationRules


def _is_missing_scalar(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return False
    if isinstance(value, Real):
        try:
            if isnan(value):
                return True
        except (TypeError, ValueError):
            pass

    # Decimal('NaN') 及少量第三方标量提供 is_nan()；不依赖第三方库，按能力
    # 探测即可。pandas.NA/NaT 也通过类型名兜底为缺失，不把它们转换成字符串。
    is_nan_method = getattr(value, "is_nan", None)
    if callable(is_nan_method):
        try:
            if is_nan_method():
                return True
        except (TypeError, ValueError):
            pass
    if type(value).__name__ in {"NAType", "NaTType"}:
        return True
    return False


def _equals_any(value: Any, candidates: Sequence[Any]) -> bool:
    for candidate in candidates:
        try:
            equal = value == candidate
        except (TypeError, ValueError):
            continue
        if isinstance(equal, bool) and equal:
            return True
    return False


def _coerce_rules(
    rules: KeyNormalizationRules | None,
    rule_version: str | None,
) -> KeyNormalizationRules:
    if rules is not None and rule_version is not None:
        raise ValueError("rules 与 rule_version 不能同时指定")
    if rules is not None:
        if not isinstance(rules, KeyNormalizationRules):
            raise TypeError("rules 必须是 KeyNormalizationRules")
        return rules
    return KeyNormalizationRules(version=rule_version or DEFAULT_RULE_VERSION)


def normalize_key_value(
    value: Any,
    rules: KeyNormalizationRules | None = None,
) -> Any | None:
    """公开的单字段标准化函数；不修改输入值。"""

    return (rules or KeyNormalizationRules()).normalize(value)


def _validate_keys(keys: Iterable[str]) -> tuple[str, ...]:
    if isinstance(keys, str):
        raise ValueError("keys 必须是包含至少一个字段名的序列，而不是单个字符串")
    key_tuple = tuple(keys)
    if not key_tuple:
        raise ValueError("keys 至少一个字段")
    if any(not isinstance(key, str) or not key for key in key_tuple):
        raise ValueError("keys 中的字段名必须是非空字符串")
    if len(key_tuple) != len(set(key_tuple)):
        raise ValueError("keys 中的字段名不得重复")
    return key_tuple


def _row_columns(row: RowLike) -> set[str]:
    if isinstance(row, Mapping):
        return {str(key) for key in row.keys()}
    keys_method = getattr(row, "keys", None)
    if callable(keys_method):
        return {str(key) for key in keys_method()}
    try:
        return {str(key) for key in vars(row)}
    except TypeError:
        return set()


def _row_value(row: RowLike, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return getattr(row, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


def _materialize_rows(rows: Iterable[RowLike] | RowLike | None) -> list[RowLike]:
    if rows is None:
        return []
    if isinstance(rows, Mapping):
        return [rows]

    # 兼容 pandas DataFrame 等提供 to_dict(orient="records") 的表格对象，但
    # 模块本身不导入 pandas，也不把 DataFrame 连接或复制成笛卡尔积。
    to_dict = getattr(rows, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except (TypeError, ValueError):
            pass
        else:
            return list(records)

    try:
        return list(rows)
    except TypeError as exc:
        raise TypeError("输入必须是记录可迭代对象或单条映射记录") from exc


def _validate_row_schema(rows: Sequence[RowLike], keys: Sequence[str], side: str) -> None:
    missing: set[str] = set()
    for row in rows:
        columns = _row_columns(row)
        missing.update(key for key in keys if key not in columns)
    if missing:
        ordered = [key for key in keys if key in missing]
        raise KeyError({side: ordered})


def normalize_composite_key(
    row: RowLike,
    keys: Iterable[str],
    *,
    rules: KeyNormalizationRules | None = None,
) -> CompositeKey | None:
    """返回一条记录的标准化复合键。

    任一组成字段缺失时返回 ``None``。合法键的每个组成值必须可哈希，以保证
    后续索引是确定的；这也避免用不可控的字符串 repr 偷换业务键语义。
    """

    key_tuple = _validate_keys(keys)
    active_rules = rules or KeyNormalizationRules()
    values = tuple(
        active_rules.normalize(_row_value(row, key))
        for key in key_tuple
    )
    if any(value is None for value in values):
        return None
    normalized_key = values
    try:
        hash(normalized_key)
    except TypeError as exc:
        raise TypeError("复合键字段必须是可哈希的标量值") from exc
    return normalized_key


@dataclass(frozen=True)
class _SideIndex:
    rows: tuple[RowLike, ...]
    normalized_keys: tuple[CompositeKey | None, ...]
    key_to_indices: dict[CompositeKey, tuple[int, ...]]


def _index_side(
    rows: Sequence[RowLike],
    keys: Sequence[str],
    rules: KeyNormalizationRules,
) -> _SideIndex:
    normalized: list[CompositeKey | None] = []
    positions: dict[CompositeKey, list[int]] = {}
    for position, row in enumerate(rows):
        composite_key = normalize_composite_key(row, keys, rules=rules)
        normalized.append(composite_key)
        if composite_key is not None:
            positions.setdefault(composite_key, []).append(position)
    return _SideIndex(
        rows=tuple(rows),
        normalized_keys=tuple(normalized),
        key_to_indices={key: tuple(indices) for key, indices in positions.items()},
    )


def _rows_at(rows: Sequence[RowLike], positions: set[int]) -> tuple[RowLike, ...]:
    return tuple(row for position, row in enumerate(rows) if position in positions)


def _positions_for_keys(
    index: _SideIndex,
    positions: set[int],
    keys: set[CompositeKey],
) -> tuple[RowLike, ...]:
    return _rows_at(index.rows, positions & {
        position
        for key in keys
        for position in index.key_to_indices.get(key, ())
    })


@dataclass(frozen=True)
class KeyIntegrityResult:
    """复合键完整性分类结果。

    ``valid_left``/``valid_right`` 表示“本侧键非空且本侧唯一”，并不表示
    一定可以匹配；其中对侧重复的唯一记录会同时出现在对应的
    ``*_blocked_by_*_duplicate`` 类别。五类门控类别（空、重复、左右独有、
    blocked_by_duplicate）之间按每一侧记录互斥；``matched_*`` 才是安全的一对一
    精确匹配候选。
    """

    keys: tuple[str, ...]
    rule_version: str
    valid_left: tuple[RowLike, ...]
    valid_right: tuple[RowLike, ...]
    null_left: tuple[RowLike, ...]
    null_right: tuple[RowLike, ...]
    duplicate_left: tuple[RowLike, ...]
    duplicate_right: tuple[RowLike, ...]
    left_only_rows: tuple[RowLike, ...]
    right_only_rows: tuple[RowLike, ...]
    left_blocked_by_right_duplicate: tuple[RowLike, ...]
    right_blocked_by_left_duplicate: tuple[RowLike, ...]
    matched_left: tuple[RowLike, ...]
    matched_right: tuple[RowLike, ...]
    matched_pairs: tuple[tuple[RowLike, RowLike], ...]
    normalized_left_keys: tuple[CompositeKey | None, ...]
    normalized_right_keys: tuple[CompositeKey | None, ...]
    left_key_counts: dict[CompositeKey, int]
    right_key_counts: dict[CompositeKey, int]

    @property
    def rules_version(self) -> str:
        """``rule_version`` 的复数别名。"""

        return self.rule_version

    @property
    def key_rule_version(self) -> str:
        return self.rule_version

    @property
    def empty_left(self) -> tuple[RowLike, ...]:
        return self.null_left

    @property
    def empty_right(self) -> tuple[RowLike, ...]:
        return self.null_right

    @property
    def left_only(self) -> tuple[RowLike, ...]:
        return self.left_only_rows

    @property
    def right_only(self) -> tuple[RowLike, ...]:
        return self.right_only_rows

    @property
    def blocked_by_duplicate_left(self) -> tuple[RowLike, ...]:
        return self.left_blocked_by_right_duplicate

    @property
    def blocked_by_duplicate_right(self) -> tuple[RowLike, ...]:
        return self.right_blocked_by_left_duplicate

    @property
    def empty_key_count_left(self) -> int:
        return len(self.null_left)

    @property
    def empty_key_count_right(self) -> int:
        return len(self.null_right)

    @property
    def duplicate_key_count_left(self) -> int:
        return sum(count > 1 for count in self.left_key_counts.values())

    @property
    def duplicate_key_count_right(self) -> int:
        return sum(count > 1 for count in self.right_key_counts.values())

    @property
    def duplicate_record_count_left(self) -> int:
        return sum(count for count in self.left_key_counts.values() if count > 1)

    @property
    def duplicate_record_count_right(self) -> int:
        return sum(count for count in self.right_key_counts.values() if count > 1)

    @property
    def duplicate_key_count(self) -> int:
        """左右两侧重复键问题数之和；同一键两侧均重复会计两侧。"""

        return self.duplicate_key_count_left + self.duplicate_key_count_right

    @property
    def duplicate_record_count(self) -> int:
        return self.duplicate_record_count_left + self.duplicate_record_count_right

    @property
    def duplicate_key_counts(self) -> dict[str, int]:
        return {"left": self.duplicate_key_count_left, "right": self.duplicate_key_count_right}

    @property
    def duplicate_record_counts(self) -> dict[str, int]:
        return {"left": self.duplicate_record_count_left, "right": self.duplicate_record_count_right}

    @property
    def left_only_count(self) -> int:
        return len(self.left_only_rows)

    @property
    def right_only_count(self) -> int:
        return len(self.right_only_rows)

    @property
    def blocked_by_duplicate_left_count(self) -> int:
        return len(self.left_blocked_by_right_duplicate)

    @property
    def blocked_by_duplicate_right_count(self) -> int:
        return len(self.right_blocked_by_left_duplicate)

    @property
    def matched_key_count(self) -> int:
        return len(self.matched_pairs)

    @property
    def categories_are_mutually_exclusive(self) -> bool:
        """检查五类记录类别是否无交集，供门控/测试直接使用。"""

        # 用分区总数判断而不是用 ``id(row)`` 判断：同一个 dict/Row 对象被调用
        # 方有意重复传入时，它代表两个输入位置，不能因对象身份相同而把同一
        # 类别内部的两条记录误判为跨类别重叠。
        left_count = (
            len(self.null_left)
            + len(self.duplicate_left)
            + len(self.left_only_rows)
            + len(self.left_blocked_by_right_duplicate)
            + len(self.matched_left)
        )
        right_count = (
            len(self.null_right)
            + len(self.duplicate_right)
            + len(self.right_only_rows)
            + len(self.right_blocked_by_left_duplicate)
            + len(self.matched_right)
        )
        return left_count == len(self.normalized_left_keys) and right_count == len(self.normalized_right_keys)

    def iter_matched_pairs(self) -> Iterator[tuple[RowLike, RowLike]]:
        """按完整键顺序返回一对一候选，不生成重复连接行。"""

        yield from self.matched_pairs

    def summary(self) -> dict[str, Any]:
        """返回适合 Finding/Evidence 适配层使用的计数摘要。

        摘要只保留计数和规则版本；来源行仍在分类字段中原样保留，避免为每条
        问题复制整张输入表。
        """

        return {
            "keys": list(self.keys),
            "rule_version": self.rule_version,
            "left_rows": len(self.normalized_left_keys),
            "right_rows": len(self.normalized_right_keys),
            "categories_are_mutually_exclusive": self.categories_are_mutually_exclusive,
            "empty": {
                "left": self.empty_key_count_left,
                "right": self.empty_key_count_right,
            },
            "duplicate": {
                "keys": self.duplicate_key_counts,
                "records": self.duplicate_record_counts,
            },
            "left_only": self.left_only_count,
            "right_only": self.right_only_count,
            "blocked_by_duplicate": {
                "left": self.blocked_by_duplicate_left_count,
                "right": self.blocked_by_duplicate_right_count,
            },
            "matched": {
                "keys": self.matched_key_count,
                "left_records": len(self.matched_left),
                "right_records": len(self.matched_right),
            },
        }

    as_record = summary


def classify_composite_keys(
    left: Iterable[RowLike] | RowLike | None,
    right: Iterable[RowLike] | RowLike | None,
    keys: Iterable[str],
    *,
    rules: KeyNormalizationRules | None = None,
    rule_version: str | None = None,
) -> KeyIntegrityResult:
    """分类左右两组记录的复合键完整性。

    参数：
        left/right: 映射记录、SQLite ``Row`` 或记录迭代器。输入行不被修改。
        keys: 复合键字段，至少一个且不得重复；键比较使用完整字段组合。
        rules: 版本化标准化规则；未传时使用 ``composite-key-v1``。
        rule_version: 仅需切换版本标识而使用默认规则时的快捷参数。

    分类优先级及互斥边界：

    1. 任一键组成字段缺失 → ``null_left``/``null_right``；
    2. 本侧同键出现多次 → ``duplicate_left``/``duplicate_right``，该侧全部
       记录不再进入左右独有或安全匹配；
    3. 本侧唯一且对侧同键重复 → ``left_blocked_by_right_duplicate`` 或反向类；
    4. 本侧唯一且完整键只存在本侧 → ``left_only_rows``/``right_only_rows``；
    5. 左右均唯一的同键只产生一个 ``matched_pairs`` 元素。

    函数只用哈希索引按键定位行，不调用 merge/join，也不生成重复键的笛卡尔
    乘积。因此 ``len(matched_pairs)`` 始终等于左右均唯一的共同键数。
    """

    key_tuple = _validate_keys(keys)
    active_rules = _coerce_rules(rules, rule_version)
    left_rows = _materialize_rows(left)
    right_rows = _materialize_rows(right)
    _validate_row_schema(left_rows, key_tuple, "left")
    _validate_row_schema(right_rows, key_tuple, "right")

    left_index = _index_side(left_rows, key_tuple, active_rules)
    right_index = _index_side(right_rows, key_tuple, active_rules)

    left_nonempty_positions = {
        position for position, key in enumerate(left_index.normalized_keys) if key is not None
    }
    right_nonempty_positions = {
        position for position, key in enumerate(right_index.normalized_keys) if key is not None
    }
    left_duplicate_keys = {
        key for key, positions in left_index.key_to_indices.items() if len(positions) > 1
    }
    right_duplicate_keys = {
        key for key, positions in right_index.key_to_indices.items() if len(positions) > 1
    }
    left_unique_keys = {
        key for key, positions in left_index.key_to_indices.items() if len(positions) == 1
    }
    right_unique_keys = {
        key for key, positions in right_index.key_to_indices.items() if len(positions) == 1
    }
    left_unique_positions = {
        position
        for key in left_unique_keys
        for position in left_index.key_to_indices[key]
    }
    right_unique_positions = {
        position
        for key in right_unique_keys
        for position in right_index.key_to_indices[key]
    }

    left_only_keys = left_unique_keys - set(right_index.key_to_indices)
    right_only_keys = right_unique_keys - set(left_index.key_to_indices)
    left_blocked_keys = left_unique_keys & right_duplicate_keys
    right_blocked_keys = right_unique_keys & left_duplicate_keys
    matched_keys = left_unique_keys & right_unique_keys

    left_only_rows = _positions_for_keys(left_index, left_unique_positions, left_only_keys)
    right_only_rows = _positions_for_keys(right_index, right_unique_positions, right_only_keys)
    left_blocked = _positions_for_keys(left_index, left_unique_positions, left_blocked_keys)
    right_blocked = _positions_for_keys(right_index, right_unique_positions, right_blocked_keys)
    matched_left = _positions_for_keys(left_index, left_unique_positions, matched_keys)
    matched_right = _positions_for_keys(right_index, right_unique_positions, matched_keys)

    matched_pairs: list[tuple[RowLike, RowLike]] = []
    # 以左侧首次出现顺序遍历共同唯一键，一键最多取一行；不对重复键做任何
    # pairing，因此不会出现 m×n 行放大。
    for key, left_positions in left_index.key_to_indices.items():
        if key not in matched_keys:
            continue
        right_positions = right_index.key_to_indices[key]
        matched_pairs.append((left_index.rows[left_positions[0]], right_index.rows[right_positions[0]]))

    duplicate_left_positions = {
        position
        for key in left_duplicate_keys
        for position in left_index.key_to_indices[key]
    }
    duplicate_right_positions = {
        position
        for key in right_duplicate_keys
        for position in right_index.key_to_indices[key]
    }

    result = KeyIntegrityResult(
        keys=key_tuple,
        rule_version=active_rules.version,
        valid_left=_rows_at(left_index.rows, left_unique_positions),
        valid_right=_rows_at(right_index.rows, right_unique_positions),
        null_left=_rows_at(left_index.rows, set(range(len(left_rows))) - left_nonempty_positions),
        null_right=_rows_at(right_index.rows, set(range(len(right_rows))) - right_nonempty_positions),
        duplicate_left=_rows_at(left_index.rows, duplicate_left_positions),
        duplicate_right=_rows_at(right_index.rows, duplicate_right_positions),
        left_only_rows=left_only_rows,
        right_only_rows=right_only_rows,
        left_blocked_by_right_duplicate=left_blocked,
        right_blocked_by_left_duplicate=right_blocked,
        matched_left=matched_left,
        matched_right=matched_right,
        matched_pairs=tuple(matched_pairs),
        normalized_left_keys=left_index.normalized_keys,
        normalized_right_keys=right_index.normalized_keys,
        left_key_counts={key: len(positions) for key, positions in left_index.key_to_indices.items()},
        right_key_counts={key: len(positions) for key, positions in right_index.key_to_indices.items()},
    )
    return result


__all__ = [
    "BLOCKED_BY_DUPLICATE",
    "CompositeKeyRules",
    "DUPLICATE",
    "DEFAULT_RULE_VERSION",
    "EMPTY",
    "KeyIntegrityResult",
    "KeyIntegrityRules",
    "KeyNormalizationRules",
    "KEY_RULE_VERSION",
    "LEFT_ONLY",
    "RIGHT_ONLY",
    "classify_composite_keys",
    "normalize_composite_key",
    "normalize_key_value",
]
