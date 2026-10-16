"""编码函数 —— 数值和字节之间的"翻译官"。

这个模块只有五六个函数,却有一个非常容易踩的点:**``hex`` 对整数用的是
"小端字节串"语义**,不是"把十进制数字写成十六进制"。

.. code-block:: text

   hex(255)
   ┌────────────────────────────────────────────────────────┐
   │ 255 的字面量类型是 UInt8(最小能装下它的类型)          │
   │ UInt8 → 1 个字节                                       │
   │ 小端写出来就是这一个字节: 0xFF                          │
   │ 十六进制大写 → "FF"                                    │
   └────────────────────────────────────────────────────────┘
   ⇒ hex(255) = 'FF'          ← 不是 '255' 的某种 16 进制

   同理:
   hex(toUInt16(256)) = '0001'    ← 小端!高位在后,低位在前
   hex(toUInt32(1))   = '01000000'
   hex('ab')          = '616263'  ← 字符串直接按字节转
   bin(1)             = '00000001'  ← 同样的字节,换成二进制形式
   bin(toUInt16(1))   = '0000000100000000'

**为什么是小端?** 因为 ``hex`` 的本意是"把这个值在内存里的字节打印出来",
而 x86 是小端。ClickHouse 沿用了这个"内存转储"语义,而不是"数值转十六进制",
所以 ``hex(256)`` 得到 ``'0001'`` 而不是常见的 ``'0100'``。
第 17 章讲二进制格式时会再遇到它。

.. note::

   ``hex`` 输出的是**大写**十六进制(``'FF'`` 不是 ``'ff'``),
   这与真实 ClickHouse 一致。想要小写,请自己 ``lower(hex(x))``。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Sequence

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .base import FunctionBase, register_function
from .hashing import input_bytes

__all__ = ["bytes_for_hex"]

_STRING = get_data_type("String")


class _EncodingFunction(FunctionBase):
    """逐行底座 —— ``hex`` 需要类型来决定"取几个字节"。"""

    def compute(self, value: Any, t: DataType) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute() not implemented")

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        col = args[0].column
        if col.is_const():
            col = col.convert_to_full_column()
        t = args[0].type
        out = self.get_return_type([a.type for a in args]).create_column()
        for i in range(rows):
            out.append(self.compute(col.get(i), t))
        return out


def bytes_for_hex(value: Any, t: DataType) -> bytes:
    """``hex`` / ``bin`` 用的字节视图 —— 就是"这个值在内存里的样子"。"""
    if t.is_string() or not t.is_numeric():
        # String / FixedString / Date / DateTime / UUID / IP … 都走类型自己的字节布局
        return input_bytes(value, t)
    return input_bytes(value, t)


class FunctionHex(_EncodingFunction):
    """``hex(x)`` —— 字节的高十六进制大写形式。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        return bytes_for_hex(value, t).hex().upper()


class FunctionUnhex(_EncodingFunction):
    """``unhex(s)`` —— 十六进制串还原成字节(String)。

    ``unhex`` 是 ``hex`` 的逆 —— 但**它逆不回来类型信息**:
    ``unhex(hex(toUInt32(1)))`` 得到的是 4 个字节的 String,
    不是 UInt32。这正是"String 是二进制安全的"这一条的用处。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        text = value if isinstance(value, str) else input_bytes(value, t).decode(
            "utf-8", "surrogateescape"
        )
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"unhex: cannot parse {text!r} as hexadecimal: {exc}",
            )


class FunctionBin(_EncodingFunction):
    """``bin(x)`` —— 字节的二进制形式(每个字节 8 位)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        return "".join(f"{b:08b}" for b in bytes_for_hex(value, t))


class FunctionUnbin(_EncodingFunction):
    """``unbin(s)`` —— 二进制串还原成字节。

    .. warning::

       ``unbin`` 每一段的长度**必须是 8 的倍数**,否则抛错而不是静默补齐
       —— 静默补齐会让 "0111" 变成 "\x70"(右对齐),用户拿到的是
       一个自己都不认识的字节。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        text = value if isinstance(value, str) else input_bytes(value, t).decode(
            "utf-8", "surrogateescape"
        )
        if len(text) % 8 != 0:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"unbin: the input length ({len(text)}) must be a multiple of 8",
            )
        try:
            return bytes(int(text[i : i + 8], 2) for i in range(0, len(text), 8))
        except ValueError as exc:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"unbin: {text!r} is not a binary string: {exc}",
            )


class FunctionBase64Encode(_EncodingFunction):
    """``base64Encode(s)`` —— 标准 base64(带 ``=`` 补齐)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        return base64.b64encode(input_bytes(value, t)).decode("ascii")


class FunctionBase64Decode(_EncodingFunction):
    """``base64Decode(s)`` —— 解码,非法输入**抛错**。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        text = value if isinstance(value, str) else input_bytes(value, t).decode(
            "utf-8", "surrogateescape"
        )
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"base64Decode: invalid base64 input: {exc}",
            )


class FunctionTryBase64Decode(_EncodingFunction):
    """``tryBase64Decode(s)`` —— 非法输入返回**空串**(不抛错)。

    ``base64Decode`` 与 ``tryBase64Decode`` 的差别只有这一点:一个抛错,
    一个给空串。真实 ClickHouse 里这两个函数存在的原因,就是
    "批量解码用户上传的数据时,你不想因为一行坏了就整条查询失败"。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, value: Any, t: DataType) -> Any:
        text = value if isinstance(value, str) else input_bytes(value, t).decode(
            "utf-8", "surrogateescape"
        )
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return ""


register_function("hex", FunctionHex,
                  aliases=["toHex"],
                  description="值的字节表示转成大十六进制(整数按小端)")
register_function("unhex", FunctionUnhex, description="十六进制串还原成字节")
register_function("bin", FunctionBin,
                  aliases=["toBin"],
                  description="值的字节表示转成二进制串")
register_function("unbin", FunctionUnbin, description="二进制串还原成字节")
register_function("base64Encode", FunctionBase64Encode,
                  description="base64 编码")
register_function("base64Decode", FunctionBase64Decode,
                  description="base64 解码(失败抛错)")
register_function("tryBase64Decode", FunctionTryBase64Decode,
                  description="base64 解码(失败返回空串)")
