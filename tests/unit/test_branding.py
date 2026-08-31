"""价盾品牌迁移的包、资源、目录和兼容标识契约。"""

from __future__ import annotations

from jiadun import branding
from jiadun.ui.workbench import _export_files


def test_current_branding_contract():
    assert branding.PRODUCT_SLUG == "jiadun"
    assert branding.PRODUCT_NAME == "Jiadun"
    assert branding.PRODUCT_DISPLAY_NAME == "价盾"
    assert branding.CONFIG_DIR_NAME == "Jiadun"
    assert branding.WORKSPACE_DIR_NAME == "JiadunProjects"
    assert branding.RESOURCE_DIR_NAME == "jiadun_resources"
    assert branding.RUN_STATE_PREFIX == ".jiadun-run-state-"


def test_legacy_branding_is_explicit_and_non_destructive():
    assert branding.LEGACY_PRODUCT_SLUG == "costguard"
    assert branding.LEGACY_CONFIG_DIR_NAME == "CostGuard"
    assert branding.LEGACY_WORKSPACE_DIR_NAME == "CostGuardProjects"
    assert branding.LEGACY_RUN_STATE_PREFIX == ".costguard-run-state-"
    # macOS identity remains the legacy identifier during this display-name migration.
    assert branding.BUNDLE_IDENTIFIER == "io.github.fahaxiki67.costguard"


def test_export_listing_keeps_legacy_files_visible(tmp_path):
    """品牌迁移不得让旧成果从项目导出卡片中消失。"""
    old_excel = tmp_path / "CostGuard审核底稿_20260830.xlsx"
    new_excel = tmp_path / "价盾审核底稿_20260831.xlsx"
    old_docx = tmp_path / "CostGuard管理层摘要_20260830.docx"
    for path in (old_excel, new_excel, old_docx):
        path.write_bytes(b"synthetic")

    assert set(_export_files(tmp_path, "excel")) == {old_excel, new_excel}
    assert _export_files(tmp_path, "docx") == [old_docx]
