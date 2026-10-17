#!/usr/bin/env python3
"""第 12 章 §12.7 那张"四种查询模式下 `_distance` 语义不同"的表的原始脚本。

跑法：

    python -X utf8 examples/verify/ch12_refine_factor.py

**这个脚本是补写的。** 原来那一节里的表是手抄的，其中 `refine_factor(1)`
那一行抄错了——它显示的距离被重排成了真实值，而实现里
`mini_lancedb/index/ivf.py` 是按 `refine > 1` 判断要不要精排的，
所以 `refine_factor=1` 是**空操作**，输出应该和默认模式逐位相同。
手抄的输出不会报错，只会安静地错下去，这就是补这个脚本的理由。

脚本刻意把查询向量设成库里的第 42 行，于是"真值 top-5"里必然有
`id=42` 且距离恰好是 0.0 —— 这让"距离是估计值还是真实值"一眼可辨。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

import mini_lancedb as ml  # noqa: E402

N, DIM = 20000, 256
QUERY_ROW = 42
K = 5


def fmt(rows) -> str:
    return "[" + ", ".join(f"({r['id']}, {r['_distance']:.4f})" for r in rows) + "]"


def ids(rows):
    return {r["id"] for r in rows}


def recall(rows, truth) -> float:
    return len(ids(rows) & ids(truth)) / len(truth)


def main() -> int:
    rng = np.random.default_rng(0)
    vecs = rng.normal(size=(N, DIM)).astype("float32")

    db = ml.connect("memory://")
    tbl = db.create_table("t", [{"id": i, "v": vecs[i].tolist()} for i in range(N)])
    tbl.create_index(
        "v",
        config={"index_type": "IVF_PQ", "num_partitions": 16, "num_sub_vectors": 32},
    )

    q = vecs[QUERY_ROW]

    def run(*flags, **params):
        r = tbl.search(q).limit(K)
        for name in flags:
            r = getattr(r, name)()
        for name, value in params.items():
            r = getattr(r, name)(value)
        return r.to_list()

    truth = run("bypass_vector_index")
    default = run(nprobes=20)
    rf1 = run(nprobes=20, refine_factor=1)
    rf20 = run(nprobes=20, refine_factor=20)
    np1 = run(nprobes=1)

    print(f"数据集: {N} 行 × {DIM} 维随机向量；索引 IVF_PQ(16 分区、32 子向量)")
    print(f"查询向量就是库里的第 {QUERY_ROW} 行，所以真值里它必须是 0.0\n")
    print(f"{'暴力真值':<18}: {fmt(truth)}")
    print(f"{'默认 (nprobes=20)':<18}: {fmt(default)}")
    print(f"{'refine_factor(1)':<18}: {fmt(rf1)}")
    print(f"{'refine_factor(20)':<18}: {fmt(rf20)}")
    print(f"{'nprobes(1)':<18}: {fmt(np1)}")
    print()

    print(f"refine_factor(1) 与默认逐位相同吗: {default == rf1}")
    print(
        "  —— 是。实现里精排的门槛是 `refine > 1`，所以 rf=1 是空操作，"
        "不是'只换算距离不换候选'。"
    )
    print()
    print(f"{'模式':<20} {'id=42 的距离':>14} {'recall@5':>10}")
    for label, rows in [
        ("暴力真值", truth),
        ("默认 (nprobes=20)", default),
        ("refine_factor(1)", rf1),
        ("refine_factor(20)", rf20),
        ("nprobes(1)", np1),
    ]:
        d = next((r["_distance"] for r in rows if r["id"] == QUERY_ROW), None)
        shown = "不在结果里" if d is None else f"{d:.4f}"
        print(f"{label:<20} {shown:>14} {recall(rows, truth):>10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
