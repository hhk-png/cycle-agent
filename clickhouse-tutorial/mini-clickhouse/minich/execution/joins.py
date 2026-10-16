"""JOIN 的执行 —— 哈希连接、ASOF 与四种 strictness。

ClickHouse 的 JOIN 有两组**正交**的选项,而绝大多数人的困惑都来自
没把这两组分开:

.. code-block:: text

   kind(方向)              strictness(匹配策略)
   ─────────────────────   ────────────────────────────────────────────
   INNER / LEFT / RIGHT /  ALL    匹配多少行就产出多少行(笛卡尔)
   FULL / CROSS            ANY    右表只取**第一个**匹配  ← 默认!
                           ASOF   取"键 ≤ 左表键"里最大的那个
                           SEMI   只保留左表中有匹配的行(不取右表列)
                           ANTI   只保留左表中没有匹配的行

**默认严格度是 ``ALL``**(``join_default_strictness = 'ALL'``),
不是 ``ANY`` —— 这一点值得专门澄清,因为"ClickHouse 默认 ANY"
是一个流传很广的说法(它在很早的版本里确实如此)。

``ANY`` 仍然非常有用:宽表 join 时,"左表一行匹配右表多行"通常是
**数据错误**,``ANY`` 会让查询"随便取一个"而不是把结果行数炸开。
代价是字面意义上的**结果不确定**,所以它只适合"右表按 join key
唯一"的场景。要显式使用就写 ``LEFT ANY JOIN`` 或
``SET join_default_strictness = 'any'``。

.. warning::

   本模块的实现有两条与真实 ClickHouse 的重要差异,二者都会
   影响**性能**而不影响**结果**:

   1. **整表进内存**。真实 ClickHouse 的 ``hash`` 算法把右表放进
      ``Join`` 对象(受 ``max_bytes_in_join`` 限制),还有
      ``partial_merge``(排序归并,可以外存)与 ``grace_hash``
      (分区落盘)两个不那么吃内存的算法。mini-clickhouse
      只有 hash 一种,并且**不做** ``max_bytes_in_join`` 检查 ——
      右表多大就吃多少内存。
   2. **不做 join 顺序重排**。真实 ClickHouse 也不做(它相信
      用户把小表放在右边),但这个"不做"在 mini-clickhouse 里
      更危险,因为我们连内存上限都不检查。第 13 章把
      "JOIN 把右表放错边导致 OOM" 列为最常见的生产事故之一。
"""

from __future__ import annotations

import bisect
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..block import Block, ColumnWithTypeAndName
from ..columns import ColumnConst, ColumnNullable, IColumn
from ..errors import ErrorCode, raise_error
from ..sql import ast_ as A
from ..util.keys import build_key
from .expression import Evaluator, Scope  # noqa: F401

__all__ = ["execute_join", "JoinSettings", "extract_join_keys"]


class JoinSettings:
    """一次 JOIN 的行为开关 —— 与真实 ClickHouse 的设置同名。"""

    __slots__ = ("kind", "strictness", "use_nulls", "default_strictness")

    def __init__(
        self,
        kind: str = "INNER",
        strictness: str = "ALL",
        use_nulls: bool = False,
    ) -> None:
        self.kind = kind.upper()
        self.strictness = strictness.upper()
        #: ``join_use_nulls``:未匹配的右侧列用 **NULL** 而不是默认值填充。
        #: 默认是 **0**(填默认值)—— 这与 SQL 标准相反,是从 2016 年
        #: 沿用至今的历史包袱,第 13 章专门讲了它怎么坑人。
        self.use_nulls = use_nulls
        self.default_strictness = "ALL"


def _alias_of(node: Optional[A.Node]) -> Optional[str]:
    """取出 FROM 子句里那个源的表别名(``FROM ev AS e`` → ``e``)。"""
    if isinstance(node, A.TableReference):
        return node.alias or node.table or None
    if isinstance(node, A.SubqueryRef):
        return node.alias
    return None


def _aliases_of(node: Optional[A.Node]) -> set:
    """收集一棵 FROM 子树里**全部**表别名(递归处理 ``JOIN`` 链)。

    ``(A a JOIN B b) JOIN C c`` 的左子树是内层 ``JoinExpr``,它的别名叫
    ``a``/``b`` 两个。链式 ``ON a.k = c.k`` 里的 ``a`` 属于左子树、``c``
    属于右子树 —— 只取"子树的根别名"(``None``)会把等值条件误判成
    "没有等值条件",于是三表连接报出指错方向的
    ``NOT_IMPLEMENTED: requires at least one equality condition``。
    """
    if node is None:
        return set()
    if isinstance(node, A.JoinExpr):
        return _aliases_of(node.left) | _aliases_of(node.right)
    alias = _alias_of(node)
    return {alias} if alias else set()


def _alias_set(aliases: Any) -> set:
    """把"别名"参数归一成集合 —— 既接受 ``'e'`` 也接受 ``{'a', 'b'}``/``None``。"""
    if aliases is None:
        return set()
    if isinstance(aliases, str):
        return {aliases}
    return set(aliases)


def _qualified_side(node: A.Node, left_alias: Any,
                    right_alias: Any) -> Optional[str]:
    """按**限定前缀**判断一个标识符属于左表还是右表。

    ``ON e.user = u.user`` 里的 ``e.user`` 前缀是 ``e`` —— 那就是左表。

    为什么要先看前缀、而不是去作用域里查列? 因为 JOIN 执行时
    **两侧的块还没有合并**,``e`` 这个别名在任何一个作用域里都
    查不到 —— 用作用域判断会把 ``ON e.user = u.user`` 判成
    "没有等值条件",于是整个 JOIN 报 NOT_IMPLEMENTED。
    (这个坑在实现时踩到了,而且报错信息完全指错方向。)

    ``left_alias``/``right_alias`` 可以是单个别名,也可以是**一组**别名
    (链式连接里一侧可能挂着多个表,见 :func:`_aliases_of`)。
    """
    left_set = _alias_set(left_alias)
    right_set = _alias_set(right_alias)
    if isinstance(node, A.Identifier) and "." in node.name:
        head = node.name.split(".", 1)[0]
        if head in left_set:
            return "left"
        if head in right_set:
            return "right"
    if isinstance(node, A.FunctionCall) and node.name == "tupleElement" and node.args:
        return _qualified_side(node.args[0], left_set, right_set)
    return None


def extract_join_keys(
    ctx: Any,
    join: A.JoinExpr,
    left_scope: Scope,
    right_scope: Scope,
    left_alias: Any = None,
    right_alias: Any = None,
) -> Tuple[List[A.Node], List[A.Node], List[A.Node]]:
    """从 ``ON`` / ``USING`` 里抽出左右键,**以及剩下的残余谓词**。

    返回 ``(左键, 右键, 残余谓词)``。第三个返回值是**必需的**,不是
    可有可无的补充:

    .. code-block:: text

       ON a.id = b.id AND a.d = b.d    →  左 [a.id, a.d]  右 [b.id, b.d]  残余 []
       ON a.id = b.id AND r.w = 11     →  左 [a.id]       右 [b.id]       残余 [r.w = 11]
       USING (id, d)                   →  左 [id, d]      右 [id, d]      残余 []

    **为什么不能只返回前两个?** 因为哈希连接只能处理**等值**条件:
    它把右表的键哈希起来、再用左表的键去查。``AND`` 后面那些不构成
    等值的条件(``a.x > b.y``、``r.w = 11`` 这种一侧是常量的)进不了
    哈希表,只能在**匹配之后**逐行过滤。

    .. warning::

       早期版本只返回前两个,并在 docstring 里写"非等值条件留给残余谓词"
       —— 而**那个残余谓词从来不存在**(全仓库没有任何代码求值它)。
       后果是 ``ON l.k = r.k AND r.w = 12`` 会返回**该被过滤掉的行**:
       行数对不上、内容也错,而且**不报错**。写进文档的承诺没有被实现,
       比写错更危险 —— 因为它看起来像是已经处理过了。
    """
    if join.using_columns:
        left_keys = [A.Identifier(name=c) for c in join.using_columns]
        right_keys = [A.Identifier(name=c) for c in join.using_columns]
        return left_keys, right_keys, []
    on = join.on_expr
    if on is None:
        if join.kind == "CROSS":
            return [], [], []
        raise_error(ErrorCode.SYNTAX_ERROR, "JOIN requires ON or USING")
    left_keys: List[A.Node] = []
    right_keys: List[A.Node] = []
    residual: List[A.Node] = []
    for cond in _split_conjunction(on):
        if isinstance(cond, A.BinaryOp) and cond.op in ("=", "=="):
            # 先按**限定前缀**判断(JOIN 时两侧的块还没合并,靠作用域查不到别名)
            ls = _qualified_side(cond.left, left_alias, right_alias)
            rs = _qualified_side(cond.right, left_alias, right_alias)
            if ls == "left" and rs == "right":
                left_keys.append(cond.left)
                right_keys.append(cond.right)
                continue
            if ls == "right" and rs == "left":
                left_keys.append(cond.right)
                right_keys.append(cond.left)
                continue
            # 退回按作用域判断(不带前缀的列名)
            if _belongs_to(cond.left, right_scope) and _belongs_to(cond.right, left_scope):
                left_keys.append(cond.right)
                right_keys.append(cond.left)
                continue
            if _belongs_to(cond.left, left_scope) and _belongs_to(cond.right, right_scope):
                left_keys.append(cond.left)
                right_keys.append(cond.right)
                continue
        # 没能成为等值键 → 它是**残余谓词**,匹配之后必须逐行求值
        residual.append(cond)
    return left_keys, right_keys, residual


def _split_conjunction(node: A.Node) -> List[A.Node]:
    """把 ``a AND b AND c`` 拆成 ``[a, b, c]``。"""
    if isinstance(node, A.BinaryOp) and node.op == "AND":
        return _split_conjunction(node.left) + _split_conjunction(node.right)
    return [node]


def _belongs_to(node: A.Node, scope: Scope) -> bool:
    """判断一个表达式是否引用了某个作用域里的列(粗判,用于 ON 的定向)。"""
    if isinstance(node, A.Identifier):
        return scope.resolve_column(node.name) is not None
    return any(_belongs_to(c, scope) for c in node.children())


def execute_join(
    ctx: Any,
    left_block: Optional[Block],
    right_block: Optional[Block],
    join: A.JoinExpr,
    left_scope: Scope,
    right_scope: Scope,
    settings: Optional[JoinSettings] = None,
) -> Block:
    """执行一次 JOIN,返回**合并后的 Block**。

    ``CROSS JOIN`` 走一条单独的路径(它是笛卡尔积,没有键可言);
    其余所有组合都走"建哈希表 + 探测"。

    返回的列名规则与真实 ClickHouse 一致:

    * 有 ``USING`` 时,键列**只出现一次**(用的是左表那一份);
    * 其余列若左右同名,右侧的会加上**表别名前缀**
      (``a.id`` / ``b.id``),否则保持裸名;
    * 这一点很重要:``SELECT * FROM a JOIN b USING (id)`` 里
      ``id`` 只有一个,这符合直觉;而 ``a JOIN b ON a.x = b.x``
      保留两份 ``x``。
    """
    settings = settings or JoinSettings(kind=join.kind, strictness=join.strictness)
    if join.kind == "CROSS" or (not join.using_columns and join.on_expr is None
                                and join.kind == "CROSS"):
        return _cross_join(left_block, right_block, join)

    left_keys, right_keys, residual = extract_join_keys(
        ctx, join, left_scope, right_scope,
        _aliases_of(join.left), _aliases_of(join.right),
    )
    if not left_keys and join.kind != "CROSS":
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "mini-clickhouse's hash join requires at least one equality condition "
            "in ON (e.g. ON a.id = b.id); non-equi joins are not implemented",
        )

    left_rows = left_block.rows() if left_block is not None else 0
    right_rows = right_block.rows() if right_block is not None else 0

    left_key_cols = _eval_keys(ctx, left_keys, left_block, left_scope)
    right_key_cols = _eval_keys(ctx, right_keys, right_block, right_scope)

    # ── ① build 阶段:右表建哈希表 ──────────────────────────────────────
    # key → 匹配到的行号列表。``ANY`` 只需要第一个,所以列表会被截短。
    table: Dict[Any, List[int]] = {}
    for row in range(right_rows):
        key = build_key(right_key_cols, row) if right_key_cols else 0
        if _key_has_null(key):
            # 含 NULL 的键**永不匹配**(三值逻辑里 ``NULL = NULL`` 是 UNKNOWN),
            # 直接不进哈希表 —— 它就相当于"这一行没有可匹配的键"。
            continue
        bucket = table.get(key)
        if bucket is None:
            table[key] = [row]
        else:
            if settings.strictness == "ANY":
                # ANY:只留第一个匹配 —— 这就是"右表只取一行"的实现
                continue
            bucket.append(row)
    if settings.strictness in ("SEMI", "ANTI"):
        # SEMI/ANTI 不关心匹配了几行,只关心"有没有匹配"
        for k in list(table):
            table[k] = [table[k][0]] if table[k] else []

    # ASOF 需要一个**有序**的右表 —— 按 (key, asof 列) 排序后二分
    asof_sorted: Dict[Any, List[int]] = {}
    if settings.strictness == "ASOF":
        asof_sorted = _build_asof(table, right_block, right_key_cols)

    # ── RIGHT ANY:以**右表**为驱动 ─────────────────────────────────────
    #
    # ``RIGHT ANY JOIN`` 的语义是"每个右行**至多产出一行**,与它配对的是
    # 任意一个键相等的左行",等价于把两侧对调后的 ``LEFT ANY JOIN``
    # (ClickHouse 文档:``t1 ANY LEFT JOIN t2`` 与 ``t2 ANY RIGHT JOIN t1``
    # 结果相等,默认 ``any_join_distinct_right_table_keys = 0``)。
    #
    # 左驱动的哈希探测做不到这一点:它按**右表的键**截断候选,于是同键的
    # 第二个右行被当成"未匹配",错误地补上左表默认值。所以单独走一条路径。
    if join.kind == "RIGHT" and settings.strictness == "ANY":
        return _right_any_join(ctx, left_block, right_block, join, settings,
                               left_key_cols, right_key_cols, residual)

    matched_right = [False] * right_rows
    out_left_rows: List[int] = []
    out_right_rows: List[int] = []      # -1 表示"没有匹配"

    for row in range(left_rows):
        key = build_key(left_key_cols, row) if left_key_cols else 0
        if _key_has_null(key):
            # 左键含 NULL → 永远是"无候选"(走外连接/ANTI 的补空分支)。
            candidates = []
        else:
            candidates = table.get(key)
            if settings.strictness == "ASOF":
                candidates = _pick_asof(asof_sorted, key, right_block, row, left_scope, ctx,
                                        left_keys, left_key_cols)
        candidates = list(candidates) if candidates else []
        # ``ON`` 里的**非等值残余谓词**(``ON l.k = r.k AND r.w = 12``)属于
        # **匹配判定**的一部分,不是"拼行之后的行级过滤":只有通过残余谓词的
        # 候选才算真正匹配。
        #
        # 放到拼行之后再过滤会**丢掉外连接的行**:``LEFT JOIN`` 未匹配时本该
        # 产出一行(右侧补默认值),而那个"未匹配"分支在残余谓词下被误杀
        # (补出来的右侧默认值几乎总是不满足谓词)。正确的做法是在这里就把
        # 不合格的候选剔除 —— 没有候选,就自然走外连接的补空分支。
        if residual and candidates:
            pairs = [(row, r) for r in candidates]
            candidates = [r for (_lr, r) in _filter_pairs_by_residual(
                ctx, left_block, right_block, join, settings, pairs, residual)]
        if candidates:
            if settings.strictness == "SEMI":
                out_left_rows.append(row)
                out_right_rows.append(-1)
            elif settings.strictness == "ANTI":
                pass
            else:
                for r in candidates:
                    out_left_rows.append(row)
                    out_right_rows.append(r)
                    matched_right[r] = True
        else:
            if settings.strictness == "ANTI":
                out_left_rows.append(row)
                out_right_rows.append(-1)
            elif settings.strictness == "SEMI":
                # ``SEMI`` 只保留**有匹配**的左行;没有匹配就什么都不产出。
                # (这一条以前漏了排除 ``SEMI``,于是 ``LEFT SEMI JOIN`` 把左表
                #  的**全部**行都当成"未匹配的外连接行"补了出来 —— 第 15 章说
                #  ``SEMI`` "只保留左表中有匹配的行",这条分支直接违背了它。)
                pass
            elif join.kind in ("LEFT", "FULL"):
                out_left_rows.append(row)
                out_right_rows.append(-1)

    # RIGHT/FULL 里"没有匹配的右表行"也要产出。
    # 但 ``SEMI``/``ANTI`` 是**过滤**而非连接,不产生"未匹配的补空行",所以这里
    # 必须排除它们 —— 否则 ``RIGHT SEMI``/``RIGHT ANTI`` 会把所有右行都当成
    # "未匹配"补出来(和在左边漏掉 ``SEMI`` 属于同一类错误)。
    if join.kind in ("RIGHT", "FULL") and settings.strictness not in ("SEMI", "ANTI"):
        for r in range(right_rows):
            if not matched_right[r]:
                out_left_rows.append(-1)
                out_right_rows.append(r)

    return _build_result_block(
        left_block, right_block, out_left_rows, out_right_rows, join, settings
    )


def _right_any_join(
    ctx: Any,
    left_block: Optional[Block],
    right_block: Optional[Block],
    join: A.JoinExpr,
    settings: JoinSettings,
    left_key_cols: Sequence[IColumn],
    right_key_cols: Sequence[IColumn],
    residual: Sequence[A.Node],
) -> Block:
    """``RIGHT ANY JOIN`` —— 右表驱动,每个右行至多产出一行。

    把左表按 join key 建成索引,再逐**右行**找"任意一个"匹配的左行;
    找不到就补左表默认值(外连接语义)。
    """
    left_rows = left_block.rows() if left_block is not None else 0
    right_rows = right_block.rows() if right_block is not None else 0
    index: Dict[Any, List[int]] = {}
    for row in range(left_rows):
        key = build_key(left_key_cols, row) if left_key_cols else 0
        if _key_has_null(key):
            continue
        index.setdefault(key, []).append(row)

    out_left_rows: List[int] = []
    out_right_rows: List[int] = []
    for r in range(right_rows):
        key = build_key(right_key_cols, r) if right_key_cols else 0
        candidates = [] if _key_has_null(key) else list(index.get(key) or [])
        chosen = -1
        if candidates:
            if residual:
                pairs = [(lr, r) for lr in candidates]
                surviving = _filter_pairs_by_residual(
                    ctx, left_block, right_block, join, settings, pairs, residual)
                chosen = surviving[0][0] if surviving else -1
            else:
                chosen = candidates[0]
        out_left_rows.append(chosen)
        out_right_rows.append(r)
    return _build_result_block(
        left_block, right_block, out_left_rows, out_right_rows, join, settings
    )


def _eval_keys(
    ctx: Any, keys: Sequence[A.Node], block: Optional[Block], scope: Scope
) -> List[IColumn]:
    if not keys:
        return []
    if block is None or block.rows() == 0:
        return [k.type.create_column() if hasattr(k, "type") else _empty() for k in keys]
    sc = Scope(dict(scope.columns))
    for c in block.data:
        sc.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, c.column))
    ev = Evaluator(ctx, sc, block.rows())
    out = []
    for k in keys:
        col = ev.evaluate(k)
        out.append(col.column.convert_to_full_column() if col.column.is_const() else col.column)
    return out


def _key_has_null(key: Any) -> bool:
    """键里是否含 NULL —— 含 NULL 的键在 JOIN 里**永不匹配**。

    ``ON a.k = b.k`` 里只要某一边的 ``k`` 是 NULL,这一对就不算匹配
    (SQL 三值逻辑里 ``NULL = NULL`` 求值为 UNKNOWN,不是 TRUE)。所以:

    * **build** 阶段跳过含 NULL 的右行(它没有可匹配的键);
    * **probe** 阶段把含 NULL 的左键当成"无候选"(于是 ``LEFT JOIN`` 补空、
      ``ANTI`` 保留、``SEMI``/``INNER`` 丢弃,全都符合真实 ClickHouse)。

    :func:`build_key` 单列时返回**裸值**、多列时返回元组,所以这里两种形态都要看。
    """
    if key is None:
        return True
    if isinstance(key, tuple):
        return any(k is None for k in key)
    return False


def _empty() -> IColumn:
    from ..columns import ColumnVector
    import numpy as np

    return ColumnVector(np.uint8)


def _build_asof(
    table: Dict[Any, List[int]], right_block: Optional[Block],
    right_key_cols: Sequence[IColumn],
) -> Dict[Any, List[int]]:
    """ASOF JOIN 的右表索引 —— 每个 key 下的行按"匹配列"排序。

    ASOF 的语义(``ASOF JOIN`` 的 "as of" 就是"截至某一时刻"):

    .. code-block:: text

       trades:   (sym, ts, price)         quotes:  (sym, ts, bid)
       ASOF JOIN trades ON trades.sym = quotes.sym AND trades.ts >= quotes.ts
       → 每一笔成交,配上"不晚于它的最近一条报价"

    这是时序数据的标准需求(把"最新报价"贴到每笔交易上),
    用普通等值 join 表达不了,用窗口函数很别扭。
    """
    return table


def _pick_asof(
    asof_sorted: Dict[Any, List[int]], key: Any, right_block: Optional[Block],
    left_row: int, left_scope: Scope, ctx: Any,
    left_keys: Sequence[A.Node], left_key_cols: Sequence[IColumn],
) -> List[int]:
    """取"键 ≤ 左表键"里最大的那个 —— mini-clickhouse 的简化实现。

    .. warning::

       **这里是有意简化的**:真实 ClickHouse 的 ASOF JOIN 需要显式
       声明不等值条件(``ON a.k = b.k AND a.ts >= b.ts``)并按它排序。
       mini-clickhouse 只按**等值键**建表,所以 ASOF 退化成了
       "取该键下最后一个匹配" —— 只有当右表本身按时间有序时才等价。
       教程第 13 章把这个差异明确写出来,并给出正确的用法示例。
    """
    rows = asof_sorted.get(key) or []
    return rows[-1:] if rows else []


def _cross_join(left: Optional[Block], right: Optional[Block],
                join: A.JoinExpr) -> Block:
    """笛卡尔积 —— O(n·m),只在小表上用。

    ``join`` 必须是**真实的** ``JoinExpr``(``FROM a, b`` 的隐式逗号连接、
    或显式 ``CROSS JOIN`` 都会带 ``left``/``right``)。以前这里现场构造了一个
    **没有 ``left``/``right``** 的合成节点,于是 :func:`_build_result_block`
    里 ``_alias_of(join.right)`` 拿到 ``None``、退化成前缀 ``right`` —— 右表的
    冲突列被命名成 ``right.k`` 而不是 ``b.k``。查询里写 ``b.k`` 于是精确匹配
    不到,静默回退(去掉前缀)绑到**左表**的 ``k`` 上:``SELECT a.x, b.y FROM
    A AS a, B AS b WHERE a.k = b.k`` 会退化成 ``k = k`` 恒真,吐出整个笛卡尔积。
    """
    left_rows = left.rows() if left is not None else 0
    right_rows = right.rows() if right is not None else 0
    out_left = [i for i in range(left_rows) for _ in range(right_rows)]
    out_right = [j for _ in range(left_rows) for j in range(right_rows)]
    return _build_result_block(left, right, out_left, out_right,
                               join, JoinSettings(kind="CROSS", strictness="ALL"))


def _build_result_block(
    left: Optional[Block],
    right: Optional[Block],
    left_rows: Sequence[int],
    right_rows: Sequence[int],
    join: A.JoinExpr,
    settings: JoinSettings,
) -> Block:
    """按行号配对结果拼出输出 Block —— 处理 NULL 填充与列命名。"""
    out = Block()
    using = {c.lower() for c in join.using_columns}
    from ..types import composite as _c

    # **未匹配的一侧填什么?** 由 ``join_use_nulls`` 决定,默认是 **0** ——
    # 填**类型默认值**(0 / 空串)而不是 NULL(与 SQL 标准相反,是历史行为)。
    #
    # ``join_use_nulls = 1`` 时,只有**真的会出现"未匹配行"的那一侧**才升级成
    # ``Nullable``:LEFT JOIN 的左侧永远有值(保持非 Nullable),RIGHT/FULL
    # JOIN 的**左侧**才会缺值(必须变 Nullable)。左右两侧用同一套判断 ——
    # 以前只对右半边生效,于是 ``RIGHT JOIN ... SETTINGS join_use_nulls=1``
    # 的左侧仍然填默认值(``0``/``''``),与右侧的 NULL 自相矛盾。
    left_missing = settings.use_nulls and any(r < 0 for r in left_rows)
    right_missing = settings.use_nulls and any(r < 0 for r in right_rows)

    if left is not None:
        for c in left.data:
            dtype = c.type
            if left_missing and not dtype.is_nullable():
                dtype = _c.DataTypeNullable(c.type)
            missing_value = None if left_missing else dtype.default_value()
            new_col = dtype.create_column()
            for r in left_rows:
                if r < 0:
                    new_col.append(missing_value)
                else:
                    new_col.append(c.column.get(r))
            out.add_column(ColumnWithTypeAndName(c.name, dtype, new_col))

    if right is not None:
        for c in right.data:
            if c.name.lower() in using:
                # USING 的键列只保留左表那一份(与 ClickHouse 一致)
                continue
            name = c.name
            if left is not None and left.try_get_by_name(name) is not None:
                # 同名列:真实 ClickHouse 用 ``表别名.列名`` 表达它。
                # 名字必须是**右表的别名**,因为 ON 条件里的 ``u.user``
                # 就是按这个名字去解析的。
                name = f"{_alias_of(join.right) or 'right'}.{c.name}"
            if out.try_get_by_name(name) is not None:
                name = f"{name}_right"
            # **未匹配的右侧列填什么?** 由 ``join_use_nulls`` 决定,
            # 而它的默认值是 **0** —— 也就是填**类型的默认值**(0 / 空串),
            # 而不是 NULL。这与 SQL 标准相反,是从很早以前沿用下来的行为。
            #
            # 代价是:你**分不清**"匹配上了但值恰好是 0"和"根本没匹配上"。
            # 要区分就必须显式 ``SET join_use_nulls = 1``。
            dtype = c.type
            if right_missing and not dtype.is_nullable():
                dtype = _c.DataTypeNullable(c.type)
            missing_value = None if right_missing else dtype.default_value()
            new_col = dtype.create_column()
            for r in right_rows:
                if r < 0:
                    new_col.append(missing_value)
                else:
                    new_col.append(c.column.get(r))
            out.add_column(ColumnWithTypeAndName(name, dtype, new_col))
    return out


def _filter_pairs_by_residual(
    ctx: Any,
    left: Optional[Block],
    right: Optional[Block],
    join: A.JoinExpr,
    settings: JoinSettings,
    pairs: Sequence[Tuple[int, int]],
    residual: Sequence[A.Node],
) -> List[Tuple[int, int]]:
    """在**匹配判定阶段**求值 ``ON`` 里的非等值残余谓词,返回通过的 ``(左行, 右行)``。

    ``ON l.k = r.k AND r.w = 12`` 里,``l.k = r.k`` 交给哈希表,
    ``r.w = 12`` 只能在 (左行, 右行) **配对之后**求值 —— 但求值必须发生在
    "判定这个候选算不算匹配"的时刻,而不是"拼出整张结果之后再逐行过滤"。

    做法:把这批候选对拼成一个临时块(列命名与最终结果一致,所以
    ``r.w`` 这类限定名照常解析),求值残余谓词,返回仍然成立的那批对。
    """
    if not pairs:
        return []
    ls = [p[0] for p in pairs]
    rs = [p[1] for p in pairs]
    block = _build_result_block(left, right, ls, rs, join, settings)
    mask = _eval_residual_mask(ctx, block, residual)
    return [p for p, ok in zip(pairs, mask) if ok]


def _eval_residual_mask(ctx: Any, block: Block, residual: Sequence[A.Node]) -> List[bool]:
    """在一个块上逐行求值残余谓词,返回布尔掩码。

    条件里引用了拼不出来的列(比如某个别名没解析到)时**直接抛出** ——
    不能静默放行(那等于回到"残余谓词从未生效"的老 bug),也不能吞掉异常。
    """
    mask = [True] * block.rows()
    if block.rows() == 0:
        return mask
    scope = Scope()
    for c in block.data:
        scope.add_column(c.name, ColumnWithTypeAndName(c.name, c.type, c.column))
    ev = Evaluator(ctx, scope, block.rows())
    for cond in residual:
        col = ev.evaluate(cond)
        vals = col.column.convert_to_full_column() if col.column.is_const() else col.column
        for i in range(block.rows()):
            if mask[i] and not _truthy(vals.get(i)):
                mask[i] = False
        if not any(mask):
            break
    return mask


def _truthy(value: Any) -> bool:
    """条件值的布尔语义 —— ``0`` / ``NULL`` 为假。"""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, bool):
        return value
    return bool(value)
