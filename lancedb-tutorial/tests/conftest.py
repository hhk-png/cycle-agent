"""共享夹具。

**默认全部跑在 ``memory://`` 上。** 这不是图省事：本地临时目录的方案会让
"上一条用例残留的文件"影响下一条（比如某个用例中途失败、没删掉目录，
下一条用例的 ``table_names()`` 就多出一张表）。内存 store 让每条用例
从**绝对干净**的状态开始，失败信息也就永远只反映它自己。

真正落到 ``tmp_path`` 上的用例很少——附录 E 的 E.5 节数过，
只有 ``test_connection.py`` 里那么几条。**看起来"必须落盘"的那些其实没有**：
``cleanup_old_versions`` 的用例和权限表的用例都跑在内存 store 上
（"删文件"在内存 store 里同样是真实的删除操作，只是对象是内存里的字节）。
这一条容易记反，写新用例时按 E.5 的口径对一遍，别按直觉。
"""

from __future__ import annotations

import numpy as np
import pytest

import mini_lancedb as mldb

#: 主测试表的行数。刻意 **> 32**：小规模的页（10 行）走不到压缩路径，
#: 而"字符串列的压缩/解码"恰恰是最容易出错的地方（行边界丢失）。
N_ROWS = 120
#: 向量维度。选 8 是为了让 ``num_sub_vectors`` 有整除的余地。
DIM = 8

#: 一份小而真实的语料：有重复词、有停用词、有长尾。
WORDS = ["alpha", "beta", "gamma", "delta", "fox", "dog", "quick", "lazy", "brown", "jumps"]


def make_vectors(n: int = N_ROWS, dim: int = DIM, seed: int = 0) -> np.ndarray:
    """确定性的向量矩阵 ``(n, dim)`` float32。

    **确定性是硬要求**：任何"顺序对不对"的断言，只要数据是随机的，
    就会变成间歇性失败，而间歇性失败没人会去查。
    """

    rng = np.random.default_rng(seed)
    return rng.normal(size=(n, dim)).astype("float32")


def make_rows(n: int = N_ROWS, dim: int = DIM, seed: int = 0):
    """``(rows, vectors)``，行里带 ``id`` / ``text`` / ``vector`` 三列。"""

    vecs = make_vectors(n, dim, seed)
    rows = []
    for i in range(n):
        text = f"{WORDS[i % len(WORDS)]} {WORDS[(i * 3) % len(WORDS)]} doc{i}"
        rows.append({"id": i, "text": text, "vector": vecs[i].tolist()})
    return rows, vecs


def brute_force_order(
    vecs: np.ndarray, query: np.ndarray, k: int, metric: str = "l2"
) -> list:
    """用 numpy 独立算一遍最近邻的**行号顺序**。

    这是本套测试里最重要的一个工具：所有"排序对不对"的断言都拿它当
    参照，而不是拿被测代码自己的另一次输出去比。**用被测代码验证
    被测代码**，是这类测试最容易掉进去的陷阱 —— 两边同时错的时候，
    测试是绿的。
    """

    q = np.asarray(query, dtype=np.float32).reshape(1, -1)
    m = np.asarray(vecs, dtype=np.float32)
    if metric == "cosine":
        m = m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-12)
        d = 1.0 - (q @ m.T)[0]
    elif metric == "dot":
        d = 1.0 - (q @ m.T)[0]
    else:
        d = (((m - q) ** 2).sum(axis=1)).astype(np.float64)
    return list(np.argsort(d, kind="stable")[:k])


@pytest.fixture
def db():
    """一个空的、纯内存的连接。"""

    return mldb.connect("memory://")


@pytest.fixture
def rows_vecs():
    return make_rows()


@pytest.fixture
def tbl(db, rows_vecs):
    """基础表：120 行，无索引。"""

    rows, vecs = rows_vecs
    table = db.create_table("docs", rows)
    table._test_vectors = vecs  # 给测试用的参照矩阵（不走公开 API）
    return table


@pytest.fixture
def indexed_tbl(db, rows_vecs):
    """带 IVF_PQ 向量索引的表。"""

    rows, vecs = rows_vecs
    table = db.create_table("indexed", rows)
    table.create_index(
        "vector", config={"index_type": "IVF_PQ", "num_partitions": 4}
    )
    table._test_vectors = vecs
    return table


@pytest.fixture
def fts_tbl(db, rows_vecs):
    """带 FTS 索引的表。"""

    rows, vecs = rows_vecs
    table = db.create_table("fts", rows)
    table.create_index("text", config="FTS")
    table._test_vectors = vecs
    return table


@pytest.fixture
def both_indexed(db, rows_vecs):
    """同时有向量索引和 FTS 索引的表（混合检索用）。"""

    rows, vecs = rows_vecs
    table = db.create_table("both", rows)
    table.create_index("vector", config={"index_type": "IVF_PQ", "num_partitions": 4})
    table.create_index("text", config="FTS")
    table._test_vectors = vecs
    return table


def pytest_configure(config):
    """把 LanceDB 自己的噪声降下去，让失败信息显眼。"""

    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
