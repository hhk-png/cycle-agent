"""错误码与异常体系 —— ClickHouse 的「错误编号」是一等公民。

真实 ClickHouse 里,每一个错误都有一个**稳定编号**(``ErrorCodes`` 枚举,
定义在 ``src/Common/ErrorCodes.cpp``),对外表现为:

.. code-block:: text

    Code: 60. DB::Exception: Table default.hits does not exist. (UNKNOWN_TABLE)
    ^^^^^^^^                                                     ^^^^^^^^^^^^
    数字编号(程序判断用)                                          名字(人读用)

为什么要有编号? 因为**错误是接口的一部分**:

1. 客户端要靠编号区分"表不存在(重试没用)"和"连接超时(重试有用)"——
   如果只能匹配字符串,换一个语言版本就失效了;
2. 编号**跨语言、跨版本稳定**,而错误消息措辞会变;
3. 服务端把编号一路透传到 client,运维可以直接建"错误码 → 值班策略"的映射。

mini-clickhouse 沿用这个设计,但只保留教学与测试真正用到的那几十个编号。
每个编号都标注了它在真实 ClickHouse 里的值,方便读者对照官方文档
(``docs/en/operations/error-codes.md`` 只有一部分,完整表在源码里)。

注意一个容易踩的坑: **异常类的层次结构不能代替错误码**。
真实 ClickHouse 里几乎所有查询期错误都是同一个类 ``DB::Exception``,
靠 ``code()`` 区分 —— 所以 mini-clickhouse 也只定义一个 ```MiniChError``,
外加一个用于"客户端可见的干净错误"的薄子类。
"""

from __future__ import annotations

from typing import Dict, Optional

__all__ = [
    "MiniChError",
    "ErrorCode",
    "error_name",
    "error_code",
    "raise_error",
]


#: 错误编号 -> 错误名。
#:
#: 左边是**真实 ClickHouse 的编号**(我们照抄,这样读者查官方文档能对上),
#: 右边是我们真正会抛的那些。没有列出的编号,mini-clickhouse 不会抛出。
_CODES: Dict[int, str] = {
    # ── 通用 ──────────────────────────────────────────────────────────
    1: "UNSUPPORTED_METHOD",
    2: "UNSUPPORTED_PARAMETER",
    3: "UNEXPECTED_END_OF_FILE",
    4: "EXPECTED_END_OF_FILE",
    5: "CANNOT_PARSE_TEXT",
    6: "INCORRECT_NUMBER_OF_COLUMNS",
    7: "THERE_IS_NO_COLUMN",
    8: "SIZES_OF_COLUMNS_DOESNT_MATCH",
    9: "NOT_FOUND_COLUMN_IN_BLOCK",
    10: "POSITION_OUT_OF_BOUND",
    11: "PARAMETER_OUT_OF_BOUND",
    12: "SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH",
    13: "DUPLICATE_COLUMN",
    14: "NO_SUCH_COLUMN_IN_TABLE",
    15: "SIZE_OF_FIXED_STRING_DOESNT_MATCH",
    16: "CANNOT_READ_ALL_DATA_LEGACY",
    17: "CANNOT_APPEND_TO_COLUMN",
    18: "CANNOT_READ_FROM_ISTREAM",
    19: "CANNOT_WRITE_TO_OSTREAM",
    20: "CANNOT_PARSE_NUMBER",
    21: "CANNOT_PARSE_DATE",
    22: "CANNOT_PARSE_DATETIME",
    23: "CANNOT_PARSE_UUID",
    24: "ILLEGAL_TYPE_OF_ARGUMENT",
    25: "ILLEGAL_COLUMN",
    26: "CANNOT_CONVERT_TYPE",
    27: "CANNOT_PARSE_INPUT_ASSERTION_FAILED",
    28: "CANNOT_PARSE_QUOTED_STRING",
    29: "CANNOT_PARSE_ESCAPE_SEQUENCE",
    30: "CANNOT_PARSE_DATE_TIME",
    31: "ARGUMENT_OUT_OF_BOUND",
    33: "CANNOT_READ_ALL_DATA",
    # ── 语法与语义分析 ────────────────────────────────────────────────
    42: "NUMBER_OF_ARGUMENTS_DOESNT_MATCH",
    47: "UNKNOWN_IDENTIFIER",
    48: "NOT_IMPLEMENTED",
    49: "LOGICAL_ERROR",
    50: "UNKNOWN_TYPE",
    51: "EMPTY_LIST_OF_COLUMNS_QUERIED",
    52: "COLUMN_QUERIED_MORE_THAN_ONCE",
    53: "TYPE_MISMATCH",
    54: "STORAGE_REQUIRES_PARAMETER",
    55: "UNKNOWN_STORAGE",
    56: "TABLE_ALREADY_EXISTS",
    57: "TABLE_METADATA_ALREADY_EXISTS",
    58: "ILLEGAL_TYPE_OF_COLUMN_FOR_FILTER",
    59: "ALIAS_REQUIRED",
    60: "UNKNOWN_TABLE",  # 真实 CH 里 60 才是 UNKNOWN_TABLE,上面的 59 是 ALIAS_REQUIRED
    61: "SYNTAX_ERROR",
    62: "EMPTY_QUERY",
    63: "UNKNOWN_AGGREGATE_FUNCTION",
    64: "CANNOT_GET_SIZE_OF_FIELD",
    65: "NOT_FOUND_EXPECTED_DATA_PART",
    66: "TOO_MANY_UNEXPECTED_DATA_PARTS",
    67: "DIRECTORY_ALREADY_EXISTS",
    68: "CANNOT_ADD_COLUMN_DEFAULT",
    69: "ARGUMENT_TYPE_MISMATCH",
    70: "CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN",
    71: "INCOMPATIBLE_COLUMNS",
    72: "INCOMPATIBLE_COLUMN_TYPE",
    73: "INVALID_PARTITION_VALUE",
    74: "NOT_FOUND_COLUMN_IN_TABLE",
    75: "CANNOT_PARSE_STRING",
    76: "CANNOT_PARSE_FLOAT",
    77: "AMBIGUOUS_COLUMN_NAME",
    78: "UNKNOWN_FUNCTION",
    # ── 类型与转换 ────────────────────────────────────────────────────
    102: "UNKNOWN_ELEMENT_OF_ENUM",
    110: "UNKNOWN_SETTING",
    115: "UNKNOWN_PACKING",
    118: "ILLEGAL_AGGREGATION",
    119: "UNKNOWN_AGGREGATE_FUNCTION_COMBINATOR",
    120: "TABLE_IS_READ_ONLY",
    121: "UNKNOWN_DATABASE",
    122: "DATABASE_ALREADY_EXISTS",
    123: "TABLE_IS_READ_ONLY_2",
    124: "QUERY_IS_TOO_LARGE",
    # ── 内存与资源 ────────────────────────────────────────────────────
    158: "TOO_MANY_ROWS",
    159: "TIMEOUT_EXCEEDED",
    160: "TOO_SLOW",
    164: "READONLY",
    173: "ALL_CONNECTION_TRIES_FAILED",
    # ── 分布式 / 复制 ─────────────────────────────────────────────────
    202: "TOO_MANY_SIMULTANEOUS_QUERIES",
    203: "NO_FREE_CONNECTION",
    209: "SOCKET_TIMEOUT",
    210: "NETWORK_ERROR",
    216: "NO_SUCH_REPLICA",
    225: "NO_ZOOKEEPER",
    232: "TOO_MANY_PARTS",
    233: "CHECKSUM_DOESNT_MATCH",
    234: "NO_ZOOKEEPER_CONFIGURATION",
    236: "ABORTED",
    241: "MEMORY_LIMIT_EXCEEDED",
    # ── MergeTree 与 part ─────────────────────────────────────────────
    246: "BAD_DATA_PART_NAME",
    248: "NO_REPLICA_HAS_PART",
    252: "TOO_MANY_MARKS",
    255: "UNKNOWN_FORMAT",
    256: "BAD_ARGUMENTS",
    261: "UNKNOWN_POLICY",
    262: "TOO_MANY_COLUMNS",
    # ── 权限 ──────────────────────────────────────────────────────────
    497: "ACCESS_DENIED",
    511: "SETTING_CONSTRAINT_VIOLATION",
    # ── 教学实现新增(真实 ClickHouse 里没有这些编号)────────────────────
    1001: "NOT_IMPLEMENTED_IN_MINICH",
    1002: "LOGICAL_ERROR_IN_MINICH",
    1003: "VERIFICATION_FAILED",
}

# 上面那张表里有两处**有意的简化**,这里显式澄清,免得读者查表时困惑:
#
#   * 真实的 59 是 ``ALIAS_REQUIRED``、60 才是 ``UNKNOWN_TABLE``;
#     mini-clickhouse 只映射 59 和 60 到后者。我们不做列别名推导的
#     中间阶段,所以 ``ALIAS_REQUIRED`` 永远不会被抛出。
#   * 我们**只登记真正会抛出的编号**。早期版本为了让"查表时不至于查空"
#     塞了几条编造的占位映射(比如把 174、261 也映射到
#     ``UNKNOWN_ELEMENT_OF_ENUM``)—— 那比查空更糟:它让一次错误的
#     查表**看起来成功了**。
#
# 一条纪律:错误编号必须**稳定**,错误的措辞**不必**稳定 ——
# 因为程序按编号判断,人按措辞理解。

#: 反向表:错误名 -> 编号(取**最小**编号,保证确定性)
_BY_NAME: Dict[str, int] = {}
for _code in sorted(_CODES):
    _BY_NAME.setdefault(_CODES[_code], _code)
del _code


def error_code(name: str) -> int:
    """按名字取错误编号;取不到返回 1002(LOGICAL_ERROR_IN_MINICH)。"""
    return _BY_NAME.get(name, 1002)


def error_name(code: int) -> str:
    """按编号取错误名;取不到返回 ``UNKNOWN_ERROR_<code>``。"""
    return _CODES.get(code, f"UNKNOWN_ERROR_{code}")


class ErrorCode:
    """错误码常量表 —— 便于写 ``raise_error(ErrorCode.UNKNOWN_TABLE, ...)``。

    这里不做成 ``enum.Enum``,原因是真实 ClickHouse 的错误码是**开放的**
    (第三方扩展可以注册新码),用类属性更好扩展,且 `ErrorCode.UNKNOWN_TABLE`
    在 IDE 里仍然是可跳转的常量。
    """

    UNSUPPORTED_METHOD = 1
    UNSUPPORTED_PARAMETER = 2
    UNEXPECTED_END_OF_FILE = 3
    CANNOT_PARSE_TEXT = 5
    INCORRECT_NUMBER_OF_COLUMNS = 6
    THERE_IS_NO_COLUMN = 7
    SIZES_OF_COLUMNS_DOESNT_MATCH = 8
    NOT_FOUND_COLUMN_IN_BLOCK = 9
    DUPLICATE_COLUMN = 13
    NO_SUCH_COLUMN_IN_TABLE = 14
    SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH = 12
    SIZE_OF_FIXED_STRING_DOESNT_MATCH = 15
    CANNOT_READ_FROM_ISTREAM = 18
    CANNOT_WRITE_TO_OSTREAM = 19
    CANNOT_PARSE_UUID = 23
    CANNOT_PARSE_ESCAPE_SEQUENCE = 29
    CANNOT_PARSE_NUMBER = 20
    CANNOT_PARSE_DATE = 21
    CANNOT_PARSE_DATETIME = 22
    ILLEGAL_TYPE_OF_ARGUMENT = 24
    ILLEGAL_COLUMN = 25
    CANNOT_CONVERT_TYPE = 26
    CANNOT_PARSE_QUOTED_STRING = 28
    ARGUMENT_OUT_OF_BOUND = 31
    CANNOT_READ_ALL_DATA = 33
    NUMBER_OF_ARGUMENTS_DOESNT_MATCH = 42
    UNKNOWN_IDENTIFIER = 47
    NOT_IMPLEMENTED = 48
    LOGICAL_ERROR = 49
    UNKNOWN_TYPE = 50
    TYPE_MISMATCH = 53
    STORAGE_REQUIRES_PARAMETER = 54
    UNKNOWN_STORAGE = 55
    TABLE_ALREADY_EXISTS = 57
    UNKNOWN_TABLE = 60
    SYNTAX_ERROR = 61
    EMPTY_LIST_OF_COLUMNS_QUERIED = 51
    EMPTY_QUERY = 62
    CANNOT_ADD_COLUMN_DEFAULT = 68
    QUERY_IS_TOO_LARGE = 124
    UNKNOWN_AGGREGATE_FUNCTION = 63
    CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN = 70
    INCOMPATIBLE_COLUMNS = 71
    INVALID_PARTITION_VALUE = 73
    AMBIGUOUS_COLUMN_NAME = 77
    UNKNOWN_FUNCTION = 78
    CANNOT_PARSE_FLOAT = 76
    ILLEGAL_DIVISION = 153
    FUNCTION_THROW_IF_NON_ZERO = 395
    UNKNOWN_ELEMENT_OF_ENUM = 102
    UNKNOWN_SETTING = 110
    ILLEGAL_AGGREGATION = 118
    UNKNOWN_AGGREGATE_FUNCTION_COMBINATOR = 119
    TABLE_IS_READ_ONLY = 120
    UNKNOWN_DATABASE = 121
    DATABASE_ALREADY_EXISTS = 122
    TIMEOUT_EXCEEDED = 159
    TOO_SLOW = 160
    TOO_MANY_ROWS = 158
    READONLY = 164
    ALL_CONNECTION_TRIES_FAILED = 173
    TOO_MANY_PARTS = 232
    CHECKSUM_DOESNT_MATCH = 233
    ABORTED = 236
    MEMORY_LIMIT_EXCEEDED = 241
    BAD_DATA_PART_NAME = 246
    TOO_MANY_MARKS = 252
    UNKNOWN_FORMAT = 255
    ACCESS_DENIED = 497
    SETTING_CONSTRAINT_VIOLATION = 511
    NOT_IMPLEMENTED_IN_MINICH = 1001
    LOGICAL_ERROR_IN_MINICH = 1002
    VERIFICATION_FAILED = 1003


class MiniChError(Exception):
    """所有 mini-clickhouse 异常的基类。

    属性:

    * :attr:`code`     —— 错误编号(见 :data:`_CODES`)
    * :attr:`name`     —— 错误名
    * :attr:`message`  —— 人类可读的说明(**不含**编号/名字前缀)
    * :attr:`query_id` —— 出错时正在跑的那条查询的 ID(可能为 None)

    ``str(e)`` 的格式刻意做成和真实 ClickHouse 的服务端消息一致::

        Code: 60. DB::Exception: Table default.hits does not exist. (UNKNOWN_TABLE)

    这样测试里可以直接用 ``assert "UNKNOWN_TABLE" in str(e)``,
    也方便读者把 mini-clickhouse 的输出和真实 CH 的日志对照。
    """

    #: 默认错误码,子类可覆盖
    default_code: int = ErrorCode.LOGICAL_ERROR_IN_MINICH

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        query_id: Optional[str] = None,
    ) -> None:
        self.message = message
        self.code = self.default_code if code is None else code
        self.name = error_name(self.code)
        self.query_id = query_id
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - 纯格式化
        suffix = f" (query_id: {self.query_id})" if self.query_id else ""
        return f"Code: {self.code}. DB::Exception: {self.message}. ({self.name}){suffix}"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"{type(self).__name__}(code={self.code}, name={self.name!r}, "
            f"message={self.message!r})"
        )

    def with_query_id(self, query_id: str) -> "MiniChError":
        """给异常补上 query_id 后返回自身(便于一句式 ``raise e.with_query_id(q)``)。"""
        self.query_id = query_id
        return self


def raise_error(code: int, message: str, query_id: Optional[str] = None) -> None:
    """抛出一个 :class:`MiniChError`。

    写成函数而不是直接 ``raise``,是因为在**表达式内部**也能用::

        x if x > 0 else raise_error(ARGUMENT_OUT_OF_BOUND, "...")

    (Python 里 ``raise`` 是语句,不能放进条件表达式;函数可以。)
    """
    raise MiniChError(message, code=code, query_id=query_id)
