"""价盾黄金回归库执行器（P0-04）。

黄金结果是受控的、只读的发布基线。执行器对每个已登记案例建立全新临时
项目，真实走“导入 → 聚合 → A/B/C 校核 → 异常 → 匹配”链路，再把稳定的
计数、Decimal 金额、匹配关系、已知异常和 Evidence 关键计数与 JSON 规格
逐项比较。任何变化都输出字段路径、期望值和实际值，**不会自动更新黄金
文件**，也不会读取或写入 ``local_private_data/``。

脱敏真实案例由用户明确登记后才可加入 ``tests/golden/cases.json``；仓库内
的演示案例明确标记为 ``synthetic_demo``，只能证明回归框架和匿名输入可用，
不能冒充 P0-04 的真实项目覆盖。``--require-real`` 用于生产发布门槛，当前
没有可用真实案例时返回专门的非零状态。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "tests" / "golden" / "cases.json"
PRIVATE_PART = "local_private_data"
AMOUNT_SCALE = Decimal("0.01")


class GoldenRegistryError(ValueError):
    """黄金规格结构或路径不符合安全边界。"""


class GoldenCaseExecutionError(RuntimeError):
    """黄金案例执行失败。"""


def _is_private(relative: str) -> bool:
    parts = Path(relative).as_posix().strip("/").split("/")
    return PRIVATE_PART in parts


def _safe_repo_path(relative: str, root: Path) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise GoldenRegistryError("黄金案例输入路径必须是非空相对路径")
    path = Path(relative)
    if path.is_absolute() or _is_private(path.as_posix()):
        raise GoldenRegistryError(f"黄金回归禁止使用绝对路径或 local_private_data：{relative!r}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise GoldenRegistryError(f"黄金案例路径越过仓库根目录：{relative!r}") from exc
    return resolved


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    """读取并校验黄金规格；只读，不修补、不排序写回。"""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenRegistryError(f"无法读取黄金规格 {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise GoldenRegistryError("黄金规格必须是 schema_version=1 的对象")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise GoldenRegistryError("黄金规格至少需要一个案例")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise GoldenRegistryError("黄金案例必须是对象")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise GoldenRegistryError(f"黄金案例 case_id 缺失或重复：{case_id!r}")
        seen.add(case_id)
        availability = case.get("availability")
        if availability not in {"available", "not_available"}:
            raise GoldenRegistryError(f"{case_id} availability 必须为 available/not_available")
        if availability == "available":
            if not isinstance(case.get("inputs"), list) or not case["inputs"]:
                raise GoldenRegistryError(f"可用黄金案例 {case_id} 缺少 inputs")
            if not isinstance(case.get("expected"), dict):
                raise GoldenRegistryError(f"可用黄金案例 {case_id} 缺少 expected 对象")
    return data


def _decimal(value: object) -> Decimal:
    if value is None:
        raise InvalidOperation("金额缺失")
    return Decimal(str(value))


def _decimal_sum(values: list[object]) -> tuple[str | None, int]:
    total: Decimal | None = None
    missing = 0
    for value in values:
        if value is None or value == "":
            missing += 1
            continue
        try:
            parsed = _decimal(value)
        except (InvalidOperation, ValueError):
            missing += 1
            continue
        total = parsed if total is None else total + parsed
    if total is None:
        return None, missing
    return str(total.quantize(AMOUNT_SCALE, rounding=ROUND_HALF_UP)), missing


def _current_rows(conn, project_id: int, alias: str, columns: str) -> list[Any]:
    from jiadun.core.contracts import run_contract

    table = {"m": "matches", "a": "anomalies", "cr": "crosscheck_results", "pt": "period_totals"}.get(alias)
    if table is None:
        raise ValueError(f"不支持的黄金回归读取别名：{alias}")
    scope, params = run_contract.current_scope(conn, project_id, alias)
    return conn.execute(
        f"SELECT {columns} FROM {table} {alias} WHERE {alias}.project_id=? AND {scope}",
        (project_id, *params),
    ).fetchall()


def _period_amounts(conn, project_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT sp.id, sp.period_no, sp.direction
           FROM settlement_periods sp WHERE sp.project_id=?
           ORDER BY sp.direction, sp.period_no, sp.id""",
        (project_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    from jiadun.core.contracts import run_contract

    scope, params = run_contract.current_scope(conn, project_id, "pt")
    for period in rows:
        totals = conn.execute(
            f"""SELECT amount_sum FROM period_totals pt
                WHERE pt.project_id=? AND pt.period_id=? AND {scope}""",
            (project_id, period["id"], *params),
        ).fetchall()
        amount, missing = _decimal_sum([row["amount_sum"] for row in totals])
        key = f"{period['direction']}:{period['period_no']}"
        result[key] = {
            "direction": period["direction"],
            "period_no": int(period["period_no"]),
            "amount": amount if totals and missing == 0 else None,
            "available_amount": amount,
            "item_count": len(totals),
            "missing_amount_items": missing,
        }
    return result


def _cumulative_amounts(period_amounts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    directions = sorted({item["direction"] for item in period_amounts.values()})
    result: dict[str, dict[str, Any]] = {}
    for direction in directions:
        values = [item for item in period_amounts.values() if item["direction"] == direction]
        available, missing = _decimal_sum([item["available_amount"] for item in values])
        result[direction] = {
            "amount": available if values and all(item["amount"] is not None for item in values) else None,
            "available_amount": available,
            "period_count": len(values),
            "incomplete_periods": sum(item["amount"] is None for item in values),
            "missing_amount_items": missing,
        }
    return result


def _build_metrics(conn, project_id: int) -> dict[str, Any]:
    from jiadun.core.contracts import run_contract

    source_files = conn.execute(
        "SELECT COUNT(*) AS c FROM source_files WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    sheet_rows = conn.execute(
        """SELECT sheet_status, sheet_status_actor FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=?""",
        (project_id,),
    ).fetchall()
    automatic = sum(
        row["sheet_status"] == "confirmed" and row["sheet_status_actor"] == "system"
        for row in sheet_rows
    )
    pending = sum(row["sheet_status"] == "pending" for row in sheet_rows)
    detail_rows = conn.execute(
        """SELECT COUNT(*) AS c FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           WHERE sp.project_id=? AND json_extract(li.flags_json, '$.subtotal') IS NOT 1""",
        (project_id,),
    ).fetchone()["c"]
    periods = conn.execute(
        "SELECT COUNT(*) AS c FROM settlement_periods WHERE project_id=?", (project_id,)
    ).fetchone()["c"]

    period_amounts = _period_amounts(conn, project_id)
    cumulative = _cumulative_amounts(period_amounts)
    crosscheck_rows = _current_rows(
        conn,
        project_id,
        "cr",
        "cr.period_id, cr.verification_level, cr.status, cr.control_status, "
        "cr.evidence_id, cr.coverage_proof_status",
    )
    crosscheck_levels = Counter(str(row["verification_level"]) for row in crosscheck_rows)
    crosscheck_statuses = Counter(str(row["status"]) for row in crosscheck_rows)
    match_rows = _current_rows(
        conn, project_id, "m", "m.group_key, m.level, m.status, m.item_ids_json"
    )
    level_counts = Counter(str(row["level"]) for row in match_rows)
    match_keys = sorted(str(row["group_key"]) for row in match_rows)
    match_partition = {
        "candidate_group_keys": sorted(
            str(row["group_key"])
            for row in match_rows
            if row["level"] in {"confirmed", "probable"}
        ),
        "non_match_group_keys": sorted(
            str(row["group_key"])
            for row in match_rows
            if row["level"] in {"incomparable", "pending_data"}
        ),
    }
    anomaly_rows = _current_rows(conn, project_id, "a", "a.rule_id, a.status, a.evidence_id")
    anomaly_rules = sorted({str(row["rule_id"]) for row in anomaly_rows})

    evidence_scope, evidence_params = run_contract.current_scope(conn, project_id, "e")
    evidence_rows = conn.execute(
        f"""SELECT e.kind, e.scope FROM evidence e
            WHERE e.project_id=? AND {evidence_scope}""",
        (project_id, *evidence_params),
    ).fetchall()
    evidence_kinds = Counter(str(row["kind"]) for row in evidence_rows)
    historical_evidence = conn.execute(
        "SELECT COUNT(*) AS c FROM evidence WHERE project_id=? AND scope='historical'",
        (project_id,),
    ).fetchone()["c"]
    current_contract = run_contract.get_current_contract(conn, project_id)
    return {
        "file_count": int(source_files),
        "sheet_count": len(sheet_rows),
        "automatic_recognition_count": int(automatic),
        "pending_sheet_count": int(pending),
        "period_count": int(periods),
        "detail_row_count": int(detail_rows),
        "period_amounts": period_amounts,
        "cumulative_amounts": cumulative,
        "crosscheck": {
            "count": len(crosscheck_rows),
            "by_verification_level": dict(sorted(crosscheck_levels.items())),
            "by_status": dict(sorted(crosscheck_statuses.items())),
            "sufficient_period_count": sum(
                row["verification_level"] == "sufficient" for row in crosscheck_rows
            ),
            "period_ids": sorted(int(row["period_id"]) for row in crosscheck_rows),
            "evidence_linked_count": sum(row["evidence_id"] is not None for row in crosscheck_rows),
            "coverage_proof_statuses": dict(
                sorted(Counter(str(row["coverage_proof_status"]) for row in crosscheck_rows).items())
            ),
        },
        "matches": {
            "count": len(match_rows),
            "by_level": dict(sorted(level_counts.items())),
            "group_keys": match_keys,
            "partition": match_partition,
        },
        "anomalies": {
            "count": len(anomaly_rows),
            "rule_ids": anomaly_rules,
        },
        "evidence": {
            "current_count": len(evidence_rows),
            "historical_count": int(historical_evidence),
            "by_kind": dict(sorted(evidence_kinds.items())),
            "key_kinds": sorted(
                kind for kind in ("line_item_source", "sheet_coverage_proof", "cross_check", "anomaly")
                if evidence_kinds.get(kind, 0)
            ),
        },
        "run_contract": {
            "schema_version": current_contract.components.get("schema_version")
            if current_contract else None,
            "run_id_present": bool(current_contract and current_contract.run_id),
            "signature_present": bool(current_contract and current_contract.signature),
        },
    }


def _run_inputs(root: Path, conn, project_id: int, inputs: list[dict[str, Any]], project_dir: Path) -> None:
    from jiadun.core.contracts import extract as contract_extract
    from jiadun.core.engine import settlement_io

    for entry in inputs:
        if not isinstance(entry, dict):
            raise GoldenRegistryError("黄金案例 input 必须是对象")
        path = _safe_repo_path(entry.get("path"), root)
        if not path.is_file():
            raise GoldenCaseExecutionError(f"黄金案例输入不存在：{entry.get('path')}")
        expected_hash = entry.get("sha256")
        if expected_hash:
            from jiadun.core.models.source_file import sha256_of

            actual_hash = sha256_of(path)
            if actual_hash != expected_hash:
                raise GoldenCaseExecutionError(
                    f"黄金案例输入 SHA-256 不一致：{entry.get('path')}"
                )
        file_type = str(entry.get("type") or path.suffix.lower().lstrip("."))
        if file_type == "xlsx":
            settlement_io.import_settlement_file(
                conn,
                project_id,
                project_dir,
                path,
                direction=str(entry.get("direction") or "unknown"),
            )
        elif file_type == "docx":
            contract_extract.import_contract(conn, project_id, project_dir, path)
        else:
            raise GoldenCaseExecutionError(f"黄金案例暂不支持输入类型：{file_type}")


def _execute_case(case: dict[str, Any], *, root: Path, work_root: Path) -> dict[str, Any]:
    case_id = str(case["case_id"])
    if case.get("availability") != "available":
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "not_available",
            "status": "not_available",
            "reason": case.get("reason") or "未登记可复核输入",
        }

    from jiadun.core.anomalies import engine as anomaly_engine
    from jiadun.core.engine import aggregate, crosscheck
    from jiadun.core.matching import matching
    from jiadun.core.models import project as project_model

    case_dir = work_root / case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    old_settings = project_model._SETTINGS_FILE
    project_model._SETTINGS_FILE = case_dir / "settings.json"
    conn = None
    try:
        info = project_model.create_project(str(case.get("project_name") or f"黄金回归-{case_id}"), case_dir / "projects")
        info, conn = project_model.open_project(Path(info.workspace_path))
        project_dir = Path(info.workspace_path)
        _run_inputs(root, conn, info.project_id, list(case["inputs"]), project_dir)

        # 聚合每个方向独立执行；多方向项目不得隐式合并。
        directions = [
            row["direction"]
            for row in conn.execute(
                "SELECT DISTINCT direction FROM settlement_periods WHERE project_id=? ORDER BY direction",
                (info.project_id,),
            ).fetchall()
        ]
        for direction in directions:
            aggs = aggregate.aggregate_project(conn, info.project_id, direction=direction)
            aggregate.persist_period_totals(conn, info.project_id, aggs)

        crosscheck.run_crosscheck_project(conn, info.project_id)
        anomaly_engine.run_anomalies(conn, info.project_id)
        groups = matching.match_items(conn, info.project_id)
        matching.save_matches(conn, info.project_id, groups)
        actual = _build_metrics(conn, info.project_id)
        expected = case["expected"]
        diffs = compare_metrics(actual, expected)
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "available",
            "status": "passed" if not diffs else "mismatch",
            "metrics": actual,
            "diffs": diffs,
            "workspace": str(case_dir),
        }
    except Exception as exc:  # noqa: BLE001 - 单案例隔离并保留可解释失败
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "available",
            "status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "workspace": str(case_dir),
        }
    finally:
        if conn is not None:
            conn.close()
        project_model._SETTINGS_FILE = old_settings


def _flatten(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key in sorted(value):
            next_path = f"{path}.{key}" if path else str(key)
            yield from _flatten(value[key], next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{path}[{index}]")
    else:
        yield path, value


def _diff_kind(path: str) -> str:
    lowered = path.lower()
    if "amount" in lowered or "count" in lowered or "rows" in lowered:
        return "numeric_or_amount"
    if "match" in lowered or "group" in lowered:
        return "matching"
    if "evidence" in lowered:
        return "evidence"
    if "anomal" in lowered or "rule" in lowered:
        return "anomaly"
    return "contract_or_scope"


def compare_metrics(actual: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    """逐字段比较规格，不允许通过写回规格来掩盖回归。"""
    actual_flat = dict(_flatten(actual))
    diffs: list[dict[str, Any]] = []
    for path, expected_value in _flatten(expected):
        actual_value = actual_flat.get(path, "__missing__")
        if actual_value != expected_value:
            diffs.append({
                "path": path,
                "kind": _diff_kind(path),
                "expected": expected_value,
                "actual": None if actual_value == "__missing__" else actual_value,
                "reason": "黄金结果字段发生变化，需核对输入、规则或运行契约后由人工决定是否更新",
            })
    return diffs


def run_golden_regression(
    registry: Path = DEFAULT_REGISTRY,
    *,
    output: Path | None = None,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    """运行注册案例并返回结构化报告；默认清理成功案例的临时工作区。"""
    registry = Path(registry).resolve()
    root = REPO_ROOT
    data = load_registry(registry)
    work_parent: tempfile.TemporaryDirectory[str] | None = None
    if output is None:
        work_parent = tempfile.TemporaryDirectory(prefix="jiadun-golden-")
        work_root = Path(work_parent.name)
    else:
        work_root = Path(output).resolve()
        work_root.mkdir(parents=True, exist_ok=True)
    try:
        results = [_execute_case(case, root=root, work_root=work_root) for case in data["cases"]]
        available = [result for result in results if result["availability"] == "available"]
        real_available = [result for result in available if result.get("case_kind") == "sanitized_real"]
        mismatches = [result for result in available if result["status"] != "passed"]
        return {
            "schema_version": 1,
            "registry": registry.relative_to(root).as_posix()
            if registry.is_relative_to(root) else registry.name,
            "status": "passed" if not mismatches else "failed",
            "case_count": len(results),
            "available_case_count": len(available),
            "real_case_count": len(real_available),
            "not_available_case_count": len(results) - len(available),
            "mismatch_case_count": len(mismatches),
            "results": results,
            "workspace": str(work_root) if output is not None or keep_workspace else None,
        }
    finally:
        if work_parent is not None and not keep_workspace:
            work_parent.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行价盾黄金回归库（只读比较，不自动更新）")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, help="保留本次运行工作区与 JSON 报告目录")
    parser.add_argument("--keep-workspace", action="store_true", help="保留临时工作区供复核")
    parser.add_argument("--require-real", action="store_true", help="无脱敏真实案例时返回失败")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_golden_regression(
            args.registry,
            output=args.output,
            keep_workspace=args.keep_workspace,
        )
    except (GoldenRegistryError, GoldenCaseExecutionError) as exc:
        print(f"黄金回归失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"黄金回归：{report['status']}；可用 {report['available_case_count']}；"
            f"脱敏真实 {report['real_case_count']}；差异案例 {report['mismatch_case_count']}"
        )
        for result in report["results"]:
            print(f"- {result['case_id']}: {result['status']}")
            for diff in result.get("diffs", [])[:20]:
                print(f"  · {diff['path']}: expected={diff['expected']!r}, actual={diff['actual']!r}")
    if report["status"] != "passed":
        return 1
    if args.require_real and report["real_case_count"] == 0:
        print("黄金回归未满足生产门槛：尚无脱敏真实案例。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
