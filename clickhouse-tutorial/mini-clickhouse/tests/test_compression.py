"""压缩测试 —— 编解码器链、块式压缩缓冲、校验和。

ClickHouse 的压缩有一个核心洞见:**专用编解码器(Delta/DoubleDelta/…)
只把数值数据"变规整",通用编解码器(LZ4/ZSTD/NONE)才真正把字节压小。**
所以 ``CODEC(Delta)``(只有专用)是**非法**的 —— 必须跟一个通用编解码器。

这个文件覆盖:
* 每个编解码器在**折磨集**(空、1 字节、全零、单调时间戳、随机、NaN/Inf、
  极值)上的往返;
* ``parse_codec_chain`` 的接受/拒绝规则;
* 块式缓冲的**多块**往返与**校验和损坏**检测(错误码 ``CHECKSUM_DOESNT_MATCH``);
* LZ4/ZSTD 会拒绝"不是自己产生的"字节流(它们自带魔数,不与真实库互通)。
"""

from __future__ import annotations

import random
import struct

import pytest

from minich.compression import (
    CompressionCodecDelta,
    CompressionCodecDoubleDelta,
    CompressionCodecFPC,
    CompressionCodecGorilla,
    CompressionCodecLZ4,
    CompressionCodecLZ4HC,
    CompressionCodecMultiple,
    CompressionCodecNone,
    CompressionCodecT64,
    CompressionCodecZSTD,
    CompressionCodecFactory,
    CompressionMethodByte,
    CompressedReadBuffer,
    CompressedWriteBuffer,
)
from minich.errors import ErrorCode, MiniChError
from minich.util.stopwatch import ProfileEvents


# ══════════════════════════════════════════════════════════════════════════
# 折磨数据集
# ══════════════════════════════════════════════════════════════════════════


def _monotonic_ts(n=200):
    return b"".join((1_700_000_000 + i).to_bytes(8, "little") for i in range(n))


def _random_u64(n=200, seed=42):
    rnd = random.Random(seed)
    return b"".join(rnd.getrandbits(64).to_bytes(8, "little") for _ in range(n))


TORTURE = {
    "empty": b"",
    "one_byte": b"\x00",
    "zeros": b"\x00" * 512,
    "ones": b"\xff" * 512,
    "monotonic_ts": _monotonic_ts(),
    "random_u64": _random_u64(),
    "nan_inf_floats": struct.pack("<ddd", float("nan"), float("inf"), float("-inf")),
    "extremes": struct.pack("<QQq", 0, 2 ** 64 - 1, -(2 ** 63)),
    "ascii_text": b"the quick brown fox jumps over the lazy dog\n" * 20,
}

GENERIC_CODECS = ["NONE", "LZ4", "LZ4HC", "ZSTD"]
NON_GENERIC_CODECS = ["DELTA", "DOUBLEDELTA", "GORILLA", "T64", "FPC"]
ALL_CODECS = GENERIC_CODECS + NON_GENERIC_CODECS


# ══════════════════════════════════════════════════════════════════════════
# 每个编解码器的往返
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("codec_name", ALL_CODECS)
@pytest.mark.parametrize("data_name", list(TORTURE))
def test_codec_roundtrips_torture_set(codec_name, data_name):
    data = TORTURE[data_name]
    codec = CompressionCodecFactory.get(codec_name)
    blob = codec.compress(data)
    assert codec.decompress(blob, len(data)) == data


@pytest.mark.parametrize(
    "name,is_generic",
    [("NONE", True), ("LZ4", True), ("LZ4HC", True), ("ZSTD", True),
     ("DELTA", False), ("DOUBLEDELTA", False), ("GORILLA", False),
     ("T64", False), ("FPC", False)],
)
def test_codec_generic_flag(name, is_generic):
    assert CompressionCodecFactory.get(name).is_generic() is is_generic


def test_none_codec_is_identity():
    c = CompressionCodecNone()
    data = TORTURE["monotonic_ts"]
    assert c.compress(data) == data


def test_generic_codecs_shrink_repetitive_data():
    data = b"abcabcabcabc" * 200
    for name in ("LZ4", "LZ4HC", "ZSTD"):
        c = CompressionCodecFactory.get(name)
        assert len(c.compress(data)) < len(data), name


def test_double_delta_shrinks_monotonic_timestamps():
    """单调时间戳是 DoubleDelta 的主场:8 字节一个值能压到接近 1 bit。"""
    data = _monotonic_ts(1000)
    c = CompressionCodecDoubleDelta()
    comp = c.compress(data)
    assert len(comp) < len(data) // 4
    assert c.decompress(comp, len(data)) == data


def test_unknown_codec_raises_unknown_identifier():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecFactory.get("SNAPPY")
    assert ei.value.code == ErrorCode.UNKNOWN_IDENTIFIER


def test_zstd_and_lz4_share_nothing_but_lz4hc_shares_lz4_byte():
    assert CompressionMethodByte.LZ4 == CompressionMethodByte.LZ4HC == 0x82
    assert CompressionMethodByte.ZSTD == 0x90
    assert CompressionMethodByte.MULTIPLE == 0x91


def test_foreign_stream_rejected_by_lz4():
    """LZ4 自带魔数 —— 真实 LZ4 库的字节流会被挡回去。"""
    with pytest.raises(MiniChError) as ei:
        CompressionCodecLZ4().decompress(b"\x04\x22\x4d\x18\x00\x00\x00\x00abcd", 4)
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


def test_foreign_stream_rejected_by_zstd():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecZSTD().decompress(b"\x28\xb5\x2f\xfd\x00\x00\x00\x00abcd", 4)
    assert ei.value.code == ErrorCode.CANNOT_PARSE_TEXT


def test_lz4_embedded_size_mismatch_detected():
    c = CompressionCodecLZ4()
    blob = c.compress(b"hello world")
    with pytest.raises(MiniChError) as ei:
        c.decompress(blob, 999)   # 声明了一个错误的原始长度
    assert ei.value.code == ErrorCode.CANNOT_READ_ALL_DATA


# ══════════════════════════════════════════════════════════════════════════
# codec 链
# ══════════════════════════════════════════════════════════════════════════


def test_parse_codec_chain_valid():
    chain = CompressionCodecFactory.parse_codec_chain("CODEC(NONE)")
    assert [c.name for c in chain] == ["NONE"]
    chain = CompressionCodecFactory.parse_codec_chain("CODEC(Delta(8), ZSTD(1))")
    assert [c.name for c in chain] == ["Delta", "ZSTD"]
    # 省掉 CODEC(...) 外壳也可以
    chain = CompressionCodecFactory.parse_codec_chain("Delta, ZSTD")
    assert [c.name for c in chain] == ["Delta", "ZSTD"]


def test_parse_codec_chain_rejects_specialization_without_generic():
    """``CODEC(Delta)`` 非法 —— 专用编解码器后面必须跟一个通用编解码器。"""
    with pytest.raises(MiniChError) as ei:
        CompressionCodecFactory.parse_codec_chain("CODEC(Delta)")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_parse_codec_chain_rejects_generic_then_specialization():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecFactory.parse_codec_chain("CODEC(ZSTD, Delta)")
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_parse_codec_chain_rejects_empty():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecFactory.parse_codec_chain("CODEC()")
    assert ei.value.code == ErrorCode.SYNTAX_ERROR


def test_parse_codec_chain_rejects_non_integer_param():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecFactory.parse_codec_chain("CODEC(ZSTD(abc))")
    assert ei.value.code == ErrorCode.CANNOT_PARSE_NUMBER


def test_multiple_codec_roundtrip_and_generic_flag():
    m = CompressionCodecMultiple([CompressionCodecDelta(8), CompressionCodecZSTD(3)])
    assert m.is_generic() is True       # 看最后一环
    data = _monotonic_ts(100)
    assert m.decompress(m.compress(data), len(data)) == data
    # 最后一环非通用 → 整体非通用
    m2 = CompressionCodecMultiple([CompressionCodecNone(), CompressionCodecDelta(8)])
    assert m2.is_generic() is False


def test_multiple_codec_empty_chain_raises():
    with pytest.raises(MiniChError) as ei:
        CompressionCodecMultiple([]).compress(b"x")
    assert ei.value.code == ErrorCode.LOGICAL_ERROR


# ══════════════════════════════════════════════════════════════════════════
# 块式压缩缓冲
# ══════════════════════════════════════════════════════════════════════════


def test_compressed_buffer_single_block_roundtrip():
    payload = b"hello compression " * 50
    w = CompressedWriteBuffer()
    w.write(payload)
    blob = w.getvalue()
    assert CompressedReadBuffer(blob).read_all() == payload


def test_compressed_buffer_multiblock_roundtrip_and_block_count():
    payload = bytes(range(256)) * 4        # 1024 字节
    pe = ProfileEvents()
    w = CompressedWriteBuffer(block_size=64, profile_events=pe)
    w.write(payload)
    blob = w.getvalue()
    # 1024 / 64 = 16 个满块
    assert pe.get("CompressedWriteBufferBlocks") == 16
    assert CompressedReadBuffer(blob).read_all() == payload


def test_compressed_read_buffer_partial_reads():
    payload = b"x" * 100
    w = CompressedWriteBuffer(block_size=16)
    w.write(payload)
    blob = w.getvalue()
    r = CompressedReadBuffer(blob)
    assert r.read(10) == b"x" * 10
    assert r.read(5) == b"x" * 5
    rest = r.read_all()
    assert rest == b"x" * 85
    assert r.eof() is True


def test_compressed_buffer_empty_write():
    w = CompressedWriteBuffer()
    w.write(b"")
    assert w.getvalue() == b""


def test_compressed_buffer_default_codec_is_lz4():
    w = CompressedWriteBuffer()
    assert w.codec.name == "LZ4"


def test_compressed_buffer_accepts_codec_name_and_chain():
    assert CompressedWriteBuffer("ZSTD").codec.name == "ZSTD"
    chain = CompressedWriteBuffer([CompressionCodecDelta(8), CompressionCodecZSTD(3)])
    assert chain.codec.is_generic() is True


def test_checksum_corruption_is_detected():
    payload = b"important data that must not be corrupted" * 10
    w = CompressedWriteBuffer()
    w.write(payload)
    blob = bytearray(w.getvalue())
    # 改一个 payload 字节(块头是 13 字节)
    blob[-1] ^= 0xFF
    r = CompressedReadBuffer(bytes(blob))
    with pytest.raises(MiniChError) as ei:
        r.read_all()
    assert ei.value.code == ErrorCode.CHECKSUM_DOESNT_MATCH


def test_corrupt_middle_block_reports_mismatch():
    payload = bytes(range(256)) * 8
    w = CompressedWriteBuffer(block_size=512)
    w.write(payload)
    blob = bytearray(w.getvalue())
    # 破坏第 2 个块:跳到第 1 块之后(13 字节头 + payload),再改一个字节
    first_payload = struct.unpack_from("<I", blob, 1)[0]
    pos = 13 + first_payload + 12      # 第二个块的最后一个头字节前
    blob[pos] ^= 0xFF
    with pytest.raises(MiniChError) as ei:
        CompressedReadBuffer(bytes(blob)).read_all()
    assert ei.value.code == ErrorCode.CHECKSUM_DOESNT_MATCH


# ══════════════════════════════════════════════════════════════════════════
# 基准(小规模，确保它不报错且数字自洽)
# ══════════════════════════════════════════════════════════════════════════


def test_benchmark_codecs_returns_sane_rows(capsys):
    from minich.compression import benchmark_codecs

    rows = benchmark_codecs(n=2_000, seed=1, check_roundtrip=True)
    # 每行是 (列名, codec 名, 原始字节数, 压缩率, 耗时毫秒)
    assert rows
    for col_name, codec_name, orig, ratio, ms in rows:
        assert orig > 0
        assert ratio >= 0.0
        assert ms >= 0.0
    # 确认它确实打印了一张表
    out = capsys.readouterr().out
    assert "column" in out and "codec" in out
