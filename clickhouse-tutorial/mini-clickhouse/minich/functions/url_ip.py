"""URL 与 IP 函数 —— 把"字符串切分"做对,比看起来难得多。

URL 有一个很容易被忽略的性质:**它不是一个规整的字符串**。
``https://user:pass@www.example.com:8080/a/b?x=1#frag`` 里
冒号出现了两次、斜杠出现三次、点号还可能是 IPv6 的一部分。
所以本模块不"按字符切",而是先用 :func:`urllib.parse.urlsplit`
解析出**组件**,再在每个组件内部做切分。

.. code-block:: text

   https://user:pass@www.example.com:8080/a/b?x=1#frag
   └──┬─┘└───┬───┘└──────┬──────┘└─┬┘└─┬┘└─┬┘└──┬──┘
    scheme  userinfo      host     port path query fragment
                          └──── netloc ────┘
                              ↓ domain() 只要 host 这一段

   domain(...)              = 'www.example.com'   ← 不含端口、不含 userinfo
   domainWithoutWWW(...)    = 'example.com'

.. warning::

   mini-clickhouse 用标准库的 :mod:`urllib.parse`,但有几个地方
   和真实 ClickHouse **不完全一样**,都写在各个函数的文档里:

   * ``topLevelDomain`` 不做"已知公共后缀表"查询,所以
     ``'a.b.co.uk'`` 会得到 ``'uk'`` 而真实 CH 也是 ``'uk'``(它同样
     只看最后一段,不信的话去翻它的源码 —— 这里保持诚实);
   * ``decodeURLComponent`` 对非法的 ``%`` 序列是**宽容**的(原样保留),
     真实 ClickHouse 会抛错。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, List, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from .base import FunctionBase, register_function

__all__ = ["split_url", "domain_of", "ipv6_to_text"]

_UINT8 = get_data_type("UInt8")
_UINT16 = get_data_type("UInt16")
_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")
_STRING = get_data_type("String")
_ARRAY_STRING = get_data_type("Array(String)")


# ══════════════════════════════════════════════════════════════════════════
# URL 拆解
# ══════════════════════════════════════════════════════════════════════════


class _UrlParts:
    """一个 URL 拆出来的几个部件(比 ``SplitResult`` 多一点"没有 scheme 也能用"的容错)。"""

    __slots__ = ("raw", "scheme", "host", "port", "path", "query", "fragment")

    def __init__(self, raw: str, scheme: str, host: str, port: Optional[int],
                 path: str, query: str, fragment: str) -> None:
        self.raw = raw
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.query = query
        self.fragment = fragment


def split_url(url: str) -> _UrlParts:
    """把 URL 拆成组件。

    ``urlsplit`` 遇到 ``www.example.com/a`` 这种**没有 scheme** 的片段时,
    会把 ``www.example.com/a`` 整个当成 path(因为 URI 语法允许这样)。
    真实 ClickHouse 的 ``domain`` 却能认出主机名 —— 所以这里做了一层
    启发式:如果解析出来没有 netloc 而 path 里有点号,就把第一段当主机。
    """
    text = url.strip()
    # 'www.example.com/p'、'example.com:8080/p' 这种"缺了 scheme"的写法:
    # urlsplit 会把第一段当成 scheme(URI 语法允许 host 长得像 scheme),
    # 于是主机名跑丢了。补一个 '//' 就能让 urlsplit 走 authority 那条分支。
    normalized = text
    if "://" not in text and not text.startswith("//"):
        first = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "." in first:
            normalized = "//" + text
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    path = parts.path
    port: Optional[int] = None
    try:
        port = parts.port
    except ValueError:
        port = None
    return _UrlParts(
        raw=text,
        scheme=parts.scheme,
        host=host,
        port=port,
        path=path,
        query=parts.query,
        fragment=parts.fragment,
    )


def domain_of(url: str) -> str:
    """主机名(**不含端口**)。

    .. code-block:: sql

       domain('https://www.example.com:8080/p')  -- 'www.example.com'
       domain('www.example.com/p')               -- 'www.example.com'(没有 scheme 也行)
    """
    return split_url(url).host


class _UrlFunction(FunctionBase):
    """所有"输入 URL、输出字符串"的函数底座。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, parts: _UrlParts) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute() not implemented")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return self.compute(split_url(str(values[0])))


def _register_url(name: str, compute: Any, ret: DataType = _STRING, *,
                  aliases: Sequence[str] = (), description: str = "") -> type:
    class _F(_UrlFunction):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return ret

        def compute(self, parts: _UrlParts) -> Any:
            return compute(parts)

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, aliases=list(aliases), description=description)
    return _F


_register_url("domain", lambda p: p.host, description="URL 的主机名(不含端口)")
_register_url("domainWithoutWWW",
              lambda p: p.host[4:] if p.host.lower().startswith("www.") else p.host,
              description="主机名,去掉开头的 www.")
_register_url("topLevelDomain",
              lambda p: p.host.rsplit(".", 1)[-1] if "." in p.host else "",
              description="顶级域名(最后一段)")
_register_url("path", lambda p: p.path or "/",
              description="路径(不含查询串)")
_register_url("pathFull", lambda p: (p.path or "/") + (("?" + p.query) if p.query else ""),
              description="路径 + 查询串")
_register_url("queryString", lambda p: p.query, description="查询串(不含 ?)")
_register_url("fragment", lambda p: p.fragment, description="片段(不含 #)")
_register_url("queryStringAndFragment",
              lambda p: p.query + ("#" + p.fragment if p.fragment else ""),
              description="查询串 + 片段")
_register_url("port", lambda p: p.port or 0, _UINT16,
              description="端口号(没有就是 0)")
_register_url("protocol", lambda p: p.scheme, description="协议名(如 https)")


def _strip_www(host: str) -> str:
    return host[4:] if host.lower().startswith("www.") else host


class FunctionFirstSignificantSubdomain(_UrlFunction):
    """``firstSignificantSubdomain(url)`` —— "有意义的那个子域"。

    .. code-block:: sql

       firstSignificantSubdomain('https://news.yandex.ru/')  -- 'yandex'
       firstSignificantSubdomain('https://www.clickhouse.com/') -- 'clickhouse'

    规则:去掉开头的 ``www.`` 之后,取**倒数第二段**。
    两级域名(``example.com``)会得到 ``example`` —— 它其实不是子域,
    这一点与真实 ClickHouse 的边界行为可能不同。
    """

    def compute(self, parts: _UrlParts) -> Any:
        labels = _strip_www(parts.host).split(".")
        return labels[-2] if len(labels) >= 2 else ""


class FunctionCutToFirstSignificantSubdomain(_UrlFunction):
    """``cutToFirstSignificantSubdomain(url)`` —— 从第一个有意义的子域开始截。

    ``cutToFirstSignificantSubdomain('https://news.yandex.ru/hello')``
    → ``'yandex.ru/hello'``。
    """

    keep_www = False

    def compute(self, parts: _UrlParts) -> Any:
        host = parts.host
        stripped = _strip_www(host)
        labels = stripped.split(".")
        if len(labels) < 2:
            start = host
        else:
            tail = ".".join(labels[-2:])
            start = ("www." + tail) if (self.keep_www and host.lower().startswith("www.")) else tail
        rest = ""
        split = parts.raw.split(host, 1)
        if len(split) == 2:
            rest = split[1]
        if not parts.host and parts.raw:
            return parts.raw
        return start + rest


class FunctionCutToFirstSignificantSubdomainWithWWW(
    FunctionCutToFirstSignificantSubdomain
):
    keep_www = True


register_function("firstSignificantSubdomain", FunctionFirstSignificantSubdomain,
                  aliases=["firstSignificantSubdomainCustom"],
                  description="第一个有意义的子域(去 www 后倒数第二段)")
register_function("cutToFirstSignificantSubdomain",
                  FunctionCutToFirstSignificantSubdomain,
                  description="从第一个有意义的子域开始截断 URL")
register_function("cutToFirstSignificantSubdomainWithWWW",
                  FunctionCutToFirstSignificantSubdomainWithWWW,
                  description="同上,但保留开头的 www.")


# ══════════════════════════════════════════════════════════════════════════
# cut* 系列
# ══════════════════════════════════════════════════════════════════════════


class FunctionCutWWW(FunctionBase):
    """``cutWWW(url)`` —— 去掉第一个 ``www.``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        text = str(values[0])
        idx = text.find("www.")
        return text[:idx] + text[idx + 4 :] if idx >= 0 else text


class _CutTail(FunctionBase):
    """``cutQueryString`` / ``cutFragment`` / ``cutQueryStringAndFragment``。"""

    drop_query = True
    drop_fragment = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        text = str(values[0])
        end = len(text)
        if self.drop_fragment and "#" in text:
            end = min(end, text.index("#"))
        if self.drop_query and "?" in text[:end]:
            end = min(end, text.index("?"))
        return text[:end]


class FunctionCutQueryString(_CutTail):
    drop_query = True
    drop_fragment = False


class FunctionCutFragment(_CutTail):
    drop_query = False
    drop_fragment = True


class FunctionCutQueryStringAndFragment(_CutTail):
    drop_query = True
    drop_fragment = True


class FunctionCutURLParameter(FunctionBase):
    """``cutURLParameter(url, name)`` —— 删掉一个查询参数。

    同时正确处理 ``?a=1&b=2``、``?a=1``(删完变成空查询串时把 ``?`` 也去掉)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        text = str(values[0])
        name = str(values[1])
        parts = split_url(text)
        if not parts.query:
            return text
        kept = [
            pair for pair in parts.query.split("&")
            if pair.split("=", 1)[0] != name
        ]
        head = text.split("?", 1)[0]
        tail = ("#" + parts.fragment) if parts.fragment else ""
        if not kept:
            return head + tail
        return head + "?" + "&".join(kept) + tail


register_function("cutWWW", FunctionCutWWW, description="去掉第一个 'www.'")
register_function("cutQueryString", FunctionCutQueryString,
                  description="去掉 ? 及其后的查询串")
register_function("cutFragment", FunctionCutFragment, description="去掉 # 及其后的片段")
register_function("cutQueryStringAndFragment", FunctionCutQueryStringAndFragment,
                  description="同时去掉查询串与片段")
register_function("cutURLParameter", FunctionCutURLParameter,
                  description="从 URL 中删掉一个查询参数")


# ══════════════════════════════════════════════════════════════════════════
# 查询参数提取
# ══════════════════════════════════════════════════════════════════════════


def _pairs(query: str) -> List[tuple]:
    out = []
    for pair in query.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        out.append((k, v))
    return out


class FunctionExtractURLParameter(FunctionBase):
    """``extractURLParameter(url, name)`` —— 取参数值(不解码百分号)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        name = str(values[1])
        for k, v in _pairs(split_url(str(values[0])).query):
            if k == name:
                return v
        return ""


class FunctionExtractURLParameters(FunctionBase):
    """``extractURLParameters(url)`` —— 所有 ``name=value`` 组成的数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return [f"{k}={v}" for k, v in _pairs(split_url(str(values[0])).query)]


class FunctionExtractURLParameterNames(FunctionBase):
    """``extractURLParameterNames(url)`` —— 所有参数名组成的数组。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return [k for k, _ in _pairs(split_url(str(values[0])).query)]


register_function("extractURLParameter", FunctionExtractURLParameter,
                  description="取 URL 参数的值")
register_function("extractURLParameters", FunctionExtractURLParameters,
                  description="取所有 name=value 对")
register_function("extractURLParameterNames", FunctionExtractURLParameterNames,
                  description="取所有参数名")


# ══════════════════════════════════════════════════════════════════════════
# 层级
# ══════════════════════════════════════════════════════════════════════════


def _hierarchy(url: str, path_only: bool) -> List[str]:
    scheme_end = url.find("://")
    start = scheme_end + 3 if scheme_end >= 0 else 0
    if path_only:
        slash = url.find("/", start)
        start = slash if slash >= 0 else len(url)
    cuts = [i for i in range(start, len(url)) if url[i] in "/?#"]
    parts = [url[: i + 1] for i in cuts]
    if not parts or parts[-1] != url:
        parts.append(url)
    if path_only:
        prefix = url[: start - (1 if start > 0 and url[start - 1] == "/" else 0)]
        parts = [p[len(prefix):] for p in parts]
    return parts


class FunctionURLHierarchy(FunctionBase):
    """``URLHierarchy(url)`` —— 逐级截断出来的所有前缀。

    ``URLHierarchy('https://example.com/a/b')``
    → ``['https://example.com/', 'https://example.com/a/', 'https://example.com/a/b']``
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _ARRAY_STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _hierarchy(str(values[0]), path_only=False)


class FunctionURLPathHierarchy(FunctionURLHierarchy):
    """``URLPathHierarchy(url)`` —— 同上,但只保留**路径**部分。"""

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _hierarchy(str(values[0]), path_only=True)


register_function("URLHierarchy", FunctionURLHierarchy,
                  description="URL 逐级截断的所有前缀")
register_function("URLPathHierarchy", FunctionURLPathHierarchy,
                  description="路径逐级截断的所有前缀")


# ══════════════════════════════════════════════════════════════════════════
# 百分号编码
# ══════════════════════════════════════════════════════════════════════════


def _decode_component(text: str, plus_as_space: bool) -> str:
    if plus_as_space:
        text = text.replace("+", " ")
    return unquote(text, encoding="utf-8", errors="replace")


class FunctionDecodeURLComponent(FunctionBase):
    """``decodeURLComponent(s)`` —— 百分号解码(**不**把 ``+`` 当空格)。

    .. warning::

       对非法的 ``%`` 序列是**宽容**的(原样保留),真实 ClickHouse 会抛错。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _decode_component(str(values[0]), plus_as_space=False)


class FunctionDecodeURLFormComponent(FunctionDecodeURLComponent):
    """``decodeURLFormComponent(s)`` —— 百分号解码 **且** ``+`` → 空格。"""

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return _decode_component(str(values[0]), plus_as_space=True)


class FunctionEncodeURLComponent(FunctionBase):
    """``encodeURLComponent(s)`` —— 百分号编码。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return quote(str(values[0]), safe="")


class FunctionEncodeURLFormComponent(FunctionEncodeURLComponent):
    """``encodeURLFormComponent(s)`` —— 百分号编码,空格写成 ``+``。"""

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return quote(str(values[0]), safe="").replace(" ", "+")


register_function("decodeURLComponent", FunctionDecodeURLComponent,
                  description="URL 百分号解码")
register_function("decodeURLFormComponent", FunctionDecodeURLFormComponent,
                  description="表单解码(百分号 + '+' 变空格)")
register_function("encodeURLComponent", FunctionEncodeURLComponent,
                  description="URL 百分号编码")
register_function("encodeURLFormComponent", FunctionEncodeURLFormComponent,
                  description="表单编码(空格变 '+')")


# ══════════════════════════════════════════════════════════════════════════
# IP
# ══════════════════════════════════════════════════════════════════════════


class FunctionIsIPAddressInRange(FunctionBase):
    """``isIPAddressInRange(address, prefix)`` —— 地址是否落在 CIDR 网段内。

    ``isIPAddressInRange('192.168.0.5', '192.168.0.0/24')`` → 1。
    用 Python 的 :mod:`ipaddress` 做,IPv4/IPv6 都支持。
    非法输入**返回 0**(真实 ClickHouse 会抛错 —— 这条差异是刻意的,
    因为它常被用在 WHERE 里对整列做过滤,一行坏数据不该让整条查询失败)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        try:
            addr = ipaddress.ip_address(str(values[0]).strip())
            net = ipaddress.ip_network(str(values[1]).strip(), strict=False)
        except ValueError:
            return 0
        return 1 if addr.version == net.version and addr in net else 0


class FunctionIPv4NumToString(FunctionBase):
    """``IPv4NumToString(num)`` —— UInt32 → 点分十进制。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        v = int(values[0]) & 0xFFFFFFFF
        return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


class FunctionIPv4NumToStringClassC(FunctionBase):
    """``IPv4NumToStringClassC(num)`` —— 最后一段换成 ``xxx``。

    这是给"按 C 类网段聚合"准备的:把同一 /24 的地址折叠成一个标签,
    可以直接 ``GROUP BY`` 而不用先算掩码。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        v = int(values[0]) & 0xFFFFFFFF
        return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.xxx"


class FunctionIPv4StringToNum(FunctionBase):
    """``IPv4StringToNum(s)`` —— 点分十进制 → UInt32。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT32

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        parts = str(values[0]).strip().split(".")
        if len(parts) != 4:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"IPv4StringToNum: cannot parse {values[0]!r} as an IPv4 address",
            )
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"IPv4StringToNum: cannot parse {values[0]!r} as an IPv4 address",
            )
        if any(n < 0 or n > 255 for n in nums):
            raise_error(ErrorCode.ARGUMENT_OUT_OF_BOUND,
                        f"IPv4StringToNum: octet out of range in {values[0]!r}")
        return (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]


def ipv6_to_text(value: Any) -> str:
    """把 16 字节的 IPv6 表示转成文本(支持传入"文本"或"字节")。

    .. note::

       ``inet_pton`` 对**含 NUL 字节的 str**(比如 16 字节的原始地址在
       ``FixedString(16)`` 列里存成 ``'\\x00…\\x01'`` 这种形态)抛的是
       ``ValueError: embedded null character``,含代理字符(``surrogateescape``
       解出来的非 UTF-8 字节)时抛 ``UnicodeEncodeError`` —— 两者都**不是**
       ``OSError``。所以这里必须把 ``ValueError`` 一并接住,否则
       ``IPv6NumToString(IPv6StringToNum('::1'))`` 这条文档承诺的往返会直接崩。
    """
    if isinstance(value, str):
        text = value
        try:
            return socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, text))
        except (OSError, ValueError):
            # 不是"文本形式的地址",那就按"原始 16 字节内存表示"处理
            value = text.encode("utf-8", "surrogateescape")
    data = bytes(value)
    data = data.ljust(16, b"\x00")[:16]
    return socket.inet_ntop(socket.AF_INET6, data)


class FunctionIPv6NumToString(FunctionBase):
    """``IPv6NumToString(x)`` —— 16 字节表示 → 文本。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        return ipv6_to_text(values[0])


class FunctionIPv6StringToNum(FunctionBase):
    """``IPv6StringToNum(s)`` —— 文本 → 16 字节(``FixedString(16)``)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("FixedString(16)")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        try:
            return socket.inet_pton(socket.AF_INET6, str(values[0]).strip())
        except (OSError, ValueError) as exc:
            # 非法输入在 ``inet_pton`` 里分两类:纯格式错误是 ``OSError``,
            # 含 NUL 字节的 str 则是 ``ValueError`` —— 两者都必须转成
            # ``MiniChError``,不能让原生异常漏出去。
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"IPv6StringToNum: cannot parse {values[0]!r} as IPv6: {exc}",
            )


class FunctionToIPv4(FunctionBase):
    """``toIPv4(s)`` —— 转成 ``IPv4`` 类型(内部是 UInt32)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("IPv4")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        v = values[0]
        if isinstance(v, str):
            return int(ipaddress.IPv4Address(v.strip()))
        return int(v) & 0xFFFFFFFF


class FunctionToIPv6(FunctionBase):
    """``toIPv6(s)`` —— 转成 ``IPv6`` 类型(内部是 ``FixedString(16)``)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("IPv6")

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        v = values[0]
        if isinstance(v, str):
            try:
                return socket.inet_pton(socket.AF_INET6, v.strip()).decode(
                    "utf-8", "surrogateescape"
                )
            except (OSError, ValueError) as exc:
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"toIPv6: cannot parse {v!r} as IPv6: {exc}",
                )
        return ipv6_to_text(v)


register_function("isIPAddressInRange", FunctionIsIPAddressInRange,
                  description="地址是否在 CIDR 网段内(非法输入返回 0)")
register_function("IPv4NumToString", FunctionIPv4NumToString,
                  aliases=["INET_NTOA"],
                  description="UInt32 转点分十进制")
register_function("IPv4NumToStringClassC", FunctionIPv4NumToStringClassC,
                  description="UInt32 转点分十进制,末段替换为 xxx")
register_function("IPv4StringToNum", FunctionIPv4StringToNum,
                  aliases=["INET_ATON"],
                  description="点分十进制转 UInt32")
register_function("IPv6NumToString", FunctionIPv6NumToString,
                  description="16 字节转 IPv6 文本")
register_function("IPv6StringToNum", FunctionIPv6StringToNum,
                  description="IPv6 文本转 16 字节")
register_function("toIPv4", FunctionToIPv4, description="转成 IPv4 类型")
register_function("toIPv6", FunctionToIPv6, description="转成 IPv6 类型")
