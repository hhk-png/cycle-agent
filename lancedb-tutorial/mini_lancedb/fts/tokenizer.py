"""FTS 分词管线：从"一段文本"到"一串词项（token）"。

全文检索的第一性问题是：**查询串和文档串必须被同一套规则切成同一批词项**。
只要两边切的词项对不上，倒排索引就永远查不到东西 —— 而且这种错误是静默的：
索引建得出来、查得动、只是结果为空。所以 LanceDB 把一个可配置的
``TokenizerConfig`` 写进索引元数据，这样"打开索引"时能知道它当初是怎么切的。

## 管线结构

真实 LanceDB 的分词器（``lance-index/src/scalar/inverted/tokenizer.rs``）是一条
**基础分词器 + 一串过滤器** 的链，本模块照抄这个形状：

```
             ┌─────────────── 过滤器链（可按开关跳过） ───────────────┐
原始文本 ──► │ ascii 折叠 ► 基础分词 ► 小写化 ► 去停用词 ► 词干化 ► 长度过滤 │ ──► 词项流
             └───────────────────────────────────────────────────┘
                          ▲
                   base_tokenizer: simple / whitespace / ngram / raw
```

每一步都可独立开关，这正是本模块的核心价值：教程里可以把每一个开关
单独拨一下，看词项流怎么变（``analyze()`` 就是为这件事准备的）。

## 几个刻意的取舍（都会在下面逐个说清）

1. **长度过滤是"丢弃"而不是"截断"。** tantivy（Lance 的算法母本）的
   ``RemoveLongFilter::limit(40)`` 把超长词**整个丢掉**；Lucene 的
   ``StandardTokenizer.maxTokenLength`` 是**截断到 40 字符**。截断会让
   两个不同的长词变成同一个词项（假匹配），丢弃只会降低召回。我们跟 tantivy。
2. **位置"留洞"（position increment），不是"压缩"的。** 位置 = 词项在
   **基础分词之后、过滤之前** 的序号。所以 ``"the quick brown"`` 里
   ``the`` 虽然被停用词表吃掉了，``quick`` 的位置仍然是 1、``brown`` 是 2。
   这不是浪费：不压缩才能让短语查询正确 —— 查询 ``"quick brown"``
   要求文档里这两个词的位置差为 1，于是它**不会**匹配原文
   ``quick the brown``（位置差是 2）。tantivy 选择压缩，Lucene 的
   ``StopFilter(enablePositionIncrements=true)`` 选择留洞，我们跟 Lucene，
   因为"短语查询悄悄多匹配出文档"是比"索引大几个字节"严重得多的 bug。
3. **CJK 走"字符二元组"（bigram），不是真分词。** 真实 LanceDB 用
   jieba（中文）/ lindera（日韩）做词法分析；教学实现里带不起词典，
   于是退化成 **CJK 连续段的字符二元组**：``"中国人民"`` →
   ``中国 / 国人 / 人民``。这是中文检索的经典做法（Lucene 的 CJKAnalyzer
   也是这个思路），效果远好于按字切，但会带来"需要短语查询才能精确匹配
   多字词"的后果 —— 这一点在 inverted.py 里会用到。
4. **ASCII 折叠是"逐字符"做的，并且记录字符映射。** 折叠会改变字符串长度
   （``"ß" → "ss"``），如果不记映射，词项的字节偏移就会全错。
   偏移量在"高亮命中的那段原文"时必须是对的，所以这里宁可多存一张表。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import NotSupportedError, SchemaError

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


# ---------------------------------------------------------------------------
# CJK 判定
# ---------------------------------------------------------------------------

#: 我们会按"字符二元组"处理的连续字符范围。
#: 刻意**不含** CJK 标点（U+3000..U+303F）和全角形式（U+FF00..U+FFEF）：
#: 那些位置上的字符大多是标点或全角拉丁字母，把它们并进 CJK 段会让
#: "你好，世界" 产生 ``好，`` / ``，世`` 这类纯噪声的二元组。
CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # 谚文字母
    (0x3040, 0x30FF),    # 平假名 + 片假名
    (0x3130, 0x318F),    # 谚文兼容字母
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区（最常用）
    (0xAC00, 0xD7AF),    # 谚文音节
    (0xF900, 0xFAFF),    # CJK 兼容汉字
    (0x20000, 0x2FA1F),  # CJK 扩展 B ~ F
)


def is_cjk(ch: str) -> bool:
    """这个字符是不是"没有空格分隔、需要切词"的表意文字。"""

    if not ch:
        return False
    cp = ord(ch[0])
    for lo, hi in CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _is_word_char(ch: str) -> bool:
    """基础分词器眼里的"词内字符"。

    用 ``str.isalnum()`` 而不是 ASCII 字母表，是为了让 ``"café"``、
    ``"naïve"`` 这类词保持完整（UAX#29 的词边界也是这么定的）。
    额外把 ``_`` 当成词内字符：``foo_bar`` 在编程语言语境里是一个整体，
    拆成三个词反而更容易误匹配。
    """

    return ch.isalnum() or ch == "_"


# ---------------------------------------------------------------------------
# ASCII 折叠
# ---------------------------------------------------------------------------

#: NFD 分解处理不了的那些字符（它们本身不是"基字符 + 组合记号"）。
#: 真实 Lucene 的 ``ASCIIFoldingFilter`` 有一张完整映射表（覆盖西里尔、
#: 希腊、数学符号等），这里只挑最常见的一批 —— 教学实现够用，
#: 缺口是"显式"的而不是隐藏的。
_ASCII_FOLD_EXTRA: Dict[str, str] = {
    "ß": "ss", "ẞ": "SS",
    "Æ": "AE", "æ": "ae",
    "Œ": "OE", "œ": "oe",
    "Ø": "O", "ø": "o",
    "Đ": "D", "đ": "d",
    "Ð": "D", "ð": "d",
    "Þ": "TH", "þ": "th",
    "Ł": "L", "ł": "l",
    "Ħ": "H", "ħ": "h",
    "Ŋ": "N", "ŋ": "n",
    "Ŧ": "T", "ŧ": "t",
    "ı": "i",
    "ƒ": "f",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-",
    " ": " ",  # 不换行空格
}


def fold_ascii(text: str) -> str:
    """把带变音符的拉丁字母压成 ASCII：``"Café"`` → ``"Cafe"``。

    做法是 Unicode 的标准套路 **NFD 分解 + 去掉组合记号**：

    ``é`` 在 NFD 下是 ``e`` + U+0301（combining acute accent），
    把 ``unicodedata.combining(c) != 0`` 的字符全部丢掉即可。
    这个套路的好处是**不用维护映射表**就能覆盖整个拉丁字母系统；
    坏处是它对 ``ß``/``ø``/``æ`` 这些"没有分解形式"的字符无效，
    所以要额外补一张小表（见 ``_ASCII_FOLD_EXTRA``）。

    注意最后**没有**再 NFC 归并：我们的目的是拿纯 ASCII，不是拿标准形。
    """

    out: List[str] = []
    for ch in text:
        extra = _ASCII_FOLD_EXTRA.get(ch)
        if extra is not None:
            out.append(extra)
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        out.append(stripped)
    return "".join(out)


def _fold_with_spans(text: str) -> Tuple[str, List[Tuple[int, int]]]:
    """折叠成 ASCII，同时记录"折叠后的第 j 个字符来自原文的哪一段"。

    返回 ``(folded, spans)``，``spans[j] == (start_char, end_char)`` 是原文里的
    字符下标区间。为什么要这么麻烦：折叠会改变长度 —— ``"ß"`` 一个字符
    变成 ``"ss"`` 两个字符 —— 如果不记录映射，算出来的字节偏移就是错的，
    而这正是"在原文里高亮命中片段"时唯一不能错的东西。

    纯组合记号（比如独立出现的声调符）会被丢掉，但它的字符区间会**并进前
    一个字符**，这样任何覆盖到该位置的词项仍然能圈进那个符号，不会漏字节。
    """

    folded_chars: List[str] = []
    spans: List[Tuple[int, int]] = []
    for i, ch in enumerate(text):
        extra = _ASCII_FOLD_EXTRA.get(ch)
        if extra is not None:
            piece = extra
        else:
            decomposed = unicodedata.normalize("NFD", ch)
            piece = "".join(c for c in decomposed if not unicodedata.combining(c))
        if not piece:
            if spans:
                spans[-1] = (spans[-1][0], i + 1)
            continue
        for c in piece:
            folded_chars.append(c)
            spans.append((i, i + 1))
    return "".join(folded_chars), spans


def _utf8_prefix(text: str) -> List[int]:
    """``prefix[i]`` = 前 ``i`` 个字符的 UTF-8 字节长度。

    词项的偏移量用**字节**而不是字符：Lance 文件里存的是字节，
    "高亮第 3~7 个字节"才是数据层能直接用的坐标。
    """

    out = [0] * (len(text) + 1)
    pos = 0
    for i, ch in enumerate(text):
        pos += len(ch.encode("utf-8"))
        out[i + 1] = pos
    return out


# ---------------------------------------------------------------------------
# 停用词
# ---------------------------------------------------------------------------

#: 英文停用词表（约 180 个）。
#: 来源是通用的英文功能词清单（冠词/介词/代词/助动词/连词及其缩写形式）。
#: 这些词在语料里出现频率极高、区分度极低，留着只会：
#: ① 撑大 postings；② 让 BM25 的 IDF 被高频词拉低整体判别力。
ENGLISH_STOP_WORDS: frozenset = frozenset(
    """
    a about above after again against all am an and any are aren't as at be
    because been before being below between both but by can cannot can't could
    couldn't did didn't do does doesn't doing don't down during each few for
    from further had hadn't has hasn't have haven't having he he'd he'll he's
    her here here's hers herself him himself his how how's i i'd i'll i'm i've
    if in into is isn't it it's its itself just let's me more most mustn't my
    myself no nor not of off on once only or other ought our ours ourselves out
    over own same shan't she she'd she'll she's should shouldn't so some such
    than that that's the their theirs them themselves then there there's these
    they they'd they'll they're they've this those through to too under until
    up very was wasn't we we'd we'll we're we've were weren't what what's when
    when's where where's which while who who's whom why why's with won't would
    wouldn't you you'd you'll you're you've your yours yourself yourselves
    """.split()
)


# ---------------------------------------------------------------------------
# Porter Stemmer（经典 Porter 1980 算法）
# ---------------------------------------------------------------------------


class PorterStemmer:
    """Porter 词干提取算法的经典实现（步骤 1a ~ 5b）。

    为什么值得手写一遍：它是**规则型**词干化的祖师爷，一行数据库都不需要，
    纯靠对英文构词法的观察。核心概念只有三个：

    * ``m`` —— 词干里 "元音-辅音" 交替的对数，即 **VC 序列的个数**。
      ``m == 0`` 表示词干没有元音核心（像 ``tr``），``m == 1`` 是 ``tree``
      这种，``m == 2`` 是 ``trouble`` 这种。贴''e''、去重辅音等规则
      都以 "``m > 1``（词够长）" 为门槛，防止把短词削得面目全非。
    * ``vowel_in_stem`` —— 词干里还有没有元音。
      ``"ed"/"ing"`` 只有在词干含元音时才删，否则会把 ``"sing"`` 削成 ``"s"``。
    * ``cvc`` —— 结尾是不是"辅音-元音-辅音"且末辅音不是 ``w/x/y``。
      这类词（``hop``、``run``）要补回一个 ``e`` 或双写末辅音，
      即 ``hop → hopping`` 而不是 ``hop → hoping``。

    实现刻意保持**索引可变、原地修改字符串**的风格（对照 Martin Porter 的
    官方 C 实现）：每一步都是"试着匹配后缀，命中就把 ``k`` 往回拉"。
    ``k0`` 是词干起点，``k`` 是当前末尾，``j`` 是"去掉后缀后新的末尾"。
    这种指针写法是词干化算法的标准表述，比用正则重写更能看清算法本身。

    已知的边界：Porter 对**已经**是词干的词应当是幂等的
    （``run → run``、``relat → relat``），我们在测试里覆盖了这一点。
    """

    __slots__ = ("b", "k", "k0", "j")

    def __init__(self) -> None:
        self.b: str = ""
        self.k: int = 0
        self.k0: int = 0
        self.j: int = 0

    # -- 主入口 -----------------------------------------------------------
    def stem(self, word: str) -> str:
        """返回 ``word`` 的词干。非 ASCII 词（含 CJK）原样返回。"""

        if not word:
            return word
        # 只对纯 ASCII 拉丁字母做词干化：Porter 的全部规则都建立在
        # "英文后缀"上，对别的字符集做匹配只会误伤。
        if not word.isascii() or not word.isalpha():
            return word
        self.b = word
        self.k = len(word) - 1
        self.k0 = 0
        self.j = 0
        # 长度 <= 2 的词一律不动（经典实现的门槛，避免 base/baseball 互串）
        if self.k <= self.k0 + 1:
            return self.b[: self.k + 1]
        self._step1ab()
        self._step1c()
        self._step2()
        self._step3()
        self._step4()
        self._step5()
        return self.b[self.k0 : self.k + 1]

    # -- 基础判定 ---------------------------------------------------------
    def _cons(self, i: int) -> bool:
        """第 ``i`` 个字符是不是辅音。

        ``y`` 是特例：它在词首算辅音（``yes``），在别处算元音
        （``happy`` 里的 ``y`` 是元音），所以要看它前一个字符。
        """

        c = self.b[i]
        if c in "aeiou":
            return False
        if c == "y":
            return True if i == self.k0 else not self._cons(i - 1)
        return True

    def _m(self) -> int:
        """数 ``[k0, j]`` 区间里 VC 序列的个数（经典的 measure 函数）。"""

        n = 0
        i = self.k0
        j = self.j
        if i > j:
            return 0
        while i <= j and self._cons(i):  # 跳过开头的辅音串
            i += 1
        i += 1  # 跨过第一个元音
        while True:
            while i <= j and not self._cons(i):  # 跳过元音串
                i += 1
            if i > j:
                return n
            i += 1
            n += 1
            while i <= j and self._cons(i):  # 跳过辅音串
                i += 1
            if i > j:
                return n
            i += 1

    def _vowel_in_stem(self) -> bool:
        for i in range(self.k0, self.j + 1):
            if not self._cons(i):
                return True
        return False

    def _doublec(self, j: int) -> bool:
        """``b[j]`` 是不是双写的辅音。"""

        if j < self.k0 + 1:
            return False
        if self.b[j] != self.b[j - 1]:
            return False
        return self._cons(j)

    def _cvc(self, i: int) -> bool:
        """``b[i-2..i]`` 是不是"辅-元-辅"且末辅音不是 ``w/x/y``。"""

        if (
            i < self.k0 + 2
            or not self._cons(i)
            or self._cons(i - 1)
            or not self._cons(i - 2)
        ):
            return False
        return self.b[i] not in "wxy"

    # -- 后缀操作 ---------------------------------------------------------
    def _ends(self, s: str) -> bool:
        """当前词是否以 ``s`` 结尾；命中则把 ``j`` 设为去掉后缀后的末尾。"""

        ln = len(s)
        if ln == 0 or self.b[self.k] != s[-1]:
            return False
        if ln > self.k - self.k0 + 1:  # 后缀比整词还长
            return False
        if self.b[self.k - ln + 1 : self.k + 1] != s:
            return False
        self.j = self.k - ln
        return True

    def _setto(self, s: str) -> None:
        """把 ``[j+1, k]`` 这段后缀替换成 ``s``。"""

        ln = len(s)
        self.b = self.b[: self.j + 1] + s + self.b[self.j + ln + 1 :]
        self.k = self.j + ln

    def _r(self, s: str) -> None:
        """仅在 ``m > 0`` 时替换后缀（防止把短词削光）。"""

        if self._m() > 0:
            self._setto(s)

    # -- 五个步骤 ---------------------------------------------------------
    def _step1ab(self) -> None:
        """1a 复数、1b 过去式/进行时。

        1a 的三条分支刻意按"最长匹配优先"排列：
        ``-sses → -ss``（caresses → caress）、``-ies → -i``（ponies → poni）、
        ``-s``（cats → cat）；遇到 ``-ss`` 结尾（caress）不动手，
        否则会把 ``class`` 削成 ``clas``。
        """

        if self.b[self.k] == "s":
            if self._ends("sses"):
                self.k -= 2
            elif self._ends("ies"):
                self._setto("i")
            elif self.b[self.k - 1] != "s":
                self.k -= 1
        if self._ends("eed"):
            # 1b 的特例：-eed 只在 m>0 时去掉一个 e（agreed → agre），
            # 否则保留（feed → feed），这是为了不破坏 feed/bleed 这类原形词。
            if self._m() > 0:
                self.k -= 1
        elif (self._ends("ed") or self._ends("ing")) and self._vowel_in_stem():
            self.k = self.j
            if self._ends("at"):
                self._setto("ate")
            elif self._ends("bl"):
                self._setto("ble")
            elif self._ends("iz"):
                self._setto("ize")
            elif self._doublec(self.k):
                # 双写末辅音（hopping → hop 而不是 hopp）
                self.k -= 1
                if self.b[self.k] in "lsz":
                    self.k += 1  # 但 l/s/z 例外（roll、press、fizz）
            elif self._m() == 1 and self._cvc(self.k):
                self._setto("e")  # hopping/hoping 的对称性：短词补回 e

    def _step1c(self) -> None:
        """词干含元音的 ``-y`` → ``-i``（happy → happi）。"""

        if self._ends("y") and self._vowel_in_stem():
            self.b = self.b[: self.k] + "i" + self.b[self.k + 1 :]

    def _step2(self) -> None:
        """把一组名词/形容词后缀映射到更短的形式，取代条件是 ``m > 0``。"""

        if self.k <= self.k0 + 1:
            return
        if self._ends("ational"):
            self._r("ate")
        elif self._ends("tional"):
            self._r("tion")
        elif self._ends("enci"):
            self._r("ence")
        elif self._ends("anci"):
            self._r("ance")
        elif self._ends("izer"):
            self._r("ize")
        elif self._ends("bli"):
            self._r("ble")  # 官方实现里为 "pilot" 语料补的一条
        elif self._ends("alli"):
            self._r("al")
        elif self._ends("entli"):
            self._r("ent")
        elif self._ends("eli"):
            self._r("e")
        elif self._ends("ousli"):
            self._r("ous")
        elif self._ends("ization"):
            self._r("ize")
        elif self._ends("ation"):
            self._r("ate")
        elif self._ends("ator"):
            self._r("ate")
        elif self._ends("alism"):
            self._r("al")
        elif self._ends("iveness"):
            self._r("ive")
        elif self._ends("fulness"):
            self._r("ful")
        elif self._ends("ousness"):
            self._r("ous")
        elif self._ends("aliti"):
            self._r("al")
        elif self._ends("iviti"):
            self._r("ive")
        elif self._ends("biliti"):
            self._r("ble")
        elif self._ends("logi"):
            self._r("log")

    def _step3(self) -> None:
        if self.k <= self.k0 + 1:
            return
        if self._ends("icate"):
            self._r("ic")
        elif self._ends("ative"):
            self._r("")
        elif self._ends("alize"):
            self._r("al")
        elif self._ends("iciti"):
            self._r("ic")
        elif self._ends("ical"):
            self._r("ic")
        elif self._ends("ful"):
            self._r("")
        elif self._ends("ness"):
            self._r("")

    def _step4(self) -> None:
        """删掉一批后缀，但要求 ``m > 1``（词得够长才敢删）。

        这里的 ``elif`` 链有个细节：``-ion`` 只有在前一个字符是 ``s`` 或 ``t``
        时才处理（``adoption`` 可以，``lion`` 不行）—— 这是为了防止把
        本来就是名词的词根削掉。
        """

        if self.k <= self.k0 + 1:
            return
        if self._ends("al"):
            pass
        elif self._ends("ance"):
            pass
        elif self._ends("ence"):
            pass
        elif self._ends("er"):
            pass
        elif self._ends("ic"):
            pass
        elif self._ends("able"):
            pass
        elif self._ends("ible"):
            pass
        elif self._ends("ant"):
            pass
        elif self._ends("ement"):
            pass
        elif self._ends("ment"):
            pass
        elif self._ends("ent"):
            pass
        elif self._ends("ion") and self.b[self.j] in "st":
            pass
        elif self._ends("ou"):
            pass
        elif self._ends("ism"):
            pass
        elif self._ends("ate"):
            pass
        elif self._ends("iti"):
            pass
        elif self._ends("ous"):
            pass
        elif self._ends("ive"):
            pass
        elif self._ends("ize"):
            pass
        else:
            return
        if self._m() > 1:
            self.k = self.j

    def _step5(self) -> None:
        """5a 删结尾 ``e``（m>1，或 m==1 且结尾不是 cvc）；5b 删双写 ``l``。"""

        self.j = self.k
        if self.b[self.k] == "e":
            a = self._m()
            if a > 1 or (a == 1 and not self._cvc(self.k - 1)):
                self.k -= 1
        if self.b[self.k] == "l" and self._doublec(self.k) and self._m() > 1:
            self.k -= 1


_STEMMER = PorterStemmer()


def stem_word(word: str) -> str:
    """模块级便利函数（内部复用同一个 ``PorterStemmer`` 实例）。"""

    return _STEMMER.stem(word)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

#: 四种基础分词器。名字和真实 ``TokenizerConfig.base_tokenizer`` 取值一致。
BASE_TOKENIZERS: Tuple[str, ...] = ("simple", "whitespace", "ngram", "raw")

#: 我们支持的词干化语言。真实 LanceDB 还支持 French/German/... 等。
SUPPORTED_STEMMER_LANGUAGES: Tuple[str, ...] = ("English",)


@dataclass
class TokenizerConfig:
    """分词器配置。字段名与默认值都对齐真实 LanceDB 的 ``TokenizerConfig``。

    这份配置会被**原样写进索引元数据**（``IndexMetadata.params["tokenizer"]``），
    因为"查询时拿哪套配置切词"是索引自描述的一部分：换一个 tokenizer 去查
    老索引，等于拿另一把钥匙开锁。

    :param base_tokenizer: ``"simple"``（UAX#29 风格词边界 + CJK 二元组）、
        ``"whitespace"``（按空白切）、``"ngram"``（字符 n 元组）、
        ``"raw"``（整段文本一个词项）。
    :param lower_case: 是否把词项转小写。关掉它意味着 ``Run`` 和 ``run``
        是两个词项 —— 只有大小写有含义的场景（比如代码标识符）才该关。
    :param stem: 是否做词干化。开启后 ``running`` 和 ``run`` 会命中同一个词项，
        召回变高、精度变低；关掉则相反。
    :param remove_stop_words: 是否丢掉停用词。见 :data:`ENGLISH_STOP_WORDS`。
    :param ascii_folding: 是否做 ASCII 折叠（``Café → Cafe``）。
    :param max_token_length: 单个词项的最大**字符**数，超过就丢弃。
    :param with_position: 是否记录词项位置。关掉能显著缩小索引体积，
        代价是**无法再做短语查询**（``"hello world"`` 只能退化成两个词的 AND）。
    :param ngram_min_length: ``base_tokenizer="ngram"`` 时的最短 n。
    :param ngram_max_length: 最长 n。两者相等就是固定长度的 n 元组。
    :param language: 语言，决定内置停用词表用哪一份。
    :param stemmer_language: 词干化用哪套规则。
    :param custom_stop_words: 自定义停用词表。**注意它是"取代"而不是"追加"** ——
        传了它就用它，内置表完全不参与。想追加的话自己拼：
        `` sorted(ENGLISH_STOP_WORDS | {"foo", "bar"})``。
    """

    base_tokenizer: str = "simple"
    lower_case: bool = True
    stem: bool = True
    remove_stop_words: bool = True
    ascii_folding: bool = False
    max_token_length: int = 40
    with_position: bool = True
    ngram_min_length: int = 3
    ngram_max_length: int = 3
    language: str = "English"
    stemmer_language: str = "English"
    custom_stop_words: Optional[Sequence[str]] = None

    def __post_init__(self) -> None:
        if self.base_tokenizer not in BASE_TOKENIZERS:
            raise SchemaError(
                f"未知的 base_tokenizer: {self.base_tokenizer!r}",
                available=list(BASE_TOKENIZERS),
            )
        if self.max_token_length <= 0:
            raise SchemaError("max_token_length 必须为正", got=self.max_token_length)
        if self.ngram_min_length <= 0 or self.ngram_max_length <= 0:
            raise SchemaError(
                "ngram 长度必须为正",
                min=self.ngram_min_length,
                max=self.ngram_max_length,
            )
        if self.ngram_min_length > self.ngram_max_length:
            raise SchemaError(
                "ngram_min_length 不能大于 ngram_max_length",
                min=self.ngram_min_length,
                max=self.ngram_max_length,
            )
        # 只有在真的要词干化时才校验语言 —— 不词干化时这个字段是惰性的，
        # 没必要因此拒绝一份本来能用的配置。
        if self.stem and self.stemmer_language not in SUPPORTED_STEMMER_LANGUAGES:
            raise NotSupportedError(
                f"stemmer_language={self.stemmer_language!r} 的词干化规则",
                hint=f"mini 实现只带了 Porter（English）；可选："
                f"{list(SUPPORTED_STEMMER_LANGUAGES)}，或设 stem=False",
            )
        if self.custom_stop_words is not None:
            self.custom_stop_words = sorted({str(w).lower() for w in self.custom_stop_words})

    # -- 派生量 -----------------------------------------------------------
    @property
    def stop_words(self) -> frozenset:
        """本次配置实际生效的停用词集合。"""

        if self.custom_stop_words is not None:
            return frozenset(self.custom_stop_words)
        if self.language.lower().startswith("english"):
            return ENGLISH_STOP_WORDS
        return frozenset()

    def to_dict(self) -> Dict[str, Any]:
        """序列化进索引元数据用。"""

        d = {
            "base_tokenizer": self.base_tokenizer,
            "lower_case": self.lower_case,
            "stem": self.stem,
            "remove_stop_words": self.remove_stop_words,
            "ascii_folding": self.ascii_folding,
            "max_token_length": self.max_token_length,
            "with_position": self.with_position,
            "ngram_min_length": self.ngram_min_length,
            "ngram_max_length": self.ngram_max_length,
            "language": self.language,
            "stemmer_language": self.stemmer_language,
        }
        if self.custom_stop_words is not None:
            d["custom_stop_words"] = list(self.custom_stop_words)
        return d

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "TokenizerConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in obj.items() if k in known})

    def describe(self) -> str:
        """一行摘要，便于在 ``index_stats()`` 里展示。"""

        parts = [f"{self.base_tokenizer}"]
        if self.lower_case:
            parts.append("lower")
        if self.remove_stop_words:
            parts.append("stop")
        if self.stem:
            parts.append(f"stem({self.stemmer_language})")
        if self.ascii_folding:
            parts.append("ascii")
        if self.with_position:
            parts.append("pos")
        return "+".join(parts)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


@dataclass
class Token:
    """一个词项在**原文**里的落点。

    * ``text`` —— 经过全管线之后的词项文本（可能被小写/词干化改过形）；
    * ``position`` —— 在**基础词项流**里的序号（从 0 开始，会因为被过滤掉的
      词项而**留空档**）：``"the quick brown"`` 得到的是 ``quick@1, brown@2``，
      不是 ``quick@0, brown@1``。空档是短语查询判对的关键（见模块 docstring）。
      当 ``with_position=False`` 时它是 ``None`` —— 位置是短语查询唯一的依据，
      不记录位置就等于放弃了短语查询能力。
    * ``start`` / ``end`` —— 在**原始文本**里的 UTF-8 **字节**偏移（左闭右开）。
      注意它指向原文，不是指向词项文本：所以 ``running → run`` 的时候，
      ``start/end`` 圈出来的仍然是原文那 7 个字符的 ``running``。
    * ``is_cjk`` —— 该词项是否来自 CJK 二元组切分（调试/统计用）。
    """

    text: str
    position: Optional[int] = None
    start: int = 0
    end: int = 0
    is_cjk: bool = False

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        pos = "-" if self.position is None else str(self.position)
        return f"Token({self.text!r}@{pos} [{self.start},{self.end}))"


# ---------------------------------------------------------------------------
# 分阶段的分析轨迹
# ---------------------------------------------------------------------------


@dataclass
class AnalysisTrace:
    """``analyze()`` 的返回值：管线每一阶段之后的词项流。

    这是调 FTS 的第一件工具。绝大部分"搜索查不到东西"的问题，
    看完这张表就明白是哪一步把词项吃掉了：是被停用词表吃了、被长度过滤吃了、
    还是被词干化成了另一个形（``"conditional" → "condit"``，
    于是用 ``"conditional"`` 去搜也是同一个词项，不会出错；
    但如果两边配置不一样，就会错）。
    """

    text: str
    folded: str
    base: List[str]
    lowered: List[str]
    no_stop_words: List[str]
    stemmed: List[str]
    final: List[str]
    tokens: List[Token]

    _STAGES = (
        ("原始文本", "text"),
        ("ascii 折叠", "folded"),
        ("基础分词", "base"),
        ("小写化", "lowered"),
        ("去停用词", "no_stop_words"),
        ("词干化", "stemmed"),
        ("长度过滤", "final"),
    )

    def render(self) -> str:
        lines: List[str] = []
        width = max(len(label) for label, _ in self._STAGES)
        for label, attr in self._STAGES:
            value = getattr(self, attr)
            shown = value if isinstance(value, str) else " ".join(
                f"{t!r}" for t in value
            )
            lines.append(f"  {label:<{width}} : {shown}")
        detail = ", ".join(
            f"{t.text}@{t.position}[{t.start},{t.end})" for t in self.tokens
        )
        lines.append(f"  {'最终词项':<{width}} : {detail}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - 只是打印
        return self.render()

    def counts(self) -> Dict[str, int]:
        """每一阶段剩余的词项数 —— 一眼看出"哪一步吃掉了大量词项"。"""

        return {
            "base": len(self.base),
            "lowered": len(self.lowered),
            "no_stop_words": len(self.no_stop_words),
            "stemmed": len(self.stemmed),
            "final": len(self.final),
        }


# ---------------------------------------------------------------------------
# 分词器
# ---------------------------------------------------------------------------


class Tokenizer:
    """可配置的分词管线。

    典型用法::

        tok = Tokenizer()                      # 全默认：simple + 小写 + 停用词 + 词干
        tok.tokenize("The RUNNING dogs")       # -> [run, dog]
        print(tok.analyze("The RUNNING dogs"))  # 看每一阶段发生了什么
    """

    def __init__(self, config: Optional[Any] = None) -> None:
        if config is None:
            config = TokenizerConfig()
        elif isinstance(config, Mapping):
            config = TokenizerConfig.from_dict(config)
        elif not isinstance(config, TokenizerConfig):
            raise SchemaError(
                "config 必须是 TokenizerConfig 或 dict",
                got=type(config).__name__,
            )
        self.config = config
        self.stop_words = config.stop_words
        self._stemmer = _STEMMER if config.stem else None

    # -- 便捷属性 ---------------------------------------------------------
    @property
    def with_position(self) -> bool:
        return self.config.with_position

    def __repr__(self) -> str:  # pragma: no cover
        return f"Tokenizer({self.config.describe()})"

    # -- 对外接口 ---------------------------------------------------------
    def _configured(self, config: Optional[Any]) -> "Tokenizer":
        """``config`` 给定时换一套配置跑 —— 只是给 ``analyze`` 对比用的小便利。"""

        if config is None or config is self.config:
            return self
        return Tokenizer(config)

    def tokenize(self, text: Optional[str], config: Optional[Any] = None) -> List[Token]:
        """完整管线，返回带位置和字节偏移的词项列表。"""

        return self._configured(config)._run(text, keep_stop_words=False)[1]

    def tokenize_query(
        self,
        text: Optional[str],
        config: Optional[Any] = None,
        *,
        keep_stop_words: bool = False,
    ) -> List[Token]:
        """查询侧分词。

        管线和 :meth:`tokenize` **完全一样**（这一点是硬要求：两边用不同配置
        是 FTS 最经典的静默 bug）。默认也**一样丢掉停用词** —— 这是短语查询
        能对上的前提。

        传 ``keep_stop_words=True`` 会保留停用词，只有两种场景该用它：
        调试时想看清完整管线，或者索引本身就是用
        ``remove_stop_words=False`` 建的（那种索引里才有停用词的 postings）。
        位置信息（留洞）在两种模式下都一样，所以偏移仍然是对的。
        """

        return self._configured(config)._run(text, keep_stop_words=keep_stop_words)[1]

    def token_texts(self, text: Optional[str], config: Optional[Any] = None) -> List[str]:
        """只要词项文本（建索引时最常用）。"""

        return [t.text for t in self.tokenize(text, config)]

    def analyze(self, text: Optional[str], config: Optional[Any] = None) -> AnalysisTrace:
        """跑一遍管线并保留每一阶段的中间结果。"""

        stages, tokens = self._configured(config)._run(text, keep_stop_words=False, trace=True)
        return AnalysisTrace(
            text=text or "",
            folded=stages["folded"],
            base=stages["base"],
            lowered=stages["lowered"],
            no_stop_words=stages["no_stop_words"],
            stemmed=stages["stemmed"],
            final=stages["final"],
            tokens=tokens,
        )

    # -- 管线实现 ---------------------------------------------------------
    def _run(
        self,
        text: Optional[str],
        *,
        keep_stop_words: bool,
        trace: bool = False,
    ) -> Tuple[Dict[str, Any], List[Token]]:
        cfg = self.config
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)

        # ① ASCII 折叠（字符级预处理）。折叠会改长度，所以要同时留下映射。
        if cfg.ascii_folding:
            folded, spans = _fold_with_spans(text)
        else:
            folded, spans = text, None

        # ② 基础分词：在**折叠后**的文本上切，返回 (词, 起始字符, 结束字符)。
        #    紧接着给每个词项打上它在基础流里的序号 —— 这就是它的 position。
        #    后面的过滤器只**删**元素，不改这个序号，于是被删掉的词项会留下
        #    空档（position increment），短语查询靠这个空档才能判对。
        base = self._base_tokenize(folded)
        base = [(t, a, b, i) for i, (t, a, b) in enumerate(base)]

        # ③ 小写化
        lowered = [(t.lower(), a, b, p) for t, a, b, p in base] if cfg.lower_case else list(base)

        # ④ 去停用词。**大小写不敏感地**比对：停用词表全是小写，
        #    关掉 lower_case 之后 "The" 也应该被认出来，否则关掉小写化
        #    会连带把停用词过滤废掉，两个开关就耦合了。
        if cfg.remove_stop_words and not keep_stop_words:
            words = self.stop_words
            stopped = [item for item in lowered if item[0].lower() not in words]
        else:
            stopped = list(lowered)

        # ⑤ 词干化
        if self._stemmer is not None:
            stemmed = [(self._stemmer.stem(t), a, b, p) for t, a, b, p in stopped]
        else:
            stemmed = list(stopped)

        # ⑥ 长度过滤（丢弃，不是截断 —— 见模块 docstring）与空串清理
        limit = cfg.max_token_length
        current = [(t, a, b, p) for t, a, b, p in stemmed if t and len(t) <= limit]

        # ⑦ 打包成 Token（偏移换算成原文的 UTF-8 字节坐标）
        if text:
            prefix = _utf8_prefix(text)
        else:
            prefix = [0]
        tokens: List[Token] = []
        for t, a, b, p in current:
            oa, ob = (spans[a][0], spans[b - 1][1]) if spans is not None else (a, b)
            tokens.append(
                Token(
                    text=t,
                    position=p if cfg.with_position else None,
                    start=prefix[min(oa, len(text))],
                    end=prefix[min(ob, len(text))],
                    is_cjk=any(is_cjk(c) for c in t),
                )
            )

        stages = {
            "folded": folded,
            "base": [t for t, _, _, _ in base],
            "lowered": [t for t, _, _, _ in lowered],
            "no_stop_words": [t for t, _, _, _ in stopped],
            "stemmed": [t for t, _, _, _ in stemmed],
            "final": [t.text for t in tokens],
        }
        return stages, tokens

    # -- 基础分词器 -------------------------------------------------------
    def _base_tokenize(self, text: str) -> List[Tuple[str, int, int]]:
        name = self.config.base_tokenizer
        if name == "simple":
            return _split_simple(text)
        if name == "whitespace":
            return _split_whitespace(text)
        if name == "ngram":
            return _split_ngram(
                text, self.config.ngram_min_length, self.config.ngram_max_length
            )
        if name == "raw":
            return [(text, 0, len(text))] if text else []
        raise SchemaError(f"未知的 base_tokenizer: {name!r}")  # pragma: no cover


def _split_simple(text: str) -> List[Tuple[str, int, int]]:
    """UAX#29 风格词边界的简化版。

    规则只有两条，但覆盖了绝大多数情况：

    * **ASCII/Unicode 字母数字连成一串** —— ``"hello_world2024"`` 是一个词项；
    * **CJK 连续段切成字符二元组** —— ``"中国人民银行"`` →
      ``中国/国人/人民/民银/银行``（5 个二元组）。
      单字段直接就是单字词项。
      注意**没有**尾随的单字 ``行``：二元组的循环是 ``range(i, j-1)``，
      n 个字的连续段产出 n-1 个二元组，首尾都不补边界符。
      （这个例子原来多写了一个 ``/行``，是照"n 个字切出 n 个东西"的
      直觉写的；实现里首尾都不补，所以是 n-1 个。）

    二元组而不是单字的理由：单字索引会让"中国"匹配到所有含"中"或"国"的文档，
    IDF 直接失效；二元组保留了局部顺序信息，精度高得多。
    代价是**多字词需要短语查询才能精确匹配**（见 inverted.py 的 PhraseQuery）。
    """

    out: List[Tuple[str, int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if is_cjk(ch):
            j = i
            while j < n and is_cjk(text[j]):
                j += 1
            if j - i == 1:
                out.append((text[i:j], i, j))
            else:
                for k in range(i, j - 1):
                    out.append((text[k : k + 2], k, k + 2))
            i = j
        elif _is_word_char(ch):
            j = i
            while j < n and _is_word_char(text[j]) and not is_cjk(text[j]):
                j += 1
            out.append((text[i:j], i, j))
            i = j
        else:
            i += 1
    return out


def _split_whitespace(text: str) -> List[Tuple[str, int, int]]:
    """按空白切。标点会留在词里（``"hello,"`` 是一个词项）—— 这是它和
    ``simple`` 的关键差别，也是"改 tokenizer 等于换索引"的典型例子。"""

    out: List[Tuple[str, int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        out.append((text[i:j], i, j))
        i = j
    return out


def _split_ngram(text: str, nmin: int, nmax: int) -> List[Tuple[str, int, int]]:
    """字符 n 元组。

    顺序对齐 Lucene 的 ``NGramTokenizer``：从每个起点出发，先吐短的再吐长的。
    空白**不做特殊处理** —— ``"ab cd"`` 的 3 元组里会出现 ``"b c"`` 这种
    带空格的词项。真实实现也是这样，别以为它是 bug。

    字符跨度用的是**字符**下标（不是字节），所以 CJK 文本的 n 元组
    也按字符算 —— 这是中文场景下唯一说得通的口径。
    """

    out: List[Tuple[str, int, int]] = []
    n = len(text)
    for i in range(n):
        for size in range(nmin, nmax + 1):
            if i + size <= n:
                out.append((text[i : i + size], i, i + size))
    return out


# ---------------------------------------------------------------------------
# 模块级便利函数
# ---------------------------------------------------------------------------


def tokenize(text: Optional[str], config: Optional[Any] = None) -> List[Token]:
    return Tokenizer(config).tokenize(text)


def tokenize_query(text: Optional[str], config: Optional[Any] = None) -> List[Token]:
    return Tokenizer(config).tokenize_query(text)


def analyze(text: Optional[str], config: Optional[Any] = None) -> AnalysisTrace:
    """分阶段打印一条文本的分词过程 —— 调试 FTS 的第一件事。"""

    return Tokenizer(config).analyze(text)
