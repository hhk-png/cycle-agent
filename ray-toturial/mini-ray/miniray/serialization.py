"""序列化 —— mini-ray 的「跨进程传值」层。

分布式计算框架绕不开一个问题:**用户写的函数和对象,怎么跑到另一个进程里去执行?**

Ray 的答案是 ``cloudpickle`` + 一条自定义规则:

1. **函数**按「引用」传 —— 驱动进程第一次用到某个 ``@ray.remote`` 函数时,
   把它导出到 GCS 的函数表并拿到一个 ``FunctionID``;之后每次提交任务只传
   FunctionID(几十字节)。worker 首次遇到未知 FunctionID 时再去 GCS 拉函数体。
   (见 ``function_manager.py`` / ``gcs.py`` 的 ``export_function``)
2. **参数**按「值」传 —— 用 pickle 序列化,但 :class:`ObjectRef` 例外:
   它被序列化成一个占位符,worker 在**执行前**把依赖对象取回来再填进去。
   这就是 Ray 文档里的 "inlined object refs"。

   为什么必须内联?因为任务参数里可能嵌着 ``[ref1, {"k": ref2}]`` 这种结构,
   worker 拿到的是「还没算出来的值」。内联机制让 worker 只在真正需要时才去取,
   而且取的是已经就绪的对象(调度器保证了依赖就绪才调度)。

3. **``__main__`` 里定义的函数/类**按「值」传 —— 这是 cloudpickle 的关键贡献。
   如果用户的脚本是 ``python train.py``,函数 ``__module__`` 是 ``__main__``,
   在 worker 进程里 ``import __main__`` 拿到的是**另一个**模块对象,函数根本找不到。
   所以 Ray 必须把函数体、闭包、被引用的全局变量一起打包送过去。

mini-ray 完整实现了这三条。没有 cloudpickle 依赖(环境里通常没有),
所以这里自带一个「cloudpickle-lite」:用 ``marshal`` 打包 code object,
再把被引用的全局变量、闭包 cell、默认值一起带上。

.. code-block:: text

   驱动进程                                     worker 进程
   ─────────                                    ───────────
   f.remote(x, ref)                             task 消息 = (FunctionID, args_bytes)
      │                                                  │
      ├─ 导出/查表 ──▶ FunctionID                          ├─ 查函数表(有缓存)
      ├─ args_bytes = dumps([x, ref],                      ├─ 取依赖对象(含内联 ref)
      │        inline_object_refs=True)                    ├─ loads(args_bytes, ref_values)
      └─ RPC: submit_task(FunctionID, args_bytes) ────────▶└─ 执行

序列化钩子(对齐 Ray 的 ``ray.util.serialization``)::

    from miniray.util.serialization import register_serializer
    register_serializer(MyClass, lambda o: o.to_bytes(), lambda b: MyClass.from_bytes(b))
"""

from __future__ import annotations

import builtins
import importlib
import marshal
import pickle
import sys
import threading
import types
from typing import Any, Callable, Dict, Optional, Tuple, Type

from .errors import MiniRayError

__all__ = [
    "dumps",
    "loads",
    "extract_object_ids",
    "register_serializer",
    "register_object_ref_type",
    "PicklingError",
]

PicklingError = pickle.PicklingError

# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

#: 自定义序列化器:类 -> (reducer, deserializer)
_CUSTOM_SERIALIZERS: Dict[type, Tuple[Callable[[Any], Any], Callable[[Any], Any]]] = {}

#: ObjectRef 类型(由 object_ref.py 在导入时注册,避免循环导入)
_OBJECT_REF_TYPE: Optional[type] = None

#: 模块可否导入的缓存
_MODULE_IMPORTABLE: Dict[str, bool] = {}

#: 「需要按值序列化」判定的缓存:type/function 对象 id -> bool
_BY_VALUE_CACHE: Dict[int, bool] = {}

_PERSISTENT_REF_TAG = b"MINIRAY_OBJREF"


def register_serializer(
    cls: type,
    reducer: Callable[[Any], Any],
    deserializer: Callable[[Any], Any],
) -> None:
    """注册自定义序列化器(对齐 ``ray.util.serialization.register_serializer``)。

    ``reducer`` 把对象转成「可 pickle 的形式」,``deserializer`` 反过来。
    典型用途:把第三方库里不可 pickle 的对象(句柄、连接)转成 bytes。
    """
    _CUSTOM_SERIALIZERS[cls] = (reducer, deserializer)


def register_object_ref_type(cls: type) -> None:
    """由 :mod:`miniray.object_ref` 在导入时调用。"""
    global _OBJECT_REF_TYPE
    _OBJECT_REF_TYPE = cls


# --------------------------------------------------------------------------
# 判定:什么时候必须「按值」序列化一个函数/类
# --------------------------------------------------------------------------


def _module_importable(module_name: Optional[str]) -> bool:
    """模块能否在**另一个**进程里被 import 到。

    这里踩过一个坑:``builtins`` 这类内置模块没有 ``__file__``,只看 ``__file__``
    会把 ``object``/``int`` 这些内置类型误判成「不可导入」→ 走按值序列化 →
    序列化 ``object.__new__`` 时又碰到 ``object`` 自身 → 无限递归。
    所以判定顺序是:``__file__`` → ``__spec__`` → ``find_spec``。
    """
    if not module_name:
        return False
    cached = _MODULE_IMPORTABLE.get(module_name)
    if cached is not None:
        return cached
    ok = False
    if module_name in sys.modules:
        mod = sys.modules[module_name]
        if getattr(mod, "__file__", None) is not None:
            ok = True
        elif getattr(mod, "__spec__", None) is not None:
            # 内置模块 / 冻结模块 / 已经正常导入过的包:__spec__ 一定非空
            ok = True
    else:
        try:
            ok = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError, AttributeError):
            ok = False
    _MODULE_IMPORTABLE[module_name] = ok
    return ok


def _name_resolves_to(module_name: str, qualname: str, obj: Any) -> bool:
    """``module.qualname`` 现在指向的是不是**这个**对象?

    这一条比看起来重要。考虑最常见的 Ray 代码::

        @ray.remote
        def f(x): ...

    装饰器把模块属性 ``f`` 换成了 ``RemoteFunction`` 包装器,原始函数对象
    只能通过 ``f._function`` 拿到。此时按引用序列化 ``<function f>`` 会失败
    (pickle 会去 ``module.f`` 找一个**不是它**的东西),必须按值传。

    所以判据不是「模块可导入吗」,而是「模块里那个名字还指向它吗」。
    """
    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            return False
    target: Any = module
    for part in qualname.split("."):
        if part == "<locals>":
            return False
        target = getattr(target, part, None)
        if target is None:
            return False
    return target is obj


def _needs_by_value(obj: Any) -> bool:
    """判断一个函数/类是否必须**按值**序列化。

    按值的四种情况:

    * ``__main__`` 里定义的(脚本、REPL、Jupyter 里的函数)——
      worker 进程里的 ``__main__`` 是另一个模块,按引用一定失败;
    * 嵌套函数 / lambda(``<locals>``)—— 它们根本不是模块级属性,无法按引用找;
    * 所在模块不可导入(finder 找不到);
    * **模块里那个名字已经指向别的对象了**(被 ``@ray.remote`` 之类的装饰器换掉了)。
    """
    key = id(obj)
    cached = _BY_VALUE_CACHE.get(key)
    if cached is not None:
        return cached

    result = False
    module = getattr(obj, "__module__", None)
    qualname = getattr(obj, "__qualname__", "") or ""
    name = getattr(obj, "__name__", "") or ""
    if module in ("__main__", "__mp_main__", None):
        result = True
    elif "<locals>" in qualname or name == "<lambda>":
        result = True
    elif not _module_importable(module):
        result = True
    elif not _name_resolves_to(module, qualname, obj):
        result = True
    else:
        result = False

    _BY_VALUE_CACHE[key] = result
    return result


def _is_by_value_class(cls: type) -> bool:
    if not isinstance(cls, type):
        return False
    return _needs_by_value(cls)


# --------------------------------------------------------------------------
# 按值序列化:code object + 全局变量 + 闭包
# --------------------------------------------------------------------------


def _iter_code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _iter_code_objects(const)


def _split_module_globals(values: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """把「模块对象」从全局变量里摘出来,换成模块名。

    为什么必须特殊处理?因为 **pickle 不能序列化 module 对象**
    (``pickle.dumps(time)`` 直接 TypeError)。而函数里 ``import time`` 之后
    用到的 ``time`` 就是一个模块对象 —— 这是最常见的写法之一,
    所以必须支持,否则「任何用了标准库的远程函数」都传不过去。

    做法:模块记下**名字**,反序列化端 ``importlib.import_module(name)`` 还原。
    cloudpickle 也是这么干的(``_import_module``)。
    """
    plain: Dict[str, Any] = {}
    modules: Dict[str, str] = {}
    for name, value in values.items():
        if isinstance(value, types.ModuleType):
            modules[name] = value.__name__
        else:
            plain[name] = value
    return plain, modules


def _referenced_globals(func: types.FunctionType):
    """挑出函数真正会用到的那部分全局变量(返回 ``(普通值, 模块名)``)。

    做法是遍历 code object 的 ``co_names``(即所有按名字查找的符号),
    取其中存在于 ``func.__globals__`` 的那些。这样既不漏(少一个变量就 NameError),
    也不会把整个模块的全局变量(可能几百 MB)一起打包。

    注意 ``co_names`` 对**嵌套函数也是递归覆盖**的 —— 内层函数用的是同一份 globals。
    """
    names = set()
    for code in _iter_code_objects(func.__code__):
        names.update(code.co_names)
    g = func.__globals__
    used = {name: g[name] for name in names if name in g}
    return _split_module_globals(used)


def _make_cell(value: Any):
    """造一个装着 value 的 cell 对象(标准库没有公开的构造函数)。"""
    return (lambda: value).__closure__[0]


def _closure_values(func: types.FunctionType) -> Tuple[Any, ...]:
    if func.__closure__ is None:
        return ()
    return tuple(cell.cell_contents for cell in func.__closure__)


# --------------------------------------------------------------------------
# 循环引用:延迟引用 + 载入后回填
# --------------------------------------------------------------------------
#
# 这是整个序列化层最绕的一段,值得说清楚**为什么必须这么绕**。
#
# pickle 的 save_reduce() 顺序是:先 save(func) → 再 save(args) → 写 REDUCE
# 指令 → **最后**才 memoize(obj)。也就是说,当一个函数/类在自己的参数里引用
# 自己时(memo 还没建立),pickle 会一路递归下去,直到栈溢出:
#
#     def fib(n):            # fib 的 globals 里就有 fib
#         return n if n<2 else fib(n-1)+fib(n-2)
#
#     # 类也一样常见:
#     class Node:
#         def clone(self): return Node(...)   # 方法里引用了 Node 自己
#
# cloudpickle 的解法是把闭包塞进 __setstate__ 的 state 里(state 是在 memoize
# \*之后\* 才写进流的)。函数的 __setstate__ 我们没法定制,所以换一条路:
#
#   1. 正在序列化的函数/类登记为「in progress」;
#   2. 一旦在它内部再次遇到它自己 → 写一个延迟引用 :class:`_Deferred`(带索引);
#   3. 反序列化时,每个被重建的对象按索引登记到「本次载入的注册表」;
#   4. 整个流 load 完之后做一次**回填**:把 globals 字典里、闭包 cell 里、
#      类命名空间里的 _Deferred 换成真正的对象。
#
# 兜底:_Deferred 自己实现了 __call__/__getattr__ 转发,所以即使它出现在一个
# 没被回填到的角落里(比如嵌在某个列表深处),调用仍然是对的 —— 只是 is 判断
# 会失败。这是有意的取舍:常见位置做到「完全一致」,罕见位置做到「能用」。


class _DefContext:
    """一次序列化过程中的「按值定义」登记表(每个 pickle 流一个)。

    只用一个 ``index_of`` 就够了 —— 它同时承担两个职责:

    * 分配定义编号(每个函数/类只按值发一份);
    * 判断「是不是又遇到了自己」。

    为什么不需要额外的 "in_progress" 状态?因为 pickle 自己会 memoize:
    同一个对象在**别处**再次出现时,``save()`` 开头的 memo 检查就把它拦掉了,
    根本不会调到 ``reducer_override``。唯一能二次调到的情形,就是它出现在
    **自己那份定义的参数里**(此时 save_reduce 还没来得及 memoize)—— 也就是
    我们要断开的循环引用。所以「已经发过定义」与「正在发自己的定义」是等价的。
    """

    __slots__ = ("index_of", "_next")

    def __init__(self) -> None:
        self.index_of: Dict[int, int] = {}
        self._next = 0

    def index_for(self, obj: Any) -> int:
        key = id(obj)
        index = self.index_of.get(key)
        if index is None:
            index = self._next
            self._next += 1
            self.index_of[key] = index
        return index


_LOAD_STATE = threading.local()


def _new_load_state() -> Dict[str, Any]:
    return {"registry": {}, "pending_globals": [], "pending_cells": [], "deferred": []}


def _load_state() -> Dict[str, Any]:
    state = getattr(_LOAD_STATE, "state", None)
    if state is None:
        state = _new_load_state()
        _LOAD_STATE.state = state
    return state


def _make_deferred(index: int) -> "_Deferred":
    """反序列化端:_Deferred 的构造函数(被 pickle 按引用调用)。

    每个新建的占位符都登记进本次载入的 ``deferred`` 列表,载入结束时统一
    「接上真身」—— 这样占位符自身持有目标,不依赖任何全局状态存活。
    """
    marker = _Deferred(index)
    _load_state()["deferred"].append(marker)
    return marker


class _Deferred:
    """一个「还没建好」的函数/类的占位符。

    为什么需要它?见上面「循环引用」那段注释:pickle 在 ``save_reduce`` 里
    先序列化参数、后 memoize,所以「函数在自己参数里引用自己」没法用 memo 断开。
    更麻烦的是,pickle 会把**这个占位符**记成那个对象的规范表示 —— 也就是说
    在整条流里,对 ``fib`` 的所有引用都指向同一个占位符实例(身份是一致的)。

    因此这里做了三层保障:

    1. 载入结束时 :func:`_fixup_definitions` 会把它从 globals/cell/命名空间
       里换成真对象,顶层 dict/list 里的也一并换掉 —— 绝大多数场景拿到的是真函数;
    2. 万一它出现在更深的角落,``__call__`` / ``__getattr__`` 会转发到真对象,
       调用、取属性都是对的;
    3. ``__class__`` 也转发,于是 ``isinstance(f, types.FunctionType)`` 同样成立。
    """

    __slots__ = ("index", "_target")

    def __init__(self, index: int) -> None:
        self.index = index
        self._target: Any = None

    # -- 解析 -----------------------------------------------------------
    def _resolve(self):
        if self._target is not None:
            return self._target
        target = _load_state()["registry"].get(self.index)
        if target is None:
            raise MiniRayError(
                f"延迟引用 #{self.index} 没有被解析(序列化流不完整?)。"
                "这是 mini-ray 序列化层的 bug,请附上复现代码提 issue。"
            )
        return target

    # -- 透明转发 -------------------------------------------------------
    def __call__(self, *args: Any, **kwargs: Any):
        return self._resolve()(*args, **kwargs)

    def __getattr__(self, name: str):
        if name in ("index", "_target"):  # __slots__ 还没赋值时的兜底
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    @property
    def __class__(self):  # type: ignore[override]
        return type(self._resolve())

    def __repr__(self) -> str:  # pragma: no cover - 调试
        if self._target is None:
            return f"<miniray._Deferred #{self.index}>"
        return repr(self._target)

    def __reduce__(self):
        # 重新序列化时直接用真身,不要把一个未解析的占位符再传出去
        return (_identity, (self._resolve(),))


def _identity(value: Any) -> Any:
    return value


def _register_definition(index: int, obj: Any) -> Any:
    state = _load_state()
    state["registry"][index] = obj
    return obj


def _fixup_definitions(root: Any = None) -> None:
    """载入结束后的回填:把代理换成真正的对象。

    三件事,按重要性排序:

    1. 让每个占位符**自己拿到真身**(``_target``)—— 这样即使它藏在很深的地方,
       也不再依赖"载入状态还活着"(载入状态在 ``loads`` 返回时就被还原了);
    2. 把 globals 字典 / 闭包 cell / 类命名空间里的占位符换成真对象,
       于是常见位置拿到的是**真正的函数对象**(类型、身份都对);
    3. 顺手把根容器(用户传进来的 dict/list)第一层里的占位符也换掉。
    """
    state = _load_state()
    registry = state["registry"]

    for marker in state["deferred"]:
        target = registry.get(marker.index)
        if target is not None:
            marker._target = target

    for container, name, index in state["pending_globals"]:
        target = registry.get(index)
        if target is not None:
            container[name] = target
    for cell, index in state["pending_cells"]:
        target = registry.get(index)
        if target is not None:
            try:
                cell.cell_contents = target  # cell 是可写的,这行才是关键
            except AttributeError:  # pragma: no cover - 极少见的空 cell
                pass

    if isinstance(root, dict):
        for key, value in list(root.items()):
            if isinstance(value, _Deferred):
                root[key] = value._resolve()
    elif isinstance(root, list):
        for i, value in enumerate(root):
            if isinstance(value, _Deferred):
                root[i] = value._resolve()

    state["pending_globals"].clear()
    state["pending_cells"].clear()
    state["deferred"].clear()


def _rebuild_function(
    code_bytes: bytes,
    name: str,
    qualname: str,
    module: str,
    globals_items: Dict[str, Any],
    module_imports: Dict[str, str],
    defaults: Any,
    kwdefaults: Any,
    closure_values: Tuple[Any, ...],
    func_dict: Dict[str, Any],
    def_index: int,
):
    """在 worker 进程里重建函数(反序列化的落地点)。"""
    gdict: Dict[str, Any] = {"__builtins__": builtins.__dict__}
    state = _load_state()
    for alias, module_name in (module_imports or {}).items():
        try:
            gdict[alias] = importlib.import_module(module_name)
        except ImportError:  # 对面环境没有这个模块 —— 只在真正用到时才会报错
            pass

    # 先把非延迟项填进 globals;延迟项留个代理占位,等回填阶段换掉
    for key, value in globals_items.items():
        if isinstance(value, _Deferred):
            gdict[key] = value
            state["pending_globals"].append((gdict, key, value.index))
        else:
            gdict[key] = value

    code = marshal.loads(code_bytes)
    cells = None
    if closure_values:
        cells = []
        for value in closure_values:
            cell = _make_cell(value)
            if isinstance(value, _Deferred):
                state["pending_cells"].append((cell, value.index))
            cells.append(cell)
        cells = tuple(cells)

    fn = types.FunctionType(code, gdict, name, defaults, cells)
    fn.__qualname__ = qualname
    fn.__module__ = module
    if kwdefaults is not None:
        fn.__kwdefaults__ = kwdefaults
    if func_dict:
        # 函数自带属性里也可能挂着延迟引用(比如 func.cb = func),一并回填
        for key, value in func_dict.items():
            if isinstance(value, _Deferred):
                state["pending_globals"].append((func_dict, key, value.index))
        fn.__dict__.update(func_dict)

    # 登记自己,这样别人(包括递归的自己)的延迟引用就能解析到它
    _register_definition(def_index, fn)
    return fn


def _reduce_function(func: types.FunctionType, def_index: int):
    globals_items, module_imports = _referenced_globals(func)
    return (
        _rebuild_function,
        (
            marshal.dumps(func.__code__),
            func.__name__,
            getattr(func, "__qualname__", func.__name__),
            getattr(func, "__module__", "__main__"),
            globals_items,
            module_imports,
            func.__defaults__,
            func.__kwdefaults__,
            _closure_values(func),
            dict(func.__dict__),
            def_index,
        ),
    )


def _class_namespace(cls: type) -> Dict[str, Any]:
    """类字典的「可重建」版本。

    要丢掉几类东西(它们的语义由 ``type()`` 自己重建):
      * ``__dict__`` / ``__weakref__`` 的 slot 描述符;
      * ``__slots__`` 生成的 member_descriptor(不可 pickle);
      * ``__doc__`` 里可能含着不可 pickle 的东西? 不,保留。
    """
    ns: Dict[str, Any] = {}
    for key, value in cls.__dict__.items():
        if key in ("__dict__", "__weakref__"):
            continue
        if isinstance(value, (types.MemberDescriptorType, types.GetSetDescriptorType)):
            # __slots__ 生成的描述符:由 type() 依据 __slots__ 自动重建
            continue
        ns[key] = value
    return ns


def _rebuild_class(
    name: str,
    qualname: str,
    module: str,
    bases: tuple,
    ns: Dict[str, Any],
    module_imports: Dict[str, str],
    metaclass: type,
    def_index: int,
):
    state = _load_state()
    for alias, module_name in (module_imports or {}).items():
        try:
            ns[alias] = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover
            pass
    for key, value in list(ns.items()):
        if isinstance(value, _Deferred):
            state["pending_globals"].append((ns, key, value.index))
    new_cls = metaclass(name, bases, ns)
    new_cls.__qualname__ = qualname
    new_cls.__module__ = module
    _register_definition(def_index, new_cls)
    return new_cls


def _reduce_class(cls: type, def_index: int):
    namespace, module_imports = _split_module_globals(_class_namespace(cls))
    return (
        _rebuild_class,
        (
            cls.__name__,
            getattr(cls, "__qualname__", cls.__name__),
            getattr(cls, "__module__", "__main__"),
            cls.__bases__,
            namespace,
            module_imports,
            type(cls),
            def_index,
        ),
    )


# --------------------------------------------------------------------------
# Pickler / Unpickler
# --------------------------------------------------------------------------


class _MiniPickler(pickle.Pickler):
    """在标准 pickle 上加了三条规则(见模块 docstring)。"""

    def __init__(self, file, *, inline_object_refs: bool) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self._inline_object_refs = inline_object_refs
        self._defctx = _DefContext()

    # -- 规则 1:ObjectRef 内联 -------------------------------------------------
    def persistent_id(self, obj):
        if not self._inline_object_refs:
            return None
        if _OBJECT_REF_TYPE is not None and type(obj) is _OBJECT_REF_TYPE:
            # 注意:这里只能放「可序列化的最小信息」——对象 ID。
            # 真正的值由 worker 在执行前取回来,通过 persistent_load 填进去。
            return (_PERSISTENT_REF_TAG, obj.id.binary())
        return None

    # -- 规则 2/3:自定义序列化器 + 按值序列化函数/类 ---------------------------
    def reducer_override(self, obj):
        cls = type(obj)
        custom = _CUSTOM_SERIALIZERS.get(cls)
        if custom is not None:
            reducer, deserializer = custom
            return (deserializer, (reducer(obj),))

        if isinstance(obj, types.FunctionType):
            if _needs_by_value(obj):
                return self._by_value_reduce(obj, _reduce_function)
            return NotImplemented

        if isinstance(obj, type):
            if _is_by_value_class(obj):
                return self._by_value_reduce(obj, _reduce_class)
            return NotImplemented

        return NotImplemented

    def _by_value_reduce(self, obj: Any, reducer: Callable):
        """按值序列化一个函数/类,并处理「自己引用自己」的循环。"""
        ctx = self._defctx
        if id(obj) in ctx.index_of:
            # 它出现在自己的定义里(递归函数、方法里 new 自己的类……)
            # → 写一个延迟引用,等整个流载入完再回填成真正的对象
            return (_make_deferred, (ctx.index_of[id(obj)],))
        return reducer(obj, ctx.index_for(obj))


class _MiniUnpickler(pickle.Unpickler):
    def __init__(self, file, *, ref_values: Optional[Dict[bytes, Any]]) -> None:
        super().__init__(file)
        self._ref_values = ref_values or {}

    def persistent_load(self, pid):
        tag, object_id = pid
        if tag != _PERSISTENT_REF_TAG:  # pragma: no cover - 防御
            raise MiniRayError(f"未知的 persistent id: {pid!r}")
        try:
            return self._ref_values[object_id]
        except KeyError:
            # 正常不该发生:调度器保证依赖对象在执行前已经取回。
            # 真发生了说明依赖解析有 bug,直接报错而不是静默返回 None。
            from .ids import ObjectID

            raise MiniRayError(
                f"参数里引用的对象 {ObjectID(object_id).short()} 没有在反序列化前取回。"
                "这是依赖解析的 bug(或者对象已丢失)。"
            ) from None

    def find_class(self, module: str, name: str):
        try:
            return super().find_class(module, name)
        except (ImportError, AttributeError) as exc:
            # 给出比 "No module named xxx" 更有用的信息:这是 worker 侧最常见的错误
            raise MiniRayError(
                f"反序列化失败:无法在 worker 进程中解析 {module}.{name}。"
                f"如果它在 __main__ 里定义,mini-ray 会按值传过去,不应该走到这里;"
                f"否则请确认该模块在 worker 进程中可导入(工作目录相同、已安装)。"
                f"原因: {exc!r}"
            ) from exc


# --------------------------------------------------------------------------
# 公共 API
# --------------------------------------------------------------------------


def _dumps_once(value: Any, *, inline_object_refs: bool) -> bytes:
    import io

    buf = io.BytesIO()
    _MiniPickler(buf, inline_object_refs=inline_object_refs).dump(value)
    return buf.getvalue()


def dumps(value: Any, *, inline_object_refs: bool = False) -> bytes:
    """序列化成 bytes。

    :param inline_object_refs: ``True`` 时把 :class:`ObjectRef` 替换成占位符,
        留给 worker 在执行前填值(任务参数必须用 ``True``);
        ``False`` 时 ObjectRef 作为普通值传递(任务**返回值**里如果含 ref,用它)。

    """
    try:
        return _dumps_once(value, inline_object_refs=inline_object_refs)
    except PicklingError:
        raise
    except Exception as exc:  # 把 pickle 的报错包成更好懂的提示
        raise PicklingError(
            f"无法序列化 {type(value).__name__} 类型的值: {exc!r}。"
            "提示:远程函数的参数/返回值必须可 pickle;"
            "持有锁、文件句柄、网络连接的全局变量需要在 worker 里重新创建。"
        ) from exc


def extract_object_ids(data: bytes) -> list:
    """从 pickle 流里**扫出**被内联的 ObjectRef(不反序列化)。

    调度器需要知道「这个 task 依赖哪些对象」才能建依赖图。有两条路:

    1. 反序列化一遍,遍历对象图找 ObjectRef —— 会触发用户对象的构造/副作用,慢;
    2. 直接扫 pickle 指令流,找 persistent id 操作数 —— 快、无副作用。

    Ray 的做法是第 1 种(序列化时就把 ref 列表单独算出来)。mini-ray 用第 2 种:
    ``pickletools.genops`` 遍历所有 bytes 操作数,再用 ObjectID 的类型码过滤 ——
    随机 16 字节恰好以 ``b"OBJ\\x00"`` 结尾的概率可以忽略。

    .. note::
       这是「元数据与数据分离」的一个缩小版实践:任务消息里只需要 ref 的 ID
       就能建依赖图,不需要把对象本身读进来。
    """
    if not data:
        return []
    import io
    import pickletools

    from .ids import ObjectID

    # 直接用类型码(而不是 ObjectID.nil() 的尾巴)—— nil ID 的类型码是保留的,
    # 两者现在等价,但显式取类型码更不容易被后人改坏。
    suffix = ObjectID._type_code  # noqa: SLF001
    found: list = []
    seen = set()
    try:
        for opcode, arg, _pos in pickletools.genops(io.BytesIO(data)):
            if (
                opcode.name in ("SHORT_BINBYTES", "BINBYTES", "BINBYTES8")
                and isinstance(arg, bytes)
                and len(arg) == 16
                and arg[-4:] == suffix
                and arg not in seen
            ):
                seen.add(arg)
                found.append(arg)
    except Exception:  # pragma: no cover - 流损坏时不阻塞提交,让后续步骤报错
        return found
    return found


def loads(data: bytes, ref_values: Optional[Dict[bytes, Any]] = None) -> Any:
    """反序列化。

    :param ref_values: ``{ObjectID 的二进制: 已经取回的值}``,用于填充占位符。

    ``loads`` 结束时必须跑一次 :func:`_fixup_definitions` —— 把「延迟引用」
    换成真正重建出来的函数/类。这一步是递归函数/类能工作的关键。
    """
    import io

    previous = getattr(_LOAD_STATE, "state", None)
    _LOAD_STATE.state = _new_load_state()
    try:
        obj = _MiniUnpickler(io.BytesIO(data), ref_values=ref_values).load()
        _fixup_definitions(obj)
        return obj
    finally:
        # 支持「自定义 deserializer 里再调 loads」的嵌套场景
        if previous is None:
            try:
                del _LOAD_STATE.state
            except AttributeError:  # pragma: no cover
                pass
        else:
            _LOAD_STATE.state = previous


# --------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * Ray 直接用 cloudpickle;mini-ray 自带精简实现,只覆盖「函数/类按值」这一
#     核心场景(不含 cloudpickle 对 generator、module、sliced 对象的特殊处理)。
#   * Ray 在 GCS 里维护函数表并且**按 hash 去重**;mini-ray 同样实现了函数表
#     (见 function_manager.py),但去重键是「模块名 + 函数名 + 代码 hash」。
#   * ``ray.register_serializer`` 只能注册不能注销,mini-ray 提供了
#     ``_CUSTOM_SERIALIZERS.clear()`` 作为测试后门(见 tests)。
# --------------------------------------------------------------------------
