"""页面编码（page encoding）—— 列存压缩的第一战场。

通用压缩（LZ4/Zstd）是**通用**的：它不知道自己在压的是时间戳还是向量分量，
所以只能利用"字节重复"。列存真正的压缩率来自**结构化编码**：
在压缩之前先按列的语义把数据重排成"更容易压"的形态。

Lance 支持的编码大致分五类，本模块逐一实现：

**整数**

* ``constant`` —— 整页只有一个值。真实数据里非常常见：版本号、状态位、租户 id。
* ``bitpack`` —— 定宽位打包。值域 0..7 的列，每值 3 bit，比 int64 省 21 倍。
* ``for``（frame of reference）—— 先减最小值（或基准值），再位打包差值。
* ``delta`` —— 只存首值 + 相邻差分。单调递增的 id 列能压到几乎为零。
* ``varint`` —— LEB128。分布跨度大但不均匀时最好。
* ``rle`` —— 游程编码。排序过的列（比如按时间排序的日志）效果极好。

**浮点**

* ``byte_stream_split`` —— 把 ``[f0 f1 f2 ...]`` 按**字节位**重排成
  ``[b0 of all][b1 of all]...``。浮点数的指数位高度重复、尾数位接近随机，
  拆开之后指数部分几乎能压成常数。这是 Gorilla / Parquet 里的经典技巧，
  也是**向量列唯一有效的通用压缩手段**（向量的每个分量都是近似随机的
  浮点，直接压几乎没有收益，拆字节后能省 20~30%）。

**字符串**

* ``dictionary`` —— 低基数列（枚举、语言代码、状态）先字典化再位打包下标。
* ``fsst`` —— 高基数列用符号表把常见子串换成单字节码。
  这是 FSST 论文算法的简化版：只用单轮贪心建表，不做多轮迭代。

**布尔**

* ``bool_bitmap`` —— 8 个布尔塞进 1 字节。

每种编码都写成 ``encode/decode`` 一对，并且**全部参与往返测试**
（``tests/test_encoding.py`` 里对随机数据、极值、空列、全 null 列逐一验证）。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..errors import CorruptedFileError, TypeMismatchError
from ..types import DataType, narrowest_int_type
from .buffer import BufferRole
from .compress import (
    COMPRESSION_LZ4,
    COMPRESSION_NONE,
    COMPRESSION_ZLIB,
    compress_with,
    decompress_with,
)

__all__ = [
    "PageLayout",
    "EncodedPage",
    "encode_numeric",
    "decode_numeric",
    "encode_bytes",
    "decode_bytes",
    "choose_numeric_encoding",
    "choose_bytes_encoding",
    "compare_numeric_encodings",
    "compare_bytes_encodings",
    "NUMERIC_ENCODERS",
    "BYTE_ENCODERS",
    "ENCODING_NAMES",
    "compress_page",
    "decompress_page",
    "pack_bits",
    "unpack_bits",
    "varint_encode",
    "varint_decode",
    "build_fsst_symbol_table",
    "fsst_compress",
    "fsst_decompress",
    "FSST_ESCAPE",
]

#: 位打包时允许的最大位宽（64 位整数）
MAX_BIT_WIDTH = 64
#: FSST 里保留给"转义"的码字
FSST_ESCAPE = 255


class PageLayout:
    """**结构化编码**（Lance v2 里叫 ``PageLayout``）。

    真实 Lance v2 的编码是**两层**的，理解这一点比记住具体编码名字重要：

    1. **结构化层**决定"值怎么铺进 buffer" ——
       ``mini_block``（按 ~32 行切小块，每块独立编码，便于随机访问）、
       ``constant``（整页一个值）、``full_zip``（整个页的值铺满全部 buffer）、
       ``blob``（大对象外置）、``sparse``（极稀疏/大量 null）；
    2. **压缩层**（``CompressiveEncoding``）决定"这一块用什么算法压" ——
       plain / bitpacking / varint / rle / dictionary / fsst /
       byte_stream_split / range / delta / generalized(LZ4/Zstd)……

    两层正交，所以组合数是 5 × 15。本实现只取其中最常用的一小组组合，
    但字段名和分层方式照抄真实格式，看官方 spec 时能一眼对上。
    """

    MINI_BLOCK = "mini_block"
    CONSTANT = "constant"
    FULL_ZIP = "full_zip"
    BLOB = "blob"
    SPARSE = "sparse"


#: 编码 → 结构化布局的映射。
#: 定长值走 ``full_zip``（整页铺满），整页同值走 ``constant``，
#: 变长值走 ``mini_block``（v2 里变长列天然按块切，块内共享 offsets 基准）。
_LAYOUT_BY_ENCODING: Dict[str, str] = {
    "constant": PageLayout.CONSTANT,
    "plain": PageLayout.FULL_ZIP,
    "bitpack": PageLayout.FULL_ZIP,
    "for": PageLayout.FULL_ZIP,
    "delta": PageLayout.FULL_ZIP,
    "varint": PageLayout.FULL_ZIP,
    "rle": PageLayout.FULL_ZIP,
    "dictionary": PageLayout.MINI_BLOCK,
    "bool_bitmap": PageLayout.FULL_ZIP,
    "byte_stream_split": PageLayout.FULL_ZIP,
    "fsst": PageLayout.MINI_BLOCK,
}


@dataclass
class EncodedPage:
    """一页编码后的结果。

    ``buffers`` 与 ``roles`` 一一对应 —— 角色决定了读的时候怎么解释它
    （值 / 有效性 / 字典 / offsets / 下标）。
    """

    encoding: str
    buffers: List[bytes]
    roles: List[str]
    num_values: int
    metadata: Dict[str, Any] = dc_field(default_factory=dict)
    #: 结构化布局，取值见 :class:`PageLayout`
    layout: str = PageLayout.FULL_ZIP
    #: 是否在结构化编码之后还做了通用压缩
    compression: str = COMPRESSION_NONE
    #: 未压缩时的字节数（解压时要用）
    uncompressed_size: int = 0

    def __post_init__(self) -> None:
        # 布局由编码器名推出来，避免每个编码器都要重复填一遍
        if self.layout == PageLayout.FULL_ZIP:
            self.layout = _LAYOUT_BY_ENCODING.get(self.encoding, PageLayout.FULL_ZIP)

    @property
    def payload_bytes(self) -> int:
        return sum(len(b) for b in self.buffers)

    def __str__(self) -> str:  # pragma: no cover
        comp = "" if self.compression == COMPRESSION_NONE else f"+{self.compression}"
        return (
            f"Page({self.encoding}{comp}, {self.num_values} 值, "
            f"{self.payload_bytes} B, buffers={len(self.buffers)})"
        )


# ---------------------------------------------------------------------------
# 位打包基础设施
# ---------------------------------------------------------------------------


def bit_width_for(max_value: int) -> int:
    """装下 ``0..max_value`` 需要多少 bit。"""

    if max_value <= 0:
        return 0
    return int(max_value).bit_length()


def pack_bits(values: np.ndarray, width: int) -> bytes:
    """把无符号整数按 ``width`` 位紧密打包（小端、按位连续的 LSB-first）。

    打包方向选 LSB-first 是因为 Lance / Arrow 的位图都是这个约定，
    而且解码时可以用同一套移位逻辑处理位图和位打包数组。
    """

    if width == 0:
        return b""
    n = len(values)
    total_bits = n * width
    out = bytearray((total_bits + 7) // 8)
    v = values.astype(np.uint64, copy=False)
    # 逐值写入：教学实现追求可读性；生产实现会用 SIMD 一次处理 32/64 个值。
    bitpos = 0
    for i in range(n):
        x = int(v[i])
        byte = bitpos >> 3
        shift = bitpos & 7
        need = (shift + width + 7) >> 3
        chunk = (x << shift).to_bytes(need + 1, "little")
        for k in range(need):
            out[byte + k] |= chunk[k]
        bitpos += width
    return bytes(out)


def unpack_bits(data: bytes, width: int, count: int) -> np.ndarray:
    if width == 0:
        return np.zeros(count, dtype=np.uint64)
    out = np.zeros(count, dtype=np.uint64)
    mask = (1 << width) - 1
    bitpos = 0
    for i in range(count):
        byte = bitpos >> 3
        shift = bitpos & 7
        need = (shift + width + 7) >> 3
        if byte + need > len(data):
            raise CorruptedFileError(
                "位打包数据被截断", need_bytes=byte + need, have=len(data)
            )
        chunk = int.from_bytes(data[byte : byte + need], "little")
        out[i] = (chunk >> shift) & mask
        bitpos += width
    return out


# 向后兼容的短名（教程里两种写法都会出现）
def bitpack_encode(values: np.ndarray, width: int) -> bytes:
    return pack_bits(values, width)


def bitpack_decode(data: bytes, width: int, count: int) -> np.ndarray:
    return unpack_bits(data, width, count)


# ---------------------------------------------------------------------------
# LEB128 / zigzag
# ---------------------------------------------------------------------------


def zigzag_encode(x: int) -> int:
    """有符号 → 无符号，让"接近 0 的小负数"也变成小整数。"""

    return (x << 1) ^ (x >> 63) if x < 0 else (x << 1)


def zigzag_decode(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def varint_encode(values: Sequence[int]) -> bytes:
    out = bytearray()
    for v in values:
        u = zigzag_encode(int(v))
        while True:
            b = u & 0x7F
            u >>= 7
            if u:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
    return bytes(out)


def varint_decode(data: bytes, count: int) -> List[int]:
    out: List[int] = []
    i = 0
    n = len(data)
    while len(out) < count:
        shift = 0
        u = 0
        while True:
            if i >= n:
                raise CorruptedFileError("varint 数据被截断", decoded=len(out))
            b = data[i]
            i += 1
            u |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63:
                raise CorruptedFileError("varint 超过了 64 位")
        out.append(zigzag_decode(u))
    return out


# ---------------------------------------------------------------------------
# 数值编码器
# ---------------------------------------------------------------------------


class NumericEncoder:
    """整数/浮点列的一页编码器。"""

    name: str = ""

    @classmethod
    def can_encode(cls, data: np.ndarray, dtype: DataType, valid: Optional[np.ndarray]) -> bool:
        return False

    @classmethod
    def encode(cls, data: np.ndarray, dtype: DataType, valid: Optional[np.ndarray]) -> EncodedPage:
        raise NotImplementedError

    @classmethod
    def decode(cls, page: EncodedPage, dtype: DataType) -> np.ndarray:
        raise NotImplementedError

    # -- 工具 -------------------------------------------------------------
    @staticmethod
    def _effective(data: np.ndarray, valid: Optional[np.ndarray]) -> np.ndarray:
        """只看有效值。空列/全 null 列要单独处理，不能直接 min/max。"""

        if valid is None or valid.all():
            return data
        return data[valid]


class ConstantEncoder(NumericEncoder):
    """整页同一个值。

    真实数据里出现频率远超直觉：布尔状态、版本号、租户 id、枚举。
    一页 100 万个 8 也不占一个字节。
    """

    name = "constant"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        eff = cls._effective(data, valid)
        if len(eff) == 0:
            return len(data) > 0
        return bool(np.all(eff == eff[0]))

    @classmethod
    def encode(cls, data, dtype, valid):
        eff = cls._effective(data, valid)
        value = eff[0] if len(eff) else np.array(0, dtype=data.dtype)
        raw = np.asarray([value], dtype=data.dtype).tobytes()
        return EncodedPage(
            cls.name,
            [raw],
            [BufferRole.VALUES],
            len(data),
            {"value": _jsonable(value)},
        )

    @classmethod
    def decode(cls, page, dtype):
        arr = np.frombuffer(page.buffers[0], dtype=dtype.numpy_dtype)
        return np.repeat(arr[:1], page.num_values)


class BoolBitmapEncoder(NumericEncoder):
    """布尔列：1 bit / 值。"""

    name = "bool_bitmap"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        return data.dtype == np.bool_

    @classmethod
    def encode(cls, data, dtype, valid):
        bits = pack_bits(data.astype(np.uint8), 1)
        ntrue = int(np.count_nonzero(data))
        return EncodedPage(cls.name, [bits], [BufferRole.VALUES], len(data), {"true_count": ntrue})

    @classmethod
    def decode(cls, page, dtype):
        return unpack_bits(page.buffers[0], 1, page.num_values).astype(bool)


class DeltaEncoder(NumericEncoder):
    """首值 + 差分 varint。适合单调递增的 id / 时间戳。"""

    name = "delta"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if data.dtype.kind not in "iu" or len(data) <= 1:
            return False
        d = np.diff(data.astype(np.int64))
        # 单调且增量不大时才有意义；随机数据上 delta 反而更长
        return bool(np.all(d >= 0) and np.all(d < (1 << 20)))

    @classmethod
    def encode(cls, data, dtype, valid):
        i64 = data.astype(np.int64)
        first = np.asarray([i64[0]], dtype=np.int64).tobytes()
        deltas = np.diff(i64)
        return EncodedPage(
            cls.name,
            [first, varint_encode(deltas.tolist())],
            [BufferRole.VALUES, BufferRole.AUX],
            len(data),
            {},
        )

    @classmethod
    def decode(cls, page, dtype):
        first = int(np.frombuffer(page.buffers[0], dtype=np.int64)[0])
        deltas = np.asarray(
            varint_decode(page.buffers[1], page.num_values - 1), dtype=np.int64
        )
        out = np.empty(page.num_values, dtype=np.int64)
        out[0] = first
        if page.num_values > 1:
            # **先累加差分、再把首值加回去。** 少加这个 first，
            # 在"首值恰好为 0"的列上结果完全正确 —— 于是这类 bug
            # 会一直藏到某一天列不再从 0 开始。这就是为什么
            # 编码器的测试必须覆盖"基值非零"的输入。
            np.cumsum(deltas, out=out[1:])
            out[1:] += first
        return out.astype(dtype.numpy_dtype)


class FrameOfReferenceEncoder(NumericEncoder):
    """``for``：先减去基准值，再定宽位打包。

    位宽由 ``max - base`` 决定，所以一张"金额都在 100~200 元之间"的
    浮点列如果先量化成整数，就能把 64 bit 压到 8 bit。
    """

    name = "for"

    @classmethod
    def _range(cls, data, valid):
        eff = cls._effective(data, valid)
        if len(eff) == 0:
            return 0, 0
        return int(eff.min()), int(eff.max())

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if data.dtype.kind not in "iu":
            return False
        lo, hi = cls._range(data, valid)
        return bit_width_for(hi - lo) < data.dtype.itemsize * 8

    @classmethod
    def encode(cls, data, dtype, valid):
        lo, hi = cls._range(data, valid)
        width = bit_width_for(hi - lo)
        shifted = data.astype(np.int64) - lo
        base = np.asarray([lo], dtype=np.int64).tobytes()
        packed = pack_bits(shifted.astype(np.uint64), width)
        return EncodedPage(
            cls.name,
            [base, packed],
            [BufferRole.VALUES, BufferRole.AUX],
            len(data),
            {"base": lo, "bit_width": width},
        )

    @classmethod
    def decode(cls, page, dtype):
        base = int(np.frombuffer(page.buffers[0], dtype=np.int64)[0])
        width = int(page.metadata["bit_width"])
        values = unpack_bits(page.buffers[1], width, page.num_values).astype(np.int64)
        return (values + base).astype(dtype.numpy_dtype)


class BitpackEncoder(NumericEncoder):
    """纯位打包：位宽由整列的值域决定（不减去基准）。"""

    name = "bitpack"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if data.dtype.kind not in "iu":
            return False
        eff = cls._effective(data, valid)
        hi = int(eff.max()) if len(eff) else 0
        return bit_width_for(hi) < data.dtype.itemsize * 8

    @classmethod
    def encode(cls, data, dtype, valid):
        eff = cls._effective(data, valid)
        hi = int(eff.max()) if len(eff) else 0
        width = bit_width_for(hi)
        packed = pack_bits(data.astype(np.uint64), width)
        return EncodedPage(
            cls.name,
            [packed],
            [BufferRole.VALUES],
            len(data),
            {"bit_width": width},
        )

    @classmethod
    def decode(cls, page, dtype):
        width = int(page.metadata["bit_width"])
        return unpack_bits(page.buffers[0], width, page.num_values).astype(dtype.numpy_dtype)


class RleEncoder(NumericEncoder):
    """游程编码：``(值, 重复次数)`` 两张表。

    对**排序过**的列（时间序列、按类别聚簇的数据）几乎是免费的压缩。
    对随机顺序的列则会膨胀 —— 所以能不能用必须由数据说话。
    """

    name = "rle"

    @classmethod
    def _runs(cls, data: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if len(data) == 0:
            return np.zeros(0, dtype=data.dtype), np.zeros(0, dtype=np.int64)
        change = np.flatnonzero(data[1:] != data[:-1]) + 1
        starts = np.concatenate(([0], change))
        lengths = np.diff(np.concatenate((starts, [len(data)])))
        return data[starts], lengths

    @classmethod
    def _estimated_size(cls, data: np.ndarray) -> int:
        vals, lengths = cls._runs(data)
        # 值按原 dtype 存 + 长度 varint
        return len(vals) * data.dtype.itemsize + sum(_varint_len(int(l)) for l in lengths)

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if len(data) == 0:
            return False
        # 只有明显能省才值得选（游程数 < 行数的 1/2）
        return len(cls._runs(data)[0]) * 2 < len(data)

    @classmethod
    def encode(cls, data, dtype, valid):
        vals, lengths = cls._runs(data)
        return EncodedPage(
            cls.name,
            [vals.tobytes(), varint_encode(lengths.tolist())],
            [BufferRole.VALUES, BufferRole.AUX],
            len(data),
            {"num_runs": int(len(vals))},
        )

    @classmethod
    def decode(cls, page, dtype):
        vals = np.frombuffer(page.buffers[0], dtype=dtype.numpy_dtype)
        lengths = varint_decode(page.buffers[1], len(vals))
        return np.repeat(vals, np.asarray(lengths, dtype=np.int64))


class VarintEncoder(NumericEncoder):
    """LEB128：小值 1 字节，大值按需扩展。"""

    name = "varint"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if data.dtype.kind not in "iu":
            return False
        return dtype.numpy_dtype.itemsize >= 2

    @classmethod
    def encode(cls, data, dtype, valid):
        raw = varint_encode(data.astype(np.int64).tolist())
        return EncodedPage(cls.name, [raw], [BufferRole.VALUES], len(data), {})

    @classmethod
    def decode(cls, page, dtype):
        vals = varint_decode(page.buffers[0], page.num_values)
        return np.asarray(vals, dtype=np.int64).astype(dtype.numpy_dtype)


class DictionaryNumericEncoder(NumericEncoder):
    """数值字典编码：取值种类少时，存"取值表 + 位打包下标"。"""

    name = "dictionary"

    @classmethod
    def _unique(cls, data, valid):
        eff = cls._effective(data, valid)
        if len(eff) == 0:
            return np.zeros(0, dtype=data.dtype)
        return np.unique(eff)

    @classmethod
    def can_encode(cls, data, dtype, valid):
        if len(data) == 0:
            return False
        uniq = cls._unique(data, valid)
        # 全 null 的列**不能**走字典编码：字典是空的，解码时 ``uniq[idx]``
        # 会直接越界。这里必须拿 ``uniq`` 判空，而不是拿 ``data`` —— 全 null
        # 列的 ``len(data)`` 是 N 而不是 0，光看 ``data`` 拦不住它，
        # 于是 ``bit_width_for(-1)`` 算出 0、cost 算出 0，一路"通过"。
        # 症状是**写成功了、读不回来**：alter_columns 把一列全 null 的
        # float32 改成 float64 之后，提交正常返回，之后任何一次读整张表
        # 都抛 IndexError。见 tests/test_regressions.py。
        if len(uniq) == 0:
            return False
        if len(uniq) > 1 << 16:
            return False
        width = bit_width_for(len(uniq) - 1)
        cost = len(uniq) * max(1, data.dtype.itemsize) + math.ceil(len(data) * width / 8)
        return cost < len(data) * data.dtype.itemsize

    @classmethod
    def encode(cls, data, dtype, valid):
        uniq = cls._unique(data, valid)
        idx = np.searchsorted(uniq, data)
        width = bit_width_for(len(uniq) - 1)
        return EncodedPage(
            cls.name,
            [uniq.tobytes(), pack_bits(idx.astype(np.uint64), width)],
            [BufferRole.DICTIONARY, BufferRole.INDICES],
            len(data),
            {"bit_width": width, "num_unique": int(len(uniq))},
        )

    @classmethod
    def decode(cls, page, dtype):
        uniq = np.frombuffer(page.buffers[0], dtype=dtype.numpy_dtype)
        width = int(page.metadata["bit_width"])
        idx = unpack_bits(page.buffers[1], width, page.num_values)
        return uniq[idx]


class ByteStreamSplitEncoder(NumericEncoder):
    """浮点列的字节流拆分。

    32 位浮点按"符号+指数 → 尾数"排列。把 4 个字节按位置分组
    （``[b0 of all][b1 of all][b2 of all][b3 of all]``）之后，
    高位字节的取值高度集中，能压掉一大截；低位尾数接近随机，压不动。

    **但实测的收益比传闻小很多，而且会反向。** 在 2000×128 的
    N(0,1) 归一化 float32 向量上，我们用真实的 LZ4 实现量过：

    * plain 布局 + LZ4：1,027,900 字节（比原始 1,024,000 还大 —— 随机数据压不动）；
    * 字节拆分 + LZ4：954,896 字节，比上面小 **7.1%**。

    而在"已经量化过、尾数字节大量为 0"的浮点上，拆分反而**更差 12%**：
    量化后的低字节全是 0，plain 布局把它们挨在一起，LZ4 一眼就能压掉；
    按字节位拆开之后，这种重复关系被打散了。

    教训是：**结构性变换和通用压缩是耦合的，不能独立决策。**
    真实 Lance 的做法是让编码选择由数据分布驱动（有一套按优先级
    排列的 compression strategy），而不是"浮点就上 byte_stream_split"。
    """

    name = "byte_stream_split"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        return data.dtype.kind == "f" and data.dtype.itemsize in (2, 4, 8)

    @classmethod
    def encode(cls, data, dtype, valid):
        itemsize = data.dtype.itemsize
        raw = np.ascontiguousarray(data).view(np.uint8).reshape(len(data), itemsize)
        buffers = [np.ascontiguousarray(raw[:, j]).tobytes() for j in range(itemsize)]
        return EncodedPage(
            cls.name,
            buffers,
            [BufferRole.VALUES] * itemsize,
            len(data),
            {"itemsize": itemsize},
        )

    @classmethod
    def decode(cls, page, dtype):
        itemsize = int(page.metadata["itemsize"])
        cols = [np.frombuffer(b, dtype=np.uint8) for b in page.buffers]
        mat = np.stack(cols, axis=1)
        return np.ascontiguousarray(mat).view(dtype.numpy_dtype).reshape(page.num_values)


class PlainEncoder(NumericEncoder):
    """兜底：原样小端存。"""

    name = "plain"

    @classmethod
    def can_encode(cls, data, dtype, valid):
        return True

    @classmethod
    def encode(cls, data, dtype, valid):
        return EncodedPage(
            cls.name, [np.ascontiguousarray(data).tobytes()], [BufferRole.VALUES], len(data), {}
        )

    @classmethod
    def decode(cls, page, dtype):
        arr = np.frombuffer(page.buffers[0], dtype=dtype.numpy_dtype)
        return arr[: page.num_values]


# ---------------------------------------------------------------------------
# 字符串/二进制编码器
# ---------------------------------------------------------------------------


class ByteEncoder:
    """变长（字符串/二进制）列的一页编码器。

    输入输出都是 ``(offsets, buffer)`` 这一对 —— 和 Arrow 的物理布局一致。
    """

    name: str = ""

    @classmethod
    def can_encode(cls, offsets: np.ndarray, buf: bytes, count: int) -> bool:
        return False

    @classmethod
    def encode(cls, offsets: np.ndarray, buf: bytes, count: int) -> EncodedPage:
        raise NotImplementedError

    @classmethod
    def decode(cls, page: EncodedPage) -> Tuple[np.ndarray, bytes]:
        raise NotImplementedError


class PlainByteEncoder(ByteEncoder):
    name = "plain"

    @classmethod
    def can_encode(cls, offsets, buf, count):
        return True

    @classmethod
    def encode(cls, offsets, buf, count):
        # offsets 固定用 int32 存：4 GiB 的页已经远超合理范围
        off = offsets.astype(np.int32) if offsets[-1] < 2**31 else offsets.astype(np.int64)
        return EncodedPage(
            cls.name,
            [off.tobytes(), buf],
            [BufferRole.OFFSETS, BufferRole.DATA],
            count,
            {"offset_bits": 32 if off.dtype == np.int32 else 64},
        )

    @classmethod
    def decode(cls, page):
        dt = np.int32 if page.metadata.get("offset_bits", 32) == 32 else np.int64
        off = np.frombuffer(page.buffers[0], dtype=dt).astype(np.int64)
        return off, page.buffers[1]


class DictionaryByteEncoder(ByteEncoder):
    """字符串字典编码。高重复度文本（日志模板、URL 前缀、语言代码）的主力。"""

    name = "dictionary"

    @classmethod
    def _analysis(cls, offsets, buf, count):
        slices = [buf[offsets[i] : offsets[i + 1]] for i in range(count)]
        uniq: Dict[bytes, int] = {}
        idx = np.zeros(count, dtype=np.int64)
        for i, s in enumerate(slices):
            if s not in uniq:
                uniq[s] = len(uniq)
            idx[i] = uniq[s]
        return list(uniq.keys()), idx

    @classmethod
    def can_encode(cls, offsets, buf, count):
        if count == 0:
            return False
        uniq, idx = cls._analysis(offsets, buf, count)
        if len(uniq) > 1 << 24:
            return False
        width = bit_width_for(len(uniq) - 1)
        dict_bytes = sum(len(u) + 4 for u in uniq)
        cost = dict_bytes + math.ceil(count * width / 8) + 4 * (count + 1)
        return cost < 4 * (count + 1) + len(buf)

    @classmethod
    def encode(cls, offsets, buf, count):
        uniq, idx = cls._analysis(offsets, buf, count)
        width = bit_width_for(len(uniq) - 1)
        dict_offsets = np.zeros(len(uniq) + 1, dtype=np.int32)
        parts: List[bytes] = []
        total = 0
        for i, s in enumerate(uniq):
            parts.append(s)
            total += len(s)
            dict_offsets[i + 1] = total
        return EncodedPage(
            cls.name,
            [
                dict_offsets.tobytes(),
                b"".join(parts),
                pack_bits(idx.astype(np.uint64), width),
            ],
            [BufferRole.OFFSETS, BufferRole.DICTIONARY, BufferRole.INDICES],
            count,
            {"bit_width": width, "num_unique": len(uniq), "index_bits": 32},
        )

    @classmethod
    def decode(cls, page):
        count = page.num_values
        if count == 0:
            return np.zeros(1, dtype=np.int64), b""
        dict_off = np.frombuffer(page.buffers[0], dtype=np.int32).astype(np.int64)
        dict_buf = page.buffers[1]
        width = int(page.metadata["bit_width"])
        idx = unpack_bits(page.buffers[2], width, count)
        entries = [
            dict_buf[dict_off[i] : dict_off[i + 1]] for i in range(len(dict_off) - 1)
        ]
        out_off = np.zeros(count + 1, dtype=np.int64)
        np.cumsum([len(entries[i]) for i in idx], out=out_off[1:])
        return out_off, b"".join(entries[i] for i in idx)


def build_fsst_symbol_table(
    samples: Sequence[bytes], *, max_symbols: int = FSST_ESCAPE, max_len: int = 8
) -> List[bytes]:
    """用"贪心多长度计数"建一张符号表。

    真正的 FSST 会做多轮迭代并做增益评估（论文里的 *sample → iterate →
    finalize* 三阶段）；这里只做一轮：从长到短统计 n-gram 频率，
    按 ``(频次 × (长度-1))`` 排序，取前 ``max_symbols`` 个互不冲突的。

    这个简化版在"重复子串很多"的数据上能拿到 2~4 倍压缩，
    在随机数据上则几乎无效（会退化成 plain + 转义字节开销）。
    """

    counts: Dict[bytes, int] = {}
    for L in range(min(max_len, 8), 1, -1):
        for s in samples:
            for i in range(len(s) - L + 1):
                counts[s[i : i + L]] = counts.get(s[i : i + L], 0) + 1

    scored = sorted(
        counts.items(),
        key=lambda kv: (-(kv[1] * (len(kv[0]) - 1)), kv[0]),
    )
    table: List[bytes] = []
    seen: set = set()
    for sym, cnt in scored:
        if cnt < 2 or len(table) >= max_symbols:
            break
        if sym in seen:
            continue
        if any(sym != other and (sym in other or other in sym) for other in table[:64]):
            # 避免"一个符号是另一个的子串"导致解码时贪心歧义；
            # 只和前 64 个比，纯粹是性能取舍
            continue
        table.append(sym)
        seen.add(sym)
    return table


def fsst_compress(offsets: np.ndarray, buf: bytes, count: int, table: List[bytes]) -> bytes:
    """按符号表做贪心最长匹配编码。码 255 是转义，后面跟一个原始字节。"""

    # 按首字节分组，避免对每个位置都扫整张表
    by_first: Dict[int, List[Tuple[int, bytes]]] = {}
    for code, sym in enumerate(table):
        by_first.setdefault(sym[0], []).append((code, sym))
    for lst in by_first.values():
        lst.sort(key=lambda kv: -len(kv[1]))

    out = bytearray()
    for i in range(count):
        s = buf[offsets[i] : offsets[i + 1]]
        j = 0
        n = len(s)
        while j < n:
            best_code = -1
            best_len = 0
            for code, sym in by_first.get(s[j], ()):
                ln = len(sym)
                if ln > best_len and s.startswith(sym, j):
                    best_code, best_len = code, ln
            if best_code >= 0:
                out.append(best_code)
                j += best_len
            else:
                out.append(FSST_ESCAPE)
                out.append(s[j])
                j += 1
    return bytes(out)


def fsst_decompress(data: bytes, table: List[bytes]) -> Tuple[np.ndarray, bytes]:
    """把 FSST 流解回 ``(片段边界, 原始字节)``。

    ⚠️ 返回的 offsets 是**码字片段**的边界，不是**行**的边界 ——
    一行可能被切成好几个片段（一个片段一个符号/转义字节）。
    调用方要用它做逐行切分时必须自己另存行边界，见
    :class:`FsstEncoder` 里对第 4 个 buffer 的说明。
    """

    parts: List[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        i += 1
        if code == FSST_ESCAPE:
            if i >= n:
                raise CorruptedFileError("FSST 转义字节后面没有内容")
            parts.append(bytes([data[i]]))
            i += 1
        else:
            if code >= len(table):
                raise CorruptedFileError(
                    "FSST 码字超出符号表", code=code, table_size=len(table)
                )
            parts.append(table[code])
    offsets = np.zeros(len(parts) + 1, dtype=np.int64)
    np.cumsum([len(p) for p in parts], out=offsets[1:])
    return offsets, b"".join(parts)


class FsstEncoder(ByteEncoder):
    name = "fsst"

    @classmethod
    def can_encode(cls, offsets, buf, count):
        if count == 0 or len(buf) == 0:
            return False
        # 先在小样本上估价：全量建表太贵，Lance 也是采样的
        sample_idx = np.linspace(0, count - 1, min(count, 256)).astype(int)
        samples = [buf[offsets[i] : offsets[i + 1]] for i in sample_idx]
        table = build_fsst_symbol_table(samples, max_symbols=64)
        if len(table) < 8:
            return False
        # 用样本估压缩率
        sample_off = np.zeros(len(samples) + 1, dtype=np.int64)
        np.cumsum([len(s) for s in samples], out=sample_off[1:])
        packed = fsst_compress(sample_off, b"".join(samples), len(samples), table)
        raw = sum(len(s) for s in samples)
        symbol_bytes = sum(len(t) + 1 for t in table)
        return len(packed) + symbol_bytes < raw

    @classmethod
    def encode(cls, offsets, buf, count):
        all_slices = [buf[offsets[i] : offsets[i + 1]] for i in range(count)]
        table = build_fsst_symbol_table(all_slices, max_symbols=FSST_ESCAPE)
        packed = fsst_compress(offsets, buf, count, table)
        sym_offsets = np.zeros(len(table) + 1, dtype=np.int32)
        parts: List[bytes] = []
        total = 0
        for i, sym in enumerate(table):
            parts.append(sym)
            total += len(sym)
            sym_offsets[i + 1] = total
        # 行边界必须**单独**存一份。
        #
        # 压缩流本身无法恢复"哪几个码字属于同一行"：``fsst_compress`` 是
        # 逐行重置再贪心匹配的，解码器却只能一个码字一个码字地往下读，
        # 不知道某一行在哪里结束（码字长度不一，且没有分隔符 —— 255 已经被
        # 转义字节占了）。所以把原始 offsets（按页内首行归零）作为第 4 个
        # buffer 写进去。这也是真实的 FSST 列存格式必须存 offsets 的原因：
        # 压缩的是**数据**，行边界是**结构**，结构不参与压缩。
        base = int(offsets[0]) if count else 0
        row_offsets = np.asarray(offsets[: count + 1], dtype=np.int64) - base
        if row_offsets.size and row_offsets[-1] < 2**31:
            row_offsets = row_offsets.astype(np.int32)
            bits = 32
        else:
            bits = 64
        return EncodedPage(
            cls.name,
            [
                sym_offsets.tobytes(),
                b"".join(parts),
                packed,
                row_offsets.tobytes(),
            ],
            [
                BufferRole.OFFSETS,
                BufferRole.DICTIONARY,
                BufferRole.DATA,
                BufferRole.OFFSETS,
            ],
            count,
            {"num_symbols": len(table), "row_offset_bits": bits},
        )

    @classmethod
    def decode(cls, page):
        sym_off = np.frombuffer(page.buffers[0], dtype=np.int32).astype(np.int64)
        sym_buf = page.buffers[1]
        table = [sym_buf[sym_off[i] : sym_off[i + 1]] for i in range(len(sym_off) - 1)]
        _frag_offsets, buf = fsst_decompress(page.buffers[2], table)
        if len(page.buffers) > 3:
            dt = np.int32 if page.metadata.get("row_offset_bits", 32) == 32 else np.int64
            offsets = np.frombuffer(page.buffers[3], dtype=dt).astype(np.int64)
            return offsets, buf
        # 兼容没有行边界的旧页（它们本来就解不出正确的行划分）
        return _frag_offsets, buf


# ---------------------------------------------------------------------------
# 编码选择器
# ---------------------------------------------------------------------------

NUMERIC_ENCODERS: List[type] = [
    ConstantEncoder,
    BoolBitmapEncoder,
    DeltaEncoder,
    DictionaryNumericEncoder,
    FrameOfReferenceEncoder,
    BitpackEncoder,
    RleEncoder,
    VarintEncoder,
    ByteStreamSplitEncoder,
    PlainEncoder,
]

BYTE_ENCODERS: List[type] = [
    DictionaryByteEncoder,
    FsstEncoder,
    PlainByteEncoder,
]

ENCODING_NAMES = [c.name for c in NUMERIC_ENCODERS] + ["fsst"]


def compare_numeric_encodings(
    data: np.ndarray, dtype: DataType, valid: Optional[np.ndarray] = None
) -> List[Tuple[str, int]]:
    """把这一列在所有可行编码下的字节数排出来。

    教程里直接打印它，就能看到"同一列数据，编码不同差几十倍"。
    """

    rows: List[Tuple[str, int]] = []
    for enc in NUMERIC_ENCODERS:
        try:
            if not enc.can_encode(data, dtype, valid):
                continue
            page = enc.encode(data, dtype, valid)
            rows.append((enc.name, page.payload_bytes))
        except Exception:  # pragma: no cover - 单列失败不影响整体对比
            continue
    rows.sort(key=lambda kv: kv[1])
    return rows


def compare_bytes_encodings(
    offsets: np.ndarray, buf: bytes, count: int
) -> List[Tuple[str, int]]:
    rows: List[Tuple[str, int]] = []
    for enc in BYTE_ENCODERS:
        try:
            if not enc.can_encode(offsets, buf, count):
                continue
            page = enc.encode(offsets, buf, count)
            rows.append((enc.name, page.payload_bytes))
        except Exception:  # pragma: no cover
            continue
    rows.sort(key=lambda kv: kv[1])
    return rows


def choose_numeric_encoding(
    data: np.ndarray,
    dtype: DataType,
    valid: Optional[np.ndarray] = None,
    *,
    exhaustive: bool = True,
) -> EncodedPage:
    """选一个编码并产出页面。

    ``exhaustive=True`` 时把所有可行编码都试一遍取最小 ——
    简单、结果最好，代价是 CPU。真实 Lance 走的是启发式
    （看数据类型、基数、单调性），因为这个决策在 PB 级数据上
    必须便宜。我们默认开 exhaustive，因为教学场景数据小、
    而且"看到每种编码的实际大小"本身就是内容。
    """

    if not exhaustive:
        for enc in NUMERIC_ENCODERS:
            if enc.can_encode(data, dtype, valid):
                return enc.encode(data, dtype, valid)
        return PlainEncoder.encode(data, dtype, valid)

    best: Optional[EncodedPage] = None
    for enc in NUMERIC_ENCODERS:
        try:
            if not enc.can_encode(data, dtype, valid):
                continue
            page = enc.encode(data, dtype, valid)
        except Exception:
            continue
        if best is None or page.payload_bytes < best.payload_bytes:
            best = page
    if best is None:
        best = PlainEncoder.encode(data, dtype, valid)
    return best


def choose_bytes_encoding(offsets: np.ndarray, buf: bytes, count: int) -> EncodedPage:
    best: Optional[EncodedPage] = None
    for enc in BYTE_ENCODERS:
        try:
            if not enc.can_encode(offsets, buf, count):
                continue
            page = enc.encode(offsets, buf, count)
        except Exception:
            continue
        if best is None or page.payload_bytes < best.payload_bytes:
            best = page
    if best is None:
        best = PlainByteEncoder.encode(offsets, buf, count)
    return best


# ---------------------------------------------------------------------------
# 通用解码分派
# ---------------------------------------------------------------------------

_NUMERIC_BY_NAME = {c.name: c for c in NUMERIC_ENCODERS}
_BYTE_BY_NAME = {c.name: c for c in BYTE_ENCODERS}


def encode_numeric(
    data: np.ndarray, dtype: DataType, valid: Optional[np.ndarray] = None
) -> EncodedPage:
    return choose_numeric_encoding(data, dtype, valid)


def decode_numeric(page: EncodedPage, dtype: DataType) -> np.ndarray:
    try:
        enc = _NUMERIC_BY_NAME[page.encoding]
    except KeyError:
        raise CorruptedFileError(
            f"未知的数值编码 {page.encoding!r}", available=sorted(_NUMERIC_BY_NAME)
        ) from None
    return enc.decode(page, dtype)


def encode_bytes(offsets: np.ndarray, buf: bytes, count: int) -> EncodedPage:
    return choose_bytes_encoding(offsets, buf, count)


def decode_bytes(page: EncodedPage) -> Tuple[np.ndarray, bytes]:
    try:
        enc = _BYTE_BY_NAME[page.encoding]
    except KeyError:
        raise CorruptedFileError(
            f"未知的字节编码 {page.encoding!r}", available=sorted(_BYTE_BY_NAME)
        ) from None
    return enc.decode(page)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _varint_len(v: int) -> int:
    n = 1
    u = zigzag_encode(v)
    while u >= 0x80:
        u >>= 7
        n += 1
    return n


def _jsonable(v: Any) -> Any:
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    # 0 维 ndarray 不属于上面任何一种 numpy *标量*，会被原样返回，
    # 然后在页元数据写 JSON 那一步炸掉（TypeError: Object of type
    # ndarray is not JSON serializable）。ConstantEncoder 在一列
    # "全 null"上正好会造出 np.array(0, dtype=...) 这个形状。
    if isinstance(v, np.ndarray) and v.ndim == 0:
        return v.item()
    return v


def compress_page(page: EncodedPage, algorithm: str = COMPRESSION_LZ4) -> EncodedPage:
    """对编码后的缓冲区再做一层通用压缩。

    注意：**先结构化编码、再通用压缩**这个顺序不能反。
    反过来的话，LZ4 会把字节搞乱，位打包之类的结构就没了。
    """

    if algorithm == COMPRESSION_NONE:
        return page
    total = page.payload_bytes
    buffers = [compress_with(algorithm, b) for b in page.buffers]
    out = EncodedPage(
        page.encoding,
        buffers,
        list(page.roles),
        page.num_values,
        dict(page.metadata),
        algorithm,
        total,
    )
    if out.payload_bytes >= total:
        # 压不动就别压 —— 否则每次读都要多付一次解压成本
        return page
    return out


def decompress_page(page: EncodedPage) -> EncodedPage:
    if page.compression == COMPRESSION_NONE:
        return page
    buffers = [
        decompress_with(page.compression, b, None) for b in page.buffers
    ]
    return EncodedPage(
        page.encoding,
        buffers,
        list(page.roles),
        page.num_values,
        dict(page.metadata),
        COMPRESSION_NONE,
        page.uncompressed_size,
    )
