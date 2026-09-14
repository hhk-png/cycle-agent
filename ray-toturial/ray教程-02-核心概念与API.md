仓库地址：https://github.com/hhk-png/cycle-agent

# 第 2 章：核心概念与 API

> 本章目标：把 Ray Core 的**全部**概念一次讲清 —— 任务、对象、Actor、ObjectRef、
> 依赖、资源声明、错误模型。读完你应该能凭记忆写出 90% 的 Ray 代码，
> 并说清每一行在系统里触发了什么。

---

## 2.1 三个抽象，一个模型

Ray Core 只有三个抽象。整个分布式系统就建立在这三个词上：

| 抽象 | 是什么 | 代码形态 | 生命周期 |
|---|---|---|---|
| **Task**（任务） | 一次无状态的远程函数调用 | `f.remote(args)` | 调用开始 → 返回结果对象后结束 |
| **Actor**（角色） | 一个常驻的、有状态的对象 | `A.remote(args)` 创建，`a.m.remote()` 调用方法 | 创建 → 显式杀死/崩溃 |
| **Object**（对象） | 不可变的数据 | `ray.put(v)`、任务的返回值 | 引用计数归零后**由批量上报触发**回收（不是瞬时，见第 3 章 §3.6 与第 7 章 §7.5） |

再加一个贯穿三者的东西：

| **ObjectRef** | 「未来值的句柄」 | `f.remote()` 的返回值 | 引用它的进程都退出后消失 |

把它们放在一起看，Ray 的编程模型就是：

```python
@ray.remote                       # ① 声明:把函数变成「可远程调用」
def f(x):
    return x * 2

ref = f.remote(21)                # ② 提交:立刻返回 ObjectRef(异步!)
value = ray.get(ref)              # ③ 取值:阻塞,得到 42
```

**这三个步骤的时间语义完全不同**：`f.remote()` 是微秒级的本地操作（只是把任务
丢进队列），真正的执行发生在别处，`ray.get` 才是同步点。理解这一点，
后面所有的性能与并发问题都能自己推出来。

---

## 2.2 `@ray.remote` 到底做了什么

它没有魔法，返回的是一个**包装对象**：

```python
import ray

@ray.remote
def square(x):
    return x * x

print(type(square))          # <class 'ray.remote_function.RemoteFunction'>
square(3)                    # TypeError: 远程函数不能直接调用
square.remote(3)             # ObjectRef(...)

# 原函数还在，但**挂的是私有名**（Ray 是 ._function，mini-ray 同名）：
print(square._function)      # <function square at 0x...>
```

> ⚠️ **不要依赖 `square.__wrapped__`**：Ray 的 `RemoteFunction` **没有**
> 这个标准装饰器属性（那是 `functools.wraps` 留下的约定，Ray 没遵守）。
> 网上有些示例写它，在真机上会 `AttributeError`。
> 需要拿原函数时用私有属性 `_function` —— 但既然是私有的，
> 跨版本就不保证稳定，正规做法是**在装饰之前**先留一个引用：
>
> ```python
> def _square(x): return x * x     # 原函数先存下来
> square = ray.remote(_square)     # 装饰的是它
> ```

`RemoteFunction` 上可以挂**选项**，选项决定调度行为：

```python
@ray.remote(num_cpus=2, num_gpus=1, memory=4 * 1024**3, resources={"TPU": 1}, max_retries=3)
def train_shard(path):
    ...

# 运行期改选项（返回副本，不改原对象）
ref = train_shard.options(num_cpus=8).remote("s3://bucket/shard-0")
```

`.options()` 支持的常用键（完整清单见附录 A）：

| 键 | 含义 | 默认值 |
|---|---|---|
| `num_cpus` / `num_gpus` | 资源需求（可以是小数，如 `0.5`） | task 1 CPU / 0 GPU |
| `resources` | 自定义资源，如 `{"TPU": 4}` | `{}` |
| `memory` | 内存需求，**用于内存感知调度**（不写就是「不看内存」） | 不设（= 不参与） |
| `max_retries` | **worker 崩溃**的重试次数（**不含应用异常**，见 §2.8） | **3**（⚠️ 生成器任务的重试语义特殊，见 §2.8 与第 5 章 §5.3） |
| `retry_exceptions` | **应用异常**是否重试；`True` / 异常类型列表（**只有这两种**，见 §2.8） | `False` |
| `max_calls` | 这个 worker 执行多少次任务后**退休**（换新进程） | **CPU 任务不限 / GPU 任务 = 1**（见下） |
| `num_returns` | 返回值个数；`"streaming"` 表示生成器任务（**旧名 `"dynamic"` 已弃用**，见第 5 章 §5.3） | 1（生成器任务**自动**为 `"streaming"`，不必显式写） |
| `runtime_env` | 依赖注入（环境变量、pip、working_dir…） | 继承 init 时的配置 |
| `scheduling_strategy` | 放置约束（节点亲和、放置组） | `None`（由 Ray 决定，通常落到 `"DEFAULT"` / hybrid） |
| `name` | 便于在 State API / Dashboard 里辨认 | 函数名 |

### `max_calls`：治「worker 内存缓慢上涨」的官方药方

⚠️ **先记住默认值不是「不限」**：Ray 的默认是
「**CPU 任务不限，GPU 任务为 1**」（源码 `remote_function.py`：
`num_gpus > 0 and max_calls is None → max_calls = 1`），
目的是**强制 GPU 任务跑完就释放显存**。
所以「为什么我的 GPU 任务每次都重新加载模型」的答案就在这里 ——
想让 GPU 任务复用 worker，**必须显式写 `max_calls=0`**。

这是最被低估的一个参数。Ray 的 worker 进程是**复用**的，如果你的任务里
有 C 扩展、全局缓存、或者第三方库的泄漏，内存会随执行次数缓慢上涨，
最后被内存监控杀掉（第 07 章 §7.7）。

```python
@ray.remote(max_calls=100)      # 每跑满 100 次,这个 worker 就退出、换一个新的
def leaky_inference(batch):
    return model.predict(batch)  # model 是模块级全局,常驻内存
```

代价是**每次换进程都要重新 import 模块、重新加载模型** —— 所以 `max_calls`
是「拿启动开销换内存稳定」的权衡，值要按「多久涨到危险线」来估，
不要凭感觉填个小数字。

> ⚠️ **`max_calls` 在 mini-ray 里没有实现**（附录 A 的对照列标的就是 ❌）。
> mini-ray 只有**空闲退休**：`init(worker_idle_timeout_ms=...)` 控制的那个超时。
> 真实 Ray 除此之外还会主动杀内存超标的 worker，mini-ray 也不做。
> 见附录 B §B.5。

还有两个容易忽略但很有用的用法：

```python
# ① .bind()：把**全部**参数绑成一个 DAG 节点（Ray 2.0 的 DAG API 起就有）
@ray.remote
def add(a, b):
    return a + b

node = add.bind(10, 5)         # 返回 FunctionNode，不是「半成品函数」
ray.get(node.execute())        # 15
# ⚠️ .bind() 是 **DAG 节点构造，不是偏函数应用**（没有 functools.partial 的语义）：
#    它把传进去的 args 原样存进节点，而 .execute(*args) **忽略自己收到的参数**，
#    只做 add.remote(*绑定时存下的args)。所以
#        add.bind(10).execute(5)
#    不是「补上第二个参数」，而是提交 add.remote(10)，直接抛
#        TypeError: add() missing 1 required positional argument: 'b'
#    必须像上面那样**一次性绑全**。
#    返回的 FunctionNode **也不能 .remote()** —— 写了会抛
#    "AttributeError: .remote() cannot be used on <class 'FunctionNode'>"
# ⚠️ 而 .execute() 这条路**本身也已弃用**（PR #63716，GCS KV 无界增长）：
#    上面两行适合理解语义，**生产请用第 26 章的编译图**
#    （graph.experimental_compile() 之后 execute）。

# ⚠️ **mini-ray 在 .bind() 上与真实 Ray 分叉**：mini-ray 的 .bind(10) 做的是
#    偏函数应用（返回一个还能 .remote(5) 的 RemoteFunction），并且**没有
#    .execute()**。两边别互相照抄。见第 26 章。

# ② ray.remote 直接调用（不用装饰器语法）
square = ray.remote(lambda x: x * x)
```

> **和 mini-ray 的对照**：mini-ray 实现了 `RemoteFunction`、`.options()`、
> `.bind()`，以及这些键：`num_cpus` / `num_gpus` / `memory` / `resources` /
> `max_retries` / `num_returns` / `runtime_env` / `scheduling_strategy` / `name`
> （源码见 `mini-ray/miniray/remote.py` 的 `TASK_OPTION_KEYS`）。
> **没有** `max_calls` 和 `retry_exceptions` —— 这两个键在 mini-ray 上会被
> 当成未知选项**明确报错**，而不是静默忽略（静默忽略是更坏的行为：
> 你以为加了重试，其实没有）。
> 另一个差别在于「函数怎么送到 worker」：真实 Ray 用 cloudpickle，
> mini-ray 用自带的 cloudpickle-lite，判定规则与边界条件见第 06 章。

---

## 2.3 ObjectRef：未来值的句柄

`f.remote()` 返回的不是值，而是一个**句柄**。这是 Ray 异步性的来源。

```python
refs = [square.remote(i) for i in range(4)]   # 4 个任务几乎同时提交
# 此刻:任务可能一个都没跑完,但你已经拿到了 4 个句柄
print(refs[0])                                 # ObjectRef(45b3a2f1...)
values = ray.get(refs)                         # 一次等齐 4 个结果
```

ObjectRef 的四条关键性质：

**(1) 它可以当参数传给别的任务** —— 这是 Ray 表达依赖的方式：

```python
@ray.remote
def add(a, b):
    return a + b

x = square.remote(3)          # 依赖:square(3)
y = square.remote(4)
total = add.remote(x, y)      # add 会等 x、y 就绪后才执行
ray.get(total)                # 25
```

**注意这里没有「传值」**：`add` 收到的是两个 ObjectRef 的**占位符**，
worker 在执行前才把它们替换成真实的值（Ray 文档称之为 *inlined object refs*）。
好处是：如果 `x` 和 `y` 分布在不同节点，数据不需要先回到 driver 再发出去。

**(2) 它可以嵌套在任意结构里**：

```python
result = ray.get({"a": [x, 3], "b": (y, {"deep": x})})   # 按原结构返回
```

**(3) 它不可变**，拿到的 numpy 数组是**只读**的：

```python
array = ray.get(ray.put(np.arange(10)))
array[0] = 1        # ValueError: assignment destination is read-only
```

这不是限制，而是零拷贝的前提：如果允许改，所有共享这块内存的读者都会受影响
（详见第 07 章）。

**(4) 它参与引用计数** —— 句柄活着，对象就不会被回收；句柄被 GC，对象才可能被释放。
所以「内存下不去」的第一嫌疑永远是：**某个地方还攥着 ObjectRef**。

### `ray.wait`：等「任意 N 个」

`ray.get` 是「等全部」，`ray.wait` 是「等够 N 个就走」：

```python
ready, remaining = ray.wait(refs, num_returns=2, timeout=5.0)
```

签名（Ray 2.58）：

```python
ray.wait(ray_waitables, *, num_returns=1, timeout=None, fetch_local=True)
    -> (ready: List[ObjectRef], remaining: List[ObjectRef])
```

`fetch_local=True`（默认）的含义是：**「就绪」的判定标准是对象已经在本节点可用**，
而不只是「在集群里存在」。这意味着 `ray.wait` 返回的 ready 列表里，
`ray.get` 通常是**零拷贝**的；如果设成 `False`，则只要对象在集群任意位置
就算就绪，但随后 `ray.get` 可能要等一次网络拉取。

**什么时候要设 `False`**：只想知道「上游算完了没」而不急着取数据时
（比如纯做进度统计），设 `False` 能让 `ray.wait` 更早返回，不必等数据传输完。
默认值 `True` 对绝大多数「取回来就用」的场景是对的。

它是**做背压的标准工具**。官方推荐的模式（*limit-pending-tasks*）：

```python
MAX_PENDING = 100
pending = []
for item in items:
    if len(pending) >= MAX_PENDING:
        ready, pending = ray.wait(pending, num_returns=1)
        handle(ray.get(ready)[0])
    pending.append(process.remote(item))
```

> 为什么要这样写，而不是靠某个「最大队列长度」参数？因为**限流是应用语义**：
> 系统不知道你能承受多少并发。Ray 官方明确说过，靠降低并发数来限流会伤害
> 调度性能，正确做法是**改每个任务的资源需求**，让 Ray 自己算出并行度。

---

## 2.4 对象：`ray.put` / `ray.get` 的语义与代价

### 什么时候该 `ray.put`

```python
big = load_dataset()               # 假设 2GB
ref = ray.put(big)                 # 放进对象存储一次

@ray.remote
def process(chunk_ref, config):
    ...
```

三种传值方式的代价对比：

| 写法 | 序列化次数 | 跨进程拷贝 | 适用 |
|---|---|---|---|
| `f.remote(big)` 直接传 | 每次调用 1 次 | 每个任务 1 份 | 小对象 |
| `ref = ray.put(big)` 后传 ref | 1 次 | **同节点零拷贝** | 大对象、被多个任务复用 |
| 任务自己重新加载（读文件） | 0 | 0（但重复 IO/CPU） | 数据在共享存储上且加载很快 |

**经验法则**：对象大于 ~100KB 且会被用两次以上，就用 `ray.put`。

### 什么时候**不要**批量 `ray.get`

```python
# 反模式:每次 get 都是一次同步点,把并行度压成串行
for ref in refs:
    results.append(ray.get(ref))

# 正确:一次 get 一批
results = ray.get(refs)
```

`ray.get` 的签名与限制：

```python
ray.get(object_refs, *, timeout=None)   # 支持嵌套结构;timeout 超时抛 GetTimeoutError
```

---

## 2.5 依赖与 DAG：图是「写出来」的，不是「声明」的

**默认路径下**，Ray 没有显式的 DAG 声明 API，依赖图是**从数据流里推断**出来的。
（⚠️ 例外：如果你想**显式**声明一个有向无环图，Ray 有 `.bind()` 的
DAG API —— 见 §2.2 与第 26 章。两者不是替代关系：推断出来的图用于普通任务，
显式声明的图是为了让 Ray 能**编译**它。本节讲的是前者。）看这个例子：

```
       ┌──────────┐        ┌──────────┐
       │ shard 0  │        │ shard 1  │
       └────┬─────┘        └────┬─────┘
            ▼                   ▼
       ┌──────────┐        ┌──────────┐
       │  parse   │        │  parse   │     ← 两个 parse 并行
       └────┬─────┘        └────┬─────┘
            └────────┬─────────┘
                     ▼
              ┌─────────────┐
              │  aggregate  │                 ← 等两个都完成
              └─────────────┘
```

对应代码就是：

```python
raw = [read_shard.remote(i) for i in range(2)]
parsed = [parse.remote(r) for r in raw]
result = ray.get(aggregate.remote(parsed))
```

这里有三条重要推论：

1. **并行度是自动的**：只要没有数据依赖，任务就会并行；
2. **依赖失败会传播**：上游任务失败，下游任务也会失败（`ray.get` 抛出的异常里
   能看到原因链）；
3. **对象一旦就绪，消费者就会被唤醒**：调度是事件驱动的，不是轮询
   （依赖反查表的实现见第 06 章 §6.7 与 `miniray/scheduler.py`；
   调度**策略**层面见第 08 章）。

**一个常见的性能陷阱**：如果 `aggregate.remote(parsed)` 里传的是一个包含 1000 个
ref 的列表，那么 1000 个依赖对象会在 aggregate 开始时**全部拉取**到它所在的节点。
这不是 bug，但要清楚它的代价。

---

## 2.6 Actor：有状态的那一半

```python
@ray.remote
class Counter:
    def __init__(self, start=0):
        self.value = start          # 状态:活在 actor 进程的内存里

    def inc(self, k=1):
        self.value += k
        return self.value

counter = Counter.remote(10)         # 创建(异步)
print(ray.get(counter.inc.remote())) # 11
print(ray.get(counter.inc.remote(5))) # 16
```

### 必须记住的四条 Actor 语义

**① 方法调用是异步的，actor 内部按提交顺序执行**（默认并发度 1）：

```python
refs = [counter.inc.remote() for _ in range(5)]   # 5 个调用几乎同时发出
ray.get(refs)                                     # [.., .., .., .., ..] 严格递增
```

**② 资源是终身持有的**：`Counter.options(num_cpus=2).remote()` 创建后，
那 2 个 CPU 一直属于这个 actor，直到它被杀死。这是「actor 太多把集群占满」的根因。

**③ 默认不重启**：`max_restarts=0`。actor 进程挂了，在飞的方法调用全部失败，
状态丢失。要重启得显式配置（重启后 `__init__` 会重跑，**状态不会恢复**）：

```python
@ray.remote(max_restarts=3, max_task_retries=2)
class Robust:
    ...
```

**④ 句柄可以当参数传** —— 这是参数服务器、多 actor 协作的基础：

```python
@ray.remote
def worker(server, i):
    weights = ray.get(server.pull.remote())
    return ray.get(server.push.remote(compute_grad(weights, i)))

ray.get([worker.remote(server, i) for i in range(8)])
```

### 并发模型：三种 Actor

Ray 的 actor 并发模型由「方法是不是 `async def`」+「`max_concurrency`」决定：

| 形态 | 触发条件 | 默认并发度 | 方法执行方式 |
|---|---|---|---|
| **同步 actor** | 方法都是普通函数 | 1（串行） | 一个方法一个方法地执行 |
| **线程化 actor** | 同步方法 + `max_concurrency=N` | N | N 个线程并发执行 |
| **asyncio actor** | 有 `async def` 方法 | **1000** | 事件循环，可 `await` |

```python
# asyncio actor:方法里可以 await(包括 await 一个 ObjectRef!)
@ray.remote
class AsyncWorker:
    async def fetch(self, url):
        data = await http_get(url)
        return data

    async def pipeline(self, ref):
        value = await ref          # 直接 await 别的任务的结果
        return value * 2
```

**并发组**（Ray 2.x 引入，常被忽略但很实用）：不同方法用不同的并发度，
互不干扰 —— 典型场景是「IO 密集的方法要高并发，计算密集的方法要低并发」：

```python
@ray.remote(concurrency_groups={"io": 8, "compute": 2})
class Mixed:
    @ray.method(concurrency_group="io")
    async def fetch(self, url): ...

    @ray.method(concurrency_group="compute")
    def crunch(self, data): ...
```

> ⚠️ 并发度 > 1 时，**方法可能乱序执行**。有状态 actor 要自己想清楚
> 「哪些操作必须串行」。Ray 只保证：同一个并发组内、同一时刻最多 N 个在跑。
>
> 另一个坑：用了 `concurrency_groups` 之后，**没有标注 `@ray.method(concurrency_group=...)`
> 的方法会落进一个默认组**，它们的并发度由顶层的 `max_concurrency` 决定 ——
> 而 `max_concurrency` 没显式指定时，**asyncio actor 是 1000、同步 actor 是 1**
> （见上表）。想让默认组串行，得显式给默认组指定并发度。

**邮箱背压**：`max_concurrency` 只管「同时在跑多少」，不管「排队的积了多少」。
一个慢 actor 被狂轰滥炸时，队列会把 driver 的内存吃光：

```python
@ray.remote(max_concurrency=4, max_pending_calls=100)
class Limited:
    async def work(self, x): ...
```

`max_pending_calls` 限制**排队中**的调用数（默认 `-1`，即无上限）；超出后新调用会
**立刻失败**，抛的是 **`ray.exceptions.PendingCallsLimitExceeded`**
（不是 `RayActorError`），而不是无限堆积。
⚠️ 两个容易搞错的点：① 计数是**按 handle 计**的，不是按 actor 全局计；
② 它把背压责任交给调用方 —— 调用方必须自己 `ray.wait` 限流（§2.3 的
*limit-pending-tasks* 模式），不能指望 actor 无限吸收。

**Actor 的生成器方法**（流式返回，用途比想象中多）：

```python
@ray.remote
class Streamer:
    @ray.method(num_returns="streaming")   # 旧名 "dynamic" 已弃用
    def stream(self, n):
        for i in range(n):
            yield i * i                 # 逐块产出,不必等全部算完

gen = Streamer.remote().stream.remote(5)
for ref in gen:                         # ObjectRefGenerator 可迭代
    print(ray.get(ref))                 # 0 1 4 9 16
```

---

## 2.7 用 Python 描述集群：资源声明

Ray 把「资源」当成一等公民，而资源就是 `@ray.remote` 上的几个数字：

```python
ray.init(num_cpus=8, num_gpus=2, resources={"TPU": 4})

@ray.remote(num_gpus=1)
def train():
    import os
    print(os.environ["CUDA_VISIBLE_DEVICES"])   # Ray 自动分配:0 或 1

@ray.remote(resources={"TPU": 1})
def tpu_work():
    ...
```

几个关键语义：

* **0 CPU 任务**：`num_cpus=0` 的任务不占资源，理论上可以无限并发 ——
  限制它的是 worker 池大小。这是「我的并发上不去」类问题最常见的根因；
* **小数资源**：`num_cpus=0.5` 允许两个任务共享一个核（Ray 不做 CPU 隔离，
  只是记账）；
* **GPU 默认独占，但可以声明小数**：`num_gpus=1` 的任务会拿到
  `CUDA_VISIBLE_DEVICES`，同一块卡不会同时分给两个任务（靠可见性隔离，
  **Ray 不会阻止你绕过它**）。写 `num_gpus=0.5` 则表示「两块这样的任务挤一张卡」——
  此时两个任务会看到**同一个** `CUDA_VISIBLE_DEVICES`，**显存要你自己分**
  （Ray 只做记账，不做显存隔离）。这是把「小模型多副本」塞进一张卡的常用手段，
  代价是 OOM 风险转移给了你；
* **资源不足时任务排队**，不是失败。但如果「单任务请求量 > 单节点总量」，
  就是**永远排不上**的硬错误 —— 见 §2.8 的 `TaskUnschedulableError`。

```python
ray.cluster_resources()     # {'CPU': 8.0, 'GPU': 2.0, 'TPU': 4.0, 'node:10.0.0.1': 1.0, ...}
ray.available_resources()   # 当前还能用的
```

---

## 2.8 错误模型：跨进程的异常怎么传

分布式系统里「错误发生在另一个进程」是常态。Ray 的做法是：

1. worker 捕获异常，把**类型名、消息、堆栈字符串**打包；
2. 调用方 `ray.get` 时重新抛出一个 `RayTaskError`；
3. 原始异常挂在 `.cause` 上，可以用 `.as_instanceof_cause()` 还原成原类型。

```
worker 进程                                  driver 进程
───────────                                  ───────────
raise ValueError("boom")
     │
     ▼
RayTaskError(cause=ValueError, traceback_str=…)
     │  ────────── pickle ──────────▶
     ▼
                                          ray.get(ref)
                                          └─ raise RayTaskError
                                             ├─ .cause            → ValueError 实例
                                             ├─ .traceback_str    → 远程堆栈
                                             └─ .as_instanceof_cause() → 还原 ValueError
```

异常家族（`ray.exceptions`）：

| 异常 | 含义 | 该怎么做 |
|---|---|---|
| `RayTaskError` | 任务（函数）抛异常 | 看 `.cause` 和 `.traceback_str` |
| `RayActorError` | **actor 进程死了 / 暂时不可用**（子类 `ActorDiedError`、`ActorUnavailableError`）；⚠️ **actor 方法抛的 Python 异常不是它** —— 那和普通任务一样是 `RayTaskError` | 检查 actor 状态 |
| `WorkerCrashedError` | worker 进程崩溃（段错误/OOM/`os._exit`） | 通常是内存或本地库问题 |
| `ActorDiedError` | actor 死亡且无法重启 | 状态已丢，需要重建 |
| `TaskCancelledError` | 被 `ray.cancel` 取消 | —— |
| `GetTimeoutError` | `ray.get(timeout=)` 超时 | 看是排队、依赖未完成还是上游失败 |
| `ObjectLostError` | 对象丢失且无法重建 | lineage 重建失败的最终结果 |
| `OwnerDiedError` | **对象的所有者（owner）进程死了**，对象无法再取到 | 别让 driver 持有长生命周期对象的 owner 身份；用 detached actor 或 `ray.put` 到具名 actor |
| `TaskUnschedulableError` | 任务**永远排不上**（资源需求超过集群总量） | 检查 `num_cpus`/`num_gpus` 是否比集群还大；这类错误不会自愈 |
| `ActorUnschedulableError` | actor 排不上（同上，且 actor 会一直重试占资源） | 同上；注意 actor 会**持续**占用资源等待 |
| `ObjectStoreFullError` | 对象存储满且无法腾空间 | 调大 `object_store_memory` 或切小对象 |
| `RaySystemError` | Ray 内部错误（GCS/raylet 异常） | 查 raylet/GCS 日志，通常是集群级问题 |
| `RayChannelError` | Compiled Graph / 通道（channel）出错 | 见第 26 章 |

**⚠️ 默认不重试应用异常** —— 这一点被大量中文资料写错：

| 参数 | 默认值 | 管什么 |
|---|---|---|
| `max_retries` | **3** | **worker 崩溃 / 机器故障**导致的失败（`-1` 无限，`0` 关闭） |
| `retry_exceptions` | **`False`** | **应用异常**要不要重试；`True` = 全重试，或传**异常类型列表**。**只有这两种** —— 见下面的 ⚠️ |

```python
@ray.remote                                  # 崩溃重试 3 次;抛 ValueError 不重试
def strict(): ...

@ray.remote(retry_exceptions=True)           # 任何异常都重试(最多 1+3 次)
def flaky_io(): ...

@ray.remote(retry_exceptions=[ConnectionError, TimeoutError])
def call_service(): ...                      # 只重试这两类
```

> ⚠️ **`retry_exceptions` 只接受布尔或异常类型列表，不接受判定函数。**
> 2.58 的类型约束是 `(bool, list, tuple)`（源码
> `python/ray/_common/ray_option_utils.py` 的
> `"retry_exceptions": Option((bool, list, tuple), ...)`），
> 校验失败时在 **`@ray.remote` 装饰期**就抛：
>
> ```
> TypeError: The type of keyword 'retry_exceptions' must be
> (<class 'bool'>, <class 'list'>, <class 'tuple'>), but received type <class 'function'>
> ```
>
> 想要「只重试 5xx」这种粒度，做法是**在函数体里判断，再转成白名单里的异常类型**：
>
> ```python
> class RetryableHTTPError(Exception): ...
>
> @ray.remote(retry_exceptions=[RetryableHTTPError])
> def call():
>     try:
>         ...
>     except HTTPError as e:
>         if e.status_code >= 500:
>             raise RetryableHTTPError(str(e))   # 转成白名单类型 → 会重试
>         raise                                  # 其余照原样抛 → 不重试
> ```

Ray 官方文档的原话是 *"By default, Ray will **not** retry tasks upon exceptions
thrown by application code."* 之所以拆成两个参数：两类失败的性质完全不同 ——
worker 崩溃**一定**是环境问题，重试几乎总是对的；而应用异常**大多数**是代码 bug，
重试只会把错误放大 3 倍、拖慢失败反馈。

> 写 `retry_exceptions` 时**优先用异常类型列表**，别图省事写 `True` ——
> `True` 会把 `ValueError`、`KeyError` 这类「重试一万次也一样」的 bug
> 也重试满 4 次。完整讨论见第 10 章 §10.2。

**取消**：

```python
ray.cancel(ref)                  # 只取消还没开始执行的任务
ray.cancel(ref, force=True)      # 掐掉正在执行它的 worker(会牵连该 worker 上其它工作)
```

---

## 2.9 生命周期与命名空间

```python
ray.init()                       # 启动/连接集群(本地则自动起一个)
print(ray.is_initialized())      # True
ray.shutdown()                   # 关闭
```

* **重复 init 会报错**（除非 `ignore_reinit_error=True`）；
* `address` 的四种写法（**连不上集群，九成问题出在这里**）：

| 值 | 含义 | 用途 |
|---|---|---|
| 不传 / `None` | 本地起一个单机集群 | 开发、笔记本 |
| `"auto"` | 连接**本机已存在**的集群（查 `/tmp/ray/` 或 `RAY_ADDRESS`） | 在集群节点上跑 driver |
| `"local"` | **忽略已有集群、强制新起一个本地多进程集群**（不是单进程调试！） | 想确保「跑的绝不是别人的集群」时 |
| `"ray://<head>:10001"` | **Ray Client** —— 连远端集群的控制面 | 笔记本连远端集群 |

`ray://` 这一种要特别说明：它走的是 **Ray Client**，端口是 **10001**
（client server），不是 GCS 的 6379。它的特点是「driver 逻辑在本地跑，
每次 `ray.get`/`.remote()` 都是一次网络往返」——所以**它对细粒度任务很慢**，
且不少库（如 Ray Data）在 Client 模式下有限制。

> **2026 年的建议**：Ray Client 已进入维护状态，官方更推荐
> **Jobs API**（§4.7）或**在集群内跑 driver**。新项目别把 `ray://` 当默认方案，
> 它更适合「临时调试远端集群」。见第 17 章与第 24 章 D.2 的 Q7。

* **namespace** 是「名字服务」的隔离边界，主要影响命名 actor 和 Dashboard 的可见性：

```python
ray.init(namespace="team-a")
Counter.options(name="counter", lifetime="detached").remote()
ray.get_actor("counter")         # 同一 namespace 内可查
```

`lifetime="detached"` 表示 driver 退出后 actor 仍然活着（供后续 job 复用）——
这是 Ray 里唯一「比 driver 活得久」的东西。

---

## 2.10 API 全景

| 类别 | API |
|---|---|
| 任务 | `@ray.remote` / `.remote()` / `.options()` / `.bind()` / `ray.method` |
| 对象 | `ray.put` / `ray.get` / `ray.wait` / `ray.cancel` / `ObjectRef` |
| Actor | `@ray.remote`（装饰类）/ `ray.get_actor` / `ray.util.state.list_actors`（列全部）/ `ray.util.list_named_actors`（列命名）/ `ray.kill`。⚠️ **`ray.list_actors` 与 `ray.util.list_actors` 都不存在** |
| 集群 | `ray.init` / `ray.shutdown` / `ray.is_initialized` / `ray.nodes` / `ray.cluster_resources` / `ray.available_resources` |
| 调度 | `ray.util.placement_group` / `ray.util.scheduling_strategies` |
| 可观测性 | `ray.util.state` / `ray.timeline` / `ray.get_runtime_context` / `ray.get_gpu_ids` |
| 工具 | `ray.util.ActorPool` / `ray.util.queue` / `ray.util.serialization` |

---

## 2.11 本章小结

* 三个抽象：**Task（无状态）/ Actor（有状态）/ Object（不可变数据）**，
  ObjectRef 是贯穿三者的「未来值句柄」。
* `f.remote()` 微秒级返回，`ray.get` 才是同步点 —— 所有并发/性能问题都从这条推论出来。
* 依赖图是**数据流隐式表达**的：把 ref 当参数传就行。
* Actor 四条语义：异步方法 + 顺序执行、资源终身持有、默认不重启、句柄可传参；
  并发度由 `async def` 与 `max_concurrency` 决定（asyncio 默认 1000、其它 1）。
* 资源就是几个数字，**资源不足会排队而不是失败**；0 CPU 任务不受资源限制。
* 错误跨进程传递：`RayTaskError` + `.cause` + `.as_instanceof_cause()`；
  **默认不重试应用异常** —— `max_retries=3`（默认值）只管 **worker 崩溃**这类系统失败，
  想让 `ValueError` 这类应用异常也重试，必须显式写 `retry_exceptions=True`（见 §2.8）。

下一章我们把镜头拉远，看这四个抽象背后站着哪些进程：GCS、raylet、
core worker、对象存储 —— 以及它们是怎么协作把一次 `f.remote()` 变成远程执行的。
