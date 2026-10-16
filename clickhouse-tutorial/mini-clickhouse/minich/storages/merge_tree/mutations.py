"""mutation —— ``ALTER TABLE ... UPDATE / DELETE`` 的执行模型。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  ClickHouse 的 UPDATE / DELETE 是**异步**的、而且**不原地改**        ║
   ╚══════════════════════════════════════════════════════════════════════╝

   MySQL / PostgreSQL 的做法(原地改):

   .. code-block:: text

      UPDATE t SET x = 1 WHERE id = 5
         │
         ▼
      找到那一行 → **就地**把 x 改成 1 → 写 WAL / undo log
      ↑ 这需要一个"能定位到行"的索引,以及事务日志

   ClickHouse 的做法(重写 part):

   .. code-block:: text

      ALTER TABLE t UPDATE x = 1 WHERE id = 5
         │
         ├── ① 立刻返回:生成一条 mutation 记录(带编号)
         │        system.mutations 里能看到:
         │          mutation_id  command          is_done  parts_to_do
         │          0000000001  UPDATE x = 1 …    0        3
         │
         └── ② 之后**每次这个 part 参与合并**时,
                顺手把谓词套用一遍,产出"改过的"新 part
                parts_to_do 递减 → 到 0 → is_done = 1

   **为什么不做原地改?**

   * clickhouse 的数据是**按列压缩**存的 —— 改一行里的一个字段,
     要解压那一列、改一个值、再压回去;而且 part 是**不可变**的
     (正在读它的查询可能还持有引用);
   * "不可变文件"是它敢把读路径做得那么激进的**前提**:
     没有写锁、没有 MVCC 版本链、可以直接 mmap。

   代价就是"最终一致":一条 DELETE 可能几秒生效,也可能几小时 ——
   取决于这张表的合并频率。``OPTIMIZE TABLE ... FINAL`` 可以催它。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  mutation 与合并是**同一条流水线** —— 这就是它"慢"的原因              │
   └──────────────────────────────────────────────────────────────────────┘

     源 part:  A, B, C
                 │
                 ├── 合并:归并排序         ← 顺序写,快
                 └── 顺带:套用 mutation     ← 复用这次顺序写,**不额外写**
                        │
                        ▼
                   新 part D(已改过)

     如果你只 UPDATE 一行,却只有 A 一个 part —— 那也要**整份重写 A**。
     这是"列存 + 不可变文件"模型下,单行更新必然很贵的原因。
     所以 ClickHouse 官方建议:小批量的单行更新用
     ``ReplacingMergeTree``/``CollapsingMergeTree`` 的表达方式,
     而不是 ``ALTER ... UPDATE``。
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...block import Block, ColumnWithTypeAndName
from ...errors import ErrorCode, MiniChError, raise_error
from .expression import evaluate_expression

__all__ = [
    "MutationCommand",
    "MutationEntry",
    "MutationRegistry",
    "apply_mutation_to_block",
    "apply_mutation_to_columns",
]


# ══════════════════════════════════════════════════════════════════════════
# 一条 mutation 命令
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class MutationCommand:
    """一条 ``UPDATE`` 或 ``DELETE`` 命令。

    .. code-block:: text

       ALTER TABLE t UPDATE x = 1, y = y + 2 WHERE id > 5
                        └── assignments ──┘    └─ predicate ─┘

       ALTER TABLE t DELETE WHERE id < 0
                     └─ assignments 为空,kind='DELETE'

    真实 ClickHouse 的 ``MutationCommand`` 支持 6 种 ``type``
    (``UPDATE``/``DELETE``/``DROP_COLUMN``/``DROP_INDEX``/``MODIFY_TTL`` …),
    因为它们**都能用同一条流水线完成**:读 part → 产出新 part。
    我们只实现需要逐行求值的两种(UPDATE / DELETE)。
    """

    #: ``"UPDATE"`` 或 ``"DELETE"``
    kind: str
    #: ``WHERE`` 谓词(可能为 None —— DELETE 没有 WHERE 就是"删全表")
    predicate: Any = None
    #: ``[(列名, 表达式 AST), …]`` —— UPDATE 的赋值列表
    assignments: List[Tuple[str, Any]] = field(default_factory=list)
    #: 原始 SQL 文本(进 ``system.mutations.command``)
    text: str = ""
    #: 受影响的列名(信息性,便于展示)
    affected_columns: List[str] = field(default_factory=list)

    def is_delete(self) -> bool:
        return self.kind.upper() == "DELETE"

    def describe(self) -> str:
        if self.text:
            return self.text
        if self.is_delete():
            return f"DELETE WHERE {_sql(self.predicate)}"
        assigns = ", ".join(f"{k} = {_sql(v)}" for k, v in self.assignments)
        return f"UPDATE {assigns} WHERE {_sql(self.predicate)}"


# ══════════════════════════════════════════════════════════════════════════
# 一条 mutation 记录(system.mutations 的一行)
# ══════════════════════════════════════════════════════════════════════════


class MutationEntry:
    """一条已登记、正在推进的 mutation —— 与 ``system.mutations`` 的列对应。

    .. code-block:: text

       ┌──────────────────────── system.mutations 的关键列 ─────────────────┐
       │ database, table                   哪张表                          │
       │ mutation_id                       编号("0000000001" 这种零填充)    │
       │ command                           原始 SQL 文本                    │
       │ create_time                       什么时候提交的                   │
       │ **is_done**                       有没有全部完成                   │
       │ **parts_to_do**                   还有几个 part 没改               │
       │ **latest_fail_reason**            最近一次失败的原因(空=没失败)  │
       │ block_numbers                     影响的 block 号(用于"只读新数据") │
       └────────────────────────────────────────────────────────────────────┘

    ``parts_to_do`` 是这里最有价值的观测点:它不为 0 就说明
    "这条 mutation 还没生效完",而**它只能靠合并推进** ——
    所以你在等它的时候,应该去 ``system.merges`` 看有没有合并任务在跑。
    """

    def __init__(
        self,
        mutation_id: str,
        commands: Sequence[MutationCommand],
        database: str = "",
        table: str = "",
        block_numbers: Optional[Sequence[int]] = None,
    ) -> None:
        self.mutation_id = mutation_id
        self.commands: List[MutationCommand] = list(commands)
        self.database = database
        self.table = table
        self.create_time = _dt.datetime.now()
        self.is_done = False
        self.latest_fail_reason = ""
        self.latest_fail_time: Optional[_dt.datetime] = None
        self.parts_to_do = 0
        self.parts_to_do_names: List[str] = []
        self.block_numbers: List[int] = list(block_numbers or [])
        self._lock = threading.RLock()

    # ── 推进 ────────────────────────────────────────────────────────────

    def mark_part_done(self, part_name: str) -> None:
        with self._lock:
            if part_name in self.parts_to_do_names:
                self.parts_to_do_names.remove(part_name)
            self.parts_to_do = len(self.parts_to_do_names)
            if self.parts_to_do == 0 and not self.latest_fail_reason:
                self.is_done = True

    def mark_failed(self, reason: str) -> None:
        with self._lock:
            self.latest_fail_reason = reason
            self.latest_fail_time = _dt.datetime.now()
            self.is_done = False

    def mark_parts(self, part_names: Sequence[str]) -> None:
        with self._lock:
            self.parts_to_do_names = list(part_names)
            self.parts_to_do = len(self.parts_to_do_names)
            self.is_done = self.parts_to_do == 0

    # ── 展示 ────────────────────────────────────────────────────────────

    def status_row(self) -> Dict[str, Any]:
        """``system.mutations`` 的一行。"""
        return {
            "database": self.database,
            "table": self.table,
            "mutation_id": self.mutation_id,
            "command": "; ".join(c.describe() for c in self.commands),
            "create_time": self.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            "block_numbers": self.block_numbers,
            "parts_to_do": self.parts_to_do,
            "parts_to_do_names": list(self.parts_to_do_names),
            "is_done": 1 if self.is_done else 0,
            "latest_failed_part": self.parts_to_do_names[0] if self.parts_to_do_names else "",
            "latest_fail_time": (self.latest_fail_time.strftime("%Y-%m-%d %H:%M:%S")
                                 if self.latest_fail_time else ""),
            "latest_fail_reason": self.latest_fail_reason,
        }

    def __repr__(self) -> str:  # pragma: no cover
        state = "done" if self.is_done else f"parts_to_do={self.parts_to_do}"
        return f"<MutationEntry {self.mutation_id} {state}>"


class MutationRegistry:
    """一张表的 mutation 清单 —— ``system.mutations`` 的数据源。

    ``mutation_id`` 的编号规则与真实 ClickHouse 一致:
    **递增的十进制数,零填充到 10 位**(``0000000001``)。
    零填充的原因是**字符串排序要等于数值排序** ——
    否则 ``"10" < "9"`` 会让 ``ORDER BY mutation_id`` 乱序,
    而客户端常常靠它取"最新一条"。
    """

    ID_WIDTH = 10

    def __init__(self) -> None:
        self.entries: List[MutationEntry] = []
        self._next = 1
        self._lock = threading.RLock()

    def next_id(self) -> str:
        with self._lock:
            mid = str(self._next).zfill(self.ID_WIDTH)
            self._next += 1
            return mid

    def add(self, entry: MutationEntry) -> MutationEntry:
        with self._lock:
            self.entries.append(entry)
            return entry

    def undone(self) -> List[MutationEntry]:
        with self._lock:
            return [e for e in self.entries if not e.is_done]

    def all_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.status_row() for e in self.entries]

    def clear(self) -> None:
        with self._lock:
            self.entries.clear()


# ══════════════════════════════════════════════════════════════════════════
# 套用
# ══════════════════════════════════════════════════════════════════════════


def apply_mutation_to_block(
    command: MutationCommand,
    block: Block,
    context: Any = None,
) -> Block:
    """把一个 ``MutationCommand`` 套用到 Block 上(**就地改**,返回同一个 block)。

    .. code-block:: text

       DELETE WHERE pred:   求 pred → 掩码 → block.filter(掩码)
       UPDATE a = expr:     求 expr → 用结果列覆盖 a 列
                            (注意:赋值之间**互不影响** —— 右边读到的是旧值)

    ``UPDATE a = a + 1, b = a`` 里的 ``b`` 拿到的是**旧的 a**
    (与 SQL 标准一致;真实 ClickHouse 也是这样,因为它对每个赋值
    各建一个 ExpressionActions,读的都是输入块)。
    """
    if command.is_delete():
        if command.predicate is None:
            block.resize(0)
            return block
        matched = _predicate_mask(command.predicate, block, context)
        # ★ DELETE 保留的是**谓词为假**的那些行。
        #   ``block.filter(mask)`` 的语义是"保留 mask[i] 为真的行",
        #   所以这里必须取反 —— 忘了取反的后果是"删掉了没匹配的行、
        #   留下了该删的行",一个 100% 错的删除(比报错危险得多)。
        block.filter([not m for m in matched])
        return block

    if not command.assignments:
        return block
    # ★ UPDATE **只改谓词为真的那些行**。
    #   忘了这一步的后果极其恶劣:``UPDATE v = v + 1 WHERE k < 5``
    #   会把**每一行**的 v 都 +1 —— 结果全错,而且不报错。
    #   所以这里先把掩码算出来,再逐行决定"取新值还是取旧值"。
    mask: Optional[List[bool]] = None
    if command.predicate is not None:
        mask = _predicate_mask(command.predicate, block, context)

    new_columns: List[Tuple[str, IColumn]] = []
    for name, expr in command.assignments:
        col = evaluate_expression(expr, block, context).column
        if col.is_const():
            col = col.convert_to_full_column()
        new_columns.append((name, col))

    # 全部算完之后再统一替换 —— 保证"赋值之间互不影响"的语义
    # (``UPDATE a = a + 1, b = a`` 里的 b 拿到的是**旧的 a**)
    for name, col in new_columns:
        cwt = block.get_by_name(name)
        if mask is None or all(mask):
            cwt.column = col
            continue
        original = cwt.column.convert_to_full_column() if cwt.column.is_const() \
            else cwt.column
        merged = cwt.column.clone_empty()
        for i in range(block.rows()):
            merged.append(col.get(i) if mask[i] else original.get(i))
        cwt.column = merged
    block.check_number_of_rows()
    return block


def _predicate_mask(predicate: Any, block: Block, context: Any = None) -> List[bool]:
    from .expression import evaluate_predicate

    return evaluate_predicate(predicate, block, context)


def apply_mutation_to_columns(
    reader: Any,
    command: MutationCommand,
    data: Any,
    ctx: Any = None,
) -> Any:
    """把一个 mutation 套用到 :class:`MergeTreePartReader` 的内存列上。

    这是"mutation 在合并时完成"的落点:reader 已经是"读出来的整份 part",
    在它上面做过滤/赋值,然后再进入归并 —— 于是 mutation 的执行
    **完全搭上了合并这趟车**,不额外产生磁盘写。
    """
    block = Block([
        ColumnWithTypeAndName(name, reader._column_type(reader.part, name), col)
        for name, col in reader.data.items()
    ])
    before = block.rows()
    apply_mutation_to_block(command, block, ctx)
    after = block.rows()
    if after == before and not command.assignments:
        return reader
    # 把结果写回 reader,并重建排序键(过滤不改变相对顺序,所以键仍有序)
    for c in block.data:
        reader.data[c.name] = c.column
    reader.rows = after
    reader._keys = reader._build_keys()
    return reader


def build_mutation_command(alter_command: Any, table: Any = None) -> MutationCommand:
    """把 AST 的 ``AlterCommand`` 转成 :class:`MutationCommand`。

    对应 ``minich.sql.ast_.AlterCommand`` 的 ``kind``:
    ``"UPDATE"``(带 ``update_assignments`` + ``predicate``)与
    ``"DELETE"``(带 ``predicate``)。
    """
    kind = getattr(alter_command, "kind", "").upper()
    if kind == "DELETE":
        return MutationCommand(
            kind="DELETE",
            predicate=getattr(alter_command, "predicate", None),
            text=_sql(alter_command),
        )
    if kind == "UPDATE":
        assigns = list(getattr(alter_command, "update_assignments", []) or [])
        return MutationCommand(
            kind="UPDATE",
            predicate=getattr(alter_command, "predicate", None),
            assignments=assigns,
            text=_sql(alter_command),
            affected_columns=[name for name, _ in assigns],
        )
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"ALTER command {kind!r} cannot be expressed as a mutation",
    )


def _sql(node: Any) -> str:
    try:
        return node.to_sql() if node is not None else ""
    except Exception:  # pragma: no cover
        return repr(node)
