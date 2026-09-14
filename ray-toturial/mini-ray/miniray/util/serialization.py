"""``miniray.util.serialization`` —— 序列化钩子的 Ray 兼容路径。

对应 Ray 的 ``ray.util.serialization.register_serializer``:当你有**不可 pickle
的对象**(数据库连接句柄、C 扩展对象、第三方 SDK 客户端)要跨进程传时,
注册一对「怎么变成字节 / 怎么变回来」的函数,框架就会自动用它。

.. code-block:: python

    from miniray.util.serialization import register_serializer

    class Conn:                       # 假设它内部有 socket,不可 pickle
        def __init__(self, dsn): self.dsn = dsn
        def to_bytes(self): return self.dsn.encode()
        @staticmethod
        def from_bytes(raw): return Conn(raw.decode())

    register_serializer(Conn, lambda c: c.to_bytes(), Conn.from_bytes)

    @miniray.remote
    def query(conn):                  # 现在可以当参数传了
        return conn.dsn

注意:注册是**进程级**的。worker 是独立进程,所以要保证注册发生在 import 时
(比如放在模块顶层),这样 worker 反序列化时也会执行到注册代码。
"""

from __future__ import annotations

from ..serialization import (  # noqa: F401
    dumps,
    loads,
    register_object_ref_type,
    register_serializer,
)

__all__ = ["register_serializer", "dumps", "loads", "register_object_ref_type"]
