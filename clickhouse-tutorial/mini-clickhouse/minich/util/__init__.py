"""通用工具 —— 哈希、键、字符串转义、计时、日志。

这一层的特点:**它不知道"表"和"查询"是什么**。任何模块都可以依赖它,
它不依赖任何模块 —— 这是"基础设施层"应有的性质。

===========================  ==================================================
模块                          提供什么
===========================  ==================================================
``hashing``                  SipHash / xxHash / CRC(全部是真实算法)
``keys``                     分组键、排序列描述(``KeyDescription``)
``strings``                  TSV/CSV/SQL 转义、标识符引号、可读大小
``stopwatch``                ``Stopwatch`` / ``ProfileEvents`` / ``MetricsRegistry``
``logger``                   ``system.text_log`` 的写入端
``json_text``                JSON 编解码(含 NaN/Inf 的 ClickHouse 语义)
===========================  ==================================================
"""

from __future__ import annotations

from .hashing import (
    combine_hashes,
    crc32,
    hash_bytes,
    int_hash64,
    siphash64,
    siphash128,
    xxhash64,
)
from .json_text import from_json_text, json_escape_string, to_json_text
from .keys import NAN_KEY, KeyDescription, build_key, normalize_key_value
from .logger import LogLevel, LogRecord, Logger, all_loggers, get_logger, set_log_level
from .stopwatch import KNOWN_EVENTS, MetricsRegistry, ProfileEvents, Stopwatch, measure
from .strings import (
    escape_csv_field,
    escape_sql_string,
    escape_tsv,
    format_readable_quantity,
    format_readable_size,
    indent,
    parse_csv_line,
    quote_identifier,
    strip_identifier_quotes,
    unescape_tsv,
    wrap_text,
)

__all__ = [
    # hashing
    "siphash64",
    "siphash128",
    "xxhash64",
    "crc32",
    "int_hash64",
    "hash_bytes",
    "combine_hashes",
    # keys
    "normalize_key_value",
    "build_key",
    "KeyDescription",
    "NAN_KEY",
    # strings
    "escape_tsv",
    "unescape_tsv",
    "escape_csv_field",
    "parse_csv_line",
    "escape_sql_string",
    "quote_identifier",
    "strip_identifier_quotes",
    "indent",
    "wrap_text",
    "format_readable_size",
    "format_readable_quantity",
    # timing
    "Stopwatch",
    "ProfileEvents",
    "MetricsRegistry",
    "KNOWN_EVENTS",
    "measure",
    # logging
    "Logger",
    "LogRecord",
    "LogLevel",
    "get_logger",
    "set_log_level",
    "all_loggers",
    # json
    "to_json_text",
    "from_json_text",
    "json_escape_string",
]
