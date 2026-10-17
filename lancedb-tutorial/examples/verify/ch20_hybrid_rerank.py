# -*- coding: utf-8 -*-
"""第 20 章引用的融合数字：RRF 的手算对照、k 敏感性、weights、线性加权。

这一章原先**没有任何可运行脚本**——`/20.3` 那张"实跑输出"和
`/20.4` 的 weights 对照都是手写在正文里的，结果其中一处顺序印错、
还多出一行不存在的 `rowid=6`，半年没人发现。这个脚本就是为了堵住这个口子：
正文里每一个 RRF 数字，都能在这里一条命令跑出来。

数据是固定的：5 篇文档、两路召回，
向量路名次 `[1,2,3,4,5]`、FTS 路名次 `[3,1,4,2,5]`。
两路只给名次、不给真实分数——因为 RRF **只看名次**，
这也正是它对分数尺度免疫的原因。

跑法：

    cd lancedb-tutorial
    python -X utf8 examples/verify/ch20_hybrid_rerank.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mini_lancedb.rerank import (  # noqa: E402
    DISTANCE_COL,
    RELEVANCE_COL,
    SCORE_COL,
    LinearCombinationReranker,
    RRFReranker,
)

#: 向量路名次（`_distance` 升序：越靠前越相似）
VEC_ORDER = [1, 2, 3, 4, 5]
#: FTS 路名次（`_score` 降序）
FTS_ORDER = [3, 1, 4, 2, 5]

#: 手算期望值：score(d) = 1/(rank_vec + k) + 1/(rank_fts + k)
HAND_COMPUTED = {
    1: 1 / 61 + 1 / 62,
    2: 1 / 62 + 1 / 64,
    3: 1 / 63 + 1 / 61,
    4: 1 / 64 + 1 / 63,
    5: 1 / 65 + 1 / 65,
}


def rows(order, key):
    """按名次顺序造两路结果。

    分数列的值只是"名次本身"——`0.0, 1.0, 2.0...`。
    故意用这种"假分数"是为了说明 RRF 的性质：
    它丢掉幅度、只用名次，所以分数取什么值都不影响输出。
    """
    return [{"_rowid": row_id, key: float(rank)} for rank, row_id in enumerate(order)]


def ids(out):
    return [r["_rowid"] for r in out]


def main() -> int:
    vec = rows(VEC_ORDER, DISTANCE_COL)
    fts = rows(FTS_ORDER, SCORE_COL)

    print("=" * 66)
    print("1. RRF 手算对照（k=60，两路权重都是 1）")
    print("=" * 66)
    out = RRFReranker(k=60).rerank_hybrid(None, vec, fts)
    print("  rowid   vec 名次   fts 名次              RRF 分数   手算")
    ok = True
    for r in out:
        row_id = r["_rowid"]
        got = r[RELEVANCE_COL]
        want = HAND_COMPUTED[row_id]
        same = abs(got - want) < 1e-12
        ok = ok and same
        rank_v = VEC_ORDER.index(row_id) + 1
        rank_f = FTS_ORDER.index(row_id) + 1
        print(
            f"  {row_id:>5}   {rank_v:>8}   {rank_f:>8}   "
            f"{got:>20.17f}   {'一致' if same else '不一致'}"
        )
    print(f"\n顺序 = {ids(out)}（正文 §20.3 写的就是这个）")
    print(f"手算逐位一致：{ok}")

    print()
    print("=" * 66)
    print("2. k 敏感性：k 变了，分数变、顺序不变")
    print("=" * 66)
    print("正文说'顺序居然完全没变'——这里能看到原因：")
    print("这 5 篇的两路名次互不重合，k 只等比缩放所有项，不改变相对大小。")
    for k in (1, 60, 10_000):
        out = RRFReranker(k=k).rerank_hybrid(None, vec, fts)
        print(f"  k={k:<7} order={ids(out)}  top={out[0][RELEVANCE_COL]:.8f}")

    print()
    print("=" * 66)
    print("3. weights：把 FTS 路的权重翻倍")
    print("=" * 66)
    base = ids(RRFReranker(k=60).rerank_hybrid(None, vec, fts))
    weighted = ids(RRFReranker(k=60, weights=[1.0, 2.0]).rerank_hybrid(None, vec, fts))
    print(f"  等权重 [1.0, 1.0] -> {base}")
    print(f"  FTS×2  [1.0, 2.0] -> {weighted}")
    print("  rowid=3 是 FTS 路的第 1 名，权重翻倍后它升到第一位。")

    print()
    print("=" * 66)
    print("4. 线性加权：它和 RRF 给出**不同**的顺序")
    print("=" * 66)
    print("两路分数用上面那组'名次假装分数'的值，所以这里只看形状：")
    out = LinearCombinationReranker(weight=0.5).rerank_hybrid(None, vec, fts)
    for r in out:
        print(f"  rowid={r['_rowid']}  rel={r[RELEVANCE_COL]:.4f}")
    print(f"\n线性加权顺序 = {ids(out)}")
    print(f"RRF     顺序 = {base}")
    print("同一批候选、同一组输入，两个融合器给出不同答案——")
    print("这不是 bug，是'只用名次'和'保留幅度'两种设计目标的必然结果。")

    print()
    print("=" * 66)
    print("5. RRF 的量纲上限（正文 §20.2 那条更正）")
    print("=" * 66)
    rrf_out = RRFReranker(k=60).rerank_hybrid(None, vec, fts)
    top_rrf = rrf_out[0][RELEVANCE_COL]
    print("每篇文档最多拿 m 项（m = 路数），每项不超过 1/(1+k)，")
    print("所以上限是 m/(1+k)，**不是** 1/k。两路 k=60 时：")
    print(f"  上限 m/(1+k) = 2/61 = {2 / 61:.10f}")
    print(f"  常见说法 1/k = 1/60 = {1 / 60:.10f}   <- 偏小，它只算了一路")
    print(f"  本批 top     = {top_rrf:.10f}（没人两路都排第一，所以没顶到上限）")
    print("  对照 §20.8 那个例子的 top 是 0.032787，**超过了 1/k**——")
    print("  这就是'上限是 1/k'这个说法站不住的直接证据。")
    return 0 if ok and base == [1, 3, 2, 4, 5] else 1


if __name__ == "__main__":
    raise SystemExit(main())
