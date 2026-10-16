"""字符串函数 —— 每一个都必须在"**字节**"和"**字符**"之间选边站。

ClickHouse 的 ``String`` 是**二进制安全的**:它不校验编码,内部就是一串
字节。由此产生了一条几乎所有从 MySQL/Python 迁过来的人都会踩的规则:

.. code-block:: text

   length('你好')     = 6     ← 字节数!
   lengthUTF8('你好') = 2     ← 码点数

   同样地,大小写转换也分成两族:
   lower/upper       —— 只动 ASCII(A-Z / a-z),非 ASCII 字节原样保留
   lowerUTF8/upperUTF8 —— 走真正的 Unicode 大小写映射

.. code-block:: sql

   SELECT lower('Ä'), lowerUTF8('Ä');
   -- 'Ä'      'ä'

   SELECT upper('ß'), upperUTF8('ß');
   -- 'ß'      'SS'      ← 注意 upperUTF8 会把 ß 展开成两个字符

**这条区分是刻意做出来的,不是实现漏洞。** 为什么? 因为 ``lower`` 在
真实 ClickHouse 里是"逐字节的、常量时间的"操作,它被用在正则预处理、
索引比较这类**必须极快且不能改变字节长度**的地方 —— 如果它去查 Unicode
大小写表,长度就可能变(``'ß' → 'SS'``),下游依赖"长度不变"的代码就崩了。

.. warning::

   正则相关的函数(``match`` / ``replaceRegexp*`` / ``extract*`` /
   ``splitByRegexp``)在真实 ClickHouse 里走的是 **RE2**(线性时间、
   不支持反向引用、不支持环视)。mini-clickhouse 用的是 Python 的 ``re``
   —— 因此 **``\\1`` 反向引用、``(?=...)`` 环视、``(?P<name>...)``
   命名组在我们这里能跑,而在真实 ClickHouse 里会报错**。反过来,
   RE2 支持的超大重复次数 ``a{100000}`` 在 Python 里可能极慢。
   第 15 章把这个差异单独列了一节。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional, Sequence

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .base import FunctionBase, register_function

__all__ = [
    "ascii_upper",
    "ascii_lower",
    "like_to_regex",
    "format_readable_size",
    "format_readable_quantity",
    "format_readable_time_delta",
    "format_readable_decimal_size",
]


# ══════════════════════════════════════════════════════════════════════════
# 共用小工具
# ══════════════════════════════════════════════════════════════════════════

#: 复用的类型对象 —— 类型是不可变的,没必要每次构造一个新的
_UINT8 = get_data_type("UInt8")
_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")
_STRING = get_data_type("String")

#: 只包含 ASCII 的大小写映射表 —— 这就是 lower/upper 与 lowerUTF8/upperUTF8
#: 的分水岭。``str.translate`` 走的是 C 层,比逐字符判断快得多。
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def ascii_lower(text: str) -> str:
    """**只**把 ASCII 的大写字母转小写(等价于真实 CH 的 ``lower``)。"""
    return text.translate(_ASCII_LOWER)


def ascii_upper(text: str) -> str:
    """**只**把 ASCII 的小写字母转大写(等价于真实 CH 的 ``upper``)。"""
    return text.translate(_ASCII_UPPER)


def _as_str(value: Any) -> str:
    """把一个列值规范成 ``str``(列里取出来的已经是 str,这里只是兜底)。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "surrogateescape")
    return str(value)


def _as_bytes(value: Any) -> bytes:
    """把值转成**原始字节** —— 所有"按字节数数"的函数都用它。"""
    return _as_str(value).encode("utf-8", "surrogateescape")


def compile_regex(pattern: str) -> "re.Pattern[str]":
    """编译一个正则,失败时抛出**带上下文的**错误。

    .. note::

       真实 ClickHouse 在这里抛 ``CANNOT_COMPILE_REGULAR_EXPRESSION``。
       mini-clickhouse 复用了 ``ILLEGAL_TYPE_OF_ARGUMENT`` —— 因为错误码
       表里没有那一个,而"宁可给出一个有据可查的编号,也不要临时造一个"。
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Cannot compile regular expression {pattern!r}: {exc} "
            f"(note: mini-clickhouse uses Python's re, not RE2)",
        )


def like_to_regex(pattern: str, case_insensitive: bool = False) -> "re.Pattern[str]":
    """把 SQL ``LIKE`` 的通配符翻译成正则。

    规则(**必须做对转义**,否则 ``like(s, 'a.b')`` 会去匹配 ``axb``):

    .. code-block:: text

       模式里的字符       正则里的含义
       --------------    -------------------------
       %                  .*        (任意长度,含换行)
       _                  .         (任意一个字符)
       \\x                x 的字面量(反斜杠转义下一个字符)
       其余字符           re.escape 之后按字面量匹配

    因为 ``%`` 在 ClickHouse 里能跨行匹配,翻译后的正则开了 ``re.DOTALL``
    —— 这是一个容易被忽略的细节:``like('a\\nb', 'a%b')`` 是 **1**。
    """
    out: List[str] = ["(?s)^"]
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    expr = "".join(out)
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(expr, flags)


# ══════════════════════════════════════════════════════════════════════════
# 注册辅助 —— 把"一个名字 + 一个 Python 函数"变成一整个函数类
# ══════════════════════════════════════════════════════════════════════════


def _register(
    name: str,
    impl: Any,
    return_type: DataType,
    *,
    aliases: Sequence[str] = (),
    description: str = "",
    propagate_nulls: bool = True,
) -> type:
    """注册一个"逐行执行"的标量函数。

    真实 ClickHouse 里这些函数各自是一个 ``IFunction`` 子类;在这里它们
    共享同一个壳子,差别只在 ``impl`` 与 ``get_return_type``。这不是偷懒 ——
    **"函数"和"怎么注册一个函数"本来就该解耦**,第 15 章会讨论真实
    ClickHouse 为什么用注册表 + 工厂而不是一堆硬编码的 ``if``。
    """

    class _Generated(FunctionBase):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return return_type

        def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
            return impl(values)

    _Generated.__name__ = "Function" + name[:1].upper() + name[1:]
    _Generated.propagate_nulls = propagate_nulls  # type: ignore[assignment]
    register_function(name, _Generated, aliases=list(aliases), description=description)
    return _Generated


#: 常用的"单字符串参数"适配器
def _unary_str(fn: Any) -> Any:
    return lambda values: fn(_as_str(values[0]))


# ══════════════════════════════════════════════════════════════════════════
# 长度与"空"
# ══════════════════════════════════════════════════════════════════════════

_register(
    "length", _unary_str(lambda s: len(_as_bytes(s))), _UINT64,
    aliases=["lengthBytes"],
    description="字符串的**字节**长度(String 是二进制安全的)",
)
_register(
    "lengthUTF8", _unary_str(len), _UINT64,
    aliases=["char_length", "character_length"],
    description="字符串的 **Unicode 码点**个数",
)
_register(
    "empty", _unary_str(lambda s: 1 if len(s) == 0 else 0), _UINT8,
    description="字符串是否为空(空串为 1)",
)
_register(
    "notEmpty", _unary_str(lambda s: 0 if len(s) == 0 else 1), _UINT8,
    description="字符串是否非空",
)


# ══════════════════════════════════════════════════════════════════════════
# 大小写
# ══════════════════════════════════════════════════════════════════════════

_register(
    "lower", _unary_str(ascii_lower), _STRING,
    aliases=["lcase"],
    description="**只**把 ASCII 大写字母转小写,非 ASCII 字节原样保留",
)
_register(
    "upper", _unary_str(ascii_upper), _STRING,
    aliases=["ucase"],
    description="**只**把 ASCII 小写字母转大写,非 ASCII 字节原样保留",
)
_register(
    "lowerUTF8", _unary_str(lambda s: s.lower()), _STRING,
    description="按 Unicode 规则转小写(可能改变字节长度)",
)
_register(
    "upperUTF8", _unary_str(lambda s: s.upper()), _STRING,
    description="按 Unicode 规则转大写(如 'ß' -> 'SS')",
)


# ══════════════════════════════════════════════════════════════════════════
# 反转
# ══════════════════════════════════════════════════════════════════════════

def _reverse_bytes(s: str) -> str:
    return _as_bytes(s)[::-1].decode("utf-8", "surrogateescape")


_register(
    "reverse", _unary_str(_reverse_bytes), _STRING,
    description="按**字节**反转字符串",
)
_register(
    "reverseUTF8", _unary_str(lambda s: s[::-1]), _STRING,
    description="按 **Unicode 码点**反转字符串",
)


# ══════════════════════════════════════════════════════════════════════════
# 拼接
# ══════════════════════════════════════════════════════════════════════════


class FunctionConcat(FunctionBase):
    """``concat(s1, s2, ...)`` —— 变参拼接,不跳过 NULL。

    .. warning::

       真实 ClickHouse 的 ``concat`` 遇到 NULL 会**返回 NULL**(整行),
       而不是跳过它。mini-clickhouse 交给默认的 NULL 传播处理 ——
       效果一致:任一参数为 NULL,结果就是 NULL。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return "".join(_as_str(v) for v in values)


register_function(
    "concat", FunctionConcat,
    description="按顺序拼接所有字符串参数",
)


class FunctionConcatWithSeparator(FunctionBase):
    """``concatWithSeparator(sep, s1, s2, ...)`` —— 用分隔符拼接。

    第一个参数是分隔符 —— 这一点和多数人的直觉相反(以为分隔符在最后)。
    真实 ClickHouse 的签名就是 ``concatWithSeparator(separator, arg1, arg2, ...)``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        if len(values) < 1:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "concatWithSeparator requires at least one argument",
            )
        sep = _as_str(values[0])
        return sep.join(_as_str(v) for v in values[1:])


register_function(
    "concatWithSeparator", FunctionConcatWithSeparator,
    aliases=["concat_ws"],
    description="用第一个参数作分隔符拼接其余字符串",
)


# ══════════════════════════════════════════════════════════════════════════
# 取子串 —— 1-based,负偏移从尾部数
# ══════════════════════════════════════════════════════════════════════════


def _substring_pos(offset: int, size: int) -> int:
    """把 ClickHouse 的 ``offset`` 规整成 1-based 的起始位置(0 表示"取不到")。

    .. code-block:: text

       offset > 0    → 直接就是 1-based 位置
       offset == 0   → 0(结果恒为空串)
       offset < 0    → size + offset + 1(所以 -1 是最后一个字符)

    这三条规则是 ``substring`` 系列**唯一**容易写错的地方,单独抽出来
    是为了让它只写一遍。
    """
    if offset < 0:
        return size + offset + 1
    if offset == 0:
        return 0
    return offset


def _substring_impl(values: Sequence[Any], *, utf8: bool) -> str:
    s = _as_str(values[0])
    if utf8:
        units: Any = s
    else:
        units = _as_bytes(s)
    size = len(units)
    offset = int(values[1])
    pos = _substring_pos(offset, size)
    if pos < 1 or pos > size:
        return "" if not utf8 else ""
    start = pos - 1
    if len(values) >= 3:
        length = int(values[2])
        if length < 0:
            # 真实 ClickHouse 里负的 length 会被当成 0(返回空串)。
            length = 0
    else:
        length = size - start
    if start + length > size:
        length = size - start
    piece = units[start : start + length]
    return piece if utf8 else piece.decode("utf-8", "surrogateescape")


class FunctionSubstring(FunctionBase):
    """``substring(s, offset[, length])`` —— 别名 ``substr`` / ``mid``。

    **1-based**;``offset`` 为负时从尾部往前数;``offset = 0`` 返回空串。
    超过末尾的 ``length`` 会被截到串尾(不报错)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        if len(values) < 2 or len(values) > 3:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                "substring expects 2 or 3 arguments (s, offset[, length])",
            )
        return _substring_impl(values, utf8=False)


class FunctionSubstringUTF8(FunctionBase):
    """``substringUTF8(s, offset[, length])`` —— 按**码点**而不是字节取子串。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _substring_impl(values, utf8=True)


register_function(
    "substring", FunctionSubstring,
    aliases=["substr", "mid"],
    description="按字节取子串(1-based,负偏移从尾部数,0 返回空串)",
)
register_function(
    "substringUTF8", FunctionSubstringUTF8,
    description="按 Unicode 码点取子串",
)


_register(
    "left", lambda v: _as_bytes(_as_str(v[0]))[: max(0, int(v[1]))].decode("utf-8", "surrogateescape"),
    _STRING, description="取字符串的前 n 个字节",
)
_register(
    "right",
    lambda v: _as_bytes(_as_str(v[0]))[
        max(0, len(_as_bytes(_as_str(v[0]))) - max(0, int(v[1]))) :
    ].decode("utf-8", "surrogateescape"),
    _STRING, description="取字符串的后 n 个字节",
)


# ══════════════════════════════════════════════════════════════════════════
# 去空白
# ══════════════════════════════════════════════════════════════════════════

_WS = " \t\n\r\f\v"

_register("trim", _unary_str(lambda s: s.strip(_WS)), _STRING,
          aliases=["trimBoth"], description="去掉两端的空白字符")
_register("trimLeft", _unary_str(lambda s: s.lstrip(_WS)), _STRING,
          aliases=["ltrim"], description="去掉左端的空白字符")
_register("trimRight", _unary_str(lambda s: s.rstrip(_WS)), _STRING,
          aliases=["rtrim"], description="去掉右端的空白字符")


# ══════════════════════════════════════════════════════════════════════════
# 前后缀与查找
# ══════════════════════════════════════════════════════════════════════════

_register("startsWith", lambda v: 1 if _as_str(v[0]).startswith(_as_str(v[1])) else 0,
          _UINT8, description="字符串是否以给定前缀开头")
_register("endsWith", lambda v: 1 if _as_str(v[0]).endswith(_as_str(v[1])) else 0,
          _UINT8, description="字符串是否以给定后缀结尾")


def _position_bytes(haystack: str, needle: str) -> int:
    if needle == "":
        return 1  # 空串在所有位置都匹配,ClickHouse 返回 1
    idx = _as_bytes(haystack).find(_as_bytes(needle))
    return 0 if idx < 0 else idx + 1


class FunctionPosition(FunctionBase):
    """``position(haystack, needle)`` —— 别名 ``locate`` / ``instr``。

    返回 **1-based 的字节位置**;找不到返回 **0**(注意不是 -1)。
    空 needle 返回 1(与真实 ClickHouse 一致)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _position_bytes(_as_str(values[0]), _as_str(values[1]))


register_function("position", FunctionPosition, aliases=["locate", "instr"],
                  description="子串第一次出现的 1-based 字节位置(0 = 未找到)")


def _position_utf8(haystack: str, needle: str) -> int:
    if needle == "":
        return 1
    idx = haystack.find(needle)
    return 0 if idx < 0 else idx + 1


class FunctionPositionCaseInsensitive(FunctionBase):
    """``positionCaseInsensitive`` —— ASCII 大小写不敏感的 ``position``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _position_bytes(
            ascii_lower(_as_str(values[0])), ascii_lower(_as_str(values[1]))
        )


_register("positionUTF8", lambda v: _position_utf8(_as_str(v[0]), _as_str(v[1])),
          _UINT64, description="子串第一次出现的 1-based 码点位置(0 = 未找到)")
register_function("positionCaseInsensitive", FunctionPositionCaseInsensitive,
                  description="ASCII 大小写不敏感的 position")


# ══════════════════════════════════════════════════════════════════════════
# 多模式搜索
# ══════════════════════════════════════════════════════════════════════════


class FunctionMultiSearchAny(FunctionBase):
    """``multiSearchAny(haystack, needles_array)`` —— 任一命中即为 1。

    比"写多个 OR 的 like"快得多:真实 ClickHouse 会为这组模式建一个
    **Aho–Corasick 自动机**,一趟扫完。mini-clickhouse 只是逐个 ``in`` ——
    语义对,性能不对,这一点在教程里标注。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        haystack = _as_bytes(_as_str(values[0]))
        for needle in values[1] or []:
            if _as_bytes(_as_str(needle)) in haystack:
                return 1
        return 0


class FunctionMultiSearchFirstPosition(FunctionBase):
    """``multiSearchFirstPosition(haystack, needles_array)`` —— 最早的命中位置。

    返回 **1-based 字节位置**,全部未命中返回 **0**。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        haystack = _as_bytes(_as_str(values[0]))
        best = 0
        for needle in values[1] or []:
            idx = haystack.find(_as_bytes(_as_str(needle)))
            if idx >= 0 and (best == 0 or idx + 1 < best):
                best = idx + 1
        return best


register_function("multiSearchAny", FunctionMultiSearchAny,
                  description="haystack 是否包含 needles 数组里的任意一个")
register_function("multiSearchFirstPosition", FunctionMultiSearchFirstPosition,
                  description="needles 数组里最早命中的 1-based 位置(0 = 都没有)")


# ══════════════════════════════════════════════════════════════════════════
# 正则与 LIKE
# ══════════════════════════════════════════════════════════════════════════


class FunctionMatch(FunctionBase):
    """``match(s, pattern)`` —— 正则搜索,命中返回 1。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        rx = compile_regex(_as_str(values[1]))
        return 1 if rx.search(_as_str(values[0])) else 0


class FunctionLike(FunctionBase):
    """``like(s, pattern)`` —— SQL 通配符 ``%`` / ``_``。"""

    case_insensitive = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        if self.case_insensitive:
            s = ascii_lower(s)
        rx = like_to_regex(_as_str(values[1]), self.case_insensitive)
        return 1 if rx.match(s) else 0


class FunctionNotLike(FunctionLike):
    """``notLike(s, pattern)`` —— ``like`` 取反。"""

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 0 if super().execute_row(values, context) else 1


class FunctionILike(FunctionLike):
    """``ilike(s, pattern)`` —— ASCII 大小写不敏感的 LIKE。

    .. note::

       真实 ClickHouse 的 ``ilike`` 只折叠 **ASCII** 大小写;
       mini-clickhouse 用同样的 ``ascii_lower`` 折叠,行为一致。
    """

    case_insensitive = True


register_function("match", FunctionMatch,
                  description="正则搜索(RE2 语义;mini-clickhouse 用 Python re)")
register_function("like", FunctionLike,
                  description="SQL LIKE:'%' 任意串、'_' 任意单字符")
register_function("notLike", FunctionNotLike,
                  description="LIKE 取反")
register_function("ilike", FunctionILike,
                  description="ASCII 大小写不敏感的 LIKE")


# ══════════════════════════════════════════════════════════════════════════
# 计数与替换
# ══════════════════════════════════════════════════════════════════════════

_register("countMatches", lambda v: len(compile_regex(_as_str(v[1])).findall(_as_str(v[0]))),
          _UINT64, description="正则命中的次数(不重叠)")


def _count_substrings(haystack: str, needle: str) -> int:
    if needle == "":
        return len(haystack) + 1
    return haystack.count(needle)


_register("countSubstrings", lambda v: _count_substrings(_as_str(v[0]), _as_str(v[1])),
          _UINT64, description="子串出现的次数(不重叠)")
_register(
    "countSubstringsCaseInsensitive",
    lambda v: _count_substrings(ascii_lower(_as_str(v[0])), ascii_lower(_as_str(v[1]))),
    _UINT64,
    description="ASCII 大小写不敏感的子串计数",
)


class FunctionReplace(FunctionBase):
    """字面量替换 —— ``replaceOne`` 只换第一处,``replaceAll`` 换全部。"""

    all_occurrences = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        src = _as_str(values[1])
        dst = _as_str(values[2])
        if self.all_occurrences:
            return s.replace(src, dst)
        return s.replace(src, dst, 1)


class FunctionReplaceOne(FunctionReplace):
    all_occurrences = False


class FunctionReplaceRegexp(FunctionBase):
    """正则替换 —— ``replaceRegexpOne`` 只换第一处,``replaceRegexpAll`` 换全部。

    .. warning::

       替换串里的 ``\\1`` 在 ClickHouse(RE2)里是**组 1 的引用**,
       在 Python 的 ``re.sub`` 里同样是组引用 —— 这一条两边恰好一致。
       但 ``\\0``(整段匹配)在 RE2 里合法、在 Python 里要用 ``\\g<0>``,
       所以我们在传下去之前先把 ``\\0`` 改写成 ``\\g<0>``。
    """

    all_occurrences = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        rx = compile_regex(_as_str(values[1]))
        repl = _as_str(values[2]).replace("\\0", "\\g<0>")
        try:
            if self.all_occurrences:
                return rx.sub(repl, s)
            return rx.sub(repl, s, count=1)
        except re.error as exc:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Invalid replacement {repl!r} for pattern {values[1]!r}: {exc}",
            )


class FunctionReplaceRegexpOne(FunctionReplaceRegexp):
    all_occurrences = False


register_function("replaceOne", FunctionReplaceOne,
                  description="把第一次出现的子串换掉")
register_function("replaceAll", FunctionReplace,
                  description="把所有出现的子串换掉")
register_function("replaceRegexpOne", FunctionReplaceRegexpOne,
                  description="正则替换第一次匹配")
register_function("replaceRegexpAll", FunctionReplaceRegexp,
                  description="正则替换所有匹配")


# ══════════════════════════════════════════════════════════════════════════
# 提取
# ══════════════════════════════════════════════════════════════════════════


class FunctionRegexpExtract(FunctionBase):
    """``regexpExtract(haystack, pattern[, index])`` —— 取某个捕获组。

    ``index`` 默认 **0**(整段匹配)。没有匹配时返回**空串**(不是 NULL)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        rx = compile_regex(_as_str(values[1]))
        index = int(values[2]) if len(values) >= 3 else 0
        m = rx.search(s)
        if not m:
            return ""
        if index < 0 or index > m.re.groups:
            # 组号越界:真实 ClickHouse 返回空串而不是报错
            return ""
        return m.group(index)


class FunctionExtract(FunctionBase):
    """``extract(haystack, pattern)`` —— 有捕获组时返回**组 1**,否则整段匹配。

    这是 ``extract`` 与 ``regexpExtract(haystack, pattern, 0)`` 的唯一区别,
    也是它最容易和别名混淆的原因 —— 真实 ClickHouse 的文档里写得很含糊,
    我们用"组 1 优先"实现,并在教程里明确。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        rx = compile_regex(_as_str(values[1]))
        m = rx.search(s)
        if not m:
            return ""
        return m.group(1) if m.re.groups >= 1 else m.group(0)


class FunctionExtractAll(FunctionBase):
    """``extractAll(haystack, pattern)`` —— 所有匹配(整段)组成的数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("Array(String)")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        rx = compile_regex(_as_str(values[1]))
        return [m.group(0) for m in rx.finditer(_as_str(values[0]))]


register_function("regexpExtract", FunctionRegexpExtract,
                  description="正则取第 index 个捕获组(默认 0 = 整段匹配)")
register_function("extract", FunctionExtract,
                  description="正则取匹配(有捕获组时取组 1)")
register_function("extractAll", FunctionExtractAll,
                  description="正则所有匹配组成的数组")


# ══════════════════════════════════════════════════════════════════════════
# 切分
# ══════════════════════════════════════════════════════════════════════════

_ARRAY_STRING = get_data_type("Array(String)")


class FunctionSplitByChar(FunctionBase):
    """``splitByChar(separator, s)`` —— **分隔符在第一个参数**。

    空串输入返回**空数组**(不是 ``['']``)—— 这是真实 ClickHouse 的行为,
    也是最常被误解的一条(很多人以为和 Python 的 ``str.split`` 一样)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        sep = _as_str(values[0])
        s = _as_str(values[1])
        if len(sep) != 1:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"splitByChar requires a single-character separator, got {sep!r}",
            )
        if s == "":
            return []
        return s.split(sep)


class FunctionSplitByString(FunctionBase):
    """``splitByString(separator, s)`` —— 多字节分隔符。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        sep = _as_str(values[0])
        s = _as_str(values[1])
        if s == "":
            return []
        if sep == "":
            # 空分隔符:真实 ClickHouse 返回整个串(一个元素),不是逐字符切
            return [s]
        return s.split(sep)


class FunctionSplitByRegexp(FunctionBase):
    """``splitByRegexp(regexp, s)`` —— 用正则切分。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        pattern = _as_str(values[0])
        s = _as_str(values[1])
        if s == "":
            return []
        if pattern == "":
            # 空正则 → 空数组(与真实 CH 一致,而不是逐字符切)
            return []
        return compile_regex(pattern).split(s)


class FunctionSplitByWhitespace(FunctionBase):
    """``splitByWhitespace(s)`` —— 按**连续**空白切分,丢弃空片段。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        return re.split(r"[ \t\n\r\f\v]+", s.strip(_WS)) if s.strip(_WS) else []


class FunctionSplitByNonAlpha(FunctionBase):
    """``splitByNonAlpha(s)`` —— 按**连续**非字母数字切分。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        return [p for p in re.split(r"[^0-9A-Za-z]+", s) if p]


register_function("splitByChar", FunctionSplitByChar,
                  description="按单字符分隔符切分(空串 -> 空数组)")
register_function("splitByString", FunctionSplitByString,
                  description="按多字符分隔符切分")
register_function("splitByRegexp", FunctionSplitByRegexp,
                  description="按正则切分")
register_function("splitByWhitespace", FunctionSplitByWhitespace,
                  description="按连续空白切分")
register_function("splitByNonAlpha", FunctionSplitByNonAlpha,
                  description="按连续非字母数字切分")


# ══════════════════════════════════════════════════════════════════════════
# 数组拼成字符串 / 格式化
# ══════════════════════════════════════════════════════════════════════════


class FunctionArrayStringConcat(FunctionBase):
    """``arrayStringConcat(arr[, sep])`` —— 把数组元素拼成一个字符串。

    默认分隔符是**空串**(不是逗号)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        sep = _as_str(values[1]) if len(values) >= 2 else ""
        return sep.join(_as_str(v) for v in (values[0] or []))


register_function("arrayStringConcat", FunctionArrayStringConcat,
                  description="数组元素拼接为字符串(默认无分隔符)")


class FunctionFormat(FunctionBase):
    """``format(pattern, ...)`` —— 花括号占位符格式化。

    支持 ClickHouse 文档里的那几种写法:

    .. code-block:: text

       {}        自动编号(第 0 个、第 1 个……)
       {0} {1}   显式编号
       {{ }}     字面量花括号

    .. warning::

       真实 ClickHouse 的 ``format`` 还支持 ``{name}`` 具名占位与
       ``!`` 转义,mini-clickhouse **没实现具名占位**。遇到 ``{name}``
       会抛 ``NOT_IMPLEMENTED``,而不是静默输出原样 —— 静默会让
       "格式化没生效"变成一个很难查的 bug。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        pattern = _as_str(values[0])
        rest = _as_str_many(values[1:])
        out: List[str] = []
        i = 0
        auto = 0
        n = len(pattern)
        while i < n:
            ch = pattern[i]
            if ch == "{":
                if i + 1 < n and pattern[i + 1] == "{":
                    out.append("{")
                    i += 2
                    continue
                end = pattern.find("}", i + 1)
                if end < 0:
                    raise_error(
                        ErrorCode.SYNTAX_ERROR,
                        f"Unmatched '{{' in format pattern {pattern!r}",
                    )
                spec = pattern[i + 1 : end]
                if spec == "":
                    idx = auto
                    auto += 1
                else:
                    try:
                        idx = int(spec)
                    except ValueError:
                        raise_error(
                            ErrorCode.NOT_IMPLEMENTED,
                            f"mini-clickhouse's format() does not support the "
                            f"named placeholder {{{spec}}}; use positional {{0}}",
                        )
                if idx < 0 or idx >= len(rest):
                    raise_error(
                        ErrorCode.ARGUMENT_OUT_OF_BOUND,
                        f"format() placeholder {{{idx}}} has no matching argument "
                        f"(got {len(rest)})",
                    )
                out.append(rest[idx])
                i = end + 1
                continue
            if ch == "}":
                if i + 1 < n and pattern[i + 1] == "}":
                    out.append("}")
                    i += 2
                    continue
                out.append("}")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)


def _as_str_many(values: Sequence[Any]) -> List[str]:
    return [_as_str(v) for v in values]


register_function("format", FunctionFormat,
                  description="按 {} / {0} 占位符格式化字符串")


# ══════════════════════════════════════════════════════════════════════════
# 重复 / 补白
# ══════════════════════════════════════════════════════════════════════════

_register("repeat", lambda v: _as_str(v[0]) * max(0, int(v[1])), _STRING,
          description="把字符串重复 n 次")
_register("space", lambda v: " " * max(0, int(v[0])), _STRING,
          description="由 n 个空格组成的字符串")


class FunctionPad(FunctionBase):
    """``leftPad`` / ``rightPad`` —— 补到指定**字节**长度。"""

    to_left = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_bytes(_as_str(values[0]))
        length = int(values[1])
        pad = _as_bytes(_as_str(values[2])) if len(values) >= 3 else b" "
        if length <= len(s):
            # 超长时**从右侧截断**(与真实 CH 一致)
            return s[:length].decode("utf-8", "surrogateescape")
        if not pad:
            return s.decode("utf-8", "surrogateescape")
        need = length - len(s)
        filler = (pad * (need // len(pad) + 1))[:need]
        out = filler + s if self.to_left else s + filler
        return out.decode("utf-8", "surrogateescape")


class FunctionLeftPad(FunctionPad):
    to_left = True


class FunctionRightPad(FunctionPad):
    to_left = False


register_function("leftPad", FunctionLeftPad, aliases=["lpad"],
                  description="在左侧补字符直到指定长度")
register_function("rightPad", FunctionRightPad, aliases=["rpad"],
                  description="在右侧补字符直到指定长度")


# ══════════════════════════════════════════════════════════════════════════
# 码点 / 编码相关的小函数
# ══════════════════════════════════════════════════════════════════════════


class FunctionChar(FunctionBase):
    """``char(n)`` —— 码点 n 对应的字符(返回 String)。

    与 ``char`` 在别的数据库里"返回 ASCII 字符"不同,ClickHouse 的
    ``char`` 是 **Unicode 码点**语义:``char(0x4f60)`` 是 ``'你'``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        code = int(values[0])
        try:
            return chr(code)
        except ValueError:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"char({code}): not a valid Unicode code point",
            )


class FunctionAscii(FunctionBase):
    """``ascii(s)`` —— 首字符的 Unicode 码点,空串返回 0。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT32

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        return ord(s[0]) if s else 0


register_function("char", FunctionChar,
                  description="Unicode 码点对应的字符")
register_function("ascii", FunctionAscii,
                  description="首字符的 Unicode 码点(空串为 0)")


_register("isValidUTF8",
          _unary_str(lambda s: 1 if _is_valid_utf8(s) else 0),
          _UINT8, description="字符串是否是合法的 UTF-8")
_register("toValidUTF8",
          _unary_str(lambda s: _as_bytes(s).decode("utf-8", "replace")),
          _STRING, description="把非法 UTF-8 字节替换成 U+FFFD")
_register("isASCII",
          _unary_str(lambda s: 1 if all(b < 128 for b in _as_bytes(s)) else 0),
          _UINT8, description="字符串是否只含 ASCII 字节")


def _is_valid_utf8(s: str) -> bool:
    """判定一个 ``String`` 列的原始字节是否是合法 UTF-8。

    注意 ``surrogateescape`` 的作用:非法字节在解码时变成 U+DC80..U+DCFF
    的代理码点,再编码回去**恰好还原成原字节** —— 所以我们能无损地
    拿到"原始字节"再判定。
    """
    try:
        _as_bytes(s).decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class FunctionNormalizeUTF8(FunctionBase):
    """``normalizeUTF8(form, s)`` —— Unicode 规范化。

    ``form`` 取 ``NFC`` / ``NFD`` / ``NFKC`` / ``NFKD`` 之一。
    真实 ClickHouse 的常量折叠会在**查询分析期**就把常量串规范化掉,
    所以``SELECT normalizeUTF8('NFC', 'a')`` 在 EXPLAIN 里看不到这个函数;
    mini-clickhouse 不做这层折叠。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        form = _as_str(values[0]).upper()
        if form not in ("NFC", "NFD", "NFKC", "NFKD"):
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"normalizeUTF8: unknown normalization form {values[0]!r}; "
                f"expected NFC/NFD/NFKC/NFKD",
            )
        return unicodedata.normalize(form, _as_str(values[1]))


register_function("normalizeUTF8", FunctionNormalizeUTF8,
                  description="Unicode 规范化(NFC/NFD/NFKC/NFKD)")


# ══════════════════════════════════════════════════════════════════════════
# substringIndex / hasToken / alphaTokens
# ══════════════════════════════════════════════════════════════════════════


def _substring_index(s: str, delim: str, count: int) -> str:
    """``substringIndex`` 的核心。

    .. code-block:: text

       count > 0  返回第 count 个分隔符**之前**的内容
       count < 0  返回从末尾数第 |count| 个分隔符**之后**的内容
       count = 0  返回空串
       分隔符不存在 → 返回整个串

    ``substringIndex('a,b,c,d', ',', 2) = 'a,b'``;
    ``substringIndex('a,b,c,d', ',', -2) = 'c,d'``;
    ``substringIndex('a,b,c,d', ',', 5) = 'a,b,c,d'``。
    """
    if count == 0:
        return ""
    if delim == "":
        return ""
    if count > 0:
        parts = s.split(delim)
        if count >= len(parts):
            return s
        return delim.join(parts[:count])
    parts = s.split(delim)
    k = -count
    if k >= len(parts):
        return s
    return delim.join(parts[len(parts) - k :])


class FunctionSubstringIndex(FunctionBase):
    """``substringIndex(s, delim, count)`` —— 按第 count 个分隔符切一刀。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _substring_index(_as_str(values[0]), _as_str(values[1]), int(values[2]))


register_function("substringIndex", FunctionSubstringIndex,
                  description="取第 count 个分隔符之前/之后的子串")
register_function("substringIndexUTF8", FunctionSubstringIndex,
                  description="substringIndex 的 UTF-8 版本(分隔符按码点匹配)")


_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
_ALPHA_RE = re.compile(r"[A-Za-z]")


class FunctionHasToken(FunctionBase):
    """``hasToken(s, token)`` —— s 里是否有一个**独立**的 token 等于它。

    "独立"的定义:token 前后必须是非字母数字(或串首/串尾)。
    所以 ``hasToken('hello world', 'world')`` = 1,而
    ``hasToken('helloworld', 'world')`` = 0。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        s = _as_str(values[0])
        token = _as_str(values[1])
        if token == "":
            return 0
        start = 0
        while True:
            idx = s.find(token, start)
            if idx < 0:
                return 0
            left_ok = idx == 0 or not _ALNUM_RE.match(s[idx - 1])
            end = idx + len(token)
            right_ok = end == len(s) or not _ALNUM_RE.match(s[end])
            if left_ok and right_ok:
                return 1
            start = idx + 1


class FunctionAlphaTokens(FunctionBase):
    """``alphaTokens(s)`` —— 按**连续非 ASCII 字母**切分,只保留字母片段。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return [p for p in re.split(r"[^A-Za-z]+", _as_str(values[0])) if p]


register_function("hasToken", FunctionHasToken,
                  aliases=["hasTokenCaseInsensitive"],
                  description="是否含有以非字母数字为边界的独立 token")
register_function("alphaTokens", FunctionAlphaTokens,
                  description="切出所有连续的 ASCII 字母片段")


# ══════════════════════════════════════════════════════════════════════════
# 人类可读格式化
# ══════════════════════════════════════════════════════════════════════════


def format_readable_size(size: float) -> str:
    """``formatReadableSize`` —— ``1024 -> '1.00 KiB'``。

    .. note::

       ``minich/util/strings.py`` 里也有一个 ``format_readable_size``,
       但它的用途是 ``system.parts`` 的展示(小于 1 KiB 时不带小数)。
       这里的是 **SQL 函数**语义:两样东西的**要求**不同,所以是两个实现 ——
       "长得像就合并"往往会在语义分叉时变成一个 bug。
    """
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"]
    value = float(size)
    negative = value < 0
    value = abs(value)
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    text = f"{value:.2f} {units[idx]}"
    return "-" + text if negative else text


def format_readable_quantity(n: float) -> str:
    """``formatReadableQuantity`` —— ``1234567 -> '1.23 million'``。"""
    value = float(n)
    negative = value < 0
    value = abs(value)
    text = str(value)
    for limit, suffix in (
        (1e12, "trillion"), (1e9, "billion"), (1e6, "million"), (1e3, "thousand"),
    ):
        if value >= limit:
            text = f"{value / limit:.2f} {suffix}"
            break
    return "-" + text if negative else text


def format_readable_decimal_size(size: float) -> str:
    """``formatReadableDecimalSize`` —— SI 单位,每级 **1000**(不是 1024)。"""
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    value = float(size)
    negative = value < 0
    value = abs(value)
    idx = 0
    while value >= 1000.0 and idx < len(units) - 1:
        value /= 1000.0
        idx += 1
    text = f"{value:.2f} {units[idx]}"
    return "-" + text if negative else text


def format_readable_time_delta(seconds: float) -> str:
    """``formatReadableTimeDelta`` —— ``100 -> '1 minute 40 seconds'``。

    只有**非零**的单位才会出现在结果里,最少会输出 ``0 seconds``。
    真实 ClickHouse 还接受第二个"精度单位"参数,mini-clickhouse 只做
    默认的"输出到秒"。
    """
    total = int(float(seconds))
    negative = total < 0
    total = abs(total)
    years, rem = divmod(total, 365 * 86400)
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[str] = []
    for value, unit in (
        (years, "year"), (days, "day"), (hours, "hour"),
        (minutes, "minute"), (secs, "second"),
    ):
        if value:
            parts.append(f"{value} {unit}" + ("s" if value != 1 else ""))
    if not parts:
        parts.append("0 seconds")
    text = " ".join(parts)
    return "-" + text if negative else text


class FunctionFormatReadableSize(FunctionBase):
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return format_readable_size(float(values[0]))


class FunctionFormatReadableQuantity(FunctionBase):
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return format_readable_quantity(float(values[0]))


class FunctionFormatReadableDecimalSize(FunctionBase):
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return format_readable_decimal_size(float(values[0]))


class FunctionFormatReadableTimeDelta(FunctionBase):
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return format_readable_time_delta(float(values[0]))


register_function("formatReadableSize", FunctionFormatReadableSize,
                  aliases=["FORMAT_BYTES"],
                  description="字节数转人类可读(1024 进制,如 '1.50 MiB')")
register_function("formatReadableQuantity", FunctionFormatReadableQuantity,
                  description="大数字转人类可读(如 '1.23 million')")
register_function("formatReadableDecimalSize", FunctionFormatReadableDecimalSize,
                  description="字节数转人类可读(1000 进制,如 '1.50 MB')")
register_function("formatReadableTimeDelta", FunctionFormatReadableTimeDelta,
                  description="秒数转人类可读的时间差")


# ══════════════════════════════════════════════════════════════════════════
# `length` / `empty` / `notEmpty` 的**容器重载**
# ══════════════════════════════════════════════════════════════════════════
#
# 这三个函数在 ClickHouse 里是**重载**的:同一个名字,参数是 String 时
# 量"字节数",参数是 Array/Tuple/Map 时量"元素个数"。
#
# .. warning::
#
#    早期版本只有一个"把任何值转成字符串再数字节"的实现 —— 于是
#    ``length([1,2,3])`` 得到 **9**(字符串 ``"[1, 2, 3]"`` 的形状),
#    而不是 **3**;``empty(splitByChar(',', ''))`` 得到 0(非空)而不是
#    1(空数组)。两个都是**静默的错误答案**:类型对(UInt64/UInt8)、
#    不报错、只是数错了 —— 而且 ``empty`` 那一支会让过滤条件**反向**。
#
#    修法很直接:按**值的形状**分派。``str`` 量字节,``list``/``tuple``/
#    ``dict`` 量元素个数。真实 ClickHouse 在编译期按类型选重载,
#    我们在运行期按 Python 值的类型分派 —— 语义相同。


class _FunctionLength(FunctionBase):
    """``length(x)`` —— String 量**字节**数,Array/Tuple/Map 量**元素**个数。"""

    deterministic = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT64

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _measure(values[0])


class _FunctionEmpty(FunctionBase):
    """``empty(x)`` —— 空串 / 空数组 / 空 Map 都为 1。"""

    deterministic = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 1 if _measure(values[0]) == 0 else 0


class _FunctionNotEmpty(FunctionBase):
    """``notEmpty(x)`` —— ``empty`` 的取反。"""

    deterministic = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return 0 if _measure(values[0]) == 0 else 1


def _measure(value: Any) -> int:
    """``length`` 家族的度量:容器数元素,String 数字节。

    ``None`` 返回 0 —— 真实 ClickHouse 会走 NULL 传播,这里是防御性的兜底。
    """
    if value is None:
        return 0
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(bytes(value))
    return len(str(value).encode("utf-8", "surrogateescape"))


register_function("length", _FunctionLength, aliases=["lengthBytes", "octet_length"],
                  description="String 的**字节**长度,或 Array/Tuple/Map 的**元素**个数")
register_function("empty", _FunctionEmpty,
                  description="是否为空(空串/空数组/空 Map 都为 1)")
register_function("notEmpty", _FunctionNotEmpty,
                  description="是否非空")
