"""全局唯一 ID。

Ray 里所有实体(Task / Object / Actor / Node / Worker / PlacementGroup)都用一个
**定长随机字节串**标识。这个设计有几个好处:

1. 无中心发号器 —— 任何进程都能本地生成 ID,不需要向 GCS 申请(避免单点瓶颈);
2. 可比较、可哈希 —— 能直接当 dict key、能排序;
3. 自带类型信息 —— Ray 在 ID 的**首字节**写入类型码,这样从一段 hex 就能看出
   这是 TaskID 还是 ObjectID。调试时非常有用:`ObjectRef(c8ef45cc...)` 一眼可辨。

mini-ray 沿用同样的设计,区别只在长度与类型码位置(见模块底部「与真实 Ray 的差异」)。

.. code-block:: text

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                    随机 12 字节(os.urandom)|   类型码 4 字节  |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
                                                  ^^^^ 人类可读的类型标签

类型码用 ASCII 便于肉眼识别,例如 ``b"TASK"`` / ``b"OBJ\0"`` / ``b"ACT\0"``。
"""

from __future__ import annotations

import os
from typing import Dict, Type

__all__ = [
    "UniqueID",
    "TaskID",
    "ObjectID",
    "ActorID",
    "JobID",
    "NodeID",
    "WorkerID",
    "PlacementGroupID",
    "FunctionID",
    "DriverID",
    "id_from_hex",
]

# 随机部分的字节数(总长 = random + 4 字节类型码)
_RANDOM_LEN = 12
_TOTAL_LEN = _RANDOM_LEN + 4

#: 类型码 -> 类。用于 :func:`id_from_hex` 反查具体类型。
_ID_TYPE_REGISTRY: Dict[bytes, Type["UniqueID"]] = {}


class UniqueID:
    """定长唯一 ID 的基类。

    子类只需覆盖 :attr:`_type_code`(4 字节)。实例不可变、可哈希、可排序,
    并且可以直接 pickle(跨进程传递时按值序列化)。
    """

    __slots__ = ("_bytes",)

    #: 4 字节类型码,由子类覆盖
    _type_code: bytes = b"\x00\x00\x00\x00"

    def __init__(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"{type(self).__name__} 需要 bytes,得到 {type(data).__name__}")
        data = bytes(data)
        if len(data) != _TOTAL_LEN:
            raise ValueError(
                f"{type(self).__name__} 需要 {_TOTAL_LEN} 字节,得到 {len(data)} 字节"
            )
        self._bytes = data

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_random(cls) -> "UniqueID":
        """本地生成一个新 ID(不需要任何中心节点参与)。"""
        return cls(os.urandom(_RANDOM_LEN) + cls._type_code)

    @classmethod
    def nil(cls) -> "UniqueID":
        """空 ID —— 用作「未设置」的哨兵值(Ray 里叫 NIL_ID)。

        注意**类型码保留**:``ObjectID.nil()`` 与 ``TaskID.nil()`` 不是同一个值
        (随机部分全零,但类型码不同),这样从 hex 仍能看出它是什么类型的 ID。
        """
        return cls(b"\x00" * _RANDOM_LEN + cls._type_code)

    @classmethod
    def from_hex(cls, hex_str: str) -> "UniqueID":
        """从 hex 字符串还原,自动还原成正确的子类。"""
        data = bytes.fromhex(hex_str)
        return id_from_hex(data)

    # ---------------------------------------------------------------- 访问
    def binary(self) -> bytes:
        """原始字节(RPC 传输用)。"""
        return self._bytes

    def hex(self) -> str:
        """32 字符的 hex 表示。"""
        return self._bytes.hex()

    def short(self) -> str:
        """前 8 个字符,用于日志。"""
        return self._bytes.hex()[:8]

    def is_nil(self) -> bool:
        """随机部分是否全零(类型码不计入 —— 见 :meth:`nil`)。"""
        return self._bytes[:_RANDOM_LEN] == b"\x00" * _RANDOM_LEN

    def __str__(self) -> str:
        return self.hex()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.short()})"

    def __eq__(self, other: object) -> bool:
        # 只比较类型码 + 字节:不同子类但字节相同的情况不存在,因为类型码在字节里
        return isinstance(other, UniqueID) and self._bytes == other._bytes

    def __lt__(self, other: "UniqueID") -> bool:
        return self._bytes < other._bytes

    def __hash__(self) -> int:
        return hash(self._bytes)

    def __reduce__(self):
        # 注意:这里必须走 id_from_hex,否则反序列化端的子类信息会丢
        return (id_from_hex, (self._bytes,))


class _TypedID(UniqueID):
    """通过 ``__init_subclass__`` 自动注册类型码的子类基类。"""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        code = cls._type_code
        if code == b"\x00\x00\x00\x00":
            return  # 抽象基类不注册
        existing = _ID_TYPE_REGISTRY.get(code)
        if existing is not None and existing is not cls:  # pragma: no cover - 防御
            raise ValueError(f"类型码 {code!r} 被 {existing.__name__} 与 {cls.__name__} 共用")
        _ID_TYPE_REGISTRY[code] = cls


class TaskID(_TypedID):
    """一个 task(远程函数调用)的 ID。"""

    _type_code = b"TASK"


class ObjectID(_TypedID):
    """一个对象(存在对象存储里的不可变值)的 ID。"""

    _type_code = b"OBJ\x00"


class ActorID(_TypedID):
    """一个 actor 实例的 ID。"""

    _type_code = b"ACT\x00"


class JobID(_TypedID):
    """一次 ``miniray.init()``(一个 job)的 ID。"""

    _type_code = b"JOB\x00"


class NodeID(_TypedID):
    """一个节点(raylet)的 ID。"""

    _type_code = b"NODE"


class WorkerID(_TypedID):
    """一个 worker 进程的 ID。"""

    _type_code = b"WRKR"


class PlacementGroupID(_TypedID):
    """一个放置组(placement group)的 ID。"""

    _type_code = b"PGRP"


class FunctionID(_TypedID):
    """一个被导出到 GCS 的远程函数的 ID(见 function_manager)。"""

    _type_code = b"FUNC"


class DriverID(_TypedID):
    """driver 进程的 ID。"""

    _type_code = b"DRVR"


def id_from_hex(data: bytes) -> UniqueID:
    """按类型码还原成具体 ID 子类。未知类型码时退化为基类。

    这是 :meth:`UniqueID.__reduce__` 的落地点 —— 保证 ``pickle.loads(pickle.dumps(x))``
    之后 ``type(x)`` 不变。
    """
    if len(data) != _TOTAL_LEN:
        raise ValueError(f"ID 长度必须是 {_TOTAL_LEN},得到 {len(data)}")
    cls = _ID_TYPE_REGISTRY.get(bytes(data[-4:]))
    if cls is None:
        # 未知类型码:不抛异常,退化为基础 UniqueID,保证前向兼容
        return UniqueID(data)
    return cls(data)


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异(诚实声明,教程正文会展开)
#
#   * 长度:Ray 的 ObjectID/TaskID 是 28 字节(尾部编码了 owner 地址等信息),
#     mini-ray 统一 16 字节 = 12 字节随机 + 4 字节类型码。
#   * 类型码位置:Ray 放在**首字节**,mini-ray 放在**尾部 4 字节**,可读性更好
#     (hex 末尾直接能看到 "TASK"/"OBJ" 这样的标签)。
#   * Ray 还有 ``ray.ObjectRef`` 这个「未来值的句柄」,它内部持有 ObjectID;
#     mini-ray 把这两者合并成了 :class:`miniray.object_ref.ObjectRef`
#     (同样的简化 Ray 早期版本也用过)。
# ---------------------------------------------------------------------------
