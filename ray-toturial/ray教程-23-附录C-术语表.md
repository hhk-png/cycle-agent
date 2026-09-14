仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 C：术语表

> 本附录目标：**让"我到底在看哪个词"这件事不再消耗你的注意力**。
> 正文约 **218 条**术语，按主题分组；每条给**中文名 + 英文名 + 一句话定义 +
> 它在 Ray 里的具体含义**（后者才是重点——同一个英文词在不同框架里意思常常不同）。
> 文末附按英文字母排序的索引（**索引收 178 条**，比正文少 ——
> 正文里有些是同一概念的不同侧面，索引只收主条目）。
>
> ⚠️ **两个数字别混**：第四轮修订前这里写"160 余条"，
> 而当时正文实际已有 187 条、索引 160 条 —— **自述数与实际数不符**。
> 现在按实测写，并**把索引与正文分开计数**。
>
> 交叉引用：括号里的"第 N 章"是本教程的对应章节；"(mini)"表示这条在 mini-ray
> 里有对应实现，可以去看代码。

---

## C.1 Core 概念

**任务 / Task** —— 一次远程函数调用产生的执行单元。
在 Ray 里，`@ray.remote` 装饰的函数调用 `f.remote(x)` 就创建一个 Task；它**无状态、可重试、
可并行**，生命周期止于返回值被写进对象存储。(mini: `miniray/remote.py`)

**角色 / Actor** —— 一个有状态、常驻的远程对象。
在 Ray 里，`@ray.remote` 装饰的**类**实例化后就是 Actor；它的资源从创建到销毁
**终身持有**，方法调用进邮箱后按提交顺序执行。(mini: `miniray/actor.py`)

**对象 / Object** —— Ray 里一切跨进程传递的数据。
Ray 的对象**不可变**；它不在"谁手里"，而在对象存储里，大家通过 ObjectRef 引用它。

**对象引用 / ObjectRef** —— 指向一个未来值（或已就绪值）的句柄。
在 Ray 里它是一个小 ID，可以像普通值一样放进列表/字典、当参数传给别的任务。
`ray.get(ref)` 才把值取回来。术语上它同时表达"future"和"共享内存的地址"两种含义。

**远程函数 / Remote Function** —— 被 `@ray.remote` 包装后的函数对象。
它不能直接调用（调用会抛 `TypeError`），只能 `.remote()` / `.options()` / `.bind()`。
(mini: `RemoteFunction`)

**驱动器 / Driver** —— 提交任务、调用 `ray.get` 的那个进程（通常是你的脚本）。
Ray 里 driver 也是集群的一部分：**driver 退出，这个 job 就结束**，但它拥有的对象
会按引用计数回收。(mini 的关键差异：driver 退出 = 整个集群结束)

**工作进程 / Worker** —— 真正执行任务的进程。
Ray 里 Worker 由 raylet 按需拉起、复用、空闲回收；每个 Worker 负责一个任务或
一个 Actor 的运行时。(mini: `miniray/worker.py`)

**作业 / Job** —— 一次 driver 的完整运行。
Ray 里每个 Job 有唯一 `JobID`，命名空间、runtime_env、对象所有权都以 Job 为界。

**头节点 / Head Node** —— 集群里跑 GCS 与 dashboard 的那个节点。
Ray 里通常建议给它 `--num-cpus=0`，避免业务负载跑去跟 GCS 抢资源。

**命名空间 / Namespace** —— 命名 Actor 的隔离域。
Ray 里 `ray.get_actor(name, namespace=...)` 只能看到同命名空间的 actor；
默认命名空间是 `"default"`。(mini: `miniray.init(namespace=...)`)

**运行时上下文 / Runtime Context** —— 当前 worker 的自省对象。
Ray 里 `ray.get_runtime_context()` 给出 `worker_id` / `node_id` / `job_id` /
`actor_id` / `namespace`，是排查"我到底在哪个进程里"的第一手信息。(mini 同名)

**本地模式 / local_mode** —— 让远程调用在同一进程里执行。
⚠️ **真实 Ray 已经把它移除了**（写 `ray.init(local_mode=True)` 会直接
`RuntimeError: local_mode is no longer supported`，见第 11 章 §11.7 与
第 16 章 §16.4）；单步调试请改用 **Ray Distributed Debugger**
（remote 里写 `breakpoint()` + VS Code 扩展；`ray debug` 属于 **legacy** 调试器，
需 `RAY_DEBUG=legacy`，见第 33 章 §33.6）。
**mini-ray 仍然保留** `local_mode` —— 那是它的教学特性，不是 Ray 的现状。

**依赖图 / Dependency Graph** —— 任务之间由 ObjectRef 构成的边。
Ray 里"参数里带某个 ObjectRef"就等于"这个任务依赖那个对象"，
调度器据此决定什么时候可以跑。(mini: `miniray/scheduler.py`)

**嵌套引用 / Nested ObjectRef** —— 藏在 list/dict/tuple 里的 ObjectRef。
Ray 里 `ray.get([ref1, {"k": ref2}])` 会按原结构返回，这是 API 的正式语义而不是巧合。
(mini: `_shape_of` / `_rebuild`)

**序列化 / Serialization** —— 把 Python 对象变成字节以便跨进程传递的过程。
Ray 里用 `cloudpickle` 扩展版：函数、闭包、类**按值**打包，
大对象走对象存储而非任务描述。

**cloudpickle** —— pickle 的增强版，能序列化 lambda、闭包、交互式定义的类。
Ray 用它做任务/actor 的按值传递；这也是"闭包捕获大对象会拖慢一切"的根因（第 18 章）。

**函数表 / Function Table (Function Manager)** —— 函数定义在集群里的登记处。
Ray 里第一次 `.remote()` 会把函数导出到 GCS 的函数表，其他节点按需取回并缓存。
(mini: `miniray/function_manager.py`)

**按值传参 / Pass-by-value** —— 参数被序列化进任务描述。
这是 Ray 的默认行为，也是"传大对象该用 `ray.put`"这条建议的原因。

**运行时环境 / Runtime Env** —— 任务/actor 需要的依赖描述。
Ray 里可以声明 pip 包、conda 环境、working_dir、环境变量，
由 Ray 在 worker 上准备；mini-ray 只做 `env_vars`。

**有向无环图 / DAG** —— 计算依赖的拓扑。
Ray 的依赖图**可以有环**（通过 actor），这点和 Spark 的 DAG 有本质区别。

---

## C.2 调度与资源

**逻辑资源 / Logical Resource** —— Ray 的 CPU/GPU 只是**记账单位**，不是物理隔离。
声明 `num_cpus=2` 不会真的限制进程用几个核（但会设 `OMP_NUM_THREADS`）。

**自定义资源 / Custom Resource** —— 除 CPU/GPU 外的任意资源名，如 `{"accelerator": 4}`。
Ray 用它表达"某类特殊硬件/N 个许可"，由用户自己保证语义。

**小数资源 / Fractional Resource** —— 支持小数（精度约 0.0001）的资源声明。
`num_cpus=0.5` 表示两个任务共享一个核——它同时是**限流工具**。

**零 CPU 任务 / Zero-CPU Task** —— `num_cpus=0` 的任务/actor。
Ray 里它不占调度资源，因此**没有并发上限**（可以一直起直到系统崩）；
actor 在"运行时"默认就是 0 CPU（历史原因）。

**资源形状 / Resource Shape** —— 一组资源声明的组合 `(CPU, GPU, 自定义…)`。
Ray 里相同 shape 的任务可以复用同一个 worker 进程，这是 worker 池高效的原因。

**调度器 / Scheduler** —— 决定"任务去哪台机器"的组件。
Ray 的调度是两级的：GCS 侧做全局资源视图与负载均衡，每个节点的 raylet 做本地调度。

**混合调度 / Hybrid Scheduling** —— Ray 的默认策略（`"DEFAULT"`）。
先按资源可用量筛出候选节点（`scheduler_top_k_fraction`，默认 0.2），再在候选里随机选。

**铺开调度 / Spread Scheduling** —— `"SPREAD"` 策略，尽量把任务摊到不同节点上。
适合"想避免热点"或"想测网络"的场景。

**本地性感知 / Locality-Aware Scheduling** —— 优先把任务调度到对象所在的节点。
这是"传 ObjectRef 比传对象快"的第二层原因（第一层是序列化开销）。

**节点亲和 / Node Affinity** —— `NodeAffinitySchedulingStrategy(node_id, soft=...)`。
硬亲和（`soft=False`）资源不够就一直等；软亲和不满足就退回普通调度。(mini 同名)

**拓扑感知调度 / Topology-Aware Scheduling** —— 按硬件拓扑（如 NVLink 域）放置任务。
**API 名是 `topology_strategy`**（alpha）—— 它是 `placement_group()` 的一个关键字参数，
取 `Dict[str, str]`，把**拓扑标签**映射到放置策略，例如
`topology_strategy={"ray.io/gpu-domain": "STRICT_PACK"}`
（`python/ray/util/placement_group.py:140`；与 `strategy=` **互斥**，同时传会抛
`ValueError`，文案是 *"`strategy` and `topology_strategy` cannot both be specified."*）。
⚠️ **alpha 的边界**：除 `ray.io/node-id` 外**只支持一个**拓扑标签，且该标签**只支持 `STRICT_PACK`**
（同文件 `_validate_topology_strategy()`：*"only 'STRICT_PACK' is supported for topology ..."*）。

⚠️ **版本号已核实为 2.57**（本书早先写过 2.56 与"未确认"，均已作废）——
`src/ray/protobuf/gcs.proto` 与 `python/ray/util/placement_group.py` **同时在 2.57 落地**：
`topology_strategy` 在 2.56 的这两个文件里都是 **0 命中**，在 2.57、2.58 里分别是 2 命中与 1 处签名。
⚠️ 本书第 3 章 §3.4 里出现的 **`topology_bundle_scheduling_policy` 是一个不存在的标识符**
（2.58 的 `python/` 树里 `grep -rn "topology_bundle_scheduling_policy" python/ray/` **0 命中**，
对照组 `topology_strategy` 在同一文件里 34 命中 —— 说明不是没取到文件）——
凡见到它一律读作 `topology_strategy`（第 1 章 §1.4 注①、第 8 章 §8.4、§8.11）。

**节点标签调度 / Node Label Scheduling** —— 按标签与表达式调度（alpha）。
`NodeLabelSchedulingStrategy`，mini-ray 未实现。

**调度策略 / Scheduling Strategy** —— 运行期给出的放置约束。
字符串（`"DEFAULT"` / `"SPREAD"`）或策略对象（节点亲和、放置组）。

**放置组 / Placement Group** —— **原子性地预留一组资源**的机制。
Ray 里 `placement_group([{"CPU":1,"GPU":1}] * 4)` 先占资源再往里放 actor，
用来避免"前 3 个起来了、第 4 个永远等下去"的死锁。(mini: `miniray/placement_group.py`)

**束 / Bundle** —— 放置组里的一条资源声明（如 `{"CPU": 1, "GPU": 1}`）。
调度时 bundle 是**原子单位**：要么整条满足，要么不分配。

**放置组策略** —— `PACK`（尽量少节点）/ `SPREAD`（尽量摊开，允许同节点）/
`STRICT_PACK`（必须同节点）/ `STRICT_SPREAD`（必须不同节点）。

**预留 / Reservation** —— 被放置组占住的资源。
它是对集群的**成本**：reserved 的资源普通任务用不了。

**bundle 索引 / bundle_index** —— 指定任务放进放置组的哪一个 bundle。
`-1` 表示"任意装得下的 bundle"，是默认值。

**资源租约 / Lease** —— raylet 向 GCS 申请资源的授权单位。
Ray 的分布式调度靠 lease 避免多个节点重复分配同一份资源。

**worker 池 / Worker Pool** —— 节点上可复用的 worker 进程集合。
Ray 里上限由 `num_workers_soft_limit` 控制（默认 `-1` = 用可用 CPU 数），
只作用于**空闲** worker。(mini: `miniray/worker_pool.py`，自动上限 `max(16, CPU×8)`)

**空闲回收 / Idle Worker Reclamation** —— 空闲 worker 被销毁以省内存。
Ray 里由 raylet 定期巡检控制：`kill_idle_workers_interval_ms`(200ms) +
`idle_worker_killing_time_threshold_ms`（默认 **1 秒**）+ 1 GiB 内存门槛。
⚠️ 旧常量 `worker_idle_timeout_ms`（10 秒）**在 Ray 2.58 已不存在**；
mini-ray 仍在用它（第 3 章 §3.4、第 6 章 §6.8）。

**自动扩缩器 / Autoscaler** —— 按任务压力增减集群节点的组件。
v2（alpha 于 2.10 引入）相比旧版把"期望资源"与"节点供给"解耦，支持 `drain_node`。
⚠️ **"2.54 起默认开启"在本书里是未确认项**：第 1 章版本表已标未确认，
第 17 章 §17.3 明确说"没有找到可直接引用的发布说明原文"，
第 24 章 D.2 也标注了"本书对这一条的出处标注不一致"。
**本条目早先以陈述句给出，是术语表内部的自相矛盾（见下面的 `Autoscaler v2` 条），已统一。**

**负载报告 / Load Report** —— raylet 周期性上报给 GCS 的资源使用快照。
它决定调度器看到的"哪个节点空闲"。

**加速器类型 / `accelerator_type`** —— 给 GPU **贴标签**，让任务能指定"要 A100 不要 H100"。
它本质是**自定义资源的语法糖**，标签必须由节点侧显式声明，写错只会"永远排不上队"。
（第 8 章 §8.2）

**MIG（Multi-Instance GPU）** —— 把一张物理 GPU 切成多个**硬件隔离**的实例。
Ray **不认识 MIG**：运维切好后，通过 `--num-gpus` 告诉 Ray "这台有 N 张卡"即可。
相比 `num_gpus=0.5`（仅记账、显存自己分），MIG 提供真正的故障与显存隔离。（第 8 章 §8.2、第 30 章 §30.2）

**`max_calls`** —— worker 执行多少次任务后**退休**换新进程。
治"worker 内存缓慢上涨"的官方药方；代价是换进程要重新 import 与加载模型。（第 2 章 §2.2）

**`max_pending_calls`** —— actor **邮箱排队**的调用数上限。
注意它和 `max_concurrency` 是两回事：后者限"正在跑几个"，前者限"排队积了多少"。
超出后新调用**立刻失败**，把背压责任推给调用方。（第 9 章 §9.2）

**放置组重建 / Placement Group Rescheduling** —— 宿主节点故障时，
放置组会进入 **`RESCHEDULING`** 并**尝试重新分配丢失的 bundle**（优先级高于普通调度）。
⚠️ **两个常见误解要澄清**（本书早先就是这么写错的）：

1. **资源不够时它不会退化成 `REMOVED`** —— 官方原文：*"the placement group remains in the
   **partially created state indefinitely**"*（`doc/source/ray-core/scheduling/placement-group.rst:725`），
   组会一直等在 pending 队列里；autoscaler 补上机器它就还能恢复。
   `src/ray/gcs/gcs_placement_group_manager.cc` 里**唯一的 `UpdateState(...REMOVED)` 属于显式删除路径**，
   不是"重调度失败"的归宿。
2. **组内的 actor / 任务会被自动拉起 —— 但受各自的容错策略约束**。官方原文：
   *"Ray **reschedules** Actors and tasks that use the bundle (reserved resources)
   **based on their fault tolerant policy** once Ray recovers the bundle."*（同文件 `:730`）。
   即 `max_restarts>0` / `max_task_retries>0` 的 actor 会在 bundle 恢复后重建；
   **默认 `max_restarts=0` 的 actor 不会被重启**，那部分仍要自己重建。

所以生产训练脚本该做的是：**先看组的 `state`**（`RESCHEDULING` = 还在重试，等它就好；
`REMOVED` = 已被显式删除，只能重建），再决定是等待还是自己重建 actor。
（第 8 章 §8.5 有四个状态的完整说明与 §8.10 小结；本条的措辞与第 8 章保持同一口径）

---

## C.3 对象与内存

**对象存储 / Object Store** —— 集群里存放 Ray 对象的共享内存池。
Ray 里默认占可用内存的 **30%**、上限 **200GB**、Linux 上落在 `/dev/shm`、
**macOS 上落在 `/tmp`（磁盘，因此更慢）**。(mini: `miniray/object_store.py`)

**Plasma** —— Ray 对象存储的原始名字（源自 Apache Arrow 的 Plasma 项目）。
今天代码里还能见到 `plasma_*` 的配置与指标名。

**共享内存 / Shared Memory** —— 同节点多进程零拷贝读同一块内存的机制。
Linux 上是 `/dev/shm`；Docker/K8s 里常因为 `--shm-size` 太小而出问题。

**零拷贝 / Zero-Copy** —— 同节点消费者直接拿到对象存储的内存视图，不做拷贝。
Ray 里大 numpy 数组享此待遇，条件是：**C 连续 + 同节点 + 只读**（第 18 章）。

**引用计数 / Reference Counting** —— 决定对象何时可被回收的机制。
Ray 里由**所有者（owner）**维护：谁还持有 ObjectRef，对象就活着。

**所有者 / Owner** —— 创建该对象的进程（task/worker/driver）。
Ray 的对象所有权是"谁创建谁负责"，owner 死了对象也可能被回收——
这是很多"对象莫名丢失"问题的根源。

**对象目录 / Object Directory** —— 记录"每个对象在哪些节点上"的 GCS 元数据。
Ray 里它是本地性调度的依据。(mini: GCS 内的对象目录)

**对象管理器 / Object Manager** —— 负责跨节点搬运对象的组件。
Ray 里它处理对象在节点间的拉取与复制。

**主副本与次副本 / Primary & Secondary Copy** —— 对象在集群里的两份角色。
主副本不可驱逐，优先被溢出到磁盘；次副本在内存压力下按 LRU 先被赶走。

**对象溢出 / Object Spilling** —— 内存压力下把对象写到外部存储（本地盘 / S3）。
Ray 1.3+ 默认开启；相关旋钮：`object_spilling_threshold`（0.8）、
`max_io_workers`、`min_spilling_size`（第 18 章）。

**驱逐 / Eviction** —— 从对象存储里移除对象以腾空间。
Ray 里先驱逐次副本，再考虑溢出主副本。

**LRU** —— 最近最少使用，Ray 驱逐次副本时的策略。

**钉住 / Pinning** —— 让对象不可被驱逐。
Ray 里"正在被某个任务使用"的对象会被钉住；`PINNED_IN_MEMORY` 是 `ray memory`
里的一类引用，也是"存储满了却腾不出空间"的常见原因。

**内联对象 / Inlined Object** —— 小到不值得进对象存储、直接塞进任务/调用消息的对象。
Ray 里由 `max_direct_call_object_size` 之类的阈值控制，这条路径更快但不享本地性。

**对象丢失 / ObjectLostError** —— 对象所在节点的数据没了且无法重建。
Ray 里它要么触发重建，要么抛出该异常。

**对象存储满 / ObjectStoreFullError** —— 加了钉住对象之后实在腾不出空间。
报错文本里会给对象大小并提示你用 `ray memory`。

**对象重建 / Object Reconstruction** —— 对象丢失后重跑生产它的任务来恢复。
依赖血缘信息（见 C.5）。

**Ray Direct Transport（RDT）** —— Ray 的直连数据面（**alpha**）。
目标是在不经过对象存储的情况下搬运大对象/张量，mini-ray 未实现。
⚠️ **这个版本号在本书里已经错过两次，所以这里给出三条可复现的证据，而不是一个结论。**
（第二次错法是第七轮引入的：当时写成"最早出现在 **2.52**"，实测不成立。）
* `python/ray/_private/ray_constants.py` 里检索 `Ray Direct Transport`：
  **2.52.0 / 2.52.1 都是 0 命中，最早出现在 2.53**；
  但**这个字符串只是一句注释里的提及**（"You are not using Ray Direct Transport"），
  拿它当"功能引入版本"本身就不够硬；
* 更硬的证据是**模块何时存在**：`python/ray/experimental/rdt/__init__.py`
  在 **2.54 还是 404、2.55 起返回 200**；
* 而 `@ray.method(tensor_transport=...)` 这个 **API** 在 `python/ray/actor.py`
  里的出现次数是 **2.47=0 / 2.48=38 / 2.49=50 / 2.50=77** —— **2.48 就已落地**。

**能站住的说法只有两句**："API 从 **2.48** 起就有；RDT 作为一个**模块**最早出现在 **2.55**"。
至于流传很广的"**2.50 以 alpha 公布**"，本书**找不到可核对的 release note，标未确认**。
复核命令：

```bash
curl -s https://raw.githubusercontent.com/ray-project/ray/ray-2.53.0/python/ray/_private/ray_constants.py | grep -c "Ray Direct Transport"
# 2.53.0 → 1；把版本换成 2.52.0 → 0
curl -s -o /dev/null -w "%{http_code}\n" https://raw.githubusercontent.com/ray-project/ray/ray-2.55.0/python/ray/experimental/rdt/__init__.py
# 2.55.0 → 200；换成 2.54.0 → 404
```

**内存监控 / Memory Monitor** —— Ray 2.2+ 起在 raylet 内的一个组件
（**不是一个独立进程**），在系统的 OOM killer 之前**主动杀 worker**。
它**按固定间隔采样内存**（`memory_monitor_refresh_ms`，默认 250ms），
超过 `memory_usage_threshold`（默认 0.95）就按策略杀。
⚠️ **策略在 2.56 起改成了 time-based**（本书早先写成"一次只杀一个"，
那是被取代的旧说法），实际顺序是：

1. **可重试的 worker 优先被杀** → 2. 同级用"运行更短/更新"做 tie-break、优先 idle →
3. task 优先于 actor → 4. **必要时一次杀多个**（不是"一个一个杀"）。

实践含义反直觉：**声明了重试的任务反而先被牺牲**。
它产生的异常是 `ray.exceptions.OutOfMemoryError`，
**和对象存储满（`ObjectStoreFullError`）不是一回事**。（第 7 章 §7.7）

**`object_size`** —— State API 里对象**大小**的字段名（单位：**字节**）。
⚠️ **不是 `size`** —— 写错会直接 `KeyError`；而 `call_site` 还需先设
`RAY_record_ref_creation_sites=1` 才会记录。（第 7 章 §7.5、第 11 章 §11.2）

**溢出目录 / Spill Directory** —— 对象溢出时写盘的目录，
默认落在 **session 临时目录**（`/tmp/ray/session_*/` 这一类；源码注释说
"use the `temp_dir` of the worker node as the object spilling directory"，
`python/ray/_private/node.py:1696-1702`）（**本地盘，非共享存储**）。
所以**节点一挂，溢出文件跟着丢** —— 溢出是扩容手段，不是持久化手段。
⚠️ 本书早先写成"`<temp_folder>/spill`"，**`/spill` 这一级本书未能在源码/文档中确认**。（第 7 章 §7.6）

---

## C.4 Actor

**Actor 句柄 / ActorHandle** —— 客户端持有的 actor 代理。
Ray 里它可被序列化传给别的任务/actor（这是"分布式句柄"的常见用法）。
(mini: `ActorHandle`)

**Actor 方法 / ActorMethod** —— `handle.method` 这个可 `.remote()` 的代理对象。
(mini: `ActorMethod`)

**邮箱 / Mailbox** —— actor 内部排队等待执行的方法调用队列。
默认按**提交顺序**执行（并发度为 1 时是严格的 FIFO）。

**并发度 / max_concurrency** —— 一个 actor 同时执行多少方法调用。
Ray 里 sync actor 默认 1，**async actor 默认 1000**。

**并发组 / Concurrency Group** —— 把 actor 的方法分到不同并发度的小组。
`concurrency_groups={"io": 2, "compute": 4}` + `@ray.method(concurrency_group="io")`。
(mini 同名)

**异步 Actor / Async Actor** —— 方法声明为 `async def` 的 actor。
Ray 里它在**单线程事件循环**里并发，适合高并发 IO；不需要锁。

**线程化 Actor / Threaded Actor** —— 用 `max_concurrency>1` 的同步 actor。
Ray 里它真的多线程执行，**共享状态需要你自己加锁**。

**Actor 池 / ActorPool** —— 一组同构 actor 的工作池。
Ray 里 `ray.util.ActorPool` 提供 `map` / `map_unordered`，
内部天然带背压（每个 actor 手里最多一个任务）。(mini 同名)

**命名 Actor / Named Actor** —— 用 `name=` 创建、可用 `ray.get_actor(name)` 取回的 actor。
它是跨进程/跨 job 找到同一个 actor 的唯一方式。

**Detached Actor** —— `lifetime="detached"` 的命名 actor，**活过 driver**。
代价是没人自动回收它，忘了 `ray.kill` 就会泄漏（第 18 章反模式 10）。
(mini 只在同一进程生命周期内有效)

**重启次数 / max_restarts** —— actor 崩溃后自动重启的上限，默认 **0**（不重启）。
重启会重新跑 `__init__`，**状态丢失**。

**方法重试 / max_task_retries** —— 单个 actor 方法调用的重试次数。
Ray 里它和 `max_restarts` 是两件事，别混。

**ActorDiedError** —— actor 死亡且无法重启时，在飞调用收到的异常。(mini 同名)

**顺序保证 / Ordering Guarantee** —— 同一 actor 的方法按提交顺序执行。
跨 actor 之间**没有**顺序保证——这是设计而非缺陷。

---

## C.5 容错

**容错 / Fault Tolerance** —— 部分组件失败时系统仍能完成任务的能力。
Ray 的容错是**分级**的：任务可重试、对象可重建、actor 可选重启、GCS 可换存储后端。

**血缘重建 / Lineage Reconstruction** —— 对象丢失后，按依赖关系**递归重跑**生产链。
这是 Ray 论文的核心卖点之一：不存中间结果，用"怎么算出来的"来恢复。
(mini: `miniray/raylet.py` 的 lineage 逻辑，示例 `examples/06_fault_tolerance.py`)

**WorkerCrashedError** —— worker 进程崩溃（段错误 / `os._exit` / 被 OOM 杀）。
它与普通异常的区别很重要：**要换一个 worker 重跑**，且它产生的中间对象可能丢失。
(mini 同名)

**幂等性 / Idempotency** —— 任务重复执行不产生额外副作用。
Ray 会重试任务，所以**有副作用的任务必须自己保证幂等**（写文件、发请求尤其危险）。

**GCS 容错** —— GCS 元数据不丢的能力。**2.58 有三种存储后端**
（`gcs_storage`，**默认 `"memory"`** —— 见 `src/ray/common/ray_config_def.h:446-448`，
注释列出 `'memory'` / `'redis'` / `'rocksdb'`）：
① **进程内 `memory`（默认）** —— 不开任何外部后端；
② **外部 Redis**（`RAY_REDIS_ADDRESS`，或 `RAY_gcs_storage=redis`）——
长期存在、仍是官方支持的后端，但**要显式 opt-in**；
③ **内嵌 RocksDB**（REP-64）—— **2.57 起提供，但默认关闭**，
需要显式设 `RAY_gcs_storage=rocksdb` + 持久化路径，且**仅 Linux**；
它可以直接内部复制或写外部存储（如 S3）。
⚠️ **这里本书早先有两处都写错了**：既不能写"2.57 起改用 RocksDB、
不再需要 Redis"（那只是"多了一个可选后端"），
**也不能写"默认路径仍是 Redis"** —— 2.58 的默认是**进程内 `memory`**，
Redis 与 RocksDB 都是**显式 opt-in 的可选后端**
（选择逻辑：`python/ray/_private/node.py:488-489` 的
`_resolve_ray_config("gcs_storage", "memory")`）（第 10 章 §10.6）。

**Redis** —— GCS 的**长期支持**后端之一（**不是**历史遗留）。
今天在文档/配置里遇到的 `redis_*` 名字大多是**当前有效**的，
只有 `redis_shards` 一类是已废弃的（那也正是 **6380 端口**的来源 ——
**GCS 默认端口是 6379**，第 17 章 §17.5 与附录 F 早先写的 6380 是错的，已修正）。

**健康检查 / Health Check** —— 节点/进程存活性的周期检测。
GCS 靠它判定节点死亡，进而触发对象重建。

**检查点 / Checkpoint** —— 用户自己保存的状态快照。
Ray 的 actor 重启**不恢复状态**，所以长跑的有状态组件必须自己写检查点
（Ray Train 提供 `ray.train.Checkpoint`）。

**Session Directory** —— 一次 Ray 会话的临时目录（socket、日志、溢出文件）。
driver 退出后可能被清理；排查问题时它常常是**关键证据所在地**。

**`OwnerDiedError`** —— **对象的所有者（owner）进程死了**导致对象取不到。
它与 `ObjectLostError` 的区别是**语义**上的，**不是类型树上的** ——
`OwnerDiedError` **是 `ObjectLostError` 的子类**（`exceptions.py:807`
的 `class OwnerDiedError(ObjectLostError)`），所以 `except ObjectLostError`
**能**接住它。真正的区别在于**它不会触发 lineage 重建** ——
因为血缘本身随 owner 一起没了。典型场景是"driver 里 `ray.put` 了大对象，
然后 driver 被 kill"。（第 7 章 §7.5、第 10 章 §10.4）

**`WorkerExitType`** —— worker 的**退出原因**枚举。
`NODE_OUT_OF_MEMORY` = 被 Ray 的内存监控**主动杀掉**（节点内存超阈值）；
其它值多为 worker 自己崩的。**这个区分直接决定你该去调容量，还是去查代码。**
（第 7 章 §7.7、第 10 章 §10.3）

**生成器重试语义 / Generator Retry Semantics** —— 生成器任务**支持**重试，
但语义是 ⚠️ **「整个任务从头重放」**，**不是断点续传**：
Ray 把当前 attempt 标记为失败、`attempt_number` 加一，然后**重新提交同一份 task spec**
（也就是函数从头再跑一遍）；下游之所以看到"没有重复也没有断档"，
是因为**旧 attempt 产出的对象被 `attempt_number` 过滤掉了**，
而不是因为函数跳过了前面几个值。
**因此官方明确要求生成器必须幂等且确定性**
（原文：*"this assumes that the generator task is idempotent and deterministic"*）。
（第 5 章 §5.3、第 10 章 §10.2）

**任务重试 / Task Retry（`max_retries`）** —— 控制**系统级失败**（worker 崩溃、节点故障）
的重试次数，**默认 3**。⚠️ 它**管不了应用异常** ——
`ValueError` 这类异常默认**不重试**，要显式 `retry_exceptions=True`。
（本书早先有一处把这条说反了：写成"系统级错误默认不占重试额度"，
实际上是**应用异常默认不重试**。第 2 章 §2.8、第 10 章 §10.2）

**`retry_exceptions`** —— 控制**应用异常**是否重试的参数（默认 `False`）。
它和 `max_retries`（只管 worker 崩溃）是**两个不同的东西** ——
把这两者搞混是 Ray 里最普遍的一处误解。（第 2 章 §2.8、第 10 章 §10.2）

---

## C.6 架构与内部

**GCS（Global Control Store，全局控制存储）** —— 集群的元数据服务中心。
Ray 里它存节点、actor、放置组、函数表、对象目录；**2.57 起多了一个可选的嵌入 RocksDB 后端**（alpha、opt-in、仅 Linux）—— ⚠️ **不是"改用"**，且**默认后端也不是 Redis**：2.58 的 `gcs_storage` 默认是**进程内 `memory`**，Redis / RocksDB 都是显式 opt-in，见 §C.5 的 GCS 容错条。
**它是单点**，因此有容错设计。(mini: `miniray/gcs.py`，在 driver 进程内)

**Raylet** —— 每个节点上的本地调度器 + 对象管理器。
Ray 里它是 C++ 进程，负责本地资源记账、worker 池、对象存储与对象搬运。
(mini: `miniray/raylet.py`，是线程而不是进程)

**CoreWorker** —— 每个进程（driver/worker）里的"Ray 客户端"。
它负责提交任务、维护引用计数、与 raylet/GCS 通信。(mini: `miniray/core_worker.py`)

**gRPC** —— Ray 内部组件间通信的协议。
Ray 的控制面基本是 gRPC；数据面走对象存储/RDT/NCCL。

**RPC** —— 远程过程调用。理解 Ray 的一个有用角度是：**它把 RPC 的样板全吃掉了**。
(mini 用 TCP + 长度前缀 + pickle 的极简 RPC，见 `miniray/rpc.py`)

**Ray Client** —— 从本地脚本连到远端集群的客户端模式。
注意：它**不支持 Ray Data 的 dataset API**，需要把调用包进 remote task 里绕开。

**ID 体系** —— `JobID` / `TaskID` / `ActorID` / `ObjectID` / `NodeID` / `WorkerID` /
`PlacementGroupID`。Ray 里它们出现在所有日志与 State API 输出里，是排错的主键。
(mini: `miniray/ids.py`)

**State API** —— 查询集群当前状态的结构化接口（`ray.util.state`）。
`list_tasks` / `list_objects` / `list_actors` / `list_nodes` / `list_workers` /
`list_placement_groups` 与 `summarize_*`。(mini: `miniray/state.py`)

**Dashboard** —— Ray 自带的 Web UI（默认 8265 端口）。
它展示节点/任务/actor/对象/日志/指标。**默认没有鉴权**，暴露到不可信网络是
真实的安全风险（第 1 章的 CVE-2025-62593）。(mini 明确不做，用 State API + timeline 代替)

**时间线 / Timeline & Tracing** —— 把事件的开始/结束画成甘特图。
Ray 里 `ray.timeline(filename)` 导出 Chrome Trace JSON。
(mini 还能额外输出自包含 HTML 甘特图)

**系统配置 / System Config** —— 通过 `ray.init(_system_config=...)` 或
`ray start --system-config='{...}'` 传入的内部配置，定义在 `ray_config_def.h`，
也可以用 `RAY_<配置名>` 环境变量设置（第 18 章的调度/溢出参数都是这一类）。

**编译图 / Compiled Graph** —— 见 §C.7 的 **Compiled Graph** 条
（第五轮合并：此处原有一份重复定义，与 C.7 的那条内容重叠）。

---

## C.7 AI 库

**Ray Data** —— 分布式数据管道库。
核心抽象是 `Dataset` 与 **Block**（内部的不可变数据分块），核心算子是
`map_batches`（批处理，反模式"任务太小"的正解）。

**块 / Block** —— Ray Data 内部的并行单位。
一个 Dataset 由若干 Block 组成，每个 Block 在一个 worker 上处理。

**map_batches** —— 对每个 Block 应用一个批处理函数。
它是 Ray Data 性能的关键：**批大小**决定开销占比。

**DataSourceV2** —— Ray Data 新的读取基础设施（row-group 感知的分块、谓词下推）。
2.57 起默认开启（#64821）。

**Hash Shuffle（V2）** —— Ray Data 的 shuffle 实现。hash-based shuffle **自 2.50 起
为默认**；**Hash Shuffle V2** 由 `#63598` 引入、**2.58 起支持 `join`**。
V2 去掉了聚合 actor 池，改成两个无状态 task 算子，让数据能溢出、不再预占容量。
⚠️ 它与 **DataSourceV2**（2.57 默认开启的读取路径）是**两件独立的事**，
常被混为一谈。（第 12 章 §12.3）

**Ray Train** —— 分布式训练编排库。
它接管 `MASTER_ADDR` / `RANK` / `WORLD_SIZE` 这些样板，训练循环仍是 PyTorch。

**Train V2** —— Ray Train 的重写版本（2.43 起可用，**2.51 起默认开启**）。
与 V1（`ray.air`）**不能混用**，混用会直接报错。

**Ray Tune** —— 超参搜索与试验管理。
核心概念是 **Trial**（一次试验）、**Search Algorithm**（怎么采样）、
**Scheduler**（怎么提前停掉差的试验）。

**Trial** —— Tune 里的一次试验执行。
每个 trial 是一个独立的可恢复单元，失败可以重跑或从检查点续。

**Ray Serve** —— 在线推理服务库。
核心概念是 **Deployment**（可独立扩缩的部署单元）与 **`ingress`**：
`ray.serve` 导出的 `ingress` **是一个装饰器**（`@serve.ingress(app)`，
`python/ray/serve/api.py:324`），把 FastAPI 之类的 ASGI 应用挂到某个 deployment 上；
口语里也把"接外部流量的那个 deployment"叫 ingress。
⚠️ **它不是一个与 `Deployment` 并列的大写 `Ingress` 类** ——
2.58 的 `python/ray/serve/__init__.py` 里 `grep "\bIngress\b"` **0 命中**，
`__all__` 里只有小写的 `ingress` 与 `deployment`。
（`OpenAiIngress` 确实存在，但在另一个命名空间 `ray.serve.llm.ingress` 下，不是 Serve 核心概念。）
本书早先把它当成"核心概念 / 大写类"，是旧版 Serve 的写法，已更正（第 15 章 §15.4）。

**Deployment** —— Serve 里一个可扩缩的副本组。
用 `@serve.deployment` 定义，`serve.run(app)` 启动。

**DeploymentHandle / DeploymentResponse** —— 现代 Serve 的调用接口。
`handle.remote()` 返回 `DeploymentResponse`，`.result()` 或 `await` 取值。
**它取代了 2.10 被移除的 `RayServeHandle`**。

**KV-cache 感知路由 / KV-aware Routing** —— 按请求的 KV cache 亲和性选副本。
Ray Serve LLM 在 2.58 完成的能力，目的是提高前缀缓存命中率。

**PD 分离 / Prefill-Decode Disaggregation** —— 把推理的 prefill 与 decode 阶段
拆到不同实例上（分别优化算力型与访存型负载）。Ray Serve LLM 侧的对应能力见第 15 章。

**Ray Data LLM / Ray Serve LLM** —— Ray 上做 LLM 批推理与在线服务的上层封装。
**具体 API 名称与版本对应关系本书未逐一确认**。

**RLlib** —— Ray 自带的强化学习库（含算法、环境、训练器）。
它的 TF 支持与旧 API stack 在弃用路线上（**具体版本号未确认**）。

**Ray Workflows** —— Ray 曾经的持久化工作流库。
**2.44 弃用、之后移除**（PR #53612），**`ray==2.47` 是最后一个含它的版本**。
移除之后，编排交给外部工具（Airflow/Prefect）或 Serve + Jobs —— 见附录 G §G.5。

**`ray.util.metrics`** —— **应用自定义指标**入口：`Counter` / `Gauge` / `Histogram`。
和 State API 的区别是：State API 回答"**Ray** 现在什么样"，
它回答"**我的程序**现在什么样"。指标与系统指标**共用**同一个 Prometheus 端点。
（第 11 章 §11.5、附录 A §A.8）

**`ExecutionOptions` / `DataContext`** —— Ray Data 的执行期调优入口
（在飞 block 数、对象存储预算、顺序保持等）。
文档里经常只给"调优建议"而不给 API —— 落到代码上就是这两个对象。
（第 12 章）

**`DataConfig`** —— Ray Train V2 里描述"训练数据**怎么切分**"的配置对象
（`DataConfig(datasets_to_split=..., execution_options=...)`）。
⚠️ 它**只描述切分策略，不承载数据本身** —— 数据走 `datasets={"train": …, "valid": …}`。
（第 13 章 §13.3）

**`Stopper`** —— Ray Tune 里控制"**什么时候该停**"的抽象
（`TrialPlateauStopper`、`TimeoutStopper`、自定义子类等），
与 `Scheduler`（控制"怎么分配资源"）是两个正交的概念。（第 14 章）

**Model Multiplexing（模型多路复用）** —— Ray Serve 里让**一个副本同时持有多个模型**、
按请求里的 `model_id` 选择加载的机制（`@serve.multiplexed`）。
用途是"模型很多、每个请求量都不大"的场景，避免为每个模型各起一个副本。（第 15 章 §15.16）

**Compiled Graph** —— 把一张 DAG **编译成一个分布式执行单元**，
用通道（channel）替代对象存储做数据面，消除逐任务的调度与往返开销。
它是张量并行推理的底座。⚠️ **API 状态是 beta，不是 experimental** ——
官方文档标题就是 *"Ray Compiled Graph (beta)"*，正文写着
*"Ray Compiled Graph is currently in beta (since Ray 2.44). The APIs are subject to change and expected to evolve."*
（`doc/source/ray-core/compiled-graph/ray-compiled-graph.rst:3,8`）。
⚠️ 但**方法名确实带 `experimental_` 前缀**（`experimental_compile()`）——
"beta 的 API + experimental 的方法名"这个组合本身就是官方态度，别把两者混为一谈
（第 26 章 §26.3；该处表格写的也是 beta，与本条口径一致）。

**`RayChannelError`** —— Compiled Graph / 通道（channel）相关错误。
（第 26 章）

---

## C.8 部署与运维

**KubeRay** —— 在 Kubernetes 上管理 Ray 集群的 Operator。
提供 `RayCluster` / `RayJob` / `RayService` **三个稳定 CRD**（另有 alpha 的
`RayCronJob`，见下），并负责对接 in-tree autoscaler。

**RayCluster / RayJob / RayService** —— KubeRay 的三个**稳定** CRD。
前者描述集群拓扑，中者提交一次性作业，后者描述带 HTTP 入口的服务化集群。

**RayCronJob** —— KubeRay 的**第四个** CRD：按 cron 表达式周期性地创建 RayJob。
**v1.6 起以 alpha 引入、默认关闭**，需打开 `RayCronJob` feature gate；
v1.7 新增 `timeZone` 字段。因为它是 alpha，很多资料在"KubeRay 有几个 CRD"
这个问题上只说三个 —— 两个答案都不算错，**取决于是否算上 alpha 的那个**。
详见第 17 章 §17.2。

**Kueue** —— K8s 原生的作业**准入**与配额系统（gang 准入、优先级、抢占）。
对 RayCluster/RayJob/RayService 有原生支持；把 RayCluster 当弹性作业需要
`ElasticJobsViaWorkloadSlices` 这个 **alpha** feature gate。

**Volcano / YuniKorn / scheduler-plugins** —— K8s 的批调度器，提供 gang scheduling。
KubeRay 通过 `batchScheduler.name=volcano` 这类设置启用。

**Gang Scheduling（组调度）** —— "要么全给、要么不给"的调度语义。
它解决的是分布式训练里"起了 3 个 worker 差 1 个，互相死等"的问题，
但与 Ray autoscaler 的渐进扩容天然冲突（第 19 章）。

**PodGroup** —— Volcano 等调度器表达 gang 的资源对象。
它的 `minMember` 必须随 worker 副本数变化而更新——这是已知的摩擦点。

**Jobs API** —— 见 **§C.8** 的 **Jobs API** 条（第五轮合并：此处原有一份重复定义）。
补充一点：它是 mini-ray 明确不做的能力之一。

**集群启动器 / Cluster Launcher** —— Ray 传统的云 VM 集群管理方式（`ray up` 等）。
与 KubeRay 路线并存。

**日志聚合 / Log Aggregation** —— 把各节点的 worker 日志汇总。
Ray 会转发 worker 输出并加 `(pid)` 前缀；mini-ray 则是直接继承终端。

**Token 认证 / Token Authentication** —— Ray 2.52 引入的鉴权机制。
`RAY_AUTH_MODE=token` 打开；token 来自 `RAY_AUTH_TOKEN` /
`RAY_AUTH_TOKEN_PATH` / `~/.ray/auth_token`。**默认是关闭的**（第 17、24 章）。

**RAY_AUTH_MODE** —— 鉴权模式开关，取值 `disabled`（默认）/ `token`。
必须**全集群一致**，否则某些节点会拒连。

**可观测性 / Observability** —— 通过状态 / 日志 / 指标 / 追踪理解集群行为。
⚠️ **四层是「状态、日志、指标、追踪」**（第 11 章 §11.1）——
不是"Dashboard / State API / ray memory / Timeline"那四个**工具**。
工具是实现方式，四层是**问题的分类**：
状态=现在什么样、日志=这一刻发生了什么、指标=整体健康度、
追踪=这条请求经过了谁（**追踪那一层在第 36 章**，本书前三轮是空的）。
(mini-ray 有 State API 与 timeline，**没有 memory 工具**，也没有追踪。)

**`ray status`** —— 命令行查看集群资源与 autoscaler 状态的工具。
提问前应该准备好的东西之一（附录 D 最后一节）。

**History Server** —— KubeRay 的组件，用途是 **RayCluster 被删除之后仍能访问
job 与集群日志**（惰性加载 + LRU 缓存）。它回应的是"集群删了，日志也没了"
这个运维痛点。（第 17 章 §17.2）

**Autoscaler v2** —— 新一代集群 autoscaler，**新增优先级感知的 worker group 选择**。
⚠️ **本书对"自 2.54.0 起默认开启"的标注不一致**：第 8 章 §8.8 引了 release notes，
第 17 章 §17.3 标为**未确认**。请以 `ray status` 的实际输出为准。

**Slurm** —— HPC 领域最常见的批调度器。**它和 Ray 不是竞争关系**：
典型做法是用 `sbatch`/`srun` 申请节点，再在这些节点上 `ray start` 组成集群。
研究机构与超算中心因为装不了 KubeRay，走的是这条路。（附录 G §G.3）

**Jobs API** —— 把脚本像"作业"一样提交到集群的接口
（CLI `ray job submit` + `JobSubmissionClient`）。
⚠️ 它是**长时任务**的官方推荐方式 —— 但注意「**官方文档的推荐**」与
「**Ray Client 已进入维护状态**」是两句话，**证据强度不同**：
`doc/source/cluster/running-applications/job-submission/ray-client.rst` 里只有前者的原话
（*"...we recommend using Ray Jobs instead."*，`:44`），
`grep -in "maintenance\|deprecat" ray-client.rst` **0 命中** ——
"维护状态"的说法出自 **issue #47700**，而且它的关闭评论是**社区贡献者**（`authorAssociation: CONTRIBUTOR`）
写的分诊意见，**不是维护者公告**。所以本书把它标为**本书的判断**，不当作官方事实
（第 4 章 §4.7、附录 F §F.3.5）。

---

## C.9 生态与选型

**PyTorch Foundation** —— Ray 项目 2025 年秋起归属的基金会（Linux Foundation 旗下）。
与 PyTorch、vLLM、DeepSpeed 同属一个伞下；治理结构是 TAC + Governing Board。

**Anyscale** —— Ray 的商业化公司（创始团队来自 Ray 作者）。
2026-07-30 宣布被 Nscale 收购（待交割）；**Ray 开源项目不在交易范围内**。

**vLLM** —— 高吞吐 LLM 推理引擎，多机多卡分布式执行长期使用 Ray。
RFC #35848 / PR #36836 提出 **`RayExecutorV2`**：数据面交给
`torch.distributed`/NCCL，Ray 只保留**放置与资源感知调度**——这是"分工"的教科书级证据。

**RayExecutorV2** —— vLLM 里取代 compiled-graph 版 Ray executor 的新实现。
继承 `MultiprocExecutor`，控制面走消息队列，热路径上没有 `ray.remote`。

**torchrun / torch.distributed** —— PyTorch 官方的启动器与集合通信库。
Ray 管**进程编排与放置**，`torch.distributed` 管**通信**，二者是分工关系。

**NCCL** —— NVIDIA 的集合通信库（AMD 侧对应 RCCL），`torch.distributed` 的 GPU 后端。
Ray 不做集合通信；训练/推理的数据面最终都落到它身上。
它「卡住不动」是分布式训练最经典的故障，排查入口是 `NCCL_DEBUG=INFO`（第 30 章 §30.4）。

**Spark / Databricks** —— 结构化数据处理的事实标准与它的商业平台。
与 Ray 互补：Databricks Runtime ML 15.0+ 预装 Ray，
提供 `ray.util.spark.setup_ray_cluster` 与 `ray.data.from_spark`。

**Dask** —— Python 的并行计算库，语义贴近 pandas/numpy，用图调度。
与 Ray 的差别在于 ⚠️ **actor 与对象存储都不是一等公民**
（Dask 有**实验性**的 actor：`client.submit(Cls, actor=True)`，
其文档自述是 "shamelessly stolen from the Ray Project"；
但那是附加能力，不像 Ray 那样是核心抽象 —— 见第 19 章 §19.3 的对照表）。
把 Dask 跑在 Ray 上的路径存在（`ray.util.dask`），**本书未确认其在 2.58 的状态**。

**Monarch / hyperactor** —— Meta 于 2025 年 10 月 PyTorch Conference 发布的
分布式执行引擎：single-controller、Python 前端 + Rust 后端（hyperactor / hyperactor_mesh）、
原生 RDMA。**它仍很新、API 会变，生态与成熟度远不如 Ray**（第 19 章）。

**SkyPilot** —— 多云/多集群编排工具（挑 spot 实例、跨云起集群）。
它与 Ray **不同层**：SkyPilot 负责把机器弄来，Ray 负责在上面调度。

**Modal** —— serverless GPU 平台。
与 Ray 在"只是跑一批 GPU 函数"的场景上有替代关系。
**是否存在与 Ray 的官方集成页面，本书未确认**。

**verl / OpenRLHF / SkyRL / NeMo-RL / AReaL / slime / miles** ——
主流的 LLM 后训练（RL）框架，**绝大多数构建在 Ray 上**。
它们的差异在同步/异步、colocate/分离、训练与推理后端，而不是算法列表（第 19 章）。

**Compiled Graph** —— 见 §C.7；它在生态里的意义是"把 Ray 的调度开销压到
能用于推理热路径的量级"。

**Ray Summit** —— Ray 社区年度大会。
2026 年那届在 8 月 24–26 日于旧金山举行，与首届 vLLM Conference 同期（第 19 章）。

**mini-ray** —— 本书第 06 章的配套实现：一个纯 Python 标准库 + NumPy 的 Ray 简化版，
把任务调度、对象存储、actor、容错、可观测性都实现了一遍（约 1.1 万行、192 个测试）。
它的定位是**教学实现**，不是替代品。

---

## C.10 GPU、训练与剖析工具链

这一节收录第 13、29、30、31 章反复出现的术语 —— 它们**大多不属于 Ray**，
但不知道它们就读不懂「Ray 上的 GPU 出了什么问题」。

**`CUDA_VISIBLE_DEVICES`** —— 决定一个进程能看见哪些 GPU 的环境变量。
Ray 在 **worker 启动时**注入它来实现设备隔离。⚠️ **在 worker 内必须写 `cuda:0`**
（那是「我分到的那张卡」），不要写物理号；也**永远不要自己改这个变量**（第 30 章 §30.1）。

**`ray.get_gpu_ids()`** —— 返回当前 worker 分到的**物理** GPU 号。
与 `torch.cuda.current_device()`（**进程内**逻辑号）在非 0 号卡上**永远不相等**（第 30 章 §30.1）。

**分数 GPU / Fractional GPU** —— `num_gpus=0.5` 的语义。它**只是记账**
（让两个 worker 共享同一张卡的配额），**不做显存或算力隔离**。
共享时必须自己 `torch.cuda.set_per_process_memory_fraction()`（第 30 章 §30.2）。

**集合通信 / Collective Communication** —— 多卡/多机之间同步张量的原语
（`all_reduce` / `all_gather` / `broadcast` / `reduce_scatter`）。
**Ray 不做这件事** —— 它只负责把 `MASTER_ADDR` / `RANK` / `WORLD_SIZE` 送对（第 30 章 §30.3）。

**`NCCL_SOCKET_IFNAME`** —— 指定 NCCL 走哪张网卡。
**多网卡机器上不设它，是多机训练 hang 的头号原因**（第 30 章 §30.4）。

**`torch.distributed`** —— PyTorch 的分布式通信接口（`init_process_group` 等）。
注意它的默认超时**按后端不同**：**`init_process_group` 通用默认是 30 分钟**
（`c10d/ProcessGroup.hpp:21` 的 `kProcessGroupDefaultTimeout = 30 * 60 * 1000`），
而 **NCCL 后端默认是 10 分钟**
（`c10d/ProcessGroupNCCL.hpp:131` 的 `kProcessGroupNCCLDefaultTimeout = 10 * 60 * 1000`）。
Ray Train 走 NCCL，所以实践上多半按 **10 分钟**算。大模型常需显式调大（第 30 章 §30.3）。

**AllReduce** —— 最常用的集合操作：把所有 rank 的梯度求和/平均后广播回各 rank。
DDP 的每一次 `loss.backward()` 内部就发生一次（第 13 章 §13.4）。

**DDP / FSDP / DeepSpeed / Megatron** —— 四种并行策略。
DDP 数据并行、FSDP 分片参数与优化器状态、DeepSpeed（ZeRO）分级分片、
Megatron 张量/流水并行。Ray Train **不实现它们**，只负责编排（第 13 章 §13.4）。

**ZeRO** —— DeepSpeed 的分片策略（stage 1/2/3 分别切优化器状态 / 梯度 / 参数）。
**显存算术记一个数**：全量微调约 **16 字节/参数**，7B ≈ 112 GB（第 30 章 §30.5）。

**LoRA / QLoRA** —— 参数高效微调（只训练低秩适配器 / 再把基座量化到 4bit）。
它特别适合 Ray：**checkpoint 小 → 可以频繁存 → 抢占容错变便宜**（第 29 章 §29.5）。

**PEFT** —— HuggingFace 的参数高效微调库（`LoraConfig` / `get_peft_model`）。
⚠️ `save_pretrained` 存的是**适配器**；一旦 `merge_and_unload()` 就变成全量权重（第 29 章 §29.5）。

**`CUDA out of memory` 的四种成因** —— ① 模型装不下 ② 批次太大
③ **碎片化** ④ 泄漏。**判据是看 `allocated` 与 `reserved` 的关系**，
四者修法完全不同（第 30 章 §30.5）。

**碎片化 / Fragmentation** —— `reserved` 远大于 `allocated`、报「还有空闲显存却分配失败」。
修法是 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`（第 30 章 §30.5）。

**`py-spy`** —— 不停机采样 Python 进程调用栈的工具，产出**火焰图**。
它回答「CPU 时间花在哪些调用路径上」，是排查「卡住/空转」的第一把刀（第 31 章 §31.3）。

**`memray`** —— Python 内存分配剖析器，产出**内存火焰图**（宽度 = 分配字节数）。
开销大，只适合复现问题，不适合常开（第 31 章 §31.4）。

**火焰图 / Flame Graph** —— 横轴是按采样次数归一化的时间（或内存）占比，
**只看宽度不看高度**。`time.sleep` 宽 = 在等；`pickle` 宽 = 序列化是瓶颈（第 31 章 §31.3）。

**`nsys` / Nsight Systems** —— NVIDIA 的时间线剖析器，回答「GPU 有没有闲着」。
**先用它把问题分成「GPU 内」和「GPU 外」**，再决定要不要上 `ncu`（第 31 章 §31.5）。

**`ncu` / Nsight Compute** —— 单 kernel 级剖析器，开销极大（可慢 100 倍），
必须配合 `-k`/`-c` 限定范围（第 31 章 §31.5）。

**NVTX** —— 在 `nsys` 时间线上打自定义标记的 API（`torch.cuda.nvtx.range_push`），
用来把「我关心的那段」和框架开销分开（第 31 章 §31.5）。

**`RAY_PROFILING=1`** —— 导出可用 timeline 的**两个前提之一**（不是唯一前提）。
⚠️ **必须同时设 `RAY_task_events_report_interval_ms=0`** —— 只设 `RAY_PROFILING` 的话，
事件可能停在上报队列里没进 GCS，结果**同样是空文件**。官方源码里这两个变量是一起要求的：
`python/ray/_private/state.py` 的 `timeline()` docstring：*"Ray profiling must be enabled by
setting the RAY_PROFILING=1 environment variable prior to starting Ray, and set
RAY_task_events_report_interval_ms=0"*（`:1164-1165`）；拿到空结果时打的 warning 也把两条并列
（`:662-665`）。不开的话 `ray.timeline()` 只会给你一个只有系统事件的空壳文件 ——
这是「timeline 用不了」的头号原因，而它**不报错、只打 warning**（第 11 章 §11.6、第 31 章 §31.6、第 33 章 §33.6）。

**Task Events** —— Ray 记录每个任务的调度/执行时间戳的机制。
⚠️ **2.58 只是"可以"把它移出 GCS 热路径，不是已经移出** —— 那是一个
**默认关闭的 opt-in 开关** `RAY_enable_task_events_to_dashboard_head`
（`src/ray/common/ray_config_def.h:1132-1136`：`RAY_CONFIG(bool, enable_task_events_to_dashboard_head, false)`，
注释写明 *"the state API (list tasks / ray.timeline) reads task events from the dashboard head
instead of GCS"* 只在它为 `true` 时成立）。
所以**默认路径仍然走 GCS**；升级到 2.58 后发现 task events 变少 / dashboard 上看不到，
先查这个开关是不是被显式打开了（第 20 章版本表、第 11 章 §11.2、第 31 章 §31.6）。

### 表格 ML、血缘与追踪（第四轮新增）

**GBDT / Gradient Boosted Decision Trees（梯度提升决策树）** —— 以 XGBoost /
LightGBM / CatBoost 为代表的**表格数据主力模型**。
在 Ray 上的分布式方式是**数据并行 + 直方图聚合**：每个 worker 算局部直方图，
AllReduce 聚合成全局直方图，再各自分裂 —— 所以**所有 worker 的模型逐位相同**，
这不是 ensemble。**并行度有天花板**（第 34 章 §34.3）。

**XGBoostTrainer / LightGBMTrainer** —— Ray Train 里跑 GBDT 的两个 Trainer
（`ray.train.xgboost` / `ray.train.lightgbm`）。参数就是原生库的参数字典，
但 `datasets=` 要传 `ray.data.Dataset` 而非 DataFrame。
**它们仍在，且已 V2 化**（`ray.train.v2.xgboost` / `ray.train.v2.lightgbm`）；
被 2.9 移除的是 `LightningTrainer` / `TransformersTrainer` / `AccelerateTrainer`，
别混为一谈（第 34 章 §34.3、第 13 章 §13.2）。

**scikit-learn** —— 一个"人人都用、本书却直到第 34 章才真正开讲"的库
（此前只在第 00 章的目录、这条术语表和第 32 章的链接里出现过名字，
没有一节是讲它的）。
在 Ray 上的正确用法是 **Tune + 单机模型**（第 34 章 §34.6）——
注意 `n_jobs` 与 `max_concurrent_trials` **必须配合着算**，否则机器直接卡死。

**数据指纹 / Data Fingerprint** —— 一个**短、稳定、可比**的字符串，
用来回答"这份数据是不是上次那份"。
做法是对**排序后的「文件路径 + 大小（+ mtime/ETag）」**做哈希。
⚠️ **Ray 没有公开的 Dataset 指纹 API** —— `Dataset` 内部的 UUID
**不是跨进程稳定的版本标识，不要拿它当数据版本用**（第 35 章 §35.3）。

**血缘 / Lineage** —— ⚠️ **这个词在本书里有两种含义，必须靠上下文区分**：
* 在**容错**语境（第 10 章）里，指**对象 lineage** ——
  "这个对象是由哪个任务、哪些输入产出的"，用于**对象重建**；
* 在**实验管理**语境（第 35 章）里，指**模型血缘** ——
  "这个模型是用哪份数据、哪组参数、哪个 git commit 训出来的"。
两者是**同一种信息的不同用途**，但载体不同（前者在 raylet 内存里，后者在 checkpoint 元数据里）。

**Model Registry（模型注册表）** —— 给模型版本一个**可变的别名**
（MLflow 的 `alias`、"Production" 指针），让"生产用哪个版本"变成**一次指针切换**。
⚠️ 要点：用 **`alias` 而不是已弃用的 `stage`**；生产代码按 `@production` 读，
**永远不要硬编码版本号**。MLflow 只是把它做成了服务 ——
一个写着版本号的 `PRODUCTION` 文本文件也能干同样的事（第 35 章 §35.5）。

**OpenTelemetry / OTel** —— 可观测性的开放标准（trace / metric / log 三件套）。
在 Ray 上的**核心难点**是：`opentelemetry.context` 是**进程内**的，
**跨不过 `ray.remote`** —— 必须用 `propagate.inject()` / `propagate.extract()`
把 context 序列化成 dict **当参数传**（第 36 章 §36.3）。

**Trace Context / `traceparent`** —— W3C 标准的链路追踪载体，
内容形如 `00-<trace_id>-<span_id>-01`。
在 Web 服务里由框架自动带在请求头上；**在 Ray 里要你自己传**（同上）。

**Span** —— 追踪里的一个"时间段"，带 `trace_id` / `span_id` / `parent_span_id`，
因此能构成一棵树。**span 的粒度判据**：这段耗时会是你想单独优化的吗？
是就开，不是就用属性记下来 —— 在一个 batch 内开 1000 个 span 等于什么都看不见（第 36 章 §36.6）。

**Tail Sampling（尾部采样）** —— 在 **Collector 侧**决定"哪些 trace 留下"，
而不是在应用侧按比例采样。典型策略是"**有错误的全留 + 超时长的全留 + 其余留 1%**"。
它消掉了采样率那个取舍的大半 —— 因为**慢请求和报错请求永远留得住**（第 36 章 §36.7）。

---

## C.11 Agent 工作负载

> 这一组由**第 38 章**（Ray 与 Agent 工作负载）引入。
> 它们大多**不是新机制**，而是前 37 章机制在新负载下的**重新组合** ——
> 所以每条都指回它真正依赖的那一章。

**Agent / Agentic AI** —— 一类"一次请求要跑几十秒、要调十几次工具、要记住上下文、
瓶颈常常是**在等外部 API**"的负载。第 38 章 §38.1 给了它的五个特征：
**长时程、有状态、大部分时间在等、突发且不可预测、可训练**。
一句话立场：**"智能"来自模型与框架，"能不能跑起来"来自运行时**（第 38 章 §38.1）。

**长时程 / Long-Horizon** —— Agent 的第一特征：**一次请求内部有 5～50 次模型调用**。
后果是**单次请求的失败概率被放大**（每步 99.5% 成功，50 步后整体只剩 78%），
于是第 10 章的容错从"备选项"变成"主线"（第 38 章 §38.1 特征①、§38.7）。

**会话亲和性 / Session Affinity**（**粘性路由 / Sticky Routing**）—— 把同一个会话的
请求路由到**同一个副本**上。在 Agent 场景里它是**正确性**问题而非优化：
把用户第二轮的消息路由到没有历史的副本上，模型会答非所问、**而且不报错**（第 38 章 §38.1 特征②）。
⚠️ **Ray 2.58.0 的边界必须讲准**：`session_id` 这条**管道已经铺好**
（Python 侧 `handle.options(session_id=...)`、HTTP header `x-session-id`、
gRPC metadata，一路到副本的 `RequestMetadata.session_id`），
**但默认的 power-of-two-choices 路由器完全不读它** —— 传了也不会改变任何路由决策；
**要粘性必须显式挂 `ConsistentHashRouter`**（第 38 章 §38.3.1）。

**ConsistentHashRouter** —— Ray Serve 的**会话粘性路由器**（**experimental**）：
对 `session_id` 做一致性哈希（带 vnode 与 fallback），
位于 `python/ray/serve/experimental/consistent_hash_router.py:39`
（2.58.0 实测：`class ConsistentHashRouter(RequestRouter)`）。
⚠️ **它默认不生效，必须显式挂上**：走
`RequestRouterConfig(request_router_class="ray.serve.experimental.consistent_hash_router.ConsistentHashRouter", ...)`。
且因为 `ray/serve/experimental/__init__.py` **是空文件**，
`from ray.serve.experimental import ConsistentHashRouter` 会 `ImportError`，只能按**字符串路径**挂。
它的 docstring 自称 "**affinity-first**"：**只看 key，不看负载**（第 38 章 §38.3.1）。

**人类审批 / Human-in-the-Loop** —— Agent 在执行敏感工具前**等人批准**的环节。
⚠️ **OTel 的 GenAI 语义约定不覆盖它**：规范里**没有**属性表达"这次工具调用是否经过策略审批"
"是否有人类批准"，也没有记录"谁授权了这个 Agent"，所以**审批记录必须你自己建模**
（第 38 章 §38.6、§38.8）。

**提示注入 / Prompt Injection** —— 工具调用的参数是**模型产出的字符串**，
其中可能夹带路径穿越、注入的命令、超大 payload。
⚠️ **这是应用/模型层的问题，运行时层不负责** ——
Ray 管的是"在哪跑"，不是"能碰什么"（第 38 章 §38.6）。

**MCP / Model Context Protocol** —— Agent 调用外部工具的协议。
本书只在第 38 章 §38.8 讲了它的 **OTel 追踪约定**
（规范仓库的 `docs/gen-ai/mcp.md`，**Development** 状态、不是标准；
span 名 `{mcp.method.name} {target}`，`mcp.method.name` 为 Required）——
**协议本身本书未展开**。⚠️ **A2A 在本书里未出现，故不设条目**（不凭空造定义）。

**轨迹 / Trajectory** —— Agent 一次运行产出的完整记录：输入、每一步的模型输出、
每次工具调用的参数与返回、最终结果、（如果有）人类反馈。
⚠️ **特殊之处在于它"同时是三个东西"**：排障用的**日志**、评估用的**样本**、
训练用的**数据** —— 这正是第 12 章的 Ray Data 第一次被用来处理
"Agent 自己吐出来的东西"（第 38 章 §38.9）。

**agentic RL**（**长时程 RL / multi-turn tool-use RL**）—— Agent 的强化学习。
与 RLHF 的一句话区别：**RLHF 优化"一次回答好不好"，agentic RL 优化"一串动作好不好"**。
rollout 不再是"一次前向"，而是"跑完整个 Agent 环境、收集完整轨迹、
再用最终结果（或过程奖励）算 advantage"——采样成本高一个量级，
环境还要能重放、能并发上千实例，于是又回到第 16 章的 Ray（第 38 章 §38.10）。

**分离式服务 / Disaggregated Serving** —— 一个**总称**，其实是**三件不同的事**：
① **PD 分离**（prefill 与 decode 拆到不同副本，属**编排策略**，另见 §C.7 的 PD 分离条）；
② **KV 传输**（KV 怎么从 prefill 搬到 decode，属**数据面**，NIXL 在这里）；
③ **KV 池化 / 分层**（KV 落到显存之外、跨副本复用，属**缓存层**，LMCache 在这里）。
（第 37 章 §37.15）

**NIXL** —— NVIDIA Inference Xfer Library。**只是传输层**：既不是缓存也不是引擎，
解决的是 PD 分离里"**KV 怎么从 prefill 那台机器搬到 decode 那台**"。
Ray 的实验性张量直传模块把它当作一种 transport（第 37 章 §37.15）。

**LMCache** —— KV 的**分层缓存**：prefix caching 只在**单副本显存内**复用，
LMCache 把它扩到**显存之外、副本之外**。在 Ray 侧是一等公民
（`kv_connector: "LMCacheConnectorV1"`）（第 37 章 §37.15）。

**Dynamo**（NVIDIA 开源）—— **推理编排层**，位于引擎之上。
与 Ray Serve 的关系是**协作、也带张力**：Ray Serve LLM 的 KV-aware 路由
**复用了 Dynamo 的选择服务**，而 Dynamo **也可以完全不用 Ray 运行**
（第 37 章 §37.15）。

---

## C.12 索引（按英文字母序）

```
Accelerator Type                                          —— 加速器标签
Actor                                                     —— 角色
Actor Pool                                                —— Actor 池
ActorDiedError                                            —— actor 死亡异常
ActorHandle                                               —— Actor 句柄
ActorMethod                                               —— Actor 方法
Agent                                                     —— Agent 负载
Agentic RL                                                —— Agent 的强化学习（第 38 章）
AllReduce                                                 —— 梯度全规约
Anyscale                                                  —— Ray 的商业化公司
Async Actor                                               —— 异步 Actor
Autoscaler                                                —— 自动扩缩器
Autoscaler v2                                             —— 新一代自动扩缩器
Block                                                     —— 块（Ray Data）
Bundle                                                    —— 束（放置组）
Checkpoint                                                —— 检查点
cloudpickle                                               —— 增强版 pickle
Collective Communication                                  —— 集合通信
Compiled Graph                                            —— 编译图（DAG API）
Concurrency Group                                         —— 并发组
ConsistentHashRouter                                      —— 会话粘性路由器（Serve，experimental）
CoreWorker                                                —— 每进程的 Ray 客户端
CUDA_VISIBLE_DEVICES                                      —— GPU 可见性环境变量
Custom Resource                                           —— 自定义资源
DAG                                                       —— 有向无环图
Dask                                                      —— 图调度并行库
DataConfig                                                —— 训练数据配置
DataSourceV2                                              —— Ray Data 读取基础设施
Data Fingerprint                                          —— 数据指纹（第 35 章）
DDP / FSDP / DeepSpeed / Megatron                         —— 四种并行策略
Dependency Graph                                          —— 依赖图
Deployment                                                —— 部署（Serve）
DeploymentHandle                                          —— 部署句柄
Detached Actor                                            —— 脱离生命周期的 actor
Disaggregated Serving                                     —— 分离式服务（第 37 章）
Driver                                                    —— 驱动器
Dynamo                                                    —— NVIDIA 推理编排层（第 37 章）
Eviction                                                  —— 驱逐
ExecutionOptions                                          —— 执行期调优入口
Fault Tolerance                                           —— 容错
Flame Graph                                               —— 火焰图
Fractional GPU                                            —— 分数 GPU
Fractional Resource                                       —— 小数资源
Function Table                                            —— 函数表
Gang Scheduling                                           —— 组调度
GCS                                                       —— 全局控制存储
GBDT                                                      —— 梯度提升决策树（第 34 章）
get_gpu_ids                                               —— 取本 worker 的物理 GPU 号
gRPC                                                      —— 内部通信协议
Hash Shuffle                                              —— 哈希 shuffle
Head Node                                                 —— 头节点
Health Check                                              —— 健康检查
History Server                                            —— 集群删除后仍可查日志的组件
Human-in-the-Loop                                         —— 人类审批（第 38 章）
Hybrid Scheduling                                         —— 混合调度
Idempotency                                               —— 幂等性
Idle Reclamation                                          —— 空闲回收
Inlined Object                                            —— 内联对象
Job                                                       —— 作业
Jobs API                                                  —— 作业提交接口
KubeRay                                                   —— K8s 上的 Ray Operator
Kueue                                                     —— K8s 作业准入系统
KV-aware Routing                                          —— KV 感知路由
Lease                                                     —— 资源租约
Lineage Reconstruction                                    —— 血缘重建
LMCache                                                   —— KV 分层缓存（第 37 章）
Load Report                                               —— 负载报告
Locality-Aware                                            —— 本地性感知
Logical Resource                                          —— 逻辑资源
LightGBM                                                  —— 表格数据主力模型之一（第 34 章）
Long-Horizon                                              —— 长时程
LoRA / QLoRA                                              —— 参数高效微调
LRU                                                       —— 最近最少使用
Mailbox                                                   —— 邮箱
map_batches                                               —— 批量映射（Ray Data）
max_calls                                                 —— worker 退休前的执行次数上限
max_concurrency                                           —— 并发度
max_pending_calls                                         —— actor 邮箱排队上限
max_restarts                                              —— 重启上限
MCP                                                       —— Model Context Protocol（第 38 章）
Memory Monitor                                            —— 内存监控
memray                                                    —— Python 内存剖析
MIG                                                       —— GPU 硬件切片
mini-ray                                                  —— 本书配套教学实现
Modal                                                     —— serverless GPU 平台
Model Multiplexing                                        —— 模型多路复用
Model Registry                                            —— 模型注册表（第 35 章）
Monarch                                                   —— PyTorch 的分布式执行引擎
Named Actor                                               —— 命名 actor
Namespace                                                 —— 命名空间
NCCL                                                      —— 集合通信库
Nested ObjectRef                                          —— 嵌套引用
NIXL                                                      —— KV 传输层（第 37 章）
Node Affinity                                             —— 节点亲和
Node Label                                                —— 节点标签
Nsight Compute (ncu)                                      —— 单 kernel 剖析
Nsight Systems (nsys)                                     —— GPU 时间线剖析
NVTX                                                      —— 时间线自定义标记
Object                                                    —— 对象
Object Directory                                          —— 对象目录
object_size                                               —— 对象大小字段名
Object Manager                                            —— 对象管理器
Object Reconstruction                                     —— 对象重建
Object Store                                              —— 对象存储
OpenTelemetry / OTel                                      —— 可观测性标准（第 36 章）
ObjectLostError                                           —— 对象丢失
ObjectRef                                                 —— 对象引用
ObjectSpilling                                            —— 对象溢出
ObjectStoreFullError                                      —— 对象存储满
Observability                                             —— 可观测性
OOM（四种成因）                                                 —— 显存不足的分类
Owner                                                     —— 所有者
OwnerDiedError                                            —— 所有者进程死亡
PD Disaggregation                                         —— 预填充/解码分离
PEFT                                                      —— HF 参数高效微调库
Pinning                                                   —— 钉住
Placement Group                                           —— 放置组
Placement Group Rescheduling                              —— 放置组重建
Plasma                                                    —— 对象存储的本名
PodGroup                                                  —— 组调度的资源对象
Primary Copy                                              —— 主副本
Prompt Injection                                          —— 提示注入（第 38 章）
py-spy                                                    —— Python 采样剖析器
PyTorch Foundation                                        —— Ray 的治理基金会
Ray Client                                                —— 远端连接模式
Ray Data / Train / Tune / Serve                           —— 四个主要 AI 库
Ray Summit                                                —— 社区年会
ray.util.metrics                                          —— 应用自定义指标
RAY_AUTH_MODE                                             —— 鉴权模式
RAY_PROFILING                                             —— timeline 的前提开关
RayChannelError                                           —— 通道错误
RayDirectTransport                                        —— 直连数据面
RayExecutorV2                                             —— vLLM 的新 Ray executor
Raylet                                                    —— 每节点本地调度器
Reference Counting                                        —— 引用计数
Reservation                                               —— 预留
Resource Shape                                            —— 资源形状
retry_exceptions                                          —— 应用异常重试开关
RLlib                                                     —— Ray 的 RL 库
Runtime Context                                           —— 运行时上下文
Runtime Env                                               —— 运行时环境
Scheduler                                                 —— 调度器
Scheduling Strategy                                       —— 调度策略
Secondary Copy                                            —— 次副本
Serialization                                             —— 序列化
Session Affinity                                          —— 会话亲和性 / 粘性路由
Session Directory                                         —— 会话目录
Shared Memory                                             —— 共享内存
SkyPilot                                                  —— 多云编排
Slurm                                                     —— HPC 批调度器
Spark                                                     —— 结构化数据引擎
Spill Directory                                           —— 溢出目录
State API                                                 —— 状态查询接口
Stopper                                                   —— 试验停止条件
System Config                                             —— 系统配置
Task                                                      —— 任务
Task Events                                               —— 任务事件时间戳
Threaded Actor                                            —— 线程化 actor
Timeline                                                  —— 时间线
Tail Sampling                                             —— 尾部采样（第 36 章）
Token Authentication                                      —— token 认证
Topology-Aware                                            —— 拓扑感知
torch.distributed                                         —— PyTorch 集合通信
torchrun                                                  —— PyTorch 启动器
Train V2                                                  —— Ray Train 的新版本
Trajectory                                                —— 轨迹（第 38 章）
Trial                                                     —— 试验
verl / OpenRLHF / SkyRL / NeMo-RL / AReaL / slime / miles —— RL 框架
vLLM                                                      —— 高吞吐推理引擎
Volcano                                                   —— K8s 批调度器
Worker                                                    —— 工作进程
Worker Pool                                               —— worker 池
WorkerCrashedError                                        —— worker 崩溃
Workflows                                                 —— 已移除的工作流库
ZeRO                                                      —— DeepSpeed 分片策略
Zero-Copy                                                 —— 零拷贝
Zero-CPU Task                                             —— 零 CPU 任务
碎片化 / Fragmentation                                       —— 显存分配碎片
```
