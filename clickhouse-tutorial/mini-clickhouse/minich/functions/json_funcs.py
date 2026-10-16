"""JSON 函数 —— ``visitParam*`` 和 ``JSONExtract*`` 是**两种完全不同的东西**。

这是本模块唯一重要的一件事,先把它们摆在一起:

.. code-block:: text

   ┌─ visitParam* / simpleJSON* ──────────────────────────────────┐
   │ 快速、正则式扫描、**不处理转义**、不校验结构完整性           │
   │ 目标:日志解析这类"一行几万条、字段格式可控"的吞吐场景        │
   │                                                              │
   │   visitParamExtractString('{"a":"x\\ny"}', 'a')              │
   │      →  'x\\ny'  ← 注意:反斜杠和 n 是两个字符,没被解码!    │
   └──────────────────────────────────────────────────────────────┘
   ┌─ JSONExtract* ───────────────────────────────────────────────┐
   │ 真正的 JSON 解析、处理转义、支持多级路径                     │
   │ 目标:正确性优先的场景                                        │
   │                                                              │
   │   JSONExtractString('{"a":"x\\ny"}', 'a')                    │
   │      →  'x<换行>y'  ← 转义被正确解码                         │
   └──────────────────────────────────────────────────────────────┘

真实 ClickHouse 的官方文档对这一点说得很直白:``visitParam*`` 是
"if it looks like a duck, it is a duck" —— 它**只**找 ``"name":`` 这串字符
然后把它后面的内容按最简单的方式切出来。字段嵌套、重复键、转义,
它一概不管。所以它能比 ``JSONExtract*`` 快一个数量级,代价是
"看起来能用的输入"和"真的合法的输入"之间的缝隙。

.. warning::

   ``JSONExtract`` 的返回类型取决于**最后一个参数的值**(类型名,
   如 ``JSONExtract(j, 'a', 'UInt64')``),而 ``get_return_type``
   只能看到**类型**。所以 mini-clickhouse 的 ``JSONExtract`` 一律返回
   **字符串**(值的文本形式)。要拿到数值请用 ``JSONExtractUInt`` /
   ``JSONExtractInt`` / ``JSONExtractFloat``,或对结果套 ``toUInt64``。
   真实 ClickHouse 的重载解析器能读到那个常量参数。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .base import FunctionBase, register_function

__all__ = ["navigate_json", "parse_json_path"]

_UINT8 = get_data_type("UInt8")
_UINT64 = get_data_type("UInt64")
_INT64 = get_data_type("Int64")
_FLOAT64 = get_data_type("Float64")
_STRING = get_data_type("String")
_ARRAY_STRING = get_data_type("Array(String)")


class _Missing:
    """路径走不到时的哨兵 —— 用它而不是 ``None``,因为 JSON 里的
    ``null`` 也是一个**存在的值**,两者必须区分开。"""

    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


MISSING = _Missing()


# ══════════════════════════════════════════════════════════════════════════
# visitParam* / simpleJSON* —— 正则式快速扫描
# ══════════════════════════════════════════════════════════════════════════


def _find_field(params: str, name: str) -> Optional[int]:
    """找到 ``"name"`` 后面那个 ``:`` 的位置;找不到返回 ``None``。

    这就是 ``visitParam*`` 的全部"解析":一次字符串查找。
    """
    needle = '"' + name + '"'
    start = 0
    while True:
        idx = params.find(needle, start)
        if idx < 0:
            return None
        after = idx + len(needle)
        j = after
        while j < len(params) and params[j] in " \t\r\n":
            j += 1
        if j < len(params) and params[j] == ":":
            return j + 1
        start = idx + 1


def _visit_raw_value(params: str, pos: int) -> str:
    """从 ``pos`` 开始切出一个"看起来像 JSON 值"的片段(**不处理转义**)。"""
    if pos >= len(params):
        return ""
    ch = params[pos]
    if ch == '"':
        end = params.find('"', pos + 1)
        return params[pos : (len(params) if end < 0 else end + 1)]
    if ch in "[{":
        depth = 0
        i = pos
        while i < len(params):
            c = params[i]
            if c == '"':
                j = params.find('"', i + 1)
                i = len(params) if j < 0 else j + 1
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return params[pos : i + 1]
            i += 1
        return params[pos:]
    end = pos
    while end < len(params) and params[end] not in ",}]":
        end += 1
    return params[pos:end].strip()


def _visit_string(params: str, name: str) -> str:
    """``visitParamExtractString`` 的核心 —— **不做转义解码**。"""
    pos = _find_field(params, name)
    if pos is None:
        return ""
    while pos < len(params) and params[pos] in " \t\r\n":
        pos += 1
    if pos >= len(params) or params[pos] != '"':
        return ""
    end = params.find('"', pos + 1)
    if end < 0:
        return ""
    return params[pos + 1 : end]


def _visit_raw(params: str, name: str) -> str:
    pos = _find_field(params, name)
    if pos is None:
        return ""
    while pos < len(params) and params[pos] in " \t\r\n":
        pos += 1
    return _visit_raw_value(params, pos)


class _VisitParamBase(FunctionBase):
    """``visitParam*`` 的公共壳:第一个参数是 JSON 文本,第二个是字段名。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING


class FunctionVisitParamHas(_VisitParamBase):
    """``visitParamHas(json, name)`` —— 字段是否存在。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 1 if _find_field(str(values[0]), str(values[1])) is not None else 0


class FunctionVisitParamExtractString(_VisitParamBase):
    """``visitParamExtractString(json, name)`` —— 取字符串字段。

    .. warning::

       **不做转义解码**。``\\n`` 会原样保留成两个字符(反斜杠 + n)。
       这是真实 ClickHouse 的行为,也是 ``visitParam*`` 家族"快"的代价。
       需要正确解码请用 ``simpleJSONExtractString`` 或 ``JSONExtractString``。
    """

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _visit_string(str(values[0]), str(values[1]))


class FunctionVisitParamExtractRaw(_VisitParamBase):
    """``visitParamExtractRaw(json, name)`` —— 取**原始**片段(含引号/括号)。"""

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _visit_raw(str(values[0]), str(values[1]))


class _VisitParamNumber(FunctionBase):
    """``visitParamExtractInt`` / ``UInt`` / ``Float`` / ``Bool`` 的公共实现。"""

    kind = "int"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return {"int": _INT64, "uint": _UINT64, "float": _FLOAT64,
                "bool": _UINT8}[self.kind]

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        pos = _find_field(str(values[0]), str(values[1]))
        if pos is None:
            return 0
        raw = _visit_raw_value(str(values[0]), _skip_ws(str(values[0]), pos))
        if self.kind == "bool":
            return 1 if raw.strip().lower() == "true" else 0
        text = raw.strip().strip('"')
        try:
            if self.kind == "float":
                return float(text)
            if self.kind == "int":
                return int(float(text))
            return int(float(text)) & ((1 << 64) - 1)
        except ValueError:
            return 0


def _skip_ws(params: str, pos: int) -> int:
    while pos < len(params) and params[pos] in " \t\r\n":
        pos += 1
    return pos


class FunctionVisitParamExtractInt(_VisitParamNumber):
    kind = "int"


class FunctionVisitParamExtractUInt(_VisitParamNumber):
    kind = "uint"


class FunctionVisitParamExtractFloat(_VisitParamNumber):
    kind = "float"


class FunctionVisitParamExtractBool(_VisitParamNumber):
    kind = "bool"


register_function("visitParamHas", FunctionVisitParamHas, aliases=["visitParamHasCaseInsensitive"],
                  description="JSON 里是否有某字段(快速扫描)")
register_function("visitParamExtractString", FunctionVisitParamExtractString,
                  description="取字符串字段(**不解码转义**)")
register_function("visitParamExtractRaw", FunctionVisitParamExtractRaw,
                  description="取字段的原始 JSON 片段")
register_function("visitParamExtractInt", FunctionVisitParamExtractInt,
                  description="取整数字段")
register_function("visitParamExtractUInt", FunctionVisitParamExtractUInt,
                  description="取无符号整数字段")
register_function("visitParamExtractFloat", FunctionVisitParamExtractFloat,
                  description="取浮点字段")
register_function("visitParamExtractBool", FunctionVisitParamExtractBool,
                  description="取布尔字段")


# ══════════════════════════════════════════════════════════════════════════
# 真正的 JSON 解析
# ══════════════════════════════════════════════════════════════════════════


def _load(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse JSON: {exc}")


def navigate_json(root: Any, path: Sequence[Any]) -> Any:
    """沿路径取值。走不到时返回 :data:`MISSING`。

    路径元素可以是**键名**(字符串)或**数组下标**(整数,0-based)。
    """
    cur = root
    for step in path:
        if isinstance(step, bool):
            return MISSING
        if isinstance(step, int):
            if not isinstance(cur, list) or step < 0 or step >= len(cur):
                return MISSING
            cur = cur[step]
        else:
            key = str(step)
            if not isinstance(cur, dict) or key not in cur:
                return MISSING
            cur = cur[key]
    return cur


def _path_of(values: Sequence[Any]) -> List[Any]:
    return [v for v in values[1:]]


class _JSONExtractBase(FunctionBase):
    """``JSONExtract*`` 的底座 —— 解析 JSON,按路径取值。"""

    def field(self, value: Any) -> Any:
        return value

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        if node is MISSING:
            return self.missing()
        return self.field(node)

    def missing(self) -> Any:
        return ""


class FunctionJSONExtractString(_JSONExtractBase):
    """``JSONExtractString(json, path...)`` —— 取字符串(**会正确解码转义**)。

    值不是字符串时返回空串(与真实 ClickHouse 一致)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def field(self, value: Any) -> Any:
        return value if isinstance(value, str) else ""


class FunctionJSONExtractInt(_JSONExtractBase):
    """``JSONExtractInt(json, path...)`` —— 取整数,类型不符返回 0。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT64

    def field(self, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        return int(value) if isinstance(value, int) else 0

    def missing(self) -> Any:
        return 0


class FunctionJSONExtractUInt(FunctionJSONExtractInt):
    """``JSONExtractUInt(json, path...)`` —— 取无符号整数。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64


class FunctionJSONExtractFloat(_JSONExtractBase):
    """``JSONExtractFloat(json, path...)`` —— 取浮点。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _FLOAT64

    def field(self, value: Any) -> Any:
        if isinstance(value, bool):
            return 0.0
        return float(value) if isinstance(value, (int, float)) else 0.0

    def missing(self) -> Any:
        return 0.0


class FunctionJSONExtractBool(_JSONExtractBase):
    """``JSONExtractBool(json, path...)`` —— 取布尔(0/1)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def field(self, value: Any) -> Any:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return 1 if value else 0
        return 0

    def missing(self) -> Any:
        return 0


class FunctionJSONExtractRaw(_JSONExtractBase):
    """``JSONExtractRaw(json, path...)`` —— 取值的**原始 JSON 文本**。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def field(self, value: Any) -> Any:
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class FunctionJSONExtract(_JSONExtractBase):
    """``JSONExtract(json, path..., [type])``。

    .. warning::

       真实 ClickHouse 会根据最后一个参数(类型名)决定返回类型;
       mini-clickhouse 的 ``get_return_type`` 看不到那个**值**,
       所以这里一律返回**值的文本形式**(String)。
       需要数值请用 ``JSONExtractInt`` / ``JSONExtractUInt`` /
       ``JSONExtractFloat``,或者在外面套一个 ``toUInt64(...)``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        path = list(_path_of(values))
        # 末尾如果是一个"类型名"字符串,它属于返回类型而不是路径
        _KNOWN = {
            "STRING", "INT8", "INT16", "INT32", "INT64", "INT128", "INT256",
            "UINT8", "UINT16", "UINT32", "UINT64", "UINT128", "UINT256",
            "FLOAT32", "FLOAT64", "BOOL", "DATE", "DATETIME", "ARRAY", "TUPLE",
            "MAP", "JSON",
        }
        if path and isinstance(path[-1], str) and path[-1].upper() in _KNOWN:
            path.pop()
        node = navigate_json(root, path)
        if node is MISSING:
            return ""
        if isinstance(node, bool):
            return "true" if node else "false"
        if node is None:
            return "null"
        if isinstance(node, (dict, list)):
            return json.dumps(node, ensure_ascii=False, separators=(",", ":"))
        return str(node)


class FunctionJSONHas(_JSONExtractBase):
    """``JSONHas(json, path...)`` —— 路径是否存在。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        return 0 if navigate_json(root, _path_of(values)) is MISSING else 1


class FunctionJSONLength(_JSONExtractBase):
    """``JSONLength(json[, path...])`` —— 数组元素个数或对象字段个数。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        if node is MISSING:
            return 0
        if isinstance(node, (list, dict)):
            return len(node)
        return 0


class FunctionJSONType(_JSONExtractBase):
    """``JSONType(json[, path...])`` —— ``'String'`` / ``'Int64'`` / ``'Array'`` …。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        if node is MISSING:
            return "None"
        if node is None:
            return "Null"
        if isinstance(node, bool):
            return "Bool"
        if isinstance(node, str):
            return "String"
        if isinstance(node, int):
            return "Int64" if node < 0 else "UInt64"
        if isinstance(node, float):
            return "Float64"
        if isinstance(node, list):
            return "Array"
        return "Object"


class FunctionJSONExtractKeys(_JSONExtractBase):
    """``JSONExtractKeys(json[, path...])`` —— 对象的键数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        return list(node.keys()) if isinstance(node, dict) else []


class FunctionJSONExtractArrayRaw(_JSONExtractBase):
    """``JSONExtractArrayRaw(json[, path...])`` —— 数组每个元素的原始 JSON 文本。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        if not isinstance(node, list):
            return []
        return [json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in node]


class FunctionJSONExtractKeysAndValues(_JSONExtractBase):
    """``JSONExtractKeysAndValues(json, path...)`` —— 对象的键值对数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("Array(Tuple(String, String))")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = navigate_json(root, _path_of(values))
        if not isinstance(node, dict):
            return []
        return [
            (k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                         separators=(",", ":")))
            for k, v in node.items()
        ]


class FunctionIsValidJSON(FunctionBase):
    """``isValidJSON(s)`` —— 是否是合法 JSON。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        try:
            json.loads(str(values[0]))
            return 1
        except (json.JSONDecodeError, ValueError):
            return 0


register_function("JSONExtractString", FunctionJSONExtractString,
                  description="解析 JSON 并按路径取字符串(解码转义)")
register_function("JSONExtractInt", FunctionJSONExtractInt,
                  description="解析 JSON 并按路径取整数")
register_function("JSONExtractUInt", FunctionJSONExtractUInt,
                  description="解析 JSON 并按路径取无符号整数")
register_function("JSONExtractFloat", FunctionJSONExtractFloat,
                  description="解析 JSON 并按路径取浮点")
register_function("JSONExtractBool", FunctionJSONExtractBool,
                  description="解析 JSON 并按路径取布尔")
register_function("JSONExtractRaw", FunctionJSONExtractRaw,
                  description="按路径取值的原始 JSON 文本")
register_function("JSONExtract", FunctionJSONExtract,
                  description="按路径取值(⚠️ 一律返回文本形式)")
register_function("JSONHas", FunctionJSONHas,
                  description="路径是否存在")
register_function("JSONLength", FunctionJSONLength,
                  aliases=["JSON_ARRAY_LENGTH"],
                  description="数组元素个数或对象字段个数")
register_function("JSONType", FunctionJSONType,
                  description="值的 JSON 类型名")
register_function("JSONExtractKeys", FunctionJSONExtractKeys,
                  aliases=["JSONExtractKey"],
                  description="对象的键数组")
register_function("JSONExtractArrayRaw", FunctionJSONExtractArrayRaw,
                  description="数组元素的原始 JSON 文本数组")
register_function("JSONExtractKeysAndValues", FunctionJSONExtractKeysAndValues,
                  description="对象的键值对数组")
register_function("isValidJSON", FunctionIsValidJSON,
                  description="是否是合法 JSON")


# ══════════════════════════════════════════════════════════════════════════
# simpleJSON* —— 与 JSONExtract* 语义相同,只是真实 CH 里走了 SIMD
# ══════════════════════════════════════════════════════════════════════════
#
# 真实 ClickHouse 里 simpleJSON* 是"用 SIMD 指令实现的快速路径",
# 结果与 JSONExtract* **逐位相同**。所以这里直接复用同一批类,只是换个名字
# 注册 —— 既不重复实现,也不会出现"两条路径结果不一样"的诡异 bug。

register_function("simpleJSONExtractString", FunctionJSONExtractString,
                  description="simpleJSON 版的取字符串")
register_function("simpleJSONExtractInt", FunctionJSONExtractInt,
                  description="simpleJSON 版的取整数")
register_function("simpleJSONExtractUInt", FunctionJSONExtractUInt,
                  description="simpleJSON 版的取无符号整数")
register_function("simpleJSONExtractFloat", FunctionJSONExtractFloat,
                  description="simpleJSON 版的取浮点")
register_function("simpleJSONExtractBool", FunctionJSONExtractBool,
                  description="simpleJSON 版的取布尔")
register_function("simpleJSONExtractRaw", FunctionJSONExtractRaw,
                  description="simpleJSON 版的取原始文本")
register_function("simpleJSONHas", FunctionJSONHas,
                  description="simpleJSON 版的路径存在性判断")


# ══════════════════════════════════════════════════════════════════════════
# JSONPath 风格(github.com/... 那套 $ 语法)
# ══════════════════════════════════════════════════════════════════════════


def parse_json_path(path: str) -> List[Any]:
    """把一个 JSONPath 拆成"键 / 下标"序列。

    支持一个**够用的小子集**:``$.a.b``、``$.a[0]``、``$['a']``、
    ``$.a[*]``(星号表示"数组整体")。真实 ClickHouse 走的是完整的
    JSONPath 实现(带过滤表达式),mini-clickhouse 不做那部分。
    """
    s = path.strip()
    if s.startswith("$"):
        s = s[1:]
    out: List[Any] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == ".":
            i += 1
            start = i
            while i < len(s) and s[i] not in ".[":
                i += 1
            if i > start:
                out.append(s[start:i])
            continue
        if ch == "[":
            end = s.find("]", i)
            if end < 0:
                break
            token = s[i + 1 : end].strip()
            if token.startswith("'") and token.endswith("'"):
                token = token[1:-1]
            elif token.startswith('"') and token.endswith('"'):
                token = token[1:-1]
            if token == "*":
                out.append("*")
            else:
                try:
                    out.append(int(token))
                except ValueError:
                    out.append(token)
            i = end + 1
            continue
        i += 1
    return out


def _jsonpath_navigate(root: Any, path: List[Any]) -> Any:
    cur = root
    for step in path:
        if step == "*":
            continue
        if isinstance(step, int):
            if not isinstance(cur, list) or step >= len(cur) or step < -len(cur):
                return MISSING
            cur = cur[step]
        else:
            if not isinstance(cur, dict) or step not in cur:
                return MISSING
            cur = cur[step]
    return cur


class _JSONPathFunction(FunctionBase):
    """``JSON_QUERY`` / ``JSON_VALUE`` / ``JSON_EXISTS`` 的底座。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        node = _jsonpath_navigate(root, parse_json_path(str(values[1])))
        if node is MISSING:
            return self.on_missing()
        return self.result(node)

    def on_missing(self) -> Any:
        return ""

    def result(self, node: Any) -> Any:
        return node


class FunctionJSONQuery(_JSONPathFunction):
    """``JSON_QUERY(json, path)`` —— JSONPath 取值,返回**原始 JSON**,取不到返回 NULL。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("Nullable(String)")

    def on_missing(self) -> Any:
        return None

    def result(self, node: Any) -> Any:
        if isinstance(node, (dict, list)):
            return json.dumps(node, ensure_ascii=False, separators=(",", ":"))
        return json.dumps(node, ensure_ascii=False)


class FunctionJSONValue(_JSONPathFunction):
    """``JSON_VALUE(json, path)`` —— JSONPath 取值,返回**标量文本**,取不到返回 NULL。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("Nullable(String)")

    def on_missing(self) -> Any:
        return None

    def result(self, node: Any) -> Any:
        if node is None:
            return None
        if isinstance(node, bool):
            return "true" if node else "false"
        if isinstance(node, (dict, list)):
            return json.dumps(node, ensure_ascii=False, separators=(",", ":"))
        return str(node)


class FunctionJSONExists(_JSONPathFunction):
    """``JSON_EXISTS(json, path)`` —— 路径是否存在。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        root = _load(str(values[0]))
        return 0 if _jsonpath_navigate(root, parse_json_path(str(values[1]))) is MISSING else 1


register_function("JSON_QUERY", FunctionJSONQuery,
                  description="JSONPath 取值,返回原始 JSON")
register_function("JSON_VALUE", FunctionJSONValue,
                  description="JSONPath 取值,返回标量文本")
register_function("JSON_EXISTS", FunctionJSONExists,
                  description="JSONPath 路径是否存在")
