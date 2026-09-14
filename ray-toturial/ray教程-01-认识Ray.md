仓库地址：https://github.com/hhk-png/cycle-agent

# 第 1 章：认识 Ray

> 本章目标：搞清楚 Ray 到底是什么、它解决的是什么问题、今天（2026 年）处于什么状态。
> 读完你应该能回答三个问题：**为什么要有 Ray**、**它和 Spark/Dask/asyncio 差在哪**、
> **什么场景不该用它**。

---

## 1.1 一句话定义

> **Ray 是一个把「普通 Python 函数和类」变成分布式计算单元的框架。**

你写的是：

```python
@ray.remote
def square(x):
    return x * x

refs = [square.remote(i) for i in range(1000)]   # 1000 个并行任务
values = ray.get(refs)                            # 收结果
```

跑起来的是：1000 个任务被调度到集群的多台机器/多个进程上执行，结果通过
共享内存对象存储返回，失败会被重试，依赖关系自动满足，**而你一行分布式代码都没写**。

它的野心写在官方的一句话里（来自 Ray 联合创始人 Robert Nishihara 在 Ray 加入
PyTorch Foundation 时的发言）：

> "our goal is to make distributed computing as straightforward as writing Python code."

## 1.2 它解决的是什么问题

### 问题一：并行计算的三个层次，Python 都很难受

| 层次 | 典型需求 | 传统方案 | 痛点 |
|---|---|---|---|
| 数据并行 | 处理 1TB 日志 | Spark / Dask | 不是所有计算都能塞进 SQL/DataFrame 模型 |
| 任务并行 | 跑 1000 个实验/模拟 | multiprocessing / Celery | 跨机器、依赖管理、容错都要自己写 |
| 有状态服务 | 参数服务器、推理服务 | 自己写 RPC + 调度 | 状态放哪、怎么扩缩、挂了怎么办 |

Ray 的答案是**一套统一抽象**同时覆盖这三层：函数是任务、类是 actor、对象是数据。
这就是它和「只做 DataFrame」的 Spark、以任务图/集合抽象为中心的 Dask、
只做消息队列的 Celery 最大的区别。

### 问题二：AI 工作负载的形状变了

Ray 诞生于 2017 年的 UC Berkeley RISELab（作者包括 Robert Nishihara、Philipp Moritz、
Ion Stoica，后者也是 Spark 的作者之一）。当时他们观察到一件事：

> 强化学习、超参搜索、仿真这类**新兴 AI 负载**，既不是纯数据并行，也不是纯
> 有向无环图（DAG）。它们的特征是：**任务数极多、单个任务可能很短、
> 任务之间有动态依赖、还需要有状态的组件长期活着。**

这是 Ray 论文（*Ray: A Distributed Framework for Emerging AI Applications*, OSDI 2018）
的核心论点。用当时的 Spark 跑这类负载要付两笔额外的成本：按阶段（stage）同步的
开销，以及「所有计算都要表达成算子」的表达力损失。

### 问题三：AI 时代的规模变化

到 2023 年之后，这个问题变成了「怎么把一个 100B 参数模型的训练/推理/后训练
拆到几百上千张 GPU 上」。Ray 因为上面那套抽象，恰好成了**编排层**的默认选择：

* vLLM 用它做多机多卡的分布式 executor；
* 主流 RL 后训练框架（verl、SkyRL、OpenRLHF）都构建在 Ray 之上；
* Ray Train / Ray Data 把「数据 → 训练 → 调参 → 服务」串成一条流水线。

## 1.3 Ray 的全景图

```
┌───────────────────────────── 用户接口 ─────────────────────────────┐
│  Python        Java(官方)      C++(官方)      其它语言(社区)        │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌────────────────────────── Ray AI Libraries ────────────────────────┐
│  Ray Data      Ray Train      Ray Tune      Ray Serve     RLlib    │
│  (数据管道)     (分布式训练)    (超参搜索)     (在线服务)     (强化学习) │
│      └──────────── Ray Data LLM / Ray Serve LLM ──────────┘        │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌──────────────────── Ray Core(本教程的主角)────────────────────────┐
│   Task(远程函数)   Actor(远程类)   Object(不可变对象)   ObjectRef   │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  GCS(元数据)  │  Raylet(每节点调度+对象存储)  │  CoreWorker  │ │
│  └──────────────────────────────────────────────────────────────┘ │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌──────────────────────── 集群与运行时 ─────────────────────────────┐
│  本地多进程   │  Ray 集群(ray start)  │  KubeRay / Kubernetes      │
│  对象存储(Plasma) │  GCS 容错(RocksDB/Redis) │  Autoscaler v2     │
└───────────────────────────────────────────────────────────────────┘
```

本教程的**主线是 Ray Core**（第 02–11 章），因为上层的所有东西都是它的组合；
然后在第 12–19 章把每个上层库讲透，最后在第 20 章谈未来。

## 1.4 现在的 Ray（截至 2026 年 9 月）

这一节的事实都来自官方仓库/文档/新闻稿，来源标注在括号里；**不确定的我会写清楚**。

### 版本与节奏

* 最新稳定版是 **Ray 2.58.0**，发布于 **2026-08-23**（GitHub Releases / PyPI）。
* **没有 Ray 3.0**。PyPI 上不存在 3.x 正式版本，官方也没有 3.0 路线图。
  （注意：搜索引擎里的 "Ray 3.0" 常常指 Luma 的视频模型或某个同名调试工具，
  和这个 Ray 无关。）
* 要求 **Python ≥ 3.10**；3.9 **自 2.52 起弃用、2.54.0 起不再支持**
  （PyPI 元数据可查：2.53.0 仍带 `Python :: 3.9` 分类器，2.54.0 起变为 `>=3.10`）。
* 发布节奏大约每 4–6 周一个小版本。近一年值得记的变化：

| 版本 | 时间 | 变化 |
|---|---|---|
| 2.44 | 2025-03 | Compiled Graph 进入 beta；**Workflows 弃用** |
| 2.50 | 2025-10 | Ray Data 默认 shuffle 改为 hash-based；Ray Direct Transport(RDT) 以 **alpha 公布**（⚠️ `@ray.method(tensor_transport=...)` 的 API 在 **2.48** 就已落地，见下注③）；**`ray up` 起默认开启 autoscaler v2**（第八轮已核实 —— ⚠️ 只对 `ray up` 这条路径，裸 `ray start` 的组件默认仍是关的，见第 17 章 §17.3） |
| 2.51 | 2025-10 | **Ray Train V2 默认开启** |
| 2.52 | 2025-11 | Python 3.9 **弃用**（2.54.0 起正式不再支持）；内置 token 认证（默认关闭） |
| 2.54 | 2026-02 | （早先这里写"新的集群 autoscaler 默认开启"，第八轮核实为**错** —— 翻转发生在 **2.50.0**，已并入上一行） |
| 2.55 | 2026-04 | Ray Data `DataSourceV2`；GPU shuffle；Python 3.14 支持 |
| 2.57 | 2026-**08** | **GCS 容错新增内嵌 RocksDB 后端**（alpha、**opt-in**、仅 Linux；⚠️ 不是"改用"——外部 Redis 仍是默认路径，见第 10 章 §10.6）；topology-aware 调度的**公开 Python API `topology_strategy`**（**已核实**，见下注①） |
| 2.58 | 2026-08 | Serve LLM 完成 KV-cache + token-aware 路由；实验性 **Ray Sandbox（gVisor）** |

> ⚠️ **两点关于这张表的诚实说明**：
>
> ① **topology-aware 调度的版本号：第八轮已核实，原先的「未确认」可以去掉。**
> 这里原本有两种互相打架的说法（本节写 2.57、第 3 章 §3.4 写 2.56），
> 第八轮做了跨版本检索，结论是**两件事落在两个版本上**：
>
> | 层 | 从哪个版本起 | 可复核的判据 |
> |---|---|---|
> | **公开 Python API** `topology_strategy` | **2.57** | 对 `python/ray/util/placement_group.py` 逐 tag grep `topology_strategy`：2.55=**0**、2.56=**0**、2.57=**34**、2.58=**34**（对照组 `def placement_group` 每版都 = 2） |
> | **C++ 侧独立策略** `topology_bundle_scheduling_policy` | **2.58** | `src/ray/raylet/scheduling/policy/topology_bundle_scheduling_policy.cc` 在 2.57 是 **404**、2.58 是 **200**（对照组 `hybrid_scheduling_policy.cc` 两版都 200）；`scheduling_options.h` 里 `topology` 命中数 2.57=**0**、2.58=**22** |
>
> **⚠️ 所以"2.56 起支持"是错的**（2.56 两层都没有）。
> 而 2.57 那一版是个**过渡实现**：拓扑策略还只在 **Python 侧**近似 ——
> 源码注释自称 *"Current implementation derives node level strategy from
> topology_strategy, while we pass a topology strategy with node level strategy
> stripped."*，`_derive_node_level_strategy()` 只从
> `topology_strategy[ray.io/node-id]` 推出**节点级**策略。
> **要用完整的拓扑语义，需要 2.58+。**
>
> 第 3 章 §3.4 的那句已按此改写（两处口径现已一致）。
>
> ② 表里的**时间列**来自各版本 release notes 的发布日期。
> **2.57 的月份已在第五轮确认为 `2026-08`**（PyPI 上 `ray 2.57.0` 的上传时间
> 是 **2026-08-11**，2.58.0 是 2026-08-23）—— 早先这里写的是 2026-07 的推测值，
> 现已改成实测值。**2.50 与 2.51 都落在 2025-10** 属正常（相邻小版本可以同月）。
> 唯一需要记住的基线是 **2.58.0 = 2026-08-23**。
>
> ③ 关于 **RDT 的版本号**：本书早先三处（本节、第 3 章 §3.7、第 7 章 §7.8）
> 都写「2.50 起 alpha」。按源码，`@ray.method(tensor_transport=...)`
> 在 `ray/actor.py` 里出现的次数是 **2.47=0 / 2.48=38 / 2.49=50 / 2.50=77**
> —— 即 **API 在 2.48 就已落地**。所以准确说法是
> **"2.48 引入 API、2.50 以 alpha 公布"**；若官方 release note 把 alpha
> 定在 2.50，那指的是**宣告**而非代码。**这一点本书未找到可核对的 release note。**
>
> ⚠️ **第七轮补记（因为这条又被改错过一次）**：第六轮之后有人把附录 C 的
> 版本改成「**最早出现在 2.52**」，依据是在 `ray_constants.py` 里
> grep `Ray Direct Transport`。**那个依据本身不成立** —— 实测 2.52.0 与
> 2.52.1 都是 **0 命中，最早出现在 2.53**；而且那句只是注释里的一次**提及**，
> 不能当"功能引入版本"。更硬的证据是**模块**何时存在：
> `python/ray/experimental/rdt/__init__.py` 在 **2.54 是 404、2.55 起 200**。
> 所以现在能站住的只有两句：**API 从 2.48 起、RDT 模块最早 2.55**，
> 而"2.50 公布 alpha"**仍未确认**。
> 完整证据与复核命令见附录 C 的「Ray Direct Transport」条。
>
> **这件事的方法论价值大于版本号本身**：它是本书第三次踩同一个坑 ——
> **拿到了一个 grep 命中，就把它当成了全局结论**（前两次是"三处一致了对的是错的值"
> 和"测试全绿不等于没有 bug"）。详见第 39 章结语里的四条教训 ——
> **第八轮又补上了第四条：核查这个动作本身也会出错，而且它出错时最像成功。**

### 治理：Ray 已进入 PyTorch Foundation

* **2025 年秋**（PyTorch Conference 上宣布，官方新闻稿日期为 10 月 22 日），
  Ray 被捐赠给 **PyTorch Foundation**（Linux Foundation 旗下），与 PyTorch、
  vLLM、DeepSpeed 等成为同一伞下的项目（Linux Foundation 新闻稿）。
* 官方口径的项目规模：**39,000+ GitHub stars、237M+ 下载量**（同一条新闻稿）。
* 治理结构是基金会的 **TAC（Technical Advisory Council）+ Governing Board**。
  ⚠️ **未确认**：Ray 是否有自己独立的技术指导委员会（TSC）—— 检索没有找到证据，
  所以本书不写"Ray TSC"。

### 商业公司的变化：Anyscale 被 Nscale 收购

* **2026-07-30**，英国 AI 云厂商 **Nscale** 宣布签署收购 **Anyscale** 的最终协议，
  预计 2026 下半年完成（需监管批准）。金额官方未披露；媒体报道约 16.5 亿美元，
  **这个数字不是官方公布**（Reuters / TechCrunch / Anyscale 官方博客）。
* **Ray 开源项目不在交易范围内** —— 它 2025 年就已经归 PyTorch Foundation，
  由社区治理（Anyscale 官方声明）。
* 对本教程的意义：**"框架" 和 "商业控制面" 是两件事**。你在书里学到的一切
  （Ray Core、Data/Train/Tune/Serve）都属于基金会托管的那部分；
  Nscale 买走的是 Anyscale Platform 那层托管控制面。

### 一个重要且容易被忽略的信号

**vLLM 正在把 Ray 从"执行引擎"降级为"进程启动器 + 放置管理器"。**
vLLM 的 RFC #35848（由 Ray 团队提交）提出用新的 `RayExecutorV2` 取代基于
Ray compiled graph 的旧 executor：新实现继承 `MultiprocExecutor`，
**控制面走共享内存消息队列、数据面走 `torch.distributed`/NCCL，
热路径上没有 `ray.remote` 调用**。保留 Ray 变体的理由只剩三条：
跨节点并行的简便性、GPU 的细粒度放置、资源感知调度。

这件事说明两件事：

1. Ray 在推理栈里的位置**正在收缩**到"调度与放置"这一层；
2. 同时 Ray 在**训练/后训练/数据**那一侧反而更深了（RL 框架几乎都是 Ray 的）。

第 19、20 章会把这条线索展开。

### 安全：这是 Ray 的真实短板

Ray 的设计假设是"集群在网络隔离的可信环境里"，所以**默认不做鉴权**。这导致了
一连串 CVE（GitHub Security Advisories）：

* **CVE-2025-62593**（CVSS 9.4，影响 < 2.52.0）：DNS rebinding + User-Agent 绕过，
  开发者只要在浏览器里打开一个恶意页面，本机的 Ray dashboard 端口（8265）
  就可能被用来执行任意命令。**2026-08-17 被 CISA 列入已知被利用漏洞目录**。
* **CVE-2026-27482**（中危，修复于 2.54.0）：dashboard 的 DELETE 端点缺少防护。
* 缓解手段是 `RAY_AUTH_MODE=token`（2.52 引入，**默认仍是关闭的**）。

17 章会专门讲生产环境的加固清单。这里先记住一句：**不要把 Ray 的端口暴露到
不可信网络**。

## 1.5 什么时候该用 Ray，什么时候不该

想清楚"不该用"比"该用"更有价值。

**适合**：

* 计算单元是**函数/类**，而不是"表上的算子"：仿真、超参搜索、RL 采样、
  数据处理里的非结构化部分；
* 需要**有状态的长驻组件**：参数服务器、推理服务、仿真环境池；
* 需要**弹性和容错**：任务跑到一半挂了能自动重跑；
* 需要**同一套代码从笔记本跑到千卡集群**。

**不适合**（或者该先考虑别的）：

* 纯 SQL/DataFrame 型的 ETL：**Spark / DuckDB / Polars 更合适**（Ray Data 在
  "任意 SQL 查询都能比专用引擎更快"这件事上并不占优，这是社区公认的批评）；
* 单个超大任务（比如一次 8 卡 DDP 训练）：**直接 torchrun / torch.distributed
  更简单**，Ray 的价值在于"很多任务 + 有状态组件"；
* 延迟敏感的微服务：Ray Serve 可以，但如果你只需要一个 HTTP 服务，
  FastAPI + K8s 的运维复杂度更低；
* 单机小数据：`multiprocessing` / `concurrent.futures` 的调试体验更好；
* 团队里没人愿意运维集群：Ray 的调试与运维确实有门槛（第 18 章会讲）。

用 Ray 社区里一句流传很广的话概括（来自 Hacker News 上的讨论）：

> "If you have 1 massive job, Ray sucks... If you have **50M tiny jobs**,
> Ray and kuberay is great."

## 1.6 本教程的结构

```
第 1 部分  概念与架构     01 认识 Ray  → 02 核心概念与 API  → 03 架构设计
第 2 部分  动手           04 快速上手  → 05 任务、对象与依赖
第 3 部分  从零实现       06 从零实现简化版 Ray（mini-ray）
第 4 部分  深入机制       07 对象存储 → 08 调度 → 09 Actor → 10 容错 → 11 可观测性
第 5 部分  AI 库          12 Ray Data → 13 Train → 14 Tune → 15 Serve → 16 RLlib
第 6 部分  工程实践       17 生产部署与安全 → 18 性能调优与反模式 → 19 生态与选型
第 7 部分  展望与附录     20 现状与未来 → 21–25 附录 A–E（API 速查/工程手册/术语/FAQ/实战）
第 8 部分  补齐的三块     26 编译图与 DAG API → 27 附录 F（Ray Client/runtime_env/多语言）
                          → 28 附录 G（CI-CD/Slurm/云上调度）
第 9 部分  生态、GPU、工具链
                          29 PyTorch/HuggingFace → 30 GPU 与集合通信 → 31 性能剖析
第 10 部分  收口          32 MLOps 集成 → 33 CLI 全集
                          → 34 表格数据与传统 ML → 35 数据版本与模型注册
                          → 36 分布式追踪与 OTel
第 11 部分  推理引擎层    37 LLM 推理引擎与性能优化
第 12 部分  Agent 工作负载 38 与 Agent 工作负载
第 13 部分  核查方法       39 源码阅读与事实核查指南（**教你怎么否决前面这些章**，全书的收尾）
```

> ⚠️ **章号顺序 ≠ 阅读顺序**：第 8–13 部分都是历次修订在末尾**追加**的，
> 所以 26 排在附录后面。按用途重排的阅读顺序见第 0 章 **§0.4.1**。

**怎么读**：

* 只想用起来：1 → 4 → 5 → 12/13/15；
* 想搞懂原理：1 → 2 → 3 → 6 → 7–11（第 6 章的 mini-ray 是全书重心，
  它用约 1.1 万行可运行代码把后面的机制都实现了一遍）；
* 准备上生产：17 → 18 → 19，配合 20 看趋势。

**前置知识**：会写 Python、知道进程/线程/序列化的基本概念就够了。
不需要分布式系统背景 —— 需要的那部分我会在第 3 章补上。

## 1.7 本章小结

* Ray = **任务 + Actor + 对象**三件套的统一抽象，用 Python 表达分布式计算。
* 它诞生的动机是"新兴 AI 负载既不是纯数据并行、也不是纯 DAG"。
* 2026 年的 Ray：稳定版 2.58.0、无 3.0、已由 PyTorch Foundation 托管、
  商业公司 Anyscale 被 Nscale 收购（待交割）、在推理栈的位置收缩、
  在训练/后训练/数据侧加深、**默认无鉴权是真实短板**。
* 它有明确的适用边界：大量中小任务 + 有状态组件 + 需要弹性，是它的主场；
  纯 SQL、单个大任务、延迟敏感的简单服务，通常有更省事的选择。

下一章我们钻进 API：`@ray.remote` 到底把函数变成了什么？
ObjectRef 为什么是"未来值的句柄"？Actor 和任务的区别到底在哪一行代码上？
