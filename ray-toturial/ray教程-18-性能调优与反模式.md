仓库地址：https://github.com/hhk-png/cycle-agent

# 第 18 章：性能调优与反模式

> 本章目标：把「Ray 跑得慢」这件事**变成可测量、可归因、可修**的工程问题。
> 读完你应该能做到三件事：**知道开销花在哪四类地方**、**认出十个最常见的反模式**、
> **用 State API 自己测出优化前后的差别**。
>
> 前提：你已经写过真实能跑的 Ray 代码（第 4、5、9 章），并且知道 ObjectRef 是什么。

---

## 18.1 先量化，再优化：Ray 的四个开销来源

分布式的性能问题之所以难调，是因为**开销的来源是分层的**，而每一层的量级差了两个数量级
以上。先看这张表，它决定了你该往哪里看：

| 开销来源 | 量级（官方口径/可得证据） | 什么时候成为主瓶颈 | 观测手段 |
|---|---|---|---|
| 任务提交与调度延迟 | Ray Core 单任务调用 **约 1ms**；compiled graph **< 50µs** | 单任务计算 < 1ms 时 | `state.list_tasks()` 的 `duration` vs 提交间隔 |
| 序列化（cloudpickle） | 与对象大小、图结构线性相关，**无官方统一数字** | 参数里塞了大对象/大闭包 | 任务启动前的墙钟差、`task_events` 里的时间戳 |
| 对象传输 | 同节点共享内存**零拷贝**；跨节点一次网络拷贝 | 跨节点读大对象、GPU 直传缺失 | `state.list_objects()` 的 `node_id` 与大小 |
| worker 启动 | Python 进程 + import，**未确认具体数字**（与 import 量强相关） | 任务极短且不复用 worker | worker 池视图、进程数随时间变化 |

前两行的数字来自 Ray 官方文档的 Compiled Graph 页面
（`ray-core/compiled-graph/ray-compiled-graph`）：文档给出的对照是，经典 Ray Core API
调用一次远程方法的系统开销约 1ms，而 compiled graph 的 `graph.execute()` 路径
**低于 50µs**（Anyscale 博客 *Announcing Compiled Graphs* 的表述是 1–2ms 对约 50µs）。
**这是官方口径，不是本书实测**；你自己的机器上会不一样。

### 一个用来做心算的模型

```
总耗时 ≈ 提交开销 × 任务个数
       + 序列化开销 × 传给每个任务的参数字节数
       + 传输开销 × 跨节点搬运的字节数
       + 启动开销 × 冷启动的 worker 个数
       + 真正干活的 CPU/GPU 时间
```

这个模型最重要的推论是：**前三项与"任务个数"成正比，最后一项与"真正的工作量"成正比。**
当你把一个大任务切成 N 个小任务时，最后一项不变，前三项乘以 N。
所以「切分粒度」是调优的第一个旋钮，而它有一个明确的最优区间。

```
吞吐
  ▲
  │        ┌───────────────┐
  │       ╱                 ╲
  │      ╱                   ╲   ← 切太细：调度/序列化开销吃掉收益
  │     ╱                     ╲
  │    ╱   ← 切太粗：并行度不够，机器闲着
  │   ╱
  └───┴───────┴───────────────┴────────▶ 每任务计算量
       <1ms        ~10ms-1s        >1min
       (亏)        (甜点区)        (考虑再切)
```

**经验规则**：每个任务的**纯计算时间**在 10ms 到 1s 之间时，Ray 的开销占比通常是可接受的；
低于 1ms 就要认真考虑批量化或 compiled graph，高于几十秒则要考虑任务内部再做并行
（否则一个节点挂了就损失几十秒的计算）。

---

## 18.2 反模式目录

Ray 官方文档有一组 anti-pattern 页面（`ray-core/patterns/anti-patterns`，以及各条
`ray-core/patterns/*` 的 pattern 页）。本节按**症状 → 原因 → 正确做法**重写它们，
并补上几本实战中高频、但文档里没单独成页的坑。

### 反模式 1：任务太小（每任务 < 1ms 的计算）

**症状**：把 100 万个元素拆成 100 万个任务，跑完比单进程还慢；CPU 利用率上不去，
但 `state.list_tasks()` 里任务个数是百万级。

**原因**：每个任务要付约 1ms 的提交/调度成本（见 18.1）。100 万个任务 =
约 1000 秒的纯调度开销，而这些开销分布在 driver 与 raylet 上，很多时候是**串行**的。

**正确做法**：

1. **批量化**：一次处理一批而不是一个元素。这是 Ray Data `map_batches` 的核心理念
   （`map_batches` 给你的是一个 batch，而不是一行）。
2. 需要更细粒度、又确实是"反复执行同一张图"的场景（LLM 推理、RL rollout），
   用 **compiled graph** 把调度开销降到 <50µs。

```python
# 反模式:一百万个 1 元素任务
refs = [process.remote(x) for x in items]        # 100 万次提交

# 正确:切成 1000 个批次,每批 1000 个元素
def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

refs = [process_batch.remote(batch) for batch in chunks(items, 1000)]
```

### 反模式 2：在任务里传大对象，而不用 ObjectRef

**症状**：提交任务本身就很慢；`ray.put` 一次之后再用却还是慢；集群网络流量异常高。

**原因**：`f.remote(big_array)` 中，`big_array` 会被**按值序列化进任务描述**。
虽然 Ray 对 numpy 数组有特殊处理（见 18.5），但任务描述会随任务一起被复制到
**每个** worker，且不享受对象存储的引用计数、溢出、本地性优化。

**正确做法**：`ray.put` 一次，传 ref。

```python
import numpy as np, ray

big = np.zeros((10_000, 10_000))          # 约 800MB

# 反模式:每个任务都把 800MB 序列化一遍
refs = [f.remote(big, i) for i in range(100)]

# 正确:放一次,之后传的都是 8 字节级的 ObjectRef
big_ref = ray.put(big)
refs = [f.remote(big_ref, i) for i in range(100)]
```

**判定标准**：如果你在提交任务时**手动序列化出来的字节数 × 任务数**超过了
数据本身的规模，就该用 `ray.put`。参数超过几 MB 就该考虑。

### 反模式 3：在 driver 里循环 `ray.get` 单个 ref（把并行吃成串行）

**症状**：任务明明都在并行跑（state 里 `RUNNING` 一大堆），但 driver 的墙钟时间
差不多等于每个任务耗时之和。

**原因**：`ray.get` 是阻塞的。循环里逐个 `ray.get` 会让 driver 变成串行执行器，
更糟的是——**它还会改变调度行为**：当 driver 阻塞在 `ray.get(ref_i)` 时，
后续任务的依赖关系无法被及时推进。

```python
# 反模式:并行提交、串行收
refs = [f.remote(i) for i in range(1000)]
for ref in refs:
    results.append(ray.get(ref))          # 第一个慢,后面全等着

# 正确:一次收完
results = ray.get(refs)

# 更好:需要边出结果边处理时,用 ray.wait 做流式
pending = list(refs)
while pending:
    ready, pending = ray.wait(pending, num_returns=1)
    results.append(ray.get(ready[0]))
```

### 反模式 4：闭包里捕获巨大对象

**症状**：代码看起来"只是引用了一个外部变量"，但每个任务的内存占用都多出几百 MB。

**原因**：`cloudpickle` 会把闭包里捕获的对象**按值**序列化进任务。也就是说：

```python
# 反模式:每个任务都被塞了一份 lookup_table
lookup_table = load_huge_table()      # 500MB

@ray.remote
def lookup(key):
    return lookup_table[key]           # 闭包捕获 → 每个 worker 一份 500MB
```

Ray 官方把它单独列为一条反模式（`ray-core/patterns/closure-capture-large-objects`）。

**正确做法**：

- 用 `ray.put` 把大对象放进对象存储，任务参数传 ref；
- 或者把它放到 **actor** 里当成员变量（见反模式 7），让一份副本长期驻留；
- 或者干脆在 worker 内部按需加载（配合 `runtime_env` 的 `working_dir`）。

**怎么发现**：`ray memory` 会把 `CAPTURED_IN_OBJECT` 这类引用类型列出来，
这就是"闭包/嵌套对象里夹带的 ObjectRef"的指纹。

### 反模式 5：在 actor 方法里 `ray.get` 另一个 actor

**症状**：跑着跑着全卡住，CPU 掉到 0，没有任何报错，超时后才各种失败。

**原因**：这是分布式系统里的经典死锁。actor A 的方法阻塞在等 actor B 的结果，
而 actor B 的方法又阻塞在等 A——两边的邮箱都被"正在执行的那个调用"占着，
谁都不肯让出执行权。默认并发度为 1 的同步 actor 尤其容易踩。

```
actor A (并发度 1)                actor B (并发度 1)
  method_a()  ── ray.get ──▶  B.method_b()
                               B 里 ── ray.get ──▶ A.method_c()
  A 的邮箱被 method_a 占着 ◀──── 永远排队,永远等不到
```

**正确做法**：

1. 改成**数据流**：把依赖表达成 ObjectRef 的传递，让调度器去满足依赖，
   而不是在 actor 内部同步等待；
2. 确实需要互调时，把等待放到 driver 侧；
3. 或者给 actor 设置 `max_concurrency > 1`（async actor 默认并发度是 1000，
   sync actor 是 1）——这能缓解，但**不能消除**循环依赖，只是把死锁变成死锁得更慢。

### 反模式 6：无界提交（对象存储爆 / OOM）

**症状**：提交 100 万个任务后进程被 OOM killer 干掉；
或者 `ObjectStoreFullError`；或者 driver 内存持续上涨。

**原因**：每次 `f.remote(...)` 都会在 driver 侧留下一个 ObjectRef，
每个任务还要在 raylet 的队列里留一条记录。任务产生的结果对象会堆在对象存储里等
driver 来取。**提交速度超过消费速度 = 无界队列 = 迟早爆**。

**正确做法**：`ray.wait` 背压。这是 Ray 官方文档里的标准模式
（`ray-core/patterns/limit-pending-tasks`，doc code 在
`doc/source/ray-core/doc_code/limit_pending_tasks.py`）：

```python
import ray

MAX_NUM_PENDING_TASKS = 100          # 在飞任务上限
result_refs = []

for i in range(NUM_TASKS):
    if len(result_refs) > MAX_NUM_PENDING_TASKS:
        # 至少回收一个完成的,保持队列有界
        ready_refs, result_refs = ray.wait(result_refs, num_returns=1)
        ray.get(ready_refs)
    result_refs.append(worker.heavy_compute.remote(i))

ray.get(result_refs)
```

**两个必须说清楚的细节**：

* **不要写 `RAY_max_pending_tasks`**。这个环境变量在 Ray 2.58 里**不存在**，
  本书在 2.58 的配置清单里也没有找到它（很多中文博客里的这个名字来自很老的版本）。
  官方给的方案是**应用层背压**，不是某个配置项。
* 官方文档明确提醒：这个模式的本意是**限制在飞任务数**，而不是"限制并发度"。
  想限制并发度，应该改每个任务的资源声明（比如把 `num_cpus` 从 1 改成 2），
  让调度器自己去算——用 `ray.wait` 限并发会损害调度性能。

### 反模式 7：每个任务都重新加载模型

**症状**：每个任务里 `from_pretrained(...)` 一次，GPU 利用率极低，
90% 时间花在加载权重上。

**原因**：任务（task）是**无状态、用完即弃**的执行单元。每个任务在自己的 worker
进程里跑，worker 可能被回收、可能换一个，模型只能重新加载。
一个 7B 模型加载一次是秒级到十几秒级，乘以任务数就是灾难。

**正确做法**：

1. **actor + 常驻模型**：把模型放在 actor 的 `__init__` 里，方法只做前向：

```python
@ray.remote(num_gpus=1)
class Inferencer:
    def __init__(self, model_path):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(model_path)
        self.model = AutoModelForCausalLM.from_pretrained(model_path, device_map="cuda")

    def generate(self, prompts):
        ...                              # 只做前向,不再加载

# 每张卡一个常驻 actor
engines = [Inferencer.remote(path) for _ in range(num_gpus)]
```

2. **`ray.util.ActorPool`**：一组 actor + 自动背压的批处理，是"同构 actor 池"的标准工具。
   mini-ray 里实现的是同一个 API（`miniray/util/actor_pool.py`），关键点也是背压：
   每个 actor 手里最多一个任务，拿到结果再补下一个。

```python
from ray.util.actor_pool import ActorPool

pool = ActorPool(engines)
for out in pool.map_unordered(lambda a, p: a.generate.remote(p), prompts):
    ...                                   # 谁先算完先返回谁
```

### 反模式 8：需要状态却用任务（拿 `ray.put` 传状态）

**症状**：每轮迭代都要把整个状态 `ray.put` 一遍再传给下一个任务；
状态越大越慢，且每轮之间无法并行。

**原因**：任务是无状态的。用「任务 + 把状态当参数传」来模拟有状态计算，
等价于每轮做一次「全量序列化 + 全量传输」。这正是 MapReduce 式框架在迭代算法上的
老问题，也是 Ray 论文当年批评的对象。

**正确做法**：用 actor。状态留在 actor 的 Python 对象里，方法只传增量。

```python
# 反模式:状态在数据面来回搬
@ray.remote
def step(state, batch):
    return update(state, batch)

state = init()
for batch in batches:
    state = ray.get(step.remote(state, batch))     # 每轮全量复制状态

# 正确:状态在 actor 里,只搬增量
@ray.remote
class Trainer:
    def __init__(self):
        self.state = init()
    def step(self, batch):
        self.state = update(self.state, batch)     # 原地更新
        return summarize(self.state)

trainer = Trainer.remote()
for batch in batches:
    ray.get(trainer.step.remote(batch))
```

参数服务器（第 09 章 §9.7 模式一，mini-ray 侧对应 `examples/10_parameter_server.py`）、
RL 的 rollout worker、仿真的环境池，都是这个模式。

### 反模式 9：把 actor 当无状态函数用

**症状**：起了 1000 个 actor，每个只处理一个请求；集群资源被占满但吞吐没变；
`ray status` 里 GPU 全被"预留"但利用率很低。

**原因**：**actor 的资源是终身持有的**——从创建到销毁一直占着它声明的 CPU/GPU，
哪怕它 99% 的时间在邮箱里等消息。它的定位是"常驻的有状态组件"，
不是"能并发的函数"。把无状态函数写成 actor，等于用最贵的方式做最便宜的事。

**正确做法**：

- 无状态并行 → 用 **task**（`@ray.remote` 装饰函数）；
- 需要固定并发上限的 IO 密集场景 → 用 task + `num_cpus` 声明（甚至 `num_cpus=0.1`）；
- 确实需要常驻（模型、连接、缓存）→ 才用 actor，并且**数量要按资源算**：
  N 张卡就起 N 个 actor，而不是 N×10 个。

**顺便记住一个默认值**：actor 在**调度**时默认占 1 个逻辑 CPU，但在**运行时**
默认占 0 个 CPU。这是历史原因造成的（官方文档建议总是显式给 actor 写 `num_cpus`）。
推论是：如果你不写资源声明，可以起近乎无限个 actor，直到系统崩掉。

### 反模式 10：命名 actor 泄漏，忘了 `ray.kill`

**症状**：任务跑完了，集群里的 actor 还在；第二次跑同一个脚本时
`ValueError: The actor with name=X already exists`；GPU 一直被占着。

**原因**：`lifetime="detached"` 的命名 actor 会**活过 driver**。这是它的设计目的
（跨 job 复用），也是它的代价：没人回收它。普通 actor 会随 driver 退出而销毁，
但 detached actor 不会。

**正确做法**：

```python
# 1) 用完显式杀
ray.kill(handle)                       # 默认 no_restart=True

# 2) 用 get_actor 做幂等获取,避免重复创建
try:
    handle = ray.get_actor("serving")
except ValueError:
    handle = Serving.options(name="serving", lifetime="detached").remote()

# 3) 定期盘点
from ray.util.state import list_actors
for a in list_actors(filters=[("state", "=", "ALIVE")]):
    print(a.name, a.actor_id, a.num_restarts)
```

**mini-ray 的对应实现**（签名已对源码核对）：
`miniray.get_actor(name, namespace=None)`、
`miniray.kill(actor, *, no_restart=True)` 与 Ray 对齐；
⚠️ 但 **`miniray.list_actors()` 不接受任何参数** —— 它没有 Ray 那边的
`filters=[...]`（docstring 只说"对齐 `ray.util.list_actors` 的返回形状"）。
所以上面那段 `list_actors(filters=[("state", "=", "ALIVE")])` 是**真 Ray 的写法**，
搬到 mini-ray 上会 `TypeError`。
另外 `detached` actor 只在**同一进程生命周期内**有效（因为 mini-ray 没有独立的
raylet 进程，见 `mini-ray/README.md` 的「已知取舍」第 5 条）。

---

## 18.3 内存与 OOM

Ray 里"内存"至少有四个不同的池子，混着看必然调错：

| 池子 | 里面是什么 | 用完了会怎样 | 看哪里 |
|---|---|---|---|
| 对象存储（共享内存） | `ray.put` 的对象、任务返回值 | 溢出到磁盘；再不行 `ObjectStoreFullError` | `ray memory`、`state.list_objects()` |
| worker 堆内存 | 你代码里的 Python 对象 | Ray 的 memory monitor 杀 worker → `OutOfMemoryError` | dashboard Memory 视图、`htop` |
| driver 堆内存 | `ray.get` 反序列化出来的副本 | driver 被 OOM killer 杀 | 同上 |
| GPU 显存 | 模型权重、KV cache | CUDA OOM | `nvidia-smi`；**Ray 不管这一层** —— 四种 OOM 的成因与排查见**第 30 章 §30.5** |

### 对象存储的容量

* 默认占**可用内存的 30%**，上限 **200GB**，由常量
  `DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES` 设定（`ray_constants.py`）。
  要突破 200GB，需要在 **Python 进程启动前**设置
  `RAY_DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES`（它是 import 时读的）。
* 环境变量名就是这个长的；网上常见的 `DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES`
  （去掉 `RAY_` 前缀）是错的。
* **最小值 75MiB** —— 这条**是确定的**（本书早期版本曾标为"未确认"，现已核实）：
  `ray/_private/ray_constants.py` 里有
  `OBJECT_STORE_MINIMUM_MEMORY_BYTES = 75 * 1024 * 1024`，
  注释写明"所允许的对象存储内存下限，必须大于 `MEMORY_RESOURCE_UNIT_BYTES`"。
  显式 `object_store_memory=` 一个更小的值会撞到这个下限。
* **Linux** 上对象存储落在 `/dev/shm`（共享内存）；**macOS 上没有共享内存语义，
  落到 `/tmp`（磁盘）**，性能明显更差。这是「为什么 Mac 上跑同样的代码更慢」的
  最常见答案。而且 Mac 上不只是"慢一点"：常量
  `MAC_DEGRADED_PERF_MMAP_SIZE_LIMIT = 2 * 2**30` 把容量**主动压到 2GB**
  （超过 2GB 性能退化，见 issue #20388），所以 Mac 上的默认容量远小于"内存的 30%"。
* 当配置的对象存储容量**超过 `/dev/shm` 的实际大小**时，Ray 会直接报错：

```
ValueError: The configured object store size (10.24 GB) exceeds /dev/shm size (8.0 GB).
This will harm performance. Consider deleting files in /dev/shm or increasing its
size with --shm-size in Docker. To ignore this warning, set
RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1.
```

  能生效的变量名是 **`RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE`**，**不是**
  `RAY_ALLOW_SLOW_STORAGE`。⚠️ 但说"短名查无依据"**不准确**：短名确实出现在
  `ray_constants.py` 紧跟 `REQUIRE_SHM_SIZE_THRESHOLD` 的注释里，
  只是那行注释陈旧、`services.py` 读的是长名。**用长名。** Docker/K8s 里对应的是
  `--shm-size` / `shm` 卷大小。
* **关于"超过约 10GB 的对象默认报错"**：不存在"单对象 10GB 上限"这条规则 ——
  但 **10GB 这个数字确实存在**，它是
  `REQUIRE_SHM_SIZE_THRESHOLD = 10**10`：**当对象存储的容量**超过 10GB、
  而 `/dev/shm` 装不下时，Ray 默认**拒绝启动**（就是上面那条报错），
  除非设 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1`。
  它与"单个对象多大"无关。真正会遇到的失败通常来自 ① 对象存储本身太小或塞满了
  pinned 对象（`ObjectStoreFullError`，报错文本里会给出对象大小并提示用 `ray memory` 排查），
  ② 序列化路径上 PyArrow/Plasma 出现过 2GB 级别的 `Maximum size exceeded (2GB)`
  报错（社区帖，未确认是否仍是 2.58 的普遍限制）。

### 对象溢出（spilling）

对象存储满了不是立刻失败，而是先把对象**溢出到磁盘**。相关开关（来源：
官方 Object Spilling 文档 `ray-core/objects/object-spilling` 与
`ray_config_def.h`）：

| 配置 | 含义 | 默认/示例 |
|---|---|---|
| `object_spilling_config` | 溢出目标的 JSON 配置 | 例如 `{"type": "filesystem", "params": {"directory_path": ["/tmp/spill"]}}`；**置空可禁用溢出** |
| `object_spilling_directory` | 只指定目录的便捷写法 | **优先级已定**（`ray_config_def.h` 的注释原文）：同时给 `object_spilling_config` 时**它胜出**；而 `ray.init()` / `ray start` 传的目录**又优先于这个配置项** |
| `automatic_object_spilling_enabled` | 是否自动溢出 | Ray 1.3+ 默认开启；有用户在 2.3/2.44 报告**关不掉**（论坛帖），需要 `RAY_object_spilling_threshold=1.0` 兜底 |
| `object_spilling_threshold` | 已用比例超过它就开始主动溢出 | ⚠️ **两个来源对不上**：官方 object-spilling 文档写 `0.95`，而本仓库源码 `src/ray/common/ray_config_def.h` 里写的是 `RAY_CONFIG(float, object_spilling_threshold, 0.8)` —— **源码是 `0.8`**。**本节不给单一数字** —— 请以你的版本上 `ray start --help` / `ray_config_def.h` 为准。**不要照抄任何二手数字** |
| `max_io_workers` | 溢出/恢复的 IO 线程数 | **默认 `4`**（`ray_config_def.h`）；远端存储建议调大 |
| `min_spilling_size` | 一次最少溢出多少字节 | **默认 `100 * 1024 * 1024`**（100MB，`ray_config_def.h`） |

这些是**系统配置**，通过 `ray.init(_system_config={...})` 或
`ray start --system-config='{...}'` 传入，也可以写成 `RAY_<name>` 环境变量
（例如 `RAY_object_spilling_threshold=<你确认过的取值>`）。

**溢出的代价**：溢出后的对象再次被 `ray.get` 时会从磁盘读回来，
延迟从微秒级变成毫秒级甚至更高。**把溢出当成安全网，不要当成容量方案。**

### 排查内存问题的正确姿势

```bash
# 1) 谁在占对象存储:按对象大小排序,看 call site
ray memory --sort-by=OBJECT_SIZE

# 2) 按调用栈聚合,找"哪一行代码泄漏"
ray memory --group-by=STACK_TRACE

# 3) 只看对象/任务/actor 的汇总(等价物是 State API)
ray summary objects
ray summary tasks
```

`ray memory` 的五个引用类型是排查的关键，它们分别代表对象被"谁"钉住：

| 引用类型 | 含义 | 典型泄漏场景 |
|---|---|---|
| `LOCAL_REFERENCE` | driver/worker 手里的 ObjectRef | 你在列表里攒了几百万个 ref |
| `PINNED_IN_MEMORY` | 反序列化出来的副本直接指向对象存储内存 | `x = ray.get(big_ref)` 之后 x 一直活着 |
| `USED_BY_PENDING_TASK` | 排队中的任务要用的对象 | 无界提交（反模式 6） |
| `CAPTURED_IN_OBJECT` | 被夹在另一个 `ray.put` 的对象里 | 闭包捕获（反模式 4） |
| 序列化的 ObjectRef 引用 | ObjectRef 被包在列表/字典里传出去 | 把 ref 塞进大结构体到处传 |

**注意**：官方文档里明确写到的是 `--sort-by` 与 `--group-by` 两个选项。
`ray memory --stats-only`（只打印对象存储的聚合统计、不逐条列对象）
**是真实存在的 flag**（本书早期版本曾标为"未确认"，现已核实：
`ray/scripts/scripts.py` 里定义为 `is_flag=True, help="Display plasma store stats only."`）。
新版 Ray 里 State API 的 `ray summary objects` 是更"结构化"的替代品。

另外两个实战技巧：

* **Ray 的 memory monitor 会先动手**。Ray 2.2+ 有应用级内存监控，
  在 Linux OOM killer 之前杀掉 worker，异常类型是
  `ray.exceptions.OutOfMemoryError`（定义在 `python/ray/exceptions.py`）。
  ⚠️ **文案在 2.55 改过，别照抄老博客去搜**（本轮核对结果）：

  | 版本 | 报错文案 |
  |---|---|
  | **2.44 / 2.51 / 2.54** | `Task was killed due to the node running low on memory.` |
  | **2.55+（含 2.58）** | `N worker(s) were killed due to the node running low on memory. <details>\n<suggestions>` |

  也就是 2.55 起**从"Task"改成了带计数的"worker(s)"**，而且后面还拼了
  kill details 与建议（`src/ray/raylet/node_manager.cc` 里
  `worker_exit_message` 那段）。**如果你按旧文案 `grep` 日志，在 2.55+ 上一行都搜不到** ——
  正确的抓法是搜 `were killed due to the node running low on memory`。
  这条消息意味着"某个 worker 的堆内存涨太快"，而不是对象存储满了。
  它同时会以 `Out of Memory` 事件推给 dashboard（`RAY_EVENT_EVERY_MS(ERROR, ...)`）。
* **`--num-cpus=0` 的 head 节点**是官方推荐的做法（KubeRay 里给 head pod 设
  `num-cpus: "0"`），目的就是别让 worker 跑到 head 上跟 GCS 抢内存。

---

## 18.4 调度与并发调优

Ray 的默认调度策略叫 **hybrid**（混合）：先按资源可用量挑出一小撮候选节点，
再在候选里**随机**选一个。三个参数决定它的行为（来源：`ray_config_def.h`，
defaults 已由官方文档 PR #65264 "Add a scheduling overview with defaults" 对源核对过）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `scheduler_spread_threshold` | `0.5` | 节点利用率**低于**这个阈值时得分为 0，于是"和别的轻载节点一样好" |
| `scheduler_top_k_fraction` | `0.2` | 候选集大小占集群节点数的比例（默认 20%） |
| `scheduler_top_k_absolute` | `1` | 候选集大小的下限 |

候选集大小 `k = max(节点数 × scheduler_top_k_fraction, scheduler_top_k_absolute)`。
**k 的意义**：k 越大越倾向于"全局最优但慢"，k 越小越倾向于"随机但快"。
超大集群（几千节点）里调小 `scheduler_top_k_fraction` 能显著降低 GCS 上的调度压力。

写成环境变量是 `RAY_scheduler_spread_threshold` / `RAY_scheduler_top_k_fraction`
这种形式（`RAY_` 前缀 + 配置名）。

### 0-CPU 任务的语义

| 声明 | 调度时占用 | 运行时占用 | 推论 |
|---|---|---|---|
| task，不给 `num_cpus` | 1 CPU | 1 CPU | 默认就是"一个任务一个核" |
| actor，不给 `num_cpus` | 1 CPU | **0 CPU** | 历史原因；可以起无限多个 actor 直到系统崩 |
| 显式 `num_cpus=0` | 0 | 0 | 适合纯 IO/等待型负载，**没有并发上限保护** |
| 显式 `num_cpus=0.1` | 0.1 | 0.1 | 支持小数，精度约 0.0001；用来做"最多 N 个并发"的闸门 |

两个必须知道的副作用：

1. **Ray 会用 `num_cpus` 设置 `OMP_NUM_THREADS`**：给了 `num_cpus=2` 意味着
   `OMP_NUM_THREADS=2`；不给则设为 1（避免多 worker 的线程争抢，见 issue #6998）。
   所以如果你的 numpy/MKL 代码莫名只用 1 个线程，就是这里。
2. **0 CPU 的任务没有真正的并发上限**。论坛里的原话是"0 CPU 的话你可以起无限个 actor
   直到把系统搞崩"。要限流就显式给小数资源，或者用 18.2 的 `ray.wait` 背压。

### worker 复用与空闲回收

让 worker 活着比反复拉起便宜得多。相关机制：

* **worker 复用**：同一个 worker 进程会被复用去跑同一类任务（同 job、同
  runtime_env、同资源声明）。这就是为什么反模式 7 里"用 actor 常驻模型"有效。
* **空闲回收**：长时间空闲的 worker 会被回收（raylet 每 200ms 巡检一次，
  时间门槛 `idle_worker_killing_time_threshold_ms` 默认 **1 秒**，另有 1 GiB
  内存门槛）。⚠️ 旧文档里的 `worker_idle_timeout_ms = 10000`（10 秒）**在 Ray 2.58
  已不存在**（Ray 1.x 遗留）。mini-ray 沿用旧名字与旧默认值
  （`miniray.init()` 的 `worker_idle_timeout_ms`，见 `miniray/runtime.py`），
  所以**别把两边的数字互相对照**。调试时调大能减少冷启动噪声，生产上保持默认。
* **worker 池上限**：`num_workers_soft_limit` 的默认是 **-1**，含义是
  "用可用的 CPU 数"（`ray_config_def.h` 的注释：这个上限只作用于**空闲** worker，
  因为实际用掉的 worker 总数取决于应用）。mini-ray 里的等价参数是
  `max_workers_per_node=0`，自动值取 `max(16, 节点CPU数 × 8)`
  （`miniray/worker_pool.py:204`）。

---

## 18.5 序列化成本：为什么"塞大对象"比"塞 ObjectRef"慢

一句话：**传 ObjectRef 传的是 8 字节的 ID，传对象传的是整个对象的字节。**
但真正的差别不止字节数，还有三件事：

| | 传对象（按值） | 传 ObjectRef |
|---|---|---|
| 提交时的序列化 | 每次提交都要序列化一遍 | 一次（`ray.put` 时） |
| 跨节点传输 | 任务描述跟着任务走，可能重复传 | 对象存储按需拉取，可复用 |
| 本地性 | 无 | 调度器可以把任务调度到对象所在节点 |
| 溢出/回收 | 不受对象存储管理 | 受引用计数 + LRU 管理 |
| 零拷贝 | 取决于类型与路径 | 同节点大 numpy 数组可零拷贝 |

**numpy 零拷贝的适用条件**（这是最容易误解的一点）：

* 数组必须是 **C 连续**的（`arr.flags['C_CONTIGUOUS']`）；切片、转置后的视图
  往往不连续，会退化成拷贝。`np.ascontiguousarray(arr)` 可以先规整。
* 只有**同节点**的消费者才能拿到共享内存视图（mini-ray 的
  `object_store.py` 就是这个语义：同节点共享内存，跨节点一次拷贝）。
* 拿到的是**只读**视图；写会触发拷贝或者报错（取决于库的版本与路径）。
  所以"我改了传进去的数组，任务里看到的是旧值"是预期行为，不是 bug——
  Ray 的对象是**不可变**的。
* 对象超过一定大小后走对象存储，小于阈值时可能走**直接调用**路径
  （`max_direct_call_object_size` 控制"多大以下可以直接塞进调用里"）。
  这条路径更快，但不进对象存储、不享本地性。

**实用判定**：

```python
import sys, numpy as np

def payload_bytes(x):
    if isinstance(x, np.ndarray):
        return x.nbytes
    return len(pickle.dumps(x))          # 粗略;只用来做量级判断

# 提交前先算一笔账
total = sum(payload_bytes(a) for a in args) * num_tasks
if total > 100 * 1024**2:               # 100MB 是个保守的警戒线
    print("考虑 ray.put 后传 ref")
```

---

## 18.6 自己动手：优化前后对比实测模板

**不要相信任何人的优化数字，包括本书的。** 下面这套模板能让你在自己的机器上
拿到可信的前后对比。它只依赖标准库 + Ray 的 State API。

```python
"""bench.py —— 优化前后对比模板。

用法:
    python bench.py before     # 跑"反模式"版本
    python bench.py after      # 跑"优化后"版本
"""
import sys
import time
import ray
from ray.util.state import list_tasks


@ray.remote
def tiny(x):
    return x + 1


@ray.remote
def batched(xs):
    return [x + 1 for x in xs]


def run_before(n):
    return ray.get([tiny.remote(i) for i in range(n)])


def run_after(n, batch=1000):
    refs = [batched.remote(list(range(i, min(i + batch, n)))) for i in range(0, n, batch)]
    out = ray.get(refs)
    return [v for chunk in out for v in chunk]


def measure(fn, *args):
    t0 = time.perf_counter()
    result = fn(*args)
    wall = time.perf_counter() - t0
    return wall, result


def task_durations():
    """从 State API 拿每个任务的执行耗时(秒)。

    这是把"墙钟时间"拆成"调度开销"和"真实计算"的关键:
    wall_time - sum(duration) 就是调度/提交/等待的总开销。
    """
    tasks = list_tasks()
    ds = [t.duration for t in tasks if getattr(t, "duration", None) is not None]
    return len(ds), sum(ds), (max(ds) if ds else 0.0)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "before"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000

    ray.init(ignore_reinit_error=True)
    try:
        # ⚠️ task_durations() 统计的是 State API 里**当前所有**任务(含历史),
        #    所以「before / after」必须**各起一个干净进程**跑（见下面的用法），
        #    否则第二次会把第一次的任务也算进去，overhead_ratio 直接失真。
        wall, _ = measure(run_before if mode == "before" else run_after, n)
        num, total, worst = task_durations()

        print(f"mode={mode} n={n}")
        print(f"wall_clock_s        = {wall:.3f}")
        print(f"tasks_in_state      = {num}")
        print(f"sum(task.duration)_s= {total:.3f}")
        print(f"max(task.duration)_s= {worst:.4f}")
        if wall > 0:
            print(f"overhead_ratio      = {1 - total / wall:.1%}")   # 调度/等待占比
            print(f"throughput_per_s    = {n / wall:.0f}")
    finally:
        ray.shutdown()
```

**为什么这样测才可信**：

1. **用 `time.perf_counter()` 而不是 `time.time()`**：前者单调，不受系统时间调整影响。
2. **一定要看 `state.list_tasks()` 的 `duration`**。只看墙钟时间你分不清"慢在哪"：
   `wall_clock - sum(duration)` 就是**调度 + 提交 + 序列化 + 等待**的总和。
   这个差值大 → 是框架开销问题；这个差值小但 `sum(duration)` 大 → 是真实计算问题，
   该优化算法而不是优化调度。
3. **跑三次取中位数**，第一次跑包含 worker 冷启动，天然偏慢。
4. **固定 worker 数量与 `num_cpus`**，否则两次跑的资源视图不同，比较没意义。
5. **用同一个进程跑 before/after 会互相污染**（worker 已预热、对象存储有残留），
   用两次独立进程跑。

实测表格模板（把它填上你自己的数字，贴进 PR 或者周报）：

| 场景 | 任务数 | 墙钟 (s) | `sum(duration)` (s) | 开销占比 | 吞吐 (任务/s) | 备注 |
|---|---|---|---|---|---|---|
| before：1 元素/任务 | 10000 | 待测 | 待测 | 待测 | 待测 | 基线 |
| after：1000 元素/批量 | 10 | 待测 | 待测 | 待测 | 待测 | 批量化 |
| after：+ compiled graph | 10 | 待测 | 待测 | 待测 | 待测 | 重复执行固定图时 |

---

## 18.7 本章小结

* Ray 的开销有四个来源：**提交/调度、序列化、对象传输、worker 启动**。
  官方口径是经典 API 单任务约 1ms、compiled graph < 50µs；其余量级需自己测。
* 优化的第一性原则：**先量化**。把墙钟时间拆成 `sum(task.duration)` 与剩余开销，
  才知道该动框架还是动算法。工具是 `time.perf_counter()` + `state.list_tasks()`。
* **十个反模式**：任务太小、传大对象不用 ref、循环 `ray.get`、闭包捕获大对象、
  actor 内互等死锁、无界提交、每任务重载模型、用任务做有状态计算、
  把 actor 当函数、命名 actor 泄漏。每一个都有明确的"症状 → 原因 → 正确做法"。
* **内存**分四个池子（对象存储 / worker 堆 / driver 堆 / 显存），不要混着调。
  对象存储默认 30% 内存、上限 200GB、Linux 走 `/dev/shm`、**macOS 走 `/tmp` 因此更慢**。
  溢出是安全网而不是容量方案。
* **调度**默认是 hybrid，`scheduler_spread_threshold=0.5`、
  `scheduler_top_k_fraction=0.2` 决定候选集；`num_workers_soft_limit=-1`
  表示按 CPU 数；**0-CPU 任务没有并发上限**。
* **传 ref 而不是传对象**，并且记住 numpy 零拷贝的三个条件：
  C 连续、同节点、只读。
* ✅ **已从"未确认"升级为"已核实"的三条**（本书早期版本标注过，现已对照源码确认）：
  **75MiB 下限**（`OBJECT_STORE_MINIMUM_MEMORY_BYTES`）、
  **`ray memory --stats-only`**（`scripts.py` 中确有此 flag）、
  **"10GB" 的真实身份**（是 `REQUIRE_SHM_SIZE_THRESHOLD` 共享内存校验阈值，
  **不是**单对象上限）。
* 仍在"未确认"状态的点：`object_spilling_directory` 与
  `object_spilling_config` 的优先级、`RAY_TLS_*` 的完整清单 ——
  请以你手头版本的 `--help` 与源码为准。

下一章换个角度：不调优，而是**选型**——同样的活儿，Spark、Dask、torchrun、
Monarch、SkyPilot 各自强在哪，什么时候不该用 Ray。
