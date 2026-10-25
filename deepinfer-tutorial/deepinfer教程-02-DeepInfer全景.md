# 第 2 章 DeepInfer 全景

本章导读：先回答“DeepInfer 是什么、不是什么”，再把它放回 DeepSeek 的生产推理系统中，解释开源仓库覆盖了哪一段、省略了哪一段。读完本章，你应该能画出 DeepInfer 的组件关系，说清楚它与 vLLM、SGLang、TensorRT-LLM 的继承和差异，并能判断一个团队是否适合使用它。

## 2.1 一句话定义

DeepInfer 是 DeepSeek 开源的高性能大模型推理框架，定位是“支撑大规模生产服务的离线推理核心”。它于 2026 年 10 月开源，使用 Apache License 2.0，目标模型是 DeepSeek-V4.1-Flash，支持 NVIDIA SM100（Blackwell）GPU 和华为 Ascend 950 NPU。

这里的“离线推理”不是指“只能做批处理”，而是指开源仓库提供的是 **offline inference 入口**：你给它一组 prompt，它返回生成结果。仓库没有提供完整的在线服务层（HTTP 网关、鉴权、限流、跨副本负载均衡），但引擎内部的调度、KV 管理、并行和采样都是按生产负载设计的。

DeepInfer 官方仓库列出的特性可以分成六组。

第一组是**优化 kernel 与运行时**：FlashMLA、DeepGEMM、DeepEP、TileKernels、DeepSelect，以及自研 TileLang kernel；模型执行与采样运行时。

第二组是**并行**：Attention DP、balanced CP、MoE EP + EPLB。

第三组是**推测解码**：DSpark 与 confidence-scheduled verification。

第四组是**SWA 有界重放**：decoder 只重放最近一段 suffix，减少 prefill 计算。

第五组是**异步调度**：zero-overhead batch scheduler，把 CPU 调度与设备执行重叠。

第六组是**正确性相关**：batch-invariant inference、deterministic sampling、structured outputs。

这六组不是并列的功能清单，而是一条从 kernel 到调度的优化链：kernel 决定单步能跑多快，并行决定多卡怎么分，推测解码决定每步能出多少 token，SWA 重放和 KV 管理决定前缀能省多少，异步调度决定 GPU 会不会等 CPU，正确性机制决定这些优化能不能在评测和生产中被信任。

## 2.2 它与生产系统的关系

DeepSeek 官方 Q&A 描述的 V4.1-Flash 推理系统是一个 **E/P/D 分离 + 专用存储** 的架构：Vision Encoder、Prefill、Decode 三个阶段独立部署，前面有 Prefill/Decode/Vision Encoder/Expert-Parallel 四类负载均衡器，旁边有 KV cache 存储集群和 DeepSeek Recipe 负责 API 协议与 token 编解码。

可以用一段文字画出这个系统：

```text
client -> API gateway -> Recipe (protocol + tokenizer)
                         |
                         +-> Vision Encoder pool -> image embeddings
                         +-> Prefill Load Balancer -> P instances -> KV
                         |                                      |
                         |                                3FS KV store
                         |                                      |
                         +-> Decode Load Balancer -> D instances <-+
                                                              |
                                                      metrics / autoscaling
```

开源仓库覆盖的是其中的“引擎计算路径”：模型怎么加载、attention/MoE 怎么算、请求怎么调度、KV 怎么在单实例内分页、推测解码怎么验证、结构化输出怎么掩码。它明确说明，要组成完整推理服务，还缺三块：

- **KV cache system**：跨请求、跨节点、可持久化的缓存，以及 3FS 上的存取和 GC。
- **VE/Prefill/Decode disaggregation**：把三个阶段拆到不同机器，并做 KV 传输与调度。官方说“大部分引擎功能已实现”，但部署层没有开源。
- **跨 data-parallel 副本的负载均衡**：prefill 和 decode 的全局路由。

这三块恰好是“单机引擎”与“生产服务”之间最难的鸿沟。它们涉及集群、存储、网络、故障域和容量规划，不能靠一个 Python 包解决。本书会在第 14、15 章分别拆解 KV 系统和 PD/负载均衡的设计，在第 19 章讨论它们带来的容量与故障问题，并在第 27 章讨论从 toy 到生产还差什么。

## 2.3 设计目标

官方 Q&A 把系统目标总结为三条，这三条也是理解所有技术选择的钥匙。

**第一，支持多厂商加速器。** 同一套框架要跑在 NVIDIA SM100 和华为 Ascend 950 上。这意味着 kernel 层要有平台抽象，通信层要能切换 NCCL/HCCL，低精度格式要能在两种硬件上表达。仓库里 `deep_cluster/hardware/` 和 `deepinfer/platform.py` 就是这层抽象的一部分。

**第二，围绕 commodity hardware 组织 prefill 和 decode。** prefill 用普通的 8 卡机器，decode 用 64 到 128 卡的 supernode；计算、通信、调度都按两个阶段的负载特征分别设计。这解释了为什么 DeepInfer 不把 TP 作为默认方案，也不把 CP 当成所有场景的默认：它用 EP/DP 和 Wide-EP 作为主轴，同时提供 balanced CP 作为长上下文和特定拓扑下的补充。目标是在普通机器和超节点之间做性价比最优的组合。

**第三，KV cache 保留超过 72 小时。** 长上下文和 Agent 负载会反复命中相同前缀，保留 KV 能显著减少重复 prefill。代价是存储容量、命中率管理和跨节点传输。官方给出的 TTL 是 Agent 流量 3 天、Chatbot 流量 7 天；3FS 集群约 100 PiB，按每 GPU 14.4 TB 逻辑容量规划。

这三条目标互相牵制。多厂商支持会增加 kernel 分支；长 KV 保留会挤占存储和网络；Wide-EP 会放大通信和负载不均衡。DeepInfer 的很多“看起来奇怪”的设计，都是在这些约束下求局部最优。

## 2.4 与 vLLM、SGLang、TensorRT-LLM 的关系

DeepInfer 的 README 明确写了：它最初主要基于 vLLM 构建，并受到 SGLang 和 TensorRT-LLM 的启发。这不是客套话，代码里能看到大量 vLLM 的影子：`EngineArgs`、`CacheConfig`、`SchedulerConfig`、`Sequence`、分页 KV、continuous batching 的术语体系都一脉相承；C++ 调度器里也能看到 vLLM 调度策略的改造痕迹。

但它不是 vLLM 的一个分支那么简单。差异主要在四处。

第一，**模型绑定更深**。DeepInfer 只支持 DeepSeek-V4.1-Flash，因此可以针对 CED、CSA2、Engram、DSpark、mHC 这些结构做专门优化，而不必维护一个通用模型注册表。代价是换模型需要改代码。

第二，**kernel 栈更垂直**。FlashMLA、DeepGEMM、DeepEP、DeepSelect、TileLang 都是 DeepSeek 自己维护或深度参与的库，DeepInfer 可以直接按 kernel 的布局和约束组织张量，减少通用框架的抽象开销。

第三，**并行策略更偏 EP/DP**。vLLM 的 TP 生态更成熟；DeepInfer 在 decode 侧选择 Wide-EP，用专家并行换更低的单步内存流量，并用 EPLB 处理专家热点。

第四，**开源边界不同**。vLLM 试图覆盖从单机到分布式、从离线到在线的完整路径；DeepInfer 这次只开放计算引擎，把 KV 存储、PD 分离和全局负载均衡留在内部。这个选择降低了开源维护面，但也意味着它更像“生产引擎的核心切片”，而不是“开箱即用的服务平台”。

SGLang 的影响主要体现在调度和前缀缓存思路上；TensorRT-LLM 的影响主要体现在 kernel 融合、CUDA Graph 和低精度执行上。读 DeepInfer 时，如果某个设计似曾相识，先问“它为了 V4.1 的哪个结构做了改动”，通常能找到答案。

## 2.5 硬件与构建门槛

官方列出的前置条件是：Linux、GCC 13 及以上、Python 3.12、CUDA 13（NVIDIA）或 CANN 9.2（Ascend）。构建时先创建空的 Python 3.12 环境，导出 `DEEPINFER_ACCELERATOR=nvidia` 或 `ascend`，然后执行 `./build.sh`。默认 checkpoint 是 `deepseek-ai/DeepSeek-V4.1-Flash-1017`，首次运行会下载到 Hugging Face cache；设置 `DEEPINFER_HF_MODEL_DIR` 可以使用本地副本。

这些门槛说明 DeepInfer 不是一个“pip install 就能跑”的框架。它假设你有 SM100 或 Ascend 950 级别的硬件、能编译 C++/CUDA、能管理大模型权重。也正因为如此，本书的 mini 实现选择 CPU + NumPy，不是为了替代它，而是为了让你在没有这些硬件时仍能理解并验证机制。

如果你的目标是学习，CPU 版足够；如果你的目标是生产，先确认三件事：模型是不是 V4.1-Flash，硬件是不是受支持的加速器，团队有没有能力维护 3FS、PD 分离和监控。三者缺一，使用通用框架可能更省事。

## 2.6 仓库地图

打开仓库，最值得先看的目录是：

- `deepinfer/engine/`：`LLMEngine`、`EngineArgs`、`profile_server`、`stats_logger`。请求从这里进入。
- `csrc/engine/`：C++ 调度器、block manager、sequence、ZOS、MTP varlen。性能敏感的调度逻辑在这里。
- `deepinfer/worker/`：`Worker`、`ModelRunner`、`CacheEngine`、CUDA Graph runner。设备侧执行。
- `deepinfer/model_executor/`：模型、层、kernel 封装、并行状态、EPLB、输入 metadata。
- `deepinfer/model_executor/models/v4_modules/`：V4.1 的 attention、compress、index_attn、moe、engram、yoco、mHC。
- `deepinfer/structure_output/`：xgrammar 编译器、matcher、触发器与回滚。
- `deepinfer/distributed/`、`deepinfer/model_executor/parallel_utils/`：共享内存广播、并行组、CP/EP 通信。
- `vendor/`：DeepEP、DeepGEMM、DeepSelect、FlashMLA 等子模块。
- `eval/`、`examples/`、`tests/`：评测、离线示例和回归测试。

`doc/inference_deployment_qa.md` 是理解生产系统的最好入口；它比 README 更详细地解释了 E/P/D、3FS、EPLB、DSpark 和异步调度。本书的很多数字来自这份 Q&A，引用时请以官方原文为准。

## 2.7 谁应该用 DeepInfer

如果你要服务的模型就是 DeepSeek-V4.1-Flash，硬件是 SM100 或 Ascend 950，并且你有能力维护集群、3FS 和监控，那么 DeepInfer 提供了一个接近生产形态的起点。它的 kernel 和调度是针对这个模型调过的，省去了从通用框架重新适配的工作。

如果你要服务多种模型、需要开箱即用的 OpenAI API、或者只有消费级 GPU，DeepInfer 不是最省事的选择。vLLM、SGLang、llama.cpp 或云厂商的托管推理更合适。DeepInfer 的价值更多在于“看一个顶级实验室如何把推理系统做到极致”，以及“在自己的引擎里借鉴它的调度、KV、EPLB 和推测解码设计”。

还有一个常被忽略的用途：把 DeepInfer 当作“能力清单”。即使你不部署它，也可以拿它的特性列表逐项检查自己的服务：有没有分页 KV，有没有前缀缓存，有没有 chunked prefill，有没有推测解码，有没有 batch invariance，有没有结构化输出，有没有 PD 分离。这个清单比任何 benchmark 都更能暴露系统的短板。

## 2.8 用一组数字看这个系统

把公开资料里的生产数字放在一起，可以更直观地理解 DeepInfer 的设计目标。

公开口径的单个服务集群（可能跨多个数据中心）每天 155 万亿 token，约等于每秒 17.9 亿 token。平均 TTFT 约 400 毫秒、P99 约 1 秒，对应“对话可用”的等待感；平均输入长度约 77K token，说明长上下文不是边缘场景，而是常态。decode 约 230 token/s，编码场景可达 300 token/s，这是多人共享集群时的用户体验指标，不是单请求峰值。

输入缓存命中率约 96%，是最值得注意的数字。它意味着绝大多数输入 token 没有重新做 prefill，而是直接命中 KV。公开报道提到，命中缓存后每卡 prefill 吞吐可以从 6 万 token/s 提升到 150 万 token/s，约 25 倍；这个倍数是特定负载和硬件下的结果，但方向很清楚：长上下文服务的经济性主要取决于缓存，而不是单纯的 kernel 速度。

KV 存储约 100 PiB，TTL 为 Agent 流量 3 天、Chatbot 流量 7 天；SWA 的本地缓存有一个约 180 秒的复用窗口，窗口内命中率超过 98%。这里的 180 秒是“本地 SWA 缓存多久内还可能被复用”的工程窗口，不是 state 的持久化 TTL；SWA state 本身按序列和窗口组织，不按时间组织。这些数字说明 KV 不是“请求结束就丢”的临时状态，而是一级存储系统，需要容量规划、GC、分层和跨机传输。

benchmark 方面，公开资料给出的 decode 配置是 EP64DP64、nextn=3、接受率约 75%；这里的 nextn=3 是该 benchmark 的配置，不是框架默认值（第 3 章提到默认 nextn=5，做 baseline 时要显式设 0）。在 256 请求/rank、131K 上下文时超过 25,000 输出 token/s/GPU，约 100 token/s/user；8 请求时可达 300 token/s/user。prefill 配置是 EP4DP4、140 token 有界重放，在 4,096 token/rank 时约 6 万非缓存 token/s/GPU，65,536 token/rank 时接近 9 万，单步约 730 毫秒。所有公开 benchmark 都在 NVIDIA 上完成；Ascend 950 支持存在，但没有公开的 Ascend 性能数字。

这些数字有三个使用边界。第一，它们是厂商自报，未经第三方独立验证。第二，它们对应特定的模型、精度、上下文长度、batch 和互联配置，不能直接套到别的场景。第三，它们描述的是“生产集群的稳态”，不是“单机 demo 的峰值”。读 benchmark 时，先问“它测的是什么工作点”，再看数字。

## 2.9 EP64DP64 到底是多少张卡

公开 benchmark 里经常出现 `EP64DP64`。第一次看到这个写法，很容易把它理解成“64 乘 64，等于 4096 张卡”，或者理解成“64 个专家并行组，每组 64 张卡”。两种理解都不对。在 DeepInfer 的部署语境里，EP 和 DP 是同一批 GPU 的两种**视图**：EP 描述 MoE 专家怎么切，DP 描述 attention 和非专家部分怎么复制。`EP64DP64` 通常指一个 64 卡的通信域，每张卡持有 `物理专家数 / 64` 个本地专家，同时 attention 部分在这个域内做 64 路数据并行。卡数是 64，不是 4096。

为什么可以这样重叠？因为 MoE 层和 attention 层的并行需求不同。attention 的每个请求需要完整的 hidden，但不同请求之间没有依赖，适合 DP；MoE 的专家很多，单个专家放不进一张卡，适合 EP。把同一组 64 张卡同时看成“64 路 DP”和“64 路 EP”，就不需要为两种并行各准备一套机器。代价是通信域变大：一次 all-to-all 要 64 个 rank 参与，一张卡故障会影响整个域。

这也解释了公开数字里的另一个困惑：prefill 配置写成 `EP4DP4`，只有 4 张卡，为什么能和 64 卡的 decode 配置放在同一张 benchmark 表里？因为 prefill 和 decode 是分开部署的，两者的并行规模本来就不需要相同。prefill 的 batch 大、矩阵大，小 EP 能让每个专家的 GEMM 更大；decode 的 batch 小、权重流量主导，大 EP 能把权重摊到更多卡上。把它们乘起来或者强行对齐，会得到错误的容量结论。

读任何并行配置时，先问三个问题：这个数字是卡数、进程数，还是通信域大小？EP 和 DP 是重叠的还是独立的？这个配置属于 prefill 还是 decode？把这三个问题回答清楚，再去看吞吐数字。第 15.1 节会给出四种并行的组合方式，第 15.9 节会把公开 benchmark 的工作点逐项拆开。

## 小结

DeepInfer 是 DeepSeek 生产推理系统的计算引擎切片：它把 V4.1-Flash 的模型结构、kernel、并行、调度和正确性机制开放出来，但不包含完整的 KV 存储、PD 分离部署和全局负载均衡。它继承 vLLM 的工程骨架，又为 DeepSeek 的模型和硬件做了深度定制。理解这条边界，是读懂后面所有章节的前提。

## 练习

1. 用自己的话解释“开源了计算引擎，但没有开源完整推理服务”具体缺了哪些组件。
2. 列出 DeepInfer 的三条设计目标，并为每条目标举一个它会导致的架构选择。
3. 如果你只有 8 张消费级 GPU，想服务一个 7B 模型，DeepInfer 是合适选择吗？给出三条理由。
4. 用 2.2 节的系统图，标出哪些组件在开源仓库里、哪些不在。对每个缺失组件，写出它至少依赖的一种基础设施。
