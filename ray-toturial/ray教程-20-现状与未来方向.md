仓库地址：https://github.com/hhk-png/cycle-agent

# 第 20 章：现状与未来方向

> 本章目标：把 2026 年这个时间点上「Ray 处在什么位置、往哪走」讲清楚，
> 并区分**已发生的事实**、**官方路线图**、以及**我的判断**。
> 这一章的所有事实都标注了来源；查不到的一律写"未确认"。

---

## 20.1 三条主线

如果只看一句话，2026 年的 Ray 正在同时发生三件事：

1. **在 LLM 推理栈里，它的位置从"执行引擎"收缩为"放置与调度层"**；
2. **在训练 / 后训练 / 数据侧，它的位置反而更深了**（RL 框架几乎全是它的用户）；
3. **治理与商业化分离**：项目进了 PyTorch Foundation，商业公司被收购。

这三件事方向不同，但都合理 —— 理解它们，就能理解 Ray 未来几年的定位。

> 📌 **还有第四条正在成形的线**：**Agent / agentic 工作负载** ——
> 长时程、多轮、要调工具、大部分时间在等外部系统。它既不属于"推理栈"也不
> 属于"训练栈"，但它把两边的机制都重新组合了一遍（会话亲和路由 + 工具沙箱 +
> 轨迹数据 + agentic RL）。本章 §20.5 的"在途"里点了它，
> **完整讨论见第 38 章**。

---

## 20.2 主线一：推理栈里的角色收缩（最重要的信号）

### 事实：vLLM 正在替换基于 Ray compiled graph 的 executor

vLLM 的 RFC **#35848 "Revamp Ray Distributed Executor Backend"**（**由 Ray 团队提交**，
不是外部批评）提出用 **`RayExecutorV2`** 取代旧的 Ray executor：

| 维度 | 旧（compiled graph） | 新（RayExecutorV2） |
|---|---|---|
| 继承了谁 | Ray DAG/compiled graph | `MultiprocExecutor` |
| 控制面 | Ray 的 DAG 执行 | **MessageQueue**（共享内存，跨节点退回 ZMQ/TCP） |
| 数据面 | Ray 的对象传输 | **`torch.distributed` / NCCL** |
| 热路径上的 `ray.remote` | 有 | **没有** |

迁移计划是三阶段（Phase 1 feature flag → Phase 2 默认 → Phase 3 强制并删除
`ray_executor.py`）；Ray 侧在 issue #62505 跟踪。

**保留 Ray 变体的理由被收敛到三条**：跨节点并行的简便性、
GPU worker 的细粒度放置、资源感知调度。

### 事实：同时，Ray 自己也在加强 LLM 服务这一层

* Ray Serve LLM 把 vLLM 作为**默认引擎**（`ray[llm]` 会 pin 对应版本，
  2.58 与 vLLM 0.26 的兼容性见 Anyscale release notes —— ⚠️ 具体版本号我未能逐条核实）；
* **SGLang 正在成为一等引擎**（官方 roadmap issue #62796，包含 zero-copy weight transfer 等）；
* **KV cache 成为一等对象**（issue #64389 的四个 milestone）：
  M1 聚合部署的 KV-aware routing → M2 高级形态（LoRA、数据并行、PD 分离、多模态）
  → M3 分层 KV cache 管理 → M4 hybrid KV cache。
  2.58 完成了 KV-cache + token-aware 路由：engine replica 发布 vLLM 原生 KV cache 事件，
  router 维护全局 block 索引，打分时同时估算**剩余 prefill 工作量**与**各 replica 的 decode 负载**，
  合成一个 "token load" 估计 —— 这样避免了纯 cache-affinity 路由的 request herding，
  也不需要显式 session ID（打分用 NVIDIA Dynamo 的 selection service，
  跑在 Ray Serve LLM 的 ingress replica 进程内）。
  **已知未解难题**：无法预测输出长度的请求，token load 很难估计。

### 判断（不是事实）

**这不是 Ray 的失败，而是分工的成熟。** 数据面本来就该由 NCCL 这类专用库负责；
Ray 的价值在"谁在哪里、什么时候起、给多少资源"。
真正需要担心的是**如果连"调度与放置"都被 K8s/专门的服务框架替代**，
Ray 在推理栈里的位置会进一步变薄。

**证据的另一面**：Ray 在**后训练**（RL）这一侧的位置非常稳固 —— 见下一条。

---

## 20.3 主线二：训练、后训练与数据侧的加深

### 事实：Ray 是 RL 后训练的默认编排层

* Anyscale 官方的定位是 "**Ray is the engine for veRL, skyRL and more**"，
  并强调这些库 **no rewiring required**（anyscale.com/llm-rl）；
* **verl**（源自 ByteDance Seed，2026 年 1 月迁到 `verl-project/verl`）：
  FSDP/FSDP2 + Megatron 训练，vLLM / SGLang / HF 做 rollout，
  支持 PPO/GRPO/DAPO 等一长串算法；
* **OpenRLHF**（Ray + vLLM + ZeRO 分解）、**SkyRL**（长时序 agent RL）等同样构建在 Ray 上；
* **NVIDIA 在 Ray Summit 2026 公开**：Nemotron 3 Ultra（550B 总参数 / 55B 激活）
  在 **Ray Core 上跨 3,000+ GPU** 协调 rollout engine、environment worker 与策略训练；
  GB300 上的拓扑感知 GPU 放置让 RL 迭代吞吐提升约 13%。
  ⚠️ **来源层级说明**：这是**厂商在自家大会上的公开陈述**，
  不是独立第三方基准。第 19 章 §19.10 对同类数字标注为
  "二手转述，不要当基准"——**两处口径应当一致**：
  这些数字可以用来说明"有人在这么用"，**不能用来做容量规划或选型依据**；
* 学术侧的佐证：OpenRLHF 确立了 "Ray + vLLM + ZeRO" 的组合，
  HybridFlow/verl 引入 hybrid programming model 来协调多个 trainer 与 rollout engine。

**为什么 RL 特别适合 Ray**：它的负载形状和 Ray 的抽象天然吻合 ——
大量短任务（采样）、动态依赖、有状态组件（环境池、rollout engine）、
需要弹性（部分 worker 挂了不能停整个训练）。

### 事实：数据侧的目标是"可比的规模"

Ray Data 的官方路线图 issue #58665 里有几条明确目标：

* **External Shuffle Service**（与 Apache Celeborn 集成）；
* **Unbounded data source**（无界/流式数据源）；
* **Windowed Expressions**；
* **与 Ibis 集成**（把 SQL 表达力接进来）；
* 以及一条很关键的自我要求：建立 **"scalability envelope"**，
  让 Ray Data 与其他数据处理框架**有可比的基准**。

这条自我要求实际上承认了社区的批评：Ray Data 过去缺少可比的性能证据。

### 事实：训练侧的现代化接近完成

* **Ray Train V2 自 2.51.0 起默认开启**（回退需 `RAY_TRAIN_V2_ENABLED=0`），
  V1 API 已归档为 deprecated；
* 2.58 增加 **TorchTPU** 支持；TPU 从"能跑"变成"一等资源"
  （2.55 起官方支持 Google Cloud TPU，2.58 有 `SubslicePlacementGroup` 等）。

---

## 20.4 主线三：治理与商业化的分离

### 事实：Ray 归 PyTorch Foundation

* **2025 年秋**（PyTorch Conference 上宣布，官方新闻稿日期 2025-10-22），
  Ray 被捐赠给 **PyTorch Foundation**，与 PyTorch、vLLM、DeepSpeed 等同一伞下；
* 官方口径：**39,000+ stars、237M+ 下载**；
* 治理是基金会的 **TAC + Governing Board**（⚠️ 未确认 Ray 是否有独立 TSC）。

### 事实：Anyscale 被 Nscale 收购（待交割）

* **2026-07-30** 宣布最终协议；预计 2026 下半年完成，需监管批准；
* 金额官方未披露（媒体报道约 16.5 亿美元）；
* **Ray 项目不在交易范围内**（2025 年已捐给基金会）；
* 官方声明：Anyscale Platform 会继续在所有主流云上运行，portability 仍是核心。

### 判断

**"框架开源、控制面商业化"是这类项目的常态。** Ray 的开源部分由基金会托管，
这让"项目会不会被单一厂商绑架"的担忧小了很多；
但**运维体验最好、最省事的那一层（托管控制面）仍然会跟着商业公司走**。
对使用者的实践含义：

* 学习与自建：看基金会托管的开源部分（本书讲的就是它）；
* 上生产：要么接受 KubeRay 自己运维，要么接受某家托管服务（并接受它的路线图偏好）。

---

## 20.5 官方路线图上的技术方向

按「已落地 / 在途 / 远期」三档整理。

### 已落地（近一年）

| 方向 | 版本 | 内容 |
|---|---|---|
| GCS 容错 | 2.57 | **新增可选的**内嵌 RocksDB 后端（对应 REP-64）—— ⚠️ **不是"摆脱外部 Redis"**：它是 alpha、opt-in、仅 Linux，**默认路径仍是外部 Redis**（见第 10 章 §10.6 与附录 C §C.5） |
| 拓扑感知调度 | 2.56/2.57 | GPU domain 感知放置组（GB200/GB300）；公开 API |
| 控制面瘦身 | 2.58 | task events 移出 GCS 热路径 —— ⚠️ **是可选开关且默认关闭**（`RAY_enable_task_events_to_dashboard_head`，默认 `false`）。升级后发现 task events 变少，先查这个开关（见第 11 章 §11.4） |
| 数据面 | 2.50/2.55 | RDT（GPU 张量直传）alpha；多 gRPC 对象传输连接默认开启 |
| 数据管道 | `DataSourceV2` 2.57 默认开启；Hash Shuffle V2 `#63598` 引入、**2.58 支持 `join`** | 两个独立改动，不要混成一条 |
| 训练 | 2.51/2.58 | Train V2 默认；TorchTPU |
| LLM 服务 | 2.58 | KV-cache + token-aware 路由 |
| 隔离/安全 | 2.52/2.58 | token 认证（默认关闭）；实验性 Sandbox（gVisor 跑不可信代码） |

### 在途（官方 issue / RFC 明确在做）

* **KV cache 的分层管理**（M3）与 hybrid KV cache（M4）（issue #64389）；
* **SGLang 与 Ray Serve LLM 的持续对等**（#62796）；
* **Async inference**（异步推理）：维护者明确说 "here to stay"、在积极开发，
  但**至今仍是 alpha**，API 未定稿；
* **Data 的四个方向**（Celeborn shuffle / 无界数据源 / windowed expressions / Ibis）；
* **actor-only rearchitecture**：自 **2.57 起**陆续弃用 low-level scheduling API
  为其铺路（第 12 章 §12.6 记录的 `ray_remote_args=` / `ray_remote_args_fn=`
  则是在 **2.58 周期**弃用、计划 **2.64 移除** —— 两处是**同一件事的不同阶段**，
  不是两个版本号说错了；⚠️ 整体形态仍未确认）；
* **NPU/异构后端**：社区在推动 `comm_backend` 重构以支持 HCCL/RCCL
  （compiled DAG 的官方传输后端目前只有 CUDA-NCCL 与 CPU）；
* **V1 Train API / `ray.air` 的移除时间线** —— 这是**读者最关心、但本书给不出确切答案**
  的一条。第 13 章 §13.2 只说它们"已归档 deprecated、回退是过渡手段"，
  官方**没有**给出像 `ray_remote_args=` 那样的"计划某版本移除"的公开承诺。
  实用建议：**V2 已是默认（2.51 起），新代码一律按 V2 写**；
  旧代码用 `RAY_TRAIN_V2_ENABLED=0` 回退只是过渡手段，不要把它写进长期方案。
  **确切移除版本：未确认。**
* **Compiled Graph 的成熟度** —— 它是本书第 26 章的主题，也是"小任务开销大"
  这条批评（§20.7）的官方答案，但**截至 2.58 它仍然是 beta**：
  方法名带着 `experimental_` 前缀。**GA 时间表：未确认。**
  实践含义：可以用，但**要为 API 变动留出升级成本**。
* **Agent / agentic 负载** —— 这是"三条主线"（§20.1）之外**正在长出来的第四条线**，
  而且它的每一块拼图都能在 2.58 的源码里找到对应的动作：
  **多轮会话的亲和路由**（`ConsistentHashRouter`，见第 15 章 §15.7）、
  **不可信工具代码的沙箱**（`ray.experimental.sandbox` / gVisor，见第 17 章 §17.5）、
  **轨迹既是日志也是训练集**（同时喂给 OTel 与 agentic RL）。
  对这些负载，**"编排层 / 运行时层 / 引擎层"的三层分工要重新问一遍** ——
  完整讨论见**第 38 章**（Agent 工作负载），
  agentic RL 那一档的选型判据在 **§38.10**。

### 远期（官方提到但未承诺时间）

* 更**层次化的拓扑调度**、数据中心级调度、域级 `STRICT_SPREAD`（GB300 博客的"未来计划"）；
* **RDMA 的更广泛应用**（Ray Summit 2025 宣布了 RDMA 支持；
  而 Accelerated DAG RFC 里的表述是 "yes, but not for a while"，两者存在时间差，
  引用时要注意）。
* ⚠️ **没有 Ray 3.0 的任何官方计划**。检索 PyPI / GitHub / enhancements 仓库
  都没有 3.0 相关提案。

---

## 20.6 竞争格局：谁在抢 Ray 的位置

| 竞争者 | 层次 | 与 Ray 的关系 |
|---|---|---|
| **Kubernetes 原生（Kueue/Volcano/裸 K8s）** | 调度基础设施 | 有职责重叠；KubeRay 是 Ray 的官方 K8s 路径 |
| **PyTorch Monarch**（Meta，2025-10 发布） | 分布式编程框架 | **最直接的替代品**：single-controller、Python 前端 + Rust 后端、原生 RDMA、Erlang 风格 supervision tree。⚠️ **"能在单机 pdb 里 step through RL loop" 是它宣传材料里的说法，本书未确认**（同样的话在第 19 章 §19.5 标了未确认 —— 两处口径一致，别把它当已验证事实）。整体仍是 experimental、生态远不如 Ray |
| **vLLM / SGLang 自身的并行能力** | 推理数据面 | 见 20.2：它们正在"去 Ray 化"数据面 |
| **NVIDIA Dynamo / llm-d / AIBrix / KServe** | **推理编排层** | ⚠️ **本节最该补、原先却全章未出现的一层**。§20.2 的主线就是"Ray 在推理栈里角色收缩"，而这一层恰是 2025–2026 竞争最激烈处。关系是**双重**的：Dynamo 把 Ray 当作**可选的编排后端**之一（§20.2 提到的 selection service 就是集成点），同时它自己在抢 **Ray Serve LLM 的入口位置**；llm-d / KServe 则直接对标"多副本 LLM 服务的路由与扩缩"。**判断**：Ray 在这一层的优势是"和训练/数据同栈"，劣势是"推理团队更愿意只要一个薄薄的路由层" |
| **Modal / SkyPilot / 各家 neocloud** | 运行平台 | 不同层，可组合（Ray 可以跑在它们上面） |
| **Databricks / Spark** | 数据处理 | 反而是**互补且互相靠拢**：Databricks Runtime 15+ 把 Ray 做成一等公民；Ray 2.57 加入了 Catalog 抽象（Unity Catalog），**2.58 才补上 `write_delta` 写入路径** |
| **SLURM** | HPC 调度 | 在超算/研究机构里仍是首选；verl 官方教程同时覆盖 Ray-on-SLURM 与 SkyPilot 两条路径 |

**判断**：Ray 的护城河不是"性能"，而是**生态位置** ——
RL 框架、vLLM 的多机路径、数据/训练/服务的统一抽象。
它的风险是"每一层都有更专业的替代品，而它自己哪一层都不最深"。
这也是为什么"调度与放置"这条底线必须守住。

---

## 20.7 批评与官方应对

把批评和回应配对看，比只看一边更有信息量（批评都来自公开来源，
其中不少是官方自己承认的）：

| 批评 | 官方应对 |
|---|---|
| 小任务/单机开销大 | Compiled Graph（<50µs）；Accelerated DAG RFC |
| 调试难、跨层猜谜 | task events 移出 GCS；events export 加 worker 生命周期事件；GPU UUID 进 metric label；统一 `/api/healthz` |
| 背压不可观测 | 官方 issue #65607 承认，计划"暴露是哪个背压策略在阻塞 operator" |
| autoscaler 在大集群上慢 | 修复 V2 scheduler（PR #64175）；加优先级感知的 worker group 选择 |
| K8s 运维摩擦 | KubeRay 增量升级/回滚 beta、History Server、原生 NetworkPolicy、cert-manager mTLS、RocksDB 免 Redis |
| 安全默认无鉴权 | token auth（`RAY_AUTH_MODE=token`）、K8s RBAC、宣称"未来版本默认开启"（⚠️ 截至 2.58 默认仍关闭） |
| Ray Data 不如 Spark | 明确的 scalability envelope 目标；Celeborn shuffle；Ibis 集成；DataSourceV2 |
| 路线图不透明 | PyTorch Foundation TAC 主席明确目标："enhancing the visibility of development roadmaps across all contributing organizations" |
| NPU 支持弱 | 社区 `comm_backend` 重构（HCCL/RCCL）；2.55 起官方 TPU；2.58 加 Intel GPU / Apple mps |

**给使用者的建议**：把这张表当成"风险与缓解措施清单" ——
你踩到的坑，大概率官方知道且在做，但**不要指望它们在你需要的时间点被修完**。

---

## 20.8 未来三年的几个趋势判断（明确标注为主观）

以下是**我的判断**，不是事实：

1. **数据面会继续交给专用库**（NCCL/NIXL/UCX），Ray 守住控制面。
   标志是 vLLM 的 `RayExecutorV2`；这个趋势会扩散到更多推理框架。
2. **RL/agent 训练会成为 Ray 最深的场景**。
   理由：负载形状最匹配、生态已经形成、硬件趋势（一个机架一个巨型 GPU）
   让"拓扑感知 + 细粒度放置"的价值上升。
3. **"数据 + 训练 + 服务"统一编排的价值会持续**，
   因为企业不想为每个阶段引入一套调度系统。
4. **异构硬件支持会成为差异化点**：TPU/NPU/Apple silicon 的一等支持是刚需，
   而这一步需要大量设备相关的工程。
5. **安全会被迫补课**：默认无鉴权的历史包袱在 CISA 级别的事件后会逐步收紧，
   token 认证默认开启是可预期的方向（但时间未确认）。
6. **如果你在做技术选型**：把 Ray 当成"编排层"而不是"万能引擎" ——
   数据面用专用库，训练算法用 verl 这类专门框架，
   服务用 vLLM/SGLang 自己的 server，Ray 负责把它们组织起来。
   **这个定位在未来几年都不会错。**

---

## 20.9 给读者的学习建议

| 你要做的事 | 该学什么 | 该忽略什么 |
|---|---|---|
| 用 Ray 写应用 | Core API（2–5 章）+ 一个上层库（Data 或 Serve） | Ray 内部实现细节 |
| 做 LLM 推理服务 | Serve LLM + vLLM/SGLang 自身文档 + PD 分离 | 自己写分布式 executor |
| 做 RL 后训练 | verl/OpenRLHF + Ray 的放置组与资源模型 | 从零写 RL 框架 |
| 做平台/基础设施 | 全书 + KubeRay + 安全加固 + 调度源码 | —— |
| 做技术选型 | 第 19 章 + 本章的批评与回应表 | 厂商 benchmark |

**一句话**：Ray 的 API 很稳定（2.x 的 Core API 几年没大变），
**它的生态位置在变**。学 API 是一次性投入，理解位置变化才是长期能力。

> ⚠️ **但"主干稳定"不等于"全都稳定"**：被移除的往往是**边缘开关**，而不是核心 API。
> 本书自己就记录了好几个例子 —— `local_mode`（已从 Core 移除，`RuntimeError`）、
> `worker_idle_timeout_ms`（2.58 已不存在）、`ray_remote_args_fn=`（计划 2.64 移除）、
> `Checkpoint.from_dict`（已移除，调用抛 `AttributeError`）。
> **实践规则**：`@ray.remote` / `ray.get` / `ray.put` / actor 这些主干放心用；
> **任何带 `experimental_` / `_` 前缀的、以及"调试开关"性质的参数，
> 都当成随时会没**，并且升级前查一遍 release notes 的 "Deprecations / Removals" 一节。

### 20.9.1 Ray 的 API 稳定性与弃用策略

上面那句"升级前查 release notes"太笼统了。这一小节把**机制**讲清楚 ——
知道 Ray 怎么弃用一个 API，你才能在升级前**自动扫出**自己会被打断的地方。

#### ① 弃用是怎么宣布的

两条渠道并存：**release notes 的 "Deprecations / Removals" 一节**，
以及**源码里的装饰器**。后者会在 docstring 上追加一段文字
（`ray.util.annotations.Deprecated` 内部的 `_append_doc(obj, message=doc_message, directive="warning")`），
所以你在 IDE 里悬停就能看到 `**DEPRECATED**: This API is deprecated and may be
removed in future Ray releases.`

**宽限期是真实存在的，而且会给出目标版本或日期**。本书里能直接引用的三个例子
（都来自源码里的告警原文）：

| API | 源码里的原文 |
|---|---|
| `Dataset.zip` | `will be removed in Ray 2.64` |
| `ray_remote_args_fn=`（Data 算子形参） | `is deprecated and will be removed in Ray 2.64.` |
| `DataContext.scheduling_strategy` | `is deprecated and will be removed after January 2027.` |

⚠️ **但这不能当成承诺**：`Dataset.add_column`（第 12 章 §12.2）就是
**只在 docstring 上标了 `@Deprecated`、运行时并不告警**的形态
（源码是 `@Deprecated(message="Use \`with_column\` API instead")` ——
`warning` 用默认值 `False`）—— 也就是说"文档说会移除"和"运行时提醒你"是两件事。
**作为对照**，`Dataset.zip` 显式写了 `warning=True`，每次调用都会抛
`RayDeprecationWarning`。所以**两套形态都存在，升级前只依赖运行时告警是不够的。**

#### ② 🔴 `DeprecationWarning` 的语义：Ray 里有**两套**机制，行为完全不同

这是本节最需要记住的一条 —— 它直接决定了你的告警扫描能不能抓到东西。

| | **A 套：`ray._common.deprecation`** | **B 套：`ray.util.annotations.Deprecated`** |
|---|---|---|
| 入口 | `deprecation_warning(old, new=..., help=..., error=..., stacklevel=2)` 与 `@Deprecated(new=..., error=...)` | `@Deprecated(message=..., warning=False)` |
| 底层机制 | **`logger.warning(...)`** —— 是一条**日志** | **`warnings.warn(..., RayDeprecationWarning)`** —— 是**真 warning** |
| 原文 | `"DeprecationWarning: " + msg + " This will raise an error in the future!"` | `warning_message = "This API is deprecated and may be removed in future Ray releases. You could suppress this warning by setting env variable PYTHONWARNINGS=\"ignore::DeprecationWarning\""` |
| 能否被 `-W error::DeprecationWarning` 抓到 | ❌ **抓不到**（它连 `warnings` 都没经过） | ✅ **能抓到** —— `class RayDeprecationWarning(DeprecationWarning)`，是子类 |
| 触发次数 | 默认 `log_once`（`@Deprecated` 里先 `if log_once(old or obj.__name__)`），**同名 API 只告警一次** | **每次调用都 warn** |
| 想让它变成错误 | 传 `error=True`（抛 `ValueError`）或 `error=SomeException` | 只能靠 `warnings` 过滤器 |

还有一个默认行为要记住：`ray/util/annotations.py` 里有

```python
if not sys.warnoptions:
    warnings.filterwarnings("module", category=RayDeprecationWarning)
```

—— 也就是说**如果你没有自己设 `PYTHONWARNINGS`，Ray 会默认把
`RayDeprecationWarning` 按"每个模块只显示第一次"过滤掉**。
你看到"只报了一次"不是它只触发了一次。

#### ③ `ray._private` 前缀意味着什么

**Ray 的"公开性"是由装饰器声明的，不是由路径声明的**
（`ray/util/annotations.py` 里的 `AnnotationType` 枚举：
`PUBLIC_API = "PublicAPI"` / `DEVELOPER_API = "DeveloperAPI"` / `DEPRECATED = "Deprecated"`）。
路径只是**约定**：

* `ray._private.*`、`ray.*._internal.*`（如 `ray.llm._internal`）—— **没有稳定承诺**，
  随时可能改签名或消失；
* **跨命名空间重新导出是常态**，而且**导出点才是公开面**。最典型的例子：
  `ActorPoolStrategy` / `TaskPoolStrategy` 实际定义在
  `ray/data/_internal/compute.py`，但 `ray/data/__init__.py` 里
  `from ray.data._internal.compute import ActorPoolStrategy, TaskPoolStrategy`
  把它们**重新导出到了 `ray.data`**。所以
  `from ray.data import ActorPoolStrategy` 是公开用法，
  `from ray.data._internal.compute import ActorPoolStrategy` 不是；
* ⚠️ **一个容易读反的情况**：本书多处引用 `ray/data/_internal/...` 或
  `ray/train/_internal/...` 的源码来**验证语义**（比如第 12 章引
  `_internal/compute.py` 证明 `min_size >= 1` 的校验）。那是"**用来核实行为**"，
  **不是"叫你去 import 它"**——这是两件事。

#### ④ 什么算公开 API：一份可操作的判据

按顺序问，**任一为否就别依赖它**：

1. **它出现在 docs.ray.io 的 API reference 页上吗？** 没上页的一律当内部；
2. **它带 `@PublicAPI` 吗？`stability` 是什么？** —— `stable` 放心用；
   `beta` / `alpha` 可以用，但要**锁版本**（本书里 `ray.data.llm` 与
   `ray.serve.llm` 全系列截至 2.58 都还是 `beta` / `alpha`，见第 15 章 §15.12）；
3. **它带 `@DeveloperAPI` 吗？** 带了就是"**可能跨 minor 版本变**"。
   两个具体例子：`serve.get_deployment_handle()`（第 15 章 §15.2 的"三种拿法"之一）
   是 `@DeveloperAPI`；RLlib 的 `Checkpointable` 系也是 `@PublicAPI` 之外的形态。
   **能用，但要为改动留预算**；
4. **它是不是在 `_private` / `_internal` 里？** 是就别直接 import；
5. ⚠️ **反例：没装饰器 ≠ 稳定**。第 12 章 §12.6 末尾讲的
   `DataContext.__setattr__` 就是活例子 —— 给一个不存在的字段赋值
   （`ctx.use_streaming_executor = False`）**不会报错、也不会警告**，
   只会静默写进去然后什么都不发生。**"没报错"永远不能当作"这 API 是对的"。**

#### ⑤ 升级时怎么扫出自己的用法会被弃用

三道网，从便宜到贵。**A 套和 B 套要分别抓**（见上面 ②）：

```bash
# ── 网①：抓 B 套（真 warnings）。把它变成异常，测试直接失败 ──
python -W error::DeprecationWarning -m pytest tests/ -q
# 只想抓 Ray 自己那类（RayDeprecationWarning 是 DeprecationWarning 的子类）：
python -W error::ray.util.annotations.RayDeprecationWarning -m pytest tests/ -q

# ⚠️ 注意：-W 抓不到 A 套（它走 logging）。想让 A 套变异常，
#    只能在被调用的那一刻传 error=True —— 你改不了别人的代码，所以走网②。

# ── 网②：抓 A 套 —— 把 ray 的 logger 挂一个收集器，跑一遍真实流程 ──
python - <<'PY'
import logging, runpy
class Hit(logging.Handler):
    def __init__(self):
        super().__init__(); self.buf = []
    def emit(self, rec):
        if "DeprecationWarning:" in rec.getMessage():
            self.buf.append(rec.getMessage())
h = Hit()
logging.getLogger("ray").addHandler(h)
runpy.run_path("your_job.py", run_name="__main__")   # ← 换成你的入口
print("A 套弃用告警 %d 条：" % len(h.buf))
for m in h.buf:
    print("  -", m)
PY

# ── 网③：静态扫 —— 直接把安装好的 ray 里所有弃用点列出来，跟自己用到的名字对一遍 ──
python - <<'PY'
import os, ray
root = os.path.dirname(ray.__file__)
KEY = "deprecation_warning("
for dirpath, _, names in os.walk(root):
    for n in names:
        if not n.endswith(".py"):
            continue
        p = os.path.join(dirpath, n)
        try:
            for i, line in enumerate(open(p, encoding="utf8", errors="ignore"), 1):
                if KEY not in line:
                    continue
                rest = line.split(KEY, 1)[1].lstrip().lstrip("\"'")
                if rest.startswith("old="):
                    rest = rest[4:].lstrip().lstrip("\"'")
                msg = rest.replace('"', "'").split("'")[0][:60]
                print(f"{os.path.relpath(p, root)}:{i}  {msg}")
        except OSError:
            pass
PY
```

**三道网的分工**：网① 最便宜（CI 里加个 flag），但只覆盖一部分；
网② 最接近"真实使用"（它抓的是**你真跑到的路径**），适合放在发版前的冒烟脚本里；
网③ 是**穷举**，能提前看到"下一版会砍什么"，但要人肉比对。

> 📌 **一句话总结这一小节**：
> **Ray 承诺的是"先告警、给目标版本、再移除"这个流程，不是"永不改变"。**
> 所以升级前要做的事只有三件：**锁版本**（镜像里钉死 ray 的版本）、
> **开告警**（上面三道网至少上①和②）、
> **把 LLM / Agent 这类 `beta` 模块的导入面收窄到一个文件**
> —— 这样下次 API 变动你只改一个地方。

---

## 20.10 本章小结

* 三条主线：**推理栈里角色收缩（vLLM 换掉 Ray executor）**、
  **训练/后训练/数据侧加深（RL 生态 + Train V2 + Data 路线图）**、
  **治理与商业化分离（PyTorch Foundation + Nscale 收购 Anyscale）**。
* 已落地：GCS RocksDB 容错、拓扑感知调度、KV-aware 路由、Train V2 默认、
  Sandbox（gVisor）、task events 移出 GCS 热路径。
* 在途：KV cache 分层管理、SGLang 一等引擎、Async inference（仍 alpha）、
  Data 的 Celeborn/Ibis/无界数据源、actor-only 重构。
* **没有 Ray 3.0**（也没找到任何官方计划）。
* 竞争上最直接的是 PyTorch Monarch；最现实的压力来自
  「每一层都有更专业的替代品」。
* 官方对已知批评（小任务开销、调试难、背压不可观测、安全）都有对应动作，
  但**不要指望按时修完**。
* 选型建议：**把 Ray 当编排层，不要当万能引擎**。
* **升级不是"跟着升就行"**：Ray 承诺的是"先告警、给目标版本、再移除"这个
  **流程**，不是"永不改变"。而且 Ray 里有**两套弃用机制** ——
  `ray._common.deprecation.deprecation_warning` 走 **logger**（`-W error::
  DeprecationWarning` 抓不到！），`ray.util.annotations.Deprecated` 走真
  `warnings`（抛 `RayDeprecationWarning`，是 `DeprecationWarning` 的子类，能抓到）。
  所以"开个 `-W error::DeprecationWarning` 就放心了"是**错的**。
  完整的三道扫描网（`-W` / logging 收集器 / 静态扫 `deprecation_warning(`）
  与"什么算公开 API"的五条判据见 **§20.9.1**。
* **Agent / agentic 负载是三条主线之外的第四条线**（§20.1、§20.5），
  专章在第 38 章，agentic RL 的选型判据在 §38.10。

这是全书的「展望」。**往后还有 19 章**，按用途分六组：

| 组 | 章节 | 什么时候读 |
|---|---|---|
| **参考层（附录 A–G）** | 21 API 速查 · 22 mini-ray 工程手册 · 23 术语表 · 24 FAQ 与排错 · 25 端到端实战案例 · 27 Ray Client/runtime_env/多语言 · 28 CI-CD/Slurm/云上调度 | 当字典用，不必通读 |
| **机制补充** | 26 Ray Compiled Graph 与 DAG API | 当你被"小任务开销"咬到时（§20.7 那张表的第一行就是它） |
| **实战补充** | 29 Ray × PyTorch/HuggingFace · 30 GPU 与集合通信 · 31 性能剖析工具链 · 32 实验追踪与 MLOps · 33 Ray CLI 全集 | 真上手做训练/推理/调优/上生产时 |
| **收口补充（第四轮新增）** | 34 表格数据与传统 ML · 35 数据版本与模型注册 · 36 分布式追踪与 OTel | 你的负载不是"GPU + 大模型"时（34），或者你要把实验真正变成生产系统时（35、36） |
| **推理与 Agent（第六、七轮新增）** | 37 LLM 推理引擎与性能优化 · **38 Ray 与 Agent 工作负载** | 你要自己碰推理引擎的性能旋钮时（37，对应 §20.2 的"引擎层"）；你的负载是"长时程、多轮、要调工具"的 Agent 时（38） |
| **方法层（第八轮新增）** | **39 Ray 源码阅读与事实核查指南** | 你想**核实本书某个结论**、或者要自己去查 Ray 的某个事实时（这也是全书的收尾章） |

> **为什么 26 和 29–39 排在这里而不是前面**：它们是「按需深入」的章节 ——
> 26 服务于 §20.7 里"小任务开销"这条已知批评；29–31 服务于
> 2026 年最主流的真实负载形态（HF 模型 + 多卡 + 要调优）；
> 32–36 服务于「实验跑通之后」的那一段（怎么记录、怎么固化、怎么追踪）；
> 37–38 服务于 2025–2026 新长出来的两层 —— **推理引擎层**与
> **Agent 负载**（后者在 §20.2 的"三条主线"之外，是第四条正在成形的线）；
> 39 是**方法层** —— 它不含新的 Ray 知识，教的是**怎么核实**前面这些章节，
> 所以它只能排在最后。
> **本书的正文主线在 20 章已经讲完**，后面这些是纵深。
>
> ⚠️ **章号顺序 ≠ 阅读顺序**：这 19 章是七轮修订**依次追加**的
> （第 26–28 章是第一轮，29–31 是第二轮，32–33 是第三轮，34–36 是第四轮，
> 37 是第六轮，38 是第七轮，39 是第八轮），
> 所以 26 会排在附录后面。按用途重排的顺序表见
> [第 0 章 §0.4.1](ray教程-00-前言与导读.md)。
