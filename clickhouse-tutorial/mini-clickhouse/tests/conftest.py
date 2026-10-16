"""测试套件的公共夹具。

这里只放**所有测试文件都会用到**的东西,保持很小:

* :func:`make_context` —— 一个"创建 ``Context`` 并保证退出时 ``shutdown()``"
  的工厂。任何持有磁盘目录或后台合并线程的 ``Context`` 都必须经它创建,
  否则测试结束时后台线程会泄漏(见教程第 31 章"为什么必须幂等 shutdown")。
* :func:`memory_ctx` —— 纯内存 ``Context``(``data_path=None``)。
* :func:`disk_ctx` —— 基于 ``tmp_path`` 的 ``Context``(MergeTree 把 part 落盘)。
* :func:`ev` —— 一张固定的样本表,列与数据都写死,便于断言精确结果。
* :func:`assert_rows` —— 把一个 ``QueryResult`` 和期望行列表做比较。

.. note::

   夹具刻意**不**直接返回裸 ``Context`` 而不清理 —— ``yield`` + ``finally``
   保证即使测试体抛异常,``shutdown()`` 也会被调用。这是本套件"测试必须
   自己收尾"这条规矩的落地方式。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Sequence, Tuple

import pytest

from minich.block import Block
# 注意:包的 ``__init__`` 里**没有**导出 ``Context``(尽管它的文档字符串
# 写着 ``ch.Context()``)。这里按实际可用的路径导入,并把那条文档与实现的
# 偏差记录在 test_regressions.py 里。
from minich.execution.context import Context, QueryResult


# ── Context 工厂 ────────────────────────────────────────────────────────


@pytest.fixture
def make_context(tmp_path):
    """返回一个"造 Context"的可调用对象,并负责在测试结束时关掉所有它造的实例。

    用法::

        def test_x(make_context):
            ctx = make_context()                     # 纯内存
            ctx2 = make_context(data_path=...)       # 落盘
            ...
    """
    created: List[Context] = []

    def _make(**kwargs: Any) -> Context:
        ctx = Context(**kwargs)
        created.append(ctx)
        return ctx

    try:
        yield _make
    finally:
        # 显式 try/finally:即使测试体抛异常,也要把每个 Context 关掉
        # (MergeTree 的后台合并线程/TTL 线程靠 shutdown 停止)。
        for ctx in created:
            try:
                ctx.shutdown()
            except Exception:  # pragma: no cover - 清理路径不该改变测试结果
                pass


@pytest.fixture
def memory_ctx(make_context) -> Context:
    """纯内存 ``Context``(``data_path=None``)—— 不产生任何文件。"""
    return make_context()


@pytest.fixture
def disk_ctx(make_context, tmp_path) -> Context:
    """把 ``data_path`` 指向 ``tmp_path`` 的 ``Context`` —— 适合 MergeTree。"""
    data_dir = tmp_path / "minich_data"
    return make_context(data_path=str(data_dir))


# ── 样本表 ──────────────────────────────────────────────────────────────

#: ``ev`` 表的列定义(事件流:时间、用户、URL、状态码、耗时)。
EV_DDL = (
    "ts DateTime, uid UInt32, url String, status UInt16, dur Float64"
)

#: ``ev`` 表的固定数据 —— 断言里逐字引用这份,保证"期望值"唯一。
EV_ROWS: List[Tuple[Any, ...]] = [
    ("2024-01-01 00:00:00", 1, "/a", 200, 0.5),
    ("2024-01-01 00:00:01", 2, "/b", 404, 1.5),
    ("2024-01-01 00:00:02", 1, "/a", 500, 2.25),
    ("2024-01-01 00:00:03", 3, "/c", 200, 0.0),
    ("2024-01-01 00:00:04", 2, "/a", 301, 3.75),
]


@pytest.fixture
def ev(memory_ctx: Context) -> Context:
    """在内存里建一张 ``ev`` 表并填入 :data:`EV_ROWS`。

    返回 ``Context`` 本身,测试里直接 ``ctx.execute("SELECT ... FROM ev")``。
    """
    memory_ctx.create_table("ev", EV_DDL, engine_sql="Memory")
    memory_ctx.insert("ev", EV_ROWS)
    return memory_ctx


# ── 断言辅助 ────────────────────────────────────────────────────────────


def assert_rows(result: QueryResult, expected: Sequence[Sequence[Any]]) -> None:
    """断言 ``result`` 的行集合(含顺序)等于 ``expected``。

    比较前把每行包成 ``tuple`` —— ``QueryResult.rows()`` 已经是元组,
    但期望值常写成列表,这里统一。
    """
    got = [tuple(r) for r in result.rows()]
    want = [tuple(r) for r in expected]
    assert got == want, f"rows mismatch:\n  got      = {got}\n  expected = {want}"


def assert_rows_unordered(result: QueryResult, expected: Sequence[Sequence[Any]]) -> None:
    """忽略行顺序地比较行集合(用 ``sorted`` 归一化)。"""
    got = sorted(tuple(r) for r in result.rows())
    want = sorted(tuple(r) for r in expected)
    assert got == want, f"row sets differ:\n  got      = {got}\n  expected = {want}"


@pytest.fixture
def rows_of() -> Callable[[QueryResult], List[Tuple[Any, ...]]]:
    """把一个 ``QueryResult`` 拉平成 ``[(...) ...]`` 的小助手。"""
    def _rows(result: QueryResult) -> List[Tuple[Any, ...]]:
        return [tuple(r) for r in result.rows()]

    return _rows
