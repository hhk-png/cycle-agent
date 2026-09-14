"""内部模块(对应真实 Ray 的 ``ray._private``)。

这些 API **不保证稳定**,只给测试、教学演示和调试用。写业务代码时请只用
``miniray`` 顶层的公共 API。
"""

from __future__ import annotations

__all__ = ["fault_injection", "debug"]
