"""函数注册表 —— 「用户脚本里的函数」怎么跑到 worker 进程里去。

这是分布式框架里最容易被忽略、又最关键的一环。Ray 的做法分三档:

1. **模块里的函数**:按引用传(``module.qualname``),worker ``import`` 一下就有;
2. **``__main__`` 里的函数/闭包/lambda**:按值传 —— 用 cloudpickle 把 code object、
   闭包 cell、被引用的全局变量一起打包。这是 cloudpickle 存在的唯一理由;
3. **导出一次,后面只传 ID**:函数体可能有几十 KB,每次提交任务都带一份太浪费。
   Ray 在 GCS 里维护函数表(driver 导出 → 拿到 FunctionID → worker 首次用到时
   按 ID 去取),worker 侧还有缓存。

mini-ray 三档全实现了。序列化层见 ``serialization.py``,这里是「导出/取回/缓存」。

.. code-block:: text

    driver                                  GCS               worker
    ──────                                  ───               ──────
    @miniray.remote
    def f(x): ...
    f.remote(1)
      └─ export(f) ──▶ sha1(blob) 去重 ──▶ function table
                                          (id → blob)
      └─ 提交任务(FunctionID, args) ─────────────────────────▶
                                                            get_function(id)
                                                            ◀── blob(首次)
                                                            loads(blob) → f
                                                            缓存,后续直接用
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .errors import MiniRayError
from .ids import FunctionID
from . import serialization

__all__ = ["FunctionDescriptor", "FunctionManager"]


@dataclass(frozen=True)
class FunctionDescriptor:
    """函数的「坐标」。任务消息里带的就是它(几十字节),不是函数体。"""

    function_id: str  # 16 字节 ID 的 hex
    module: str
    qualname: str

    def to_dict(self) -> Dict[str, str]:
        return {"function_id": self.function_id, "module": self.module, "qualname": self.qualname}

    @staticmethod
    def from_dict(data: Dict[str, str]) -> "FunctionDescriptor":
        return FunctionDescriptor(data["function_id"], data["module"], data["qualname"])

    def __str__(self) -> str:  # pragma: no cover - 展示
        return f"{self.module}.{self.qualname}#{self.function_id[:8]}"


class FunctionManager:
    """进程内的函数导出/查找。

    :param gcs_client: 只需要两个方法 ``export_function(blob)`` / ``get_function(fid)``
        —— 这样测试时可以塞一个假对象进来。
    """

    def __init__(self, gcs_client: Any) -> None:
        self._gcs = gcs_client
        self._lock = threading.RLock()
        #: sha1(blob) → FunctionDescriptor(同一个函数只导出一次)
        self._exported: Dict[str, FunctionDescriptor] = {}
        #: function_id → 函数对象(worker 侧缓存)
        self._cache: Dict[str, Callable] = {}

    # ---------------------------------------------------------------- 导出
    def export(self, func: Callable) -> FunctionDescriptor:
        """把一个函数导出到函数表,拿到 :class:`FunctionDescriptor`。

        去重键是「序列化后字节的 sha1」—— 和 Ray 一样用内容哈希,
        因为同一个函数可能被不同名字引用(装饰器、别名)。
        """
        blob = serialization.dumps(func)
        digest = hashlib.sha1(blob).hexdigest()
        with self._lock:
            cached = self._exported.get(digest)
            if cached is not None:
                return cached

        function_id = self._gcs.export_function(blob)
        descriptor = FunctionDescriptor(
            function_id=function_id,
            module=getattr(func, "__module__", "__main__") or "__main__",
            qualname=getattr(func, "__qualname__", getattr(func, "__name__", "?")),
        )
        with self._lock:
            self._exported[digest] = descriptor
            self._cache[function_id] = func  # 导出方本地当然有这个函数
        return descriptor

    # ---------------------------------------------------------------- 取回
    def get(self, descriptor: FunctionDescriptor) -> Callable:
        """按 descriptor 取回函数(带缓存)。worker 执行任务前调用。"""
        with self._lock:
            cached = self._cache.get(descriptor.function_id)
            if cached is not None:
                return cached

        blob = self._gcs.get_function(descriptor.function_id)
        if blob is None:
            raise MiniRayError(
                f"函数表里找不到 {descriptor}(id={descriptor.function_id[:8]})。"
                "通常说明 driver 进程已经退出,或者用了别的集群导出的 FunctionID。"
            )
        func = serialization.loads(blob)
        with self._lock:
            self._cache[descriptor.function_id] = func
        return func

    def num_exported(self) -> int:
        with self._lock:
            return len(self._exported)

    def num_cached(self) -> int:
        with self._lock:
            return len(self._cache)
