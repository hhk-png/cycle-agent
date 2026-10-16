"""压缩 —— **编解码器(codec)按列堆叠**。

这是 :mod:`minich.serialization` 的下一层。序列化回答“一列变成哪些字节”,
压缩回答“这些字节怎么变小”。理解 ClickHouse 的压缩,关键只有一个洞见:

.. code-block:: text

   编解码器是**每个列各自配置**的,而且真正生效的顺序是
   「先专用(specialization),后通用(generic)」。

      列字节流                    专用编解码器                 通用编解码器         磁盘
   ┌──────────────┐        ┌─────────────────────┐     ┌──────────────┐
   │ ts UInt64 ...│───①───▶│ Delta / DoubleDelta │────▶│ LZ4 / ZSTD   │───②───▶ block
   │ (原始 8N 字节)│        │ 只改“布局”,不懂字节  │     │ 压任意字节    │
   └──────────────┘        └─────────────────────┘     └──────────────┘
       单调递增的时间戳        变成“差值”后全是 0/小数      把 0 和小数真的压掉

为什么必须是这个顺序?

* **专用编解码器(Delta / DoubleDelta / Gorilla / T64 / FPC)只能压数值列。**
  它们按固定元素宽度重新解释字节:Delta 算相邻整数的差、DoubleDelta 算差分的差分、
  Gorilla 做 XOR、T64 做字节转置、FPC 用两个预测器。它们**看不懂**字符串、看不懂
  任意字节流 —— 输入元素宽度错了,输出就是垃圾。所以叫“非通用”。
* **通用编解码器(NONE / LZ4 / ZSTD)能压任意字节流**,但它们不知道“这 8 个字节
  是一个 UInt64”。对一串单调递增的整数,LZ4 找不到重复片段(每个值都不同),几乎压不动;
  而先做 Delta 之后,数据变成“一堆 0 加一个小的低字节”,LZ4 / ZSTD 立刻就能压出 20×。

所以 ``CODEC(Delta, ZSTD)`` 的两步是**互补**的:专用编解码器负责“把数据变规整”,
通用编解码器负责“把规整的数据真的压小”。这也是为什么 ClickHouse 会拒绝
``CODEC(Delta)`` 这种**只有专用编解码器**的写法 —— 一个非通用编解码器后面
必须跟一个通用编解码器,否则数据既没变小、又不再能直接被别的系统读::

   Code: 49. DB::Exception: The combination of compression codecs is invalid. (LOGICAL_ERROR)

.. code-block:: text

   本模块实现的编解码器一览(「通用?」列就是 ``is_generic()`` 的返回值):

   ┌──────────────┬────────┬─────────┬──────┬─────────────────────────────────────┐
   │ 名字          │ 方法字节│ 通用?   │ 宽度 │ 干什么                               │
   ├──────────────┼────────┼─────────┼──────┼─────────────────────────────────────┤
   │ NONE         │ 0x02   │ ✅ 是   │ —    │ 原样返回(对照组)                     │
   │ LZ4          │ 0x82   │ ✅ 是   │ —    │ LZ77 快模式(短哈希链)                │
   │ LZ4HC        │ 0x82   │ ✅ 是   │ —    │ LZ77 高压缩模式(深哈希链)            │
   │ ZSTD         │ 0x90   │ ✅ 是   │ —    │ LZ77 + 自适应算术编码                │
   │ Delta        │ 0x83   │ ❌ 否   │ N    │ 逐字节平面做一阶差分                 │
   │ DoubleDelta  │ 0x85   │ ❌ 否   │ 8    │ 二阶差分 + 变长编码(时间戳的神器)   │
   │ Gorilla      │ 0x86   │ ❌ 否   │ 8    │ XOR + 前导/后缀零计数,位级打包       │
   │ T64          │ 0x84   │ ❌ 否   │ N    │ 字节转置 + 丢常量平面 + 位打包       │
   │ FPC          │ 0x87   │ ❌ 否   │ 8    │ 两个预测器(FCM/DFCM)+ 2 位选择子     │
   └──────────────┴────────┴─────────┴──────┴─────────────────────────────────────┘

.. warning::

   **这不是真正的 LZ4 / ZSTD,压缩结果与真实的库互不兼容,不要拿去做跨系统交换。**

   真实 ClickHouse 的 ``LZ4`` 直接调用 lz4 库的 ``LZ4_compress_default``,``ZSTD``
   调用 libzstd;两者的字节格式都有公开规范,别的系统(甚至别的语言)能解。

   mini-clickhouse 出于教学目的,**自己写**了同名算法:

   * ``LZ4`` / ``LZ4HC`` —— 自研的 **LZ77 块压缩器**(哈希链匹配器 + 长度/距离对 +
     字面量run)。它的 token 流沿用了 LZ4 块格式的约定(高位半字节存字面量长度、
     低位半字节存匹配长度减 4),但**多了 6 字节魔数 + 4 字节原始长度**的头,
     而真正的 LZ4 块格式**没有这个头**。所以:本模块的输出喂给 lz4 库一定报错,
     lz4 库的输出喂给本模块也一定被魔数挡回去。
   * ``ZSTD`` —— 自研的 **LZ77 + 自适应算术编码** 压缩器(秩序 0 的字节模型、
     自适应的长度/偏移模型)。它与真实 zstd(其熵编码是 FSE/Huffman)在算法上
     同源但**格式完全不同**。

   换句话说:这三个名字代表的是「与真实实现同类的算法」,而不是「与真实实现兼容的格式」。
   如果你需要一个能和其他系统互通的压缩层,请换用 Python 的 ``lz4`` / ``zstandard`` 包;
   本模块的价值在于把“压缩到底做了什么”摊开给你看。

.. note::

   **Delta / DoubleDelta / Gorilla / T64 / FPC 是真的算法**(数值列专用,不做兼容性声明
   也不影响正确性,因为它们只在 mini-clickhouse 内部一读一写)。

   * ``Delta`` —— 算法思想同上游(一阶差分),但**粒度不同**:上游在整数值上做,我们在字节平面上做;
   * ``DoubleDelta`` / ``Gorilla`` / ``T64`` / ``FPC`` —— 实现了与真实实现相同的
     **算法思想**(二阶差分变长编码 / XOR+零计数 / 字节转置 / 双预测器),
     但我们自己的**具体位布局**在少数细节上与上游不同(每一处都在对应类的文档里
     画了图并标注了差异,例如 Gorilla 把 lz 字段从论文的 5 bit 放宽到 6 bit)。
     这些差异**不影响压缩率量级**,只影响“逐字节能不能和上游对上”。

   真实 CH 的这些编解码器支持 1..N 字节的元素宽度,mini-clickhouse 固定为 8 字节
   (``Delta`` 与 ``T64`` 可配参数),够教学用了。

校验和:真实的 ``CompressedWriteBuffer`` 用 **CityHash128**(16 字节)做块校验,
mini-clickhouse 用 :func:`minich.util.hashing.crc32`(4 字节)—— 这是**有意的替换**,
两者都能检出损坏,但校验值不同、长度不同。块头因此比真实 CH 短 12 字节。

.. code-block:: text

   CompressedWriteBuffer 的块布局(真实 CH 就是这个结构,只是校验字段换成 u32):

   ┌──────────────┬───────────────┬─────────────────┬───────────────┬─────────────┐
   │ method_byte  │ compressed_sz │ uncompressed_sz │ checksum(u32) │ payload ... │
   │   1 字节      │   u32 小端     │    u32 小端      │  crc32,小端   │  N 字节      │
   └──────────────┴───────────────┴─────────────────┴───────────────┴─────────────┘
        ↑ 0x02/0x82/0x90/0x91 …                        ↑ 对 payload 算 crc32

   一个输入最多切成 1 MiB 的块,每块单独压缩、单独校验 —— 这样“读第 3 个块”
   不需要先解开前两个块,是 ClickHouse「随机读」的前提。

真正的压缩率与耗时**不在本文档里写死**(文档里的数字会过期),而是由
:func:`benchmark_codecs` 现场跑出来。它的典型形状是:

* 通用编解码器在**随机字节**上几乎压不动(压缩率 ≈ 1.0,还要摊掉块头开销);
* 通用编解码器在**低基数字符串**(大量重复 URL)上非常好(几个数量级);
* 数值专用编解码器在**单调时间戳**上大放异彩(DoubleDelta 可以把 8 字节一个值
  压到“约 1 bit 一个值”,这就是它被说成“快 50×”的来源),但在**随机 UInt64**
  上基本无收益;Delta 自己要配合通用编解码器才出效果。
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence, Tuple

from .errors import ErrorCode, raise_error
from .serialization import read_var_uint, write_var_uint
from .util.hashing import crc32
from .util.stopwatch import ProfileEvents, Stopwatch

__all__ = [
    "CompressionMethodByte",
    "ICompressionCodec",
    "CompressionCodecNone",
    "CompressionCodecLZ4",
    "CompressionCodecLZ4HC",
    "CompressionCodecZSTD",
    "CompressionCodecDelta",
    "CompressionCodecDoubleDelta",
    "CompressionCodecGorilla",
    "CompressionCodecT64",
    "CompressionCodecFPC",
    "CompressionCodecMultiple",
    "CompressionCodecFactory",
    "CompressedWriteBuffer",
    "CompressedReadBuffer",
    "benchmark_codecs",
]

MASK64 = (1 << 64) - 1


# ══════════════════════════════════════════════════════════════════════════
# 压缩方法编号 —— 照抄真实 ClickHouse 的 src/Compression/CompressionMethod.h
# ══════════════════════════════════════════════════════════════════════════


class CompressionMethodByte:
    """压缩方法编号 —— 写在每个块的头一个字节上。

    这个编号的作用是:**读的时候不需要外部信息就知道用哪个解压器**。
    一个 part 文件里,不同列的块可能用不同 codec,甚至同一列的不同 granule
    也可能不同(算法会根据数据自适应),所以每个块都得自带编号。

    .. note::

       ``LZ4`` 与 ``LZ4HC`` **共用一个编号 0x82** —— 因为 LZ4HC("HC" = high
       compression)只改变**压缩端的搜索强度**,产出的字节格式和快速 LZ4
       完全一样,解压端不需要区分。这在真实 ClickHouse 里也是 0x82。
    """

    NONE = 0x02
    LZ4 = 0x82
    LZ4HC = 0x82          # 与 LZ4 同编号:只是压缩端更慢、压得更狠
    DELTA = 0x83
    T64 = 0x84
    DOUBLE_DELTA = 0x85
    GORILLA = 0x86
    FPC = 0x87
    DEFLATE_QPL = 0x88
    ZSTD = 0x90
    MULTIPLE = 0x91       # codec 链(CODEC(A, B) 这种)用这个编号
    ZSTD_QPL = 0x92


# ══════════════════════════════════════════════════════════════════════════
# 位级读写器 —— Gorilla / DoubleDelta / T64 / FPC 都要按“位”而不是“字节”打包
# ══════════════════════════════════════════════════════════════════════════


class _BitWriter:
    """把整数按**高位在前(MSB-first)**写进一个位流。

    为什么不直接按字节写? 因为这些编解码器的收益全在“**一个值只花几位**”上:
    Gorilla 用一个控制位表示“和上一个值 XOR 等于 0”,DoubleDelta 用一位表示
    “二阶差分等于 0”。如果按字节写,一个 0 也要占 8 位,收益就没了。

    ``write(value, nbits)`` 写入 ``value`` 的**低 nbits 位**(其余位丢弃),
    高位先写。``finish()`` 把最后一个不满 8 位的字节补齐并返回全部字节。
    """

    __slots__ = ("buf", "acc", "nbits")

    def __init__(self) -> None:
        self.buf = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            return
        value &= (1 << nbits) - 1
        self.acc = (self.acc << nbits) | value
        self.nbits += nbits
        while self.nbits >= 8:
            self.nbits -= 8
            self.buf.append((self.acc >> self.nbits) & 0xFF)
        self.acc &= (1 << self.nbits) - 1

    def finish(self) -> bytes:
        if self.nbits:
            self.buf.append((self.acc << (8 - self.nbits)) & 0xFF)
            self.acc = 0
            self.nbits = 0
        return bytes(self.buf)


class _BitReader:
    """ :class:`_BitWriter` 的逆操作。"""

    __slots__ = ("data", "pos", "acc", "nbits")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.acc = 0
        self.nbits = 0

    def read(self, nbits: int) -> int:
        if nbits <= 0:
            return 0
        while self.nbits < nbits:
            byte = self.data[self.pos] if self.pos < len(self.data) else 0
            self.pos += 1
            self.acc = (self.acc << 8) | byte
            self.nbits += 8
        self.nbits -= nbits
        value = (self.acc >> self.nbits) & ((1 << nbits) - 1)
        self.acc &= (1 << self.nbits) - 1
        return value


# ══════════════════════════════════════════════════════════════════════════
# 编解码器接口
# ══════════════════════════════════════════════════════════════════════════


class ICompressionCodec(ABC):
    """编解码器接口 —— 与真实 ClickHouse 的 ``ICompressionCodec`` 同名同义。

    子类需要提供:

    * :attr:`name` —— 人类可读的名字(``"Delta"``),用于错误信息和 codec 链描述;
    * :attr:`method_byte` —— 写进块头的编号(见 :class:`CompressionMethodByte`);
    * :meth:`do_compress` —— 真正的压缩逻辑(名字对齐真实 CH 的 ``doCompressData``);
    * :meth:`decompress` —— 解压;
    * :meth:`is_generic` —— 能不能压任意字节流(默认 ``True``);
    * :attr:`params` —— 构造参数的元组,用于把 codec 链序列化进块里。

    ``compress`` 是公开入口(做一次 ``bytes()`` 归一化再转发给 ``do_compress``),
    这样调用方既能用 ``codec.compress(x)`` 也能用 ``codec.do_compress(x)``。
    """

    #: 人类可读名字(子类覆盖)
    name: str = "abstract"
    #: 块头里的方法编号(子类覆盖)
    method_byte: int = 0
    #: 构造参数元组 —— 让 codec 链可以自描述地写进块里(子类覆盖)
    params: Tuple[int, ...] = ()

    def compress(self, data: bytes) -> bytes:
        """压缩 —— 公开入口。"""
        return self.do_compress(bytes(data))

    @abstractmethod
    def do_compress(self, data: bytes) -> bytes:
        """真正的压缩逻辑。"""

    @abstractmethod
    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        """解压出 ``uncompressed_size`` 字节。

        为什么要显式传原始长度? 因为像 Gorilla / DoubleDelta 这种编解码器
        **不把长度写进数据流**(真实 CH 也一样,长度来自块头或列类型)——
        解压端必须被告诉要产出多少字节。
        """

    def is_generic(self) -> bool:
        """是否是通用编解码器(能压任意字节)。

        默认 ``True``。Delta / DoubleDelta / Gorilla / T64 / FPC 覆盖为 ``False``
        —— 它们要求输入是**定宽数值**的字节,喂字符串会输出垃圾。
        """
        return True

    def byte_size_after_compress(self, data: bytes) -> int:
        """压缩后大约多少字节 —— 让调用方(比如自适应选 codec)先估算一下。

        默认就是真的压一遍量长度;能算准的子类可以覆盖成更便宜的估计。
        真实 CH 对应的概念是 ``getMaxCompressedDataSize`` 之类。
        """
        return len(self.do_compress(bytes(data)))

    def __repr__(self) -> str:  # pragma: no cover - 纯格式化
        if self.params:
            return f"<Codec {self.name}{self.params}>"
        return f"<Codec {self.name}>"


class CompressionCodecNone(ICompressionCodec):
    """``NONE`` —— 原样返回。

    看起来“什么都没做”的编解码器其实很有用:它是**对照组**。想知道 Delta 到底带来
    多少收益? 和 NONE 比。真实 ClickHouse 里 ``CODEC(NONE)`` 也是合法的,
    用在“数据本身已经压不动、不想再花 CPU”的列上(比如加密过的 blob)。
    """

    name = "NONE"
    method_byte = CompressionMethodByte.NONE

    def do_compress(self, data: bytes) -> bytes:
        return bytes(data)

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        out = bytes(data)
        if len(out) != uncompressed_size:
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"NONE codec expected {uncompressed_size} bytes, got {len(out)}",
            )
        return out


# ══════════════════════════════════════════════════════════════════════════
# LZ77 匹配器 —— LZ4 / LZ4HC / ZSTD 共用
# ══════════════════════════════════════════════════════════════════════════
#
# LZ77 的核心就一句话:如果“当前位置开始的若干字节”在“前面某处”出现过,
# 就不要再写一遍,只写 (能重复多长, 往前回退多远) —— 这就是一个 (长度, 距离) 对。
# 没找到匹配的字节照原样写出去,叫“字面量(literal)”。
#
# 难点全在“怎么快速找到前面哪里出现过”。工业实现都用**哈希链**:
#
#   ┌─ hash 表(head)──────────────────────────────────────────────────┐
#   │ head[hash(4字节)] ──▶ 最近一次出现这个 4 字节的位置(存 pos+1,0 表示空)│
#   └──────────────────────────────────────────────────────────────────┘
#   ┌─ prev 数组(链)──────────────────────────────────────────────────┐
#   │ prev[pos] ──▶ 上一个“撞同一个 hash 桶”的位置 ──▶ … ──▶ 空           │
#   └──────────────────────────────────────────────────────────────────┘
#
# 找匹配时:从 head[hash] 进去,沿 prev 链往回走,每步真的比字节、记下最长的那次。
# 走多深(max_chain)就是“快”与“压得狠”的旋钮:
#
#   LZ4  = 浅链(几步就走),快,ZIP 级别的一般压缩率;        ← max_chain 小
#   LZ4HC= 深链(走几十上百步),慢一点,压得更好;             ← max_chain 大
#   ZSTD = 深链 + 熵编码(见后面);                            ← max_chain 大 + 算术编码
#
# 窗口距离上限 65535:因为 LZ4 块格式用 u16 存距离。超过就够不着了。

_HASH_LOG = 16
_HASH_SIZE = 1 << _HASH_LOG
_MIN_MATCH = 4
_MAX_OFFSET = 65535


def _hash4(data: bytes, i: int) -> int:
    """把 ``data[i:i+4]`` 这 4 个字节哈希成 ``[0, _HASH_SIZE)``。

    用的是 Knuth 乘法哈希(乘一个奇数常量、取高位)—— 和真实 LZ4 的
    ``sequence * 2654435761 >> (32 - hashLog)`` 是同一套。
    """
    x = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
    return ((x * 2654435761) & 0xFFFFFFFF) >> (32 - _HASH_LOG)


def _lz77_parse(
    data: bytes, max_chain: int, nice_len: int
) -> List[Tuple[int, int, int, int]]:
    """把 ``data`` 解析成一串“序列”。

    每个序列是 ``(literal_start, literal_len, match_dist, match_len)``:
    先照抄 ``data[literal_start:literal_start+literal_len]`` 这一段字面量,
    再复制往前 ``match_dist`` 字节处开始的 ``match_len`` 字节。

    **最后一个序列一定是“只有字面量、没有匹配”** (``match_len == 0``)——
    这样解码端有一个确定的收尾条件。``data`` 为空时返回空列表。

    ``max_chain`` 控制沿哈希链能走多深;``nice_len`` 是“够好了,别再找了”的阈值
    (找到这么长的匹配就直接收手,省时间)。
    """
    n = len(data)
    if n == 0:
        return []
    seqs: List[Tuple[int, int, int, int]] = []
    head = [0] * _HASH_SIZE
    prev = [0] * n
    i = 0
    anchor = 0
    while i < n:
        best = 0
        bestd = 0
        if i + _MIN_MATCH <= n:
            h = _hash4(data, i)
            prev[i] = head[h]           # 把 i 插进链(prev 指向旧的 head)
            head[h] = i + 1             # head 记 pos+1,0 表示“空”
            cand = prev[i] - 1
            chain = max_chain
            limit = i - _MAX_OFFSET
            maxl = n - i
            while cand >= 0 and cand >= limit and chain > 0:
                chain -= 1
                # 快速排除:如果候选位置在“当前已知最佳长度”处就对不上,跳过
                if best and (i + best >= n or data[cand + best] != data[i + best]):
                    cand = prev[cand] - 1
                    continue
                l = 0
                while l < maxl and data[cand + l] == data[i + l]:
                    l += 1
                if l > best:
                    best = l
                    bestd = i - cand
                    if l >= nice_len:
                        break
                cand = prev[cand] - 1
        if best >= _MIN_MATCH:
            seqs.append((anchor, i - anchor, bestd, best))
            i += best
            anchor = i
        else:
            i += 1
    seqs.append((anchor, n - anchor, 0, 0))
    return seqs


# ── LZ4 块格式的 token 流(我们自己写的编码/解码)────────────────────────────
#
# 每个“序列”写成:
#
#   ┌────────┬──────────────────┬──────────┬───────────┬───────────────────────┐
#   │ token  │ (可选)字面量长度  │  字面量   │ 距离 u16  │ (可选)匹配长度扩展      │
#   │ 1 字节  │  若干 0xFF…+尾巴  │  N 字节   │  小端      │  若干 0xFF…+尾巴        │
#   └────────┴──────────────────┴──────────┴───────────┴───────────────────────┘
#      token = (字面量长度,封顶15) << 4 | (匹配长度-4,封顶15)
#      字面量长度 >= 15 时:后面补 [0xFF…, 尾字节] 的累加
#      匹配长度-4 >= 15 时:距离字节之后再补 [0xFF…, 尾字节]
#
# 这正是 LZ4 块格式的约定。但注意模块开头的 warning:我们**在整段前面加了
# 6 字节魔数 + 4 字节原始长度**,真实 LZ4 块没有这个头,所以两边不互通。


def _lz4_emit(
    out: bytearray, data: bytes, lit_start: int, lit_len: int, dist: int, match_len: int
) -> None:
    """把一个序列写进 ``out``。"""
    tpos = len(out)
    out.append(0)                       # 先占位,最后回填 token
    token = min(lit_len, 15) << 4
    if lit_len >= 15:
        rem = lit_len - 15
        while rem >= 255:
            out.append(255)
            rem -= 255
        out.append(rem)
    out += data[lit_start : lit_start + lit_len]
    if match_len:
        ml = match_len - _MIN_MATCH
        token |= min(ml, 15)
        out.append(dist & 0xFF)
        out.append((dist >> 8) & 0xFF)
        if ml >= 15:
            rem = ml - 15
            while rem >= 255:
                out.append(255)
                rem -= 255
            out.append(rem)
    out[tpos] = token


def _lz4_encode_tokens(data: bytes, seqs: Sequence[Tuple[int, int, int, int]]) -> bytes:
    out = bytearray()
    for lit_start, lit_len, dist, match_len in seqs:
        if lit_len == 0 and match_len == 0:
            continue                    # 末尾那个空序列不用写
        _lz4_emit(out, data, lit_start, lit_len, dist, match_len)
    return bytes(out)


def _lz4_decode_tokens(tokens: bytes, original_size: int) -> bytes:
    out = bytearray()
    pos = 0
    while len(out) < original_size:
        token = tokens[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = tokens[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        out += tokens[pos : pos + lit_len]
        pos += lit_len
        if len(out) >= original_size:
            break
        dist = tokens[pos] | (tokens[pos + 1] << 8)
        pos += 2
        ml = token & 0x0F
        if ml == 15:
            while True:
                b = tokens[pos]
                pos += 1
                ml += b
                if b != 255:
                    break
        ml += _MIN_MATCH
        start = len(out) - dist
        for k in range(ml):            # 重叠复制:dist < ml 时逐字节向外扩
            out.append(out[start + k])
    return bytes(out)


_LZ4_MAGIC = b"MCHLZ1"      # 6 字节魔数 —— 用来把“别人的 LZ4”挡在门外


class _CompressionCodecLZ4Base(ICompressionCodec):
    """LZ4 家族共用的实现(快模式与 HC 模式只差哈希链深度)。"""

    name = "LZ4"
    method_byte = CompressionMethodByte.LZ4
    params: Tuple[int, ...] = ()

    def __init__(self, max_chain: int, nice_len: int) -> None:
        self.max_chain = max_chain
        self.nice_len = nice_len

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        seqs = _lz77_parse(data, self.max_chain, self.nice_len)
        body = _lz4_encode_tokens(data, seqs)
        return _LZ4_MAGIC + struct.pack("<I", len(data)) + body

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        if data[:6] != _LZ4_MAGIC:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                "LZ4 codec: missing mini-clickhouse magic header. "
                "This stream was NOT produced by mini-clickhouse (real LZ4 output "
                "is not accepted); see minich/compression.py for details.",
            )
        stored_size = struct.unpack_from("<I", data, 6)[0]
        if stored_size != uncompressed_size:
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"LZ4 codec: embedded size {stored_size} != requested {uncompressed_size}",
            )
        return _lz4_decode_tokens(data[10:], uncompressed_size)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Codec {self.name}(max_chain={self.max_chain})>"


class CompressionCodecLZ4(_CompressionCodecLZ4Base):
    """``LZ4`` —— LZ77 **快模式**:哈希链很浅(默认 4 步)。

    浅链的意思是:遇到哈希冲突,只在最近几个候选里找最长匹配就收手。
    这换来了速度,代价是压缩率不如 HC 模式。真实 LZ4 的 ``LZ4_compress_default``
    也是这个思路(它甚至只记住“最近一个”位置)。

    .. warning::
       产出**不是**真实 LZ4 格式,详见模块开头的 ``.. warning::``。
    """

    name = "LZ4"

    def __init__(self, level: int = 1) -> None:
        # 快模式基本不看 level,给一个固定的浅链。
        super().__init__(max_chain=4, nice_len=64)


class CompressionCodecLZ4HC(_CompressionCodecLZ4Base):
    """``LZ4HC`` —— LZ77 **高压缩模式**:哈希链更深,压得更狠。

    ``level`` (1..12)控制链深:level 越大,``max_chain`` 越大,找匹配越费时间。
    真实 ClickHouse / lz4 的 ``LZ4HC`` 也是这个语义(压缩慢、解压和 LZ4 一样快)。

    .. warning::
       产出**不是**真实 LZ4 格式,详见模块开头的 ``.. warning::``。
       ``LZ4`` 与 ``LZ4HC`` 的**解码器完全相同**(方法字节都是 0x82)。
    """

    name = "LZ4HC"

    def __init__(self, level: int = 9) -> None:
        if level < 1:
            level = 1
        if level > 12:
            level = 12
        self.level = level
        self.params = (level,)
        super().__init__(max_chain=min(4 << (level - 1), 256), nice_len=128)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Codec LZ4HC(level={self.level})>"


# ══════════════════════════════════════════════════════════════════════════
# 区间编码器(range coder)—— ZSTD 的熵编码层
# ══════════════════════════════════════════════════════════════════════════
#
# LZ77 只做“去重复”:它把重复片段换成 (长度, 距离)。但换出来的“长度 / 距离 /
# 字面量”本身还有偏斜 —— 比如匹配长度往往是 4、5、6 这种小值,距离往往是“最近用过的”。
# 熵编码就是**利用这种偏斜**,给常见符号分配更少的位。
#
# 真实 zstd 用 FSE(有限状态熵)+ Huffman;这里用一个**自适应二进制算术编码器**
# (LZMA 那套 11 位概率 + 移位更新的经典实现)。它的好处是:码表**不需要先统计再传输**,
# 编解码双方从“概率都是 0.5”出发,每处理一个 bit 就朝实际分布挪一点点,自然收敛。
#
# 原理一句话:把整个输出看成一个 [0,1) 区间,每编码一个 bit 就按“这个 bit 的概率”
# 把区间切一刀、留一半。最后把落在哪个小区间用一个数表示出来 —— 出现概率高的 bit
# 分到的区间大,需要的位数就少。这是“压缩的下界”(香农熵)能达到的办法。


class _RangeEncoder:
    """LZMA 风格的二进制算术编码器(编码端)。"""

    __slots__ = ("low", "range", "cache", "cache_size", "out")

    def __init__(self) -> None:
        self.low = 0
        self.range = 0xFFFFFFFF
        self.cache = 0
        self.cache_size = 1
        self.out = bytearray()

    def _shift_low(self) -> None:
        low = self.low
        if low < 0xFF000000 or low > 0xFFFFFFFF:
            carry = low >> 32
            temp = self.cache
            while True:
                self.out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if self.cache_size == 0:
                    break
            self.cache = (low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (low << 8) & 0xFFFFFFFF

    def encode_bit(self, probs: List[int], i: int, bit: int) -> None:
        """按概率模型 ``probs[i]`` 编码一个 bit,并把模型朝这个 bit 更新一点。

        ``probs[i]`` 是“下一个 bit 为 **0**”的概率 × 2048。更新用的是
        ``p += (2048 - p) >> 5``(偏向 0)或 ``p -= p >> 5``(偏向 1)——
        步长 ``1/32`` 是 LZMA 的经典取值:太快会震荡,太慢收敛不了。
        """
        p = probs[i]
        bound = (self.range >> 11) * p
        if bit == 0:
            self.range = bound
            probs[i] = p + ((2048 - p) >> 5)
        else:
            self.low += bound
            self.range -= bound
            probs[i] = p - (p >> 5)
        while self.range < (1 << 24):
            self.range = (self.range << 8) & 0xFFFFFFFF
            self._shift_low()

    def flush(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


class _RangeDecoder:
    """ :class:`_RangeEncoder` 的逆操作。"""

    __slots__ = ("data", "pos", "code", "range")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos
        self.code = 0
        self.range = 0xFFFFFFFF
        for _ in range(5):
            self.code = ((self.code << 8) | self._read()) & 0xFFFFFFFF

    def _read(self) -> int:
        if self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            return b
        return 0

    def decode_bit(self, probs: List[int], i: int) -> int:
        p = probs[i]
        bound = (self.range >> 11) * p
        if self.code < bound:
            self.range = bound
            probs[i] = p + ((2048 - p) >> 5)
            bit = 0
        else:
            self.range -= bound
            self.code -= bound
            probs[i] = p - (p >> 5)
            bit = 1
        while self.range < (1 << 24):
            self.range = (self.range << 8) & 0xFFFFFFFF
            self.code = ((self.code << 8) | self._read()) & 0xFFFFFFFF
        return bit


def _enc_bittree(enc: _RangeEncoder, probs: List[int], value: int, nbits: int) -> None:
    """用一棵“二叉树”模型编码一个 ``nbits`` 位整数(每个内部节点一个概率)。

    这就是 LZMA 的 ``BitTree``:从一个根节点出发,按每一位 0/1 决定往左还是往右,
    走到哪个叶子就是哪个符号。共 ``2^nbits`` 个叶子、``2^nbits`` 个概率槽。
    """
    m = 1
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        enc.encode_bit(probs, m, bit)
        m = (m << 1) | bit


def _dec_bittree(dec: _RangeDecoder, probs: List[int], nbits: int) -> int:
    m = 1
    for _ in range(nbits):
        bit = dec.decode_bit(probs, m)
        m = (m << 1) | bit
    return m - (1 << nbits)


class _UIntModel:
    """无符号整数的自适应模型:按 7 位一组(低位在前)编码,每组一个“还有下一组吗”的 bit。

    这样小整数(0..127)只花 8 bit 左右,大整数花得多 —— 正好贴合“长度 / 距离
    大多是小值”的事实。每组一个独立的 bit-tree,组内 7 位按位编码。
    """

    __slots__ = ("trees", "conts")

    def __init__(self, max_chunks: int = 10) -> None:
        self.trees = [[1024] * 128 for _ in range(max_chunks)]
        self.conts = [1024] * (max_chunks + 1)

    def encode(self, enc: _RangeEncoder, value: int) -> None:
        k = 0
        while True:
            chunk = value & 0x7F
            _enc_bittree(enc, self.trees[k], chunk, 7)
            value >>= 7
            if value:
                enc.encode_bit(self.conts, k, 1)
                k += 1
            else:
                enc.encode_bit(self.conts, k, 0)
                return

    def decode(self, dec: _RangeDecoder) -> int:
        value = 0
        shift = 0
        k = 0
        while True:
            chunk = _dec_bittree(dec, self.trees[k], 7)
            value |= chunk << shift
            if dec.decode_bit(self.conts, k):
                shift += 7
                k += 1
            else:
                return value


_ZSTD_MAGIC = b"MCHZS1"


class CompressionCodecZSTD(ICompressionCodec):
    """``ZSTD`` —— LZ77(**深**哈希链) + 自适应算术编码。

    和 LZ4 的两点区别,也正是真实 zstd 相对 LZ4 的区别:

    1. **搜得更狠**:``max_chain`` 更大(默认 32),更可能找到更长的匹配;
    2. **压得更细**:找到的“字面量 / 长度 / 距离”再走一遍熵编码器
       (见本文件上面的区间编码器),把偏斜吃干净。

    真实 zstd 的熵编码是 FSE/Huffman,这里是自适应算术编码 —— **同源但不同格式**。
    详见模块开头的 ``.. warning::``。

    每次 ``do_compress`` / ``decompress`` 都**新建**一套概率模型(它们是有状态的:
    处理这个块时学到的分布不能带到下一个块,否则解码端对不上)。
    """

    name = "ZSTD"
    method_byte = CompressionMethodByte.ZSTD

    def __init__(self, level: int = 3) -> None:
        if level < 1:
            level = 1
        if level > 22:
            level = 22
        self.level = level
        self.params = (level,)
        self.max_chain = min(32 * level, 256)
        self.nice_len = 256

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        seqs = _lz77_parse(data, self.max_chain, self.nice_len)
        enc = _RangeEncoder()
        seq_model = _UIntModel()
        litlen_model = _UIntModel()
        mlen_model = _UIntModel()
        off_model = _UIntModel()
        lit_model = [1024] * 256          # 8 位字面量的 bit-tree
        seq_model.encode(enc, len(seqs))
        last = len(seqs) - 1
        for idx, (ls, ll, dist, ml) in enumerate(seqs):
            litlen_model.encode(enc, ll)
            for k in range(ls, ls + ll):
                _enc_bittree(enc, lit_model, data[k], 8)
            if idx != last:
                mlen_model.encode(enc, ml - _MIN_MATCH)
                off_model.encode(enc, dist)
        body = enc.flush()
        return _ZSTD_MAGIC + struct.pack("<I", len(data)) + body

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        if data[:6] != _ZSTD_MAGIC:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                "ZSTD codec: missing mini-clickhouse magic header. "
                "This stream was NOT produced by mini-clickhouse (real zstd output "
                "is not accepted); see minich/compression.py for details.",
            )
        stored_size = struct.unpack_from("<I", data, 6)[0]
        if stored_size != uncompressed_size:
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"ZSTD codec: embedded size {stored_size} != requested {uncompressed_size}",
            )
        dec = _RangeDecoder(data, 10)
        seq_model = _UIntModel()
        litlen_model = _UIntModel()
        mlen_model = _UIntModel()
        off_model = _UIntModel()
        lit_model = [1024] * 256
        num = seq_model.decode(dec)
        out = bytearray()
        for idx in range(num):
            ll = litlen_model.decode(dec)
            for _ in range(ll):
                out.append(_dec_bittree(dec, lit_model, 8))
            if idx != num - 1:
                ml = mlen_model.decode(dec) + _MIN_MATCH
                dist = off_model.decode(dec)
                start = len(out) - dist
                for k in range(ml):
                    out.append(out[start + k])
        return bytes(out)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Codec ZSTD(level={self.level})>"


# ══════════════════════════════════════════════════════════════════════════
# Delta —— 一阶差分(真实 ClickHouse 在**值**上做,这里在**字节平面**上做)
# ══════════════════════════════════════════════════════════════════════════
#
# 这可能是所有编解码器里最反直觉、也最有教学价值的一个。
#
# 先看真实 ClickHouse 怎么做。``CompressionCodecDelta::compressDataForType<T>``
# 是一段很短的循环:
#   T prev_src = 0;
#   while (…) { T curr = load<T>(src); store<T>(dst, curr - prev_src); prev_src = curr; }
#
# 它在**整个 T 类型上**做整数减法(``T`` 取 UInt8/UInt16/UInt32/UInt64),进位与借位
# 会在整个宽度上传播;头部一个字节存 ``delta_bytes_size``(1/2/4/8),解压端做一次
# **包含式前缀和**(``accumulator += delta``)。
#
# 我们这里**没有照抄**,做的是**逐字节平面**的差分:
#   把每个定宽整数拆成 W 个“字节平面”(plane 0 = 每值最低字节,plane 1 = 次低字节,…),
#   然后对**每个平面**单独做 ``out[plane][k] = in[plane][k] - in[plane][k-1]`` (mod 256)。
#
# 能这么偷懒,是因为 codec 这一层只拿到“一坨字节 + 一个元素宽度 W”,按字节差分
# 就能对所有定宽整数用同一套代码。两种做法在**高低字节都变化**的列上结果不同
# (字节平面不做进位传播),这是本实现与真实 CH 的一处**已知偏差**:它不影响往返
# 正确性,但**不要期待与官方库字节兼容**。
#
# 不变的是它的定位:Delta 是**预处理器**,自己**不缩小体积**(输出与输入等长),
# 只是把“单调递增”变成“一堆 0 加一个小的低字节”,让后面的 LZ4/ZSTD 好压。
# 所以 ``CODEC(Delta)`` 这种“只有专用 codec”的写法在真实 CH 里会被拒绝,必须配通用 codec。
#
# 看一个例子(UInt64,值 1000,1001,1002,只画低 2 字节):
#   原始低字节平面:  0xE8 0xE9 0xEA
#   Delta 之后:      0xE8 0x01 0x01    ← 每个差值常常很小,且大量相同 → 好压
#   (高字节平面本来就是 0x00,差分后还是 0x00 → 更好压)


class CompressionCodecDelta(ICompressionCodec):
    """``Delta`` —— 定宽整数的逐字节平面一阶差分(纯变换,不改变长度)。

    ``element_size`` (W)是元素宽度(字节)。默认 1 —— 对任意字节流都能安全往返,
    但真正有用的是配到列类型上(UInt64 → 8,UInt32 → 4,DateTime → 4…)。

    .. note::

       真实 CH 里 W 由**列类型**推出来,写进表元数据;mini-clickhouse 的 codec
       只拿到裸字节,所以 W 是构造参数。这也意味着 ``CODEC(Delta)``(不写参数)
       在真实 CH 里 W 来自类型,在我们这儿默认成 1。要压时间戳请写 ``Delta(8)``。
    """

    name = "Delta"
    method_byte = CompressionMethodByte.DELTA

    def __init__(self, element_size: int = 1) -> None:
        if element_size < 1 or element_size > 64:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Delta codec element size must be in [1, 64], got {element_size}",
            )
        self.element_size = element_size
        self.params = (element_size,)

    def is_generic(self) -> bool:
        return False

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        n = len(data)
        w = self.element_size
        if n == 0:
            return b""
        out = bytearray(n)
        prefix = min(w, n)
        out[:prefix] = data[:prefix]
        for i in range(prefix, n):
            out[i] = (data[i] - data[i - w]) & 0xFF
        return bytes(out)

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        n = uncompressed_size
        w = self.element_size
        if len(data) < n:
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"Delta codec: need {n} bytes, have {len(data)}",
            )
        out = bytearray(data[:n])
        prefix = min(w, n)
        for i in range(prefix, n):
            out[i] = (out[i] + out[i - w]) & 0xFF
        return bytes(out)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Codec Delta(element_size={self.element_size})>"


# ══════════════════════════════════════════════════════════════════════════
# DoubleDelta —— 二阶差分 + 变长编码(时间戳/计数器列的神器)
# ══════════════════════════════════════════════════════════════════════════
#
# 上一节 Delta 做的是“一阶差分”。DoubleDelta 更进一步:**对差分再差分**
# (二阶差分,double-delta):
#
#   delta[i]  = value[i] - value[i-1]
#   dd[i]     = delta[i] - delta[i-1]            ← 这才是要编码的东西
#
# 对**规律采样的时间戳**(比如每秒一个点、每个点加 1 秒),delta 恒等于 1,
# 于是 dd 恒等于 0 —— 一整列 8 字节的时间戳,二阶差分起来**全是 0**。
#
# 于是编码策略就很自然了:
#
#   值 0:一个 bit 就够 —— 写一个 ``0``。
#   值非 0:先写一个 ``1``,再把 zigzag(dd) 用变长整数写出去。
#
# 对每秒一个点的时间戳:开头写两个 8 字节(首值和首个 delta),之后**每个值只花 1 bit**。
# 100 万个值原本 8 MiB,压完约 125 KiB —— 这就是“约 50×”的来历。
#
# .. code-block:: text
#
#   位流布局(全部 MSB-first):
#
#   ┌────────────────┬────────────────────┬────────────────────────────────────┐
#   │ 首值 (64 bit)   │ 首个 delta (64 bit) │ 对 i>=2 的每个值:                   │
#   └────────────────┴────────────────────┴────────────────────────────────────┘
#                                        ┌─ 1 bit: 0 → dd == 0,结束
#                                        └─ 1 bit: 1 → 接着是一个变长 zigzag(dd)
#                                                      │
#                                                      └─ 7 bit 一组,每组的第 8 位是
#                                                         “后面还有吗”(1=有)
#
#   zigzag 把有符号的 dd 映射成无符号:0→0, -1→1, 1→2, -2→3, 2→4 …
#   这样“小的正负波动”都落在小整数区,变长编码才占得少。
#
# 真实 CH 的 ``DoubleDeltaCodec`` 也是“二阶差分 + 变长编码”,我们这里的变长是
# zigzag + 7 位组;CH 的位切分略有不同,但**算法思想一致、压缩率行为一致**。
# 元素宽度固定 8 字节(UInt64 / Int64 / DateTime64 / 大部分计数器都是这个宽度)。

_DD_VALUES_PER_ELEM = 8


def _zigzag(v: int) -> int:
    """有符号 → 无符号:0→0, -1→1, 1→2, -2→3 …"""
    return (v << 1) ^ (v >> 63)


def _unzigzag(v: int) -> int:
    return (v >> 1) ^ (-(v & 1))


class CompressionCodecDoubleDelta(ICompressionCodec):
    """``DoubleDelta`` —— 二阶差分 + 变长编码。**它是非通用的**:只吃 8 字节定宽数值。

    输入按**小端** 8 字节一组解释成 u64(与 ClickHouse 的列序列化一致),
    不足 8 字节的尾巴原样放在最前面(``uncompressed_size % 8`` 个字节)。
    算术全程 mod 2^64,所以负数、溢出(绕回)的整数都能无损往返。
    """

    name = "DoubleDelta"
    method_byte = CompressionMethodByte.DOUBLE_DELTA

    def is_generic(self) -> bool:
        return False

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        n = len(data)
        values = n // _DD_VALUES_PER_ELEM
        tail = data[values * _DD_VALUES_PER_ELEM :]     # 末尾不足 8 字节的部分
        bw = _BitWriter()
        if values >= 1:
            v0 = int.from_bytes(data[0:8], "little")
            bw.write(v0, 64)
        if values >= 2:
            v1 = int.from_bytes(data[8:16], "little")
            delta = (v1 - v0) & MASK64
            bw.write(delta, 64)
            prev = v1
            prev_delta = delta
            for i in range(2, values):
                v = int.from_bytes(data[i * 8 : i * 8 + 8], "little")
                d = (v - prev) & MASK64
                dd = (d - prev_delta) & MASK64
                signed = dd - (1 << 64) if dd >= (1 << 63) else dd
                zz = _zigzag(signed)
                if zz == 0:
                    bw.write(0, 1)
                else:
                    bw.write(1, 1)
                    z = zz
                    while z >= 128:
                        bw.write((z & 0x7F) | 0x80, 8)
                        z >>= 7
                    bw.write(z, 8)
                prev = v
                prev_delta = d
        return tail + bw.finish()

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        n = uncompressed_size
        tlen = n % _DD_VALUES_PER_ELEM
        values = n // _DD_VALUES_PER_ELEM
        tail = data[:tlen]
        br = _BitReader(data[tlen:])
        out = bytearray()
        if values >= 1:
            v0 = br.read(64)
            out += v0.to_bytes(8, "little")
        if values >= 2:
            delta = br.read(64)
            v1 = (v0 + delta) & MASK64
            out += v1.to_bytes(8, "little")
            prev = v1
            prev_delta = delta
            for _ in range(2, values):
                flag = br.read(1)
                if flag == 0:
                    dd = 0
                else:
                    zz = 0
                    shift = 0
                    while True:
                        b = br.read(8)
                        zz |= (b & 0x7F) << shift
                        if b & 0x80:
                            shift += 7
                        else:
                            break
                    signed = _unzigzag(zz)
                    dd = signed & MASK64
                d = (prev_delta + dd) & MASK64
                v = (prev + d) & MASK64
                out += v.to_bytes(8, "little")
                prev = v
                prev_delta = d
        return out + tail

    def __repr__(self) -> str:  # pragma: no cover
        return "<Codec DoubleDelta>"


# ══════════════════════════════════════════════════════════════════════════
# Gorilla —— XOR + 前导/后缀零计数(来自 Facebook Gorilla 论文)
# ══════════════════════════════════════════════════════════════════════════
#
# Gorilla 是 Facebook 那篇著名的时序数据库论文(《Gorilla: A Fast, Scalable,
# In-Memory Time Series Database》,2015)提出的浮点/整数压缩法。ClickHouse 实现了它。
#
# 核心观察:时间序列里**相邻的浮点数往往很接近** —— 它们的二进制表示只有低位不同,
# 高位(long 的符号/指数/尾数高位)常常一模一样。所以:
#
#   先算 xor = value[i] ^ value[i-1]  →  相同的位变成 0,不同的位留下。
#
# 如果两个值完全相同,xor 就是 0 —— 只写**一个 bit ``0``** 就完事。
# 不然,xor 的“中间一段”才有用:去掉前面的一串 0(前导零)和后面的一串 0(后缀零),
# 只把中间那段“有意义的位”写出去。而且如果这次的“前导/后缀零范围”比上次的窄,
# 还能复用上次的范围。
#
# .. code-block:: text
#
#   xor = 0b00000000_00000000_00000000_00100101_00000000_00000000_00000000_00000000
#          └──── 前导零 lz=26 ────┘└─ 有意义位 ─┘└──── 后缀零 tz=27 ────┘
#
#   每个值(V0 之后)的位流:
#
#     1 bit: 0  → xor == 0,值不变,结束
#     1 bit: 1  → xor != 0,继续:
#        ├ 1 bit: 0  → **复用**上次的 (lz, tz),接下来只写“有意义位”
#        └ 1 bit: 1  → **新的** (lz, tz),接下来写:
#                     lz        (6 bit)
#                     (有意义位数 - 1) (6 bit)   ← 由此可推出 tz = 64 - lz - 有意义位数
#                     有意义位  (若干 bit)
#
#   .. note::
#      论文原文用 5 bit 存 lz、6 bit 存“有意义位数”。但 5 bit 只能表示 0..31,
#      对**完整的 64 位值**不够(前导零可以到 63)。mini-clickhouse 用 **6 bit 存 lz**,
#      6 bit 存“有意义位数-1”,这样 64 位全范围都覆盖。这是**有意的修正**,特此说明。
#
# 元素宽度固定 8 字节。首值原样 64 bit;不足 8 字节的尾巴照 DoubleDelta 那样处理。


class CompressionCodecGorilla(ICompressionCodec):
    """``Gorilla`` —— XOR + 变长“有意义位”编码。**非通用**,只吃 8 字节定宽数值。"""

    name = "Gorilla"
    method_byte = CompressionMethodByte.GORILLA

    def is_generic(self) -> bool:
        return False

    @staticmethod
    def _leading_zeros(x: int) -> int:
        if x == 0:
            return 64
        return 64 - x.bit_length()

    @staticmethod
    def _trailing_zeros(x: int) -> int:
        if x == 0:
            return 64
        return (x & -x).bit_length() - 1

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        n = len(data)
        tlen = n % 8
        values = n // 8
        tail = data[values * 8 :]
        bw = _BitWriter()
        if values == 0:
            return tail
        prev = int.from_bytes(data[0:8], "little")
        bw.write(prev, 64)
        prev_lz = -1
        prev_tz = -1
        for i in range(1, values):
            cur = int.from_bytes(data[i * 8 : i * 8 + 8], "little")
            xor = cur ^ prev
            if xor == 0:
                bw.write(0, 1)
            else:
                bw.write(1, 1)
                lz = self._leading_zeros(xor)
                tz = self._trailing_zeros(xor)
                if prev_lz >= 0 and lz >= prev_lz and tz >= prev_tz:
                    bw.write(0, 1)                       # 复用上次的范围
                    width = 64 - prev_lz - prev_tz
                    bw.write(xor >> prev_tz, width)
                else:
                    bw.write(1, 1)                       # 新的范围
                    meaningful = 64 - lz - tz
                    bw.write(lz, 6)
                    bw.write(meaningful - 1, 6)
                    bw.write(xor >> tz, meaningful)
                    prev_lz = lz
                    prev_tz = tz
            prev = cur
        return tail + bw.finish()

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        n = uncompressed_size
        tlen = n % 8
        values = n // 8
        tail = data[:tlen]
        br = _BitReader(data[tlen:])
        out = bytearray()
        if values == 0:
            return tail
        prev = br.read(64)
        out += prev.to_bytes(8, "little")
        prev_lz = -1
        prev_tz = -1
        for _ in range(1, values):
            if br.read(1) == 0:
                cur = prev
            else:
                if br.read(1) == 0:
                    width = 64 - prev_lz - prev_tz
                    xor = br.read(width) << prev_tz
                else:
                    lz = br.read(6)
                    meaningful = br.read(6) + 1
                    tz = 64 - lz - meaningful
                    xor = br.read(meaningful) << tz
                    prev_lz = lz
                    prev_tz = tz
                cur = (prev ^ xor) & MASK64
            out += cur.to_bytes(8, "little")
            prev = cur
        return out + tail

    def __repr__(self) -> str:  # pragma: no cover
        return "<Codec Gorilla>"


# ══════════════════════════════════════════════════════════════════════════
# T64 —— 字节转置 + 丢常量平面 + 位打包
# ══════════════════════════════════════════════════════════════════════════
#
# T64 的想法和 Delta 是一路的:数值列的**字节之间有很强的规律**,但原始的
# “值挨着值”的布局把它藏起来了。转置一下,规律就露出来。
#
#   原始布局(小端,值挨着值):
#     v0: b0 b1 b2 … bW-1 | v1: b0 b1 b2 … bW-1 | v2: …
#
#   转置后(同一个字节位置的挤在一起):
#     plane0: v0.b0 v1.b0 v2.b0 …   ← 所有值的最低字节
#     plane1: v0.b1 v1.b1 v2.b1 …   ← 次低字节
#     …
#     planeW-1: v0.bW-1 v1.bW-1 …   ← 最高字节(常常全是 0,或全是 0xFF)
#
# 转置之后做两件事:
#
#   1. **丢常量平面**:如果某个平面所有字节都相同(比如高位平面全是 0,
#      或者全是 0xFF —— 负数的符号扩展),就只记“这个平面是常量 C”,不存数据。
#      对小整数列,大部分高位平面都是全 0,一下子全丢了。
#   2. **位打包**:剩下的平面里,每个字节往往用不到 8 位(比如某平面最大只到 3,
#      那每个字节 2 bit 就够)。所以按该平面的“最大值的位宽”打包。
#
# .. code-block:: text
#
#   输出 = [尾巴字节] + [W 一个字节] + 位流:
#
#   位流开头是每个平面的头:
#     ┌──── 4 bit ────┬──── 8 bit(仅当上面是 0 时)────┐  ×W 个平面
#     │ 该平面的位宽   │ 常量填充值(该平面全是它)      │
#     │ 0 = 常量平面   │                              │
#     └───────────────┴──────────────────────────────┘
#   位流后面是所有“非常量平面”的打包数据:每个值占 `位宽` bit。
#
# ``CODEC(T64)`` 单独用就够了(它自己就能压);和通用 codec 串起来也可以
# (转置后的字节对 LZ4 更友好)。
#
# .. note::
#   真实 CH 的 T64 也是“转置 + 压缩常量/高位”;我们这里的**具体头格式是
#   mini-clickhouse 自己的**(4 bit 位宽 + 可选 8 bit 常量),为的是把“位打包”
#   这件事画清楚。算法思想一致。


class CompressionCodecT64(ICompressionCodec):
    """``T64`` —— 定宽数值的字节转置编解码器。**非通用**。"""

    name = "T64"
    method_byte = CompressionMethodByte.T64

    def __init__(self, width: int = 8) -> None:
        if width < 1 or width > 64:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"T64 codec width must be in [1, 64], got {width}",
            )
        self.width = width
        self.params = (width,)

    def is_generic(self) -> bool:
        return False

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        n = len(data)
        w = self.width
        values = n // w
        tail = data[values * w :]
        limit = values * w
        # 转置:planes[j][k] = 第 k 个值的第 j 个字节(小端,plane 0 = 最低字节)
        planes = [data[j:limit:w] for j in range(w)]
        # 第一遍:决定每个平面是“常量”(填 0..255)还是“位宽打包”
        infos: List[Tuple[int, int]] = []         # (位宽, 常量填充值;位宽 0 时后者有效)
        plan: List[Tuple[bytes, int]] = []        # 需要打包的平面 + 位宽
        for j in range(w):
            plane = planes[j]
            if values == 0:
                infos.append((0, 0))
                continue
            lo = min(plane)
            hi = max(plane)
            if lo == hi:
                infos.append((0, hi))
            else:
                bits = hi.bit_length()
                infos.append((bits, 0))
                plan.append((plane, bits))
        # 第二遍:头和打包数据写进**同一个**位流(中间不能字节对齐,否则读端对不上)
        bw = _BitWriter()
        for bits, fill in infos:
            bw.write(bits, 4)
            if bits == 0:
                bw.write(fill, 8)
        for plane, bits in plan:
            for byte in plane:
                bw.write(byte, bits)
        return tail + bytes([w]) + bw.finish()

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        n = uncompressed_size
        w = self.width
        values = n // w
        tlen = n % w
        tail = data[:tlen]
        p = tlen
        stored_w = data[p]
        p += 1
        if stored_w != w:
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"T64 codec: width {stored_w} != {w}",
            )
        br = _BitReader(data[p:])
        planes: List[List[int]] = []
        widths: List[int] = []
        for _ in range(w):
            bits = br.read(4)
            if bits == 0:
                fill = br.read(8)
                planes.append([fill] * values)
                widths.append(0)
            else:
                planes.append([0] * values)
                widths.append(bits)
        # 头(每个平面 4 bit 位宽,常量平面再跟 8 bit 填充值)和打包数据是**同一个
        # 位流**里连续的位,顺序读完即可 —— 好处是不受 w 奇偶影响(中间不会字节对齐)。
        for j in range(w):
            bits = widths[j]
            if bits:
                plane = planes[j]
                for k in range(values):
                    plane[k] = br.read(bits)
        out = bytearray(values * w)
        for j in range(w):
            plane = planes[j]
            for k in range(values):
                out[k * w + j] = plane[k]
        return bytes(out) + tail

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Codec T64(width={self.width})>"


# ══════════════════════════════════════════════════════════════════════════
# FPC —— 两个预测器(FCM / DFCM)+ 2 位选择子
# ══════════════════════════════════════════════════════════════════════════
#
# FPC (Fast Parallel Compression) 来自 Burtscher & Ratanaworabhan 的论文
# 《FPC: A High-Speed Compressor for Double-Precision Floating-Point Data》。
# ClickHouse 用它来压浮点列。
#
# 它和 Gorilla 解决的是同一类问题(相邻值很接近),但办法不同:Gorilla 是“XOR + 零计数”,
# FPC 是“**拿两个预测器去猜下一个值**,猜得越准越好”。
#
# 两个预测器,各自的预测都放进一张哈希表,表的索引是“上一个值的哈希”:
#
#   ┌─ 预测器 1:FCM (Frequent Value Predictor)───────────────────────────────┐
#   │ 表的每一项直接存“上次在这个上下文里见到的值”。                          │
#   │ 预测:pred1 = fcm[h(prev)]                                              │
#   │ 更新:fcm[h(prev)] = cur                                                │
#   └───────────────────────────────────────────────────────────────────────┘
#   ┌─ 预测器 2:DFCM (Differential Finite Context Method)────────────────────┐
#   │ 表的每一项存“上次在这个上下文里见到的步长(stride)”。                    │
#   │ 预测:pred2 = prev + dfcm[h(prev)]                                      │
#   │ 更新:dfcm[h(prev)] = cur - prev                                        │
#   └───────────────────────────────────────────────────────────────────────┘
#
# 对每个值 cur:分别算 `xor1 = cur ^ pred1`、`xor2 = cur ^ pred2`,选**前导零更多**的那个
# (前导零越多,后面要写的“有意义字节”越少),用一个 **2 位选择子**告诉解码端选了哪个。
# 若两个都不省(前导零为 0),就原样写。
#
#   每个值的位流:
#     ┌──── 2 bit 选择子 ────┬──────────────────────────────────────────────┐
#     │ 0 = 用 pred1 (FCM)   │ 4 bit 前导零字节数 lzb (0..8)                  │
#     │ 1 = 用 pred2 (DFCM)  │ 接着写 (8 - lzb) 个字节 = xor 的低位有效字节   │
#     ├──────────────────────┼──────────────────────────────────────────────┤
#     │ 2 = 原样             │ 直接写 64 bit 的 cur                          │
#     └──────────────────────┴──────────────────────────────────────────────┘
#
#   lzb = 8 表示 xor == 0(完全猜中),此时不写任何有效字节,只花 2+4=6 bit。
#
# 元素宽度固定 8 字节。坐标表和“上一个值”在编解码两端**同步**地推进,所以完全确定、
# 无损。哈希函数用的是论文里的思路(XOR 折叠),具体常量见下。


_FPC_TABLE_BITS = 16
_FPC_TABLE_SIZE = 1 << _FPC_TABLE_BITS
_FPC_MASK = _FPC_TABLE_SIZE - 1


def _fpc_hash_fcm(v: int) -> int:
    """FCM 表的索引:把 64 位值的高低两半 XOR 折叠。"""
    return (v ^ (v >> 32)) & _FPC_MASK


def _fpc_hash_dfcm(v: int) -> int:
    """DFCM 表的索引:用低位(步长往往由低位决定)。"""
    return (v ^ (v >> 16)) & _FPC_MASK


class CompressionCodecFPC(ICompressionCodec):
    """``FPC`` —— 双预测器(值预测 + 步长预测)压缩。**非通用**,只吃 8 字节定宽数值。"""

    name = "FPC"
    method_byte = CompressionMethodByte.FPC

    def is_generic(self) -> bool:
        return False

    @staticmethod
    def _leading_zero_bytes(x: int) -> int:
        """xor 从最高位起有多少个**完整为 0 的字节**(0..8)。"""
        if x == 0:
            return 8
        return 8 - ((x.bit_length() + 7) // 8)

    def do_compress(self, data: bytes) -> bytes:
        data = bytes(data)
        n = len(data)
        tlen = n % 8
        values = n // 8
        tail = data[values * 8 :]
        fcm = [0] * _FPC_TABLE_SIZE
        dfcm = [0] * _FPC_TABLE_SIZE
        bw = _BitWriter()
        prev = 0
        for i in range(values):
            cur = int.from_bytes(data[i * 8 : i * 8 + 8], "little")
            hf = _fpc_hash_fcm(prev)
            hd = _fpc_hash_dfcm(prev)
            pred1 = fcm[hf]
            pred2 = (prev + dfcm[hd]) & MASK64
            xor1 = cur ^ pred1
            xor2 = cur ^ pred2
            lzb1 = self._leading_zero_bytes(xor1)
            lzb2 = self._leading_zero_bytes(xor2)
            if max(lzb1, lzb2) >= 1:
                if lzb2 > lzb1:
                    bw.write(1, 2)
                    lzb = lzb2
                    xor = xor2
                else:
                    bw.write(0, 2)
                    lzb = lzb1
                    xor = xor1
                bw.write(lzb, 4)
                nbytes = 8 - lzb
                if nbytes:
                    bw.write(xor, nbytes * 8)
            else:
                bw.write(2, 2)
                bw.write(cur, 64)
            # 两端同步更新预测器
            fcm[hf] = cur
            dfcm[hd] = (cur - prev) & MASK64
            prev = cur
        return tail + bw.finish()

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        n = uncompressed_size
        tlen = n % 8
        values = n // 8
        tail = data[:tlen]
        br = _BitReader(data[tlen:])
        fcm = [0] * _FPC_TABLE_SIZE
        dfcm = [0] * _FPC_TABLE_SIZE
        out = bytearray()
        prev = 0
        for _ in range(values):
            hf = _fpc_hash_fcm(prev)
            hd = _fpc_hash_dfcm(prev)
            pred1 = fcm[hf]
            pred2 = (prev + dfcm[hd]) & MASK64
            selector = br.read(2)
            if selector == 2:
                cur = br.read(64)
            else:
                lzb = br.read(4)
                nbytes = 8 - lzb
                xor = br.read(nbytes * 8) if nbytes else 0
                pred = pred1 if selector == 0 else pred2
                cur = (pred ^ xor) & MASK64
            out += cur.to_bytes(8, "little")
            fcm[hf] = cur
            dfcm[hd] = (cur - prev) & MASK64
            prev = cur
        return bytes(out) + tail

    def __repr__(self) -> str:  # pragma: no cover
        return "<Codec FPC>"


# ══════════════════════════════════════════════════════════════════════════
# codec 链 —— CODEC(A, B, …)
# ══════════════════════════════════════════════════════════════════════════


class CompressionCodecMultiple(ICompressionCodec):
    """``CODEC(A, B, …)`` 这种**链式** codec 的载体 —— 对应真实 CH 的
    ``CompressionCodecMultiple``。

    压缩时按顺序 ``A.compress(B.compress(data))`` 那样依次套用;解压时**反着来**。

    .. code-block:: text

       compress:   data ──▶[A]──▶ X ──▶[B]──▶ Y   (Y 就是块里的 payload)
       decompress: Y ──▶[B]⁻¹──▶ X ──▶[A]⁻¹──▶ data

    难点在哪? **解压时要知道每一层的“原始长度”。** ``B`` 解压需要知道它该产出多少字节
    (也就是 ``X`` 的长度),而这个长度**不在**任何地方的固定字段里。真实 ClickHouse 是从
    表的元数据里读出整个 codec 链再重建的;mini-clickhouse 为了让一个块**自描述**,
    在 payload 最前面把链写成:

    .. code-block:: text

       ┌──┬───────────────────────────┬─────────────────────┬─────────┐
       │n │ n 个 codec 的描述          │ n-1 个中间长度        │ 数据     │
       │1B│ 名字 + 参数(见下)         │ varint               │ …       │
       └──┴───────────────────────────┴─────────────────────┴─────────┘
       每个 codec 描述 = [varint 名字长度][名字 utf8][varint 参数个数][各参数 varint]

    这样解压端只凭这一块就能把链重建出来(真实 CH 靠表元数据;我们额外把这信息塞进块里,
    是**有意的简化**,块因此略大一点点)。
    """

    name = "Multiple"
    method_byte = CompressionMethodByte.MULTIPLE

    def __init__(self, codecs: Sequence[ICompressionCodec]) -> None:
        self.codecs = list(codecs)

    def is_generic(self) -> bool:
        # 链能不能压任意字节,取决于它的**最后一环** —— 因为数据流是先经过前面几环
        # (它们可能只吃数值),最后一环才面对“任意字节流”。
        return bool(self.codecs) and self.codecs[-1].is_generic()

    @staticmethod
    def _write_codec(out: bytearray, codec: ICompressionCodec) -> None:
        name = codec.name.encode("utf-8")
        write_var_uint(out, len(name))
        out += name
        write_var_uint(out, len(codec.params))
        for p in codec.params:
            write_var_uint(out, p)

    @staticmethod
    def _read_codec(data: bytes, pos: int) -> Tuple[ICompressionCodec, int]:
        name_len, pos = read_var_uint(data, pos)
        name = data[pos : pos + name_len].decode("utf-8")
        pos += name_len
        nparams, pos = read_var_uint(data, pos)
        params = []
        for _ in range(nparams):
            v, pos = read_var_uint(data, pos)
            params.append(v)
        return CompressionCodecFactory.get(name, params), pos

    def do_compress(self, data: bytes) -> bytes:
        if not self.codecs:
            raise_error(ErrorCode.LOGICAL_ERROR, "CompressionCodecMultiple: empty codec chain")
        cur = bytes(data)
        sizes = []
        for codec in self.codecs:
            cur = codec.compress(cur)
            sizes.append(len(cur))
        head = bytearray()
        write_var_uint(head, len(self.codecs))
        for codec in self.codecs:
            self._write_codec(head, codec)
        # 存 n-1 个“中间长度”:sizes[i] = 套用完 codecs[0..i] 之后的长度。最后一个
        # codec 的输出长度就是 payload 长度,不需要存;而原始长度在块头里有。
        for i in range(len(self.codecs) - 1):
            write_var_uint(head, sizes[i])
        return bytes(head) + cur

    def decompress(self, data: bytes, uncompressed_size: int) -> bytes:
        data = bytes(data)
        pos = 0
        n, pos = read_var_uint(data, pos)
        codecs = []
        for _ in range(n):
            codec, pos = self._read_codec(data, pos)
            codecs.append(codec)
        sizes = []
        for _ in range(n - 1):
            v, pos = read_var_uint(data, pos)
            sizes.append(v)
        cur = data[pos:]
        # 反向解压。第 i 个 codec 的“期望产出长度”:i==0 → 原始长度;否则 sizes[i-1]。
        for i in range(n - 1, -1, -1):
            expected = uncompressed_size if i == 0 else sizes[i - 1]
            cur = codecs[i].decompress(cur, expected)
        return cur

    def __repr__(self) -> str:  # pragma: no cover
        inner = ", ".join(repr(c) for c in self.codecs)
        return f"<Codec Multiple({inner})>"


def _validate_chain(codecs: Sequence[ICompressionCodec]) -> None:
    """校验 codec 链 —— 实现真实 ClickHouse 的“专用必须跟通用”规则。

    规则一句话:**最后一环必须是通用 codec。**
    这样 ``CODEC(Delta)``(只有专用)会被拒,``CODEC(ZSTD, Delta)``(通用在前、
    专用在后)也会被拒 —— 后者会把已经压成字节流的数据再当数值去“差分”,纯属胡来,
    真实 CH 同样拒绝。
    """
    if not codecs:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            "The combination of compression codecs is invalid: empty codec list",
        )
    if not codecs[-1].is_generic():
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            "The combination of compression codecs is invalid: a specialization codec "
            "(Delta/DoubleDelta/Gorilla/T64/FPC) must be followed by a generic codec "
            "(LZ4/ZSTD/NONE), because it only transforms numeric data and does not "
            "make the output smaller on its own.",
        )


# ══════════════════════════════════════════════════════════════════════════
# 工厂 —— 按名字造 codec,按 "CODEC(…)" 文本解析 codec 链
# ══════════════════════════════════════════════════════════════════════════


def _split_top_level(text: str) -> List[str]:
    """按**顶层**逗号切分 —— 括号里的逗号不算(为 ``ZSTD(1)`` 这种参数服务)。"""
    parts: List[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


class CompressionCodecFactory:
    """按名字创建 codec,并把 ``CODEC(...)`` 文本解析成 codec 链。"""

    @staticmethod
    def get(name: str, params: Sequence[int] = ()) -> ICompressionCodec:
        """按名字 + 参数创建**单个** codec。

        ``params`` 是 ``CODEC(...)`` 里括号内的整数参数,例如 ``ZSTD(1)`` → ``[1]``、
        ``Delta(8)`` → ``[8]``。
        """
        p = list(params)
        up = name.strip().upper()
        if up == "NONE":
            return CompressionCodecNone()
        if up == "LZ4":
            return CompressionCodecLZ4()
        if up == "LZ4HC":
            return CompressionCodecLZ4HC(p[0] if p else 9)
        if up == "ZSTD":
            return CompressionCodecZSTD(p[0] if p else 3)
        if up == "DELTA":
            return CompressionCodecDelta(p[0] if p else 1)
        if up == "DOUBLEDELTA":
            return CompressionCodecDoubleDelta()
        if up == "GORILLA":
            return CompressionCodecGorilla()
        if up == "T64":
            return CompressionCodecT64(p[0] if p else 8)
        if up == "FPC":
            return CompressionCodecFPC()
        raise_error(
            ErrorCode.UNKNOWN_IDENTIFIER,
            f"Unknown compression codec: {name!r}",
        )

    @staticmethod
    def get_by_method_byte(method_byte: int) -> ICompressionCodec:
        """按块头的**方法字节**造一个能解压的 codec —— 读块时用。

        注意:方法字节里**不包含参数**(比如 Delta 的元素宽度)。所以链式 codec
        (0x91)的 ``decompress`` 会从 payload 自己把链和参数读出来;单独的非通用
        codec 也不会通过这个方法字节来解(它们本来就只能出现在链里)。
        """
        if method_byte == CompressionMethodByte.NONE:
            return CompressionCodecNone()
        if method_byte == CompressionMethodByte.LZ4:
            return CompressionCodecLZ4()
        if method_byte == CompressionMethodByte.ZSTD:
            return CompressionCodecZSTD()
        if method_byte == CompressionMethodByte.MULTIPLE:
            # 链的成员从 payload 里读,这里给个空壳 —— 它的 decompress 会自己解析。
            return CompressionCodecMultiple([])
        raise_error(
            ErrorCode.UNKNOWN_IDENTIFIER,
            f"Unknown compression method byte: 0x{method_byte:02x}",
        )

    @staticmethod
    def parse_codec_chain(text: str) -> List[ICompressionCodec]:
        """解析 ``CODEC(...)`` 文本,返回 codec 列表(**已经过校验**)。

        .. code-block:: python

           from minich.compression import CompressionCodecFactory as F
           F.parse_codec_chain("CODEC(Delta, ZSTD(1))")   # → [Delta(1), ZSTD(1)]
           F.parse_codec_chain("CODEC(NONE)")             # → [NONE]
           F.parse_codec_chain("CODEC(LZ4HC(9))")         # → [LZ4HC(9)]

        也可以省掉 ``CODEC(...)`` 外壳,直接写 ``"Delta, ZSTD"``(方便)。
        """
        s = text.strip()
        if s.upper().startswith("CODEC") and "(" in s:
            # 去掉最外层的 CODEC( … )
            open_idx = s.index("(")
            if not s.endswith(")"):
                raise_error(
                    ErrorCode.SYNTAX_ERROR,
                    f"Malformed codec chain (missing closing paren): {text!r}",
                )
            s = s[open_idx + 1 : s.rfind(")")]
        items = [x.strip() for x in _split_top_level(s) if x.strip()]
        if not items:
            raise_error(ErrorCode.SYNTAX_ERROR, f"Empty codec chain: {text!r}")
        codecs: List[ICompressionCodec] = []
        for item in items:
            if "(" in item:
                name = item[: item.index("(")].strip()
                inner = item[item.index("(") + 1 : item.rindex(")")]
                params = []
                for tok in inner.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        params.append(int(tok))
                    except ValueError:
                        raise_error(
                            ErrorCode.CANNOT_PARSE_NUMBER,
                            f"Codec parameter must be an integer, got {tok!r} in {item!r}",
                        )
            else:
                name = item
                params = []
            codecs.append(CompressionCodecFactory.get(name, params))
        _validate_chain(codecs)
        return codecs


# ══════════════════════════════════════════════════════════════════════════
# 块式压缩缓冲 —— CompressedWriteBuffer / CompressedReadBuffer
# ══════════════════════════════════════════════════════════════════════════

#: 一个块的最大原始大小。真实 ClickHouse 的 ``DBMS_DEFAULT_BUFFER_SIZE`` 是 1 MiB。
DEFAULT_BLOCK_SIZE = 1 << 20

#: 块头固定长度:[method_byte][u32 压缩后大小][u32 原始大小][u32 crc32]
_BLOCK_HEADER_SIZE = 1 + 4 + 4 + 4


class CompressedWriteBuffer:
    """把写入的字节流切成 ≤ 1 MiB 的块,逐块压缩后写成:

    ``[method_byte][u32 压缩后大小][u32 原始大小][u32 crc32][payload]``

    这正是真实 ClickHouse ``CompressedWriteBuffer`` 的布局(唯一的替换:
    校验字段从 16 字节的 CityHash128 换成 4 字节 crc32)。

    .. code-block:: python

       w = CompressedWriteBuffer()          # 默认 LZ4
       w.write(b"x" * 100000)
       blob = w.getvalue()

    分块的三个理由:

    1. **随机读**:要读第 k 个 granule,只需定位并解压它所在的块,不必解压前面的块;
    2. **并行**:不同的块可以由不同线程独立解压;
    3. **校验粒度**:一块坏了只丢一块,且能精确报出是哪一块。
    """

    def __init__(
        self,
        codec: Optional[object] = None,
        block_size: int = DEFAULT_BLOCK_SIZE,
        profile_events: Optional[ProfileEvents] = None,
    ) -> None:
        if codec is None:
            codec = CompressionCodecLZ4()
        elif isinstance(codec, str):
            codec = CompressionCodecFactory.get(codec)
        elif isinstance(codec, (list, tuple)):
            codec = CompressionCodecMultiple(list(codec))
        self.codec: ICompressionCodec = codec  # type: ignore[assignment]
        self.block_size = block_size
        self.profile_events = profile_events
        self._out = bytearray()
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf += data
        while len(self._buf) >= self.block_size:
            block = bytes(self._buf[: self.block_size])
            del self._buf[: self.block_size]
            self._write_block(block)

    def _write_block(self, block: bytes) -> None:
        payload = self.codec.compress(block)
        self._out.append(self.codec.method_byte)
        self._out += struct.pack("<I", len(payload))
        self._out += struct.pack("<I", len(block))
        self._out += struct.pack("<I", crc32(payload))
        self._out += payload
        if self.profile_events is not None:
            self.profile_events.increment("CompressedWriteBufferBlocks")
            self.profile_events.increment("CompressionSize", len(payload))
            self.profile_events.increment("UncompressedSize", len(block))

    def getvalue(self) -> bytes:
        """把剩余缓冲冲成一个块,返回全部输出。"""
        if self._buf:
            self._write_block(bytes(self._buf))
            self._buf.clear()
        return bytes(self._out)

    def __repr__(self) -> str:  # pragma: no cover
        return f"CompressedWriteBuffer(codec={self.codec!r}, pending={len(self._buf)})"


class CompressedReadBuffer:
    """ :class:`CompressedWriteBuffer` 的读端 —— 按需解块。

    构造时只拿到 blob(通常是一段 ``.bin`` 流),真正的解压发生在 ``read()``
    第一次需要某个块的时候。这是“懒惰解压”:只读一小段就不必解开整列。
    """

    def __init__(
        self,
        data: bytes,
        profile_events: Optional[ProfileEvents] = None,
    ) -> None:
        self._data = bytes(data)
        self._pos = 0
        self._block = bytearray()          # 当前已解开的块
        self._block_pos = 0
        self.profile_events = profile_events

    def _next_block(self) -> bool:
        if self._pos >= len(self._data):
            return False
        method_byte = self._data[self._pos]
        csize, usize = struct.unpack_from("<II", self._data, self._pos + 1)
        checksum = struct.unpack_from("<I", self._data, self._pos + 9)[0]
        start = self._pos + _BLOCK_HEADER_SIZE
        payload = self._data[start : start + csize]
        if crc32(payload) != checksum:
            raise_error(
                ErrorCode.CHECKSUM_DOESNT_MATCH,
                f"Compressed block checksum mismatch at offset {self._pos} "
                f"(stored {checksum:#010x})",
            )
        codec = CompressionCodecFactory.get_by_method_byte(method_byte)
        self._block = bytearray(codec.decompress(payload, usize))
        self._block_pos = 0
        self._pos = start + csize
        if self.profile_events is not None:
            self.profile_events.increment("CompressedReadBufferBlocks")
            self.profile_events.increment("ReadCompressedBytes", csize)
            self.profile_events.increment("ReadUncompressedBytes", usize)
        return True

    def read(self, n: int) -> bytes:
        """读最多 ``n`` 字节(到头了会少读,不报错)。"""
        out = bytearray()
        while len(out) < n:
            if self._block_pos >= len(self._block):
                if not self._next_block():
                    break
                continue
            take = min(n - len(out), len(self._block) - self._block_pos)
            out += self._block[self._block_pos : self._block_pos + take]
            self._block_pos += take
        return bytes(out)

    def read_all(self) -> bytes:
        """把剩下的全部读完 —— 一次性 IO 时用。"""
        out = bytearray()
        while True:
            chunk = self.read(DEFAULT_BLOCK_SIZE)
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def eof(self) -> bool:
        return (
            self._pos >= len(self._data) and self._block_pos >= len(self._block)
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"CompressedReadBuffer(pos={self._pos}/{len(self._data)}, "
            f"block={self._block_pos}/{len(self._block)})"
        )


# ══════════════════════════════════════════════════════════════════════════
# 基准 —— 现场跑出真实压缩率与耗时(教程正文的数字从这里来)
# ══════════════════════════════════════════════════════════════════════════


def _column_monotonic_ts(n: int) -> bytes:
    """n 个单调递增的 UInt64 时间戳(每个 +1 秒)—— DoubleDelta 的主场。"""
    out = bytearray()
    t = 1_700_000_000
    for _ in range(n):
        out += t.to_bytes(8, "little")
        t += 1
    return bytes(out)


def _column_random_u64(n: int, seed: int) -> bytes:
    """n 个随机 UInt64 —— 基本压不动,是“底线”。"""
    import random

    rnd = random.Random(seed)
    out = bytearray()
    for _ in range(n):
        out += rnd.getrandbits(64).to_bytes(8, "little")
    return bytes(out)


def _column_low_cardinality_strings(n: int, seed: int) -> bytes:
    """n 个低基数 String(从少量 URL 里挑)—— 通用 codec 的主场。

    用 :class:`minich.serialization.SerializationString` 的磁盘布局:
    ``[varint 长度][字节]``。
    """
    import random

    rnd = random.Random(seed)
    urls = [
        "/index.html", "/api/v1/users", "/api/v1/orders", "/static/app.js",
        "/static/style.css", "/favicon.ico", "/login", "/logout",
        "/api/v1/products", "/api/v1/cart", "/health", "/metrics",
    ]
    out = bytearray()
    for _ in range(n):
        s = rnd.choice(urls).encode("utf-8")
        write_var_uint(out, len(s))
        out += s
    return bytes(out)


def _benchmark_codec_specs() -> List[Tuple[str, ICompressionCodec]]:
    """基准里跑哪些 codec —— 包含几组“专用 + 通用”的链,用来展示堆叠的威力。"""
    return [
        ("NONE", CompressionCodecNone()),
        ("LZ4", CompressionCodecLZ4()),
        ("LZ4HC(9)", CompressionCodecLZ4HC(9)),
        ("ZSTD(3)", CompressionCodecZSTD(3)),
        ("Delta(8)", CompressionCodecDelta(8)),
        ("Delta(8)+ZSTD", CompressionCodecMultiple([CompressionCodecDelta(8), CompressionCodecZSTD(3)])),
        ("DoubleDelta", CompressionCodecDoubleDelta()),
        ("Gorilla", CompressionCodecGorilla()),
        ("T64(8)", CompressionCodecT64(8)),
        ("FPC", CompressionCodecFPC()),
    ]


def benchmark_codecs(
    n: int = 100_000, seed: int = 0, check_roundtrip: bool = False
) -> List[Tuple[str, str, int, float, float]]:
    """在一组代表性列上跑遍所有 codec,打印(并返回)一张表。

    返回的行是 ``(列名, codec 名, 原始字节数, 压缩率, 压缩耗时毫秒)``:
    **压缩率 = 原始字节数 / 压缩后字节数**,越大越好(1.0 表示没压小,<1 表示还变大了)。

    三列数据刻意选得覆盖三种典型形状:

    * **单调时间戳** —— 数值专用 codec(DoubleDelta / Gorilla / Delta)的主场;
    * **随机 UInt64** —— 谁都压不动,看“底线”;
    * **低基数字符串** —— 通用 codec(LZ4 / ZSTD)的主场(专用 codec 在这里是垃圾,
      所以不测它们,只测通用 codec + 通用 codec 链)。

    ``n`` 默认 10 万。纯 Python 实现的 LZ 匹配在 10 万级数据上要跑几秒到几十秒 ——
    想快可以传小一点(比如 ``n=20_000``),压缩率的**相对关系**不会变。
    """
    columns: List[Tuple[str, bytes, bool]] = [
        ("ts monotonic (UInt64)", _column_monotonic_ts(n), True),
        ("random (UInt64)", _column_random_u64(n, seed), True),
        ("low-card strings", _column_low_cardinality_strings(n, seed), False),
    ]

    rows: List[Tuple[str, str, int, float, float]] = []
    header = f"{'column':<22} {'codec':<15} {'orig':>10} {'comp':>10} {'ratio':>8} {'ms':>9}"
    print(header)
    print("-" * len(header))
    for col_name, payload, numeric in columns:
        orig = len(payload)
        specs = _benchmark_codec_specs()
        for codec_name, codec in specs:
            # 数值专用 codec(非通用)只在数值列上测:它们本来就不吃字符串。
            if not numeric and not codec.is_generic():
                continue
            sw = Stopwatch()
            compressed = codec.compress(payload)
            sw.stop()
            if check_roundtrip:
                back = codec.decompress(compressed, orig)
                if back != payload:
                    raise_error(
                        ErrorCode.VERIFICATION_FAILED,
                        f"benchmark round-trip failed for {codec_name} on {col_name}",
                    )
            comp = len(compressed)
            ratio = (orig / comp) if comp else 0.0
            ms = sw.elapsed_ms()
            rows.append((col_name, codec_name, orig, ratio, ms))
            print(
                f"{col_name:<22} {codec_name:<15} {orig:>10} {comp:>10} "
                f"{ratio:>7.2f}x {ms:>9.1f}"
            )
        print()
    return rows


# 方便 `python -m minich.compression` 直接看表。
if __name__ == "__main__":  # pragma: no cover
    benchmark_codecs()











