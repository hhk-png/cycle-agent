"""``numbers`` 的**惰性生成器** —— 表函数与 ``system.numbers`` 的共同底座。

``numbers(...)`` 是 ClickHouse 里最"不讲道理"却最常用的表函数:

.. code-block:: text

   SELECT * FROM numbers(5);            -- 0,1,2,3,4
   SELECT * FROM numbers(10, 15);       -- [10,15) → 10,11,12,13,14
   SELECT * FROM numbers(0, 100, 7);    -- 步长 7 → 0,7,14,…,98
   SELECT count() FROM numbers(10000000);   -- 一千万元素,**不**在内存里物化

最后一行是本节的核心:``numbers`` 必须**惰性 + 分块**,否则
``SELECT count() FROM numbers(10000000)`` 会先把一千万行塞进内存再数 —— 白白
浪费几百 MB。正确的做法是像真实 ClickHouse 那样,每次产出
``min(max_block_size, 剩余)`` 行,下游算一块丢一块。

.. code-block:: text

   numbers(0, 10_000_000) 的读取时间线:

     block 0 → [0 … 65408]     ┐
     block 1 → [65409 …130817] │  每次只驻留一块
     block 2 → …               │  上游(比如 count)算完即忘
     …                         ┘  因此内存占用与 N 无关

列名固定是 ``number``,类型固定是 ``UInt64`` —— 与真实 ClickHouse 一字不差。

.. note::

   ``numbers_mt`` 是"多线程版"。mini-clickhouse 是纯 Python,
   GIL 下多线程不会真的并行 —— 所以 ``numbers_mt`` 与 ``numbers``
   **行为完全相同**,我们保留这个名字只是因为教程里的 SQL 会用到它。
   **这一点写在报告里**:``_mt`` 变体接受但单线程执行。
"""

from __future__ import annotations

from typing import Any, Iterator, List, Optional, Sequence

import numpy as np

from ..block import Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ._common import _check_chunk, _make_chunk

__all__ = ["NumbersSource", "parse_numbers_args", "NUMBERS_TYPE", "NUMBERS_COLUMN"]

#: ``number`` 列的类型(全局单例,类型对象是不可变的)
NUMBERS_TYPE = get_data_type("UInt64")
NUMBERS_COLUMN = "number"


def parse_numbers_args(values: Sequence[Any]) -> tuple:
    """解析 ``numbers`` 的参数,返回 ``(start, end, step)``。

    支持三种写法(与真实 ClickHouse 一致):

    .. code-block:: text

       numbers(N)         →  start=0, end=N,  step=1
       numbers(N, M)      →  start=N, end=M,  step=1   (半开区间 [N, M))
       numbers(N, M, S)   →  start=N, end=M,  step=S

    参数个数不对抛 ``NUMBER_OF_ARGUMENTS_DOESNT_MATCH``;
    步长非正抛 ``ILLEGAL_TYPE_OF_ARGUMENT``(真实 CH 的 ``BAD_ARGUMENTS``
    在本实现的错误码表里映射到这两个之一)。
    """
    args = [int(v) for v in values]
    if len(args) == 1:
        start, end, step = 0, args[0], 1
    elif len(args) == 2:
        start, end, step = args[0], args[1], 1
    elif len(args) == 3:
        start, end, step = args[0], args[1], args[2]
    else:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            f"Function numbers requires 1 to 3 arguments, got {len(args)}",
        )
    if step <= 0:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"The step of function numbers must be positive, got {step}",
        )
    return start, end, step


class NumbersSource:
    """一个半开区间 ``[start, end)``(步长 ``step``)的惰性行源。

    :meth:`count` 是 O(1) 的(纯算术,不数),:meth:`iter_chunks` 每次只造一块。
    """

    __slots__ = ("start", "end", "step")

    def __init__(self, start: int, end: int, step: int = 1) -> None:
        self.start = int(start)
        self.end = int(end)
        self.step = int(step)

    def count(self) -> int:
        """总行数 —— 纯算术,O(1),**不**物化任何数据。"""
        if self.step <= 0 or self.end <= self.start:
            return 0
        return (self.end - self.start + self.step - 1) // self.step

    def header(self) -> List[ColumnWithTypeAndName]:
        return [ColumnWithTypeAndName(NUMBERS_COLUMN, NUMBERS_TYPE)]

    def make_column(self, offset: int, count: int) -> Any:
        """造第 ``offset`` 段起、共 ``count`` 个值的一列(UInt64)。"""
        column = NUMBERS_TYPE.create_column()
        if count <= 0:
            return column
        # 用向量化的 arange 而不是 Python 循环 —— 这是"惰性"之外的第二层优化。
        values = (np.arange(count, dtype=np.uint64) * np.uint64(self.step)) + np.uint64(self.start)
        values = values + np.uint64(offset * self.step)
        column.append_many(values)
        return column

    def iter_chunks(
        self,
        block_size: int,
        limit: Optional[int] = None,
    ) -> Iterator[Chunk]:
        """按 ``block_size`` 分块产出 —— 内存占用与总行数无关。"""
        expected = self.header()
        total = self.count()
        if limit is not None:
            total = min(total, max(0, limit))
        offset = 0
        while offset < total:
            count = min(block_size, total - offset)
            chunk = _make_chunk(expected, [self.make_column(offset, count)])
            _check_chunk(chunk, expected)
            yield chunk
            offset += count
