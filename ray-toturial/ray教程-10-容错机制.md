仓库地址：https://github.com/hhk-png/cycle-agent

# 第 10 章：容错机制

> 本章目标：把「出错」当成常态来设计。讲清 Ray 的四层容错（任务重试、
> worker 崩溃、对象丢失的 lineage 重建、GCS 容错），以及**每层的代价与边界** ——
> 哪些是 Ray 保证的，哪些必须你自己做。

---

## 10.1 故障分类：先分清「谁的错」

| 故障 | 现象 | Ray 的机制 | 你要做什么 |
|---|---|---|---|
| 函数抛异常 | `RayTaskError` | **默认不重试**（要开 `retry_exceptions`） | 幂等 + 显式选择是否重试 |
| worker 进程崩溃 | `WorkerCrashedError` | 换 worker 重跑（`max_retries`，默认 3） | 查内存/本地库 |
| 对象丢失 | `ObjectLostError` | **lineage 重建** | 重要数据自己落盘 |
| actor 崩溃 | `RayActorError` / `ActorDiedError` | `max_restarts`（默认 0） | 状态要靠检查点恢复 |
| 节点故障 | 该节点任务/对象全丢 | 任务重跑 + 对象重建 | 多副本/检查点 |
| GCS 挂了 | **整个集群挂** | 默认无容错 | RocksDB/Redis 后端（有限制） |
| driver 挂了 | job 结束 | —— | 用 Jobs API / 把 driver 做薄 |

**核心前提**：Ray 的所有恢复机制都建立在同一个假设上 ——
**任务是可以重放的**。所以：

> **任务必须是幂等的（或至少是确定性的）**，否则重试/重建会得到不一致的结果。

---

## 10.2 任务重试：语义、代价与陷阱

### ⚠️ 先纠正一个最常见的误解

**`max_retries` 只管「worker 死了」，不管「代码抛异常」。**

Ray 官方文档的原话是：

> *"By default, Ray will **not** retry tasks upon exceptions thrown by
> application code."*
> —— 应用代码抛的异常**默认不重试**。

所以要分清两个参数：

| 参数 | 默认值 | 管什么 |
|---|---|---|
| `max_retries` | **3** | **worker 进程崩溃 / 机器故障**导致的失败（`-1` 无限，`0` 关闭） |
| `retry_exceptions` | **`False`** | **应用异常**要不要重试。`True` = 全重试，也可传异常类型列表 |

```python
@ray.remote                          # 崩溃会重试 3 次;抛 ValueError 不重试
def strict(): ...

@ray.remote(retry_exceptions=True)   # 任何异常都重试(最多 1+3 次)
def flaky_io(): ...

@ray.remote(retry_exceptions=[ConnectionError, TimeoutError])
def call_service(): ...              # 只重试这两类,ValueError 立刻失败
```

> ⚠️ **`retry_exceptions` 只有两种写法：布尔、或异常类型列表 —— 没有判定函数。**
> 2.58 的类型约束是 `(bool, list, tuple)`
> （`python/ray/_common/ray_option_utils.py` 的
> `"retry_exceptions": Option((bool, list, tuple), ...)`），
> 传 `lambda` 会在 **`@ray.remote` 装饰期**直接抛 `TypeError`
> （`The type of keyword 'retry_exceptions' must be ... but received type <class 'function'>`）。
>
> 那「只重试 `HTTPError` 里 5xx 的那些」怎么做？**在函数体里判断，
> 转成白名单里的异常类型**：
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
>             raise RetryableHTTPError(str(e))   # 白名单类型 → 会重试
>         raise                                  # 4xx 照原样抛 → 不重试
> ```
>
> 多一层自定义异常，换来的是**粒度**：白名单决定"哪类重试"，
> 函数体决定"什么情况下归到那一类"。

**Actor 侧的另一套参数**（别和任务的搞混）：

| 参数 | 默认值 | 管什么 |
|---|---|---|
| `max_task_retries` | **0** | actor **崩溃**后，未完成的方法要不要重跑（`-1` 无限） |
| `max_restarts` | **0** | actor **进程**重启次数（`-1` 无限） |
| `retry_exceptions` | **False** | actor 方法里的**应用异常**要不要重试。**⚠️ 它不是 actor 的类级选项**，而是**方法级**的 —— 写在 `@ray.method(...)` 上，见下 |

> ⚠️ **`retry_exceptions` 写到 actor 类上会被拒。**
> 它在 2.58 的 `ray_option_utils.py` 里属于 `_task_only_options`，
> 而 `actor_options = {**_common_options, **_actor_only_options}` —— **不含它**。
> 所以 `Counter.options(retry_exceptions=True).remote()` 会抛
> `ValueError: Invalid option keyword retry_exceptions for actors.`
>
> 正确写法是**方法级**：
>
> ```python
> @ray.remote
> class Worker:
>     @ray.method(retry_exceptions=[ConnectionError])
>     def fetch(self, url): ...
> ```
>
> 上面那张表把它和类级的 `max_restarts` / `max_task_retries` 并列，
> 是因为它管的是"actor 里的应用异常"，**不是**因为它能写在类上。

注意 actor 的 `max_task_retries` **默认是 0**（与任务的 `max_retries=3` 不同）——
Ray 不默认帮 actor 重试，因为 actor 有状态，重跑未必安全。

**为什么要分成两个参数？** 因为这两类失败的性质完全不同：
worker 崩溃**一定**是环境问题，重试几乎总是对的；
而应用异常**大多数**是代码 bug，重试只会把错误放大 3 倍、拖慢失败反馈。
把它拆开，是 Ray 相对「无脑重试」的进步。

> **一句实用建议**：写 `retry_exceptions` 时**永远用异常类型列表**，
> 别写 `True`。`True` 会把 `ValueError`、`KeyError`、`TypeError`
> 这些「重试一万次也一样」的 bug 也重试满 4 次。

**全局覆盖**：不想逐个任务改，可以设环境变量
`RAY_TASK_MAX_RETRIES`（对应 `max_retries`）。

**排查技巧**：`state.list_tasks()` 里 `attempt_number > 1` 就是重试过的任务
（字段名是 **`attempt_number`**，不是 `num_attempts` —— 后者是 mini-ray 的叫法）。
重试次数突然升高，通常意味着环境在抖动（网络、依赖服务）。

### 重试的代价

```python
@ray.remote(max_retries=3)
def charge_card(user_id, amount):
    stripe.charge(user_id, amount)     # ✗ 重试 = 重复扣款!
```

**有副作用的任务必须自己做幂等**：用业务侧的幂等键（`idempotency_key`）、
先写「待处理」再确认、或者干脆把副作用挪到 actor 里用状态去重。

### 重试的边界

* **不会自动重试应用异常**（除非开了 `retry_exceptions`，见上）；
* **不会恢复被 `ray.cancel` 取消的任务**；
* **`ObjectStoreFullError` 之类的失败**：对象存储内部已有重试
  （`object_store_full_delay_ms`，默认 10ms），所以别指望 `max_retries` 帮上忙 ——
  该做的是背压或调容量。

**生成器任务会重试，但方式是「整任务重放」**（常见误解有两个：
一是"流不能被重放所以不重试"，二是"会从断点续传" —— 两个都不对）：

> 一个流式生成器如果**已经 yield 了若干个值**、然后抛异常，
> Ray 会把当前 attempt 标记为失败、**attempt number 加一**，
> 然后**重新提交同一份 task spec** —— 也就是**从头再跑一遍**。

下游之所以看到「已经发出去的 4 个值没有重复、也没有断档」，
是因为**旧 attempt 产出的对象被 `attempt_number` 过滤掉了**，
而不是因为函数跳过了前 4 个。

⚠️ **这意味着 Ray 假设你的生成器是幂等且确定性的**
（官方 Internals 原文：*"this assumes that the generator task is
idempotent and deterministic"*）。如果生成器的输出依赖内部随机状态、时间、
或外部副作用，**重放会产出不一样的结果**。这是把生成器用于有副作用场景时的真实风险。

> ⚠️ **mini-ray 的差异**：mini-ray 要求生成器任务的 `max_retries=0`
> （见 `object_ref.py` 的注释），**不支持**生成器任务重试。
> 这是刻意的简化 —— 重放的幂等性要求很难在一段玩具实现里讲清楚，
> 宁可显式不支持，也不给一个会错位的实现。

---

## 10.3 worker 崩溃：最难的一类

```python
@ray.remote(max_retries=2)
def might_crash():
    os._exit(1)        # 段错误/OOM 也是这个效果
```

**Ray 的区分很重要**：

| 异常 | 含义 | 处理方式 |
|---|---|---|
| `RayTaskError` | 函数自己抛的 | 原地重试（大概率还是失败） |
| `WorkerCrashedError` | **进程没了** | 换一个 worker 重跑 |

为什么必须区分？因为「进程没了」意味着：
① 这个 worker 上的其它任务也可能受影响；② 它的内存状态全丢；
③ 可能是整机性问题（内存超了），重试前应该先看看资源。

### 崩溃的常见根因与处置

| 根因 | 证据 | 处置 |
|---|---|---|
| OOM（worker 堆爆） | `dmesg`/容器事件里有 `Killed`；Ray 日志有 worker 退出码 | 拆小任务、声明 `memory=`、降 batch |
| 段错误（C 扩展） | 退出码 139 / `Segmentation fault` | 固定依赖版本、隔离到独立任务 |
| `os._exit` / `sys.exit` 被误用 | 代码里显式调用 | 用户代码问题，改掉 |
| GPU 显存爆（CUDA OOM） | CUDA 报错 | 降 batch、用 `PYTORCH_CUDA_ALLOC_CONF` |
| 被 raylet 主动杀掉（内存监控） | raylet 日志有 "killed worker" | 调大对象存储/减少在飞对象 |

Ray 的 raylet 有内存监控（`memory_usage_threshold` 默认 0.95，
`memory_monitor_refresh_ms` 默认 250），会在节点内存接近上限时**杀 worker**——
这是有意的保护，不是 bug（否则整机会 OOM，损失更大）。

⚠️ **但「杀谁」要分两路看** —— 本书第四轮在这里只写了后一路，与第 7 章 §7.7
冲突，第五轮已统一（官方 OOM Prevention 文档的规则是**先分流**）：

| 候选 | 选择规则 |
|---|---|
| **idle / 可被杀的 worker** | **优先选内存占用最大的那个**（官方原文：*"select the worker with the largest memory footprint first"*）。⚠️ 冷启动就 idle 的 worker 还要**超过 `RAY_idle_worker_killing_memory_threshold_bytes`（默认 1 GiB）**才会被杀 |
| **active worker** | 2.56 起默认 **time-based killing policy**（`worker_killing_policy_by_group=false`）：**① 可重试的优先被杀** → ② 同级里「运行更短/更新」的优先 → ③ task 优先于 actor → ④ **必要时一次杀多个** |

所以"**不是**杀占用最多的"这句话**只对 active worker 成立** ——
对 idle worker 恰恰相反。两章现在同源，可互看 §7.7 / §10.3。

实践含义很重要：**声明了重试的任务反而会先被牺牲**（因为杀它代价最小），
而 `max_retries=0` 的任务会被尽量保留。要回到旧的「按内存占用」行为，
设 `RAY_worker_killing_policy_by_group=true`（详见第 7 章 §7.7）。

---

## 10.4 对象丢失与 lineage 重建

### 机制

Ray 的默认对象存储是**内存**，所以对象会丢（节点故障、内存压力下的驱逐）。
Ray 的应对是**不保存数据，保存"怎么算出来"**：

```
提交任务时,除了参数,还记下「血缘」:
    task_spec = (函数, 参数(含依赖 ref), 返回值个数, 资源需求…)

对象丢了(节点故障/驱逐后仍需要):
    ① 找到产出它的任务
    ② 递归重建它的依赖(深度优先)
    ③ 重新执行那个任务,**结果对象沿用原来的 ID**
    ④ 等待者无感:ray.get 只是继续等,拿到了正确的值
```

**为什么「沿用原 ID」很关键**：否则所有引用者都要被通知「引用换了」，
整个依赖图要重建 —— 那是 O(n) 的复杂度，而现在只需要重算一个对象。

### 成本与边界

| 边界 | 说明 |
|---|---|
| **任务必须确定性** | 如果任务读随机数/当前时间/外部状态，重建出来的值会不一样 |
| **重算是要花时间的** | 一个 10 分钟的预处理任务丢了，就要再花 10 分钟 |
| **依赖链会级联** | 一个对象丢了可能触发一整条链的重算 |
| **没有血缘的对象救不回来** | `ray.put` 的对象、外部数据 —— 只能报 `ObjectLostError` |
| **重建失败的最终结果** | `ObjectLostError`（Ray 会重试重建，失败才报） |
| **owner 死了是另一回事** | 见下：那是 `OwnerDiedError`，**不会**触发 lineage 重建 |
| **`max_retries=0` 会关掉重建** | 见下，这是最容易踩的一个连带效应 |

#### ⚠️ 两个「看起来一样、其实不同」的错误

对象取不到有**两种**截然不同的原因，错误类型不同、处置也不同：

| 错误 | 触发条件 | 能否靠 lineage 恢复 |
|---|---|---|
| `ObjectLostError` | 对象**内容**丢了（节点故障、驱逐），但产出它的任务规格还在 | ✅ 能，重放任务 |
| `OwnerDiedError` | **持有该对象所有权（owner）的进程死了** | ❌ 不能，血缘本身随 owner 一起没了 |

> ⚠️ **两者的语义不同，但类型上是父子关系**：2.58 的 `ray/exceptions.py` 里
> `class OwnerDiedError(ObjectLostError)`。所以**顺序反了的 `except` 会吃掉
> 前者** —— 必须把 `OwnerDiedError` 写在 `ObjectLostError` 之前
> （见第 5 章 §5.5 的代码示例）。只 `except ObjectLostError` 就以为
> "两者都处理了"是这里最隐蔽的错。

**owner 是谁**：创建这个 ObjectRef 的那个进程（通常是 driver）。
所以「driver 里 `ray.put` 了一个大对象，然后 driver 被 kill 掉」→
下游拿到的是 `OwnerDiedError`，而不是 `ObjectLostError`。

**怎么避免**：让**长期存活**的角色持有所有权 —— 用 **detached actor**
加载数据并对外提供句柄，而不是 driver。这是「driver 一挂，整个流水线全崩」
类故障的根因（参见第 07 章 §7.5 与第 24 章 D.4）。

#### ⚠️ `max_retries=0` 会**连带**关掉 lineage 重建

这是最容易踩的一个连带效应。Ray 的重建逻辑建立在「任务可以重放」之上，
而任务重放受 `max_retries` 控制 —— 所以：

```python
RAY_TASK_MAX_RETRIES=0     # 本意:别重试,失败就快点报错
                           # 后果:对象丢了也重建不了 → ObjectLostError
```

**什么时候会踩**：线上出问题时，有人为了「快速失败」把重试关掉止血，
结果把 lineage 重建也一起关了，于是**对象丢失从「自动恢复」变成「直接报错」**，
故障面反而扩大。关重试前先想清楚：你要关的是**应用的**重试
（那该用 `retry_exceptions=False`，本来就默认关着），还是**系统的**容错。

### 什么时候不该指望它

> **长任务（分钟级以上）必须自己写检查点。**

原因很简单：重算 30 分钟的任务是不可接受的，而检查点可以让你只损失几分钟。
这也是 Ray Train 把「checkpoint 上报与恢复」做成一等公民的原因
（见第 13 章）。

### 手工验证 lineage 重建（可运行）

```bash
cd mini-ray && python examples/06_fault_tolerance.py
```

输出会展示：模拟节点故障丢掉对象后，`ray.get` 依然返回正确值，
且 `num_reconstructions` 计数增加。**这是理解 lineage 重建最直观的方式。**

### 一个真实的 lineage 重建 bug（值得细读）

写 mini-ray 时遇到的**最隐蔽的一个 bug**，它完美示范了
"分布式系统的错误信息会把你指向完全错误的方向"：

**症状**：端到端示例的容错演练**约 2/3 概率超时**，报错是

```
reconstruct::preprocess  FAILED  err='ObjectLostError' object is not subscriptable
```

**第一反应**（错的）：以为是重建逻辑本身有问题。
但 `'ObjectLostError' object is not subscriptable` 这句话其实在说：
**任务拿到了一个"错误对象"当参数**，然后对它做了 `p["count"]`。

**真正的根因**是**两份状态没对齐**：

| | 存储里 | 账本 `_object_states` 里 |
|---|---|---|
| 丢对象**之前** | 有 | `READY` |
| 丢对象**之后** | **没了** | **还是 `READY`** ← 问题在这 |
| 重建判定的结论 | —— | "依赖还在，直接重放任务" |
| 任务实际拿到 | `ObjectLostError` 占位对象 | —— |

也就是说：删除动作只改了存储，**忘了同步账本**。
重建代码读的是账本，于是它以为依赖齐了，把任务放了出去 ——
任务拿到一个"错误对象"当输入，报出一个和真正原因毫无关系的错。

**修法是两处**：

1. **失效化**：丢对象时立刻把账本状态改成 `LOST`（存储与账本同进同退）；
2. **区分两个概念**：新增 `_is_available()`，把
   「**状态是 READY**」和「**真的能当输入用**」分开 ——
   被判定丢失的对象，状态也是 READY（这样 `ray.get` 才能拿到明确的报错），
   但它**不能**被喂给任务。

**修复效果**：修复前 6 次运行 4 次失败；修复后连续 10 次全部通过。

> **这一课比 lineage 重建本身更重要**：
> 当系统里同一个事实有**两份记录**（这里是"对象在不在"同时存在
> 存储和账本两处），它们**一定会**在某个时刻不一致。
> 好的设计要么有**唯一事实来源**，要么保证所有修改都是**原子的**。
> mini-ray 这里踩的坑，真实 Ray 在 raylet 与 GCS 之间同样会遇到 ——
> 这正是为什么真实 Ray 的注释里到处都是"这个状态必须在 X 之前更新"。

---

## 10.5 Actor 容错：状态一定会丢

```python
@ray.remote(max_restarts=3, max_task_retries=2)
class Counter:
    def __init__(self):
        self.n = 0
```

| 参数 | 默认 | 语义 |
|---|---|---|
| `max_restarts` | 0 | 崩溃后重启次数（**重启 = 新进程 + `__init__` 重跑**） |
| `max_task_retries` | 0 | 在飞的方法调用是否重试 |

**关键事实**：Ray 恢复的是「actor 这个**服务**」，不是「actor 的**状态**」。
重启后 `self.n` 回到 0。要恢复状态只有两条路：

```python
# ① __init__ 里加载检查点(简单直接,推荐)
@ray.remote(max_restarts=3)
class Trainer:
    def __init__(self, ckpt_dir):
        self.step, self.weights = load_latest(ckpt_dir)

    def train_step(self, batch):
        ...
        if self.step % 100 == 0:
            save(self.ckpt_dir, self.step, self.weights)   # 定期存
```

```python
# ② 状态外置(数据库/对象存储/另一个 actor)
@ray.remote(max_restarts=3)
class Coordinator:
    def __init__(self, store):
        self.store = store          # 状态在别处
```

⚠️ **`max_task_retries > 0` 的隐患**：方法可能执行两次。
如果方法有副作用（发通知、写外部系统），需要幂等。

---

## 10.6 GCS 容错：集群级单点

回顾第 3 章：GCS 默认**内存存储、无容错**，挂了集群就挂。两种后端：

| 后端 | 状态 | 限制 |
|---|---|---|
| 外部 Redis | 官方支持 | 官方文档明确：**仅在「用 KubeRay 做 Ray Serve 容错」时才官方支持**，其它场景风险自负 |
| 内嵌 RocksDB（`RAY_gcs_storage=rocksdb`） | **2.57 起 alpha、opt-in、仅 Linux** | 需要持久化路径；**可选的额外后端** —— 开启 GCS FT 时**外部 Redis 仍是默认**，它只是「可以不用 Redis」的选项 |

恢复期间的语义（官方文档）：

* **不可用**：actor 创建/删除/重建、放置组操作、资源管理、worker 节点注册、worker 进程创建；
* **仍然可用**：已在跑的 task/actor 继续跑，已有对象仍可读；
* raylet 与 GCS 的重连超时默认 60 秒（`RAY_gcs_rpc_server_reconnect_timeout_s`）。

**实践建议**：

1. 生产上要么用托管方案，要么按官方推荐的 KubeRay + RocksDB 路径配置；
2. **GCS 不可用期间不要创建 actor/放置组**（会失败）；
3. 把「集群元数据的备份/恢复」写进灾难恢复手册（哪些 actor 是命名/detached 的、
   哪些放置组是启动期必须的）。

---

## 10.7 节点故障、抢占与优雅排水

在 K8s 环境下，「节点消失」是常态（抢占式实例、滚动升级、缩容）。要做的准备：

```bash
# 1) 给 Ray 留出排水时间(收到 SIGTERM 后先停止接新任务,再退出)
RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S=30

# 2) 优雅关闭 ray start(SIGTERM 会被处理)
ray start --block --address=...
```

⚠️ 一个真实案例值得记住：Ray 2.32→2.33 之间的一个回归导致
**RayService 零停机升级时爆发 HTTP 500** —— 原因是 bash 的 `exec` 优化让
`ray start` 成了 PID 1，SIGTERM 直达 Ray 的 handler，
在旧 proxy 还在路由的情况下拆掉了 Serve replica。
修复需要显式开启 `RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`。
**教训**：升级 Ray 版本时，一定要在预发环境演练「滚动升级期间业务是否无损」。

---

## 10.8 检查点：自己动手的正确姿势

Ray 的哲学是「**框架保证可重放，持久化交给你**」。所以检查点要自己设计：

```python
@ray.remote(max_retries=3)
def long_running_shard(shard_id, ckpt_dir):
    start = load_progress(ckpt_dir, shard_id)      # 从上次的位置继续
    for i in range(start, num_items):
        process(shard_id, i)
        if i % 1000 == 0:
            save_progress(ckpt_dir, shard_id, i)    # 原子写:先写临时文件再 rename
    return "done"
```

四条实践原则：

1. **检查点要原子写**（临时文件 + `os.replace`），否则崩溃时会留下半个文件；
2. **检查点要带版本/指纹**（代码版本、数据版本），避免恢复出「用旧代码算的中间结果」；
3. **不要放对象存储**（它是内存、会丢）—— 放共享存储（S3/NFS/PVC）；
4. **恢复路径要测**：写一个「杀掉任务再重启」的测试（第 10.9 节）。

Ray Train 把这些做成了内建能力：`ray.train.report(metrics, checkpoint=...)`
配合 `Result.checkpoint`，框架负责上传与恢复（第 13 章）。

---

## 10.9 怎么测容错：故障注入

容错能力**必须被测试**，否则你只是「相信」它有效。三种注入方式：

```python
# ① 应用级:让任务故意失败/崩溃(最常用)
@ray.remote(max_retries=2)
def flaky(marker):
    if not os.path.exists(marker):
        open(marker, "w").close()
        os._exit(1)                # 模拟 worker 崩溃
    return "recovered"

# ② 对象级:丢掉对象,验证 lineage 重建
from miniray._private import fault_injection      # mini-ray
fault_injection.lose_objects()

# 真实 Ray 里对应的是「节点故障」:可以杀掉节点上的 raylet 进程
# kubectl delete pod <ray-worker-xxx>   (KubeRay 场景)

# ③ 集群级:滚动重启 / 驱逐节点,验证业务不中断
```

**测试清单**（建议进 CI）：

- [ ] 任务抛异常 → 重试次数正确、最终异常可读
- [ ] worker 崩溃 → 换 worker 重跑成功
- [ ] 对象丢失 → 重建后值正确
- [ ] actor 崩溃 → 重启后服务可用（状态按预期重置）
- [ ] 长任务中断 → 从检查点恢复，不重复处理已完成的批次
- [ ] driver 重启 → 命名/detached actor 仍可获取

---

## 10.10 mini-ray 的实现对照

| 机制 | mini-ray 实现 | 与 Ray 的差异 |
|---|---|---|
| 任务重试 | `task_failed` → 重新入 READY 队列（结果 ID 不变） | ⚠️ **不一致**：次数都是默认 3，但 mini-ray **连应用异常也重试**，Ray **默认不重试应用异常**（要 `retry_exceptions=True`）—— 方向相反，别对照 |
| 崩溃检测 | `WorkerPool.reap()` 轮询进程 + RPC 断开 | Ray 有更细的 worker 心跳与内存监控 |
| 崩溃错误 | `WorkerCrashedError`（与 `RayTaskError` 区分） | 一致 |
| lineage 重建 | `_reconstruct_object`：递归重建依赖 + 重放任务 | 一致（含「沿用原 ID」与「无血缘则 `ObjectLostError`」） |
| actor 重启 | `max_restarts` + 重跑 `__init__` | 一致 |
| 故障注入 | `_private.fault_injection.lose_objects()` | Ray **没有面向用户的公开**故障注入 API，靠 kill 进程；但它自己内部有成套的 RPC 混沌机制（`RAY_testing_rpc_failure`、`src/ray/rpc/rpc_chaos.cc`，PR #58512 等），用在 GCS/raylet/core worker 的容错测试里 —— 是**未公开**，不是不存在 |
| GCS 容错 | 无 | Ray 有 RocksDB/Redis 后端 |
| 检查点 | 无（用户自己写） | Ray Train 有内建支持 |

**测试证据**（`tests/test_fault_tolerance.py`，6 个用例）：
任务重试次数、崩溃后恢复、lineage 重建、依赖链递归重建、
`ray.put` 对象丢失报错、多返回值任务失败时全部带错。

---

## 10.11 本章小结

* 所有容错都建立在「**任务可重放**」这个假设上 ——
  所以幂等性与确定性是用户的责任。
* 四层机制：**任务重试**（`max_retries`，默认 3，副作用要幂等）、
  **worker 崩溃恢复**（换进程重跑，`WorkerCrashedError`）、
  **对象 lineage 重建**（不存数据存算法，沿用原对象 ID）、
  **actor 重启**（恢复服务不恢复状态）。
* 有边界的部分要清楚：**生成器任务会重试，但语义是「整任务从头重放」**
  （不是断点续传，旧 attempt 的产出按 `attempt_number` 丢弃，所以要求幂等）、
  `ray.put` 的对象**没有血缘**（丢了就是丢了）、
  **长任务必须自己写检查点**、GCS 默认无容错（2.57 起有 RocksDB 后端）。
* **别把两件事搞反**：`max_retries` 管崩溃、`retry_exceptions` 管应用异常；
  且 **`RAY_TASK_MAX_RETRIES=0` 会连 lineage 重建一起关掉** ——
  对象丢了就只能报 `ObjectLostError`。这是「为了止血关了重试，
  结果容错也一起没了」的典型事故。
* 抢占/滚动升级要配置优雅排水（`RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`），
  并且**在预发环境演练**。
* 容错必须被测试：故障注入（故意崩溃、丢对象、杀节点）应该进 CI。

下一章讲可观测性与调试：State API、Dashboard、日志、指标、Timeline，
以及一份「按症状查工具」的排查手册。
