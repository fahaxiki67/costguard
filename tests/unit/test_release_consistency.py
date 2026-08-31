"""发布候选版本的一致性门槛测试。"""

from pathlib import Path

from scripts import release_consistency_check


def test_release_documents_match_current_preview_version():
    result = release_consistency_check.check_release_consistency(
        Path(__file__).parents[2], expected_version="0.1.9"
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
