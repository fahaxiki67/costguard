"""可追溯的本地验收运行包元数据。

运行包记录“基于哪一份源码、哪一组输入、哪一份运行契约、执行了哪些阶段、
产出了哪些文件”。它只读取 Git 和文件元数据，不修改工作区，不替用户创建
提交，也不把真实性未知的合成指标写成精确率/召回率。
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from costguard.core.contracts import run_contract
from costguard.core.db import migrations
from costguard.core.evidence.finding import canonical_json, stable_fingerprint

BUNDLE_VERSION = 1
GOLDEN_VECTOR_SHA256 = "19a471c29f5fe2208cb464e9743d475147b67e60e4f861ed30305147856ce84b"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False, text=False
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _excluded(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    return normalized == "local_private_data" or normalized.startswith("local_private_data/")


def _tracked_diff_hash(root: Path) -> str:
    # Git 的 patch 输出包含 tracked/untracked 之外的变更内容；只读获取，路径
    # 过滤用 pathspec，避免私密目录进入运行包。
    raw = _git(root, "diff", "--binary", "--", ".", ":!local_private_data")
    return _sha256_bytes(raw.encode("utf-8"))


def repository_state(root: Path) -> dict[str, Any]:
    """读取 HEAD、脏状态、跟踪差异和未跟踪关键文件哈希。"""
    root = Path(root).resolve()
    status_lines = [
        line for line in _git(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        if line and not _excluded(line[3:] if len(line) >= 4 else line)
    ]
    untracked: list[dict[str, Any]] = []
    for line in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        relative = line.strip()
        if not relative or _excluded(relative):
            continue
        path = root / relative
        if not path.is_file():
            continue
        untracked.append({
            "path": relative,
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        })
    untracked.sort(key=lambda item: item["path"])
    uv_lock = root / "uv.lock"
    return {
        "git_head": _git(root, "rev-parse", "HEAD").strip() or None,
        "dirty": bool(status_lines),
        "status_porcelain": status_lines,
        "tracked_diff_sha256": _tracked_diff_hash(root),
        "untracked_files": untracked,
        "uv_lock_sha256": _sha256_file(uv_lock) if uv_lock.is_file() else None,
    }


def _file_metadata(paths: list[Path] | tuple[Path, ...], root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in paths:
        path = Path(path)
        relative = _relative(path, root)
        if _excluded(relative):
            continue
        result.append({
            "path": relative,
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size if path.is_file() else None,
        })
    return result


def _rule_config_hash() -> str:
    try:
        config = run_contract._rule_config()
    except Exception as exc:  # 没有规则配置时显式保留不可用状态
        return stable_fingerprint({"error": type(exc).__name__})
    return stable_fingerprint(config)


def canonical_bundle_hash(bundle: dict[str, Any]) -> str:
    """计算不包含自身字段的运行包哈希，避免自引用。"""
    body = dict(bundle)
    integrity = body.pop("integrity", None)
    if isinstance(integrity, dict):
        body["integrity"] = {
            key: value for key, value in integrity.items() if key != "bundle_sha256"
        }
    return _sha256_bytes(canonical_json(body).encode("utf-8"))


def build_acceptance_bundle(
    *,
    run_id: str,
    repo_root: Path,
    input_paths: list[Path] | tuple[Path, ...],
    output_paths: list[Path] | tuple[Path, ...],
    stages: list[dict[str, Any]],
    run_contract_signature: str | None,
    config: dict[str, Any],
    truth_status: str = "not_available",
    truth_metrics: dict[str, Any] | None = None,
    unknown_metrics: list[str] | None = None,
) -> dict[str, Any]:
    """构造一份完整运行包；所有输入/输出均只记录哈希，不复制数据。"""
    root = Path(repo_root).resolve()
    repo = repository_state(root)
    matching_file = root / "src/costguard/core/matching/matching.py"
    golden_input = {
        "missing": None,
        "decimal": "10.00",
        "directions": ["upward", "downward"],
        "empty_is_not_zero": True,
    }
    golden_hash = stable_fingerprint(golden_input)
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": {
            "schema_version": migrations.LATEST_SCHEMA_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "repository": repo,
        "inputs": _file_metadata(list(input_paths), root),
        "configuration": {
            "run_contract_signature": run_contract_signature,
            "rule_config_hash": _rule_config_hash(),
            "matching_config_hash": _sha256_file(matching_file),
            "benchmark_config": config,
        },
        "stages": stages,
        "outputs": _file_metadata(list(output_paths), root),
        "truth": {
            "status": truth_status,
            "metrics": truth_metrics if truth_metrics is not None else {
                "precision": None,
                "recall": None,
                "f1": None,
            },
            "unknown_metrics": unknown_metrics or [
                "precision", "recall", "f1", "false_positive_rate", "false_negative_rate"
            ],
        },
        "golden_vector": {
            "name": "costguard-canonical-json-v1",
            "input": golden_input,
            "sha256": golden_hash,
            "expected_sha256": GOLDEN_VECTOR_SHA256,
            "matches_expected": golden_hash == GOLDEN_VECTOR_SHA256,
        },
        "integrity": {
            "excluded_paths": ["local_private_data/"],
            "bundle_sha256": None,
        },
    }
    bundle["integrity"]["bundle_sha256"] = canonical_bundle_hash(bundle)
    return bundle


__all__ = [
    "BUNDLE_VERSION",
    "GOLDEN_VECTOR_SHA256",
    "build_acceptance_bundle",
    "canonical_bundle_hash",
    "repository_state",
]
