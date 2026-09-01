"""发布候选版本的一致性门槛测试。"""

import json
import subprocess
import sys
from pathlib import Path

from scripts import release_consistency_check


def test_release_documents_match_current_preview_version():
    result = release_consistency_check.check_release_consistency(
        Path(__file__).parents[2], expected_version="0.1.10"
    )
    assert result["ok"], result["issues"]


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
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    golden = next(item for item in payload["checks"] if item["name"] == "golden_regression")
    assert golden["passed"] is True
