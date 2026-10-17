#!/usr/bin/env python3
"""第 22 章 §22.3 的原始测量脚本：四个离线嵌入函数的区分度。

跑法：

    python -X utf8 examples/verify/ch22_embedding_functions.py

**这个脚本是补写的。** 那一节原来给的两组数字（"相关落在 0.5774 到
0.6860、不相关落在 0.2041 到 0.2835、间隔 0.3732 到 0.4082"，
以及 char-ngram 的 "0.136 / 0.544 / -0.123"）没有配套脚本，
而且第一组复现不出来。相似度是**确定性**的量——同样的文本、同样的
函数、同样的机器，结果必须逐位相同——所以对不上就是真错，
不能当成"抖动"。

脚本直接复用 `tests/test_embedding.py` 里的 `RELATED_PAIRS`：
那 8 组"查询 / 词面重叠 / 不相关"是测试已经用着的夹具，
拿它当基准可以保证"教程里的数字"和"测试里的断言"看的是同一批数据。
**用同一份夹具这一点是有意的**：如果教程自己另造一组句子，
两边就会各自漂移，而没人会发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402

from mini_lancedb.embedding.builtin import (  # noqa: E402
    BOWEmbeddingFunction,
    CharNgramEmbeddingFunction,
    DeterministicFakeEmbedding,
    HashingEmbeddingFunction,
)
from test_embedding import RELATED_PAIRS, VOCAB  # noqa: E402


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(a @ b / (na * nb))


def main() -> int:
    families = [
        ("hashing", HashingEmbeddingFunction.create()),
        ("char-ngram", CharNgramEmbeddingFunction.create()),
        ("fake", DeterministicFakeEmbedding.create()),
        ("bow", BOWEmbeddingFunction(VOCAB)),
    ]

    print(f"夹具: tests/test_embedding.py::RELATED_PAIRS（{len(RELATED_PAIRS)} 组）")
    print()
    print(f"{'函数':<12}{'维数':>6}{'相关 [min, max]':>24}{'不相关 [min, max]':>26}{'最小间隔':>12}")

    for name, func in families:
        related, unrelated = [], []
        for query, rel_doc, irr_doc in RELATED_PAIRS:
            q = func.compute_query_vectors([query])[0]
            related.append(cosine(q, func.compute_source_vectors([rel_doc])[0]))
            unrelated.append(cosine(q, func.compute_source_vectors([irr_doc])[0]))
        gap = min(r - u for r, u in zip(related, unrelated))
        print(
            f"{name:<12}{func.ndims():>6}"
            f"{f'[{min(related):.4f}, {max(related):.4f}]':>24}"
            f"{f'[{min(unrelated):.4f}, {max(unrelated):.4f}]':>26}"
            f"{gap:>12.4f}"
        )

    print()
    print("逐对明细（hashing vs char-ngram）：")
    h, c = families[0][1], families[1][1]
    print(f"{'#':<3}{'查询':<26}{'hashing 相关/不相关':>26}{'char-ngram 相关/不相关':>28}")
    for i, (query, rel_doc, irr_doc) in enumerate(RELATED_PAIRS, 1):
        def pair(f):
            q = f.compute_query_vectors([query])[0]
            return (
                cosine(q, f.compute_source_vectors([rel_doc])[0]),
                cosine(q, f.compute_source_vectors([irr_doc])[0]),
            )

        hr, hu = pair(h)
        cr, cu = pair(c)
        print(
            f"{i:<3}{query:<26}"
            f"{f'{hr:+.4f} / {hu:+.4f}':>26}"
            f"{f'{cr:+.4f} / {cu:+.4f}':>28}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
