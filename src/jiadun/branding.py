"""价盾的产品命名常量。

平台、界面、打包和测试使用同一组品牌常量，避免再次出现产品 slug、应用
显示名、用户可见中文名和历史兼容标识彼此漂移的情况。核心结算计算不依赖
本模块，也不改变数据库 schema 或历史数据。
"""
from __future__ import annotations

# 机器可读的发行包、Python import 和 CLI slug。
PRODUCT_SLUG = "jiadun"
# 英文展示名和应用/DMG 基础名。
PRODUCT_NAME = "Jiadun"
# 中文用户可见产品名。
PRODUCT_DISPLAY_NAME = "价盾"
PRODUCT_DISPLAY_FULL = "Jiadun（价盾）"
ORGANIZATION_NAME = PRODUCT_DISPLAY_NAME

# 新安装优先使用的目录和资源命名。
CONFIG_DIR_NAME = "Jiadun"
WORKSPACE_DIR_NAME = "JiadunProjects"
RESOURCE_DIR_NAME = "jiadun_resources"
RUN_STATE_PREFIX = ".jiadun-run-state-"

# 仅用于旧安装和旧项目的只读发现/兼容读取；不得因品牌迁移删除或改写。
LEGACY_PRODUCT_SLUG = "costguard"
LEGACY_PRODUCT_NAME = "CostGuard"
LEGACY_CONFIG_DIR_NAME = "CostGuard"
LEGACY_WORKSPACE_DIR_NAME = "CostGuardProjects"
LEGACY_RESOURCE_DIR_NAME = "costguard_resources"
LEGACY_RUN_STATE_PREFIX = ".costguard-run-state-"

# 为避免 macOS 应用身份分裂，本轮继续使用既有 bundle 标识；它是兼容约束，
# 不是当前产品展示名。变更 identifier 需要单独的发布迁移和签名评估。
BUNDLE_IDENTIFIER = "io.github.fahaxiki67.costguard"
LEGACY_BUNDLE_IDENTIFIER = BUNDLE_IDENTIFIER

__all__ = [
    "BUNDLE_IDENTIFIER",
    "CONFIG_DIR_NAME",
    "LEGACY_BUNDLE_IDENTIFIER",
    "LEGACY_CONFIG_DIR_NAME",
    "LEGACY_PRODUCT_NAME",
    "LEGACY_PRODUCT_SLUG",
    "LEGACY_RESOURCE_DIR_NAME",
    "LEGACY_RUN_STATE_PREFIX",
    "LEGACY_WORKSPACE_DIR_NAME",
    "ORGANIZATION_NAME",
    "PRODUCT_DISPLAY_FULL",
    "PRODUCT_DISPLAY_NAME",
    "PRODUCT_NAME",
    "PRODUCT_SLUG",
    "RESOURCE_DIR_NAME",
    "RUN_STATE_PREFIX",
    "WORKSPACE_DIR_NAME",
]
