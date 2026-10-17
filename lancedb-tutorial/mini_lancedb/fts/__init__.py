"""全文检索子包：分词管线（本包）+ 倒排索引（``mini_lancedb.index.inverted``）。

把分词单独放一层的理由：**它同时被写入路径和查询路径使用**。
放在索引里就会变成"索引内部私有的东西"，于是上层很容易拿另一套配置去查，
而那是 FTS 最难排查的一类 bug（索引建好了、查得动、就是查不出来）。
把它提到包的顶层，等于在代码结构上写明"这是两边共享的契约"。
"""

from .tokenizer import (
    BASE_TOKENIZERS,
    CJK_RANGES,
    ENGLISH_STOP_WORDS,
    AnalysisTrace,
    PorterStemmer,
    Token,
    Tokenizer,
    TokenizerConfig,
    analyze,
    fold_ascii,
    is_cjk,
    stem_word,
    tokenize,
    tokenize_query,
)

__all__ = [
    "Token",
    "TokenizerConfig",
    "Tokenizer",
    "AnalysisTrace",
    "PorterStemmer",
    "analyze",
    "tokenize",
    "tokenize_query",
    "stem_word",
    "fold_ascii",
    "is_cjk",
    "ENGLISH_STOP_WORDS",
    "CJK_RANGES",
    "BASE_TOKENIZERS",
]
