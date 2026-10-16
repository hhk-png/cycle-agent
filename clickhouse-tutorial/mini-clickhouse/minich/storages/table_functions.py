"""表函数(table function)注册表 —— ``FROM numbers(10)`` / ``file(...)`` 的落地。

表函数是"出现在 ``FROM`` 里的函数":

.. code-block:: text

   SELECT * FROM numbers(10);                        -- 生成 10 行
   SELECT * FROM file('x.csv', CSV, 'a UInt8');      -- 读文件
   SELECT * FROM remote('host', db, t);              -- 读远端(需分布式模块)
   SELECT * FROM generateRandom('a UInt8', 42);      -- 造随机数据

它和**表引擎**的关系是"生成器 vs 存储":表函数负责**产出一个行流**,
表引擎负责**存/取一个已经存在的数据集**。所以 ``numbers`` 是表函数
(它每调用一次就现产生数据),而 ``Memory``/``MergeTree`` 是引擎。

.. code-block:: text

   调用方(解释器)看到的接口:

     tf = get_table_function("numbers")
     cols = tf.get_columns([Literal(5)])      -- 先问结构(用于 prepare)
     for chunk in tf.execute([Literal(5)], ctx):   -- 再要数据(惰性)
         …

   args 是**求值前或求值后的参数**:mini-clickhouse 同时接受 AST 节点
   (``Literal``/``Identifier``)与已经求好的 Python 值,由 _arg_value 统一。

.. warning::

   **本模块只注册"引擎无关"的表函数。** 依赖外部系统的那些
   (``s3`` / ``hdfs`` / ``jdbc`` / ``mysql`` / ``postgresql`` / ``sqlite`` /
   ``odbc`` / ``executable`` / ``dictionary`` / ``getClientHTTPHeader``)
   在 mini-clickhouse 里**没有实现**,调用它们会抛 ``NOT_IMPLEMENTED`` 并
   说明原因 —— 我们不会"注册一个假装能用的 stub"。

   ``remote`` / ``cluster`` / ``clusterAllReplicas`` 会在上下文里找一个
   ``remote_executor``(由分布式模块注入);找不到就 ``NOT_IMPLEMENTED``,
   并把"分布式支持在另一个模块里"写进错误消息。
"""

from __future__ import annotations

import os
import random
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..block import Block, Chunk, ColumnWithTypeAndName, chunk_from_block
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ._common import (
    _check_chunk,
    _make_chunk,
    _max_block_size,
    _parse_structure,
    _read_limit,
    _settings_of,
    get_input_format_compat,
)
from .numbers import NUMBERS_COLUMN, NUMBERS_TYPE, NumbersSource, parse_numbers_args

__all__ = [
    "ITableFunction",
    "register_table_function",
    "get_table_function",
    "table_function_exists",
    "registered_table_functions",
]

ZERO_TYPE = get_data_type("UInt8")


# ══════════════════════════════════════════════════════════════════════════
# 接口
# ══════════════════════════════════════════════════════════════════════════


class ITableFunction:
    """表函数接口 —— 与真实 ClickHouse 的 ``ITableFunction`` 同名同义。

    只有两个方法,对应真实实现里的两个阶段:

    * :meth:`get_columns` —— **准备阶段**回答"我产出哪些列"(用于类型推导、
      列裁剪,此时还没有数据);
    * :meth:`execute` —— **执行阶段**产出一个 ``Chunk`` 流(惰性)。
    """

    #: 函数名 —— ``system.table_functions`` 里显示的就是它
    name: str = "Unknown"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Table function {self.name} does not implement get_columns()",
        )

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Table function {self.name} does not implement execute()",
        )


# ══════════════════════════════════════════════════════════════════════════
# 参数求值小工具
# ══════════════════════════════════════════════════════════════════════════


def _arg_value(node: Any) -> Any:
    """把一个参数(可能是 AST 节点,也可能是已求值的 Python 值)取值。"""
    if node is None or isinstance(node, (str, int, float, bool, bytes)):
        return node
    cls = type(node).__name__
    if cls == "Literal":
        return getattr(node, "value", None)
    if cls == "Identifier":
        return getattr(node, "name", None)
    value = getattr(node, "value", None)
    if value is not None and cls != "FunctionCall":
        return value
    return node  # 不透明节点(如 SelectQuery)—— 原样返回,由具体函数处理


def _arg_str(node: Any) -> Optional[str]:
    value = _arg_value(node)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _arg_int(node: Any, default: Optional[int] = None) -> Optional[int]:
    value = _arg_value(node)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
# numbers / zeros / system.numbers
# ══════════════════════════════════════════════════════════════════════════


class NumbersTableFunction(ITableFunction):
    """``numbers([start,] end[, step])`` —— 惰性生成 ``number UInt64``。"""

    name = "numbers"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return [ColumnWithTypeAndName(NUMBERS_COLUMN, NUMBERS_TYPE)]

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        values = [_arg_value(a) for a in args]
        start, end, step = parse_numbers_args(values)
        source = NumbersSource(start, end, step)
        block_size = _max_block_size(_settings_of(ctx))
        yield from source.iter_chunks(block_size, limit=_read_limit(ctx))


class SystemNumbersTableFunction(ITableFunction):
    """``system.numbers`` —— 无界(用同一个 ``number`` 列)。"""

    name = "system.numbers"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return [ColumnWithTypeAndName(NUMBERS_COLUMN, NUMBERS_TYPE)]

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        block_size = _max_block_size(_settings_of(ctx))
        limit = _read_limit(ctx)
        end = (1 << 62) if limit is None else limit
        yield from NumbersSource(0, end, 1).iter_chunks(block_size, limit=limit)


class ZerosTableFunction(ITableFunction):
    """``zeros(n)`` —— n 行 ``zero UInt8``(全是 0)。"""

    name = "zeros"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return [ColumnWithTypeAndName("zero", ZERO_TYPE)]

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        if not args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function zeros requires an argument: zeros(n)",
            )
        count = _arg_int(args[0], 0) or 0
        block_size = _max_block_size(_settings_of(ctx))
        limit = _read_limit(ctx)
        total = min(count, limit) if limit is not None else count
        expected = self.get_columns(args)
        offset = 0
        while offset < total:
            n = min(block_size, total - offset)
            column = ZERO_TYPE.create_column()
            column.append_many_default(n)
            chunk = _make_chunk(expected, [column])
            _check_chunk(chunk, expected)
            yield chunk
            offset += n


# ══════════════════════════════════════════════════════════════════════════
# input / values
# ══════════════════════════════════════════════════════════════════════════


class InputTableFunction(ITableFunction):
    """``input(structure)`` —— 把"查询附带的输入数据"当成表。

    真实 ClickHouse 用它读 ``clickhouse-client`` 追加在语句后的数据
    (``INSERT INTO t SELECT * FROM input(...)``)。mini-clickhouse 的数据
    由解释器挂到上下文的 ``input_data`` / ``input_blocks`` 上。
    """

    name = "input"

    def _structure(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        if not args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function input requires a structure argument, e.g. "
                "input('a UInt8, b String')",
            )
        text = _arg_str(args[0]) or ""
        return _parse_structure(text)

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return self._structure(args)

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        expected = self._structure(args)
        blocks = None
        if ctx is not None:
            if getattr(ctx, "input_blocks", None) is not None:
                blocks = ctx.input_blocks
            elif getattr(ctx, "input_data", None) is not None:
                data = ctx.input_data
                blocks = [data] if isinstance(data, Block) else data
        if blocks is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "Table function input() needs the interpreter to attach the query's "
                "input data (ctx.input_data / ctx.input_blocks); none was found",
            )
        for block in blocks:
            block = block.get_block_with_data() if isinstance(block, Chunk) else block
            columns = []
            for cwt in expected:
                src = block.try_get_by_name(cwt.name)
                if src is None:
                    column = cwt.type.create_column()
                    column.append_many_default(block.rows())
                else:
                    column = src.column
                columns.append(column)
            yield _make_chunk(expected, columns)


class ValuesTableFunction(ITableFunction):
    """``values('a UInt8, b String', (1,'x'), …)`` —— 字面量行。"""

    name = "values"

    def _structure(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        if not args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function values requires a structure argument",
            )
        return _parse_structure(_arg_str(args[0]) or "")

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return self._structure(args)

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        expected = self._structure(args)
        rows = [_arg_value(a) for a in args[1:]]
        columns = []
        for cwt in expected:
            column = cwt.type.create_column()
            for row in rows:
                value = None
                if isinstance(row, (list, tuple)) and len(row) == len(expected):
                    value = row[list(c.name for c in expected).index(cwt.name)]
                elif not isinstance(row, (list, tuple)):
                    value = row if len(expected) == 1 else None
                column.append(value)
            columns.append(column)
        if not rows:
            return
        yield _make_chunk(expected, columns)


# ══════════════════════════════════════════════════════════════════════════
# file / url / format
# ══════════════════════════════════════════════════════════════════════════


def _resolve_path_format_structure(
    args: Sequence[Any], *, default_format: str = "TSV"
) -> Tuple[Optional[str], str, Optional[List[ColumnWithTypeAndName]]]:
    """从 ``[path, format, structure]`` 里挑出三样东西(顺序可变)。"""
    location: Optional[str] = None
    fmt: Optional[str] = None
    structure: Optional[List[ColumnWithTypeAndName]] = None
    for arg in args:
        text = _arg_str(arg)
        if text is None:
            continue
        if " " in text or "(" in text:
            structure = _parse_structure(text)
            continue
        if fmt is None and not text.startswith(("http://", "https://")) and _is_known_format(text):
            fmt = text
            continue
        if location is None:
            location = text
        elif fmt is None:
            fmt = text
    return location, fmt or default_format, structure


def _is_known_format(name: str) -> bool:
    try:
        get_input_format_compat(name)
        return True
    except Exception:
        return False


class FileTableFunction(ITableFunction):
    """``file(path [, format [, structure]])`` —— 把一个文件当表读。"""

    name = "file"

    def _parts(self, args: Sequence[Any]):
        return _resolve_path_format_structure(args)

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        _, fmt, structure = self._parts(args)
        if structure is not None:
            return structure
        path, _, _ = self._parts(args)
        if path and os.path.exists(path):
            return self._peek_header(path, fmt)
        return []

    def _peek_header(self, path: str, fmt: str) -> List[ColumnWithTypeAndName]:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        reader = get_input_format_compat(fmt)
        blocks = reader.read(text, Block([]), None)
        for block in blocks:
            return [c.clone_empty() for c in block.data]
        return []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        path, fmt, structure = self._parts(args)
        if not path:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function file requires a path argument",
            )
        if not os.path.exists(path):
            return
        columns = structure if structure is not None else self._peek_header(path, fmt)
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        reader = get_input_format_compat(fmt)
        sample = Block([c.clone_empty() for c in columns])
        for block in reader.read(text, sample, _settings_of(ctx)):
            yield chunk_from_block(block)


class UrlTableFunction(ITableFunction):
    """``url(url [, format [, structure]])`` —— 把一个 HTTP 端点当表读。"""

    name = "url"

    def _parts(self, args: Sequence[Any]):
        return _resolve_path_format_structure(args)

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        _, _, structure = self._parts(args)
        return structure or []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        url, fmt, structure = self._parts(args)
        if not url:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function url requires a url argument",
            )
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=10.0) as resp:
                text = resp.read().decode("utf-8", "surrogateescape")
        except urllib.error.URLError as exc:
            from ..errors import error_code

            raise_error(error_code("NETWORK_ERROR"), f"url() GET {url} failed: {exc.reason}")
            return
        reader = get_input_format_compat(fmt)
        sample = Block([c.clone_empty() for c in (structure or [])])
        for block in reader.read(text, sample, _settings_of(ctx)):
            yield chunk_from_block(block)


class FormatTableFunction(ITableFunction):
    """``format(FORMAT, data [, structure])`` —— 把内联的一段文本当表解析。"""

    name = "format"

    def _parts(self, args: Sequence[Any]):
        if len(args) < 2:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function format requires (format_name, data [, structure])",
            )
        fmt = _arg_str(args[0]) or "TSV"
        data = _arg_str(args[1]) or ""
        structure = _parse_structure(_arg_str(args[2]) or "") if len(args) > 2 else None
        return fmt, data, structure

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        _, _, structure = self._parts(args)
        return structure or []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        fmt, data, structure = self._parts(args)
        reader = get_input_format_compat(fmt)
        sample = Block([c.clone_empty() for c in (structure or [])])
        for block in reader.read(data, sample, _settings_of(ctx)):
            yield chunk_from_block(block)


# ══════════════════════════════════════════════════════════════════════════
# generateRandom
# ══════════════════════════════════════════════════════════════════════════


class GenerateRandomTableFunction(ITableFunction):
    """``generateRandom(structure [, seed [, max_string_length [, max_array_length]]])``。"""

    name = "generateRandom"

    def _structure(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        if not args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function generateRandom requires a structure argument",
            )
        return _parse_structure(_arg_str(args[0]) or "")

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return self._structure(args)

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        columns = self._structure(args)
        seed = _arg_int(args[1], None) if len(args) > 1 else None
        max_string = _arg_int(args[2], 10) if len(args) > 2 else 10
        max_array = _arg_int(args[3], 10) if len(args) > 3 else 10
        rng = random.Random(seed)
        limit = _read_limit(ctx)
        rows = limit if limit is not None else _max_block_size(_settings_of(ctx))
        block_size = _max_block_size(_settings_of(ctx))
        offset = 0
        while offset < rows:
            n = min(block_size, rows - offset)
            out = []
            for cwt in columns:
                column = cwt.type.create_column()
                for _ in range(n):
                    column.append(_random_value(cwt.type, rng, max_string or 10, max_array or 10))
                out.append(column)
            chunk = _make_chunk(columns, out)
            _check_chunk(chunk, columns)
            yield chunk
            offset += n


_ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _random_value(dtype: DataType, rng: random.Random, max_string: int, max_array: int) -> Any:
    """按类型造一个随机值(只覆盖 mini-clickhouse 支持的类型)。"""
    if dtype.is_nullable():
        return None if rng.random() < 0.1 else _random_value(dtype.nested, rng, max_string, max_array)
    if dtype.is_enum():
        names = getattr(dtype, "names", None)
        if names:
            return rng.choice(list(names))
    if dtype.is_numeric():
        if dtype.is_float():
            return round(rng.uniform(-1e6, 1e6), 3)
        bits = getattr(dtype, "bits", 32)
        signed = getattr(dtype, "signed", True)
        lo = -(1 << (bits - 1)) if signed else 0
        hi = (1 << (bits - 1)) - 1 if signed else (1 << min(bits, 31)) - 1
        return rng.randint(lo, hi)
    if dtype.is_string():
        n = rng.randint(0, max_string)
        return "".join(rng.choice(_ASCII) for _ in range(n))
    if dtype.is_date():
        return rng.randint(0, 20000)
    if dtype.is_date_time():
        return rng.randint(0, 2_000_000_000)
    if dtype.is_uuid():
        return rng.getrandbits(128)
    if dtype.is_array():
        return [
            _random_value(dtype.element, rng, max_string, max_array)
            for _ in range(rng.randint(0, max_array))
        ]
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"generateRandom does not support the type {dtype.get_name()}",
    )
    return None  # pragma: no cover


# ══════════════════════════════════════════════════════════════════════════
# view / merge / null
# ══════════════════════════════════════════════════════════════════════════


class ViewTableFunction(ITableFunction):
    """``view(SELECT ...)`` —— 把一个子查询当成表。"""

    name = "view"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        executor = getattr(ctx, "subquery_executor", None) if ctx is not None else None
        if executor is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                "Table function view() cannot run: no subquery executor is attached "
                "(the interpreter must provide ctx.subquery_executor)",
            )
        select = args[0] if args else None
        for item in executor(select, ctx):
            yield item if isinstance(item, Chunk) else chunk_from_block(item)


class MergeTableFunction(ITableFunction):
    """``merge(db, regex)`` —— 把匹配的表拼成一张只读虚拟表。"""

    name = "merge"

    def _resolve(self, args: Sequence[Any], ctx: Any):
        if len(args) < 2:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "Table function merge requires (database, table_regexp)",
            )
        database = _arg_str(args[0]) or ""
        pattern = _arg_str(args[1]) or ""
        if database in ("", "currentDatabase()", "current_database()"):
            database = getattr(ctx, "database", "default") if ctx is not None else "default"
        return database, pattern

    def _matched(self, args: Sequence[Any], ctx: Any):
        import re

        database, pattern = self._resolve(args, ctx)
        catalog = getattr(ctx, "catalog", None) if ctx is not None else None
        if catalog is None:
            return []
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"merge() got an invalid regexp {pattern!r}: {exc}",
            )
        out = [t for t in catalog.tables_of(database) if regex.search(t.name)]
        out.sort(key=lambda t: t.name)
        return out

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        for table in self._matched(args, ctx):
            if table.engine is None:
                continue
            yield from table.engine.read(ctx)


class NullTableFunction(ITableFunction):
    """``null([structure])`` —— 产出零行;用于丢弃写入 / 占位。"""

    name = "null"

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        if args:
            text = _arg_str(args[0])
            if text:
                return _parse_structure(text)
        return []

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        return iter(())


# ══════════════════════════════════════════════════════════════════════════
# remote / cluster —— 委托给上下文里的 remote_executor
# ══════════════════════════════════════════════════════════════════════════


class RemoteTableFunction(ITableFunction):
    """``remote`` / ``cluster`` / ``clusterAllReplicas`` —— 分片代理。

    真正的分布式支持在另一个模块里(``Distributed`` 引擎 + 集群拓扑)。
    这里只做**委托**:上下文里若注入了 ``remote_executor``,就交给它;
    否则抛 ``NOT_IMPLEMENTED`` 并说清楚原因。
    """

    def __init__(self, name: str = "remote") -> None:
        self.name = name

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        return []

    def _executor(self, ctx: Any) -> Any:
        executor = getattr(ctx, "remote_executor", None) if ctx is not None else None
        if executor is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Table function {self.name}() is not available: distributed/cluster "
                f"support lives in a separate module (Distributed engine + cluster "
                f"topology). No remote_executor is registered on the context.",
            )
        return executor

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        executor = self._executor(ctx)
        handler = getattr(executor, "table_function", None)
        if callable(handler):
            yield from handler(self.name, args, ctx)
            return
        if callable(executor):
            yield from executor(self.name, args, ctx)
            return
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"remote_executor for {self.name}() does not expose a table_function() "
            f"method or a callable interface",
        )


# ══════════════════════════════════════════════════════════════════════════
# 明确不实现的表函数
# ══════════════════════════════════════════════════════════════════════════


class NotSupportedTableFunction(ITableFunction):
    """一个**诚实的占位**:注册了名字,但调用就抛 ``NOT_IMPLEMENTED``。"""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def get_columns(self, args: Sequence[Any]) -> List[ColumnWithTypeAndName]:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"Table function {self.name}: {self.reason}")

    def execute(self, args: Sequence[Any], ctx: Any) -> Iterator[Chunk]:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"Table function {self.name}: {self.reason}")


# 每条都给出**为什么不做**,而不是一句"未实现"。
_NOT_SUPPORTED_REASONS: Dict[str, str] = {
    "s3": "S3 object storage integration requires an AWS/S3 client and network "
          "credentials; mini-clickhouse is offline and does not bundle an S3 SDK.",
    "s3Cluster": "same as s3() plus cluster topology — both layers are out of scope.",
    "hdfs": "HDFS requires libhdfs / a Hadoop client, which is not available offline.",
    "jdbc": "JDBC requires a JVM and JDBC drivers; not available in a pure-Python build.",
    "mysql": "the MySQL table function needs a MySQL client protocol implementation.",
    "postgresql": "the PostgreSQL table function needs a PG wire-protocol client.",
    "sqlite": "the SQLite table function needs a sqlite3-backed bridge (only the "
              "in-process Python sqlite3 module exists, which is a different model).",
    "odbc": "ODBC requires platform ODBC drivers; not portable in this environment.",
    "executable": "executable() runs an external process per query; disabled for "
                  "safety and reproducibility in the teaching environment.",
    "dictionary": "dictionaries are a separate subsystem (source/layout/lifetime) and "
                  "are not part of the storage-layer deliverable.",
    "getClientHTTPHeader": "requires the HTTP client session (X-ClickHouse-* headers), "
                           "which needs the network layer that mini-clickhouse omits.",
    "timeSeries": "time-series table functions are experimental and outside this scope.",
    "timeSeriesMetrics": "time-series table functions are experimental and outside this scope.",
    "timeSeriesData": "time-series table functions are experimental and outside this scope.",
    "timeSeriesTags": "time-series table functions are experimental and outside this scope.",
}


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════


class _TFEntry:
    __slots__ = ("name", "factory", "description")

    def __init__(self, name: str, factory: Callable[[], ITableFunction], description: str) -> None:
        self.name = name
        self.factory = factory
        self.description = description


_REGISTRY: Dict[str, _TFEntry] = {}


def register_table_function(
    name: str,
    cls: Any = None,
    *,
    description: str = "",
    factory: Optional[Callable[[], ITableFunction]] = None,
    aliases: Sequence[str] = (),
) -> None:
    """注册一个表函数。名字**大小写不敏感**。

    ``cls`` 可以是类(每次 :func:`get_table_function` 构造一个新实例),
    ``factory`` 是一个返回实例的可调用对象;两者给一个即可。
    """
    if factory is None:
        if cls is None:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"register_table_function({name!r}) needs either cls or factory",
            )
        factory = cls
    entry = _TFEntry(name, factory, description)
    _REGISTRY[name.lower()] = entry
    for alias in aliases:
        _REGISTRY[alias.lower()] = entry


def table_function_exists(name: str) -> bool:
    return str(name).lower() in _REGISTRY


def get_table_function(name: str) -> ITableFunction:
    """按名字取一个表函数实例。"""
    entry = _REGISTRY.get(str(name).lower())
    if entry is None:
        raise_error(
            ErrorCode.UNKNOWN_FUNCTION,
            f"Unknown table function {name}. Known table functions: "
            f"{', '.join(sorted(_REGISTRY))}",
        )
    return entry.factory()


def registered_table_functions() -> List[Tuple[str, str]]:
    """``[(名字, 说明)]`` —— 数据源是 ``system.table_functions``。

    只列**正名**(与真实 ClickHouse 一致),别名不单独成行。
    """
    seen: Dict[int, Tuple[str, str]] = {}
    for entry in _REGISTRY.values():
        seen[id(entry)] = (entry.name, entry.description)
    return sorted(seen.values(), key=lambda row: row[0])


# ── 具体注册 ────────────────────────────────────────────────────────────


register_table_function(
    "numbers",
    NumbersTableFunction,
    description="生成 [start, end) 的整数序列(列 number UInt64),惰性分块。",
    aliases=("numbers_mt",),
)
register_table_function(
    "zeros",
    ZerosTableFunction,
    description="生成 n 行全 0(列 zero UInt8)。",
    aliases=("zeros_mt",),
)
register_table_function(
    "system.numbers",
    SystemNumbersTableFunction,
    description="system.numbers:无界整数序列 0,1,2,…(需配 LIMIT)。",
    aliases=("system.numbers_mt",),
)
register_table_function(
    "input",
    InputTableFunction,
    description="把查询附带的输入数据(结构由参数给出)当成表。",
)
register_table_function(
    "values",
    ValuesTableFunction,
    description="把一串字面量行当成表:values('a UInt8', (1), (2))。",
)
register_table_function(
    "file",
    FileTableFunction,
    description="读一个文件并解析成表:file(path, format, structure)。",
)
register_table_function(
    "url",
    UrlTableFunction,
    description="读一个 HTTP 端点并解析成表:url(url, format, structure)。",
)
register_table_function(
    "format",
    FormatTableFunction,
    description="把内联的一段文本按格式解析成表:format(FORMAT, data, structure)。",
)
register_table_function(
    "generateRandom",
    GenerateRandomTableFunction,
    description="按结构生成随机数据(支持 mini-clickhouse 的所有类型)。",
)
register_table_function(
    "view",
    ViewTableFunction,
    description="把一个子查询当成表(需要解释器注入 subquery_executor)。",
)
register_table_function(
    "merge",
    MergeTableFunction,
    description="把 db 下匹配正则的多张表拼成一张只读虚拟表。",
)
register_table_function(
    "null",
    NullTableFunction,
    description="产出零行(丢弃 / 占位)。",
)
for _alias_name in ("remote", "cluster", "clusterAllReplicas"):
    register_table_function(
        _alias_name,
        factory=(lambda n=_alias_name: RemoteTableFunction(n)),
        description="分片代理:委托给上下文里的 remote_executor(分布式模块)。",
    )
for _name, _reason in _NOT_SUPPORTED_REASONS.items():
    register_table_function(
        _name,
        factory=(lambda n=_name, r=_reason: NotSupportedTableFunction(n, r)),
        description="未实现:" + _reason[:80],
    )
del _alias_name, _name, _reason
