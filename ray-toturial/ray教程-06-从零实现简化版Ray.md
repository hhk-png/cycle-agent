仓库地址：https://github.com/hhk-png/cycle-agent

# 第 6 章：从零实现一个简化版 Ray（mini-ray）

> 这一章是教程的核心。我们用**纯 Python 标准库 + NumPy** 从零写一个可以真正运行的
> 最小 Ray —— 任务调度、对象存储、Actor、容错、可观测性全都在。代码在
> `ray-toturial/mini-ray/`，25 个顶层模块（含子包共 35 个文件）、约 1.1 万行，**192 个测试全部通过**。
> 读完这一章，你对 Ray 的理解不会再停留在"会用"。

---

## 6.1 为什么要写一个简化版

### 理由一：只有写过，才知道哪些是「必须的」

看 Ray 的文档，你会觉得那些机制都是理所当然的。真写一遍才会发现：

* 为什么对象存储必须是**共享内存**而不是一个 dict？—— 因为否则每次 `ray.get`
  都要拷贝，1GB 的数组会变成性能灾难；
* 为什么 worker 必须是**进程**而不是线程？—— 因为任务会崩溃、会改全局状态、
  要独占 GPU；
* 为什么要有 **owner** 和引用计数？—— 因为内存回收需要一个跨进程的共识；
* 为什么 `ray.get` 拿到的数组是**只读**的？—— 因为共享内存必须有不可变约定。

### 理由二：分布式系统的坑，只有在真的跨进程时才会出现

这一章最有价值的部分不是"我实现了什么"，而是 **6.11 节那 12 个真实的坑**：
pickle 的 `save_reduce` 顺序、`__builtins__` 在模块里是 dict、
`multiprocessing.spawn` 会重新 import 用户脚本、参数名冲突导致服务端超时被当成
客户端超时……这些坑在论文和文档里都不会写，但它们是分布式系统的**真实形状**。

### 理由三：验证「我真的懂了」

读完教程只能证明你读懂了；写完并跑通 192 个测试，才能证明机制真的工作。

### mini-ray 的三个硬约束

1. **零第三方依赖**：只用标准库 + NumPy。没有 cloudpickle？自己写一个精简版。
   没有 plasma？用 `multiprocessing.shared_memory` 自己搭。
2. **API 与 Ray 对齐**：名字、参数、语义、异常都要对得上，
   `import miniray as ray` 之后代码基本不用改。
3. **可验证**：每个机制都有对应的测试，而且测试要能跑出「预期会失败」的用例
   （比如故意让 worker 崩溃、故意丢对象）。

### 先看它能跑什么

```bash
cd mini-ray
python -m pytest tests/ -q          # 192 个测试
python examples/06_fault_tolerance.py   # lineage 重建演示
```

```python
import miniray as ray

@ray.remote
def square(x):
    return x * x

@ray.remote
class Counter:
    def __init__(self):
        self.n = 0
    def inc(self):
        self.n += 1
        return self.n

ray.init(num_cpus=4)
print(ray.get([square.remote(i) for i in range(8)]))    # [0, 1, 4, 9, 16, 25, 36, 49]
counter = Counter.remote()
print(ray.get([counter.inc.remote() for _ in range(5)]))  # [1, 2, 3, 4, 5]
ray.shutdown()
```

---

## 6.2 核心设计决策：怎么把分布式塞进一个进程

真实 Ray 的组件都是独立进程（GCS/raylet 是 C++ 进程，worker 是 Python 进程）。
mini-ray 要在**一台机器、零依赖**的前提下保留同样的**语义**，于是做了这些取舍：

| 设计问题 | 真实 Ray | mini-ray 的决策 | 代价 |
|---|---|---|---|
| 控制面放哪 | GCS/raylet 独立进程 | **driver 进程里的两个线程**（RPC 服务 + 调度循环） | driver 退出 = 集群结束 |
| 多节点怎么模拟 | 真的多机 | `num_nodes` 参数切分资源，每节点一个对象存储 | 跨节点传输退化成一次进程内拷贝 |
| 进程间通信 | gRPC + protobuf | TCP + 4 字节长度前缀 + pickle（`rpc.py`） | 无跨语言能力 |
| worker 怎么拉起 | `subprocess` 直接执行 worker 脚本（`setup_worker.py` 包装 `default_worker.py`） | **同样**（见 6.6，这是踩坑后的选择） | 无 |
| 对象存储 | Plasma（C++ mmap） | `multiprocessing.shared_memory` + 分桶分配器 | 无 mmap 读回、无独立 IO 池 |
| 函数序列化 | cloudpickle | 自带的 cloudpickle-lite | 边界情况更少（见 6.4） |
| 调度 | 多策略 + lease | 单策略（本地性 → 最空闲）+ 直接指派 | 无抢占/优先级 |

```
                        driver 进程(你的脚本)
  ┌───────────────────────────────────────────────────────────────┐
  │  GCS 服务线程             Raylet 服务线程                      │
  │  ├ 节点表                 ├ 调度循环(事件驱动)                 │
  │  ├ 函数表                 ├ 对象存储 × num_nodes               │
  │  ├ 对象目录  ◀────────────┤ 对象目录缓存 + 跨节点拉取            │
  │  ├ actor 表 ◀─────────────┤ worker 池(subprocess)             │
  │  └ 放置组表 ◀─────────────┤ actor 运行时 + 血缘账本             │
  │                                                               │
  │  Driver CoreWorker ──────────────┐                            │
  └──────────┬───────────────────────┼────────────────────────────┘
             │ TCP(127.0.0.1:随机端口)│
             ▼                       ▼
      worker 进程 1 … N        actor 进程(每 actor 一个)
      └ 每个 worker 里有一个 CoreWorker(自己的连接)
```

**这个结构的最大好处是可调试**：所有状态都在一个进程里，
`pdb` 可以停在调度器里，State API 不需要跨进程查询。

---

## 6.3 模块地图

```
mini-ray/miniray/
├── __init__.py          # 公共 API(与 ray 对齐):remote/get/put/wait/cancel/init/…
├── ids.py               # ID 体系(16 字节 = 12 随机 + 4 类型码)
├── errors.py            # 异常体系(RayTaskError/RayActorError/WorkerCrashedError/…)
├── serialization.py     # ⭐ cloudpickle-lite + ref 内联 + 依赖扫描
├── object_store.py      # ⭐ Plasma 式对象存储(共享内存/零拷贝/pin/溢出/LRU)
├── rpc.py               # 极简 RPC(线程 per 连接 + 长轮询)
├── core_worker.py       # 每个进程的「Ray 客户端」
├── raylet_client.py     # raylet 的 RPC 封装
├── gcs.py               # 集群元数据服务
├── scheduler.py         # 调度决策 + 资源记账(纯逻辑,可单测)
├── raylet.py            # ⭐ 调度循环 + 对象管理 + worker 池 + actor + 血缘
├── worker_pool.py       # worker 进程池(subprocess)
├── worker.py            # worker 进程主循环 + actor 运行时
├── execution.py         # 任务执行公共逻辑(worker 与 local_mode 共用)
├── function_manager.py  # 函数导出/取回(函数表 + 缓存)
├── remote.py            # @miniray.remote / RemoteFunction / .options() / .bind()
├── actor.py             # ActorClass / ActorHandle / ActorMethod
├── object_ref.py        # ObjectRef / ObjectRefGenerator / 引用计数
├── placement_group.py   # 放置组
├── scheduling_strategies.py
├── state.py             # State API
├── _timeline.py         # timeline(Chrome Trace + HTML 甘特图)
├── runtime.py           # init / shutdown / 全局状态
├── cli.py, __main__.py  # python -m miniray status|demo
├── util/                # ray.util 兼容命名空间
│   ├── actor_pool.py    # ActorPool
│   ├── queue.py         # Queue(async actor 承载,含背压)—— 附录 B §B.9 有走读
│   ├── metrics.py       # Counter / Gauge / Histogram
│   └── state.py 等      # state / strategies / placement_group 的兼容路径
└── _private/            # 故障注入(fault_injection)
```

下面按「最难 → 最核心」的顺序走读。

---

## 6.4 序列化层：整个项目最难的部分

**问题**：用户的 `@miniray.remote def f(x)` 定义在脚本里（`__module__ == "__main__"`）。
worker 是另一个进程，它 import 不到你的脚本 —— 函数怎么过去？

Ray 的答案是 cloudpickle。mini-ray 没有它，所以自己实现了一个精简版，
规则只有三条：

### 规则一：函数按引用传，必要时按值传

```python
def _needs_by_value(obj) -> bool:
    module = obj.__module__
    if module in ("__main__", "__mp_main__", None):     # ① 脚本/REPL/Jupyter
        return True
    if "<locals>" in obj.__qualname__ or obj.__name__ == "<lambda>":  # ② 闭包/lambda
        return True
    if not _module_importable(module):                   # ③ 模块导不进来
        return True
    if not _name_resolves_to(module, obj.__qualname__, obj):  # ④ 名字被装饰器换掉了
        return True
    return False
```

第 ④ 条是踩坑后加的，值得单独说：

```python
# 最常见的 Ray 代码
@ray.remote
def f(x): ...

# 此时 module.f 是 RemoteFunction,不是原始函数对象!
# 如果按引用序列化 f._function,pickle 会去 module.f 找一个「不是它」的东西 → 失败
```

判定标准因此不是「模块能不能导入」，而是「**模块里那个名字还指向它吗**」。

按值传时要打包什么？——**code object + 被引用的全局变量 + 闭包 cell + 默认值**：

```python
def _reduce_function(func, def_index):
    globals_items, module_imports = _referenced_globals(func)
    return (_rebuild_function, (
        marshal.dumps(func.__code__),      # 字节码(marshal 能序列化 code object)
        func.__name__, func.__qualname__, func.__module__,
        globals_items,                     # 只挑 co_names 里真正用到的全局变量
        module_imports,                    # 模块对象单独处理(见规则三)
        func.__defaults__, func.__kwdefaults__,
        _closure_values(func),             # 闭包 cell 里的值
        dict(func.__dict__),
        def_index,
    ))
```

⚠️ **只挑用到的全局变量**很关键：如果打包整个 `__globals__`（模块字典），
你会把整个模块（包括几百 MB 的数据集）复制到每个 worker。

### 规则二：参数里的 ObjectRef 变成占位符

```python
class _MiniPickler(pickle.Pickler):
    def persistent_id(self, obj):
        if not self._inline_object_refs:
            return None                      # 关闭内联时完全走普通 pickle 路径
        if type(obj) is _OBJECT_REF_TYPE:    # ← 不是 `is ObjectRef`
            return (_PERSISTENT_REF_TAG, obj.id.binary())    # 只写 ID
        return None
```

**`serialization.py` 里没有 `import ObjectRef`** —— 那样会形成
`object_ref.py → serialization.py → object_ref.py` 的循环依赖。
mini-ray 的做法是**反向注册**：由 `object_ref.py` 在 import 时调用
`register_object_ref_type(ObjectRef)`，把类型对象交给序列化层（见第 22 章 B.6）。
注册发生在模块级，所以 `_OBJECT_REF_TYPE` 在真正用到时一定已经就绪。

worker 执行前先把依赖取回来，再用 `persistent_load` 填进去。

这一条同时带来一个额外好处：**依赖图可以从序列化结果里直接扫出来**，
不需要反序列化：

```python
def extract_object_ids(data: bytes) -> list:
    """扫 pickle 指令流,找 16 字节且以 OBJ 类型码结尾的 bytes 操作数。

    返回 list(而不是生成器),并且**去重** —— 同一个 ref 在参数里出现两次时,
    依赖图里只需要一条边。
    """
    ...
    suffix = ObjectID._type_code          # 直接用类型码,不用硬编码常量
    found, seen = [], set()
    for opcode, arg, _pos in pickletools.genops(io.BytesIO(data)):
        if (opcode.name in ("SHORT_BINBYTES", "BINBYTES", "BINBYTES8")
                and isinstance(arg, bytes)
                and len(arg) == 16
                and arg[-4:] == suffix
                and arg not in seen):
            seen.add(arg)
            found.append(arg)
    return found
```

> 注意三个 bytes 操作码都要覆盖：`SHORT_BINBYTES`（<256 字节，pickle 协议 4 起
> 最常用）、`BINBYTES`、`BINBYTES8`。只写 `endswith("BINBYTES")` 会漏掉
> `SHORT_BINBYTES` —— 而 16 字节的 ID 恰好就走那条路径。

### 规则三：模块对象按名字传

`pickle` **不能**序列化 module 对象（`pickle.dumps(time)` 直接 TypeError）。
而 `import time` 之后在函数里用 `time.sleep` 是最常见的写法，
所以必须支持：

```python
def _split_module_globals(values):
    plain, modules = {}, {}
    for name, value in values.items():
        if isinstance(value, types.ModuleType):
            modules[name] = value.__name__      # 记名字
        else:
            plain[name] = value
    return plain, modules
# 反序列化端:gdict[alias] = importlib.import_module(module_name)
```

### 循环引用：这一节是全文最绕的地方

递归函数、方法里 new 自己的类 —— 这些都会让「函数引用自己」。天真实现会栈溢出，
因为 **pickle 的 `save_reduce` 顺序是：先序列化参数、最后才 memoize 对象**：

```
save_reduce(obj=f, func=rebuild, args=(…))
   ├─ save(func)
   ├─ save(args)        ← 参数里的 f 又会被 reducer_override 处理
   ├─ write(REDUCE)
   └─ self.memoize(obj) ← 太晚了!此时已经在无限递归里了
```

更麻烦的是：如果我们在第二次遇到 `f` 时返回一个「延迟引用」占位符，
**pickle 会把那个占位符记成 `f` 的规范表示**（因为 `save_reduce` 里 memoize 的是
那个占位符）。结果是整条流里对 `f` 的所有引用都指向占位符，
连最外层的那个值也变成了占位符。

mini-ray 的解法是**两段式：延迟引用 + 载入后回填**：

```
序列化时(每个 pickle 流一个 _DefContext)
  第一次遇到 f  → 分配 def_index,正常按值写定义
  又遇到 f      → 说明它在自己的定义里 → 写 (_make_deferred, (index,))

反序列化时
  _rebuild_function(...) 建出函数后:
     ① registry[def_index] = fn
     ② 把 globals 里的 _Deferred 占位符登记到待回填列表
     ③ 闭包 cell 里的占位符同样登记
  loads() 结束时统一回填:
     gdict[name] = registry[index]        (换掉 dict 里的占位符)
     cell.cell_contents = registry[index] (cell 是可写的!)
     顶层 dict/list 里的占位符也换掉
```

兜底：`_Deferred` 自己实现了 `__call__` / `__getattr__` / `__class__` 转发，
所以即使它藏在某个没被回填到的角落（比如 globals 里某个 list 的深处），
**调用和取属性都是对的**，只是 `is` 判断不成立。这是有意的取舍：
常见位置做到"完全一致"，罕见图位置做到"能用"。

> cloudpickle 是怎么做的？它把闭包放进 `__setstate__` 的 **state** 通道 ——
> 而 state 是在 memoize **之后**才写进流的，所以不存在这个问题。
> 函数的 `__setstate__` 我们没法定制，所以走了另一条路。

### 函数注册表：不要每次提交都传函数体

函数体可能有几十 KB，每次提交都带一份太浪费。Ray 在 GCS 里维护函数表：

```
driver                          GCS                     worker
──────                          ───                     ──────
f.remote(x)
  ├─ export(f) → sha1(blob) 去重 ──▶ 函数表(id → blob)
  ├─ 提交任务(FunctionID, args) ─────────────────────────▶
                                                        get_function(id)  (首次)
                                                        loads(blob) → f,然后缓存
```

mini-ray 完整实现（`function_manager.py`）。去重键是**内容哈希**，
因为同一个函数可能被不同名字引用（装饰器、别名）。

---

## 6.5 对象存储：Plasma 的缩小版

### 分桶共享内存分配器

```python
_MIN_BUCKET_SHIFT = 12   # 4KB
_MAX_BUCKET_SHIFT = 28   # 256MB —— 必须钳制,否则超大对象会算出 1<<29 的桶
_SLOTS_PER_SEGMENT = 8

class SharedMemoryAllocator:
    """按 2 的幂分桶 + bump 分配 + free list。"""

    @staticmethod
    def _bucket_for(size: int) -> int:
        shift = max(_MIN_BUCKET_SHIFT, (size - 1).bit_length())
        return min(shift, _MAX_BUCKET_SHIFT)        # ← 上限钳制

    def allocate(self, size):
        size = max(1, int(size))
        bucket = self._bucket_for(size)
        slot = 1 << bucket
        with self._lock:
            state = self._buckets.get(bucket)       # ← 不是 setdefault
            if state is None:
                state = self._new_segment(bucket)
                self._buckets[bucket] = state
            if state["free"]:
                offset = state["free"].pop()        # 复用释放的槽
            else:
                offset = state["offset"]
                if offset + slot > state["size"]:
                    state = self._new_segment(bucket)   # 装不下就开新段
                    self._buckets[bucket] = state
                    offset = 0
                state["offset"] = offset + slot
            return BlockHandle(state["shm"].name, offset, size, bucket)
```

> ⚠️ 这里有个**照抄会亏性能的坑**：写成
> `state = self._buckets.setdefault(bucket, self._new_segment(bucket))`
> 看起来更 Pythonic，但 `setdefault` 的**默认值总会被求值** ——
> 于是**每次 allocate 都会先白白创建一个 `SharedMemory` 段**（然后立刻丢掉）。
> 必须用 `get` + `if None` 的写法。

和 Plasma 一样：实现极简、分配 O(1)，代价是内部碎片。

### 零拷贝：数据只存一份

写入时只做一次 memcpy：

```python
def put_ndarray(self, object_id, array):
    nbytes = array.nbytes
    with self._lock:
        self._require_space(nbytes, object_id)      # 先按容量做回收/溢出
        block = self._allocator.allocate(nbytes)
    if block is None:                               # 分配器被禁用 → 退回 pickle
        self.put_bytes(object_id, pickle.dumps(array, protocol=5))
        return
    shm = _open_shm(block.shm_name)
    dest = _np.ndarray(array.shape, array.dtype, buffer=shm.buf, offset=block.offset)
    dest[:] = array                                 # ← 唯一的一次 memcpy
    descriptor = ndarray_descriptor(array, block)
    with self._lock:
        self._insert(object_id, size=nbytes, plain=None,
                     block=block, descriptor=descriptor)
```

注意 `_insert` 的签名是 **keyword-only** 的
（`_insert(self, object_id, *, size, plain, block, descriptor=None)`）——
`size` 与 `plain` 是必填，只传 `block=` 和 `descriptor=` 会 `TypeError`。

读取时在**消费者进程**里建视图（零拷贝）：

```python
def create_ndarray_view(descriptor):
    shm = _open_shm(descriptor["shm"])          # 按名字 attach 到同一块内存
    arr = np.ndarray(shape, dtype, buffer=shm.buf, offset=descriptor["offset"])
    arr.flags.writeable = False                  # 不可变,所以能安全共享
    return arr
```

测试里有一个「跨进程写穿」的用例：子进程改了数组的第一个元素，
父进程立刻能看到 —— 这才叫零拷贝。

### 只读 + pin：两个必须一起讲的约束

* **只读**：对象不可变是共享内存能安全使用的前提。想改就 `.copy()`。
* **pin**：只要还有进程持有指向这块内存的 numpy 视图，这块内存
  **既不能被驱逐也不能被溢出**（否则视图悬空 → 段错误）。

pin 的释放时机是个设计要点：

```python
array = ray.get(ref)
# pin 应该跟着 **array** 的生命周期,而不是 ref!
# 因为用户可能:  arr = ray.get(ref); del ref; 继续用 arr
counter = self.references

def _release(oid: str = object_id_hex) -> None:
    counter.add(f"__unpin__:{oid}", 1)     # 走引用计数器,而不是发 RPC

weakref.finalize(array, _release)          # ← 数组被 GC 时自动 unpin
```

`_release` 是个**零参闭包**（`oid` 用默认参数绑定，避免 `finalize` 传参的坑），
它不直接发 RPC，而是往引用计数器里塞一个 `__unpin__:<oid>` 的**负引用**，
由计数器按 0.05 秒批量上报给 raylet。

Ray 用的是同样的思路（`PlasmaBuffer`）。mini-ray 用 `weakref.finalize` 实现 ——
顺带避开一个坑：**不能**在 `ObjectRef.__del__` 里发 RPC（解释器退出时会炸）。

### 写满时的顺序：回收 → 溢出 → 报错

```python
def _enforce_capacity(self, extra):
    attempts = 0
    while self._used + extra > self.capacity_bytes:
        attempts += 1
        if attempts > 100000:                        # 防御死循环
            break
        if not self._evict_one(reclaim_only=True):   # ① refcount==0 且无 pin → 删
            # ② 溢出是**可关闭的**(enable_spilling),关了就直接进 ③
            if not self.enable_spilling or not self._evict_one(reclaim_only=False):
                self.num_full_errors += 1
                raise ObjectStoreFullError(...)      # ③ 绝不静默丢数据
```

注意第二个分支里的 **`self.enable_spilling`** —— 关掉溢出
（`enable_object_spilling=False`）时，内存不够会**直接报
`ObjectStoreFullError`**，而不是溢出到磁盘。这也是第 22 章练习 2
「关掉溢出观察报错」能成立的原因。

**顺序不能换**：先丢掉「用户已经拿不到」的对象，再把「还可能被用到」的搬到磁盘，
最后才报错。被 pin 住的对象**永远不动** —— 宁可报 `ObjectStoreFullError`，
也不让用户手里的数组变成野指针。

---

## 6.6 worker 为什么用 `subprocess` 而不是 `multiprocessing`

这一节值得单独写，因为它是**踩坑后改的设计**，而且直接对应用户体验。

`multiprocessing` 的 spawn 模式在启动子进程时，会让子进程**重新 import 用户的主模块**
（为了能反序列化主模块里定义的对象）。后果：

```python
# 用户脚本 train.py
import ray
ray.init()                     # ← 没有 if __name__ == "__main__" 保护
@ray.remote
def f(): ...
ray.get(f.remote())
```

worker 启动 → 重新 import `train.py` → **又执行了一遍 `ray.init()` 并提交任务** →
在「正在 import 主模块」的状态下再启动进程 → `RuntimeError: An attempt has been
made to start a new process before the current process has finished its
bootstrapping phase`。这就是社区里流传的「Ray 脚本必须写 `if __name__ == '__main__'`」
说法的来源（真实 Ray 的 worker 由 `subprocess` **直接执行 worker 脚本**启动、
**不重新 import 用户脚本**，所以**大多数**情况下确实不需要它 —— 但**不是所有情况**：
`ray.util.multiprocessing`（spawn 语义）、以及 **Windows** 上的若干场景
仍然要求主模块保护。见第 4 章 §4.8）。

**真实 Ray 的做法**是 `subprocess` 直接执行 worker 脚本：
`<python> …/workers/setup_worker.py …/workers/default_worker.py --node-ip-address=…`
（源码 `services.py` 的 `start_worker_command`），worker 配置走命令行参数。
这样子进程是一个干净的解释器，
根本不知道用户脚本的存在 —— 用户的函数是**按值**序列化过去的。

mini-ray 照做：

```python
_WORKER_BOOTSTRAP = """
import sys, pickle
with open(sys.argv[1], "rb") as fh:
    config = pickle.load(fh)
sys.path[:0] = [p for p in config.get("sys_path", []) if p]   # 恢复父进程的 sys.path
from miniray.worker import worker_main
worker_main(config)
"""

process = subprocess.Popen(
    [sys.executable, "-u", "-c", _WORKER_BOOTSTRAP, config_path],
    cwd=os.getcwd(),           # stdout/stderr 直接继承(worker 的 print 出现在同一终端)
)
```

效果：**普通脚本、`python -c`、Jupyter、pytest 里都能直接跑，不需要任何保护。**

---

## 6.7 调度器：依赖反查表 + 资源记账

调度器（`scheduler.py`）是**纯逻辑**：不做任何 I/O，所以可以单独写单元测试。

### 依赖：反向索引 + 事件驱动

```python
def add_task(self, record, *, ready_objects=None):
    """注意 ready_objects:提交这一刻**已经就绪**的依赖。"""
    missing = record.deps - (ready_objects or set())
    record.missing_deps = len(missing)
    for dep in missing:
        self._waiting.setdefault(dep, set()).add(record.task_id.hex())
    if record.missing_deps == 0:
        self._make_ready(record)

def mark_object_ready(self, object_id):
    newly_ready = []
    for task_hex in self._waiting.pop(object_id, ()):     # ← 反查:谁在等它
        record = self.tasks.get(task_hex)                 # ← get,不是 []
        if record is None or record.state is not TaskState.PENDING:
            continue                                      # ← 状态守卫
        record.missing_deps -= 1
        if record.missing_deps <= 0:
            newly_ready.append(record)
    ...
```

**`get` + 状态守卫这两处不能省**：任务可能已经被取消、已经失败、
或者已经在别的路径上被判定为就绪 —— 此时再减一次 `missing_deps`
就会把计数减成负数，任务被重复派发。

`ready_objects` 这个参数是**踩坑后加的**（见 6.11 的第 6 条坑）：
把已有的 ObjectRef 当参数传给新任务时，如果只按「依赖个数」计数，
那个依赖永远不会再触发一次「就绪」事件，任务就永久卡在 PENDING。

### 资源模型

```python
@dataclass
class Resources:
    cpu: float; gpu: float; memory: float; custom: Dict[str, float]
```

节点的账本是
`NodeResources(node_id, total, available, pg_free, pg_gpus, gpus_free)`，
放置组预留的资源从 `available` 里**扣走**，放进 `pg_free`（GPU 部分进 `pg_gpus`）——
只有使用该 bundle 的任务能用，这是「放置组能保证不卡死」的实现基础。

调度决策（简化版 hybrid）：

```python
def pick_node(self, record):
    # ① 放置组:资源必须从该 bundle 的预留下出
    # ② 硬约束:节点亲和/放置组 bundle 不满足 → 等
    # ③ 依赖本地性:依赖对象就在这个节点 → 优先(省一次跨节点传输)
    # ④ 否则:最空闲的节点(利用率最低)
```

### 放置组：先在副本上试算,再真正扣

```python
def allocate_placement_group(self, bundles, strategy, nodes):
    sim = {nid: node.available.copy() for nid in nodes}    # ← 在副本上试
    ...
    return assignment                                      # 失败时集群状态不变
```

四种策略的语义（与 Ray 一致）：`STRICT_PACK`（全在同一节点）、
`PACK`（尽量少用节点）、`SPREAD`（尽量摊开）、`STRICT_SPREAD`（每 bundle 一个节点）。

---

## 6.8 Raylet：一个文件里的六个职责

`raylet.py` 是整个项目最大的文件（**2227 行**，占全部代码的 1/5），因为它承担了真实 raylet 的
全部职责：

```
┌─ Raylet ───────────────────────────────────────────────────────────────┐
│ 1. 调度循环      事件驱动:任务就绪/对象就绪/worker 空闲 → 派活          │
│ 2. 对象管理      put/fetch/wait/引用计数/丢失重建                       │
│ 3. worker 池     按需拉起、复用、空闲退休、崩溃回收                      │
│ 4. actor 生命周期 创建/邮箱/并发组/重启/杀死                            │
│ 5. 血缘账本      哪个 task 产出了哪个对象 → 对象丢了能重算               │
│ 6. 放置组        资源预留 + 分配                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### 调度循环：所有「等」都是条件变量

```python
def _scheduler_loop(self):
    while not self._stopping.is_set():
        self._schedule_needed.wait(timeout=_TICK)     # 事件 + 0.2s 兜底 tick
        self._schedule_needed.clear()
        self._schedule_tasks()                        # 派活
        self._process_pending_placement_groups()      # 重试放置组分配
        self._process_pending_actors()                # 重试 actor 分配
        self._pool.reap()                             # 回收死掉的 worker
        self._retire_idle_workers()                   # 退休闲置太久的 worker
```

最后一行 `_retire_idle_workers()` 对应 mini-ray 自己的配置项
`worker_idle_timeout_ms`（默认 10000）：
worker 闲置超过这个时间就被退休（进程退出），把内存还给系统 ——
> ⚠️ 这个名字是 **mini-ray 的**，真实 Ray 2.58 里**没有** `worker_idle_timeout_ms`
> （那是 Ray 1.x 的旧常量）；真实 Ray 现在用 `kill_idle_workers_interval_ms`
> + `idle_worker_killing_time_threshold_ms`(1s) + 1 GiB 内存门槛，见第 3 章 §3.4。
否则一个跑过 8 个任务、每个都 import 过 pandas 的池子会一直占着内存。

**为什么事件驱动而不是轮询？** 因为分布式系统里「等」是最贵的。
三类等待全部用条件变量：任务等依赖（调度器的反查表）、
worker 等任务（长轮询挂在 `WorkerSlot.cond`）、driver 等结果（`ray.get` 挂在 raylet 的条件变量）。
长轮询还顺便做了心跳：1 秒超时返回 `noop`，worker 卡死了立刻能发现。

### worker 派活：空闲复用 vs 按需拉起

```python
def _dispatch(self, record, node_id):
    idle = [s for s in self._pool.idle_slots(node_id) if s.worker_type == "task"]
    if idle:
        slot = idle[0]
        try:
            self._scheduler.assign(record, node_id)  # 扣资源
        except ScheduleError:                        # ← 资源竞态:扣不动了
            return False                             #   让别的节点/下一轮再试
        slot.deliver(self._task_message(record))     # 唤醒它的长轮询
        return True
    if not self._pool.can_spawn(node_id, node.total.cpu):
        return False                                 # 池子满了 → 等
    ...
    slot = self._pool.spawn(node_id, self._worker_config(worker_id, node_id))
    slot.deliver(...)                                # 新 worker 带着任务出生
    return True
```

注意最后一行：**新 worker 是「带着任务出生」的**（任务先放进 slot，
worker 一连上来就取到），这样避免了「进程起来了但任务是空的」这种竞态。

### 对象就绪：一个函数负责唤醒所有相关方

```python
def _mark_object_ready(self, object_id, node_id):
    self._object_states[object_id] = {"state": "READY", "node_id": node_id}
    self._cond.notify_all()                              # ① 唤醒 ray.get 的等待者
    self.gcs.add_object_locations([object_id], node_id)  # ② 更新对象目录
    newly_ready = self._scheduler.mark_object_ready(oid) # ③ 唤醒依赖它的任务
    self._reconstructing.discard(object_id)              # ④ 解除 lineage 重建守卫
    self._schedule_needed.set()                          # ⑤ 让调度循环转起来
```

**对象就绪是系统的中心事件** —— 一处的状态变化会牵动等待者、目录、调度、
以及 lineage 重建的守卫。

第 ④ 步 `_reconstructing.discard(object_id)` 看着不起眼，却是**整个容错机制里
最难的一处**：在对象被重建出来之前，这个集合防止同一个对象被**重复重放**。
实测中一次丢失最多能触发 **4 倍**的重放 —— 因为「对象丢失」会被多个消费者
分别发现，每个都发起一次重建。守卫必须等到对象**真正就绪**才能解除，
早一步解除就等于把重建风暴放出来（代码里为此写了 6 行注释，见 `raylet.py`）。

### 引用计数：跨进程的内存共识

**这是 6.1 提出的四个必答题之一**（"为什么要有 owner 和引用计数？"），
也是整个系统里最容易做错的一处 —— 因为内存要在**多个进程之间**达成共识：
worker A 手里的 ref 已经丢了，但 worker B 还攥着一个，对象就**不能**回收。

mini-ray 的做法与 Ray 同构：**每个进程只上报增量（delta），由 raylet 汇总**。

```python
# core_worker.py —— 每个进程一个
class ReferenceCounter:
    """本进程持有的对象引用计数(delta 批量上报)。"""

    def add(self, object_id_hex, count=1):
        with self._lock:
            self._deltas[object_id_hex] = self._deltas.get(object_id_hex, 0) + count

    def remove(self, object_id_hex, count=1):
        self.add(object_id_hex, -count)          # 减法就是加负数

    def flush(self):
        with self._lock:
            if not self._deltas:
                return                            # 没有变化就不发 RPC
            deltas, self._deltas = self._deltas, {}
        self._flush(deltas)                       # 交给 CoreWorker 发出去
```

关键在**什么时候 flush**：一个后台线程每 **0.05 秒**
（`_REF_FLUSH_INTERVAL = 0.05`）把攒下的 delta 打包发一次。

**为什么不每次 `ray.get` 都发一次 RPC？** 因为那会让引用计数成为性能瓶颈 ——
一个循环里 `ray.get` 十万次就是十万次 RPC。批量上报把这个数字压到
「每 0.05 秒一次」，代价是**回收有最多 50ms 的延迟**。

> 这正好解释了第 3 章 / 第 7 章那句话：对象在引用归零后**不是瞬时回收**的，
> 而是「异步、批量、最多延迟一个 flush 周期」。写教程时容易写成"立即回收"，
> 那是不准确的。

raylet 侧按 **owner（哪个 worker）+ 数量**两层记账：

```python
# raylet.py
def update_ref_counts(self, worker_id, deltas):
    with self._lock:
        for object_id, delta in deltas.items():
            owners = self._ref_counts.setdefault(object_id, {})
            count = owners.get(worker_id, 0) + int(delta)
            if count > 0:
                owners[worker_id] = count
            else:
                owners.pop(worker_id, None)       # 归零就从账本上摘掉
        self._push_ref_counts(deltas.keys())      # 再把总数推给对象存储

def _push_ref_counts(self, object_ids):
    """把「集群范围内还有多少引用」压给对象存储(它据此决定能不能回收)。"""
    for object_id in object_ids:
        total = sum(self._ref_counts.get(object_id, {}).values())
        raw = bytes.fromhex(object_id)
        for store in self._stores.values():
            if store.contains(raw):
                store.set_ref_count(raw, total)
```

**为什么要按 owner 分开记，而不是只记一个总数？** 因为进程会死。
如果只记总数，一个 worker 崩溃时它欠下的引用就永远还不上了 ——
按 owner 记账才能在该 worker 死亡时，**把属于它的那一份一次性抹掉**。
这是「引用计数 + 崩溃」这一对矛盾的标准解法（Ray 里对应
`reference_count.cc` 的 borrower 记账）。

### 任务状态机：卡住时该看哪个状态

调试 Ray 时最常问的一句话是"我的任务为什么不跑"。答案在状态机里。

**mini-ray 的 `TaskState` 是 6 个值**（比真实 Ray 少，这是刻意的简化）：

```
        submit
          │
          ▼
      ┌────────┐  依赖就绪    ┌────────┐  分到节点/worker  ┌─────────┐
      │PENDING │─────────────▶│ READY  │─────────────────▶│ RUNNING │
      └────────┘              └────────┘                  └────┬────┘
           ▲                                                     │
           │              ┌──────────────────────────────────────┤
           │              ▼                    ▼                 ▼
           │       ┌────────────┐      ┌────────────┐    ┌────────────┐
           │       │  FINISHED  │      │   FAILED   │    │ CANCELLED  │
           │       └────────────┘      └─────┬──────┘    └────────────┘
           │                               │ 还有重试额度
           └───────────────────────────────┘
```

`PENDING` 同时承担了 Ray 里 `PENDING` + `PENDING_ARGS_FETCH` 两种含义，
`READY` 则合并了「等资源」与「等派发」。

**真实 Ray 的状态更细**，排查时按这张表对号入座
（Ray 侧的状态常量是 `ray/_private/custom_types.py` 里的 `TASK_STATUS`，
共 **14** 个值 —— 本表只列了排查时最常打交道的那些）：

| Ray 状态 | mini-ray 对应 | 含义 | 先查什么 |
|---|---|---|---|
| `PENDING` | `PENDING` | 依赖没满足 | 上游任务状态；`missing_deps` |
| `PENDING_ARGS_FETCH` | `PENDING` | 在等参数对象拉过来 | 对象在不在别的节点、有没有丢 |
| `RUNNING`（等资源） | `READY` | 依赖齐了但**分不到资源** | `ray status` / `state.list_nodes()` 的 `available`；是不是被 actor / 放置组占满 |
| `RUNNING`（等派发） | `READY` | 派了但 worker 没接 | worker 是不是卡死；池子上限 |
| `RUNNING_IN_RAY_GET` | 无 | 任务在等**它自己提交的子任务** | 子任务状态；典型的嵌套 `ray.get` 死锁 |
| `RUNNING_IN_RAY_WAIT` | 无 | 任务阻塞在 `ray.wait` | 同上；常出现在自制的背压循环里 |
| `FINISHED` / `FAILED` | 同名 | 完成 / 失败 | `FAILED` 还有额度就会回到 `PENDING` 重试 |

> **`RUNNING_IN_RAY_GET` 是"任务明明在跑却不出结果"的第一线索** ——
> 前面的状态都说明"还没轮到它跑"，只有这个状态说明"它在跑，但被自己卡住了"。
> mini-ray 没有这个状态，所以遇到嵌套 `ray.get` 死锁时只能看 timeline。

> 这一节的存在本身就是个教训：**教程写了 6.1 提出"为什么要有引用计数"，
> 却在正文里从没回答过**。机制类教程最容易犯的错，就是把"重要性"讲透了，
> 却把"机制本身"跳过去了。

---

## 6.9 Actor 运行时：线程与协程

actor 是「一个常驻进程 + 一个邮箱」。mini-ray 的 actor 运行时按并发模型分两条路：

| 形态 | 触发条件 | 实现 | 默认并发度 |
|---|---|---|---|
| 同步 actor | 方法都是普通函数 | 1 个线程轮询邮箱 | 1 |
| 线程化 actor | 同步方法 + `max_concurrency=N` | N 个线程，各自轮询 | N |
| 并发组 | `concurrency_groups={"io": 4, "compute": 2}` | 每组成员一个线程 | 各组独立 |
| asyncio actor | 有 `async def` 方法 | 事件循环 + 单轮询协程 | **1000** |

实现上的一个巧妙之处：**线程数就是并发度**。
每个执行槽位是一个线程，各自长轮询 `actor_poll(group)`；
raylet 侧按组维护「在飞数量」，超过并发度就不发任务。
于是「默认并发度 1」和「并发度 4」用的是同一段代码，只是线程数不同。

```python
def run(self):   # 一个执行槽位
    core = CoreWorker(...)                       # 每个线程自己的连接
    while True:
        reply = core.actor_poll(self.actor_id, worker_id, self.group)
        if reply["kind"] == "shutdown": return
        if reply["kind"] != "actor_task": continue
        task = reply["task"]
        method, args, kwargs = self.prepare(task, core)   # 取依赖 + 反序列化
        result = method(*args, **kwargs)
        self.finish(core, task, result)                    # 结果写回对象存储
```

async actor 则用事件循环：一个轮询协程（把阻塞的 `actor_poll` 丢到线程池里跑），
每个任务 `asyncio.ensure_future`，并用计数器限制在飞数量。

**顺带实现了 `await ObjectRef`**（async actor 里 `await ref` 直接拿值）：

```python
def __await__(self):
    return _await_ref(self).__await__()      # 内部用 run_in_executor 包装阻塞的 ray.get
```

---

## 6.10 容错：三种故障、三种机制

| 故障 | 机制 | mini-ray 的实现 |
|---|---|---|
| 函数抛异常 | 重试（默认 3 次） | `task_failed` → 重新入 READY 队列，结果对象 ID 不变 |
| worker 崩溃 | 换 worker 重跑 | `WorkerPool.reap()` 发现进程死了 → `_on_worker_death` → 按崩溃处理 |
| 对象丢失 | **lineage 重建** | `_reconstruct_object`：递归重建依赖，再重放产出它的任务 |

lineage 重建是 Ray 最有意思的设计，实现也就几十行：

```python
def _reconstruct_object(self, object_id):
    if object_id in self._reconstructing: return True      # 防环
    entry = self._lineage[object_id]                       # 提交时记的「怎么算出来」
    self._object_states[object_id] = {"state": "RECONSTRUCTING"}

    # ① 先重建依赖(深度优先)
    for dep in _extract_deps(entry["task_spec"]["args"]):
        if self._object_states[dep.hex()].get("state") != "READY":
            if not self._reconstruct_object(dep.hex()):
                self._mark_lost(object_id, "依赖无法重建")
                return False

    # ② 重新提交生产它的任务(结果对象 ID 保持不变!)
    original_task = self._scheduler.tasks.get(entry["task_id"])
    result_ids = (list(original_task.result_ids) if original_task
                  else [bytes.fromhex(object_id)])     # 退化路径:至少保住自己
    replay_spec = dict(entry["task_spec"])
    replay_spec["max_retries"] = 0                     # ← 重放不再重试,避免风暴
    record = TaskRecord(task_id=TaskID.from_random(),
                        result_ids=result_ids, ...)
    self._scheduler.add_task(record, ready_objects=self._ready_deps_of(record))
    return True
```

**关键点**：重建出来的对象沿用**原来的 ObjectRef ID**，所以等待者完全无感 ——
不需要通知任何人「对象换了」，`ray.get` 只是继续等。

两个容易被忽略的设计决策：

* **`result_ids` 从原任务的记录里取**，而不是凭空构造。如果原任务记录已经不在
  （例如上游也被清理了），退化路径是**只保住当前这个对象 ID**，其余依赖走各自的重建；
* **重放的任务 `max_retries` 被强制置 0**。否则一个「算一次崩一次」的任务会在
  重建时反复重试，把一次对象丢失放大成一场重试风暴 —— 这是实测踩出来的。

故障注入（用于测试与演示）：

```python
from miniray._private import fault_injection
fault_injection.lose_objects()      # 模拟节点故障:丢掉所有对象
ray.get(ref)                        # 依然返回正确结果(重新算了一遍)
state.summarize_objects()["num_reconstructions"]   # > 0
```

---

## 6.11 踩过的 12 个坑（这一节比代码更值钱）

按「发现难度」排序，每个都给出症状、根因、修法。

### 坑 1：`builtins` 没有 `__file__` → 无限递归

**症状**：序列化一个普通类时 `RecursionError('Stack overflow (used 2912 kB)')`。
**根因**：判断「模块是否可导入」时我只看了 `__file__`。`builtins` 没有 `__file__`，
于是 `object`、`int` 这些内置类型被判定为「需按值序列化」，
而序列化 `object.__new__` 时又碰到 `object` 自身 → 无限递归。
**修法**：判定顺序改成 `__file__` → `__spec__` → `find_spec()`。

### 坑 2：pickle 的 `save_reduce` 先序列化参数、后 memoize

**症状**：递归函数序列化栈溢出（`def fib(n): return fib(n-1)+fib(n-2)`）。
**根因**：见 6.4 的详细分析 —— memo 在参数之后才建立，自引用必然递归。
**修法**：延迟引用 + 载入后回填。

### 坑 3：pickle 会把「占位符」记成对象的规范表示

**症状**：修完坑 2 之后不再溢出，但 `ray.get` 拿到的"函数"是个代理对象。
**根因**：返回延迟引用时，pickle 把那个占位符 memoize 成了 `f` 的规范表示，
整条流里对 `f` 的引用都指向占位符（包括最外层那个值）。
**修法**：占位符持有解析目标（`_target`），并在载入结束时把顶层容器里的
占位符也换成真对象。

### 坑 4：「按值兜底」会把序列化器自己也按值序列化

**症状**：给 `dumps` 加了一个「失败就全部按值重试」的兜底后，简单类也爆炸。
**根因**：兜底模式下 `_rebuild_function`（重建函数用的那个函数）也被按值序列化，
而它的 globals 里又有序列化器的其它函数 → 自我指向的巨型图。
**修法**：删掉兜底，改用**精确判定**（坑 5）。

### 坑 5：被装饰器替换掉的名字

**症状**：`@ray.remote def f(x)` 之后导出函数失败。
**根因**：模块属性 `f` 已经是 `RemoteFunction`，按引用序列化会拿到"不是它"的东西。
**修法**：判定标准从「模块可导入吗」改成「**模块里那个名字还指向它吗**」。

### 坑 6：依赖已经就绪的任务永远卡在 PENDING

**症状**：`f.remote(existing_ref)`（把已有 ref 当参数传）永久挂起。
**根因**：`add_task` 只按「依赖个数」计数，已经就绪的依赖不会再触发一次就绪事件。
**修法**：`add_task(record, ready_objects=…)`，提交时把已就绪的依赖直接算作满足。

### 坑 7：actor 任务的结果无处落地

**症状**：`ray.get(actor.method.remote())` 永久阻塞。
**根因**：actor 方法调用不进调度器的 `tasks` 表（它由 actor 自己排队），
`task_done` 找不到对应的 TaskRecord 就直接返回了。
**修法**：`_actor_task_context()` 从 actor 运行时的 `running` 表里取结果对象。

### 坑 8：参数名冲突导致「服务端等待」变「客户端超时」

**症状**：`ray.wait(refs, timeout=1.0)` 报 `MiniRayletDiedError`。
**根因**：RPC 客户端的 `call(method, *, timeout=None, **kwargs)` 里，
`timeout` 是**客户端超时**；而 RPC 方法（`wait_for_objects`）自己也有一个
叫 `timeout` 的参数。传 `timeout=1.0` 时被客户端截走了 → 客户端 0.5 秒就放弃 →
服务端返回的"等待结果"变成连接错误。
**修法**：客户端超时改名 `_call_timeout`；同时调整 `except` 顺序
（`socket.timeout` 在 Python 3.10+ 就是 `TimeoutError`，是 `OSError` 的子类，
必须先捕获）。

### 坑 9：模块名与函数名同名，子模块 import 覆盖了函数属性

**症状**：`ray.timeline(path)` 报 `'module' object is not callable`。
**根因**：包里有 `timeline.py` 模块，函数也叫 `timeline`。第一次 `from .timeline
import timeline` 之后，Python 会把**子模块**设成包的属性，覆盖掉函数。
**修法**：模块改名 `_timeline.py`。

### 坑 10：`__builtins__` 在模块里是 dict，不是 module

**症状**：远程异常的原始类型全变成 `Exception`，`except ValueError` 抓不到。
**根因**：`getattr(__builtins__, "ValueError", None)` —— 在 `__main__` 里
`__builtins__` 是模块，在**被 import 的模块里是 dict** → 拿到 `None` → 退化成 Exception。
**修法**：`import builtins` 之后用 `getattr(builtins, name)`。

### 坑 11：worker 配置文件名用 worker_id 前缀 → actor 重启时互删文件

**症状**：actor 重启失败，日志里 `FileNotFoundError: worker-xxx.config`。
**根因**：文件名取 `worker_id[:16]`，而 actor 重启后的 worker_id
`actor-<hash>-0` / `actor-<hash>-1` 前 16 个字符相同 → 新旧 worker 共用一个文件，
旧 worker 被回收时删掉了新 worker 还没读的配置。
**修法**：文件名用 worker_id 的 **sha1**。

### 坑 12：任务结束后查不到它跑在哪个节点

**症状**：`state.list_tasks()` 里已完成任务的 `node_id` 是 `None`。
**根因**：`_release()` 在归还资源时顺手把 `record.node_id` 清空了。
**修法**：归还资源**不要**清 node_id（下一次 assign 会覆盖它），
可观测性要保留「最后跑在哪」。

> **这些坑的共同点**：没有一个来自"分布式算法难"，全部来自**边界约定**
> —— pickle 的写入顺序、模块属性的覆盖、参数名的碰撞、元数据的生命周期。
> 这大概就是分布式系统工程的真实样子。

---

## 6.12 验证：192 个测试说明什么

```bash
cd mini-ray && python -m pytest tests/ -q
# 192 passed
```

| 测试文件 | 用例数 | 覆盖的关键不变量 |
|---|---|---|
| `test_serialization.py` | 7 | 按值序列化（`__main__`/闭包/lambda/递归/类）、跨进程反序列化、ref 内联、自定义序列化器 |
| `test_object_store.py` | 15 | 分桶分配与复用、跨进程零拷贝（子进程写穿）、只读语义、引用计数回收、溢出与恢复、pin 保护、容量报错 |
| `test_core.py` | 30 | 任务/依赖/嵌套取值/多返回值/kwargs、异常类型与重试次数、`ray.wait` 背压与超时、取消（含 force）、`.bind()`、runtime_env、worker 复用 |
| `test_actors.py` | 19 | 状态持久、顺序执行、并发度、并发组隔离、async actor、重启、命名 actor、资源持有、方法多返回值、`method.bind()` |
| `test_scheduling.py` | 11 | 多节点资源视图、资源释放、跨节点分散、节点亲和、放置组四策略、资源预留与释放、PG 等待后自动分配 |
| `test_fault_tolerance.py` | 6 | worker 崩溃 → `WorkerCrashedError`、崩溃后重试成功、lineage 重建、依赖链递归重建、`ObjectLostError` |
| `test_observability.py` | 9 | State API 形状、聚合视图、资源视图、timeline JSON/HTML、对象统计、worker 池、actor 邮箱 |
| `test_local_mode.py` | 6 | 同进程执行、依赖、异常、actor、wait |
| `test_util.py` | 15 | ActorPool（有序/无序/背压）、GPU 分配、自定义资源、溢出、生命周期、命名空间 |
| `test_queue_metrics.py` | 30 | 队列的 FIFO/阻塞/超时/`Full`/`Empty`/多消费者不重不漏/背压上界/`shutdown`；指标的桶、分位数插值、标签校验、重复注册报错、线程安全、任务与 actor 内打点 |
| `test_regressions.py` | 26 | 第六、七轮加的回归用例：盯住「前 148 个测试全绿时依然活着」的 10 个真实缺陷，以及第七轮抓到的 **9 个「上一轮只修了一半」**的缺陷（详见 §6.12.1） |

三个「必须是真跨进程才算数」的测试：

1. **跨进程零拷贝**：子进程写数组，父进程读到变化（`test_object_store.py`）；
2. **`__main__` 函数跨进程执行**：脚本里定义、另一个解释器里执行
   （`tests/_ser_roundtrip.py`，用 spawn 起新解释器）；
3. **lineage 重建**：丢掉对象后 `ray.get` 仍返回正确值（`test_fault_tolerance.py`）。

---

### 6.12.1 第七轮的九个缺陷：上一轮"只修了一半"

第六轮的代码审计在 148 个测试全绿的情况下抓到 10 个缺陷（§6.11）。
第七轮又抓到 **9 个** —— 而它们的形状，比"又发现了新 bug"更值得记住：

> **它们全都是第六轮修过的那类缺陷，在另一条实现路径上的复制。**

mini-ray 里同一种能力往往有**两条实现路径**："跑在分布式"和"跑在 `local_mode`"、
"创建时就有资源"和"排队等到资源"、"普通任务"和"流式生成器"。
第六轮修好了其中的一条，**另一条一模一样地坏着**。

| 第六轮修好的那条路径 | 第七轮抓到的"另一半" | 用户看到什么 |
|---|---|---|
| 分布式生成器失败 → `done` 永远 False | **`local_mode` 下生成器失败 → 永久挂起**，且异常**覆盖了消费者已经取走的 chunk** | 每 60 秒空转一次，永不结束 |
| 普通任务的取消能到 worker | **`ray.cancel(生成器 chunk)` 静默什么都不做** | 取消"成功"了，生成器照样把 30 个 chunk 产完 |
| 创建时就有资源的 GPU actor 拿得到 `gpu_ids` | **排队等到资源的 GPU actor 拿到空 `gpu_ids`** | actor 里的 torch 看得见机器上**全部**的卡 |
| 分布式路径的任务只执行一次 | **`local_mode` 下每个任务都执行两次**（driver 一次 + 子进程一次） | 副作用翻倍，而 `ray.get` 的返回值**完全正常** |

其余五个：`local_mode` 下失败任务被重试进子进程（函数被调 5 次）、
actor 的 `__init__` 收到 ObjectRef 时**完全创建不出来**、
调度失败被判成"可重试"导致 READY 队列每 0.2 秒翻一倍（`ray.get` 永久等）、
`ray.get_actor(name)` 把 `num_returns` 写死成 1（**静默丢返回值**）、
生成器 chunk 没登记 `_object_tasks` 导致取消失效。

**这一轮真正的收获，是下面这条可以搬到你自己的代码上的规矩：**

> **修一个 bug 的时候，先问一句"这个能力有几条实现路径？"**
> 然后**每一条都要修，每一条都要有测试**。
>
> 第六轮修完 10 个缺陷后加了 17 个回归测试，**全部只覆盖了分布式那一条路径** ——
> 于是 `local_mode` 那一半原封不动地活到了第七轮。
> 这不是"测试不够多"（165 个已经不少了），而是**测试的分布不对**。

顺带一提：这也解释了为什么"测试全绿"这个信号会**系统性地**骗人 ——
它不是随机漏掉一些情况，而是**沿着实现路径成片地漏**。
所以下一轮要问的不再是"哪些路径没测"，而是更精确的一句：
**"哪些路径的孪生路径没测？"**

---

## 6.13 与真实 Ray 的差异（诚实清单）

| 项 | 差异 |
|---|---|
| 控制面 | 单进程（Ray 是 GCS/raylet 独立进程），driver 退出 = 集群结束 |
| 多节点 | `num_nodes` 模拟，跨节点传输是一次进程内拷贝 |
| 调度 | 单策略（本地性 → 最空闲）；Ray 有 hybrid/spread/label/topology 与 lease 机制 |
| 对象回收 | **只在内存压力下**回收 refcount=0（Ray 是引用归零后**批量上报**释放，通常几毫秒到 1 秒内） |
| 任务重试 | **应用异常也重试**（默认 3 次）—— Ray **默认不重试应用异常**，要 `retry_exceptions=True`。mini-ray 没有这个选项，所以这里**方向是反的**，不要拿两边对照 |
| 放置组选项 | `@miniray.remote(placement_group=…)` 的 `placement_group_bundle_index` 有实现，但第 2 章的选项表没列它 |
| 序列化 | 无 cloudpickle 的 module/生成器/sliced 对象等特殊处理；循环引用用「延迟引用+回填」而非 state 通道 |
| 溢出 | 同步溢出（Ray 有独立 IO worker 池 + mmap 读回） |
| Actor | 无 detached 跨进程存活；无 actor 迁移；并发组是线程池模型 |
| 缺失 | Dashboard、autoscaler、Jobs API、Ray Data/Train/Tune/Serve/RLlib、C++/Java 绑定、鉴权、GPU 显存管理、NCCL/RDMA |

**看待这份清单的正确方式**：每一条差异背后都有一个"为什么"。
如果你能把每条差异讲清楚（因为压进一个进程、因为没有集群发现、
因为要零依赖……），说明你已经理解了 Ray 的设计约束。

---

## 6.14 练习

1. **入门**：把 `examples/05_scheduling.py` 的 `num_nodes` 改成 4，
   观察放置组 `PACK` 与 `STRICT_PACK` 的区别。为什么 `PACK` 有时也会跨节点？
2. **改调度器**：在 `scheduler.pick_node` 里实现真正的 `SPREAD` 策略
   （总是选利用率最低的节点），并写一个测试证明它和默认策略的行为不同。
3. **加一个 API**：给 mini-ray 实现 `ray.util.queue.Queue`（Ray 里基于 actor 的
   分布式队列）。提示：它本质上就是一个 actor 包着 `collections.deque`，
   但要注意「空队列时 `get` 不能忙等」—— 用 actor 方法 + `ray.wait` 实现超时。
4. **优化对象传输**：让跨节点拉取改为「拉进本地对象存储并缓存」，
   然后测同一个对象被拉两次时第二次不再走网络。
5. **最难**：给生成器任务加上「消费端背压」——当消费速度慢于生产速度时，
   让生成器暂停（提示：raylet 在 chunk 的引用计数上做文章）。

---

## 6.15 本章小结

* mini-ray 用约 1.1 万行、零依赖实现了 Ray Core 的全部核心机制，
  **192 个测试**验证：任务/依赖/对象存储/Actor/调度/容错/可观测性/队列/指标。
  其中最后 17 个（`test_regressions.py`）是第六轮加的 —— 它们盯的是
  **前 148 个测试全绿时依然活着的 10 个真实缺陷**。
* 最难的部分不是调度，而是**序列化**：函数按值传、
  ObjectRef 内联、循环引用的延迟引用+回填、模块对象按名 import。
* 对象存储的关键是三件事：**共享内存零拷贝、只读语义、pin 与引用计数**
  —— 它们必须一起设计，缺一个就会出错。
* worker 用 `subprocess` 而不是 `multiprocessing`，是为了让用户脚本**不需要**
  `if __name__ == "__main__"` 保护（真实 Ray 也是这么做的）。
* 容错三层：重试、worker 崩溃恢复、lineage 重建；重建**沿用原对象 ID**
  所以调用方无感。
* 12 个真实的坑全部来自**边界约定**（pickle 写入顺序、模块属性覆盖、
  参数名冲突、元数据生命周期），而不是"分布式算法难"。

后面 5 章（7–11）会把本章提到的每个机制与真实 Ray 的实现做逐项对照：
对象存储与内存管理、调度与资源、Actor 模型、容错、可观测性与调试。
