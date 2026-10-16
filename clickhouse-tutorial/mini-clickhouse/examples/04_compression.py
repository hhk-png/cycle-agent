# -*- coding: utf-8 -*-
r"""04 —— 压缩:编解码器(CODEC)按列堆叠。

ClickHouse 的压缩不是"整个文件 gzip 一下",而是 **每个列各自配一套 codec,且顺序
是「先专用、后通用」**:

.. code-block:: text

   列字节流 ─① Delta/DoubleDelta/Gorilla(专用,只改布局)─▶ ─② LZ4/ZSTD(通用,压字节)─▶ 磁盘

示例分三步坐实这件事:

1. ``benchmark_codecs(n=20000)`` 现场跑真实压缩率。盯住这些对比:
   **单调时间戳** 上 ``Delta(8)+ZSTD`` 压到 ~1900×,单用 ``ZSTD`` 只有 ~8×
   (这就是堆叠的威力);**随机 UInt64** 谁都压不动(ratio≈1.0,底线);
   **低基数字符串** 是通用 codec 的主场。
2. 建表给列写不同 ``CODEC``,用 ``system.columns.codec_expression`` 证明
   **DDL 里的 codec 被真的记住了**(按列配置的直接证据)。
3. 反例说明硬规则:**专用 codec 后面必须跟通用 codec** —— ``CODEC(Delta)`` 被拒。

.. warning:: mini-clickhouse 的 LZ4 / ZSTD 是**同名不同格式**的自研实现(教学用),
   和真实 lz4/zstd 库不互通。压缩率量级是对的,但**不能跨系统交换数据**。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.compression import (  # noqa: E402
    CompressionCodecFactory,
    CompressionCodecLZ4,
    CompressionCodecMultiple,
    CompressionCodecNone,
    CompressionCodecZSTD,
    benchmark_codecs,
)
from minich.errors import MiniChError  # noqa: E402
from minich.execution.context import Context  # noqa: E402


def monotonic_timestamps(n: int) -> bytes:
    """n 个单调递增的 UInt64 时间戳(小端,每值 +1 秒)—— DoubleDelta 的主场。"""
    out = bytearray()
    t = 1_700_000_000
    for _ in range(n):
        out += t.to_bytes(8, "little")
        t += 1
    return bytes(out)


def main() -> None:
    print("=" * 78)
    print("04 · 压缩 —— 专用 codec 变规整,通用 codec 压字节")
    print("=" * 78)

    print("\n--- ① benchmark_codecs(n=20000):现场量一张真实的压缩率表 ---")
    print("(ratio = 原始字节 / 压缩后字节;越大越好,1.0 = 没压小)\n")
    benchmark_codecs(n=20_000)

    data_dir = tempfile.mkdtemp(prefix="minich04-")
    ctx = Context(data_path=data_dir)
    try:
        print("\n--- ② DDL 里给每列指定 CODEC,并证明它被记住了 ---")
        ctx.execute(
            "CREATE TABLE codecs ("
            "  ts UInt64 CODEC(Delta, ZSTD(3)),"
            "  payload String CODEC(ZSTD(3)),"
            "  raw UInt64 CODEC(NONE)"
            ") ENGINE = MergeTree ORDER BY ts"
        )
        ctx.execute(
            "INSERT INTO codecs "
            "SELECT number + 1700000000, concat('/p/', toString(number % 50)), "
            "       number * 2654435761 % 100000307 FROM numbers(5000)"
        )
        print(ctx.execute(
            "SELECT name, type, codec_expression FROM system.columns "
            "WHERE table = 'codecs' ORDER BY position").text())
        print("说明:`ts` 记的是 CODEC(Delta, ZSTD(3)),`raw` 记的是 CODEC(NONE)。")
        print("      codec 是**列级**属性,写进表元数据,不是查询时临时决定的。")
        print("      (注意:mini-clickhouse 目前不填充 data_uncompressed_bytes,"
              "所以要看压缩率得用下面的直接对比。)")

        print("\n--- ③ 直接在一个列的字节上跑 codec 链,对比大小 ---")
        column = monotonic_timestamps(20_000)
        print(f"原始列字节数: {len(column)}")
        chain = [
            ("NONE", CompressionCodecNone()),
            ("LZ4", CompressionCodecLZ4()),
            ("ZSTD(3)", CompressionCodecZSTD(3)),
            ("Delta(8)", CompressionCodecFactory.get("Delta", [8])),
            ("Delta(8)+ZSTD(3)",
             CompressionCodecMultiple([CompressionCodecFactory.get("Delta", [8]),
                                       CompressionCodecZSTD(3)])),
        ]
        for name, codec in chain:
            packed = codec.compress(column)
            ratio = len(column) / len(packed)
            note = ""
            if name == "Delta(8)":
                note = "  ← 专用 codec 只变布局,长度没变,要配通用 codec 才压得下"
            if name == "Delta(8)+ZSTD(3)":
                note = "  ← 两步互补:先变成'一堆 0 + 小差分',再真正压掉"
            print(f"  {name:<16} {len(packed):>8} 字节  ratio={ratio:>8.1f}x{note}")

        print("\n--- ④ 反例:专用 codec 单独用会被拒绝 ---")
        try:
            CompressionCodecFactory.parse_codec_chain("CODEC(Delta(8))")
            print("  (预期外的通过 —— 说明规则变了)")
        except MiniChError as exc:
            print(f"  CODEC(Delta) -> {exc.name}: {exc.message[:110]}…")
        print("  规则:一个专用 codec(Delta/DoubleDelta/Gorilla/T64/FPC)后面")
        print("        必须跟一个通用 codec(LZ4/ZSTD/NONE),否则数据既没变小、")
        print("        也不再是别的系统能直接读的字节流。")
    finally:
        ctx.shutdown()

    print("\n小结:选 codec 的顺序是 —— 先按列的数据形状选专用 codec(时间戳用")
    print("      DoubleDelta、一般整数用 Delta/T64、浮点用 Gorilla/FPC、字符串")
    print("      直接上 ZSTD),再在后面挂一个通用 codec 收尾。")


if __name__ == "__main__":
    main()
