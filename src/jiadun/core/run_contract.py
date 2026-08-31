"""Run Contract 兼容导入入口。

旧调用方仍可使用 ``jiadun.core.run_contract``；该名称与实际实现模块
保持同一模块对象，确保函数读取和 ``monkeypatch`` 注入不会因兼容层静态
重导出而分叉。
"""
from __future__ import annotations

import sys as _sys

from jiadun.core.contracts import run_contract as _implementation

# 让兼容导入和 ``jiadun.core.contracts.run_contract`` 指向同一个模块对象。
# 这样从任一入口替换属性，所有生产调用方都会观察到相同的替换。
_sys.modules[__name__] = _implementation
