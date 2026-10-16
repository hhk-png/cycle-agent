"""``minich.storages`` 内部共用的小工具 —— **不是公开 API**。

这个模块回答了每一个表引擎都会遇到的四个问题,所以把它们抽出来一次:

.. code-block:: text

   ① "我该读到哪几列?"      ReadContext 里可能带列裁剪,引擎必须给出
                            **同序同名同类型**的 chunk —— 这个校验放在
                            _check_chunk 里,是引擎层最容易出错的地方。
   ② "我该写哪几列?"        插入的 Block 可能只带一部分列、顺序不同、
                            类型也不完全一致;引擎要把它对齐到表的
                            **物理列**(ALIAS / MATERIALIZED 不落盘)。
   ③ "数据落在哪个目录?"     Log 家族 / File 引擎要落盘,但目录根来自
                            Context.data_path(可能为 None,退化成临时目录)。
   ④ "结构字符串怎么解析?"   ``Values('a UInt8', 'b String')`` 这种"引擎参数
                            里带列定义"的写法需要一小段解析。

.. note::

   真实 ClickHouse 里这些东西散在 ``IStorage`` 各处
   (``getColumnNames``、``StorageLog::write``、``getDisk``…),
   mini-clickhouse 抽成一个私有模块,**只服务于教学与内部复用** ——
   读者不需要记住它,就像不需要记住 ``src/Storages/StorageFactory.cpp``
   里的辅助函数一样。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..catalog import ColumnDefinition, StorageID, Table
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type

__all__ = [
    "_setting",
    "_settings_of",
    "_max_block_size",
    "_stored_columns",
    "_requested_columns",
    "_read_limit",
    "_check_chunk",
    "_make_chunk",
    "_slice_chunk",
    "_align_block",
    "_convert_column",
    "_resolve_data_path",
    "_write_json",
    "_read_json",
    "_serialize_column",
    "_deserialize_column",
    "_parse_structure",
    "_parse_column_def",
    "get_input_format_compat",
    "get_output_format_compat",
]

#: ``max_block_size`` 的兜底默认值 —— 与 :mod:`minich.settings` 注册的一致
DEFAULT_MAX_BLOCK_SIZE = 65409


# ══════════════════════════════════════════════════════════════════════════
# settings 访问
# ══════════════════════════════════════════════════════════════════════════


def _settings_of(ctx: Any) -> Any:
    """从各种"上下文"里掏出 settings 对象(可能为 None)。

    接受 ``ReadContext``(它有 ``.settings``)、``Context``(也有 ``.settings``)
    或 ``None``。``settings`` 参数可能还没被创建(见任务约束:
    :mod:`minich.settings` 是可选依赖),所以这里只做属性读取,不做导入。
    """
    if ctx is None:
        return None
    return getattr(ctx, "settings", None)


def _setting(settings: Any, name: str, default: Any) -> Any:
    """读一条设置,取不到就返回 ``default``。

    这是任务里约定的写法 —— **绝不**让"设置模块缺失 / 名字没注册"
    把引擎拖挂:真实 ClickHouse 里未注册的设置会报 ``UNKNOWN_SETTING``,
    但教学实现里"取不到就用默认"更稳,且不影响正确性。
    """
    if settings is None:
        return default
    try:
        return settings.try_get(name, default)
    except Exception:
        return default


def _max_block_size(settings: Any) -> int:
    """一次读/产出多少行 —— 与真实 ClickHouse 的 ``max_block_size`` 对齐。"""
    value = _setting(settings, "max_block_size", DEFAULT_MAX_BLOCK_SIZE)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_MAX_BLOCK_SIZE
    return value if value > 0 else DEFAULT_MAX_BLOCK_SIZE


# ══════════════════════════════════════════════════════════════════════════
# 列的选择与校验
# ══════════════════════════════════════════════════════════════════════════


def _stored_columns(table: Table) -> List[ColumnWithTypeAndName]:
    """表的**物理列** —— 只有 :meth:`ColumnDefinition.is_stored` 为真的列落盘。

    ``ALIAS`` / ``EPHEMERAL`` / ``MATERIALIZED`` 不落盘(见
    :class:`minich.catalog.ColumnDefinition`),所以引擎不该给它们找文件。
    这也是 :meth:`ReadContext.wanted_columns` 存在的意义 ——
    它把"查询想要的列"与"引擎真正有的列"求交。
    """
    return [c.to_cwtn() for c in table.columns if c.is_stored()]


def _requested_columns(ctx: Any, available: Sequence[ColumnWithTypeAndName]) -> List[ColumnWithTypeAndName]:
    """按 ``ReadContext`` 做列裁剪,保持 ``available`` 的顺序。"""
    names = [c.name for c in available]
    if ctx is not None and hasattr(ctx, "wanted_columns"):
        try:
            wanted = ctx.wanted_columns(names)
        except Exception:
            wanted = names
    else:
        columns = getattr(ctx, "columns", None)
        if columns is None:
            wanted = names
        else:
            want = set(columns)
            wanted = [n for n in names if n in want]
    by_name = {c.name: c for c in available}
    return [by_name[n] for n in wanted]


def _read_limit(ctx: Any) -> Optional[int]:
    """``LIMIT`` 下推 —— ``ReadContext.limit``(None = 不限制)。

    真实 ClickHouse 把 ``LIMIT`` 一路下推到读取端(甚至能提前掐断扫描),
    这就是为什么 ``SELECT * FROM huge LIMIT 5`` 是毫秒级的原因。
    """
    if ctx is None:
        return None
    limit = getattr(ctx, "limit", None)
    if limit is None:
        return None
    try:
        limit = int(limit)
    except Exception:
        return None
    return limit if limit >= 0 else None


def _check_chunk(chunk: Chunk, expected: Sequence[ColumnWithTypeAndName]) -> None:
    """内部断言 —— 校验一个 chunk 与期望的列**同名同序同类型同行数**。

    引擎层最容易出的 bug 就是"列顺序变了但不报错,结果全错"(见
    :meth:`IStorage.read` 的警告)。这个 helper 把它变成一条硬断言,
    用 ``LOGICAL_ERROR_IN_MINICH`` 抛出 —— 它会出现在测试与示例里,
    让"错位"立刻暴露,而不是悄悄算出一个错误结果。
    """
    if not isinstance(chunk, Chunk):
        raise_error(
            ErrorCode.LOGICAL_ERROR_IN_MINICH,
            f"Storage returned a {type(chunk).__name__}, expected a Chunk",
        )
    header = chunk.header
    if len(header.data) != len(expected):
        raise_error(
            ErrorCode.LOGICAL_ERROR_IN_MINICH,
            f"Chunk has {len(header.data)} column(s) but "
            f"{len(expected)} were expected",
        )
    for i, cwt in enumerate(expected):
        got = header.data[i]
        if got.name != cwt.name:
            raise_error(
                ErrorCode.LOGICAL_ERROR_IN_MINICH,
                f"Chunk column {i} is named '{got.name}', expected '{cwt.name}'",
            )
        if not got.type.equals(cwt.type):
            raise_error(
                ErrorCode.LOGICAL_ERROR_IN_MINICH,
                f"Chunk column '{got.name}' has type {got.type.get_name()}, "
                f"expected {cwt.type.get_name()}",
            )
    if len(chunk.columns) != len(expected):
        raise_error(
            ErrorCode.LOGICAL_ERROR_IN_MINICH,
            f"Chunk carries {len(chunk.columns)} column(s) of data but "
            f"{len(expected)} were expected",
        )
    for i, col in enumerate(chunk.columns):
        if col.size() != chunk.num_rows:
            raise_error(
                ErrorCode.LOGICAL_ERROR_IN_MINICH,
                f"Chunk column '{expected[i].name}' has {col.size()} rows, "
                f"chunk.num_rows is {chunk.num_rows}",
            )


def _make_chunk(expected: Sequence[ColumnWithTypeAndName], columns: List[Any]) -> Chunk:
    """用期望的 header + 一批列造一个 Chunk。"""
    header = Block([c.clone_empty() for c in expected])
    return Chunk(header, columns, columns[0].size() if columns else 0)


def _slice_chunk(
    expected: Sequence[ColumnWithTypeAndName],
    columns: Sequence[Any],
    start: int,
    count: int,
) -> Chunk:
    """从一批列里切出 ``[start, start+count)`` 组成一个**新** chunk。"""
    out: List[Any] = []
    for c in columns:
        dst = c.clone_empty()
        dst.insert_range_from(c, start, count)
        out.append(dst)
    return _make_chunk(expected, out)


# ══════════════════════════════════════════════════════════════════════════
# 写入对齐
# ══════════════════════════════════════════════════════════════════════════


def _convert_column(source: Any, dtype: DataType) -> Any:
    """把一列的值**逐值**规范到目标类型(类型不一致时用)。

    真实 ClickHouse 在 ``INSERT`` 时会插入一个 ``ConvertingTransform``
    做同样的事 —— 只是它是列式且 SIMD 化的,我们退回逐值。
    """
    out = dtype.create_column()
    n = source.size()
    for i in range(n):
        value = source.get(i)
        if value is None:
            out.append(None)
        else:
            out.append(dtype.normalize(value))
    return out


def _align_block(
    block: Optional[Block],
    columns: Sequence[ColumnDefinition],
    *,
    strict: bool = True,
    table_name: str = "",
) -> Block:
    """把一个插入的 Block 对齐到表的物理列。

    规则与真实 ClickHouse 的 ``INSERT`` 路径一致:

    * 按**名字**匹配。缺的列填该类型的默认值(``DEFAULT`` 表达式由解释器
      在更上层展开,引擎只负责落盘的零值);
    * 类型不完全一致时逐值转换;
    * ``strict=True`` 时,Block 里多出来的列会报 ``NO_SUCH_COLUMN_IN_TABLE``
      ——这正是"INSERT 写了一个表里没有的列"应当得到的反馈。
      ``strict=False`` 用于物化视图 ``push``(目标表可能只接收子集)。
    """
    if block is None:
        block = Block([])
    if strict:
        known = {c.name for c in columns}
        for c in block.data:
            if c.name not in known:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"No such column '{c.name}' in table {table_name or '<table>'}",
                )
    rows = block.rows()
    out = Block([])
    for col_def in columns:
        src = block.try_get_by_name(col_def.name) if block else None
        if src is None:
            column = col_def.type.create_column()
            column.append_many_default(rows)
        elif src.type.equals(col_def.type):
            column = src.column
        else:
            column = _convert_column(src.column, col_def.type)
        out.add_column(ColumnWithTypeAndName(col_def.name, col_def.type, column))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 落盘路径与文件往返
# ══════════════════════════════════════════════════════════════════════════


def _resolve_data_path(
    context: Any,
    params: Optional[Dict[str, Any]],
    storage_id: StorageID,
    engine_name: str,
) -> str:
    """算出某张表的数据目录并确保它存在。

    搜索顺序(与真实 ClickHouse 的"磁盘由服务端配置决定"对应):

    1. ``params['path']`` —— 建表时显式给的路径(``ATTACH`` 场景);
    2. ``context.data_path`` —— 服务端的 ``path`` 设置
       (:class:`minich.execution.context.Context` 的 ``data_path``);
    3. 系统临时目录下的 ``minich_data`` —— **降级**:没有持久化配置时,
       磁盘引擎仍然能工作,只是数据落在临时目录(进程结束可被清理)。
    """
    root = None
    if params:
        root = params.get("path")
    if root is None and context is not None:
        root = getattr(context, "data_path", None)
    if root is None:
        root = os.path.join(tempfile.gettempdir(), "minich_data")

    ident = storage_id.uuid[:12] if storage_id.uuid else "noid"
    directory = os.path.join(
        str(root), engine_name, f"{storage_id.database}.{storage_id.table}.{ident}"
    )
    os.makedirs(directory, exist_ok=True)
    return directory


def _write_json(path: str, obj: Any) -> None:
    """原子写一个小 JSON 文件(先写临时文件再改名)。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: str, default: Any = None) -> Any:
    """读一个小 JSON 文件,不存在或损坏时返回 ``default``。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _serialize_column(dtype: DataType, column: Any) -> bytes:
    """把一整列序列化成字节 —— 落盘引擎的写路径。"""
    from ..serialization import get_serialization

    out = bytearray()
    get_serialization(dtype).serialize_binary(column, out, 0, column.size())
    return bytes(out)


def _deserialize_column(dtype: DataType, data: bytes, num_rows: int) -> Any:
    """从字节解出一整列(要求刚好用完) —— 落盘引擎的读路径。"""
    from ..serialization import get_serialization

    column, pos = get_serialization(dtype).deserialize_binary(data, num_rows, 0)
    if pos != len(data):
        raise_error(
            ErrorCode.CANNOT_READ_ALL_DATA,
            f"Column {dtype.get_name()} declared {num_rows} row(s) but only "
            f"{pos} of {len(data)} byte(s) were consumed",
        )
    return column


# ══════════════════════════════════════════════════════════════════════════
# 结构字符串解析
# ══════════════════════════════════════════════════════════════════════════


def _parse_column_def(text: str) -> ColumnWithTypeAndName:
    """解析 ``"a UInt8"`` / ``"a Nullable(String)"`` / ``"a"``(无类型→String)。"""
    from ..util.strings import strip_identifier_quotes

    s = text.strip()
    if not s:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            "Empty column definition in a table-function structure",
        )
    if s.startswith("`"):
        end = s.find("`", 1)
        if end < 0:
            raise_error(ErrorCode.SYNTAX_ERROR, f"Unterminated backtick in {text!r}")
        name = s[1:end]
        rest = s[end + 1 :].strip()
    else:
        idx = 0
        while idx < len(s) and (s[idx].isalnum() or s[idx] in "_$"):
            idx += 1
        name = s[:idx]
        rest = s[idx:].strip()
    if not name:
        raise_error(ErrorCode.SYNTAX_ERROR, f"Cannot parse a column name from {text!r}")
    dtype = get_data_type(rest) if rest else get_data_type("String")
    return ColumnWithTypeAndName(strip_identifier_quotes(name), dtype)


def _parse_structure(text: str) -> List[ColumnWithTypeAndName]:
    """解析一段"结构字符串" —— ``"a UInt8, b String"``。

    table function 里 ``file(path, TSV, 'a UInt8, b String')``、
    ``generateRandom('a UInt8')``、``values('a UInt8')`` 的"结构"参数
    都是这个形式。多列时用顶层逗号分隔(忽略嵌套括号里的逗号)。
    """
    from ..literals import split_top_level

    parts = split_top_level(text, ",")
    out = [ _parse_column_def(p) for p in parts if p.strip() ]
    if not out:
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            f"Cannot parse an empty structure definition: {text!r}",
        )
    return out


# ══════════════════════════════════════════════════════════════════════════
# 格式查找(带兜底)
# ══════════════════════════════════════════════════════════════════════════
#
# File / URL 引擎与 file()/format() 表函数都要"按名字找一个格式"。
# 正确做法是查 :mod:`minich.formats` 的注册表;但教学构建里注册表可能
# 还没被填充(格式模块由另一部分代码负责注册),所以这里加一层**兜底**:
# 直接从 formats 子模块里按类名找。这样 File 引擎在"注册表为空"时
# 依然可用,而不是抛一个让人摸不着头脑的 UNKNOWN_FORMAT。

#: 格式名(小写)→ (模块名, 类名)。只列已经存在的那几个。
_FALLBACK_FORMATS: Dict[str, Tuple[str, str]] = {
    "tsv": ("text_formats", "TSVInputFormat"),
    "tsvwithnames": ("text_formats", "TSVWithNamesInputFormat"),
    "tsvwithnamesandtypes": ("text_formats", "TSVWithNamesAndTypesInputFormat"),
    "csv": ("text_formats", "CSVInputFormat"),
    "csvwithnames": ("text_formats", "CSVWithNamesInputFormat"),
}


def _fallback_input_format(name: str) -> Any:
    entry = _FALLBACK_FORMATS.get(str(name).lower())
    if entry is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Unknown format {name} (the format registry is not populated and "
            f"no built-in fallback exists for it)",
        )
    module_name, class_name = entry
    import importlib

    module = importlib.import_module(f"..formats.{module_name}", __package__)
    return getattr(module, class_name)()


def get_input_format_compat(name: str) -> Any:
    """按名字取输入格式 —— 优先注册表,失败则退回内置映射。"""
    try:
        from ..formats.base import format_exists, get_input_format

        if format_exists(name):
            return get_input_format(name)
    except Exception:
        pass
    return _fallback_input_format(name)


def get_output_format_compat(name: str) -> Any:
    """按名字取输出格式 —— 优先注册表,失败则按输入类推导输出类。"""
    try:
        from ..formats.base import format_exists, get_output_format

        if format_exists(name):
            return get_output_format(name)
    except Exception:
        pass
    entry = _FALLBACK_FORMATS.get(str(name).lower())
    if entry is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Unknown format {name} (no fallback output format available)",
        )
    module_name, class_name = entry
    import importlib

    module = importlib.import_module(f"..formats.{module_name}", __package__)
    output_name = class_name.replace("InputFormat", "OutputFormat")
    cls = getattr(module, output_name, None)
    if cls is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Format {name} has no output implementation ({output_name})",
        )
    return cls()


#: ``Values`` 格式没有 TSV 那样的一一对应,单独兜底。
try:  # pragma: no cover - 纯导入保护
    from ..formats.values_format import ValuesInputFormat as _ValuesInput
    from ..formats.values_format import ValuesOutputFormat as _ValuesOutput

    _FALLBACK_FORMATS["values"] = ("values_format", "ValuesInputFormat")
except Exception:  # pragma: no cover
    _ValuesInput = _ValuesOutput = None  # type: ignore
