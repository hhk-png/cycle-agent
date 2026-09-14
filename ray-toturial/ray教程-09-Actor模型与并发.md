仓库地址：https://github.com/hhk-png/cycle-agent

# 第 9 章：Actor 模型与并发

> 本章目标：把 Actor 从「会写」讲到「会用对」。包含邮箱与顺序语义、
> 三种并发模型（含默认并发度为什么是 1000）、异步 actor、生命周期与重启、
> 五个经典模式，以及四个会咬人的反模式。

---

## 9.1 为什么需要 Actor：三种「状态」的存放方式

任务是无状态的。一旦你需要「记住点什么」，就有三个选择：

| 方案 | 写法 | 问题 |
|---|---|---|
| 每次重新加载 | 任务里 `load()` | 慢（每个任务都付一次加载成本） |
| 用 `ray.put` 传状态 | `state_ref = ray.put(state)` 然后传参 | 状态不可变；每次更新都要新建对象；并发更新会丢更新 |
| **Actor** | `A.remote()` 常驻 | 状态在内存里、可变、串行访问 |

第三种的本质是：**一个常驻进程 + 一个邮箱 + 串行执行**。

```
        提交                       邮箱(按组排队)            执行
driver ──▶ handle.m.remote() ──▶ [t1][t2][t3] ──▶ actor 进程里的实例
                                                     └─ self.state 就在内存里
```

---

## 9.2 邮箱与顺序保证

```python
counter = Counter.remote()
refs = [counter.inc.remote() for _ in range(10)]   # 10 个调用几乎同时发出
ray.get(refs)                                       # [1..10] 严格有序
```

Ray 保证的**顺序语义**：

| 保证 | 说明 |
|---|---|
| ✅ 同一个 actor 内，方法按**提交顺序**执行 | 默认（并发度 1） |
| ✅ 同一个 client 提交的方法保持顺序 | 邮箱按到达顺序排队 |
| ⚠️ 并发度 > 1 时可能乱序 | 同一时刻最多 N 个在跑，完成顺序不保证 |
| ⚠️ 不同 client（不同 driver/worker）之间没有全局顺序 | 只能保证「各自提交的相对顺序」 |
| ❌ 不保证「提交成功」与「执行完成」之间的原子性 | 失败重试可能重复执行 |

**最后一条很重要**：actor 方法在重试语义下**可能执行两次**
（`max_task_retries > 0` 时）。所以有副作用的方法（扣款、发消息）
必须自己做幂等。

### 邮箱是无界的 —— 除非你给它设上限

上面那张表讲的都是「顺序」，但邮箱还有一个更容易出事儿的性质：**它默认不封顶**。

```
driver 疯狂提交 ──▶ [ actor 邮箱: 无限增长 ] ──▶ actor 慢慢执行
                          ▲
                          └── 这些排队的调用会一直占着 driver 侧的内存
```

一个慢 actor 被高频调用时，队列会一直涨 —— 最后 OOM 的往往不是 actor，
而是**提交方**。`max_concurrency` 管不了这件事：它只限制「同时在跑几个」，
不管「排队的积了多少」。

```python
@ray.remote(max_concurrency=4, max_pending_calls=100)
class Limited:
    async def work(self, x): ...
```

| 参数 | 限制的是 | 默认值 |
|---|---|---|
| `max_concurrency` | **正在执行**的方法数 | 1（asyncio actor 为 1000） |
| `max_pending_calls` | **排队等待**的调用数 | 无上限 |

超出 `max_pending_calls` 后，新调用会**立刻失败**而不是排队 ——
这是故意的：**背压的责任被推给调用方**。所以调用方必须自己限流
（用第 5 章 §5.7 的 *limit-pending-tasks* 模式），不能指望 actor 无限吸收。

> ⚠️ **这是个「加了才知道疼，不加更疼」的参数**：不加 → 内存涨到 OOM；
> 加了但调用方没做限流 → 调用方开始收到一堆失败。
> **两个要一起改**，只改一边等于把问题从 A 挪到 B。

### Actor 的流式方法

actor 方法也可以是生成器，用途是「常驻状态 + 边算边返回」：

```python
@ray.remote
class LogReader:
    def __init__(self, path):
        self.path = path

    @ray.method(num_returns="streaming")      # 其实可省:生成器方法默认就是 streaming
    def tail(self, n):
        with open(self.path) as f:
            for i, line in enumerate(f):
                if i >= n: break
                yield line

gen = LogReader.remote("app.log").tail.remote(100)
for chunk_ref in gen:
    print(ray.get(chunk_ref))
```

⚠️ **`num_returns="streaming"` 不必显式写**：生成器任务/方法**默认就是 streaming**
（源码 `remote_function.py`：`if num_returns is None: num_returns = "streaming"
if self._is_generator else 1`；actor 侧同构）。
网上（含本章早期版本）流传的「必须显式声明、否则报错」是**错的**；
真正要求显式声明的是 **mini-ray**，不是 Ray。

重试语义与普通生成器任务一致 —— **整个任务从头重放，旧 attempt 的产出靠
`attempt_number` 丢弃**（见第 5 章 §5.3 与第 10 章 §10.2）。
不是「跳过已产出的值、从断点继续」。

---

## 9.3 三种并发模型

| 形态 | 触发条件 | 默认并发度 | 执行方式 |
|---|---|---|---|
| **同步 actor** | 方法都是普通函数 | **1** | 一个接一个 |
| **线程化 actor** | 同步方法 + `max_concurrency=N` | N | N 个线程 |
| **asyncio actor** | 有 `async def` 方法 | **1000** | 事件循环，可 `await` |

⚠️ **asyncio actor 的默认并发度 1000**（官方文档明确给出：*"默认并发组的并发度：
AsyncIO actor 为 1000，其它为 1"*）。这意味着你只要写了一个 `async def` 方法，
actor 就自动变成高并发 —— 对「IO 密集」是好事，对「有状态且需要串行」是**陷阱**。

### 并发组：让不同方法有不同的并发度

```python
@ray.remote(concurrency_groups={"io": 8, "compute": 2})
class Hybrid:
    def __init__(self):
        self.cache = {}

    @ray.method(concurrency_group="io")
    async def fetch(self, url):
        # IO 密集:8 路并发
        return await http_get(url)

    @ray.method(concurrency_group="compute")
    def crunch(self, data):
        # CPU 密集:2 路并发(避免把机器打满)
        return heavy_math(data)
```

语义要点：

* 每组独立排队、独立限流；**跨组没有顺序保证**；
* 不指定组的方法属于「默认组」（并发度由 `max_concurrency` 决定）；
* 并发组对 asyncio 与线程化 actor 都有效，语法一样。

---

## 9.4 异步 Actor：`await` 一切

```python
@ray.remote
class AsyncPipeline:
    def __init__(self):
        self.calls = 0

    async def step(self, ref):
        value = await ref              # ← 直接 await 一个 ObjectRef!
        self.calls += 1
        return value * 2

    async def fan_out(self, refs):
        # 并发等待多个结果,而不是一个个 await
        return await asyncio.gather(*refs)
```

**`await ObjectRef` 是 Ray 的语法糖**：等价于「把阻塞的 `ray.get` 丢到线程池，
不阻塞事件循环」。它的价值在于：一个 async actor 可以同时处理成百上千个请求，
每个请求在等下游时让出事件循环。

### 什么时候该用 async actor

| 场景 | 建议 |
|---|---|
| 方法要调用外部 HTTP/RPC、等数据库 | ✅ async actor（并发度天然高） |
| 方法是纯 CPU 计算 | ❌ 用 `max_concurrency` 线程化，或干脆用任务 |
| 需要严格串行的有状态操作 | ❌ 用默认同步 actor，或显式 `max_concurrency=1` |
| 混合负载 | ✅ 并发组 + 各组不同并发度 |

⚠️ 一个常见错误：**在 async actor 里调用阻塞函数**（`requests.get`、
`time.sleep`、同步数据库驱动）—— 那会把整个事件循环卡住，
async 的好处全没了。要么用 async 版的库，要么在 executor 里跑。

---

## 9.5 生命周期：创建、重启、命名、detached

### 创建是异步的

```python
counter = Counter.remote()          # 立刻返回句柄,不阻塞
# __init__ 还没跑完也没关系,方法调用会排在邮箱里
```

`__init__` 失败时，错误在**第一次方法调用**时抛出来（`RayActorError`），
而不是在 `.remote()` 处 —— 因为创建本身是异步提交的。

### 重启语义

```python
@ray.remote(max_restarts=3, max_task_retries=2)
class Robust:
    def __init__(self):
        self.state = load_checkpoint()    # 重启后会重跑这里
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `max_restarts` | **0** | actor 进程崩溃后最多重启几次；**状态不恢复** |
| `max_task_retries` | **0** | 在飞的方法调用是否重试（跟随重启） |

**重启后状态一定会丢** —— 因为进程换了。要恢复状态只有两条路：

1. `__init__` 里从检查点加载（简单、推荐）；
2. 自己维护外部状态（数据库/共享存储）。

### 命名与 detached

```python
# 命名 actor:可以在别的进程/driver 里按名字找到
Counter.options(name="global-counter", lifetime="detached").remote()

handle = ray.get_actor("global-counter")     # 任何地方都能拿到
ray.kill(handle)                              # 显式销毁(否则一直存在)
```

* `lifetime="detached"`：**driver 退出后 actor 仍然活着**（Ray 里唯一比 job 长寿的东西）；
* 命名 actor 是「跨 job 共享状态」的标准做法（比如共享缓存、限流器）；
* ⚠️ 命名 actor 容易泄漏：忘记 `ray.kill` 就会一直占着资源和名字。

### 杀死 actor

```python
ray.kill(handle)                 # 默认 no_restart=True:不再重启
ray.kill(handle, no_restart=False)  # 让它按 max_restarts 重启
```

---

## 9.6 资源与放置

actor 的资源语义**分两种情况**，这是 Ray 里最容易记错的一处默认值
（源码 `python/ray/actor.py` 的 `_remote()`，官方文档
`doc/source/ray-core/scheduling/resources.rst`）：

| | 默认情况（**完全没写**资源） | 写了资源（`num_cpus` / `num_gpus` / `resources` 任一） |
|---|---|---|
| actor **创建**时占的 CPU | **0**（终身持有 0 个 CPU） | **1**（没写就补 1），**终身持有** |
| 每次**方法调用**占的 CPU | **1** | **0** |
| 调度时按什么放 | 按「1 CPU」放（所以 0 核节点上放不下） | 按声明的资源放 |

* ⚠️ **注意「终身持有」只在写了资源时才成立**。默认情况下 actor 终身占 **0** 个 CPU，
  你的 `num_cpus` 其实是**每次方法调用**扣的。所以：
  * 不写资源 → 可以起**近乎无限个** actor（每个方法调用才吃 1 CPU）；
  * 写了 `num_cpus=2` → 这个 actor **从生到死**占着 2 个 CPU，起不了几个。
* 排查「资源去哪了」的第一站仍然是 `state.list_actors()`，但**先看清**你属于哪种情况。
* 想做一个「只做协调、不占 CPU」的 actor，就显式写 `num_cpus=0`
  —— 这会把它切到上表右边一列：终身占 0，方法调用也占 0。
* **actor 要放 GPU 上**：`Worker.options(num_gpus=1).remote()`；
* **多个 actor 需要「一起」放下**：用放置组（第 8 章），这是分布式训练不卡死的标准做法。

```python
@ray.remote(num_gpus=1)
class Trainer:
    def __init__(self, rank):
        import os
        self.device = os.environ["CUDA_VISIBLE_DEVICES"]
```

> ⚠️ **mini-ray 的简化（读第 06 / 22 章时请记住）**：mini-ray **只实现了上表右边一列**
> —— actor 的资源一律**终身持有**，且**不为方法调用扣 CPU**。
> 也就是说，在 mini-ray 里 `@ray.remote class A` 起手就占 1 个 CPU，
> 与真实 Ray 的「默认占 0」不同。
> 这是**刻意的简化**（把「每次方法调用扣 1 CPU」也做对，需要在邮箱派发路径上
> 引入一次资源申请/释放，会让调度器的教学主线变复杂）。写 mini-ray 代码时
> 按「终身持有」估算容量；写真实 Ray 代码时按上表。

---

## 9.7 五个经典模式

### 模式 1：参数服务器

```python
@ray.remote
class ParameterServer:
    def __init__(self, dim):
        self.weights = [0.0] * dim
    def pull(self): return list(self.weights)
    def push(self, grads, lr):
        for i, g in enumerate(grads):
            self.weights[i] -= lr * g
        return list(self.weights)

@ray.remote
def worker(server, shard):
    w = ray.get(server.pull.remote())
    grads = compute_gradients(w, shard)
    return ray.get(server.push.remote(grads, 0.1))

ray.get([worker.remote(server, s) for s in shards])
```

**要点**：状态在 actor 里串行更新（避免并发写坏），worker 是无状态任务。
（真实训练会用 all-reduce 取代中心 PS，但骨架相同 —— mini-ray 的
`examples/10_parameter_server.py` 就是这个模式的可运行版本。）

### 模式 2：常驻模型推理服务

```python
@ray.remote(num_gpus=1)
class Predictor:
    def __init__(self, model_path):
        self.model = load_model(model_path)      # 只加载一次

    def predict(self, batch):
        return self.model(batch)

predictors = [Predictor.remote(path) for _ in range(4)]      # 4 张卡 4 个副本
# 轮询分发:简单但有效
refs = [predictors[i % 4].predict.remote(b) for i, b in enumerate(batches)]
```

**要点**：模型常驻、按 GPU 数复制副本、批量化调用。Ray Serve 本质上是把这个模式
产品化（加上了自动扩缩、路由、健康检查）。

### 模式 3：ActorPool：一组 actor 处理一批输入

```python
from ray.util import ActorPool

pool = ActorPool([Predictor.remote(path) for _ in range(4)])
results = list(pool.map_unordered(lambda a, x: a.predict.remote(x), batches))
```

**要点**：`map_unordered` 谁先算完先返回；`map` 保证顺序但有队头阻塞。
它内部用 `ray.wait` 实现背压（每个 actor 手里最多一个任务）。

### 模式 4：限流器 / 令牌桶

```python
@ray.remote
class RateLimiter:
    def __init__(self, rate_per_sec):
        self.rate = rate_per_sec
        self.last = time.time()

    def acquire(self):
        now = time.time()
        wait = max(0.0, 1.0 / self.rate - (now - self.last))
        time.sleep(wait)
        self.last = time.time()
        return True
```

**要点**：串行 actor 天然是「全局有序的协调点」，非常适合做限流、发号、分配 ID。

### 模式 5：有状态的任务编排（工作流）

```python
@ray.remote
class Pipeline:
    def __init__(self):
        self.stage = 0
        self.intermediate = None

    def run_stage(self, data):
        self.intermediate = heavy_step(self.stage, data)
        self.stage += 1
        return self.stage
```

⚠️ 注意：Ray Workflows 这个专门的库**已在 2.44 弃用、并从后续版本移除**
（`ray==2.47` 是最后一个含 `ray.workflows` 的版本）。所以「工作流」在 2026 年
要么手写（如上），要么用 Ray Data / Serve 的组合，要么用外部编排器（Airflow 等）。

---

## 9.8 四个会咬人的反模式

### 反模式 1：把 actor 当无状态函数用

```python
@ray.remote(num_cpus=4)
class Worker:
    def compute(self, x):        # 根本不用 self
        return heavy(x)

# ✗ 创建 100 个 → 400 CPU 被终身占住,但每个 actor 大部分时间在空闲
# ✓ 用任务(公共函数),资源按需占用按需释放
```

**判据**：如果所有方法都不读写 `self`，那它应该是任务。

### 反模式 2：无界邮箱

```python
# ✗ 一次提交 100 万个方法调用 → 邮箱爆、对象存储爆
refs = [actor.process.remote(x) for x in huge_list]

# ✓ 用 ray.wait 做背压,保证「在飞数量」有界
pending = []
for x in huge_list:
    while len(pending) >= 100:
        ready, pending = ray.wait(pending, num_returns=1)
        handle(ray.get(ready)[0])
    pending.append(actor.process.remote(x))
```

### 反模式 3：在 actor 里同步等另一个 actor（死锁）

```python
@ray.remote
class A:
    def step(self, b):
        return ray.get(b.work.remote())     # ✗ A 阻塞等 B

@ray.remote
class B:
    def work(self): ...

# 如果 B 也需要 A(循环等待) → 死锁。
# 更隐蔽的情况:actor 的并发度是 1,而它等待的结果需要它自己先完成
```

**正确做法**：actor 之间不要形成循环依赖；需要编排时把「等待」放在 driver
（或者用 async actor + `await`，让出执行权而不是阻塞线程）。

### 反模式 4：让命名 actor 泄漏

```python
Cache.options(name="cache", lifetime="detached").remote()
# ... 忘了 kill
# 下次运行 → ActorAlreadyExistsError(它是 ValueError 的子类,所以 except ValueError 仍然成立):
#   The name cache (namespace=None) is already taken. Please use a different name
#   or get the existing actor using ray.get_actor('cache', namespace='None')
```

**Ray 给的标准解法是 `get_if_exists=True`** —— 语义是"**幂等获取**"：
名字已被占用时**返回已有 actor 的句柄**，而不是报错。

```python
# ✅ 重跑多少次都不会报错;第一次创建,之后拿到同一个 actor
cache = Cache.options(name="cache", lifetime="detached",
                      get_if_exists=True).remote()
```

> ⚠️ 注意它**不检查类型**：如果那个名字上挂着的是**另一个类**的 actor，
> `get_if_exists=True` **照样会把句柄给你**，你在调用方法时才会炸。
> 所以它治的是"重跑时报名字冲突"，不是"用错了 actor"。
> 默认值是 `False`（`ray/_common/ray_option_utils.py` 里
> `"get_if_exists": Option(bool, default_value=False)`）。

**判据**：命名 actor 应该像「资源」一样管理 —— 谁创建、谁销毁，
写在代码注释里。**用 `get_if_exists=True` 做幂等入口，用 `ray.kill()` 做显式销毁。**

---

## 9.9 mini-ray 的实现对照

| 机制 | mini-ray | 与 Ray 的差异 |
|---|---|---|
| 邮箱 | `_ActorRuntime.mailboxes`（按并发组拆分 deque） | 语义一致（FIFO） |
| 并发度 | 线程数 = 并发度（每个线程一个长轮询） | Ray 是同一进程内的线程池/事件循环 |
| 默认并发度 | asyncio 1000 / 其它 1 | 与官方文档一致 |
| 并发组 | `@miniray.method(concurrency_group=...)` + 每组独立线程 | 语义一致 |
| async actor | `_AsyncActorContext`（asyncio 事件循环 + 轮询协程） | 语义一致 |
| `await ObjectRef` | `ObjectRef.__await__`（`run_in_executor`） | 一致 |
| 重启 | `max_restarts` + `max_task_retries`，重启后重跑 `__init__` | 一致（状态都会丢） |
| 命名 actor | GCS 里的 `named_actors` | 一致 |
| detached | 记录标志，但**只在同一进程生命周期内有效** | 差异（因为没有独立 raylet 进程） |
| 创建失败 | 错误在首次方法调用时抛出 | 一致 |

---

## 9.10 本章小结

* Actor = **常驻进程 + 邮箱 + 串行执行**；它解决「状态放哪」的问题。
* 顺序语义：同一 actor 内按提交顺序；**并发度 >1 或跨 client 时不再保证**，
  而且重试可能导致**方法执行两次**（副作用要幂等）。
* 三种并发模型：同步（默认 1）、线程化（`max_concurrency=N`）、
  asyncio（**默认 1000**）；`concurrency_groups` 可以让不同方法有不同并发度。
* async actor 的价值是「等待时不占线程」；但**在 async 方法里调用阻塞函数会毁掉一切**。
* 生命周期：创建异步、`__init__` 失败在首次调用时暴露、重启**必然丢状态**、
  命名 + `detached` 是跨 job 共享状态的正道，但要注意泄漏。
* 五个经典模式：参数服务器、常驻推理、ActorPool、限流器、有状态编排。
* 四个反模式：把 actor 当函数、无界邮箱、actor 之间循环等待、命名 actor 泄漏。

下一章讲容错：重试、崩溃恢复、lineage 重建，以及「什么时候该自己写检查点」。
