"""哈希函数 —— 输出**字节**还是**数字**,决定了它们能不能跨系统对照。

这个模块里有两类函数,混用它们是最常见的错误来源:

.. code-block:: text

   ┌── 返回"密码学摘要列"(FixedString(N),原始字节) ──┐
   │ MD5 / SHA1 / SHA224 / SHA256 / SHA384 / SHA512    │
   │                                                    │
   │   MD5('') 的取值是 16 个字节:                      │
   │     d4 1d 8c 9d df 00 b2 04 e9 80 09 98 ec f8 42 7e │
   │   想看到十六进制要写 hex(MD5(''))                  │
   └────────────────────────────────────────────────────┘
   ┌── 返回整数 ──────────────────────────────────────┐
   │ sipHash64 / sipHash128 / xxHash64 / cityHash64 /   │
   │ halfMD5 / murmurHash* / javaHash / CRC32 …         │
   └────────────────────────────────────────────────────┘

.. warning::

   **mini-clickhouse 与真实 ClickHouse 在这里有一处"故意的"差异**:
   ``cityHash64`` 的真实实现是一个特定版本(1.0.2)的 CityHash,
   它的输出和任何其它版本都不一样。重新实现它既费事又极易错,
   所以 mini-clickhouse 把 ``cityHash64`` **映射到 SipHash64** ——
   分布质量一样好,但**输出值与真实 ClickHouse 不同**,
   千万不要拿它跨系统比对(教程里的函数表把它标了 ⚠️)。
   这一条和 ``minich/util/hashing.py`` 的模块说明是同一件事。

.. note::

   ``MD5`` 返回的是 ``FixedString(16)``(**原始字节**),不是 32 个字符的
   十六进制串 —— 这与真实 ClickHouse 完全一致(官方文档的措辞是
   "returns the resulting set of bytes as FixedString(N)")。
   所以本模块的 ``MD5('')`` 在 Python 层是 16 个字节,而
   ``hex(MD5('')) == 'd41d8cd98f00b204e9800998ecf8427e'``。
   我们的验证脚本对这条做了断言 —— 它是"实现真的是 MD5"的证据。
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Callable, Dict, Sequence

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from ..util.hashing import crc32, int_hash64, siphash128, siphash64, xxhash64
from .base import FunctionBase, register_function

__all__ = [
    "murmur2_32",
    "murmur2_64",
    "murmur3_32",
    "murmur3_128",
    "java_hash",
    "input_bytes",
]

_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")
_UINT128 = get_data_type("UInt128")
_INT32 = get_data_type("Int32")

_MASK64 = (1 << 64) - 1
_MASK32 = (1 << 32) - 1


# ══════════════════════════════════════════════════════════════════════════
# 输入 → 字节
# ══════════════════════════════════════════════════════════════════════════


def input_bytes(value: Any, t: DataType) -> bytes:
    """把列值转成 ClickHouse 哈希时实际吃进去的那串字节。

    .. code-block:: text

       String / FixedString   → 原始字节
       整数 / 浮点             → 按**类型宽度**的小端字节序
       Date                   → UInt16(天),小端
       Date32                 → Int32(天),小端
       DateTime               → UInt32(秒),小端
       DateTime64             → Int64(刻度),小端
       UUID                   → 16 字节
       IPv4 / IPv6            → 4 / 16 字节

    "按类型宽度"这一条很重要:``sipHash64(toUInt8(1))`` 与
    ``sipHash64(toUInt64(1))`` 是**两个不同的值** —— 因为一个哈希 1 个字节,
    另一个哈希 8 个字节。真实 ClickHouse 的行为完全一样。
    """
    if t.is_string():
        if isinstance(value, str):
            return value.encode("utf-8", "surrogateescape")
        return bytes(value)
    name = t.get_name()
    if name == "Date":
        return int(value).to_bytes(2, "little", signed=False)
    if name == "Date32":
        return int(value).to_bytes(4, "little", signed=True)
    if name == "DateTime":
        return int(value).to_bytes(4, "little", signed=False)
    if name.startswith("DateTime64"):
        return int(value).to_bytes(8, "little", signed=True)
    if name == "UUID":
        return int(value).to_bytes(16, "little", signed=False)
    if name == "IPv4":
        return int(value).to_bytes(4, "little", signed=False)
    if name == "IPv6":
        data = (
            value.encode("utf-8", "surrogateescape")
            if isinstance(value, str)
            else bytes(value)
        )
        return data.ljust(16, b"\x00")[:16]
    if t.is_numeric():
        if t.is_float():
            # 浮点必须按 **IEEE-754 的真实内存表示**取字节,不能 ``int(value)``
            # —— 那会把 1.1 和 1.9 都截断成整数 1(哈希碰撞),NaN/Inf 更是
            # 直接抛 ValueError。``struct`` 打包的小端字节与内存逐位一致。
            bits = getattr(t, "bits", 64)
            return struct.pack("<f" if bits == 32 else "<d", float(value))
        bits = getattr(t, "bits", None)
        if bits is None:
            # 没有显式宽度的数值类型(Bool / Decimal 等)保持原有的 64 位
            # 无符号表示 —— 它们不是本次要修的缺陷,不动以免引入回归。
            return int(value).to_bytes(8, "little", signed=False)
        # 整数要按**有无符号**正确编码:有符号类型走二进制补码,负数写成
        # 全部位为 1 的形态(``to_bytes(signed=False)`` 会抛 OverflowError)。
        signed = bool(getattr(t, "signed", False))
        mask = (1 << bits) - 1
        ival = int(value) & mask
        if signed and ival >= (1 << (bits - 1)):
            ival -= 1 << bits
        return ival.to_bytes(bits // 8, "little", signed=signed)
    return str(value).encode("utf-8", "surrogateescape")


# ══════════════════════════════════════════════════════════════════════════
# 注册底座
# ══════════════════════════════════════════════════════════════════════════


class _HashFunction(FunctionBase):
    """逐行底座 —— 哈希函数必须同时看到**值**和**类型**(类型决定取几个字节)。"""

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


def _register_hash(name: str, impl: Callable[[Any, DataType], Any], ret: DataType, *,
                   aliases: Sequence[str] = (), description: str = "") -> type:
    class _F(_HashFunction):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return ret

        def compute(self, value: Any, t: DataType) -> Any:
            return impl(value, t)

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, aliases=list(aliases), description=description)
    return _F


# ══════════════════════════════════════════════════════════════════════════
# SipHash / xxHash / CityHash
# ══════════════════════════════════════════════════════════════════════════

_register_hash(
    "sipHash64", lambda v, t: siphash64(input_bytes(v, t)), _UINT64,
    description="SipHash-2-4 的 64 位输出(ClickHouse 的默认字符串哈希)",
)
_register_hash(
    "sipHash128", lambda v, t: siphash128(input_bytes(v, t)), _UINT128,
    description="SipHash 的 128 位变体(两个 64 位值拼成)",
)
_register_hash(
    "xxHash64", lambda v, t: xxhash64(input_bytes(v, t)), _UINT64,
    description="xxHash64(与官方实现逐位一致)",
)


class FunctionCityHash64(_HashFunction):
    """``cityHash64(x)`` —— **mini-clickhouse 用 SipHash64 顶替**。

    .. warning::

       这是一个**刻意的**不一致:真实 ClickHouse 用的是 CityHash 1.0.2,
       输出与任何其它 CityHash 版本都不同。mini-clickhouse 直接返回
       ``sipHash64(x)`` —— 分布性质一样好,但**值完全不同**。
       如果你需要与真实 ClickHouse 对齐,请改用 ``sipHash64`` 或
       ``xxHash64``(这两个我们是逐位正确的)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def compute(self, value: Any, t: DataType) -> Any:
        return siphash64(input_bytes(value, t))


register_function("cityHash64", FunctionCityHash64,
                  description="⚠️ 用 SipHash64 顶替,输出与真实 ClickHouse 不同")


# ══════════════════════════════════════════════════════════════════════════
# 密码学摘要(FixedString 原始字节)
# ══════════════════════════════════════════════════════════════════════════


class _DigestFunction(_HashFunction):
    """``MD5`` / ``SHA*`` 的公共实现 —— 返回**原始摘要字节**。

    真实 ClickHouse 的返回类型是 ``FixedString(N)``(N = 摘要长度),
    我们照做。文本格式下它是"一串二进制",这也解释了为什么大家
    写惯了 ``hex(MD5(...))``。
    """

    algorithm = "md5"
    digest_size = 16

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type(f"FixedString({self.digest_size})")

    def compute(self, value: Any, t: DataType) -> Any:
        return hashlib.new(self.algorithm, input_bytes(value, t)).digest()


def _register_digest(name: str, algorithm: str, size: int) -> None:
    cls = type(
        "Function" + name,
        (_DigestFunction,),
        {"algorithm": algorithm, "digest_size": size},
    )
    # 注册表本身大小写不敏感(键统一小写),所以 'MD5' 与 'md5' 自动互通
    register_function(name, cls, description=f"{algorithm.upper()} 摘要(原始 {size} 字节)")


_register_digest("MD5", "md5", 16)
_register_digest("SHA1", "sha1", 20)
_register_digest("SHA224", "sha224", 28)
_register_digest("SHA256", "sha256", 32)
_register_digest("SHA384", "sha384", 48)
_register_digest("SHA512", "sha512", 64)


class FunctionHalfMD5(_HashFunction):
    """``halfMD5(x)`` —— 取 MD5 的**前 8 字节**按**大端**解释成 ``UInt64``。

    注意这里是**大端**:ClickHouse 为了让它看起来像"MD5 的前半段十六进制"，
    特意用了 big-endian。所以
    ``hex(halfMD5(''))`` 会以 ``d41d8cd98f00b204`` 开头。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def compute(self, value: Any, t: DataType) -> Any:
        digest = hashlib.md5(input_bytes(value, t)).digest()
        return int.from_bytes(digest[:8], "big")


register_function("halfMD5", FunctionHalfMD5,
                  description="MD5 前 8 字节的大端解释")


# ══════════════════════════════════════════════════════════════════════════
# MurmurHash2 / MurmurHash3 / javaHash / CRC
# ══════════════════════════════════════════════════════════════════════════


def murmur2_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash2(32 位)—— Austin Appleby 的原始版本。"""
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = (seed ^ length) & _MASK32
    i = 0
    while length - i >= 4:
        k = int.from_bytes(data[i : i + 4], "little")
        k = (k * m) & _MASK32
        k ^= k >> r
        k = (k * m) & _MASK32
        h = (h * m) & _MASK32
        h ^= k
        i += 4
    tail = data[i:]
    if len(tail) == 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if len(tail) >= 1:
        h ^= tail[0]
        h = (h * m) & _MASK32
    h ^= h >> 13
    h = (h * m) & _MASK32
    h ^= h >> 15
    return h & _MASK32


def murmur2_64(data: bytes, seed: int = 0) -> int:
    """MurmurHash64A(64 位,``m = 0xc6a4a7935bd1e995``)。"""
    m = 0xC6A4A7935BD1E995
    r = 47
    length = len(data)
    h = (seed ^ ((length * m) & _MASK64)) & _MASK64
    i = 0
    while length - i >= 8:
        k = int.from_bytes(data[i : i + 8], "little")
        k = (k * m) & _MASK64
        k ^= k >> r
        k = (k * m) & _MASK64
        h ^= k
        h = (h * m) & _MASK64
        i += 8
    tail = data[i:]
    for j in range(len(tail) - 1, -1, -1):
        h ^= tail[j] << (8 * j)
        if j == 0:
            h = (h * m) & _MASK64
    h ^= h >> r
    h = (h * m) & _MASK64
    h ^= h >> r
    return h & _MASK64


def _rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & _MASK32


def _rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _MASK64


def _fmix32(h: int) -> int:
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _MASK32
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _MASK32
    h ^= h >> 16
    return h


def _fmix64(h: int) -> int:
    h ^= h >> 33
    h = (h * 0xFF51AFD7ED558CCD) & _MASK64
    h ^= h >> 33
    h = (h * 0xC4CEB9FE1A85EC53) & _MASK64
    h ^= h >> 33
    return h


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3 x86_32(返回**无符号** 32 位;ClickHouse 的 ``murmurHash3_32`` 是有符号 ``Int32``)。"""
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & _MASK32
    i = 0
    n = len(data)
    while n - i >= 4:
        k = int.from_bytes(data[i : i + 4], "little")
        k = (k * c1) & _MASK32
        k = _rotl32(k, 15)
        k = (k * c2) & _MASK32
        h ^= k
        h = _rotl32(h, 13)
        h = (h * 5 + 0xE6546B64) & _MASK32
        i += 4
    tail = data[i:]
    k = 0
    if len(tail) == 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & _MASK32
        k = _rotl32(k, 15)
        k = (k * c2) & _MASK32
        h ^= k
    h ^= n
    return _fmix32(h) & _MASK32


def murmur3_128(data: bytes, seed: int = 0) -> int:
    """MurmurHash3 x64_128,返回**低 64 位拼高 64 位**的 128 位整数。"""
    h1 = h2 = seed
    c1, c2 = 0x87C37B91114253D5, 0x4CF5AD432745937F
    n = len(data)
    i = 0
    while n - i >= 16:
        k1 = int.from_bytes(data[i : i + 8], "little")
        k2 = int.from_bytes(data[i + 8 : i + 16], "little")
        k1 = (k1 * c1) & _MASK64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * c2) & _MASK64
        h1 ^= k1
        h1 = _rotl64(h1, 27)
        h1 = (h1 + h2) & _MASK64
        h1 = (h1 * 5 + 0x52DCE729) & _MASK64
        k2 = (k2 * c2) & _MASK64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * c1) & _MASK64
        h2 ^= k2
        h2 = _rotl64(h2, 31)
        h2 = (h2 + h1) & _MASK64
        h2 = (h2 * 5 + 0x38495AB5) & _MASK64
        i += 16
    tail = data[i:]
    k1 = k2 = 0
    for j in range(len(tail) - 1, -1, -1):
        if j >= 8:
            k2 ^= tail[j] << (8 * (j - 8))
        else:
            k1 ^= tail[j] << (8 * j)
    if len(tail) > 8:
        k2 = (k2 * c2) & _MASK64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * c1) & _MASK64
        h2 ^= k2
    if len(tail) > 0:
        k1 = (k1 * c1) & _MASK64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * c2) & _MASK64
        h1 ^= k1
    h1 ^= n
    h2 ^= n
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64
    h1 = _fmix64(h1)
    h2 = _fmix64(h2)
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64
    return (h2 << 64) | h1


def _signed32(v: int) -> int:
    v &= _MASK32
    return v - (1 << 32) if v >= (1 << 31) else v


def java_hash(data: bytes) -> int:
    """Java ``String.hashCode`` 的**按字节**版本。

    Java 本身是对 UTF-16 码元做 ``h = 31h + c``;ClickHouse 明确
    "从**字节**算",所以非 ASCII 输入的结果会和 Java 不一样 ——
    这是真实 ClickHouse 的行为,不是我们的偏差。
    """
    h = 0
    for b in data:
        h = (h * 31 + b) & _MASK32
    return _signed32(h)


def _as_signed64(v: int) -> int:
    v &= _MASK64
    return v - (1 << 64) if v >= (1 << 63) else v


_register_hash("murmurHash2_32", lambda v, t: murmur2_32(input_bytes(v, t)),
               _UINT32, description="MurmurHash2 32 位")
_register_hash("murmurHash2_64", lambda v, t: murmur2_64(input_bytes(v, t)),
               _UINT64, description="MurmurHash64A")
_register_hash("murmurHash3_32", lambda v, t: _signed32(murmur3_32(input_bytes(v, t))),
               _INT32, description="MurmurHash3 x86_32(有符号)")
_register_hash("murmurHash3_64", lambda v, t: murmur3_128(input_bytes(v, t)) & _MASK64,
               _UINT64, description="MurmurHash3 x64_128 的低 64 位")
_register_hash("murmurHash3_128", lambda v, t: murmur3_128(input_bytes(v, t)),
               _UINT128, description="MurmurHash3 x64_128")


class FunctionGccMurmurHash(_HashFunction):
    """``gccMurmurHash(x)`` —— GCC ``std::hash`` 用的 MurmurHash64A。

    GCC 在实现 ``std::hash<std::string>`` 时用的是
    ``MurmurHash64A(data, len, 0xc70f6907)`` —— 那个种子是
    "c70f6907" 这个魔数。mini-clickhouse 照抄这个种子。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def compute(self, value: Any, t: DataType) -> Any:
        return murmur2_64(input_bytes(value, t), 0xC70F6907)


register_function("gccMurmurHash", FunctionGccMurmurHash,
                  description="GCC std::hash 用的 MurmurHash64A(种子 0xc70f6907)")


def _to_signed(value: Any) -> int:
    """整数参数的"按类型回绕"—— ``Int8(-1)`` 要真的变成 -1。"""
    return int(value)


class FunctionJavaHash(_HashFunction):
    """``javaHash(x)`` —— 与 Java 的 hashCode 同源(按字节)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT32

    def compute(self, value: Any, t: DataType) -> Any:
        if t.is_string():
            return java_hash(input_bytes(value, t))
        v = _to_signed(value)
        h = 0
        for b in input_bytes(v, t):
            h = (h * 31 + b) & _MASK32
        return _signed32(h)


class FunctionHiveHash(FunctionJavaHash):
    """``hiveHash(x)`` —— 与 ``javaHash`` 相同,但把符号位清零(恒非负)。"""

    def compute(self, value: Any, t: DataType) -> Any:
        return super().compute(value, t) & 0x7FFFFFFF


register_function("javaHash", FunctionJavaHash,
                  aliases=["javaHashUTF16LE"],
                  description="Java String.hashCode 的按字节版本")
register_function("hiveHash", FunctionHiveHash,
                  description="javaHash 的清符号位版本(非负)")


# ══════════════════════════════════════════════════════════════════════════
# 整数哈希与 CRC
# ══════════════════════════════════════════════════════════════════════════


class FunctionIntHash(_HashFunction):
    """``intHash32(x)`` / ``intHash64(x)`` —— 对**整数**做一次混淆混合。

    .. warning::

       真实 ClickHouse 的 ``intHash64`` 是 ``x ^ (x >> 32)`` 这类
       "跨类型一致"的混合(目的是让 ``Int32`` 与 ``Int64`` 的同一个值
       哈希到同一处)。mini-clickhouse 用的是 ``util/hashing.int_hash64``
       里的 **splitmix64 变体** —— 分布更好,但**值与真实 CH 不同**。
       ``intHash32`` 取它的高 32 位。
    """

    bits = 64

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64 if self.bits == 64 else _UINT32

    def compute(self, value: Any, t: DataType) -> Any:
        h = int_hash64(int(value))
        return h if self.bits == 64 else (h >> 32) & _MASK32


class FunctionIntHash32(FunctionIntHash):
    bits = 32


register_function("intHash32", FunctionIntHash32,
                  description="整数哈希的 32 位版本(intHash64 的高 32 位)")
register_function("intHash64", FunctionIntHash,
                  description="整数哈希(存在与真实 CH 不同的偏差)")


class FunctionCRC32(_HashFunction):
    """``CRC32`` / ``CRC32IEEE`` —— 标准 CRC-32(与 ``zlib.crc32`` 一致)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT32

    def compute(self, value: Any, t: DataType) -> Any:
        return crc32(input_bytes(value, t))


register_function("CRC32", FunctionCRC32, aliases=["CRC32IEEE"],
                  description="标准 CRC-32")


class FunctionCRC64(_HashFunction):
    """``CRC64`` —— **未实现**。

    ClickHouse 的 ``CRC64`` 用的是 ECMA-182 多项式。mini-clickhouse
    没实现它 —— 与其给一个"看起来像 CRC64"但值不对的实现,不如明确报错,
    否则用户会拿它去校验外部数据、得到一个静默的错误结论。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "CRC64 is not implemented in mini-clickhouse (real ClickHouse uses the "
            "ECMA-182 polynomial); use CRC32 instead",
        )


register_function("CRC64", FunctionCRC64,
                  description="CRC-64/ECMA-182(未实现)")
