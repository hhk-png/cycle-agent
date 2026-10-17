"""例 3：列式编码到底能省多少 —— 对应第 4 章。

对同一批数值列、字符串列和浮点列，把每种编码的实际字节数排出来。
结论会有点出乎意料：

* 同一个 int64 列，选对编码和选错编码差三个数量级；
* 随机数几乎压不动，任何"列存一定省 10 倍"的说法都是在挑好看的数据；
* ``byte_stream_split`` 在随机浮点上只省 7%，在量化过的浮点上反而更差。

跑法：  python -X utf8 examples/03_encoding_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_lancedb.arrays import StringArray  # noqa: E402
from mini_lancedb.encoding.codecs import (  # noqa: E402
    compare_bytes_encodings,
    compare_numeric_encodings,
    choose_numeric_encoding,
    compress_page,
)
from mini_lancedb.encoding.compress import (  # noqa: E402
    COMPRESSION_LZ4,
    compression_report,
    crosscheck_lz4,
)
from mini_lancedb.types import BOOL, FLOAT32, INT8, INT32, INT64  # noqa: E402

N = 100_000


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def numeric_report() -> None:
    section("数值列：同一列数据，不同编码的实际大小")
    rng = np.random.default_rng(0)
    cases = {
        "单调递增 id": (np.arange(N, dtype=np.int64), INT64),
        "随机 id (0..2^62)": (rng.integers(0, 2**62, N, dtype=np.int64), INT64),
        "状态码 (0..3)": (rng.integers(0, 4, N).astype(np.int8), INT8),
        "排序后的类别 (0..7)": (np.sort(rng.integers(0, 8, N)).astype(np.int8), INT8),
        "常量列 (全是 42)": (np.full(N, 42, dtype=np.int32), INT32),
        "布尔列": (rng.integers(0, 2, N).astype(bool), BOOL),
        "递增时间戳 (ms)": (np.arange(N, dtype=np.int64) * 1000 + 1_700_000_000_000, INT64),
        "随机向量分量 (f32)": (rng.standard_normal(N).astype(np.float32), FLOAT32),
    }
    print(
        f"{'列':<20}{'原始':>10}{'最优编码':>16}{'编码后':>10}"
        f"{'压缩比':>9}{'+LZ4':>10}"
    )
    print("-" * 78)
    for name, (data, dtype) in cases.items():
        table = compare_numeric_encodings(data, dtype)
        best = choose_numeric_encoding(data, dtype)
        lz4 = compress_page(best, COMPRESSION_LZ4)
        ratio = data.nbytes / max(1, best.payload_bytes)
        print(
            f"{name:<20}{data.nbytes:>10,}{best.encoding:>16}"
            f"{best.payload_bytes:>10,}{ratio:>8.1f}x{lz4.payload_bytes:>10,}"
        )
    print()
    print("前三种可行编码（看差距有多大）:")
    for name, (data, dtype) in cases.items():
        table = compare_numeric_encodings(data, dtype)
        detail = "  ".join(f"{enc}={size:,}" for enc, size in table[:3])
        print(f"  {name:<20} {detail}")


def string_report() -> None:
    section("字符串列：字典、FSST 与 offsets 的账")
    rng = np.random.default_rng(1)
    templated = [f"LanceDB is a multimodal lakehouse for AI applications. row {i}" for i in range(50_000)]
    high_card = [f"{rng.integers(0, 2**60):016x}" for _ in range(50_000)]
    low_card = [f"status-{rng.integers(0, 8)}" for _ in range(50_000)]

    for label, values in (
        ("模板化文本（重复度中等）", templated),
        ("随机十六进制（高基数）", high_card),
        ("状态字符串（低基数）", low_card),
    ):
        arr = StringArray.from_strings(values)
        table = compare_bytes_encodings(arr.offsets, arr.buffer, len(arr))
        raw = arr.nbytes
        print(f"  {label}")
        print(f"    原始列（int64 offsets + UTF-8 字节）: {raw:,} B")
        for enc, size in table:
            print(f"    {enc:<14} {size:>12,} B   ({raw / max(1, size):.1f}x)")
        print()

    print("  单独看 offsets 这一列（5 万行 → 50001 个 offset）:")
    from mini_lancedb.format.codec_util import delta_encode_list

    arr = StringArray.from_strings(templated)
    as_i32 = 50_001 * 4
    as_varint_delta = len(delta_encode_list(arr.offsets.tolist()))
    print(f"    int32 平铺           : {as_i32:,} B")
    print(f"    差分 + varint        : {as_varint_delta:,} B")
    print(
        f"    省了 {(1 - as_varint_delta / as_i32) * 100:.1f}%"
        " —— 因为单调递增的 offsets 差分都是小整数"
    )


def float_report() -> None:
    section("浮点与向量：一个反直觉的结论")
    rng = np.random.default_rng(2)
    dim = 128
    variants = {
        "float32 N(0,1) 归一化": np.asarray(
            (lambda v: v / np.linalg.norm(v, axis=1, keepdims=True))(
                rng.standard_normal((2000, dim)).astype(np.float32)
            )
        ),
        "float32 量化到 0.05 步长": (np.round(rng.standard_normal((2000, dim)) * 20) / 20).astype(
            np.float32
        ),
        "float16 N(0,1)": rng.standard_normal((2000, dim)).astype(np.float16),
    }
    from mini_lancedb.encoding.codecs import ByteStreamSplitEncoder, PlainEncoder
    from mini_lancedb.encoding.compress import lz4_compress

    print(f"  {'数据':<26}{'原始':>12}{'plain+LZ4':>12}{'bss+LZ4':>12}{'差别':>10}")
    print("  " + "-" * 72)
    for label, matrix in variants.items():
        flat = np.ascontiguousarray(matrix).reshape(-1)
        raw = flat.tobytes()
        dtype = FLOAT32 if flat.dtype != np.float16 else __import__(
            "mini_lancedb.types", fromlist=["FLOAT16"]
        ).FLOAT16
        plain = PlainEncoder.encode(flat, dtype, None)
        bss = ByteStreamSplitEncoder.encode(flat, dtype, None)
        plain_lz4 = sum(len(lz4_compress(b)) for b in plain.buffers)
        bss_lz4 = sum(len(lz4_compress(b)) for b in bss.buffers)
        delta = (1 - bss_lz4 / plain_lz4) * 100
        print(
            f"  {label:<26}{len(raw):>12,}{plain_lz4:>12,}{bss_lz4:>12,}{delta:>9.1f}%"
        )
    print()
    print("  结论：拆字节的收益远比传闻的小，而且在已经结构化的数据上会反向。")
    print("        结构性变换和通用压缩是耦合的，不能独立决策。")


def lz4_report() -> None:
    section("LZ4：自实现与 C 实现对拍")
    samples = {
        "空输入": b"",
        "重复短语": b"the quick brown fox jumps over the lazy dog " * 20,
        "随机字节": np.random.default_rng(3).integers(0, 256, 8192, dtype=np.uint8).tobytes(),
        "全零": bytes(100_000),
        "递增字节序列": bytes(range(256)) * 40,
    }
    print(f"  {'样本':<16}{'原始':>10}{'压缩后':>10}{'往返':>8}{'C 实现互解':>12}")
    for label, data in samples.items():
        result = crosscheck_lz4(data)
        ok = all(result.values()) if result else None
        from mini_lancedb.encoding.compress import lz4_compress, lz4_decompress

        comp = lz4_compress(data)
        rt = lz4_decompress(comp, len(data)) == data
        print(f"  {label:<16}{len(data):>10,}{len(comp):>10,}{str(rt):>8}{str(ok):>12}")
    print()
    print("  和 C 实现对拍是必须的：只测「自己压、自己解」的往返，")
    print("  会让一个 offset 算错的编码器一路蒙混过关。")


def main() -> None:
    numeric_report()
    string_report()
    float_report()
    lz4_report()


if __name__ == "__main__":
    main()
