"""通用压缩：LZ4 块格式（自实现）+ zlib。

**为什么 Lance 用 LZ4/Zstd 而不是 gzip？**

向量检索是延迟敏感型负载：查一次要解压几十上百个页，
如果每页解压要 100 µs，光解压就吃掉几毫秒。所以选型的第一优先级
是**解压速度**（LZ4 大约 2–4 GB/s，zstd 慢一个身位但压缩率高 20–40%），
而不是压缩率。Lance 的策略是"先做列专用的结构化编码
（bitpack / dictionary / FSST / byte-stream-split），
再对剩下的字节做 LZ4 兜底" —— 通用压缩是最后一道，不是第一道。

本模块的 :func:`lz4_compress` / :func:`lz4_decompress` 是**真正的 LZ4 块格式**
（不是简化版），可以和 C 实现互相解压；测试里会和 ``lz4`` 包做交叉验证。
自己实现它的教学价值在于：LZ4 的整个格式只有 12 行伪代码，
看懂它之后，"为什么通用压缩对列存效果这么好"就不再是玄学。

LZ4 块格式回顾::

    sequence := token [literal_len_extra] literals offset [match_len_extra]
    token    := (literal_len << 4) | (match_len - 4)
    额外长度的编码：先写 (值 - 15)，若 >= 255 就写 255 再继续，直到最后一个 < 255

三条硬性约束（写错了 C 实现会拒绝解压）：

1. 块的最后 5 个字节必须是字面量；
2. 最后一个匹配的起点距离块尾至少 12 字节；
3. offset 必须是 1..65535，且指向已经输出过的位置。
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable, Dict, List, Optional, Tuple

from ..errors import CorruptedFileError

__all__ = [
    "lz4_compress",
    "lz4_decompress",
    "zlib_compress",
    "zlib_decompress",
    "noop_compress",
    "COMPRESSORS",
    "DECOMPRESSORS",
    "COMPRESSION_NONE",
    "COMPRESSION_LZ4",
    "COMPRESSION_ZLIB",
    "pick_compression",
    "compress_with",
    "decompress_with",
    "compression_report",
    "crosscheck_lz4",
]

COMPRESSION_NONE = "none"
COMPRESSION_LZ4 = "lz4"
COMPRESSION_ZLIB = "zlib"

_MFLIMIT = 12  # 最后一个匹配必须在此之内结束
_LASTLITERALS = 5  # 块尾至少留这么多字面量
_MINLENGTH = 4  # 最短匹配长度
_MAX_OFFSET = 65535


# ---------------------------------------------------------------------------
# LZ4 块格式
# ---------------------------------------------------------------------------


def _emit_len(out: bytearray, extra: int) -> None:
    """把"超出 15 的那部分长度"按 LZ4 的规则写出去。"""

    while extra >= 255:
        out.append(255)
        extra -= 255
    out.append(extra)


def lz4_compress(src: bytes, *, acceleration: int = 1) -> bytes:
    """LZ4 块格式压缩（贪心 + 哈希表，没有做最优解析）。

    ``acceleration`` 越大会跳过更多候选位置、更快但压缩率更低，
    这里只用一个"步进"来近似。
    """

    n = len(src)
    out = bytearray()
    if n == 0:
        # 参考实现也会为 0 字节输入产出一个"只有一个 0 token"的块。
        # 返回 b"" 虽然自洽，但 C 版解码器会拒绝，所以照抄参考行为。
        return b"\x00"

    # 块太小时直接全部当字面量 —— 这也是 C 实现的行为
    if n < _MFLIMIT + 1:
        _emit_last_literals(out, src, 0)
        return bytes(out)

    table: Dict[bytes, int] = {}
    mflimit = n - _MFLIMIT
    matchlimit = n - _LASTLITERALS
    anchor = 0
    i = 0
    step = max(1, acceleration)

    while i < mflimit:
        key = src[i : i + _MINLENGTH]
        cand = table.get(key)
        table[key] = i
        if cand is None or i - cand > _MAX_OFFSET or src[cand : cand + _MINLENGTH] != key:
            i += step
            continue

        # 找到了 4 字节匹配，尽量往后延
        ml = _MINLENGTH
        limit = matchlimit - i
        while ml < limit and src[cand + ml] == src[i + ml]:
            ml += 1

        literals = src[anchor:i]
        _emit_sequence(out, literals, i - cand, ml)
        i += ml
        anchor = i

    _emit_last_literals(out, src, anchor)
    return bytes(out)


def _emit_sequence(out: bytearray, literals: bytes, offset: int, match_len: int) -> None:
    # offset 为 0 意味着"匹配到自己" —— 解码器会直接判为损坏文件，
    # 所以这里必须挡住，否则写出来的是一个谁也读不了的文件。
    if offset < 1 or offset > _MAX_OFFSET:
        raise CorruptedFileError(
            "LZ4 内部分配了非法 offset", offset=offset, limit=_MAX_OFFSET
        )
    lit_len = len(literals)
    ml_code = match_len - _MINLENGTH
    out.append((min(lit_len, 15) << 4) | min(ml_code, 15))
    if lit_len >= 15:
        _emit_len(out, lit_len - 15)
    out += literals
    out += struct.pack("<H", offset)
    if ml_code >= 15:
        _emit_len(out, ml_code - 15)


def _emit_last_literals(out: bytearray, src: bytes, anchor: int) -> None:
    lit_len = len(src) - anchor
    out.append(min(lit_len, 15) << 4)
    if lit_len >= 15:
        _emit_len(out, lit_len - 15)
    out += src[anchor:]


def lz4_decompress(src: bytes, uncompressed_size: Optional[int] = None) -> bytes:
    """LZ4 块格式解压。

    ``uncompressed_size`` 给了就直接预分配；不给就靠 bytearray 增长。
    注意 LZ4 块本身**不携带原始长度** —— 这也是为什么 Lance 的
    column metadata 里必须记录每一页的 num_rows/字节数，
    否则解压方不知道要还原多少字节。
    """

    out = bytearray(uncompressed_size or 0)
    out_len = 0
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise CorruptedFileError("LZ4: 字面量长度被截断")
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        if i + lit_len > n:
            raise CorruptedFileError(
                "LZ4: 字面量超出块边界", need=lit_len, remain=n - i
            )
        if lit_len:
            if len(out) < out_len + lit_len:
                out.extend(b"\x00" * (out_len + lit_len - len(out)))
            out[out_len : out_len + lit_len] = src[i : i + lit_len]
            out_len += lit_len
            i += lit_len
        if i >= n:
            break  # 最后一个序列只有字面量
        if i + 2 > n:
            raise CorruptedFileError("LZ4: offset 被截断")
        offset = struct.unpack_from("<H", src, i)[0]
        i += 2
        if offset == 0:
            raise CorruptedFileError("LZ4: offset 不能为 0")
        ml_code = token & 0x0F
        if ml_code == 15:
            while True:
                if i >= n:
                    raise CorruptedFileError("LZ4: 匹配长度被截断")
                b = src[i]
                i += 1
                ml_code += b
                if b != 255:
                    break
        match_len = ml_code + _MINLENGTH
        start = out_len - offset
        if start < 0:
            raise CorruptedFileError(
                "LZ4: offset 指向了输出起点之前", offset=offset, produced=out_len
            )
        need = out_len + match_len
        if len(out) < need:
            out.extend(b"\x00" * (need - len(out)))
        # 逐字节复制：offset < match_len 时会自我重叠，这正是 LZ4 允许的
        # 技巧（用重复的少量字节"刷"出长串），不能换成整段 memmove。
        for k in range(match_len):
            out[out_len] = out[start + k]
            out_len += 1

    if uncompressed_size is not None and out_len != uncompressed_size:
        raise CorruptedFileError(
            "LZ4: 解压长度与预期不符", expected=uncompressed_size, actual=out_len
        )
    return bytes(out[:out_len])


# ---------------------------------------------------------------------------
# zlib（代表"高压缩比"的那一档，真实 Lance 对应 Zstd）
# ---------------------------------------------------------------------------


def zlib_compress(src: bytes, level: int = 6) -> bytes:
    return zlib.compress(src, level)


def zlib_decompress(src: bytes, uncompressed_size: Optional[int] = None) -> bytes:
    return zlib.decompress(src)


def noop_compress(src: bytes) -> bytes:
    return src


def noop_decompress(src: bytes, uncompressed_size: Optional[int] = None) -> bytes:
    return src


COMPRESSORS: Dict[str, Callable[..., bytes]] = {
    COMPRESSION_NONE: noop_compress,
    COMPRESSION_LZ4: lz4_compress,
    COMPRESSION_ZLIB: zlib_compress,
}

DECOMPRESSORS: Dict[str, Callable[..., bytes]] = {
    COMPRESSION_NONE: noop_decompress,
    COMPRESSION_LZ4: lz4_decompress,
    COMPRESSION_ZLIB: zlib_decompress,
}


def compress_with(name: str, data: bytes, **kw: object) -> bytes:
    try:
        fn = COMPRESSORS[name]
    except KeyError:
        raise CorruptedFileError(f"未知压缩算法 {name!r}", available=list(COMPRESSORS)) from None
    return fn(data, **kw)  # type: ignore[arg-type]


def decompress_with(name: str, data: bytes, uncompressed_size: Optional[int] = None) -> bytes:
    try:
        fn = DECOMPRESSORS[name]
    except KeyError:
        raise CorruptedFileError(f"未知压缩算法 {name!r}", available=list(DECOMPRESSORS)) from None
    return fn(data, uncompressed_size)  # type: ignore[arg-type]


def pick_compression(
    data: bytes,
    candidates: Tuple[str, ...] = (COMPRESSION_NONE, COMPRESSION_LZ4, COMPRESSION_ZLIB),
    *,
    min_saving: int = 16,
) -> Tuple[str, bytes]:
    """逐个试，取最小的那个；省不到 ``min_saving`` 字节就选 none。

    真实 Lance 不会逐页试压缩（太贵），它按**列**选一次编码和压缩，
    并把选择写进 metadata。这里逐页试是为了让教程里的数字好看、
    便于观察"到底哪一页压得动"。
    """

    best_name = COMPRESSION_NONE
    best = data
    for name in candidates:
        if name == COMPRESSION_NONE:
            continue
        try:
            cand = compress_with(name, data)
        except Exception:  # pragma: no cover - 压缩器不该抛
            continue
        if len(cand) < len(best):
            best_name, best = name, cand
    if len(data) - len(best) < min_saving:
        return COMPRESSION_NONE, data
    return best_name, best


def compression_report(data: bytes) -> List[Tuple[str, int, float]]:
    """给教程用：把各算法的效果排成一张表。"""

    rows = []
    for name in (COMPRESSION_NONE, COMPRESSION_LZ4, COMPRESSION_ZLIB):
        out = compress_with(name, data)
        rows.append((name, len(out), len(out) / max(1, len(data))))
    return rows


# ---------------------------------------------------------------------------
# 与 C 实现的交叉验证（测试里用；缺库时静默跳过）
# ---------------------------------------------------------------------------


def crosscheck_lz4(data: bytes) -> Optional[Dict[str, bool]]:
    """和 ``lz4`` 包互解，确认我们写的是**真的** LZ4 块格式。

    返回 ``None`` 表示环境里没有 lz4 包。
    """

    try:
        import lz4.block  # type: ignore
    except ImportError:  # pragma: no cover
        return None
    mine = lz4_compress(data)
    theirs = lz4.block.compress(data, store_size=False)
    result = {
        "ours_decodes": lz4_decompress(mine, len(data)) == data,
        "theirs_decodes_ours": lz4.block.decompress(mine, uncompressed_size=len(data)) == data,
        "we_decode_theirs": lz4_decompress(theirs, len(data)) == data,
    }
    return result
