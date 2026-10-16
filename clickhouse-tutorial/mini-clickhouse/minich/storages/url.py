"""``URL`` 引擎 —— 与 :class:`~minich.storages.file.StorageFile` 同构,只是换成 HTTP。

.. code-block:: text

   CREATE TABLE remote_hits ENGINE = URL('http://example.com/hits.tsv', TSV);
   SELECT * FROM remote_hits;      -- GET 下来,按 TSV 解析
   INSERT INTO remote_hits ...;    -- GET 现有 → 合并 → POST 回去(整份重写)

**它存什么?** 存一个 URL 与一个格式。数据在网络另一端。

**它不能做什么?**
* 没有索引、没有分区、没有增量写;
* **不保证写成功**:网络抖动、对方 5xx 都可能丢数据(见下 warning);
* 不能用于高并发写 —— 两个客户端会互相覆盖。

**什么时候该选它(而不是 MergeTree)?**
把"远端的一个 HTTP 端点"临时当成一张只读表来查。**绝大多数生产场景里,
你不该用它写** —— ClickHouse 有更合适的 ``URL``/``S3`` 表函数与
``Distributed`` 引擎。

.. warning::

   **真实 ClickHouse 的 ``INSERT`` 到 URL 表是"直接把新数据 POST 出去"**,
   不做 GET 合并;mini-clickhouse 为了"文件即表"的往返语义,
   实现成 **GET → 合并 → POST 整份**(与 :class:`StorageFile` 一致)。
   这是刻意差异:

   * 好处 —— 读写能往返,教学上"URL 是一个远端文件"的模型成立;
   * 代价 —— 每次写入都要先下载整个文件,而且**绝不**用于生产。

.. note::

   mini-clickhouse **不在导入期**连接网络 —— :mod:`urllib.request` 是惰性
   导入的,只有真正 ``read``/``write`` 时才用。这样测试里建一张 URL 表
   (但不碰它)不会发任何请求。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from ..block import Block, Chunk, ColumnWithTypeAndName, chunk_from_block
from ..errors import ErrorCode, error_code, raise_error
from ._common import (
    _check_chunk,
    _read_limit,
    _requested_columns,
    _settings_of,
    _stored_columns,
    get_input_format_compat,
    get_output_format_compat,
)
from .base import IStorage, StorageFeatures, register_storage_engine
from .file import _StringWriter, _format_from_path, _looks_like_format

__all__ = ["StorageURL"]


class StorageURL(IStorage):
    """HTTP 文件表引擎 —— 见模块文档。"""

    engine_name = "URL"

    features = StorageFeatures(parallel_read=False)

    #: 单次请求超时(秒)——真实 CH 有 ``http_receive_timeout`` 等设置。
    timeout: float = 10.0

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        args = [a if isinstance(a, str) else getattr(a, "value", getattr(a, "name", None)) or str(a)
                for a in (engine_args or [])]
        url: Optional[str] = None
        fmt: Optional[str] = None
        for token in args:
            if fmt is None and _looks_like_format(token) and "://" not in token:
                fmt = token
            elif url is None:
                url = token
        self.url: str = self.params.get("url") or url or ""
        self.format: str = self.params.get("format") or fmt or (
            _format_from_path(self.url) if self.url else "TSV"
        )
        self._inferred: Optional[List[ColumnWithTypeAndName]] = None

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        stored = _stored_columns(self.table)
        if stored:
            return stored
        if self._inferred is None:
            self._inferred = self._infer_columns()
        return self._inferred

    def _infer_columns(self) -> List[ColumnWithTypeAndName]:
        """建表没写列定义时,GET 一次从响应里推断结构(失败就返回空)。"""
        text = self._fetch_text()
        if not text:
            return []
        try:
            from ..catalog import ColumnDefinition

            reader = get_input_format_compat(self.format)
            for block in reader.read(text, Block([]), None):
                columns = [c.clone_empty() for c in block.data]
                self.table.columns = [ColumnDefinition(c.name, c.type) for c in columns]
                return columns
        except Exception:
            return []
        return []

    def get_name(self) -> str:
        return f"URL('{self.url}', {self.format})"

    def row_count(self) -> int:
        n = 0
        for chunk in self.read(None):
            n += chunk.num_rows
        return n

    def describe_stats(self) -> Dict[str, Any]:
        return {"url": self.url, "format": self.format}

    # ── 网络 ────────────────────────────────────────────────────────────

    def _fetch_text(self) -> Optional[str]:
        if not self.url:
            return None
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise_error(
                error_code("NETWORK_ERROR"),
                f"URL engine GET {self.url} failed: HTTP {exc.code}",
            )
        except urllib.error.URLError as exc:
            raise_error(
                error_code("NETWORK_ERROR"),
                f"URL engine GET {self.url} failed: {exc.reason}",
            )
        return raw.decode("utf-8", "surrogateescape")

    def _post_text(self, text: str) -> None:
        import urllib.error
        import urllib.request

        data = text.encode("utf-8", "surrogateescape")
        request = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.URLError as exc:
            raise_error(
                error_code("NETWORK_ERROR"),
                f"URL engine POST {self.url} failed: {exc.reason}",
            )

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected:
            return
        text = self._fetch_text()
        if text is None:
            return
        settings = _settings_of(ctx)
        fmt = get_input_format_compat(self.format)
        sample = Block([c.clone_empty() for c in expected])
        limit = _read_limit(ctx)
        produced = 0
        for block in fmt.read(text, sample, settings):
            chunk = chunk_from_block(block)
            if limit is not None:
                if produced >= limit:
                    return
                remaining = limit - produced
                if chunk.num_rows > remaining:
                    chunk = chunk.slice(0, remaining)
            _check_chunk(chunk, expected)
            produced += chunk.num_rows
            yield chunk

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        if not self.url:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"URL engine for {self.full_table_name()} has no url",
            )
        expected = self.get_columns()
        existing = Block([])
        text = self._fetch_text()
        if text:
            fmt = get_input_format_compat(self.format)
            sample = Block([c.clone_empty() for c in expected])
            for parsed in fmt.read(text, sample, _settings_of(ctx)):
                existing.merge(parsed)
        incoming = Block([])
        for cwt in expected:
            src = block.try_get_by_name(cwt.name)
            if src is None:
                column = cwt.type.create_column()
                column.append_many_default(block.rows())
            else:
                column = src.column
            incoming.add_column(ColumnWithTypeAndName(cwt.name, cwt.type, column))
        existing.merge(incoming)
        fmt = get_output_format_compat(self.format)
        out = _StringWriter()
        settings = _settings_of(ctx)
        fmt.write_prefix(existing, out, settings)
        fmt.write(existing, out, settings)
        fmt.write_suffix(existing, out, settings)
        self._post_text(out.getvalue())

    def shutdown(self) -> None:
        self._shutdown = True


def _factory(table, storage_id, context, engine_args, params) -> StorageURL:
    return StorageURL(table, storage_id, context, engine_args, params)


register_storage_engine(
    "URL",
    _factory,
    description="把远端 HTTP 端点上的一份格式文件当成表(GET 读,GET+POST 重写)。",
)
