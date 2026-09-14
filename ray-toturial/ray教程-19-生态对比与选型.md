仓库地址：https://github.com/hhk-png/cycle-agent

# 第 19 章：生态对比与选型

> 本章目标：把「该不该用 Ray」变成一个可回答的问题。
> 读完你应该能做三件事：**分清楚哪些技术是同一层的、哪些不是**、
> **在 RL/推理/数据/训练四条线上知道现在的主流答案**、
> **照着决策树给出一个有理由的选型结论**。
>
> 本章刻意**不站队**。凡是取舍，都把两边的代价写出来。

---

## 19.1 先分清层次：很多"对比"其实是错位的

技术选型最常见的错误，是把**不同层**的东西拿来比。先把层次摆清楚：

```
┌──────────────────────────────────────────────────────────────────┐
│ 5. 平台层      Databricks / SageMaker / 各家 AI 云               │
├──────────────────────────────────────────────────────────────────┤
│ 4. 应用框架    verl / OpenRLHF / SkyRL / NeMo-RL / vLLM / DeepSpeed│
├──────────────────────────────────────────────────────────────────┤
│ 3. 编排与运行时 Ray / Spark / Dask / Monarch / torchrun           │
│                (任务的"生命周期"归谁管)                            │
├──────────────────────────────────────────────────────────────────┤
│ 2. 数据面      NCCL / RCCL / RDMA / 对象存储 / 共享内存            │
├──────────────────────────────────────────────────────────────────┤
│ 1. 集群与资源  Kubernetes / Slurm / 云 VM / SkyPilot / Modal      │
└──────────────────────────────────────────────────────────────────┘
```

**Ray 主要在 3 层**，并且正在把一部分职责让给第 2 层（见 19.4 的 vLLM 案例）。
下面每一节的对比，都会先说明"它和 Ray 是不是同一层"。

---

## 19.2 vs Spark：互补，而且在互相靠拢

这一组是最多人问、也最容易问错的。它们**处理的是不同形状的问题**：

| | Spark | Ray |
|---|---|---|
| 抽象 | 表（DataFrame）/ 算子 | 函数（task）/ 类（actor）/ 对象 |
| 擅长 | ETL、SQL、特征工程、结构化数据 shuffle | 任务并行、有状态组件、RL、仿真、HPC |
| 调度粒度 | 阶段（stage）级，按 DAG 推进 | 任务级，毫秒级 |
| 状态 | 表是数据，算子无状态 | actor 是一等公民 |
| 失败代价 | 重算一个 stage/分区 | 重算一个 task（或 lineage 重建） |
| 生态 | 极成熟：SQL 引擎、Catalog、BI 全链路 | 较新：AI 负载的编排层 |

**判断标准很简单**：

* 你的计算能不能表达成"对一张（或几张）表做算子"？能 → **Spark/DuckDB/Polars 更合适**。
  Ray Data 在"任意 SQL 都更快"这件事上并不占优，这是社区公认的批评（第 1 章引用过）。
* 你的计算是"很多个函数 + 一些长期活着的组件"？→ **Ray 更合适**。
* 两者都占？→ 见下面的"靠拢"部分。

### 二者的关系正在从"竞争"变成"分工"

这不是本书的推测，有具体证据：

* **Databricks Runtime ML 15.0+ 预装 Ray**（在这之前需要
  `%pip install ray[default]>=2.3.0`），把 Ray 做成了运行时的**一等公民**
  （Databricks 文档 `machine-learning/ray/ray-create` 与
  `machine-learning/ray/connect-spark-ray`）。
* **Ray 集群由 Spark 驱动创建**：`ray.util.spark.setup_ray_cluster(...)` /
  `shutdown_ray_cluster()`，以及 2.9.0 起的 `setup_global_ray_cluster`。
  创建出来的 Ray 集群只对当前 notebook 用户可用，notebook 脱离或空闲 30 分钟后自动关。
* **数据可以在两者之间不落地传递**：`ray.data.from_spark(df)` 直接把 Spark
  DataFrame 读进来（DBR ML 15.0+ 的**内存内传输**，需要先把 Spark 配置
  `spark.databricks.pyspark.dataFrameChunk.enabled=true` 设好再启动集群）。
  注意自动扩缩的 Spark 集群要传 `use_spark_chunk_api=False`，否则 executor 一退
  cache 就没了。
* **反向（Ray → Spark）要落盘**：写 Unity Catalog 走
  `ray.data.Dataset.write_delta(path, catalog=DatabricksUnityCatalog(...))` ——
  ⚠️ **没有 `write_databricks_table` 这个方法**（2.58 的 `dataset.py` 里查不到），
  UC 支持是 `write_delta` 的 `catalog=` 参数给的；
  非 UC 环境就用 `write_parquet` + `spark.read.parquet`。
* **Ray 侧也在往上长**：**Ray 2.57 落地的是 Catalog 抽象**
  （`ray/data/catalog.py` 的 `Catalog` / `DatabricksUnityCatalog`，
  先接在 `write_iceberg(catalog=...)` 上；2.56 里这个文件还不存在 ——
  `ray-2.56.0` 取 `python/ray/data/catalog.py` 返回 **404**）；
  而 **`write_delta` / `DeltaDatasink` 是 2.58 才进 `Dataset` 的**
  （`dataset.py` 里的 `@PublicAPI(stability="alpha") def write_delta(`；
  在 `ray-2.57.0` 的同名文件里 `grep -c "def write_delta"` = **0**，
  而对照组 `def write_parquet` = 1 —— 证明文件确实取到了）。
  支持 `APPEND` / `OVERWRITE`，worker 写 Parquet、driver 用 `deltalake` 库提交一次事务，
  **带 Unity Catalog 支持**。同一版本还把 DataSourceV2 默认打开（#64821）与
  Hash Shuffle V2 换了实现（`#63598` 引入、2.58 支持 `join`；
  **与 `DataSourceV2` 是两件事**，别混成一条）。

> **要不要写"Ray 2.57 增加 write_delta 与 Unity Catalog 集成"？**
> 可以，但要标注：`write_delta` 的实现来自 PR #64923，Unity Catalog 的 Catalog 抽象
> 来自第三方对 2.57 发布内容的整理，**本书未逐字核对官方 release notes**。
> 另外，任务书里提到的 `databricks.ray.data.from_spark` 这个命名空间**本书未确认**——
> 检索到的 Databricks 文档用的是 `ray.data.from_spark`。

**结论**：不是二选一。Spark 做结构化数据的重活，Ray 做非结构化的编排与有状态组件，
两者之间的桥已经在官方文档里了。**"用 Spark 还是用 Ray"通常是个假问题，
真问题是"这一段该放在哪一侧"。**

---

## 19.3 vs Dask：同样的出发点，不同的重心

Dask 和 Ray 都诞生于"Python 的并行计算需要一个通用答案"这个动机，但重心不同：

| | Dask | Ray |
|---|---|---|
| 核心抽象 | 任务图（graph）+ 集合类型（`dask.array` / `dask.dataframe` / `dask.bag`） | task / actor / object |
| 用户心智 | 尽量贴近 **numpy / pandas** 的 API | 尽量贴近**普通 Python 函数与类** |
| 调度模型 | 图调度（lazy，`compute()` 才执行），单调度器 | 任务级调度，GCS + raylet 两级，带对象存储 |
| 有状态组件 | 没有一等公民（要靠 `Actor`/`Client` 自己拼） | **actor 是一等公民** |
| 对象存储 | 无（数据在 worker 内存里流动） | 有（**共享内存对象存储**，引用计数、溢出、本地性）。⚠️ 旧称 **Plasma** —— 那是 Ray 1.x 的独立存储进程，2.x 已并入 raylet；现在只有 `ray memory --stats-only` 的 help 文案还留着旧词（第 3 章 §3.6、第 7 章） |
| 容错 | 图重算 | 任务重试 + lineage 重建 + actor 重启 |

**选 Dask 的场合**：

* 你的代码已经是一堆 pandas/numpy 操作，想原样放大到集群；
* 你的负载是"大数组/大表上的一串算子"，图是静态的；
* 你不想引入对象存储和 actor 这两层心智负担。

**选 Ray 的场合**：

* 你的负载里**有需要长期活着、持有状态的东西**（模型、连接池、仿真环境、参数服务器）；
* 你的依赖关系是**动态的**（运行时才知道下一个任务是谁）；
* 你需要细粒度的资源声明（GPU 小数、自定义资源、放置组）。

**Dask-on-Ray**：存在一条把 Dask 跑在 Ray 上的路径（`ray.util.dask`），
让 Dask 的图调度用 Ray 当执行后端。**本书未确认它在 Ray 2.58 上的维护状态与适用边界**，
所以只做定性描述，不给版本号也不给性能数字。实践建议是：如果你要用 Dask，
就老老实实用 Dask 自己的调度器；要用 Ray，就直接写 Ray——**混着用的调试成本通常大于收益**。

---

## 19.4 vs torchrun / torch.distributed：分工，不是替代

这一组的关系最清晰，也最能说明 Ray 在 AI 栈里的真实位置：

```
        Ray 负责的                      torch.distributed 负责的
┌───────────────────────────┐    ┌──────────────────────────────────┐
│ 进程编排(在哪台机器起几个) │    │ 集合通信(all-reduce / all-gather) │
│ 资源申请(几张卡、什么型号) │    │ NCCL / RCCL 通信组                │
│ 放置(放置组、拓扑感知)     │    │ 梯度同步、张量并行、流水线并行     │
│ 生命周期(重启、回收、容错) │    │                                  │
└───────────────────────────┘    └──────────────────────────────────┘
```

**判断标准**：

* 单个 8 卡 DDP 训练、机器固定、不需要弹性 → **直接用 torchrun**。
  Ray 在这里加的价值很小，还多一层调试面（第 1 章已经写过这条）。
* 多机、多任务、要弹性、要跟推理/数据/调参串起来 → **Ray**。
  Ray Train 的定位就是"把 torchrun 那套 `MASTER_ADDR` / `RANK` / `WORLD_SIZE`
  的活接过来"，训练脚本本身仍然是 PyTorch。

### 最好的证据：vLLM 正在把 Ray 缩到"放置层"

vLLM 的 RFC #35848（由 Ray 团队提交，标题 *Revamp Ray Distributed Executor Backend*）
提出用新的 `RayExecutorV2` 取代基于 Ray compiled graph 的旧 Ray executor。
新实现的关键点：

* **继承 `MultiprocExecutor`**，复用它的**消息队列控制面**与
  **`torch.distributed` / NCCL 数据面**；
* worker 仍然作为 **Ray actor** 被创建、被放进放置组的 bundle 里，
  bundle 分配按"**driver 节点优先**"排序，让 rank 0 和 executor 同机；
* worker 死亡检测用 `ObjectRef` 哨兵 + `ray.wait()`，关停用 `ray.kill()`，
  driver 挂掉时靠 GCS 的 actor 所有权清理；
* **热路径上没有 `ray.remote` 调用**；
* 迁移是分阶段的：Phase 1 用 `VLLM_USE_RAY_V2_EXECUTOR_BACKEND` 特性开关
  （默认 `False`），Phase 2 默认打开，Phase 3 移除旧的
  `vllm/v1/executor/ray_executor.py`。据 Ray 侧 issue #62505，新的后端
  **不再使用 compiled graph**，旧 executor 进入弃用路径；
  实现见 vLLM PR #36836，官方支持计划在 **vLLM 0.20.0**。

**这段事实说明什么**：Ray 在推理栈里的角色**收缩到"进程编排 + 资源感知放置"**——
也就是上面那张图左半边。数据面交给 NCCL 不是 Ray 的失败，而是**分层正确**的体现：
Ray 从来不做集合通信，它做的是"把正确数量的进程放到正确的位置上"。

---

## 19.5 vs PyTorch Monarch：新玩家，值得盯，但现在还不能替

**PyTorch Monarch** 由 Meta 的 PyTorch 团队在 **2025 年 10 月的 PyTorch Conference**
上发布（会议日期 10 月 22–23 日，旧金山；演讲 *Monarch: A Distributed Execution Engine
for PyTorch*，讲者是 Meta 的 Colin Taylor 与 Zachary DeVito）。它的设计要点：

| 维度 | Monarch 的做法 | 和 Ray 的差别 |
|---|---|---|
| 编程模型 | **single-controller**：一份脚本编排所有资源，不用管 rank/barrier | Ray 也是 single-controller 思路（driver），但抽象是 task/actor |
| 前端/后端 | Python 前端 + **Rust 后端** | Ray 是 Python 前端 + C++ 后端 |
| 核心库 | 底层是 Rust 的 **hyperactor**（actor 消息传递 + 监督），其上是 `hyperactor_mesh`（"向量化"的 actor mesh） | Ray 的对应物是 core worker + raylet + GCS |
| 数据面 | **原生 RDMA**：控制面（消息）与数据面（RDMA 传输）分离，提供 RDMA buffer API 与 RDMA 远程文件系统 | Ray 有 Ray Direct Transport (RDT) 等方向，但推理/训练的数据面通常直接交给 NCCL |
| 可扩展性手段 | 多播树（避免单机瓶颈）+ multipart messaging（避免拷贝、让控制面不进关键路径） | 对象存储 + 本地性调度 |
| 上层生态 | TorchForge（构建在 Monarch 原语上的 RL 框架） | verl / SkyRL / OpenRLHF / Ray Train / Ray Serve… |

**必须如实说的三件事**：

1. **它仍然很新**。2025 年 10 月才发布，处在快速演进期，**API 会变**，
   这一点和 Ray 2.x 的向后兼容策略完全不是一个量级。
2. **生态与成熟度远不如 Ray**。Ray 有 39,000+ stars、覆盖训练/推理/数据/调参/服务的
   完整库栈、PyTorch Foundation 治理、以及一大批生产用户（见 19.10）；
   Monarch 目前主要是 Meta 内部 + 早期采用者的规模。
3. **任务书里提到的"能在单机 pdb 里 step through RL loop"这个卖点，本书未确认**
   （检索到的官方博客与会议介绍里没有找到这句表述）。Monarch 官方强调的
   single-controller 带来的可调试性是**方向性的说法**，具体到 pdb 单步调试的体验，
   请以官方文档为准。

**结论**：Monarch 值得关注，因为它代表"PyTorch 原生"这条路线对 Ray 的位置发起的挑战；
但现在把它作为生产选型是在赌 API。**如果它未来成熟，受益的是整个生态——
你学会的"single-controller + actor mesh + 资源感知放置"这套心智是可以迁移的。**

---

## 19.6 vs SkyPilot / Modal：不同层，可以叠加

这两个经常和 Ray 一起被提起，但它们**不在第 3 层**：

| | 层次 | 它解决的问题 | 和 Ray 的关系 |
|---|---|---|---|
| **SkyPilot** | 第 1 层（集群与资源） | 多云/多集群编排：按价格与可用性挑 spot 实例、跨云拉起集群、自动停掉了省钱 | **在它上面跑 Ray**：SkyPilot 负责把机器弄来，Ray 负责在上面调度任务 |
| **Modal** | 第 1/5 层（serverless GPU 平台） | 把"跑一个带 GPU 的函数"变成 `@app.function(gpu="A100")`，按秒计费，无需管集群 | **替代关系**：如果你的需求就是"跑一批函数"，Modal 这类 serverless 平台更省事；需要长期驻留的 actor 池与复杂依赖图时，Ray 表达力更强 |

一个具体的证据点：有资料指出 **verl 在 Kubernetes 上的路径是走 SkyPilot 手动
`ray start`，而不是 KubeRay**（与 NeMo-RL 走 KubeRay Operator 形成对照，见 19.8 的表）。
这说明"用 SkyPilot 起 Ray 集群"是真实存在的生产路径。

**本书未确认** SkyPilot / Modal 是否有与 Ray 的**官方集成页面**，
所以此处只做定性描述，**不给任何性能或成本数字**。凡是你看到
"SkyPilot 让 Ray 成本降低 N%"这类说法，请回去找原始出处。

---

## 19.7 vs Kubernetes 原生方案：职责重叠与真实摩擦

Ray 自带调度器。Kubernetes 也自带调度器。**两个调度器管同一批资源，摩擦是必然的。**
这一节的证据来自 KubeRay / Kueue / Volcano 的官方文档与 issue。

### 生态组件各自的职责

| 组件 | 层次 | 它做什么 |
|---|---|---|
| **KubeRay** | Operator | 用 CRD（RayCluster / RayJob / RayService）管理 Ray 集群生命周期，含 in-tree autoscaler 的对接 |
| **Kueue** | 准入（admission） | 作业排队与配额：决定"这个作业什么时候可以开始"，gang（全有或全无）准入、优先级与抢占；对 RayJob/RayCluster/RayService 有原生支持 |
| **Volcano / YuniKorn / scheduler-plugins** | Pod 调度 | 决定"pod 落在哪个节点"，提供 gang scheduling（PodGroup）；KubeRay 通过 `batchScheduler.name`（如 `volcano`）启用，KubeRay v1.5.1 起 RayJob/RayCluster/RayService 都支持 |
| **裸 K8s** | 调度 | 默认调度器，逐 pod 调度，没有 gang 语义 |

### 四个真实摩擦点

1. **双 autoscaler**。Ray 的 in-tree autoscaler 会按任务压力加 worker pod，
   而 K8s 侧（HPA/Karpenter）也会动。Kueue 的解法是把 RayCluster 当成
   **弹性作业**（需要开 `ElasticJobsViaWorkloadSlices` 这个 **alpha** feature gate，
   打上 `kueue.x-k8s.io/elastic-job: "true"`，并设 `enableInTreeAutoscaling: true`）：
   Kueue 管配额与准入，in-tree autoscaler 在**已准入的配额内**扩 worker。
   局限：该弹性路径只适用于 **ray image < 2.47.0**（Kueue 文档口径）。
2. **gang scheduling 与渐进扩容天然冲突**。Volcano/Kueue 要求"一次给齐"，
   而 autoscaler 是"一点点加"。KubeRay #697 在最初调研集成就指出：
   replica 变化时需要更新 PodGroup，有些调度器不支持动态成员。
   相关缺陷见 KubeRay #4360（不同 worker group 用不同优先级 + Volcano 时，
   自动扩容出来的 pod 会继承错误的 priority class）。
3. **每个 pod 只能被一个调度器处理**（由 `schedulerName` 决定）。
   设置漏了就会绕过你想要的调度器；而 **Kueue / Volcano / YuniKorn 的 GPU 配额
   彼此不通**，各承诺一份 GPU 却只有一份物理卡，是会真的死锁的。
   **建议显式划分 GPU 容量**，别让多套配额系统互相超发。
4. **head 节点被意外调度**。官方推荐给 head pod 设 `num-cpus: "0"`，
   避免 Ray 的工作负载跑到 head 上跟 GCS 抢内存/CPU。
   顺带一提，`--num-cpus=0` 的节点会让 dashboard 的 `cpu_percent()` 里有除零路径
   （issue #63729，虽然被 catch 了但会刷日志）。

**结论**：KubeRay 是 Ray 上生产的**默认答案**，但请把"谁负责准入、谁负责放置、
谁负责扩容"三件事在架构图上画清楚，否则一定会在生产上遇到"我的 pod 为什么不调度"
这类问题。想省掉这层复杂度，就必须接受 Ray 自带的 autoscaler 与裸 VM/云的方案
（放弃 K8s 的生态）。

---

## 19.8 RL 框架生态地图：几乎都在 Ray 上

这是 Ray 在 AI 时代最稳固的地盘。Anyscale 的 LLM-RL 页面把
**verl、SkyRL、SLIME、AReaL、NeMo-RL、OpenRLHF** 都列为构建在 Ray 上的库；
vLLM 的 RFC 也把 verl / SkyRL / AReaL 归为同一类"generate 消费者"。

> ⚠️ **来源层级说明（与第 16 章 §16.8 口径一致）**：
> 上面这句是 **Anyscale 自家页面的说法**，不是本书独立核实过的结论。
> 对其中几个框架（尤其 **AReaL**），第 16 章 §16.8 的表格标的是
> **"Ray 作为编排层未在本次检索中确证"**。
> **两处说的是同一件事：来源是厂商页面，本书没有逐仓库核对。**
> 引用这些数字/定位时，请当成"厂商陈述"，不要当成第三方基准
> （与 §19.10、第 20 章 §20.3 的口径一致）。

| 框架 | 出身 | 训练后端 | 推理后端 | 同步/异步 | 部署方式 | 定位 |
|---|---|---|---|---|---|---|
| **verl** | 字节跳动 / Seed | FSDP、FSDP2、Megatron-LM | vLLM、SGLang、TensorRT-LLM | 支持全异步（HybridFlow，多流水线） | **SkyPilot 手动 `ray start`**（不走 KubeRay） | 推理模型复现的事实标准；671B 级规模；RDMA 走 CheckpointEngine + NIXL |
| **OpenRLHF** | 社区 | DeepSpeed ZeRO-3 | vLLM | 支持异步 agentic RL | — | Ray + vLLM 原生；colocate/分离部署组合最灵活；适合 7B–34B 与研究原型 |
| **SkyRL** | NovaSky / UC Berkeley | （基于 Ray 的 worker 组） | 多后端 | 支持长时程 agent RL | — | 最早做开源长时程 agent RL 的一批；实现了 Tinker API |
| **NeMo-RL** | NVIDIA | DTensor（FSDP2/TP/SP/CP）+ Megatron-Core | vLLM、Megatron 原生 | 异步 GRPO | **KubeRay Operator**（需预装） | 用于 Nemotron 系列后训练；被描述为 verl 与 SkyRL 的"架构祖先"；支持 GB200 |
| **AReaL** | 蚂蚁 | — | — | **以异步为核心设计** | — | 最早的异步 RL 开源实现之一；把 actor 与 learner 资源拆开 |
| **slime** | 智谱 | Megatron | **SGLang（只支持这一种组合）** | 训练/推理分离 + 参数同步 | — | 用于 GLM 系列内部训练；结构极简：`RayTrainGroup` + `RolloutGroup` 两个 worker |
| **miles** | RadixArk（slime 的生产化分支） | Megatron-LM（默认）、FSDP2 可选 | SGLang | **完全异步**（rollout 流式入队，trainer 按自己节奏消费） | Ray actor 编排（trainer rank、SGLang server、路由代理、异步 rollout worker 都是 actor） | PyTorch 官方博客介绍（2026-06-30），v0.1 发布于 2026-08-18；MoE 用 R3 保持路由一致；支持 BF16/FP8/MXFP8/NVFP4/INT4-QAT |

**几点值得单独说的**：

* **colocate 还是分离**是这一层的核心权衡。verl 的 colocate 是把多个角色塞进同一个
  Ray actor 实例（`create_colocated_worker_cls` / `create_colocated_worker_cls_fused`
  生成 WorkerDict / FusedWorker）；分离则是每个角色一个 Ray actor，由 Ray 调度。
  有资料称同进程通信在某些场景下快约 10 倍且内存碎片更少——**这是二手说法，
  本书未复现**。
* **都构建在 Ray 上不等于都长得一样**。它们的差别主要在"训练与推理怎么共享 GPU"
  （时间片 vs 空间分区）、"同步还是异步"、"支持哪几种算法"。
  算法的差距其实最小：GRPO / GSPO / PPO / REINFORCE++ 这几家基本都有。
* **选型的实用建议**：100+ GPU 的生产后训练看 verl；70B+/MoE/GB200 且要
  SFT→RLHF 一条龙看 NeMo-RL；研究原型与消费级卡看 OpenRLHF；
  agentic RL 看 SkyRL / OpenRLHF / RAGEN（构建于 verl）；
  要"只用 SGLang + Megatron"的极简结构看 slime / miles。
* 📌 **2026 年这条线上分出了一个新档：agentic RL**（多轮、工具调用、长时程）。
  它不是"RLHF 换了个算法"，而是**优化目标换了** —— RLHF 优化"一次回答好不好"，
  agentic RL 优化"一串动作好不好"，于是 rollout 从"一次前向"变成
  "跑完整个 Agent 环境再收集轨迹"，采样成本高一个量级、环境要能重放与并发。
  **这一档的框架清单与选型判据见第 38 章 §38.10**
  （verl / SkyRL / OpenRLHF / **ART**（OpenPipe）/ **Agent Lightning**（微软）/
  **RAGEN** —— 与 §16.12、§38.10 三处口径一致）。
  > ✅ **仓库地址的提醒（已核实）**：第 38 章 §38.10 说 **verl 的仓库已从
  > `volcengine/verl` 迁到 `verl-project/verl`**（旧地址 301 跳转）。
  > 第八轮实测复现了这个跳转：
  >
  > ```bash
  > curl -sI https://github.com/volcengine/verl | head -3
  > # HTTP/1.1 301 Moved Permanently
  > # Location: https://github.com/verl-project/verl
  > ```
  >
  > （对照组：`curl -sI https://github.com/ray-project/ray` 返回 `200` ——
  > 证明不是网络问题。）
  > 本书第七轮曾因为抓取被拦把它标成"未复核"，第八轮补上了。

---

## 19.9 选型决策树

```
你的负载是什么形状?
│
├─ 结构化数据(表)上的算子/ETL/SQL  ──────────────▶ Spark / DuckDB / Polars
│    └─ 但后面还要接 RL 或大模型后训练? ──────────▶ Spark 做前半段 + Ray 做后半段
│                                                  (ray.data.from_spark / write_delta(catalog=DatabricksUnityCatalog(...)))
│
├─ 一堆 numpy/pandas 语义的算子,lazy 图,无状态  ─▶ Dask
│
├─ 单个大任务,机器固定,无弹性需求
│    ├─ 8 卡以内 DDP 训练  ──────────────────────▶ torchrun
│    └─ 单机大数组  ──────────────────────────────▶ numpy / multiprocessing
│
├─ 很多中小任务 + 需要弹性 + 有状态组件  ─────────▶ Ray Core
│    ├─ 只是"跑一批带 GPU 的函数",不想管集群 ──▶ Modal 之类的 serverless GPU 平台
│    └─ 要求跨云/spot 省钱 ──────────────────────▶ SkyPilot 起集群 + Ray 调度
│
├─ 数据管道(非结构化/多模态/流式)  ──────────────▶ Ray Data
├─ 分布式训练编排(多机、多任务、要弹性)  ────────▶ Ray Train
│    └─ 通信仍然交给 torch.distributed + NCCL
├─ 超参搜索 / 试验管理  ────────────────────────▶ Ray Tune
├─ 在线推理服务(要 autoscaling/多模型组合)  ─────▶ Ray Serve(+ vLLM)
├─ LLM 后训练(RL)  ────────────────────────────▶ verl / NeMo-RL / OpenRLHF / slime / miles
│
└─ 已经全押 Kubernetes,不想多一套调度器?
     ├─ 接受摩擦,要 gang scheduling/配额  ───────▶ KubeRay + Kueue(+ Volcano)
     └─ 不想引入额外调度层,负载也简单  ──────────▶ 裸 K8s + 自己写 Deployment/Job
```

**用法**：从最上面往下走，**遇到第一个匹配就打住**，别往下看。
选型的成本不在"选错最强的那个"，而在"选了之后多养一套系统"。

---

## 19.10 企业采用情况：只写有出处的

这一节是**信息卫生**的示范。Ray 的采用情况里有很多"人人都知道"的说法，
其中相当一部分找不到一手来源。

### 可以写的

* **OpenAI 用 Ray 支撑 ChatGPT 训练**：这是**二手来源**。Ray/Anyscale 长期
  在官方材料与演讲里引用这一案例，但本书**未在 OpenAI 官方文档或博客里找到
  一手声明**。所以正确的写法是"据 Anyscale 一侧的公开材料"，
  而不是"OpenAI 官方表示"。
* **Ray Summit 2026 的公开采用方**：官方 recap 博客标题为
  *"Ray Summit 2026: Physical AI, RL, and the infrastructure that runs them all"*
  （Anyscale 博客，2026-09-08），会议在 **2026 年 8 月 24–26 日**于
  旧金山 Marriott Marquis 举行，规模在 2,000 人以上，与首届 **vLLM Conference**
  （由 Inferact 主办）同期同票。
  与会/演讲的公司包括 **NVIDIA、Apple、Spotify、Discord、Netflix、Recursion、
  Torc Robotics、Bedrock Robotics** 等。
  ⚠️ **本书未能直接打开该页面**（抓取被网络策略拦截），上述名单来自检索到的
  二手转述与该会议公开议程页，**请以官方 recap 原文为准**。
* 同上来源里被引用的具体生产数字（**均为二手转述，本书未逐字核对原文**）：
  Spotify 的 Hendrix 平台每月跑 100 万+ GPU 小时；Capital One 把超参优化从
  天级压到小时级；Torc Robotics 把 RL 流水线并到一个 Ray 集群后数据吞吐提升 20 倍；
  NVIDIA 用 Ray Core 在 3,000+ GPU 上训练 Nemotron 3 Ultra，
  拓扑感知放置让 RL 迭代吞吐提升 13%。**不要把这些数字当基准**，
  它们没有说明硬件配置与对照基线。

### 明确不写的

**未确认的（如 Anthropic / Meta / 字节 / 阿里官方声明使用 Ray）一律不写。**
理由很简单：这些公司里有的确实在内部用（比如在框架层面，verl 来自字节、
slime 来自智谱、AReaL 来自蚂蚁），但"框架由某公司开源"和
"某公司官方声明其生产系统使用 Ray"是两件不同的事实。
把前者写成后者，就是编造。

**一条可以自查的经验**：如果一条"某大厂用 Ray"的信息，你只能找到
PPT 截图、播客口述或者二手转述，找不到公司自己的工程博客或文档，
那就标注来源层级，或者不写。

---

## 19.11 本章小结

* **先分层再比较**。Ray 在第 3 层（编排与运行时）；Spark 在第 3 层但重心在结构化数据；
  Dask 在第 3 层但重心在集合语义；torchrun 在第 2/3 层之间且只做进程与通信；
  SkyPilot/Modal 在更下层。
* **vs Spark**：互补，而且在互相靠拢（Databricks 预装 Ray、
  `ray.util.spark.setup_ray_cluster`、`ray.data.from_spark`、
  Ray 侧 `write_delta` 与 Unity Catalog Catalog 抽象）。
* **vs Dask**：Dask 的集合 API 更贴近 pandas/numpy 用户；Ray 有 actor 与对象存储。
  两者混用（Dask-on-Ray）存在，但调试成本通常大于收益；本书未确认其在 2.58 的状态。
* **vs torchrun/torch.distributed**：Ray 管**编排与放置**，集合通信归 NCCL。
  vLLM 的 `RayExecutorV2`（RFC #35848 / PR #36836）是这条分工线的教科书级证据。
* **vs Monarch**：single-controller + Python 前端 / Rust 后端（hyperactor）+
  原生 RDMA，值得盯；但**很新、API 会变、生态远不如 Ray**，
  它宣传中的 pdb 单步调试说法本书未确认。
* **vs SkyPilot / Modal**：不同层，可与 Ray 组合（SkyPilot 起集群 + Ray 调度），
  或有替代关系（纯函数式 GPU 任务可用 serverless 平台）。未确认是否存在官方集成页面。
* **vs K8s 原生**：KubeRay 是默认答案，但要处理双 autoscaler、gang scheduling 与
  渐进扩容的冲突、每个 pod 只能有一个调度器、GPU 配额互不相通、head 节点别跑负载。
* **RL 生态**：verl / OpenRLHF / SkyRL / NeMo-RL / AReaL / slime / miles 几乎都在 Ray 上
  （⚠️ **来源是 Anyscale 页面，非本书独立核实**，见 §19.8 的说明），
  差异在同步/异步、colocate/分离、训练与推理后端，而不是在算法列表上。
* **2026 年这条线上多了一档"agentic RL"**（多轮、工具调用、长时程）：
  玩家是 verl / SkyRL / OpenRLHF / **ART**（OpenPipe）/
  **Agent Lightning**（微软）/ **RAGEN**。它不属于本章任何一张表 ——
  **单独看第 38 章 §38.10**（与 §16.12 口径一致）。
* **采用情况只写有出处的**，并且**明确标注二手来源**。

下一章不再讲"怎么用"和"用哪个"，而是把所有线索收在一起：Ray 现在处在什么位置、
它在推理栈里收缩、在训练与数据侧加深，以及 2026 年之后可能往哪走。
