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
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "tests" / "golden" / "cases.json"
DEFAULT_ANONYMIZED_REGISTRY = REPO_ROOT / "tests" / "anonymized_golden_cases" / "cases.json"
PRIVATE_PART = "local_private_data"
AMOUNT_SCALE = Decimal("0.01")
CANONICAL_COMPARISON_STATUSES = frozenset({"PASS", "FAIL", "PENDING", "INCOMPARABLE"})
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
REGISTRY_KINDS = frozenset({"synthetic_demo", "anonymized_real_project"})
SYNTHETIC_ASSET_PREFIX = "examples/demo/"
ANONYMIZED_ASSET_PREFIX = "tests/anonymized_golden_cases/assets/"


class GoldenRegistryError(ValueError):
    """黄金规格结构或路径不符合安全边界。"""


class GoldenCaseExecutionError(RuntimeError):
    """黄金案例执行失败。"""


def _is_private(relative: str) -> bool:
    parts = Path(relative).as_posix().strip("/").split("/")
    return PRIVATE_PART in parts


def _normalize_relative_path(relative: str) -> str:
    """将 registry 中的相对路径归一化为可审计的 POSIX 表达。"""
    if not isinstance(relative, str) or not relative.strip():
        raise GoldenRegistryError("黄金案例输入路径必须是非空相对路径")
    normalized = relative.replace("\\", "/")
    # ``Path`` 在 macOS/Linux 上不会把 ``C:\\...`` 识别为绝对路径；显式
    # 拒绝 Windows 盘符和 UNC 形式，避免跨平台 registry 绕过边界判断。
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("//"):
        raise GoldenRegistryError(f"黄金回归禁止使用绝对路径：{relative!r}")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _safe_repo_path(relative: str, root: Path) -> Path:
    normalized = _normalize_relative_path(relative)
    path = Path(normalized)
    if path.is_absolute() or _is_private(path.as_posix()):
        raise GoldenRegistryError(f"黄金回归禁止使用绝对路径或 local_private_data：{relative!r}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise GoldenRegistryError(f"黄金案例路径越过仓库根目录：{relative!r}") from exc
    return resolved


def _safe_asset_path(relative: str, root: Path, registry_kind: str) -> Path:
    """要求解析后的真实路径位于该 registry 的精确资产目录内。

    不能只用字符串 ``startswith``：``..`` 和指向目录外的软链接都会让
    原始字符串看似位于 assets 前缀、解析后却落到演示或私有目录。
    """
    normalized = _normalize_relative_path(relative)
    resolved = _safe_repo_path(normalized, root)
    asset_prefix = (
        SYNTHETIC_ASSET_PREFIX
        if registry_kind == "synthetic_demo"
        else ANONYMIZED_ASSET_PREFIX
    )
    asset_root = (root / asset_prefix.rstrip("/")).resolve()
    try:
        resolved.relative_to(asset_root)
    except ValueError as exc:
        raise GoldenRegistryError(
            f"{registry_kind} 黄金案例输入解析后必须位于 {asset_root}：{relative!r}"
        ) from exc
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
    registry_kind = data.get("registry_kind")
    if registry_kind not in REGISTRY_KINDS:
        raise GoldenRegistryError(
            "黄金规格 registry_kind 必须为 synthetic_demo 或 anonymized_real_project"
        )
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise GoldenRegistryError("黄金案例必须是对象")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise GoldenRegistryError(f"黄金案例 case_id 缺失或重复：{case_id!r}")
        seen.add(case_id)
        case_version = case.get("case_version")
        if not isinstance(case_version, str) or not case_version.strip():
            raise GoldenRegistryError(f"黄金案例 {case_id} 缺少 case_version")
        availability = case.get("availability")
        if availability not in {"available", "not_available"}:
            raise GoldenRegistryError(f"{case_id} availability 必须为 available/not_available")
        if availability == "not_available":
            reason = case.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise GoldenRegistryError(f"不可用黄金案例 {case_id} 必须填写非空 reason")
            continue
        if availability == "available":
            expected_kind = (
                "synthetic_demo"
                if registry_kind == "synthetic_demo"
                else "sanitized_real"
            )
            if case.get("case_kind") != expected_kind:
                raise GoldenRegistryError(
                    f"{registry_kind} 登记表中的 {case_id} case_kind 必须为 {expected_kind}"
                )
            if not isinstance(case.get("inputs"), list) or not case["inputs"]:
                raise GoldenRegistryError(f"可用黄金案例 {case_id} 缺少 inputs")
            if not isinstance(case.get("expected"), dict):
                raise GoldenRegistryError(f"可用黄金案例 {case_id} 缺少 expected 对象")
            if not case["expected"]:
                raise GoldenRegistryError(f"可用黄金案例 {case_id} expected 不能为空")
            if registry_kind == "anonymized_real_project":
                provenance = case.get("provenance")
                if not isinstance(provenance, dict):
                    raise GoldenRegistryError(f"真实黄金案例 {case_id} 缺少 provenance 对象")
                for key in ("authorized", "anonymized"):
                    if provenance.get(key) is not True:
                        raise GoldenRegistryError(
                            f"真实黄金案例 {case_id} provenance.{key} 必须明确为 true"
                        )
                for key in ("source_type", "verified_by", "verified_at", "verification_note"):
                    value = provenance.get(key)
                    if not isinstance(value, str) or not value.strip():
                        raise GoldenRegistryError(
                            f"真实黄金案例 {case_id} provenance.{key} 不得为空"
                        )
            for index, input_entry in enumerate(case["inputs"]):
                if not isinstance(input_entry, dict):
                    raise GoldenRegistryError(
                        f"可用黄金案例 {case_id} inputs[{index}] 必须是对象"
                    )
                source_path = input_entry.get("path")
                if not isinstance(source_path, str) or not source_path.strip():
                    raise GoldenRegistryError(
                        f"可用黄金案例 {case_id} inputs[{index}] 缺少 path"
                    )
                file_type = input_entry.get("type")
                if not isinstance(file_type, str) or file_type.lower() not in {"xlsx", "docx"}:
                    raise GoldenRegistryError(
                        f"可用黄金案例 {case_id} inputs[{index}] type 必须为 xlsx/docx"
                    )
                direction = input_entry.get("direction")
                if not isinstance(direction, str) or not direction.strip():
                    raise GoldenRegistryError(
                        f"可用黄金案例 {case_id} inputs[{index}] 缺少 direction"
                    )
                normalized_source_path = _normalize_relative_path(source_path)
                required_prefix = (
                    SYNTHETIC_ASSET_PREFIX
                    if registry_kind == "synthetic_demo"
                    else ANONYMIZED_ASSET_PREFIX
                )
                if not normalized_source_path.startswith(required_prefix):
                    raise GoldenRegistryError(
                        f"{registry_kind} 黄金案例 {case_id} 输入必须位于 {required_prefix}："
                        f"{source_path!r}"
                    )
                # 字符串前缀只是早期格式兼容提示；真实安全边界必须基于
                # resolve 后的目录 containment，并同时拦截目录外软链接。
                _safe_asset_path(source_path, REPO_ROOT, registry_kind)
                expected_hash = input_entry.get("sha256")
                if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
                    raise GoldenRegistryError(
                        f"可用黄金案例 {case_id} inputs[{index}] sha256 必须是 64 位十六进制字符串"
                    )
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
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise GoldenRegistryError(
                f"黄金案例输入 sha256 必须是 64 位十六进制字符串：{entry.get('path')}"
            )
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


def _matching_item_identity_map(conn, project_id: int) -> dict[int, str]:
    """为当前运行的清单行建立稳定源身份，拒绝使用临时自增 ID。

    ``line_items`` 只保存了 Sheet 外键和行级 flags；文件 SHA-256 与 Sheet
    名称从不可变原始层回读，正好对应 matching benchmark 的跨运行身份。
    缺少任一定位信息时不猜测，交给调用方把案例标为 INCOMPARABLE。
    """
    from scripts.matching_benchmark import stable_item_identity

    rows = conn.execute(
        """SELECT li.id, li.flags_json, sf.sha256, rs.sheet_name
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE sp.project_id=?
           ORDER BY li.id""",
        (int(project_id),),
    ).fetchall()
    result: dict[int, str] = {}
    for row in rows:
        try:
            flags = json.loads(row["flags_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise GoldenCaseExecutionError(
                f"清单行 {row['id']} 的来源定位 JSON 损坏，无法进行匹配 benchmark"
            ) from exc
        source_row = flags.get("row") if isinstance(flags, dict) else None
        result[int(row["id"])] = stable_item_identity(
            file_sha256=str(row["sha256"] or ""),
            sheet_name=str(row["sheet_name"] or ""),
            row=source_row,
        )
    return result


def _case_matching_benchmark(
    conn,
    project_id: int,
    groups: object,
    case: dict[str, Any],
) -> dict[str, Any] | None:
    """仅在案例明确登记 ``matching_truth`` 时执行匹配质量评价。"""
    if "matching_truth" not in case:
        return None
    from scripts.matching_benchmark import (
        evaluate_matching,
        prediction_groups_from_mapping,
    )

    identity_map = _matching_item_identity_map(conn, project_id)
    predictions = prediction_groups_from_mapping(groups, identity_map)
    return evaluate_matching(predictions, case.get("matching_truth"))


def _execute_case(case: dict[str, Any], *, root: Path, work_root: Path) -> dict[str, Any]:
    case_id = str(case["case_id"])
    if case.get("availability") != "available":
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "not_available",
            "status": "not_available",
            "comparison_status": "PENDING",
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
        matching_benchmark = _case_matching_benchmark(
            conn, info.project_id, groups, case
        )
        actual = _build_metrics(conn, info.project_id)
        expected = case["expected"]
        diffs = compare_metrics(actual, expected)
        if matching_benchmark is not None and matching_benchmark["status"] != "PASS":
            diffs.append({
                "path": "matching_benchmark",
                "kind": "matching",
                "expected": "PASS",
                "actual": matching_benchmark["status"],
                "reason": (
                    "匹配 benchmark 未达到 PASS；请优先复核误报项目对、漏匹配项目对、"
                    "人工标签和 Evidence"
                ),
            })
        comparison_status = "PASS" if not diffs else "FAIL"
        if matching_benchmark is not None and matching_benchmark["status"] == "INCOMPARABLE":
            comparison_status = "INCOMPARABLE"
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "available",
            "status": "passed" if not diffs else "mismatch",
            "comparison_status": comparison_status,
            "metrics": actual,
            "diffs": diffs,
            **({"matching_benchmark": matching_benchmark} if matching_benchmark is not None else {}),
            "workspace": str(case_dir),
        }
    except Exception as exc:  # noqa: BLE001 - 单案例隔离并保留可解释失败
        return {
            "case_id": case_id,
            "case_kind": case.get("case_kind"),
            "availability": "available",
            "status": "failed",
            # 没有可比的完整实际结果时不能伪装成金额/匹配 FAIL；
            # INCOMPARABLE 保留“执行失败/证据不足”的边界。
            "comparison_status": "INCOMPARABLE",
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
    """按闭世界逐字段比较规格，不允许静默减少或增加关键指标。

    旧实现只遍历 ``expected``，因此 expected 为空或 actual 新增指标时会
    错误返回“无差异”。黄金库是发布契约，双方字段集合必须完全一致；如
    需新增指标，应先补充说明和人工确认后的黄金值，而不是让比较器放行。
    """
    actual_flat = dict(_flatten(actual))
    expected_flat = dict(_flatten(expected))
    diffs: list[dict[str, Any]] = []
    if not expected_flat:
        return [{
            "path": "__expected__",
            "kind": "contract_or_scope",
            "expected": "non_empty_metrics",
            "actual": "empty",
            "reason": "黄金结果不能为空；缺少明确基线时禁止发布",
        }]
    for path, expected_value in expected_flat.items():
        actual_value = actual_flat.get(path, "__missing__")
        if actual_value != expected_value:
            diffs.append({
                "path": path,
                "kind": _diff_kind(path),
                "expected": expected_value,
                "actual": None if actual_value == "__missing__" else actual_value,
                "reason": "黄金结果字段发生变化，需核对输入、规则或运行契约后由人工决定是否更新",
            })
    for path, actual_value in actual_flat.items():
        if path in expected_flat:
            continue
        diffs.append({
            "path": path,
            "kind": _diff_kind(path),
            "expected": None,
            "actual": actual_value,
            "reason": "实际结果出现未登记的黄金指标，需先补充并人工确认黄金契约",
        })
    return diffs


def _overall_comparison_status(counts: dict[str, int]) -> str:
    """将案例级 canonical 状态汇总为当前回归状态。

    ``status`` 字段因兼容旧读取器仍保留 passed/failed 语义；所有新读取面应
    使用此字段，避免“有 PENDING 但旧 status=passed”造成绿色误读。
    """
    for status in ("INCOMPARABLE", "FAIL", "PENDING", "PASS"):
        if int(counts.get(status, 0)) > 0:
            return status
    return "INCOMPARABLE"


def normalize_comparison_status_counts(
    raw_counts: object,
    *,
    expected_total: object | None = None,
) -> tuple[dict[str, int], list[str]]:
    """严格校验黄金回归的 canonical 状态计数。

    发布门禁不能把非法类型、负数、未知状态或总数不一致静默转换为零；
    调用方应把返回的 ``errors`` 视为 ``INCOMPARABLE``/阻断证据。
    """
    errors: list[str] = []
    counts: dict[str, int] = {}
    if not isinstance(raw_counts, dict) or not raw_counts:
        return {"INCOMPARABLE": 1}, ["comparison_status_counts 缺失或不是对象"]
    for raw_status, raw_count in raw_counts.items():
        if not isinstance(raw_status, str):
            errors.append("comparison_status_counts 存在非字符串状态")
            continue
        status = raw_status.upper()
        if status not in CANONICAL_COMPARISON_STATUSES:
            errors.append(f"comparison_status_counts 存在未知状态 {raw_status!r}")
            continue
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count <= 0:
            errors.append(f"comparison_status_counts.{raw_status} 必须为正整数")
            continue
        counts[status] = counts.get(status, 0) + raw_count
    if not counts:
        counts = {"INCOMPARABLE": 1}
    if expected_total is not None:
        if isinstance(expected_total, bool) or not isinstance(expected_total, int) or expected_total < 0:
            errors.append("黄金案例总数必须为非负整数")
        elif sum(counts.values()) != expected_total:
            errors.append(
                "comparison_status_counts 总数与黄金案例总数不一致："
                f"counts={sum(counts.values())}, expected={expected_total}"
            )
    return dict(sorted(counts.items())), errors


def run_golden_regression(
    registry: Path = DEFAULT_REGISTRY,
    *,
    output: Path | None = None,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    """运行注册案例并返回结构化报告；默认清理成功案例的临时工作区。"""
    registry = Path(registry).resolve()
    root = REPO_ROOT
    try:
        data = load_registry(registry)
    except GoldenRegistryError as exc:
        # 直接调用该函数也必须返回可消费的失败报告；不能把路径越界、
        # 结构损坏等输入错误裸抛给 UI/发布清单，造成整批中断或误判。
        return {
            "schema_version": 1,
            "registry": registry.relative_to(root).as_posix()
            if registry.is_relative_to(root) else registry.name,
            "registry_kind": None,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "case_count": 1,
            "available_case_count": 0,
            "real_case_count": 0,
            "not_available_case_count": 0,
            "mismatch_case_count": 1,
            "comparison_status_counts": {"INCOMPARABLE": 1},
            "overall_comparison_status": "INCOMPARABLE",
            "results": [{
                "case_id": "<registry>",
                "availability": "available",
                "status": "failed",
                "comparison_status": "INCOMPARABLE",
                "error": f"{type(exc).__name__}: {exc}",
                "diffs": [],
            }],
            "workspace": None,
        }
    work_parent: tempfile.TemporaryDirectory[str] | None = None
    if output is None:
        work_parent = tempfile.TemporaryDirectory(prefix="jiadun-golden-")
        # macOS 系统 temp 前缀位于 symlink 之后，解析到真实路径避免 fail-closed 误拒。
        work_root = Path(work_parent.name).resolve()
    else:
        work_root = Path(output).resolve()
        work_root.mkdir(parents=True, exist_ok=True)
    try:
        results = [_execute_case(case, root=root, work_root=work_root) for case in data["cases"]]
        available = [result for result in results if result["availability"] == "available"]
        registry_kind = data.get("registry_kind")
        real_available = [
            result
            for result in available
            if registry_kind == "anonymized_real_project"
            and result.get("case_kind") == "sanitized_real"
        ]
        mismatches = [result for result in available if result["status"] != "passed"]
        comparison_status_counts = dict(
            sorted(
                Counter(
                    str(result.get("comparison_status") or "INCOMPARABLE").upper()
                    for result in results
                ).items()
            )
        )
        matching_benchmark_status_counts = dict(
            sorted(
                Counter(
                    str(result["matching_benchmark"].get("status") or "INCOMPARABLE")
                    for result in available
                    if result.get("matching_benchmark") is not None
                ).items()
            )
        )
        return {
            "schema_version": 1,
            "registry": registry.relative_to(root).as_posix()
            if registry.is_relative_to(root) else registry.name,
            "registry_kind": registry_kind,
            "status": "passed" if not mismatches else "failed",
            "case_count": len(results),
            "available_case_count": len(available),
            "real_case_count": len(real_available),
            "not_available_case_count": len(results) - len(available),
            "mismatch_case_count": len(mismatches),
            "comparison_status_counts": comparison_status_counts,
            "overall_comparison_status": _overall_comparison_status(comparison_status_counts),
            "matching_benchmark_status_counts": matching_benchmark_status_counts,
            "results": results,
            "workspace": str(work_root) if output is not None or keep_workspace else None,
        }
    finally:
        if work_parent is not None and not keep_workspace:
            work_parent.cleanup()


def run_golden_regression_suite(
    *,
    registries: tuple[Path, ...] | list[Path] | None = None,
    output: Path | None = None,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    """执行合成与脱敏真实黄金登记表，并汇总发布门槛。

    两个登记表都只读比较；任一可用案例失败都会让 suite 失败。脱敏真实
    案例当前可以是 ``not_available`` 占位，但该事实会保留在报告中，不能
    被合成案例数量冒充生产覆盖。
    """
    selected = tuple(Path(path) for path in (registries or (
        DEFAULT_REGISTRY, DEFAULT_ANONYMIZED_REGISTRY
    )))
    reports: list[dict[str, Any]] = []
    for index, registry in enumerate(selected, start=1):
        if not registry.is_file():
            reports.append({
                "registry": registry.name,
                "status": "failed",
                "error": f"黄金登记表不存在：{registry}",
                "available_case_count": 0,
                "real_case_count": 0,
                "not_available_case_count": 0,
                "mismatch_case_count": 0,
                "comparison_status_counts": {"INCOMPARABLE": 1},
                "overall_comparison_status": "INCOMPARABLE",
            })
            continue
        # suite 可能同时执行多个登记表；为每个登记表隔离工作区，避免
        # 同名案例、报告或导出物互相覆盖，且保留每份报告独立可复核。
        registry_output = (
            Path(output).resolve() / f"registry-{index}"
            if output is not None
            else None
        )
        try:
            report = run_golden_regression(
                registry, output=registry_output, keep_workspace=keep_workspace
            )
        except (GoldenRegistryError, GoldenCaseExecutionError) as exc:
            # 发布闸门读取外部/工作树登记表时，结构损坏必须变成可读的
            # INCOMPARABLE 证据，不能让 release checklist 直接 AttributeError
            # 或把缺失案例当成 PASS。
            reports.append({
                "registry": registry.name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "available_case_count": 0,
                "real_case_count": 0,
                "not_available_case_count": 0,
                "mismatch_case_count": 0,
                # 用一个不可比较占位与 canonical 计数保持闭世界一致；
                # 真实错误文本另行进入 comparison_status_errors。
                "case_count": 1,
                "comparison_status_counts": {"INCOMPARABLE": 1},
                "overall_comparison_status": "INCOMPARABLE",
            })
            continue
        reports.append(report)
    aggregate_counts: Counter[str] = Counter()
    aggregate_matching_counts: Counter[str] = Counter()
    aggregate_errors: list[str] = []
    for report in reports:
        if report.get("error"):
            aggregate_errors.append(
                f"{report.get('registry', '<unknown>')}: {report['error']}"
            )
        expected_total: object | None = report.get("case_count")
        if expected_total is None and (
            "available_case_count" in report or "not_available_case_count" in report
        ):
            try:
                expected_total = (
                    report.get("available_case_count", 0)
                    + report.get("not_available_case_count", 0)
                )
            except TypeError:
                expected_total = "invalid"
        counts, errors = normalize_comparison_status_counts(
            report.get("comparison_status_counts"), expected_total=expected_total
        )
        if errors:
            aggregate_errors.extend(
                f"{report.get('registry', '<unknown>')}: {error}" for error in errors
            )
            aggregate_counts["INCOMPARABLE"] += 1
            # 当前报告的 canonical 计数已经不可信；只保留一个结构化
            # INCOMPARABLE 占位，不再把畸形原计数二次累加。
            continue
        for status, count in counts.items():
            aggregate_counts[status] += count
        matching_counts = report.get("matching_benchmark_status_counts") or {}
        if not isinstance(matching_counts, dict):
            aggregate_errors.append(
                f"{report.get('registry', '<unknown>')}: matching benchmark 状态计数不是对象"
            )
            aggregate_matching_counts["INCOMPARABLE"] += 1
        else:
            for raw_status, raw_count in matching_counts.items():
                status = str(raw_status).upper()
                if status not in CANONICAL_COMPARISON_STATUSES or (
                    isinstance(raw_count, bool)
                    or not isinstance(raw_count, int)
                    or raw_count < 0
                ):
                    aggregate_errors.append(
                        f"{report.get('registry', '<unknown>')}: matching benchmark 状态计数非法"
                    )
                    aggregate_matching_counts["INCOMPARABLE"] += 1
                    continue
                if raw_count:
                    aggregate_matching_counts[status] += raw_count
    comparison_status_counts = dict(sorted(aggregate_counts.items()))
    return {
        "schema_version": 1,
        "status": (
            "passed"
            if all(report.get("status") == "passed" for report in reports) and not aggregate_errors
            else "failed"
        ),
        "registry_count": len(reports),
        "available_case_count": sum(int(report.get("available_case_count", 0)) for report in reports),
        "real_case_count": sum(int(report.get("real_case_count", 0)) for report in reports),
        "not_available_case_count": sum(
            int(report.get("not_available_case_count", 0)) for report in reports
        ),
        "mismatch_case_count": sum(int(report.get("mismatch_case_count", 0)) for report in reports),
        "comparison_status_counts": comparison_status_counts,
        "overall_comparison_status": _overall_comparison_status(comparison_status_counts),
        "matching_benchmark_status_counts": dict(sorted(aggregate_matching_counts.items())),
        "comparison_status_errors": aggregate_errors,
        "reports": reports,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行价盾黄金回归库（只读比较，不自动更新）")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, help="保留本次运行工作区与 JSON 报告目录")
    parser.add_argument("--keep-workspace", action="store_true", help="保留临时工作区供复核")
    parser.add_argument("--require-real", action="store_true", help="无脱敏真实案例时返回失败")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="canonical 比较状态不是 PASS 时返回失败（PENDING/INCOMPARABLE 不能放行）",
    )
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
            f"黄金回归当前比较状态：{report['overall_comparison_status']}；"
            f"兼容 status={report['status']}；可用 {report['available_case_count']}；"
            f"脱敏真实 {report['real_case_count']}；差异案例 {report['mismatch_case_count']}"
        )
        for result in report["results"]:
            print(
                f"- {result['case_id']}: "
                f"{result.get('comparison_status', result['status'])}"
            )
            for diff in result.get("diffs", [])[:20]:
                print(f"  · {diff['path']}: expected={diff['expected']!r}, actual={diff['actual']!r}")
    if args.require_real and report["real_case_count"] == 0:
        print("黄金回归未满足生产门槛：尚无脱敏真实案例。", file=sys.stderr)
        return 2
    if args.require_complete and report["overall_comparison_status"] != "PASS":
        print(
            "黄金回归未满足完整比较门槛："
            f"overall_comparison_status={report['overall_comparison_status']}。",
            file=sys.stderr,
        )
        return 3
    if report["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
