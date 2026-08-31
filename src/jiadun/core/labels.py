"""核心层可复用的业务展示标签。

核心计算和导出不能依赖 PySide6，但它们写入的异常说明、证据摘要仍然会
直接出现在业务成果中。因此方向标签在核心层统一定义，避免 UI、异常规则、
校核证据和 Excel 导出各自维护一套不一致的短词。
"""
DIRECTION_ZH = {
    "upward": "对上结算",
    "downward": "对下结算",
    "unknown": "未标记",
}


def direction_label(direction: str | None, fallback: str = "未标记") -> str:
    """将内部方向值转换为安全的业务显示文案。

    未知内部值不能回退为 ``upward``/``downward`` 或任意数据库字段，避免
    技术枚举泄露到普通 UI/成果列；高级详情仍可通过调用方单独保留原值。
    """
    if direction in DIRECTION_ZH:
        return DIRECTION_ZH[direction]
    return fallback
