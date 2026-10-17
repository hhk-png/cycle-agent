"""向量化求值：一次算一整列，并且**显式实现 SQL 的三值逻辑**。

## 为什么 null 不能只是"值为 None"

SQL 的 ``NULL`` 不是"某个特殊值"，而是"**未知**"。整个求值过程因此是
**三值逻辑**（TRUE / FALSE / UNKNOWN）：

===========  ==========  ==========  ==========
``a AND b``  b = TRUE    b = FALSE   b = NULL
===========  ==========  ==========  ==========
a = TRUE     TRUE        FALSE       NULL
a = FALSE    FALSE       FALSE       FALSE
a = NULL     NULL        FALSE       NULL
===========  ==========  ==========  ==========

这个表和两条结果的**区别**是有实际后果的：

1. ``WHERE a = 1`` 里 ``a`` 是 NULL 的行**不出现**（UNKNOWN 不算 TRUE）；
2. ``WHERE NOT (a = 1)`` 里 ``a`` 是 NULL 的行**也不出现** —— 因为
   ``NOT NULL`` 还是 NULL。如果实现里把 NULL 当成 False，第 1 条碰巧对，
   第 2 条就会**多返回一批行**，而且没人会注意到，直到对账时差了几千行。
   这是 SQL 引擎最经典的静默错误之一，所以本模块不把 null 折进
   ``bool`` 数组，而是单独用一条 ``valid`` 位图（``valid[i] = False``
   表示第 i 行是 UNKNOWN）。

于是内存里每一列都是"值数组 + 有效性位图"两件套，和 Arrow 的
``values + null_bitmap`` 完全同构 —— 这也是为什么它能一路零拷贝
走到 Lance 文件里。

## NaN 不是 NULL（本实现与真实 LanceDB 的一处**故意不同**）

本实现规定：**任何比较只要有一边是 NaN，结果就是 NULL**（UNKNOWN）。
理由是 NaN 表达的是"这个值没法用"，把它当未知最安全，而且
``NOT (a = NaN)`` 也会正确地不返回任何行。

真实 LanceDB（底层是 Arrow 的 compare kernel）用的是**全序比较**：
``NaN = NaN`` 为 **true**，``NaN > 1.0`` 也为 **true**（NaN 被排在所有数后面）。
本机实测（lancedb 0.40.0）确实如此：``n > 1.0`` 会把 ``n = NaN`` 的行也选出来。
这是个很容易踩的坑 —— 你的向量列里只要有一个 NaN，范围过滤结果就会
多出一行。两种语义各有道理，这里选择"更安全"的那种并把它文档化。

## 空向量行 vs 含 NaN 的向量行

这两者经常被混为一谈：

* **空向量行**：整个 vector 列是 NULL（``EmptyTableError`` 时代的产物，
  现在由 ``on_bad_vectors`` 决定是填零、丢弃还是报错）。它的
  ``valid = False``，任何比较都是 NULL，不会被 ``WHERE`` 选中；
* **含 NaN 的行**：行本身存在，只是某个分量是 NaN。按上面的规则，
  对它做比较也是 NULL。

真正的物理区分可以在 :func:`evaluate` 的返回值上验证：
``valid`` 位图不同，``values`` 也不同。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..arrays import Array, NullArray, PrimitiveArray
from ..errors import ExpressionSyntaxError, PlanError, QueryError, TypeMismatchError
from ..record import RecordBatch
from ..types import (
    BOOL,
    DataType,
    FLOAT64,
    INT64,
    NULL as NULL_TYPE,
    Null as NullType,
    Struct as StructType,
    UTF8,
    Utf8 as Utf8Type,
    promote_types,
)
from .nodes import (
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    IsIn,
    IsNull,
    Like,
    Literal,
    UnaryOp,
    lit,
)

__all__ = [
    "ValueArray",
    "evaluate",
    "evaluate_predicate",
    "fold_constants",
    "like_to_regex",
    "compile_like",
    "LikeMatcher",
    "array_to_value",
]


# ---------------------------------------------------------------------------
# 带有效性位图的一列值
# ---------------------------------------------------------------------------


class ValueArray(np.ndarray):
    """一列值 = ``np.ndarray`` + ``valid`` 位图 + 逻辑类型。

    刻意做成 :class:`numpy.ndarray` 的**子类**而不是另起一个容器类，
    理由是下游全是 numpy 代码：``np.flatnonzero(result)``、``mask &= other``
    这些写法不用改。额外信息挂在属性上：

    * :attr:`valid` —— ``True`` 表示这一行有值；``False`` 表示 UNKNOWN/NULL；
    * :attr:`values` —— 底层的**平凡** ndarray（不带位图信息）；
    * :attr:`type` —— 逻辑类型，用于决定后续算子怎么算。

    注意：numpy 的 ufunc 只认底层数组，**不会**传播 ``valid``。
    本模块因此从不直接对 :class:`ValueArray` 调 ufunc，一律先取
    :attr:`values` 算完再显式 :meth:`wrap` 回去 —— 让"哪一步丢了 NULL"
    在代码里是显式的。
    """

    # 注意：ndarray 子类不能加 __slots__，否则挂不上 valid/type 属性

    def __array_finalize__(self, obj: Any) -> None:
        if obj is None:
            return
        self.valid = getattr(obj, "valid", None)
        self.type = getattr(obj, "type", NULL_TYPE)

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def wrap(cls, values: np.ndarray, valid: Any, type: DataType) -> "ValueArray":
        base = np.asarray(values)
        out = base.view(cls)
        out.valid = np.asarray(valid, dtype=bool)
        out.type = type
        return out

    @classmethod
    def all_valid(cls, values: Any, type: DataType) -> "ValueArray":
        arr = np.asarray(values)
        return cls.wrap(arr, np.ones(len(arr), dtype=bool), type)

    # -- 访问 -------------------------------------------------------------
    @property
    def values(self) -> np.ndarray:
        """底层平凡 ndarray（去掉子类视图）。"""

        return self.view(np.ndarray)

    @property
    def nulls(self) -> np.ndarray:
        """``True`` 表示这一行是 NULL —— 和 ``arrays.Array.nulls`` 同一约定。"""

        return ~self.valid

    @property
    def num_nulls(self) -> int:
        return int((~self.valid).sum())

    def is_null_at(self, i: int) -> bool:
        return not bool(self.valid[i])

    def to_pylist(self) -> List[Any]:
        """带 null 的 Python 列表 —— 和 :meth:`mini_lancedb.arrays.Array.to_pylist` 对齐。"""

        raw = self.values.tolist()
        return [v if ok else None for v, ok in zip(raw, self.valid)]

    def to_array(self) -> Array:
        """转回 :mod:`mini_lancedb.arrays` 里的 Arrow 风格数组。"""

        from ..arrays import array_from_pylist

        t = self.type
        if isinstance(t, NullType):
            return NullArray(len(self))
        try:
            return array_from_pylist(t, self.to_pylist())
        except (TypeMismatchError, Exception):  # noqa: B014 - 类型对不上就退回推断
            from ..types import infer_type

            return array_from_pylist(infer_type(self.to_pylist()), self.to_pylist())

    def __repr__(self) -> str:
        head = self.to_pylist()[:4]
        tail = "" if len(self) <= 4 else ", ..."
        return f"ValueArray({self.type.name}, {head}{tail}, nulls={self.num_nulls})"


def _object_array(values: Sequence[Any]) -> np.ndarray:
    out = np.empty(len(values), dtype=object)
    for i, v in enumerate(values):
        out[i] = v
    return out


def _fill_object(n: int, value: Any) -> np.ndarray:
    """把同一个 Python 对象填满 n 行（list/dict 要用逐元素赋值，否则 numpy 会广播成二维）。"""

    if isinstance(value, (list, tuple, dict, np.ndarray)):
        out = np.empty(n, dtype=object)
        for i in range(n):
            out[i] = list(value) if isinstance(value, (list, tuple, np.ndarray)) else dict(value)
        return out
    try:
        return np.full(n, value, dtype=object)
    except (TypeError, ValueError):  # pragma: no cover - 兜底
        out = np.empty(n, dtype=object)
        for i in range(n):
            out[i] = value
        return out


# ---------------------------------------------------------------------------
# 求值上下文
# ---------------------------------------------------------------------------


class _Ctx:
    """求值上下文：行数 + 按列名取列（带缓存）。

    ``batch is None`` 时表示"常量求值"（常量折叠用），此时任何列引用都是
    非法访问 —— 因为调用方已经保证这棵树里没有列引用。
    """

    __slots__ = ("batch", "n", "_cache")

    def __init__(self, batch: Optional[RecordBatch], n: Optional[int] = None) -> None:
        if batch is None:
            self.n = 1 if n is None else int(n)
        else:
            self.n = batch.num_rows if n is None else int(n)
        self.batch = batch
        self._cache: Dict[str, ValueArray] = {}

    def column(self, name: str) -> ValueArray:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        if self.batch is None:
            raise PlanError(f"常量求值里不能出现列引用 {name!r}")
        try:
            arr = self.batch.column(name)
        except Exception as exc:
            raise PlanError(
                f"没有名为 {name!r} 的列",
                available=self.batch.column_names,
                hint="Valid fields are "
                + ", ".join(f"`{n}`" for n in self.batch.column_names),
            ) from exc
        v = array_to_value(arr)
        self._cache[name] = v
        return v


def array_to_value(arr: Array) -> ValueArray:
    """把 :mod:`mini_lancedb.arrays` 里的数组转成 :class:`ValueArray`。

    * 定长标量列直接借用底层 buffer（真零拷贝，只多一份布尔位图）；
    * 变长/嵌套列走 ``to_pylist()``（Arrow 里这些列本来就得解 offsets）；
    * **字符串列里 null 行的底层值会被换成 ``""``** —— 因为 numpy 的字符串
      函数遇到 ``None`` 会直接抛错。正确性由 ``valid`` 位图保证：
      这些位置永远不会被当成"有值"来用。
    """

    from ..arrays import _BinaryBackedArray

    n = len(arr)
    valid = np.ones(n, dtype=bool) if arr.nulls is None else ~arr.nulls

    if isinstance(arr, NullArray):
        return ValueArray.wrap(_object_array([None] * n), np.zeros(n, dtype=bool), NULL_TYPE)

    if isinstance(arr, PrimitiveArray):
        return ValueArray.wrap(arr.data, valid, arr.type)

    if isinstance(arr, _BinaryBackedArray):
        raw = arr.to_pylist()
        blank: Any = "" if isinstance(arr.type, Utf8Type) else b""
        filled = [v if v is not None else blank for v in raw]
        return ValueArray.wrap(_object_array(filled), valid, arr.type)

    # 其余（List / FixedSizeList / Struct / Map / Dictionary）统一走 pylist
    return ValueArray.wrap(_object_array(arr.to_pylist()), valid, arr.type)


# ---------------------------------------------------------------------------
# LIKE → 正则
# ---------------------------------------------------------------------------


def _like_tokens(pattern: str, escape: Optional[str]) -> List[Tuple[Optional[str], str]]:
    """把 LIKE 模式切成 ``(通配符类型 or None, 字符)`` 列表。

    ``escape`` 后面那个字符永远被当成**字面量**，即使它是 ``%`` 或 ``_``。
    """

    if escape is not None and len(escape) != 1:
        raise ExpressionSyntaxError("ESCAPE 只能是单个字符")
    out: List[Tuple[Optional[str], str]] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape is not None and ch == escape:
            i += 1
            if i >= len(pattern):
                raise ExpressionSyntaxError(
                    "LIKE 模式以转义符结尾，转义符后面必须有字符",
                    expression=pattern,
                    position=i - 1,
                )
            out.append((None, pattern[i]))
            i += 1
            continue
        if ch in ("%", "_"):
            out.append((ch, ch))
        else:
            out.append((None, ch))
        i += 1
    return out


def like_to_regex(
    pattern: str, escape: Optional[str] = None, *, case_insensitive: bool = False
) -> "re.Pattern[str]":
    """把 SQL 的 LIKE 模式编译成正则。

    映射规则：

    * ``%`` → ``.*``（**包括换行**，所以必须开 ``re.DOTALL``；
      Python 的 ``.`` 默认不匹配 ``\\n``，忘了这个 flag 的话
      "多行文本里匹配不到"这种 bug 极难定位）；
    * ``_`` → ``.``（同样受 DOTALL 影响）；
    * 其它字符一律 ``re.escape``，包括正则元字符 ``. ^ $ * + ? ( ) [ ] { } |``；
    * ``escape`` 后面的字符按字面量处理。

    整个模式用 ``^...$`` 锚定。
    """

    flags = re.DOTALL | (re.IGNORECASE if case_insensitive else 0)
    body: List[str] = []
    for kind, ch in _like_tokens(pattern, escape):
        if kind == "%":
            body.append(".*")
        elif kind == "_":
            body.append(".")
        else:
            body.append(re.escape(ch))
    return re.compile("^" + "".join(body) + "$", flags)


class LikeMatcher:
    """LIKE 的"分类执行器"：先看模式长什么样，再挑最便宜的算法。

    这是**索引能不能用上**的分水岭：

    * ``kind == "eq"`` —— 模式里一个通配符都没有（``'abc'``）。这是等值比较，
      能吃 BTREE 索引，也能直接变成 zone map 上的 ``col = 'abc'``；
    * ``kind == "prefix"`` —— ``'abc%'``。前缀是**有序**的，
      BTREE 上就是一次范围查询 ``[abc, abc\\uffff]``；退一步说，
      ``startswith`` 也比跑正则快一个数量级；
    * ``kind == "contains"`` —— ``'%abc%'``。无解，只能全表扫
      （BTREE 帮不上忙，得靠 NGRAM 索引或者倒排/FTS）；
    * 其它 → 只能编译正则逐行跑。

    :meth:`match` 对前三种给出**向量化**实现，并且保证结果和正则路径**逐位相同**
    （测试里用生成器随机造模式做过对照）。
    """

    __slots__ = ("pattern", "escape", "kind", "literal", "regex", "case_insensitive")

    def __init__(
        self,
        pattern: str,
        escape: Optional[str] = None,
        *,
        case_insensitive: bool = False,
    ) -> None:
        self.pattern = pattern
        self.escape = escape
        self.case_insensitive = case_insensitive
        self.regex = like_to_regex(pattern, escape, case_insensitive=case_insensitive)
        self.kind, self.literal = self._classify()

    def _classify(self) -> Tuple[str, Optional[str]]:
        toks = _like_tokens(self.pattern, self.escape)
        wilds = [(k, c) for k, c in toks if k is not None]
        text = "".join(c for k, c in toks if k is None)

        if not wilds:
            return "eq", text
        if len(toks) >= 2 and toks[-1][0] == "%" and all(k is None for k, _ in toks[:-1]):
            return "prefix", text
        if len(toks) >= 2 and toks[0][0] == "%" and all(k is None for k, _ in toks[1:]):
            return "suffix", text
        if (
            len(toks) >= 3
            and toks[0][0] == "%"
            and toks[-1][0] == "%"
            and all(k is None for k, _ in toks[1:-1])
        ):
            return "contains", text
        if len(wilds) == 1 and wilds[0][0] == "%":
            # ``'a%b'``：既不是前缀也不是后缀，但可以用"前缀+后缀"两段筛
            return "prefix_suffix", None
        return "regex", None

    # -- 单值（非字面量模式时逐行调用） ------------------------------------
    def match_one(self, value: Any) -> bool:
        return self.regex.match(value if isinstance(value, str) else str(value)) is not None

    # -- 向量化 -----------------------------------------------------------
    def match(self, values: np.ndarray) -> np.ndarray:
        arr = np.asarray(values)
        if arr.dtype.kind not in ("O", "U", "S"):
            raise PlanError(
                "LIKE 只能用在字符串列上",
                got=str(arr.dtype),
            )
        n = len(arr)
        if self.kind == "regex":
            out = np.zeros(n, dtype=bool)
            rx = self.regex
            for i in range(n):
                out[i] = rx.match(arr[i] if isinstance(arr[i], str) else str(arr[i])) is not None
            return out

        text = np.asarray(arr, dtype=object if arr.dtype.kind == "O" else arr.dtype)
        lit = self.literal if self.literal is not None else ""
        if self.case_insensitive:
            text = _str_map(text, str.lower)
            lit = lit.lower()

        if self.kind == "eq":
            return np.asarray(np.equal(text, lit), dtype=bool)
        if self.kind == "prefix":
            return _str_startswith(text, lit)
        if self.kind == "suffix":
            return _str_endswith(text, lit)
        if self.kind == "contains":
            return _str_contains(text, lit)
        # prefix_suffix（``'a%b'``）：先按前缀筛，再在命中的行里查后缀。
        # **必须同时要求长度 >= len(前缀) + len(后缀)** —— 否则 ``'ba'`` 会被
        # 判成匹配 ``'ba%ba'``（它确实既以 ba 开头又以 ba 结尾，只是前后缀
        # 重叠了）。这个 bug 只在"待匹配串比前后缀之和还短"时出现，
        # 是最容易被漏掉的一类。
        pre, _, suf = self.pattern.partition("%")
        if self.escape:
            pre = _unescape(pre, self.escape)
            suf = _unescape(suf, self.escape)
        mask = _str_startswith(text, pre)
        if pre or suf:
            need = len(pre) + len(suf)
            mask = mask & (_str_lengths(text) >= need)
        idx = np.flatnonzero(mask)
        if len(idx):
            tail = np.asarray(text[idx], dtype=object)
            mask[idx] = _str_endswith(tail, suf)
        return mask


def _unescape(text: str, escape: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(text):
        if text[i] == escape and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def compile_like(
    pattern: str, escape: Optional[str] = None, *, case_insensitive: bool = False
) -> LikeMatcher:
    return LikeMatcher(pattern, escape, case_insensitive=case_insensitive)


def _str_startswith(text: np.ndarray, prefix: str) -> np.ndarray:
    """``np.char.startswith`` 走 numpy 的 C 实现；对象数组不支持时退回循环。"""

    if prefix == "":
        return np.ones(len(text), dtype=bool)
    try:
        return np.char.startswith(np.asarray(text, dtype=str), prefix)
    except (TypeError, ValueError):  # pragma: no cover - 老版本 numpy
        return np.fromiter(
            (s.startswith(prefix) for s in text), dtype=bool, count=len(text)
        )


def _str_endswith(text: np.ndarray, suffix: str) -> np.ndarray:
    if suffix == "":
        return np.ones(len(text), dtype=bool)
    try:
        return np.char.endswith(np.asarray(text, dtype=str), suffix)
    except (TypeError, ValueError):  # pragma: no cover
        return np.fromiter(
            (s.endswith(suffix) for s in text), dtype=bool, count=len(text)
        )


def _str_contains(text: np.ndarray, needle: str) -> np.ndarray:
    if needle == "":
        return np.ones(len(text), dtype=bool)
    try:
        return np.char.find(np.asarray(text, dtype=str), needle) >= 0
    except (TypeError, ValueError):  # pragma: no cover
        return np.fromiter((needle in s for s in text), dtype=bool, count=len(text))


def _str_lengths(text: np.ndarray) -> np.ndarray:
    try:
        return np.char.str_len(np.asarray(text, dtype=str))
    except (TypeError, ValueError):  # pragma: no cover
        return np.fromiter((len(s) for s in text), dtype=np.int64, count=len(text))


def _str_map(text: np.ndarray, fn: Callable[[str], str]) -> np.ndarray:
    return np.fromiter(
        (fn(s) if isinstance(s, str) else str(s) for s in text), dtype=object, count=len(text)
    )


# ---------------------------------------------------------------------------
# 三值逻辑
# ---------------------------------------------------------------------------


def _logical_not(a: ValueArray) -> ValueArray:
    """``NOT`` 只翻值，不动有效性位图 —— ``NOT NULL`` 还是 NULL。"""

    return ValueArray.wrap(~np.asarray(a.values, dtype=bool), a.valid, BOOL)


def _true_mask(a: ValueArray) -> np.ndarray:
    return a.valid & np.asarray(a.values, dtype=bool)


def _false_mask(a: ValueArray) -> np.ndarray:
    return a.valid & ~np.asarray(a.values, dtype=bool)


def _logical_and(a: ValueArray, b: ValueArray) -> ValueArray:
    """三值 AND。

    * FALSE 是**吸收元**：只要有一边是 FALSE，结果就是 FALSE（不管另一边是不是 NULL）；
    * TRUE 要求两边都是 TRUE；
    * 其余情况是 NULL。
    """

    a_true, a_false = _true_mask(a), _false_mask(a)
    b_true, b_false = _true_mask(b), _false_mask(b)
    is_false = a_false | b_false
    is_true = a_true & b_true
    return ValueArray.wrap(is_true, is_true | is_false, BOOL)


def _logical_or(a: ValueArray, b: ValueArray) -> ValueArray:
    """三值 OR。TRUE 是吸收元。"""

    a_true, a_false = _true_mask(a), _false_mask(a)
    b_true, b_false = _true_mask(b), _false_mask(b)
    is_true = a_true | b_true
    is_false = a_false & b_false
    return ValueArray.wrap(is_true, is_true | is_false, BOOL)


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------

_COMPARE_FN: Dict[str, Callable[[Any, Any], Any]] = {
    "=": np.equal,
    "!=": np.not_equal,
    "<": np.less,
    "<=": np.less_equal,
    ">": np.greater,
    ">=": np.greater_equal,
}


def _coerce_pair(a: ValueArray, b: ValueArray) -> Tuple[np.ndarray, np.ndarray]:
    """把两侧对齐到同长度后返回底层值数组。

    正常情况下两侧长度都是"批的行数"（字面量在 :func:`_eval` 里就被广播成
    整列了）；这里额外接受"长度为 1"的输入，是为了让
    :func:`fold_constants` 的 1 行上下文也能直接复用同一套代码。
    """

    n = max(len(a), len(b))

    def grow(v: ValueArray) -> np.ndarray:
        if len(v) == n:
            return v.values
        if len(v) == 1:
            return np.full(n, v.values[0], dtype=v.values.dtype)
        raise QueryError("表达式两侧长度不一致", left=len(a), right=len(b))

    return grow(a), grow(b)


def _combine_valid(a: ValueArray, b: ValueArray, n: int) -> np.ndarray:
    """两侧都"有值"的行 —— 三值逻辑里绝大多数算子的 valid 精确就是它。"""

    valid = np.ones(n, dtype=bool)
    for v in (a, b):
        if len(v) == n:
            valid &= v.valid
        elif len(v) == 1:
            valid &= np.full(n, bool(v.valid[0]))
    return valid


def _nan_mask(v: ValueArray) -> Optional[np.ndarray]:
    arr = v.values
    if arr.dtype.kind == "f":
        return np.isnan(arr)
    return None


def _compare(op: str, a: ValueArray, b: ValueArray) -> ValueArray:
    """比较运算（含 NaN 规则）。

    步骤：先算"两边都有值"的行，再把**任一边是 NaN** 的行标成 NULL。
    后者是本实现与真实 LanceDB 的语义分叉点，见模块文档。

    把 NaN 行标成 NULL 而不是把 ``values`` 置成 False，是为了让
    ``NOT`` 之后的行为也正确：``NOT (a = NaN)`` 同样是 NULL（行被排除），
    而不是"补回来"。对 ``WHERE`` 来说两者都表现为 False（NULL 行被剔除，
    见 :func:`evaluate_predicate`），但 ``valid`` 位图保留了这个区别，
    用户想看原始三值结果时可以自己检查。
    """

    av, bv = _coerce_pair(a, b)
    n = len(av)
    valid = _combine_valid(a, b, n)

    fn = _COMPARE_FN[op]
    with np.errstate(invalid="ignore"):
        raw = fn(av, bv)
    result = np.asarray(raw, dtype=bool)

    # NaN：任何比较只要有一边是 NaN，结果就是 NULL
    for v in (a, b):
        nm = _nan_mask(v)
        if nm is None:
            continue
        if len(nm) == n:
            valid = valid & ~nm
        elif len(nm) == 1 and bool(nm[0]):
            valid = np.zeros(n, dtype=bool)
    result = result & valid
    return ValueArray.wrap(result, valid, BOOL)


# ---------------------------------------------------------------------------
# 算术
# ---------------------------------------------------------------------------


def _is_integer_like(t: DataType) -> bool:
    dt = t.numpy_dtype_or_none
    return dt is not None and dt.kind in ("i", "u")


def _arith(op: str, a: ValueArray, b: ValueArray) -> ValueArray:
    """算术运算。

    三条必须写下来的规则：

    1. **除零 → NULL**，不是异常也不是 inf。SQL 里 ``1/0`` 没有定义值，
       向量化执行时抛异常会让整个查询挂掉，返回 NULL 才是可用的行为
       （也能让 ``WHERE b <> 0 AND a/b > 1`` 这种写法自然成立）；
       为避开 numpy 的 ``RuntimeWarning``，除数为 0 的位置先换成 1 再算。
    2. **两个整数相除做截断整除**（``7/2 = 3``，``-7/2 = -3``），和
       DataFusion / PostgreSQL 一致；只要有一边是浮点就走真除法。
       这里用"先 floor_divide 再按符号校正"实现，避免 ``astype(float64)``
       在超过 2^53 的整数上丢精度。
    3. ``%`` 用 ``np.fmod``（余数符号跟着被除数），和 SQL 的 ``%`` 一致；
       numpy 的 ``np.mod`` 是"结果符号跟除数"，是另一个东西。
    """

    if isinstance(a.type, NullType) or isinstance(b.type, NullType):
        t = b.type if isinstance(a.type, NullType) else a.type
        return ValueArray.wrap(_fill_object(len(a), None), np.zeros(len(a), dtype=bool), t)

    av, bv = _coerce_pair(a, b)
    valid = _combine_valid(a, b, len(av))
    if av.dtype.kind not in "iufb" or bv.dtype.kind not in "iufb":
        raise PlanError(
            f"运算符 {op} 不能作用在 {a.type.name} 和 {b.type.name} 上",
            hint="字符串拼接请用 ||",
        )

    both_int = _is_integer_like(a.type) and _is_integer_like(b.type)

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if op == "+":
            out = av + bv
        elif op == "-":
            out = av - bv
        elif op == "*":
            out = av * bv
        elif op == "/":
            if both_int:
                safe = np.where(bv == 0, 1, bv)
                q = np.floor_divide(av, safe)
                r = av - q * safe
                neg = (r != 0) & ((av < 0) != (safe < 0))
                out = q + neg.astype(q.dtype)
            else:
                safe = np.where(bv == 0, 1.0, bv)
                out = av / safe
        elif op == "%":
            safe = np.where(bv == 0, 1, bv)
            out = np.fmod(av, safe)
        else:  # pragma: no cover - 调用方已限定
            raise PlanError(f"不支持的算术运算符 {op!r}")

    zero_div = np.zeros(len(out), dtype=bool)
    if op in ("/", "%"):
        zero_div = np.asarray(bv) == 0
    valid = valid & ~zero_div
    out = np.asarray(out)
    if out.dtype.kind == "f":
        out = np.where(valid, out, 0.0)
    else:
        out = np.where(valid, out, 0)
    result_type = a.type if both_int else (promote_types(a.type, b.type) or FLOAT64)
    if both_int and op == "/":
        result_type = promote_types(a.type, b.type) or a.type
    return ValueArray.wrap(out, valid, result_type)


def _concat(a: ValueArray, b: ValueArray) -> ValueArray:
    """``||`` 字符串拼接：NULL 传播（``'a' || NULL`` 是 NULL，不是 ``'a'``）。"""

    av, bv = _coerce_pair(a, b)
    valid = _combine_valid(a, b, len(av))
    out = np.empty(len(av), dtype=object)
    for i in np.flatnonzero(valid):
        x, y = av[i], bv[i]
        if isinstance(x, bytes) or isinstance(y, bytes):
            out[i] = (x if isinstance(x, bytes) else str(x).encode()) + (
                y if isinstance(y, bytes) else str(y).encode()
            )
        else:
            out[i] = ("" if x is None else str(x)) + ("" if y is None else str(y))
    for i in np.flatnonzero(~valid):
        out[i] = ""
    return ValueArray.wrap(out, valid, UTF8)


# ---------------------------------------------------------------------------
# 比较 / 逻辑 的求值入口
# ---------------------------------------------------------------------------


def _eval_binary(node: BinaryOp, ctx: _Ctx) -> ValueArray:
    op = node.op

    # 关键字形式（手工构造 BinaryOp 时才走这里；解析器产出专用节点）
    if op in ("LIKE", "NOT LIKE"):
        return _eval_like(Like(node.left, node.right, op == "NOT LIKE"), ctx)
    if op in ("IN", "NOT IN"):
        values = _in_values_from_expr(node.right)
        return _eval_isin(IsIn(node.left, values, op == "NOT IN"), ctx)
    if op in ("IS", "IS NOT"):
        if not (isinstance(node.right, Literal) and node.right.value is None):
            raise PlanError("IS 只支持 IS NULL / IS NOT NULL")
        return _eval_isnull(IsNull(node.left, op == "IS NOT"), ctx)

    # AND / OR 短路：左操作数是常量时根本不求右操作数
    if op in ("AND", "OR"):
        if _is_constant(node.left):
            left = _eval(node.left, ctx)
            const_true = bool(left.valid[0]) and bool(left.values[0])
            const_false = bool(left.valid[0]) and not bool(left.values[0])
            if op == "AND":
                if const_false:
                    return ValueArray.wrap(
                        np.zeros(ctx.n, dtype=bool), np.ones(ctx.n, dtype=bool), BOOL
                    )
                if const_true:
                    return _eval(node.right, ctx)
            else:
                if const_true:
                    return ValueArray.wrap(
                        np.ones(ctx.n, dtype=bool), np.ones(ctx.n, dtype=bool), BOOL
                    )
                if const_false:
                    return _eval(node.right, ctx)
        left = _eval(node.left, ctx)
        # 运行期短路：左边整列都是 FALSE（AND）或整列都是 TRUE（OR）时，
        # 右边的结果已经无关紧要 —— 不用算了
        if op == "AND" and not _true_mask(left).any() and _false_mask(left).any():
            return left
        if op == "OR" and not _false_mask(left).any() and _true_mask(left).any():
            return left
        right = _eval(node.right, ctx)
        return _logical_and(left, right) if op == "AND" else _logical_or(left, right)

    left = _eval(node.left, ctx)
    right = _eval(node.right, ctx)

    if op in _COMPARE_FN:
        return _compare(op, left, right)
    if op == "||":
        return _concat(left, right)
    return _arith(op, left, right)


def _in_values_from_expr(expr: Expr) -> List[Expr]:
    """``x IN <expr>`` 里右边那个表达式的取值列表。"""

    if isinstance(expr, Literal):
        if isinstance(expr.value, (list, tuple)):
            return [lit(v) for v in expr.value]
        return [expr]
    if isinstance(expr, IsIn):  # pragma: no cover - 防御
        return list(expr.values)
    raise ExpressionSyntaxError("IN 的右边必须是值列表")


def _eval_isin(node: IsIn, ctx: _Ctx) -> ValueArray:
    """``IN`` / ``NOT IN``。

    关键：这**不是**简单的"或起来"。SQL 把它展开成::

        x IN (a, b)      <=>  x = a OR x = b
        x NOT IN (a, b)  <=>  x <> a AND x <> b

    于是 ``x NOT IN (1, NULL)`` 永远是 NULL/FALSE —— 因为
    ``x <> NULL`` 是 NULL，而 NULL 在 AND 里会吸收掉一切 TRUE。
    这是 SQL 用户最容易踩的坑之一（"为什么过滤条件写了 NOT IN 反而一行都不剩"）。
    本实现按展开式求值，所以行为与标准一致。
    """

    child = _eval(node.child, ctx)
    values = [_eval(v, ctx) for v in node.values]

    # 直接按 SQL 的展开式求值：IN 是等值比较的 OR，NOT IN 是不等比较的 AND。
    # 这样 NULL 的传播就是三值逻辑自动的结果，不需要任何特判 ——
    # 特判恰恰是 NOT IN + NULL 这类 bug 的温床。
    if node.negated:
        acc = _compare("!=", child, values[0])
        for v in values[1:]:
            acc = _logical_and(acc, _compare("!=", child, v))
        return acc
    acc = _compare("=", child, values[0])
    for v in values[1:]:
        acc = _logical_or(acc, _compare("=", child, v))
    return acc


def _eval_isnull(node: IsNull, ctx: _Ctx) -> ValueArray:
    """``IS [NOT] NULL`` —— **永远返回 TRUE/FALSE，绝不返回 NULL**。

    ``x IS NULL`` 的答案一定是确定的：要么它（在 SQL 意义上）是未知，
    要么不是。"NULL IS NULL" 为 TRUE 就是这条规则的结果，
    也是它和 ``x = NULL``（永远是 NULL）最本质的区别。
    """

    child = _eval(node.child, ctx)
    out = ~child.valid if not node.negated else child.valid
    return ValueArray.wrap(out, np.ones(len(out), dtype=bool), BOOL)


def _eval_like(node: Like, ctx: _Ctx) -> ValueArray:
    child = _eval(node.child, ctx)
    arr = child.values
    if arr.dtype.kind not in ("O", "U", "S"):
        raise PlanError("LIKE 只能用在字符串列上", got=str(arr.dtype))
    text = np.asarray(arr, dtype=object) if arr.dtype.kind == "O" else arr

    lit_pattern = node.literal_pattern()
    if lit_pattern is not None:
        matcher = compile_like(lit_pattern, node.escape)
        mask = matcher.match(text)
        valid = child.valid.copy()
    else:
        pat = _eval(node.pattern, ctx)
        mask = np.zeros(len(child), dtype=bool)
        valid = child.valid & pat.valid
        cache: Dict[str, LikeMatcher] = {}
        pv = np.asarray(pat.values, dtype=object)
        for i in np.flatnonzero(valid):
            key = pv[i] if isinstance(pv[i], str) else str(pv[i])
            m = cache.get(key)
            if m is None:
                m = compile_like(key, node.escape)
                cache[key] = m
            mask[i] = m.match_one(text[i])

    if node.negated:
        mask = ~mask
    mask = mask & valid
    return ValueArray.wrap(mask, valid, BOOL)


def _eval_between(node: Between, ctx: _Ctx) -> ValueArray:
    """``x BETWEEN lo AND hi`` ≡ ``x >= lo AND x <= hi``（闭区间，NULL 照常传播）。"""

    child = _eval(node.child, ctx)
    low = _eval(node.low, ctx)
    high = _eval(node.high, ctx)
    inner = _logical_and(_compare(">=", child, low), _compare("<=", child, high))
    return _logical_not(inner) if node.negated else inner


# ---------------------------------------------------------------------------
# 一元
# ---------------------------------------------------------------------------


def _eval_unary(node: UnaryOp, ctx: _Ctx) -> ValueArray:
    operand = _eval(node.operand, ctx)
    if node.op == "NOT":
        return _logical_not(operand)
    arr = operand.values
    if arr.dtype.kind not in "iufb":
        raise PlanError(f"一元 {node.op} 不能作用在 {operand.type.name} 上")
    if node.op == "+":
        return ValueArray.wrap(+arr, operand.valid, operand.type)
    return ValueArray.wrap(-arr, operand.valid, operand.type)


# ---------------------------------------------------------------------------
# CAST
# ---------------------------------------------------------------------------

_INT_KINDS = ("i", "u")


def _eval_cast(node: Cast, ctx: _Ctx) -> ValueArray:
    """类型转换：**转不动的那一行变成 NULL**，不抛异常。

    这是 TRY_CAST 的语义。选它的原因是向量化执行里"某一行脏数据"
    不该让整条查询失败（真实 Lance 读一个坏页也是跳过 + 记数，不是崩）。
    代价是错误被悄悄吞掉，所以 ``cast`` 之后最好再 check 一下 null 数。
    """

    value = _eval(node.child, ctx)
    target = node.target
    src_type = value.type
    if src_type == target:
        return value

    n = len(value)
    src = value.values
    valid = value.valid.copy()
    if isinstance(src_type, NullType):
        return ValueArray.wrap(_fill_object(n, None), np.zeros(n, dtype=bool), target)
    if isinstance(target, NullType):
        return ValueArray.wrap(_fill_object(n, None), np.zeros(n, dtype=bool), target)

    tdt = target.numpy_dtype_or_none
    is_text_target = isinstance(target, Utf8Type)
    is_text_source = src.dtype.kind in ("O", "U", "S")

    # 字符串 → 数值：逐行解析，坏的置 NULL
    if is_text_target and not is_text_source:
        out = np.empty(n, dtype=object)
        for i in range(n):
            out[i] = "" if not valid[i] else str(src[i])
        return ValueArray.wrap(out, valid, target)

    if is_text_source and tdt is not None and tdt.kind in "iuf":
        out = np.zeros(n, dtype=tdt)
        for i in range(n):
            if not valid[i]:
                continue
            raw = src[i]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                valid[i] = False
                continue
            try:
                out[i] = float(raw) if tdt.kind == "f" else int(raw)
            except (TypeError, ValueError):
                try:
                    out[i] = float(raw)
                except (TypeError, ValueError):
                    valid[i] = False
        if tdt.kind in _INT_KINDS:
            out = np.where(np.isfinite(out), out, 0)
            try:
                out = out.astype(tdt)
            except (OverflowError, ValueError):  # pragma: no cover
                valid = np.zeros(n, dtype=bool)
        return ValueArray.wrap(out, valid, target)

    if isinstance(target, type(BOOL)):
        return ValueArray.wrap(np.asarray(src).astype(bool) & valid, valid, target)

    if tdt is not None:
        out = np.asarray(src)
        if out.dtype.kind in ("O",):
            out = np.asarray(
                [0 if v is None else v for v in out], dtype=tdt
            )
        out = out.astype(tdt, copy=True)
        return ValueArray.wrap(np.where(valid, out, 0), valid, target)

    raise TypeMismatchError(f"暂不支持 CAST 到 {target.name}")


# ---------------------------------------------------------------------------
# 标量函数
# ---------------------------------------------------------------------------


def _arg(arg: ValueArray, n: int) -> ValueArray:
    if len(arg) == n:
        return arg
    if len(arg) == 1:
        return ValueArray.wrap(
            np.full(n, arg.values[0], dtype=arg.values.dtype),
            np.full(n, bool(arg.valid[0])),
            arg.type,
        )
    raise QueryError("参数长度不一致", expected=n, got=len(arg))


def _string_arg(v: ValueArray) -> np.ndarray:
    arr = v.values
    if arr.dtype.kind == "O":
        return arr
    return np.asarray(arr, dtype=object)


def _iter_lists(v: ValueArray) -> List[Optional[list]]:
    out: List[Optional[list]] = []
    for i, item in enumerate(v.values):
        if not v.valid[i]:
            out.append(None)
            continue
        if item is None:
            out.append(None)
        elif isinstance(item, (list, tuple, np.ndarray)):
            out.append(list(item))
        else:
            out.append(None)
    return out


def _fn_length(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    out = np.zeros(n, dtype=np.int64)
    for i, item in enumerate(a.values):
        if not a.valid[i] or item is None:
            continue
        if isinstance(item, (list, tuple, np.ndarray, dict, str, bytes)):
            out[i] = len(item)
        else:
            out[i] = len(str(item))
    return ValueArray.wrap(out, a.valid.copy(), INT64)


def _fn_text_map(fn: Callable[[str], str]) -> Callable[[List[ValueArray], int], ValueArray]:
    def impl(args: List[ValueArray], n: int) -> ValueArray:
        a = _arg(args[0], n)
        out = np.empty(n, dtype=object)
        for i, item in enumerate(a.values):
            out[i] = fn(item) if a.valid[i] and isinstance(item, str) else ""
        return ValueArray.wrap(out, a.valid.copy(), UTF8)

    return impl


def _fn_trim(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    chars = None
    if len(args) > 1:
        c = _arg(args[1], n)
        chars = [str(x) for x in c.values]
    out = np.empty(n, dtype=object)
    for i, item in enumerate(a.values):
        if not a.valid[i] or not isinstance(item, str):
            out[i] = ""
            continue
        out[i] = item.strip(chars[i] if chars is not None else None)
    return ValueArray.wrap(out, a.valid.copy(), UTF8)


def _fn_lrtrim(left: bool) -> Callable[[List[ValueArray], int], ValueArray]:
    def impl(args: List[ValueArray], n: int) -> ValueArray:
        a = _arg(args[0], n)
        chars = None
        if len(args) > 1:
            chars = [str(x) for x in _arg(args[1], n).values]
        out = np.empty(n, dtype=object)
        for i, item in enumerate(a.values):
            if not a.valid[i] or not isinstance(item, str):
                out[i] = ""
                continue
            c = chars[i] if chars is not None else None
            out[i] = item.lstrip(c) if left else item.rstrip(c)
        return ValueArray.wrap(out, a.valid.copy(), UTF8)

    return impl


def _fn_string_pred(fn: Callable[[str, str], bool]) -> Callable[[List[ValueArray], int], ValueArray]:
    """``starts_with`` / ``ends_with`` / ``contains`` —— 两个字符串参数，返回布尔。"""

    def impl(args: List[ValueArray], n: int) -> ValueArray:
        a = _arg(args[0], n)
        b = _arg(args[1], n)
        valid = a.valid & b.valid
        out = np.zeros(n, dtype=bool)
        for i in range(n):
            if not valid[i]:
                continue
            x, y = a.values[i], b.values[i]
            try:
                out[i] = fn(x, y)
            except (TypeError, ValueError):
                out[i] = False
        return ValueArray.wrap(out, valid, BOOL)

    return impl


def _fn_array_pred(args: List[ValueArray], n: int) -> ValueArray:
    """``array_contains`` / ``array_has_any``：数组列里有没有这些值。

    第一个参数必须是列表列（``List`` / ``FixedSizeList``，也就是向量列）。
    后面的参数可以是单个值，也可以是一个列表字面量（``array_has_any(v, [1,2])``）。
    NULL 数组列的结果是 NULL —— 注意不是 FALSE，``WHERE NOT array_contains(...)``
    也不会选中它。
    """

    a = _arg(args[0], n)
    items = _iter_lists(a)
    extras = [_arg(e, n) for e in args[1:]]
    out = np.zeros(n, dtype=bool)
    for i in range(n):
        if not a.valid[i] or items[i] is None:
            continue
        for e in extras:
            if not e.valid[i]:
                continue
            v = e.values[i]
            candidates = list(v) if isinstance(v, (list, tuple, np.ndarray)) else [v]
            if any(any(_scalar_eq(c, x) for x in items[i]) for c in candidates):
                out[i] = True
                break
    return ValueArray.wrap(out, a.valid.copy(), BOOL)


def _scalar_eq(a: Any, b: Any) -> bool:
    """元素相等的宽松比较：numpy 标量和 Python 标量要对得上。"""

    try:
        return bool(a == b)
    except (TypeError, ValueError):  # pragma: no cover
        return False


def _fn_array_has_all(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    items = _iter_lists(a)
    wanted: List[List[Any]] = []
    for extra in args[1:]:
        e = _arg(extra, n)
        for i in range(n):
            v = e.values[i]
            while len(wanted) <= i:
                wanted.append([])
            if isinstance(v, (list, tuple, np.ndarray)):
                wanted[i].extend(list(v))
            elif e.valid[i]:
                wanted[i].append(v)
    out = np.zeros(n, dtype=bool)
    valid = a.valid.copy()
    for i in range(n):
        if not valid[i] or items[i] is None:
            continue
        need = wanted[i] if i < len(wanted) else []
        out[i] = all(any(_scalar_eq(w, x) for x in items[i]) for w in need)
    return ValueArray.wrap(out, valid, BOOL)


def _fn_abs(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    arr = a.values
    if arr.dtype.kind not in "iuf":
        raise PlanError("abs 只能作用在数值列上", got=str(arr.dtype))
    return ValueArray.wrap(np.abs(arr), a.valid.copy(), a.type)


def _fn_round(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    arr = np.asarray(a.values, dtype=np.float64)
    digits = 0
    if len(args) > 1:
        d = _arg(args[1], n)
        digits = int(d.values[0]) if len(d) else 0
    out = np.round(arr, digits) if digits else np.round(arr)
    return ValueArray.wrap(out, a.valid.copy(), FLOAT64)


def _fn_floor_ceil(up: bool) -> Callable[[List[ValueArray], int], ValueArray]:
    def impl(args: List[ValueArray], n: int) -> ValueArray:
        a = _arg(args[0], n)
        arr = np.asarray(a.values, dtype=np.float64)
        out = np.ceil(arr) if up else np.floor(arr)
        return ValueArray.wrap(out, a.valid.copy(), FLOAT64)

    return impl


def _fn_sqrt(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    arr = np.asarray(a.values, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        out = np.sqrt(arr)
    valid = a.valid & np.isfinite(out)
    return ValueArray.wrap(np.where(valid, out, 0.0), valid, FLOAT64)


def _fn_power(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    b = _arg(args[1], n)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.power(np.asarray(a.values, dtype=np.float64), np.asarray(b.values, dtype=np.float64))
    valid = a.valid & b.valid & np.isfinite(out)
    return ValueArray.wrap(np.where(valid, out, 0.0), valid, FLOAT64)


def _fn_coalesce(args: List[ValueArray], n: int) -> ValueArray:
    """``coalesce`` 是唯一一个**能消除 NULL** 的普通标量函数。"""

    cols = [_arg(a, n) for a in args]
    dtype = None
    for c in cols:
        if c.values.dtype.kind == "O":
            dtype = None
            break
        if dtype is None:
            dtype = c.values.dtype
        else:
            dtype = np.result_type(dtype, c.values.dtype)
    out = np.zeros(n, dtype=dtype) if dtype is not None else np.empty(n, dtype=object)
    done = np.zeros(n, dtype=bool)
    for c in cols:
        take = ~done & c.valid
        if not take.any():
            continue
        if out.dtype == object:
            src = np.asarray(c.values, dtype=object)
            out[take] = src[take]
        else:
            out[take] = np.asarray(c.values)[take]
        done |= take
    valid = done
    rtype = cols[0].type
    from ..types import promote_types as _promote

    for c in cols[1:]:
        rtype = _promote(rtype, c.type) or rtype
    return ValueArray.wrap(out, valid, rtype)


def _fn_nullif(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    b = _arg(args[1], n)
    eq = _compare("=", a, b)
    valid = a.valid & ~_true_mask(eq)
    return ValueArray.wrap(a.values, valid, a.type)


def _fn_concat(args: List[ValueArray], n: int) -> ValueArray:
    cols = [_arg(a, n) for a in args]
    out = np.empty(n, dtype=object)
    valid = np.ones(n, dtype=bool)
    for c in cols:
        valid &= c.valid
    for i in range(n):
        out[i] = "" if not valid[i] else "".join(str(c.values[i]) for c in cols)
    return ValueArray.wrap(out, valid, UTF8)


def _fn_substring(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    start = _arg(args[1], n)
    length = _arg(args[2], n) if len(args) > 2 else None
    out = np.empty(n, dtype=object)
    valid = a.valid & start.valid & (length.valid if length is not None else a.valid)
    for i in range(n):
        if not valid[i] or not isinstance(a.values[i], str):
            out[i] = ""
            continue
        # SQL 的 substring 下标从 1 开始
        s = int(start.values[i]) - 1
        if s < 0:
            s = 0
        if length is None:
            out[i] = a.values[i][s:]
        else:
            out[i] = a.values[i][s : s + int(length.values[i])]
    return ValueArray.wrap(out, valid, UTF8)


def _fn_strpos(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    b = _arg(args[1], n)
    out = np.zeros(n, dtype=np.int64)
    valid = a.valid & b.valid
    for i in range(n):
        if not valid[i]:
            continue
        x, y = a.values[i], b.values[i]
        out[i] = (str(x).find(str(y)) + 1) if isinstance(x, str) and isinstance(y, str) else 0
    return ValueArray.wrap(out, valid, INT64)


def _fn_replace(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    b = _arg(args[1], n)
    c = _arg(args[2], n)
    out = np.empty(n, dtype=object)
    valid = a.valid & b.valid & c.valid
    for i in range(n):
        out[i] = (
            str(a.values[i]).replace(str(b.values[i]), str(c.values[i]))
            if valid[i]
            else ""
        )
    return ValueArray.wrap(out, valid, UTF8)


def _fn_extreme(largest: bool) -> Callable[[List[ValueArray], int], ValueArray]:
    def impl(args: List[ValueArray], n: int) -> ValueArray:
        cols = [_arg(a, n) for a in args]
        out = np.asarray(cols[0].values).copy()
        valid = cols[0].valid.copy()
        for c in cols[1:]:
            with np.errstate(invalid="ignore"):
                better = np.greater(c.values, out) if largest else np.less(c.values, out)
            take = c.valid & (better | ~valid)
            out = np.where(take, c.values, out)
            valid = valid | c.valid
        return ValueArray.wrap(out, valid, cols[0].type)

    return impl


def _fn_struct_field(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    key = _arg(args[1], n)
    out = np.empty(n, dtype=object)
    valid = a.valid.copy()
    field_type = NULL_TYPE
    if isinstance(a.type, StructType):
        try:
            field_type = a.type.field(str(key.values[0])).type
        except Exception:  # pragma: no cover
            field_type = NULL_TYPE
    for i in range(n):
        item = a.values[i]
        if not valid[i] or not isinstance(item, dict):
            out[i] = None
            valid[i] = False
            continue
        k = key.values[i]
        out[i] = item.get(k)
        if out[i] is None:
            valid[i] = False
    return ValueArray.wrap(out, valid, field_type)


def _fn_list_extract(args: List[ValueArray], n: int) -> ValueArray:
    a = _arg(args[0], n)
    idx = _arg(args[1], n)
    items = _iter_lists(a)
    out = np.empty(n, dtype=object)
    valid = np.zeros(n, dtype=bool)
    for i in range(n):
        if items[i] is None:
            out[i] = None
            continue
        k = int(idx.values[i])
        if k < 0:  # SQL 里负下标从尾部数（-1 是最后一个）
            k += len(items[i])
        if 0 <= k < len(items[i]):
            out[i] = items[i][k]
            valid[i] = True
        else:
            out[i] = None
    return ValueArray.wrap(out, valid, NULL_TYPE)


#: 函数分发表。``array_has_any`` / ``array_contains`` 共用一套"列表里有没有值"的实现。
_FUNCTION_IMPLS: Dict[str, Callable[[List[ValueArray], int], ValueArray]] = {
    "length": _fn_length,
    "char_length": _fn_length,
    "character_length": _fn_length,
    "array_length": _fn_length,
    "lower": _fn_text_map(str.lower),
    "upper": _fn_text_map(str.upper),
    "trim": _fn_trim,
    "ltrim": _fn_lrtrim(True),
    "rtrim": _fn_lrtrim(False),
    "starts_with": _fn_string_pred(lambda x, y: str(x).startswith(str(y))),
    "ends_with": _fn_string_pred(lambda x, y: str(x).endswith(str(y))),
    "contains": _fn_string_pred(lambda x, y: str(y) in str(x)),
    "array_contains": _fn_array_pred,
    "array_has_any": _fn_array_pred,
    "array_has_all": _fn_array_has_all,
    "abs": _fn_abs,
    "round": _fn_round,
    "floor": _fn_floor_ceil(False),
    "ceil": _fn_floor_ceil(True),
    "ceiling": _fn_floor_ceil(True),
    "sqrt": _fn_sqrt,
    "power": _fn_power,
    "coalesce": _fn_coalesce,
    "nullif": _fn_nullif,
    "concat": _fn_concat,
    "substring": _fn_substring,
    "substr": _fn_substring,
    "strpos": _fn_strpos,
    "replace": _fn_replace,
    "greatest": _fn_extreme(True),
    "least": _fn_extreme(False),
    "struct_field": _fn_struct_field,
    "list_extract": _fn_list_extract,
}


def _eval_function(node: FunctionCall, ctx: _Ctx) -> ValueArray:
    impl = _FUNCTION_IMPLS.get(node.name)
    if impl is None:
        raise PlanError(f"未实现的函数 {node.name!r}", available=sorted(_FUNCTION_IMPLS))
    args = [_eval(a, ctx) for a in node.args]
    return impl(args, ctx.n)


# ---------------------------------------------------------------------------
# 主分发
# ---------------------------------------------------------------------------


def _eval(expr: Expr, ctx: _Ctx) -> ValueArray:
    n = ctx.n
    if isinstance(expr, Literal):
        value = expr.value
        if value is None:
            return ValueArray.wrap(
                _fill_object(n, None), np.zeros(n, dtype=bool), expr.type
            )
        if isinstance(value, (list, tuple)):
            return ValueArray.wrap(
                _fill_object(n, list(value)), np.ones(n, dtype=bool), expr.type
            )
        dyn = expr.type.numpy_dtype_or_none
        if dyn is not None and dyn.kind in "iufb":
            return ValueArray.wrap(np.full(n, value, dtype=dyn), np.ones(n, dtype=bool), expr.type)
        return ValueArray.wrap(_fill_object(n, value), np.ones(n, dtype=bool), expr.type)

    if isinstance(expr, ColumnRef):
        return ctx.column(expr.name)

    if isinstance(expr, BinaryOp):
        return _eval_binary(expr, ctx)

    if isinstance(expr, UnaryOp):
        return _eval_unary(expr, ctx)

    if isinstance(expr, IsNull):
        return _eval_isnull(expr, ctx)

    if isinstance(expr, IsIn):
        return _eval_isin(expr, ctx)

    if isinstance(expr, Like):
        return _eval_like(expr, ctx)

    if isinstance(expr, Between):
        return _eval_between(expr, ctx)

    if isinstance(expr, Cast):
        return _eval_cast(expr, ctx)

    if isinstance(expr, FunctionCall):
        return _eval_function(expr, ctx)

    raise PlanError(f"无法求值的表达式节点 {type(expr).__name__}")


def evaluate(expr: Expr, batch: RecordBatch) -> ValueArray:
    """在一批数据上求值，返回一列值（``np.ndarray`` 子类 + ``valid`` 位图）。

    ``valid[i] = False`` 就是 SQL 的 NULL/UNKNOWN。谓词求值请用
    :func:`evaluate_predicate` —— 它会顺手把 NULL 行剔掉。
    """

    if not isinstance(expr, Expr):
        raise QueryError("evaluate 需要 Expr", got=type(expr).__name__)
    ctx = _Ctx(batch)
    if isinstance(expr, ColumnRef):
        # 常见快路径：整列取值只做一次视图转换
        return ctx.column(expr.name)
    return _eval(expr, ctx)


def evaluate_predicate(expr: Expr, batch: RecordBatch) -> np.ndarray:
    """把表达式当谓词求值，返回长度 = 行数的**布尔掩码**。

    ``WHERE`` 的语义是"只有 TRUE 才留下"，所以：

    * 值是 FALSE 的行 → 剔除；
    * 值是 **NULL** 的行 → **剔除**（这就是三值逻辑对查询结果最直接的影响，
      也是 ``WHERE NOT (a = 1)`` 和 ``WHERE a <> 1`` 在 ``a`` 有 NULL 时
      都会"少行"的原因 —— 两个条件都不返回 NULL 行）。
    """

    value = evaluate(expr, batch)
    if value.values.dtype.kind != "b":
        raise QueryError(
            "过滤条件必须返回布尔值",
            got=value.type.name,
            sql=expr.to_sql(),
        )
    return np.asarray(value.values, dtype=bool) & value.valid


# ---------------------------------------------------------------------------
# 常量折叠
# ---------------------------------------------------------------------------


def _is_constant(expr: Expr) -> bool:
    if isinstance(expr, Literal):
        return True
    if isinstance(expr, ColumnRef):
        return False
    return all(_is_constant(c) for c in expr.children)


def fold_constants(expr: Expr) -> Expr:
    """常量折叠：把"只由字面量构成"的子树算成一个字面量。

    为什么值得做？因为查询计划里这些子树**每批数据都要重算一遍**，
    而它们的答案其实在第一秒就确定了。折叠之后：

    * ``a > 1 + 2`` 变成 ``a > 3`` —— 顺带让索引改写能认出这个范围条件
      （:func:`mini_lancedb.expr.rewrite.to_index_expr` 只认"列 op 字面量"）；
    * ``NULL AND x`` 这类整棵子树直接塌缩，后面的求值都不用做。

    实现方式很朴素：自底向上重建，遇到全是常量的子树就丢进一个
    **1 行**的上下文里跑一遍真正的向量化求值器 —— 不另写一套标量求值，
    保证"折叠的结果"和"运行时的结果"用的是同一份语义（尤其三值逻辑）。
    """

    if isinstance(expr, (Literal, ColumnRef)):
        return expr

    rebuilt = _rebuild_children(expr, fold_constants)
    if _is_constant(rebuilt):
        return _to_literal(rebuilt)
    return rebuilt


def _to_literal(expr: Expr) -> Literal:
    ctx = _Ctx(None, 1)
    value = _eval(expr, ctx)
    if not bool(value.valid[0]):
        return Literal(None, expr.type)
    raw = value.values[0]
    if isinstance(raw, np.generic):
        raw = raw.item()
    if isinstance(raw, list):
        raw = list(raw)
    return Literal(raw, expr.type)


def _rebuild_children(expr: Expr, fn: Callable[[Expr], Expr]) -> Expr:
    """按节点类型重建，把 ``fn`` 作用到每个子节点上。"""

    from .nodes import serialize, deserialize

    def sub(e: Expr) -> Expr:
        return fn(e)

    if isinstance(expr, BinaryOp):
        return BinaryOp(expr.op, sub(expr.left), sub(expr.right), expr.type)
    if isinstance(expr, UnaryOp):
        return UnaryOp(expr.op, sub(expr.operand), expr.type)
    if isinstance(expr, IsNull):
        return IsNull(sub(expr.child), expr.negated)
    if isinstance(expr, IsIn):
        return IsIn(sub(expr.child), [sub(v) for v in expr.values], expr.negated)
    if isinstance(expr, Like):
        return Like(sub(expr.child), sub(expr.pattern), expr.negated, expr.escape)
    if isinstance(expr, Between):
        return Between(sub(expr.child), sub(expr.low), sub(expr.high), expr.negated)
    if isinstance(expr, Cast):
        return Cast(sub(expr.child), expr.target)
    if isinstance(expr, FunctionCall):
        return FunctionCall(expr.name, [sub(a) for a in expr.args], expr.type)
    return deserialize(serialize(expr))  # pragma: no cover - 防御
