"""清单差异雷达（P1-01）。

差异雷达只读取已经落库的清单事实，使用 ``Decimal`` 进行金额影响计算，
并把待确认/不可比事项与“已确认净影响”严格分开。比较结果可以作为报告
数据直接消费，也可以通过 :mod:`jiadun.core.diff.radar` 绑定当前运行和
Evidence 后持久化。
"""

from jiadun.core.diff.radar import (
    CATEGORIES,
    CATEGORY_LABELS,
    DiffItem,
    DiffRadar,
    build_diff_radar,
    persist_diff_radar,
    read_current_diff_radar,
)

__all__ = [
    "CATEGORY_LABELS",
    "CATEGORIES",
    "DiffItem",
    "DiffRadar",
    "build_diff_radar",
    "persist_diff_radar",
    "read_current_diff_radar",
]
