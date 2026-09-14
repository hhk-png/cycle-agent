仓库地址：https://github.com/hhk-png/cycle-agent

# 第 3 章：架构设计

> 本章目标：把第 2 章的四个抽象「落地」到进程与数据结构上。
> 读完你应该能画出 Ray 的组件图，并能逐步说出**一次 `f.remote()` 从提交到取值
> 中间到底经过了哪些进程、哪些队列、哪些 RPC**。

---

## 3.1 组件全景

Ray 的运行时由五类东西组成（官方 *Internals* 索引页里的分区：
Task Lifecycle / Streaming Generator / Autoscaler v2 / RPC Fault Tolerance /
Object Spilling / Metric Exporter / Event Exporter …）：

```
┌──────────────────────── 一个 Ray 集群 ─────────────────────────┐
│                                                                │
│  ┌───────────── Head 节点 ─────────────┐                       │
│  │  GCS(gcs_server)                    │  集群元数据的唯一权威 │
│  │   ├ 节点表 / 资源总量                │                       │
│  │   ├ Actor 表(含命名 actor)          │                       │
│  │   ├ 放置组表                         │                       │
│  │   ├ 函数表(function_id → 函数体)     │                       │
│  │   └ 对象目录(object_id → 哪些节点有) │                       │
│  │  Autoscaler(可选)                    │                       │
│  │  Dashboard / State API(可选)         │                       │
│  └──────────────────────────────────────┘                       │
│                                                                │
│  ┌──────────── 每个节点(含 head)─────────────┐                 │
│  │  Raylet(每节点一个进程)                    │                 │
│  │   ├ 本地调度器(local lease manager)        │                 │
│  │   ├ 对象管理(LocalObjectManager:溢出/恢复) │                 │
│  │   └ worker 池(按需拉起、复用、回收)        │                 │
│  │                                           │                 │
│  │  Plasma 对象存储(节点级共享内存,独立线程)  │                 │
│  │                                           │                 │
│  │  Worker 进程 × N                          │                 │
│  │   └ 每个 worker 里有一个 CoreWorker        │                 │
│  │      (提交任务、引用计数、收发对象)         │                 │
│  └───────────────────────────────────────────┘                 │
└────────────────────────────────────────────────────────────────┘

Driver(你的脚本)也持有一个 CoreWorker —— 它对集群来说就是一个「特殊 worker」。
```

四个角色，一句话分工：

| 组件 | 数量 | 职责 | 挂了会怎样 |
|---|---|---|---|
| **GCS** | 1 | 集群元数据的唯一权威 | **整个集群挂掉**（默认内存存储，无容错） |
| **Raylet** | 每节点 1 | 本地调度 + 对象管理 + worker 池 | 该节点的任务/对象全丢（其它节点继续） |
| **CoreWorker** | 每进程 1 | 提交任务、引用计数、对象收发 | 那个 worker/driver 的任务失败 |
| **Plasma 对象存储** | 每节点 1 | 存放所有不可变对象（共享内存） | 该节点的对象丢失（触发 lineage 重建） |

---

## 3.2 一次 `f.remote()` 的完整旅程

这张时序图值得逐行读 —— 它是理解 Ray 的钥匙。

```
driver 进程                    raylet(本节点)              GCS              worker 进程
───────────                    ──────────────              ───              ───────────
f.remote(x)
  │
  ├─ ① 序列化参数(pickle,ObjectRef 变占位符)
  ├─ ② 查函数表:本地有函数体吗?没有就问 GCS ──▶ 函数表查询
  │
  ├─ ③ SubmitTask(task_id, function_id, args) ─▶
  │                                  │
  ◀── ④ 返回 ObjectRef ───────────────┤        (几十微秒,不等执行)
      (提交完立刻返回,执行是异步的)  │
                                      ├─ ⑤ 建 TaskRecord:依赖对象就绪吗?
                                      │      资源够吗?放哪个节点?
                                      │      (本地调度器决策)
                                      │
                                      ├─ ⑥ 有闲着 worker → 直接派活
                                      │   没有 → 拉起一个新 worker ──────▶ spawn
                                      │                                        │
                                      ├─ ⑦ 发任务消息 ───────────────────────▶ 收任务
                                      │                                        │
                                      │                                        ├─ ⑧ 取依赖对象
                                      │                                        │  (从 plasma 零拷贝读)
                                      │                                        ├─ ⑨ 反序列化参数(填回 ref)
                                      │                                        ├─ ⑩ 执行函数
                                      │                                        ├─ ⑪ 结果写回 plasma
                                      │                                        │
                                      ◀── ⑫ TaskDone(结果对象 ID) ───────────┘
                                      │
                                      ├─ ⑬ 更新对象目录 ────────────────────▶ 对象目录
                                      ├─ ⑭ 唤醒等待者(通知 + 调度下游任务)
ray.get(ref)
  │
  ├─ ⑮ 等对象就绪(条件变量,不是轮询)
  ├─ ⑯ 从 plasma 取:同节点 → **零拷贝视图**;跨节点 → 拉取(拷贝)
  ◀── ⑰ 返回 Python 对象
```

几个容易被忽略但很关键的细节：

* **④ 返回得快是因为不等待**：提交只是「登记 + 决策」，执行是异步的。
  这是 Ray 能表达细粒度并行的根本原因 —— 如果 `f.remote()` 要等结果，
  它就和普通函数调用没区别了；
* **⑥ 拉起 worker 是 raylet 的职责**：worker 池「按需拉起、用完复用」，
  空闲太久会被回收。⚠️ **注意别用旧常量名**：`worker_idle_timeout_ms`
  是 Ray 1.x 的遗留，**在 2.58 的 `ray_config_def.h` 里已经不存在**。
  现在这套由三个常量 + `WorkerPool::TryKillingIdleWorkers()` 控制
  （raylet 侧定期执行，不是被动等超时）：
  `kill_idle_workers_interval_ms = 200`、
  `idle_worker_killing_time_threshold_ms = 1000`（默认 **1 秒**，不是 10 秒）、
  `idle_worker_killing_memory_threshold_bytes = 1 GiB`。
  也就是说「空闲时长」不是唯一条件，**内存占用是另一个门槛**；
* **⑩ 之前 worker 才知道参数是什么**：这就是 *inlined object refs* ——
  参数里的 ObjectRef 一直保持「引用」形态，直到执行前才替换成值；
* **⑪ 结果直接进 plasma**，不回传 driver。driver 只是「另一个消费者」；
* **⑯ 同节点是零拷贝**：`ray.get` 一个 1GB 数组，同节点上不会有 1GB 的拷贝。

> ⚠️ **读这张图的一个提醒**：序号大小**不代表耗时比例**。
> ①–④ 是微秒级（提交路径），⑦–⑫ 才可能是秒级（执行路径）。
> 排查「任务为什么慢」时，要区分是**卡在提交**（前段，通常是序列化太大）
> 还是**卡在执行**（后段，通常是计算或等待）—— 两者的解法完全不同。

---

## 3.3 GCS：集群的「大脑」，也是单点

GCS（Global Control Service）管的是**全集群唯一一份**的东西。官方文档原话：

> "The Global Control Service, or GCS, manages cluster-level metadata. It also provides
> a handful of cluster-level operations including **actor, placement groups and node management**."

它保存的内容：

| 表 | 内容 | 为什么必须集中 |
|---|---|---|
| 节点表 | 每个节点的地址、资源总量 | 调度要看全局视图 |
| Actor 表 | actor 的类、状态、所在节点、命名 actor | 「按名字找 actor」只能有一个答案 |
| 放置组表 | bundle 与分配结果 | 资源预留是集群级承诺 |
| 函数表 | `function_id → 函数体字节` | 每个函数只需导出一次 |
| 对象目录 | `object_id → 哪些节点上有` | 决定「去哪儿拉数据」 |

### GCS 挂了会怎样：默认没有容错

官方文档写得很直白：

> "By default, the GCS isn't fault tolerant because it **stores all data in memory**.
> If it fails, the entire Ray cluster fails."

两种容错后端：

1. **外部 Redis**（官方支持，但有限制）：文档明确说 *"GCS fault tolerance with external
   Redis is officially supported **only if you are using KubeRay for Ray serve fault
   tolerance**"*，其它场景「风险自负」。
2. **内嵌 RocksDB**（alpha、**opt-in**、Linux only，Ray 2.57 起，对应 REP-64）：
   设 `RAY_gcs_storage=rocksdb` + 持久化路径，元数据落到本地磁盘，
   **可以不用 Redis** —— 这是 2.57 那版最值得注意的基础设施变化。
   ⚠️ 但它是**可选的额外后端**，不是新的默认：**默认的 GCS 依然是内存存储、
   没有容错**；即使开启 GCS FT，**外部 Redis 仍是默认后端**，
   RocksDB 只是「换一种后端」的选项（见第 10 章 §10.6）。

恢复期间的语义（官方文档）：

* **不可用**：actor 创建/删除/重建、放置组操作、资源管理、worker 节点注册、worker 进程创建；
* **仍然可用**：已运行的 task 与 actor 继续跑，已有对象仍然可读；
* raylet 与 GCS 的重连超时由 `RAY_gcs_rpc_server_reconnect_timeout_s` 控制（默认 60 秒），
  超时后 raylet 退出、节点被判定为失败。

> **设计上的启示**：把「元数据」集中、把「数据」分散，是分布式系统里非常经典的
> 权衡。Ray 选了「元数据单点 + 数据面去中心化」：对象在节点之间直接传输，
> 不经过 GCS。代价就是上面这条 —— GCS 一挂全完。

---

## 3.4 Raylet：每个节点的「大管家」

Raylet 是每节点一个的 C++ 进程，做三件事：

### ① 本地调度

调度是**两层**的（和 YARN/Mesos 的经典结构一致）：

* **GCS 层**：维护全局资源视图，把「资源」以 **lease（租约）** 的形式授予各节点的
  raylet。C++ 侧对应 `cluster_lease_manager`；
* **Raylet 层**：拿到 lease 之后决定「这个任务交给哪个 worker」，对应
  `local_lease_manager` + 一组调度策略。

策略实现（源码目录 `src/ray/raylet/scheduling/policy/`）包括：
`hybrid`（默认）、`spread`、`random`、`node_affinity`、`node_label`、
`bundle`（放置组）、`affinity_with_bundle`、`topology_bundle_scheduling_policy`
（GPU domain / NVLink 域感知）。
⚠️ **上面这些是 C++ 侧的「策略类名」**（`src/ray/raylet/scheduling/policy/`
目录下就是这些 `.cc/.h` 文件），**不等于用户写的 API 名** ——
Python 侧对应的公开参数叫 **`topology_strategy`**（`PlacementGroup` 的形参）。
两者的版本也不一样：**公开 API 自 2.57、C++ 独立策略自 2.58**
（跨版本检索证据见第 1 章 §1.4 的注①；本书早先写的「2.56 起」已核实为错）。
几个关键常量：

| 常量 | 默认值 | 含义 |
|---|---|---|
| `scheduler_spread_threshold` | 0.5 | 低于这个利用率就「往上堆」，否则摊开 |
| `scheduler_top_k_fraction` | 0.2 | 从前 k% 的节点里随机选，避免惊群 |
| `num_workers_soft_limit` | -1（自动 = CPU 数） | worker 池上限 |
| `max_pending_lease_requests_per_scheduling_category` | -1（不限制） | 待处理 lease 上限 |

**为什么用 lease 而不是直接指派？** 因为 raylet 可以**拒绝** lease（资源被本地
更高优先级的任务占走了、或者正在溢出对象腾不出内存），GCS 再换一个节点试。
这是「乐观调度 + 冲突重试」的思路。

### ② 对象管理

节点上的对象由 raylet 的 `LocalObjectManager` 管：

```
对象的生命周期(官方文档的四个阶段):
    Creation   ── Create RPC ──▶ 分配共享内存
    Pinning    ── PinObjectIDs RPC ──▶ 有零拷贝引用时钉住,不能被驱逐
    Consumption ── 零拷贝读共享内存
    Deletion   ── unpin ──▶ 引用计数归零后释放
```

溢出（spilling）是**三层结构**（官方文档明确写出）：

| 层 | 组件 | 干什么 |
|---|---|---|
| 检测 | Plasma 存储线程的 `CreateRequestQueue` | 发现「要 OOM 了」 |
| 编排 | Raylet 主线程的 `LocalObjectManager` | LRU 选牺牲者、pin 判断 |
| 执行 | Python IO worker 进程池 | 真正写盘（默认 4 个 worker，`max_io_workers`） |

关键参数（源码 `ray_config_def.h`）：`automatic_object_spilling_enabled=true`、
`object_spilling_threshold=0.8`（用到 80% 就开始溢）、`min_spilling_size=100MB`
（小对象先攒一攒再写）。注意官方那句强调：**"The Plasma store and the Raylet main
event loop run in separate threads"** —— 溢出不阻塞调度。

### ③ Worker 池

worker 是**按需拉起的 OS 进程**，不是线程也不是协程。原因：

* 任务可能崩溃（段错误、OOM），进程隔离是硬要求；
* 任务可能改全局状态（`os.environ`、C 库的全局变量），线程共享这些；
* 任务可能要求独占 GPU，而 GPU 上下文是进程级的。

池子的行为：空闲时保留、任务来了复用、空闲太久回收
（**Ray 默认 1 秒**：`idle_worker_killing_time_threshold_ms=1000`，
配合 `kill_idle_workers_interval_ms=200` 的扫描周期 —— 见 §3.2）。
上限由 `num_workers_soft_limit` 决定（默认等于节点 CPU 数）——
**注意 0 CPU 的任务不受此限制以外的任何约束**，所以它们能跑满 worker 池。

---

## 3.5 CoreWorker：每个进程里的「Ray 客户端」

driver 和每个 worker 进程里都有一个 CoreWorker，它负责：

1. **提交任务**：序列化参数（ObjectRef 变占位符）、导出函数（首次）、发 RPC；
2. **引用计数**：本进程持有多少个 ObjectRef，批量上报给 raylet（**不是**每个对象
   一次 RPC）—— 这就是「句柄活着，对象不被回收」的实现；
3. **对象收发**：`ray.put`/`ray.get` 的实际执行者；
4. **actor 调用**：把 `handle.method.remote()` 变成发往 actor 邮箱的消息。

> **一个常被误解的点**：driver 不是「特殊进程」，它只是**碰巧也持有 CoreWorker 的
> 普通进程**。所以你可以把「数据集」`ray.put` 之后，从任意 worker 零拷贝地读它 ——
> driver 只是第一个消费者而已。

---

## 3.6 对象存储（Plasma）：性能的地基

Plasma 是**节点级的共享内存池**，所有对象（任务返回值、`ray.put` 的值）都放这里。

| 项 | 默认值 | 说明 |
|---|---|---|
| 容量 | 可用内存的 **30%**，上限 200GB | `DEFAULT_OBJECT_STORE_MEMORY_PROPORTION = 0.3`、`DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES = 200 * 10**9` |
| 最小值 | 75 MiB | `OBJECT_STORE_MINIMUM_MEMORY_BYTES = 75 * 1024 * 1024`（`ray_constants.py`） |
| 位置（Linux） | `/dev/shm`（内存） | —— |
| 位置（macOS） | `/tmp`（**磁盘**），且容量被压到 2GB | `MAC_DEGRADED_PERF_MMAP_SIZE_LIMIT = 2 * 2**30`：Mac 上对象存储超过 2GB 性能明显退化，官方**主动设上限**（issue #20388） |
| 慢存储保护 | 阈值 10GB | `REQUIRE_SHM_SIZE_THRESHOLD = 10**10`：**当对象存储容量**超过 10GB 而 `/dev/shm` 不够时，Ray 默认拒绝启动以避免疯狂 swap；强行放开设 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE=1` |

> ⚠️ **两个常见误读**（都已在第 7 章展开，此处点到为止）：
> ① `10GB` 是**共享内存校验阈值**，**不是单对象上限** —— Ray 没有"单个对象不能超过 10GB"这条规则；
> ② 能生效的变量名是 `RAY_OBJECT_STORE_ALLOW_SLOW_STORAGE`（`services.py` 里读的就是它）。
> `RAY_ALLOW_SLOW_STORAGE` 这个短名**出现在 `ray_constants.py:119` 的注释里**，
> 但那行注释是陈旧的 —— 照着它设不会生效。详见 §7.2。

为什么这个设计值得单独记一笔：

* **零拷贝**：同节点上所有消费者拿到的是同一块内存的视图（`ray.get` 一个 numpy
  数组不产生拷贝）；
* **不可变**：所以可以安全共享，代价是拿到只读数组；
* **可溢出**：内存不够写盘而不是丢数据；
* **引用计数**：对象在「所有引用消失」后回收。⚠️ 但**不是瞬时**：CoreWorker
  的「释放对象」是**批量上报**的（`free_objects_period_milliseconds` 默认 **1000 ms**、
  `free_objects_batch_size` 默认 **100**），所以「引用归零」到「内存真正释放」
  之间隔着一个 flush 周期。

  > 📌 **两个易错点**（本书第四轮在这里写错过，第五轮对着 2.58 源码改正）：
  > ① 配置名是 **`free_objects_period_milliseconds`**，不是 `..._period_ms`
  > —— Ray 的配置名就是环境变量名，写错会**静默无效**；
  > ② 批次大小默认是 **100**，不是 10000（`ray_config_def.h` 的
  > `RAY_CONFIG(size_t, free_objects_batch_size, 100)`）。

  **别把「flush 周期短」读成「回收更激进」**：mini-ray 的上报间隔确实更短
  （0.05 s，见 §6.5、第 7 章 §7.5），但它**只在内存压力下才真正回收**；
  真实 Ray 是引用归零后经批量上报**主动**释放。两者方向相反，
  详见第 7 章 §7.9 的对照表。

---

## 3.7 对象传输：数据面

节点之间传对象这件事，Ray 有几个演进阶段：

| 机制 | 引入版本 | 说明 |
|---|---|---|
| ObjectManager（pull） | 一直都有 | 按需把远程对象拉进本地 plasma |
| 多 gRPC 连接 | 2.55 默认开启 | 提升大对象吞吐 |
| **RDT（Ray Direct Transport）** | 2.48 引入 API、**2.50 以 alpha 公布**（⚠️ 版本号口径见第 1 章 §1.4 注③） | 让张量**留在 GPU 显存**里直到必须传输；可用 Gloo/NCCL/NIXL（RDMA） |
| RDMA | Ray Summit 2025 宣布 | 面向高速网络 |

RDT 的用法（alpha，**只支持 actor 任务返回的 `torch.Tensor`**）：

```python
@ray.remote
class Model:
    @ray.method(tensor_transport="nccl")
    def forward(self, batch):
        return self.net(batch)      # 返回值直接走 NCCL,不落 CPU
```

注意它与 **Compiled Graph**（下面）是两件事：RDT 解决「怎么传」，
Compiled Graph 解决「怎么少传控制消息」。

---

## 3.8 一个针对「小任务开销」的专门优化：Compiled Graph

Ray 的普通任务派发有约 **1ms** 级的固定开销（控制面 RPC + 调度 + 消息）。
对于「每个任务只算几十微秒」的负载（比如 RL 的 rollout、小算子流水线），
这个开销会成为主要成本。

**Ray Compiled Graph**（底层 DAG API `.bind()` 自 **Ray 2.0** 就有；
`experimental_compile()` 自 **2.32** 前后引入、**2.44 进入 beta**）的思路是：
把任务图**编译**成一条固定流水线，控制消息只在开始时协商一次，之后按
「aio 提交 + NCCL 通道」直接推数据。官方口径是重复执行同一张图时
**系统开销 < 50µs**。

```python
import ray
from ray.dag import InputNode

@ray.remote
class Worker:
    def recv(self, x):
        return x * 2

w = Worker.remote()
with InputNode() as inp:
    graph = w.recv.bind(inp)
    compiled = graph.experimental_compile()
    ref = compiled.execute(1)     # 之后重复 execute 都很便宜
```

> ⚠️ 但要注意一个 2026 年的重要信号：**vLLM 正在弃用基于 compiled graph 的
> Ray executor**（vLLM RFC #35848，由 Ray 团队提交），改用继承 `MultiprocExecutor`
> 的 `RayExecutorV2`：控制面走共享内存消息队列、数据面走 `torch.distributed`/NCCL，
> **热路径上没有 `ray.remote` 调用**。也就是说：compiled graph 的方向是对的，
> 但「用 Ray 做推理引擎的数据面」这件事，业界正在退回到专用通信库。

**这一节只是概览 —— 完整的一章在第 26 章**：`InputNode`/`MultiOutputNode` 的用法、
三个硬约束（静态图、不能图内 `ray.get`、必须 `bind()`）、
通道（channel）如何取代对象存储做数据面、多 GPU collective、
`RayChannelError` 的排查、以及与普通 `ray.remote` 的取舍规则。
> 这条线索（Ray 在推理栈里收缩为放置层）在第 19、20 章会继续展开。

---

## 3.9 与 mini-ray 的逐项映射

mini-ray（第 6 章的配套实现）把这套架构压进了**一个 driver 进程**，
但组件边界保持一致：

| 真实 Ray | mini-ray | 差异 |
|---|---|---|
| gcs_server（独立 C++ 进程） | `gcs.py`（driver 进程里的 RPC 服务线程） | 元数据表结构一致；无 RocksDB/Redis 容错 |
| raylet（每节点一个 C++ 进程） | `raylet.py`（driver 进程里的两个线程：RPC 服务 + 调度循环） | `num_nodes` 是模拟参数，用来观察调度语义 |
| CoreWorker（C++） | `core_worker.py`（Python） | 职责一致：提交/引用计数/收发 |
| Plasma（C++ 共享内存） | `object_store.py`（`multiprocessing.shared_memory` + 分桶分配器） | 实现了零拷贝/pin/溢出/LRU；无 mmap 读回 |
| ObjectManager（pull） | `_pull_from_other_node` | 同节点零拷贝，跨节点一次拷贝 |
| LocalObjectManager（溢出） | `ObjectStore._spill_locked` | 同步溢出，无独立 IO worker 池 |
| 调度策略（hybrid 等） | `scheduler.py` | 单策略：本地性优先 → 最空闲 |
| Compiled Graph / RDT | 未实现 | 见**第 26 章**（专章）与 §20.5 的路线图 |

---

## 3.10 本章小结

* Ray 的运行时 = **GCS（元数据，单点）+ Raylet（每节点，调度与对象）+ CoreWorker
  （每进程，客户端）+ Plasma（每节点，共享内存对象存储）**。
* 一次 `f.remote()` 的路径：序列化参数 → 提交给本地 raylet → 本地调度决策 →
  派给 worker 或拉起 worker → worker 取依赖（零拷贝）→ 执行 → 结果进 plasma →
  唤醒等待者。**driver 不参与数据搬运**。
* GCS 默认**没有容错**（内存存储，挂了集群就挂）；开启 GCS FT 时**外部 Redis
  仍是默认后端**，2.57 起另有可选的**内嵌 RocksDB** 后端（alpha、opt-in、仅 Linux）。
* 调度是**两层 + 租约**：GCS 发 lease、raylet 决定具体 worker，
  策略有 hybrid/spread/node_affinity/bundle/topology 等。
* 对象存储是性能地基：30% 内存上限、零拷贝、只读、可溢出、引用计数回收；
  溢出是「检测/编排/执行」三层，与调度线程解耦。
* 针对小任务开销有专门的 Compiled Graph（<50µs 级），但 vLLM 的 RFC #35848 表明
  「推理数据面」正在回到 `torch.distributed`/NCCL。

下一章开始动手：装 Ray、起集群、把第一个程序跑起来，并给出「从单机脚本迁移到
Ray」的改写套路。
