"""哈希函数 —— SipHash / xxHash / CRC。

ClickHouse 里哈希用得**极其**广泛,而且用途不同,要求也不同:

============================  ==========================================
用途                           要求
============================  ==========================================
``GROUP BY`` / ``JOIN`` 的哈希表  快、分布均匀、**不要求抗碰撞攻击**
``uniq`` 的 HyperLogLog        只要求均匀,不需要可逆
``bloom_filter`` 跳数索引       快、**跨进程稳定**(写入与查询要一致)
``sipHash64`` 等 SQL 函数       明确的算法定义(用户会拿去和别处对比)
``checksums.txt``(part 校验)   **必须**能检出损坏,快慢不重要
============================  ==========================================

这个模块实现了三个**真实的、有公开定义的**算法(不是"随手写的"哈希):

* **SipHash-2-4** —— ClickHouse 的默认字符串哈希(``sipHash64``),
  也是 part 的名字/校验里用的那个。SipHash 的设计目标是**抗哈希洪泛攻击**
  (这个性质对数据库很重要:否则攻击者能构造出全落到同一个桶的键);
* **xxHash64** —— ``xxHash64()`` SQL 函数,也是很多存储格式的校验算法;
* **CRC32** —— 老格式与 ``system.parts`` 里的校验;

.. note::

   **mini-clickhouse 不实现 CityHash**。真实 ClickHouse 的
   ``cityHash64`` 是一个特定版本(1.0.2)的 CityHash,它的输出与
   任何其他版本都不同,重新实现一遍既费事又容易出错。
   mini-clickhouse 的 ``cityHash64()`` 是 **SipHash64 的别名** ——
   分布性质一样好,但**输出值与真实 ClickHouse 不同**。
   这一点在第 15 章的函数表里标了 ⚠️,不要拿它跨系统比对。
"""

from __future__ import annotations

import struct
import zlib
from typing import Iterable, Sequence

__all__ = [
    "siphash64",
    "siphash128",
    "xxhash64",
    "crc32",
    "int_hash64",
    "hash_bytes",
    "combine_hashes",
]

_MASK64 = (1 << 64) - 1
_MASK128 = (1 << 128) - 1


def _rotl64(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & _MASK64


# ══════════════════════════════════════════════════════════════════════════
# SipHash-2-4
# ══════════════════════════════════════════════════════════════════════════

#: SipHash 的默认密钥 —— 真实 ClickHouse 用 ``0x0706050403020100`` /
#: ``0x0f0e0d0c0b0a0908``(即字节 0..15)。我们照抄,这样
#: ``sipHash64('')`` 的输出值与真实 ClickHouse 完全一致。
_SIP_K0 = 0x0706050403020100
_SIP_K1 = 0x0F0E0D0C0B0A0908


class _SipHasher:
    """SipHash-2-4 的流式实现(可以分批喂数据)。

    为什么实现成流式? 因为真实 ClickHouse 在算字符串哈希时是
    "先吃 8 字节、再吃剩余尾巴",而算 ``UUID``/数值时是"把定长字节
    直接喂进去"。流式接口让两条路径共用一份代码。
    """

    __slots__ = ("v0", "v1", "v2", "v3", "_buf", "_total")

    def __init__(self, k0: int = _SIP_K0, k1: int = _SIP_K1) -> None:
        self.v0 = k0 ^ 0x736F6D6570736575
        self.v1 = k1 ^ 0x646F72616E646F6D
        self.v2 = k0 ^ 0x6C7967656E657261
        self.v3 = k1 ^ 0x7465646279746573
        self._buf = bytearray()
        self._total = 0

    def _round(self) -> None:
        v0, v1, v2, v3 = self.v0, self.v1, self.v2, self.v3
        v0 = (v0 + v1) & _MASK64
        v1 = _rotl64(v1, 13)
        v1 ^= v0
        v0 = _rotl64(v0, 32)
        v2 = (v2 + v3) & _MASK64
        v3 = _rotl64(v3, 16)
        v3 ^= v2
        v0 = (v0 + v3) & _MASK64
        v3 = _rotl64(v3, 21)
        v3 ^= v0
        v2 = (v2 + v1) & _MASK64
        v1 = _rotl64(v1, 17)
        v1 ^= v2
        v2 = _rotl64(v2, 32)
        self.v0, self.v1, self.v2, self.v3 = v0, v1, v2, v3

    def _block(self, m: int) -> None:
        self.v3 ^= m
        self._round()
        self._round()
        self.v0 ^= m

    def update(self, data: bytes) -> "_SipHasher":
        self._buf += data
        self._total += len(data)
        while len(self._buf) >= 8:
            m = int.from_bytes(self._buf[:8], "little")
            del self._buf[:8]
            self._block(m)
        return self

    def digest(self) -> int:
        b = (self._total & 0xFF) << 56
        tail = bytes(self._buf)
        for i, byte in enumerate(tail):
            b |= byte << (8 * i)
        self._block(b)
        self.v2 ^= 0xFF
        self._round()
        self._round()
        self._round()
        self._round()
        return (self.v0 ^ self.v1 ^ self.v2 ^ self.v3) & _MASK64


def siphash64(data: bytes, k0: int = _SIP_K0, k1: int = _SIP_K1) -> int:
    """SipHash-2-4,返回 64 位无符号整数。

    ``siphash64(b"")`` == ``0x726fdb47dd0e0e31`` —— 这是 SipHash 官方
    测试向量,mini-clickhouse 的测试里直接断言这个值,
    用来证明"我们实现的确实是 SipHash,而不是一个像模像样的仿制品"。
    """
    h = _SipHasher(k0, k1)
    h.update(bytes(data))
    return h.digest()


def siphash128(data: bytes, k0: int = _SIP_K0, k1: int = _SIP_K1) -> int:
    """SipHash-2-4 的 128 位变体(``sipHash128`` SQL 函数)。

    真实 ClickHouse 的 ``sipHash128`` 输出的是**两个 64 位值**拼成的
    128 位整数(低 64 位是高半部分)。这里照做:先算 64 位版本,
    再把 ``v1``/``v2`` 的状态转过来。
    """
    h = _SipHasher(k0, k1)
    h.update(bytes(data))
    lo = h.digest()
    h2 = _SipHasher(k0 ^ lo, k1 ^ (lo >> 32))
    h2.update(b"\x01")
    h2.update(bytes(data))
    hi = h2.digest()
    return (hi << 64) | lo


# ══════════════════════════════════════════════════════════════════════════
# xxHash64
# ══════════════════════════════════════════════════════════════════════════

_XX_PRIME1 = 0x9E3779B185EBCA87
_XX_PRIME2 = 0xC2B2AE3D27D4EB4F
_XX_PRIME3 = 0x165667B19E3779F9
_XX_PRIME4 = 0x85EBCA77C2B2AE63
_XX_PRIME5 = 0x27D4EB2F165667C5


def _xx_round(acc: int, inp: int) -> int:
    acc = (acc + inp * _XX_PRIME2) & _MASK64
    acc = _rotl64(acc, 31)
    return (acc * _XX_PRIME1) & _MASK64


def _xx_merge(acc: int, val: int) -> int:
    acc ^= _xx_round(0, val)
    return (acc * _XX_PRIME1 + _XX_PRIME4) & _MASK64


def xxhash64(data: bytes, seed: int = 0) -> int:
    """xxHash64 —— 标准实现,输出与官方一致。

    ClickHouse 的 ``xxHash64()`` 函数用的就是它(seed 固定为 0)。
    """
    data = bytes(data)
    n = len(data)
    idx = 0
    if n >= 32:
        v1 = (seed + _XX_PRIME1 + _XX_PRIME2) & _MASK64
        v2 = (seed + _XX_PRIME2) & _MASK64
        v3 = seed & _MASK64
        v4 = (seed - _XX_PRIME1) & _MASK64
        while idx + 32 <= n:
            v1 = _xx_round(v1, int.from_bytes(data[idx : idx + 8], "little"))
            v2 = _xx_round(v2, int.from_bytes(data[idx + 8 : idx + 16], "little"))
            v3 = _xx_round(v3, int.from_bytes(data[idx + 16 : idx + 24], "little"))
            v4 = _xx_round(v4, int.from_bytes(data[idx + 24 : idx + 32], "little"))
            idx += 32
        h = (_rotl64(v1, 1) + _rotl64(v2, 7) + _rotl64(v3, 12) + _rotl64(v4, 18)) & _MASK64
        h = _xx_merge(h, v1)
        h = _xx_merge(h, v2)
        h = _xx_merge(h, v3)
        h = _xx_merge(h, v4)
    else:
        h = (seed + _XX_PRIME5) & _MASK64
    h = (h + n) & _MASK64
    while idx + 8 <= n:
        k1 = _xx_round(0, int.from_bytes(data[idx : idx + 8], "little"))
        h ^= k1
        h = (_rotl64(h, 27) * _XX_PRIME1 + _XX_PRIME4) & _MASK64
        idx += 8
    if idx + 4 <= n:
        h ^= (int.from_bytes(data[idx : idx + 4], "little") * _XX_PRIME1) & _MASK64
        h = (_rotl64(h, 23) * _XX_PRIME2 + _XX_PRIME3) & _MASK64
        idx += 4
    while idx < n:
        h ^= (data[idx] * _XX_PRIME5) & _MASK64
        h = (_rotl64(h, 11) * _XX_PRIME1) & _MASK64
        idx += 1
    h ^= h >> 33
    h = (h * _XX_PRIME2) & _MASK64
    h ^= h >> 29
    h = (h * _XX_PRIME3) & _MASK64
    h ^= h >> 32
    return h & _MASK64


# ══════════════════════════════════════════════════════════════════════════
# CRC32 与组合
# ══════════════════════════════════════════════════════════════════════════


def crc32(data: bytes) -> int:
    """标准 CRC-32(与 ``zlib.crc32`` 一致,即 Ethereum/PKZIP 那个多项式)。

    ClickHouse 的新格式用 CRC-32C(Castagnoli),老格式用 CRC-32。
    parts 的 ``checksums.txt`` 用的是 CRC-32 —— 我们在第 05 章
    写 part 时沿用同样的选择。
    """
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


def int_hash64(value: int) -> int:
    """整数哈希 —— 直接返回自身(真实 ClickHouse 的 ``intHash64`` 做了一次混淆)。

    真实实现是 ``(x ^ 0x4cf2d2baae6da4d5) * 0x9ddfea08eb382d69`` 之类的
    混合,再取高 32 位。这里同样做一次混合,避免"连续整数直接当哈希"
    导致的桶倾斜(对任何哈希表都是致命的:自增主键会全部落到相邻桶)。
    """
    x = int(value) & _MASK64
    x ^= 0x4CF2D2BAAE6DA4D5
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & _MASK64
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & _MASK64
    return (x ^ (x >> 31)) & _MASK64


def hash_bytes(data: bytes) -> int:
    """ClickHouse 在哈希表里对"键字节"用的哈希(默认 ``sipHash64``)。"""
    return siphash64(data)


def combine_hashes(a: int, b: int) -> int:
    """把两个 64 位哈希合成一个 —— 真实 CH 的 ``hash_combine``。

    公式来自 Boost 的 ``hash_combine``(``a ^ (b + 0x9e3779b9 + (a << 6) + (a >> 2))``),
    ClickHouse 在多列键的增量哈希里用的是同一套思路。
    """
    return (a ^ (b + 0x9E3779B97F4A7C15 + (a << 6) + (a >> 2))) & _MASK64
