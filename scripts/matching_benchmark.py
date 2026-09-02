"""匹配结果的客观、闭世界 benchmark。

这个模块只做验收评价，不参与价盾的生产匹配决策，也不改变现有匹配算法。
它要求测试人员为每个脱敏案例提供稳定的项目项身份（例如文件 SHA-256、
Sheet 和物理行组成的引用），再把人工确认的匹配组作为真值。没有人工真值
时返回 ``PENDING``；标签或预测无法对齐时返回 ``INCOMPARABLE``，绝不把
缺失标签当作没有误报/漏报。

匹配评价按“项目项对”计算：一个真值组内每两个项目项组成一个正样本，
``confirmed``/``probable`` 候选组成预测正样本；``suspected`` 只计入待复核
数量，不当作自动匹配。精确率、召回率和误报率使用 Decimal 计算并序列化
为四位小数的字符串，避免 benchmark 自己引入二进制浮点误差。
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VALID_LEVELS = frozenset({"confirmed", "probable", "suspected", "incomparable", "pending_data"})
SCORED_LEVELS = frozenset({"confirmed", "probable"})
RATIO_SCALE = Decimal("0.0001")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class MatchingBenchmarkError(ValueError):
    """benchmark 输入结构不能形成可比结果。"""


def stable_item_identity(*, file_sha256: str, sheet_name: str, row: int) -> str:
    """构造跨运行稳定的清单项身份。

    只使用源文件 SHA-256、Sheet 名和物理行号，不使用临时数据库自增 ID。
    Sheet 名采用 JSON 字符串编码，避免名称中的分隔符造成身份碰撞。
    """
    if not isinstance(file_sha256, str) or not SHA256_RE.fullmatch(file_sha256.strip()):
        raise MatchingBenchmarkError("项目项身份的 file_sha256 必须是 64 位十六进制字符串")
    if not isinstance(sheet_name, str) or not sheet_name.strip():
        raise MatchingBenchmarkError("项目项身份的 sheet_name 不能为空")
    if isinstance(row, bool) or not isinstance(row, int) or row < 1:
        raise MatchingBenchmarkError("项目项身份的 row 必须是正整数")
    encoded_sheet = json.dumps(sheet_name.strip(), ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{file_sha256.strip().lower()}|sheet:{encoded_sheet}|row:{row}"


def prediction_groups_from_mapping(
    groups: object,
    item_identity_by_id: dict[int, str],
) -> list[dict[str, Any]]:
    """把匹配引擎的临时 ``item_ids`` 转换成稳定身份。

    该适配器不读取或修改数据库；调用方必须先以当前运行的 Evidence 建立
    ``item_identity_by_id``。缺失映射立即报错，避免把临时 ID 写进黄金标签。
    """
    if not isinstance(groups, list):
        raise MatchingBenchmarkError("匹配引擎 groups 必须是数组")
    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if isinstance(group, dict):
            raw_ids = group.get("item_ids")
            level = group.get("level")
            status = group.get("status", "pending")
        else:
            raw_ids = getattr(group, "item_ids", None)
            level = getattr(group, "level", None)
            status = getattr(group, "status", "pending")
        if not isinstance(raw_ids, list):
            raise MatchingBenchmarkError(f"groups[{index}].item_ids 必须是数组")
        identities: list[str] = []
        for raw_id in raw_ids:
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                raise MatchingBenchmarkError(f"groups[{index}] 存在非法 item_id：{raw_id!r}")
            identity = item_identity_by_id.get(raw_id)
            if not isinstance(identity, str) or not identity.strip():
                raise MatchingBenchmarkError(f"groups[{index}] 缺少 item_id={raw_id} 的稳定身份")
            identities.append(identity.strip())
        result.append({
            "items": identities,
            "level": level,
            "status": status,
        })
    return result


def _empty_metrics() -> dict[str, Any]:
    return {
        "true_positive": None,
        "false_positive": None,
        "false_negative": None,
        "true_negative": None,
        "precision": None,
        "recall": None,
        "f1": None,
        "false_positive_rate": None,
    }


def _empty_counts() -> dict[str, int]:
    return {
        "candidate_group_count": 0,
        "confirmed_group_count": 0,
        "probable_group_count": 0,
        "suspected_group_count": 0,
        "incomparable_group_count": 0,
        "pending_data_group_count": 0,
        "automatic_confirmation_count": 0,
        "manual_review_group_count": 0,
    }


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        RATIO_SCALE, rounding=ROUND_HALF_UP
    )
    return format(value, "f")


def _pair_list(pairs: set[tuple[str, str]]) -> list[list[str]]:
    return [[left, right] for left, right in sorted(pairs)]


def _pairs(groups: list[list[str]]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for items in groups:
        result.update(tuple(pair) for pair in itertools.combinations(sorted(items), 2))
    return result


def _item_list(raw: object, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise MatchingBenchmarkError(f"{label} 必须是数组")
    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise MatchingBenchmarkError(f"{label}[{index}] 必须是非空稳定项目项身份")
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise MatchingBenchmarkError(f"{label} 存在重复项目项身份")
    return result


def _truth_partition(truth: object) -> tuple[set[str], set[tuple[str, str]], set[str], set[str], set[str]]:
    """校验人工真值并返回 universe、正对和三类非可比项目项。"""
    if not isinstance(truth, dict):
        raise MatchingBenchmarkError("匹配真值必须是对象")
    required = (
        "item_universe",
        "matching_groups",
        "unmatched_items",
        "incomparable_items",
        "pending_items",
    )
    missing = [key for key in required if key not in truth]
    if missing:
        raise MatchingBenchmarkError(f"匹配真值缺少字段：{','.join(missing)}")
    universe_list = _item_list(truth["item_universe"], "item_universe")
    if not universe_list:
        raise MatchingBenchmarkError("item_universe 不能为空")
    universe = set(universe_list)

    raw_groups = truth["matching_groups"]
    if not isinstance(raw_groups, list):
        raise MatchingBenchmarkError("matching_groups 必须是数组")
    groups: list[list[str]] = []
    grouped: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        items = _item_list(raw_group, f"matching_groups[{index}]")
        if len(items) < 2:
            raise MatchingBenchmarkError(f"matching_groups[{index}] 至少需要两个项目项")
        overlap = grouped.intersection(items)
        if overlap:
            raise MatchingBenchmarkError(
                f"matching_groups 存在重复分组身份：{sorted(overlap)}"
            )
        grouped.update(items)
        groups.append(items)

    unmatched = set(_item_list(truth["unmatched_items"], "unmatched_items"))
    incomparable = set(_item_list(truth["incomparable_items"], "incomparable_items"))
    pending = set(_item_list(truth["pending_items"], "pending_items"))
    partition = grouped | unmatched | incomparable | pending
    duplicate_partition = (
        (grouped & unmatched) | (grouped & incomparable) | (grouped & pending)
        | (unmatched & incomparable) | (unmatched & pending) | (incomparable & pending)
    )
    if duplicate_partition:
        raise MatchingBenchmarkError(
            f"真值分区存在重复项目项身份：{sorted(duplicate_partition)}"
        )
    missing_items = sorted(universe - partition)
    extra_items = sorted(partition - universe)
    if missing_items:
        raise MatchingBenchmarkError(f"未被真值分区覆盖的项目项：{missing_items}")
    if extra_items:
        raise MatchingBenchmarkError(f"真值分区包含 universe 外项目项：{extra_items}")
    if grouped.intersection(incomparable | pending):
        raise MatchingBenchmarkError("matching_groups 不得包含不可比或待补资料项目项")
    return universe, _pairs(groups), incomparable, pending, grouped


def _prediction_groups(predicted_groups: object, universe: set[str]) -> tuple[
    list[dict[str, Any]], list[str]
]:
    if not isinstance(predicted_groups, list):
        return [], ["预测匹配组必须是数组"]
    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_group in enumerate(predicted_groups):
        if not isinstance(raw_group, dict):
            errors.append(f"predicted_groups[{index}] 必须是对象")
            continue
        try:
            items = _item_list(raw_group.get("items"), f"predicted_groups[{index}].items")
        except MatchingBenchmarkError as exc:
            errors.append(str(exc))
            continue
        level = raw_group.get("level")
        if not isinstance(level, str) or level.strip().lower() not in VALID_LEVELS:
            errors.append(
                f"predicted_groups[{index}].level 必须为 {sorted(VALID_LEVELS)}"
            )
            continue
        level = level.strip().lower()
        status = raw_group.get("status", "pending")
        if not isinstance(status, str) or not status.strip():
            errors.append(f"predicted_groups[{index}].status 必须是非空字符串")
            continue
        unknown = sorted(set(items) - universe)
        if unknown:
            errors.append(
                f"predicted_groups[{index}] 包含真值 universe 外项目项：{unknown}"
            )
        overlap = seen.intersection(items)
        if overlap:
            errors.append(
                f"预测匹配组之间重复项目项身份：{sorted(overlap)}"
            )
        seen.update(items)
        parsed.append({"items": items, "level": level, "status": status.strip().lower()})
    return parsed, errors


def _counts(groups: list[dict[str, Any]]) -> dict[str, int]:
    levels = Counter(str(group["level"]) for group in groups)
    automatic = sum(
        group["level"] == "confirmed" and group["status"] == "confirmed"
        for group in groups
    )
    result = _empty_counts()
    result.update({
        "candidate_group_count": len(groups),
        "confirmed_group_count": levels.get("confirmed", 0),
        "probable_group_count": levels.get("probable", 0),
        "suspected_group_count": levels.get("suspected", 0),
        "incomparable_group_count": levels.get("incomparable", 0),
        "pending_data_group_count": levels.get("pending_data", 0),
        "automatic_confirmation_count": automatic,
        "manual_review_group_count": len(groups) - automatic,
    })
    return result


def _invalid_report(status: str, errors: list[str], counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "metrics": _empty_metrics(),
        "counts": counts or _empty_counts(),
        "false_positive_pairs": [],
        "false_negative_pairs": [],
        "errors": errors,
    }


def evaluate_matching(predicted_groups: object, truth: object) -> dict[str, Any]:
    """评价一个案例的匹配结果，返回可直接写入 JSON 的字典。

    ``truth is None`` 表示尚未完成项目项人工标注，返回 PENDING；空数组是
    合法的“算法没有给出候选”，是否正确由真值中的 matching_groups 决定。
    """
    if truth is None:
        # 尚无 universe 时，不能把任意预测身份误报为“未知项目项”；这属于
        # 标签未补齐的 PENDING，而不是预测结构已经可判定错误。
        parsed_predictions, _ = _prediction_groups(predicted_groups, set())
        return _invalid_report(
            "PENDING",
            ["缺少人工标注的匹配真值"],
            _counts(parsed_predictions),
        )
    try:
        universe, expected_pairs, incomparable_items, pending_items, _ = _truth_partition(truth)
    except MatchingBenchmarkError as exc:
        parsed_predictions, prediction_errors = _prediction_groups(predicted_groups, set())
        return _invalid_report(
            "INCOMPARABLE",
            [str(exc), *prediction_errors],
            _counts(parsed_predictions),
        )

    groups, errors = _prediction_groups(predicted_groups, universe)
    counts = _counts(groups)
    predicted_pair_groups: list[list[str]] = []
    for group in groups:
        if group["level"] not in SCORED_LEVELS or len(group["items"]) < 2:
            continue
        items = set(group["items"])
        blocked = sorted(items.intersection(incomparable_items | pending_items))
        if blocked:
            errors.append(
                f"自动匹配候选包含不可比/待补资料项目项：{blocked}"
            )
            continue
        predicted_pair_groups.append(group["items"])

    if errors:
        return _invalid_report("INCOMPARABLE", errors, counts)

    eligible_items = universe - incomparable_items - pending_items
    all_pairs = {
        tuple(pair) for pair in itertools.combinations(sorted(eligible_items), 2)
    }
    predicted_pairs = _pairs(predicted_pair_groups)
    true_positive_pairs = predicted_pairs & expected_pairs
    false_positive_pairs = predicted_pairs - expected_pairs
    false_negative_pairs = expected_pairs - predicted_pairs
    true_negative = all_pairs - expected_pairs - predicted_pairs
    tp = len(true_positive_pairs)
    fp = len(false_positive_pairs)
    fn = len(false_negative_pairs)
    tn = len(true_negative)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = (
        _ratio(2 * tp, 2 * tp + fp + fn)
        if (tp + fp + fn) > 0
        else None
    )
    metrics = {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _ratio(fp, fp + tn),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not false_positive_pairs and not false_negative_pairs else "FAIL",
        "metrics": metrics,
        "counts": counts,
        "labels": {
            "item_count": len(universe),
            "eligible_item_count": len(eligible_items),
            "incomparable_item_count": len(incomparable_items),
            "pending_item_count": len(pending_items),
            "expected_positive_pair_count": len(expected_pairs),
        },
        "false_positive_pairs": _pair_list(false_positive_pairs),
        "false_negative_pairs": _pair_list(false_negative_pairs),
        "errors": [],
    }


def _overall_status(statuses: Counter[str]) -> str:
    for status in ("INCOMPARABLE", "FAIL", "PENDING", "PASS"):
        if statuses.get(status, 0):
            return status
    return "INCOMPARABLE"


def evaluate_cases(cases: object) -> dict[str, Any]:
    """汇总多个案例；输入是 ``[{case_id, predicted_groups, truth}]``。"""
    if not isinstance(cases, list) or not cases:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INCOMPARABLE",
            "case_count": 0,
            "comparison_status_counts": {"INCOMPARABLE": 1},
            "overall_comparison_status": "INCOMPARABLE",
            "results": [],
            "errors": ["cases 必须是非空数组"],
        }
    results: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    errors: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            result = _invalid_report("INCOMPARABLE", [f"cases[{index}] 必须是对象"])
            result["case_id"] = f"<case-{index}>"
        else:
            case_id = case.get("case_id")
            case_id = str(case_id).strip() if case_id not in (None, "") else f"<case-{index}>"
            result = evaluate_matching(case.get("predicted_groups"), case.get("truth"))
            result["case_id"] = case_id
        statuses[str(result["status"])] += 1
        if result.get("errors"):
            errors.extend(f"{result['case_id']}: {error}" for error in result["errors"])
        results.append(result)
    status = _overall_status(statuses)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if status in {"PASS", "PENDING"} else "failed",
        "case_count": len(results),
        "comparison_status_counts": dict(sorted(statuses.items())),
        "overall_comparison_status": status,
        "results": results,
        "errors": errors,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 价盾匹配 Benchmark",
        "",
        f"- 总体比较状态：`{report.get('overall_comparison_status', report.get('status'))}`",
        f"- 案例数：{report.get('case_count', 0)}",
        "",
        "| 案例 | 状态 | Precision | Recall | F1 | FP | FN | 说明 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for result in report.get("results", []):
        metrics = result.get("metrics") or {}
        errors = "；".join(result.get("errors") or [])
        lines.append(
            "| {case} | `{status}` | {precision} | {recall} | {f1} | {fp} | {fn} | {errors} |".format(
                case=result.get("case_id", ""),
                status=result.get("status", "INCOMPARABLE"),
                precision=metrics.get("precision") if metrics.get("precision") is not None else "待补资料",
                recall=metrics.get("recall") if metrics.get("recall") is not None else "待补资料",
                f1=metrics.get("f1") if metrics.get("f1") is not None else "待补资料",
                fp=metrics.get("false_positive") if metrics.get("false_positive") is not None else "—",
                fn=metrics.get("false_negative") if metrics.get("false_negative") is not None else "—",
                errors=errors or "—",
            )
        )
    lines.extend([
        "",
        "误报项目对：优先人工复核 `false_positive_pairs`；疑似匹配不计入自动正样本。",
    ])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行价盾匹配 benchmark（只读评价人工真值）")
    parser.add_argument("--input", type=Path, required=True, help="JSON 输入：cases 数组或单案例对象")
    parser.add_argument("--output", type=Path, help="JSON 报告路径；默认输出到标准输出")
    parser.add_argument("--markdown", type=Path, help="可选 Markdown 人读报告路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"匹配 benchmark 输入不可读：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        report = evaluate_cases(payload["cases"])
    elif isinstance(payload, dict):
        report = evaluate_cases([payload])
    else:
        report = evaluate_cases(payload)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown(report), encoding="utf-8")
    return 0 if report.get("overall_comparison_status") in {"PASS", "PENDING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
