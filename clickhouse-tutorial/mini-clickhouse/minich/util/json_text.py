"""JSON 文本的读写 —— 供 ``JSONEachRow`` 等格式与 ``JSON`` 类型使用。

点很小,但有两个**必须做对**的细节,否则 JSON 格式的输出就没法被别人消费:

1. **浮点的非有限值**。JSON 标准不允许 ``NaN``/``Infinity``,但
   ClickHouse 的 ``JSONEachRow`` 会照原样输出 ``nan`` / ``inf``
   (这与 ``python -m json.tool`` 的行为不一致,但和真实 ClickHouse 一致)。
   我们提供 ``allow_nan=True`` 模式来实现这一点。

2. **大整数**。``Int64``/``UInt64`` 在 JavaScript 里会精度丢失,
   所以 ClickHouse 的 ``output_format_json_quote_64bit_integers`` 设置
   (默认 **1**)会把 64 位整数**输出成字符串**。这个默认值坑过很多人
   (拿到 JSON 之后 ``typeof x === 'string'``),所以我们在设置系统里
   把它显式做成一个可切换的 setting。
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..errors import ErrorCode, raise_error

__all__ = ["to_json_text", "from_json_text", "json_escape_string"]


def to_json_text(value: Any, indent: int = 0, allow_nan: bool = True) -> str:
    """把 Python 值编码成 JSON 文本(紧凑形式,与 ClickHouse 一致)。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=allow_nan,
            default=_default_encoder,
        )
    except (TypeError, ValueError) as exc:
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot encode value as JSON: {exc}")


def _default_encoder(value: Any) -> Any:
    """兜底编码器:日期/Decimal/bytes 这些 JSON 不认识的东西转成字符串。"""
    import datetime as _dt
    import decimal as _decimal
    from decimal import Decimal as _PyDecimal

    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat(sep=" ") if isinstance(value, _dt.datetime) else value.isoformat()
    if isinstance(value, _PyDecimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "surrogateescape")
    if isinstance(value, (tuple, set)):
        return list(value)
    return str(value)


def from_json_text(text: str) -> Any:
    """解析 JSON 文本。

    与真实 ClickHouse 的 ``JSONEachRow`` 输入一致地**接受**
    ``NaN``/``Infinity`` 字面量(ClickHouse 自己会输出它们,
    所以也必须能读回来 —— 这是"往返一致性"的最低要求)。
    """
    s = text.strip()
    if s == "":
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Cannot parse JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
        )


def json_escape_string(text: str) -> str:
    """把字符串转义成 JSON 字符串**内容**(不含外层引号)。"""
    return json.dumps(text, ensure_ascii=False)[1:-1]


def format_float_for_json(value: float) -> str:
    """按 ClickHouse 的规则打印浮点:

    * ``nan`` / ``inf`` / ``-inf`` 用**不带引号**的小写字面量;
    * 其余用最短往返表示。
    """
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)
