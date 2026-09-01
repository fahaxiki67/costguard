"""发布候选版本的一致性门槛测试。"""

import json
import subprocess
import sys
from pathlib import Path

from scripts import release_consistency_check


def test_release_documents_without_real_golden_case_are_blocked_by_default():
    result = release_consistency_check.check_release_consistency(
        Path(__file__).parents[2], expected_version="0.1.13"
    )
    assert not result["ok"], result
    golden = next(item for item in result["checks"] if item["name"] == "golden_regression")
    assert golden["passed"] is False
    assert golden["status"] == "blocked"
    assert "real_case_count=0" in golden["detail"]
    assert any("真实黄金案例" in issue for issue in result["issues"])


def test_release_consistency_allows_no_real_only_with_explicit_development_flag():
    result = release_consistency_check.check_release_consistency(
        Path(__file__).parents[2],
        expected_version="0.1.13",
        allow_no_real=True,
    )
    assert result["ok"], result
    assert result["production_release_ready"] is False
    golden = next(item for item in result["checks"] if item["name"] == "golden_regression")
    assert golden["passed"] is True
    assert golden["status"] == "conditional"
    assert "allow_no_real" in golden["detail"]


def test_release_check_detects_version_mismatch(tmp_path):
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[project]\nname = "jiadun"\nversion = "0.1.6"\n', encoding="utf-8"
    )
    result = release_consistency_check.check_release_consistency(root, expected_version="0.1.9")
    assert not result["ok"]
    assert any("源码版本" in issue for issue in result["issues"])


def test_release_consistency_cli_can_import_golden_runner_from_scripts_directory():
    """直接执行脚本时也必须走到黄金回归，而不是因 sys.path 失败。"""
    root = Path(__file__).parents[2]
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "release_consistency_check.py"), "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    golden = next(item for item in payload["checks"] if item["name"] == "golden_regression")
    assert golden["passed"] is False
    assert golden["status"] == "blocked"

    allowed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "release_consistency_check.py"),
            "--json",
            "--allow-no-real",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    allowed_payload = json.loads(allowed.stdout)
    allowed_golden = next(
        item for item in allowed_payload["checks"] if item["name"] == "golden_regression"
    )
    assert allowed_golden["passed"] is True
    assert allowed_golden["status"] == "conditional"
