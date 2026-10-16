"""函数系统的底座 —— 注册表、基类、NULL 传播、lambda 实参。

ClickHouse 有 **1000 多个函数**,但它们共享一套非常薄的骨架。
理解这套骨架比记住函数名重要得多:

.. code-block:: text

   函数调用 f(a, b) 的一生:

   ① 解析      ExpressionAnalyzer 把 AST 变成 FunctionCall 节点
   ② 解析函数  FunctionFactory::get("plus", {UInt8, UInt8})
                  ├─ 查注册表(名字 → 工厂)
                  ├─ 计算返回类型  get_return_type([UInt8, UInt8]) → UInt16
                  └─ 处理 NULL 传播(见下)
   ③ 执行      execute(args: [ColumnWithTypeAndName], rows) → ColumnWithTypeAndName
                  ├─ 快路径:向量化实现(整列一次算完)
                  └─ 慢路径:逐行回退(execute_row)

**执行接口的单位是"列",不是"值"。** 这是 ClickHouse 与所有行式数据库
函数系统的根本区别,也是"写一个 ClickHouse 函数"和"写一个 MySQL 函数"
完全是两件事的原因。

.. note::

   真实 ClickHouse 的函数还有一层 ``IFunctionOverloadResolver``
   (决定"这次调用该用哪个重载")。mini-clickhouse 把重载解析
   简化成"工厂自己看参数类型决定",少一层抽象但不改变语义。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..block import ColumnWithTypeAndName
from ..columns import ColumnConst, ColumnNullable, ColumnVector, IColumn
from ..errors import ErrorCode, MiniChError, raise_error
from ..types import DataType, get_data_type
from ..types.base import DataTypePtr

__all__ = [
    "FunctionBase",
    "LambdaArgument",
    "register_function",
    "get_function",
    "function_exists",
    "registered_functions",
    "is_nullable_or_null",
    "wrap_nullable_result",
    "numpy_execute",
]


class LambdaArgument:
    """高阶函数的 lambda 实参 —— **它不是一个值,是一段程序**。

    .. code-block:: sql

       SELECT arrayMap(x -> x * 2, [1, 2, 3]);

    ``arrayMap`` 收到的第二个参数不是列,而是 ``x -> x * 2`` 这段 AST
    加上当前的捕获环境(``x`` 由 ``arrayMap`` 自己提供)。
    真实 ClickHouse 用 ``FunctionBase::executeImpl`` 的 lambda 参数
    (``ExpressionActionsPtr``)表达同一件事。

    mini-clickhouse 的做法是:

    1. 表达式求值器遇到 ``arrayMap(lambda, arr)`` 时,
       **不求值**第一个参数,而是构造一个 :class:`LambdaArgument`;
    2. 函数实现自己决定怎么调用它 —— 通常是
       ``body(x=元素值)`` 逐元素求值,或者对整列求值(向量化)。

    ``body`` 是一个可调用对象,签名是 ``body(**绑定) -> IColumn``。
    """

    __slots__ = ("names", "body", "captured")

    def __init__(self, names: Sequence[str], body: Callable[..., IColumn],
                 captured: Optional[List[ColumnWithTypeAndName]] = None) -> None:
        #: lambda 的形参名(``x -> ...`` 里就是 ``["x"]``;``(x, i) -> ...`` 是两个)
        self.names = list(names)
        #: 求值函数 —— 传入形参对应的列,返回结果列
        self.body = body
        #: 从外层作用域捕获的列(闭包变量)
        self.captured = captured or []

    def arity(self) -> int:
        return len(self.names)

    def call(self, *columns: IColumn) -> IColumn:
        if len(columns) != len(self.names):
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"Lambda expects {len(self.names)} argument(s), got {len(columns)}",
            )
        return self.body(**dict(zip(self.names, columns)))

    def __repr__(self) -> str:  # pragma: no cover
        return f"LambdaArgument({' ,'.join(self.names)})"


class FunctionBase(ABC):
    """所有函数的基类。

    子类通常只需要实现两个方法:

    * :meth:`get_return_type` —— 给定实参类型,返回结果类型;
    * :meth:`execute_row`(**或者** :meth:`execute`)—— 怎么算。

    如果只实现 ``execute_row``,基类会自动提供逐行执行的 ``execute``。
    这条路**能跑但慢** —— 因为它放弃了向量化。第 15 章会用一个
    200 万行的例子量化这个差别,并说明"什么时候值得为向量化多写代码"。
    """

    #: 函数名 —— 必须与注册时用的名字一致
    name: str = "unnamed"

    #: 是否可以在常量参数上做常量折叠(``SELECT 1 + 1`` 应当在
    #: 查询分析期就算成 ``2``)。几乎所有纯函数都是 True。
    deterministic: bool = True

    #: 是否允许在**默认值列**上执行(某些函数在"整列都是默认值"时
    #: 可以走特化路径,例如 ``sum(0)``)。
    allow_default_column: bool = True

    #: NULL 传播策略。``True``(默认)表示"任一参数为 NULL,结果就是 NULL"——
    #: 这与 SQL 标准一致,也让绝大多数函数**不必**自己处理 NULL。
    #: 置为 ``False`` 的函数必须自己处理 NULL(例如 ``isNull``、
    #: ``coalesce``、``ifNull``、``assumeNotNull``、``toTypeName``)。
    propagate_nulls: bool = True

    #: 返回值是否一定不可空(即使参数可空)。``count``、``isNull`` 这类。
    return_type_cannot_be_nullable: bool = False

    # ── 类型 ────────────────────────────────────────────────────────────

    @abstractmethod
    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        """给定实参类型,推出结果类型。

        .. warning::

           这个函数**必须**是纯函数,而且**不能**依赖行的值 ——
           因为它只在"准备阶段"被调用一次。真实 ClickHouse 里
           违反这条会导致"类型与实际数据不符",下游崩溃。
        """

    # ── 执行 ────────────────────────────────────────────────────────────

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        """向量化执行入口 —— 默认实现是**逐行回退**。

        自己的实现若覆盖了它,就拿到了"整列一次算完"的机会。
        真实 ClickHouse 里这个参数是 ``Block &``(会被就地改写),
        我们返回新列(语义更清晰,代价是多一次分配)。
        """
        return_type = self.get_return_type([a.type for a in args])
        out = return_type.create_column()
        plain = [a.column for a in args]
        # 常量列参与时,先展开成完整列 —— 否则 get(i) 会一直返回同一个值,
        # 而结果行数会错(ColumnConst 的 size 是显式给的,不是数出来的)
        plain = [c.convert_to_full_column() if c.is_const() else c for c in plain]
        for i in range(rows):
            out.append(self.execute_row([c.get(i) for c in plain], context))
        return out

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        """逐行执行的实现 —— 只实现 ``execute`` 的话不必覆盖它。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Function {self.name} does not implement execute() or execute_row()",
        )

    # ── 可选钩子 ────────────────────────────────────────────────────────

    def use_default_implementation_for_nulls(self) -> bool:
        """是否使用基类的 NULL 传播实现。"""
        return self.propagate_nulls

    def can_be_executed_on_default_column(self) -> bool:
        return self.allow_default_column

    def is_deterministic(self) -> bool:
        """非确定性函数(``rand``/``now``/``generateUUIDv4``)**不能被常量折叠**。

        这条约束经常被忽略:如果你把 ``now()`` 折叠成一个常量,
        整条查询里所有 ``now()`` 都会变成同一个时刻 —— 看起来"更一致",
        实际上和真实行为不符,而且长查询里会明显偏差。
        """
        return self.deterministic

    def is_suitable_for_constant_folding(self) -> bool:
        return self.deterministic

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════

#: 名字 → 工厂。工厂签名 ``(params: list[Any]) -> FunctionBase`` ——
#: 参数是**函数参数**(不是实参),例如 ``quantile(0.9)`` 里的 ``0.9``。
_REGISTRY: Dict[str, Callable[[Sequence[Any]], FunctionBase]] = {}

#: 名字 → 别名列表(``system.functions.alias_to`` 用)
_ALIASES: Dict[str, str] = {}

#: 名字 → 一句话说明(``system.functions.description``)
_DESCRIPTIONS: Dict[str, str] = {}

#: 名字 → 分类(``system.functions.categories``):普通 / 聚合 / 表函数
_CATEGORIES: Dict[str, str] = {}


def register_function(
    name: str,
    cls: Optional[type] = None,
    *,
    aliases: Sequence[str] = (),
    description: str = "",
    factory: Optional[Callable[[Sequence[Any]], FunctionBase]] = None,
    case_insensitive: bool = True,
) -> Any:
    """注册一个标量函数。

    三种用法::

        @register_function("plus", aliases=["add"])
        class FunctionPlus(FunctionBase): ...

        register_function("plus", FunctionPlus)

        register_function("quantile", factory=lambda p: FunctionQuantile(p))

    ``case_insensitive=True``(默认)时,``PLUS`` 与 ``plus`` 都能解析到
    同一个函数 —— 这是 ClickHouse 的**大小写不敏感**规则的落地。
    函数名在注册表里统一小写存放,查询时也小写化再查。
    """

    def _do(cls_: Optional[type] = None) -> None:
        key = name.lower() if case_insensitive else name

        def _default_factory(params: Sequence[Any], _cls: Optional[type] = cls_) -> FunctionBase:
            if _cls is None:
                raise_error(
                    ErrorCode.UNKNOWN_FUNCTION,
                    f"Function {name} was registered without an implementation",
                )
            inst = _cls()  # type: ignore[call-arg]
            inst.name = name
            if params:
                setattr(inst, "params", list(params))
            return inst

        _REGISTRY[key] = (
            factory if factory is not None else (lambda p: _default_factory(p))
        )
        if description:
            _DESCRIPTIONS[key] = description
        _CATEGORIES.setdefault(key, "Regular")
        for alias in aliases:
            akey = alias.lower() if case_insensitive else alias
            _REGISTRY.setdefault(akey, _REGISTRY[key])
            _ALIASES[akey] = key
            if description:
                _DESCRIPTIONS.setdefault(akey, description)
            _CATEGORIES.setdefault(akey, "Regular")

    if cls is not None:
        _do(cls)
        return cls

    def _decorator(klass: type) -> type:
        """装饰器形态 —— **必须把类原样返回**。

        .. warning::

           如果这里返回 ``None``(早期版本就是),``@register_function("plus")``
           会静默地把类名绑成 ``None``:注册**照样成功**(工厂闭包里
           抓着类对象),但模块级的类名丢了 —— ``__all__``、子类化、
           以及"用这个类当基类"的写法全都会踩空,而且**不报错**。
           这是一个只在"别人 import 你的模块"时才暴露的 bug。
        """
        _do(klass)
        return klass

    return _decorator


def register_alias(alias: str, target: str, description: str = "") -> None:
    """只注册一个别名(目标函数必须已经注册过)。"""
    akey, tkey = alias.lower(), target.lower()
    if tkey not in _REGISTRY:
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            f"Cannot alias {alias} to unknown function {target}",
        )
    _REGISTRY[akey] = _REGISTRY[tkey]
    _ALIASES[akey] = tkey
    if description:
        _DESCRIPTIONS[akey] = description


def function_exists(name: str) -> bool:
    return name.lower() in _REGISTRY


def get_function(
    name: str,
    params: Sequence[Any] = (),
    arg_types: Optional[Sequence[DataType]] = None,
) -> FunctionBase:
    """按名字取一个函数实例。

    .. note::

       真实 ClickHouse 在**同一个名字**下可能有一族重载
       (``plus(UInt8, UInt8)`` 与 ``plus(Date, Int32)`` 是不同实现)。
       ``FunctionFactory::get(name, arg_types)`` 会挑一个。
       mini-clickhouse 不做重载分派 —— 工厂拿到 ``arg_types`` 后
       自己决定(多数情况下返回类型推导已经足够)。
    """
    key = name.lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise_error(
            ErrorCode.UNKNOWN_FUNCTION,
            f"Unknown function {name}",
        )
    fn = factory(list(params))
    if arg_types is not None:
        fn.arg_types = list(arg_types)  # type: ignore[attr-defined]
    fn.name = name
    return fn


def registered_functions() -> List[Tuple[str, str, str]]:
    """返回 ``(名字, 分类, 说明)`` 列表 —— ``system.functions`` 的数据源。"""
    out: List[Tuple[str, str, str]] = []
    for key in sorted(_REGISTRY):
        out.append((key, _CATEGORIES.get(key, "Regular"), _DESCRIPTIONS.get(key, "")))
    return out


def function_aliases() -> Dict[str, str]:
    """别名 → 目标函数名。"""
    return dict(_ALIASES)


# ══════════════════════════════════════════════════════════════════════════
# NULL 处理的公共设施
# ══════════════════════════════════════════════════════════════════════════


def is_nullable_or_null(value: Any) -> bool:
    return value is None


def wrap_nullable_result(column: IColumn, return_type: DataType) -> Tuple[IColumn, DataType]:
    """需要时把结果列包成 ``Nullable``。"""
    if return_type.is_nullable():
        return column, return_type
    return column, return_type


def all_arguments_nullable(args: Sequence[ColumnWithTypeAndName]) -> bool:
    return any(a.type.is_nullable() for a in args)


def nullable_arguments_indices(args: Sequence[ColumnWithTypeAndName]) -> List[int]:
    return [i for i, a in enumerate(args) if a.type.is_nullable()]


def execute_with_null_propagation(
    fn: FunctionBase,
    args: Sequence[ColumnWithTypeAndName],
    rows: int,
    context: Any = None,
) -> Tuple[IColumn, DataType]:
    """**NULL 传播的统一实现** —— 这是本模块最省事的一段代码。

    规则(与真实 ClickHouse 的 ``IFunction::defaultImplementationForNulls`` 一致):

    1. 若函数声明 ``propagate_nulls = False``,直接交给它自己处理;
    2. 若任一实参是 ``Nullable``:
       a. 结果类型 = ``Nullable(get_return_type(非空版本))``;
       b. 先用各实参的**非空侧**算出结果列;
       c. 结果的空位图 = 各实参空位图的**或**;
       d. 把结果包成 ``ColumnNullable``。
    3. 否则直接执行。

    有了它,``plus`` 的实现里**完全看不到 NULL 的影子** ——
    这是"把横切关注点抽出来"的教科书例子。真实 ClickHouse 里
    这段逻辑在 ``IFunction::defaultImplementationForNulls`` 与
    ``IFunction::createResultColumnForNulls`` 里。
    """
    # 高阶函数会把 **lambda** 混在实参列表里传下来;它不是列,没有
    # ``.type`` —— 所有"按类型工作"的逻辑都必须跳过它,否则
    # ``arrayMap`` 之类的函数会在 ``LambdaArgument.type`` 上崩掉。
    # 真实 ClickHouse 不会遇到这个问题:lambda 在语法层就是一种
    # 独立的参数种类(``ASTLambda``),不会混进列列表。
    column_args = [a for a in args if isinstance(a, ColumnWithTypeAndName)]

    if not fn.use_default_implementation_for_nulls():
        ret = fn.get_return_type([a.type for a in column_args])
        return fn.execute(args, rows, context), ret

    nullable = any(a.type.is_nullable() for a in column_args)
    if not nullable:
        ret = fn.get_return_type([a.type for a in column_args])
        return fn.execute(args, rows, context), ret

    from ..types import composite as _c

    # 取出每个实参的"非空版本"
    stripped: List[Any] = []
    null_maps: List[Optional[IColumn]] = []
    const_null = False
    for a in args:
        if not isinstance(a, ColumnWithTypeAndName):
            # lambda 原样传下去
            stripped.append(a)
            null_maps.append(None)
            continue
        if a.type.is_nullable():
            nested_type = a.type.nested  # type: ignore[attr-defined]
            col = a.column
            # ``NULL`` 字面量(以及任何"常量 NULL"表达式,如 ``nullIf('a','a')``)
            # 的列形态是 **ColumnConst 包着 ColumnNullable** —— 它本身**不是**
            # ``ColumnNullable``。真实 ClickHouse 的
            # ``defaultImplementationForNulls`` 第一步就是
            # ``convertToFullColumnIfConst``:不展开的话空位图会被整段丢掉,
            # 于是裸的 ``None`` 被当成普通值传进函数(``lower(NULL)`` → ``'none'``)。
            if col.is_const():
                col = col.convert_to_full_column()
            if isinstance(col, ColumnNullable):
                if col.null_map and all(col.null_map):
                    # 整列都是 NULL —— 下面短路:任何一行任意实参为 NULL,
                    # 结果的该行就是 NULL(默认 NULL 传播规则)。
                    const_null = True
                stripped.append(ColumnWithTypeAndName(a.name, nested_type, col.nested))
                null_maps.append(col)
            else:
                stripped.append(ColumnWithTypeAndName(a.name, nested_type, col))
                null_maps.append(None)
        else:
            stripped.append(a)
            null_maps.append(None)

    inner_type = fn.get_return_type([a.type for a in stripped])
    result_type = _c.DataTypeNullable(inner_type)

    if const_null:
        # 已经确定"结果整列都是 NULL",就不必(而且**不能**)再执行函数:
        # 常量 NULL 展开后的嵌套列可能是 ``ColumnNothing``(``toInt32(NULL)``),
        # 直接跑会把裸 ``None`` 交给函数实现并炸掉。
        inner_col = inner_type.create_column()
        inner_col.append_many_default(rows)
        out = ColumnNullable(inner_col)
        out.null_map = [True] * rows
        return out, result_type

    inner_col = fn.execute(stripped, rows, context)
    out = ColumnNullable(inner_col)
    mask = [False] * rows
    for nm in null_maps:
        if nm is not None:
            for i in range(rows):
                if nm.null_map[i]:
                    mask[i] = True
    out.null_map = mask
    # 非空位置上如果被算成了 NULL(理论上不该发生),也要如实标出去
    for i in range(rows):
        if inner_col.get(i) is None and not mask[i]:
            mask[i] = True
    return out, result_type


# ══════════════════════════════════════════════════════════════════════════
# NumPy 辅助 —— 向量化函数用得最多的一段
# ══════════════════════════════════════════════════════════════════════════


def numpy_execute(
    args: Sequence[ColumnWithTypeAndName],
    rows: int,
    op: Callable[..., Any],
    return_type: DataType,
) -> IColumn:
    """对整列做 NumPy 运算的通用外壳。

    ``op`` 收到的是各实参的 ``np.ndarray`` 视图,返回一个同长度的数组
    (或标量)。结果会被包成 ``ColumnVector``。

    这是 mini-clickhouse 里"向量化"最直接的写法::

        return numpy_execute(args, rows, lambda a, b: np.add(a, b, dtype=np.uint16), ret)

    真实 ClickHouse 的对应物是一整个按类型特化的模板体系
    (``FunctionBinaryArithmetic::executeImpl`` 会为 T1/T2 的每种组合
    实例化一份代码)。NumPy 在这里替我们做了同样的特化,
    代价是每次运算多一层 Python 调用(第 15 章量化了这个开销)。
    """
    arrays = []
    for a in args:
        col = a.column.convert_to_full_column() if a.column.is_const() else a.column
        if isinstance(col, ColumnVector):
            arrays.append(col.numpy_view())
        else:
            arrays.append(np.asarray(col.to_list(), dtype=np.float64))
    result = op(*arrays)
    if np.isscalar(result):
        out = ColumnVector(type(result) if isinstance(result, bool) else np.asarray(result).dtype)
        out.append_many([result] * rows)
        return out
    arr = np.asarray(result)
    if arr.ndim == 0:
        out = ColumnVector(arr.dtype)
        out.append_many([arr.item()] * rows)
        return out
    if arr.shape[0] != rows:
        raise_error(
            ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
            f"Vectorized op returned {arr.shape[0]} values for {rows} rows",
        )
    return ColumnVector(arr.dtype, arr)


import numpy as np  # noqa: E402  —— 放在末尾是为了让上面的文档先被读到
