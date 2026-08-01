# 01 认识 vLLM

> 本章目标：弄清楚 vLLM 是什么、它解决了什么问题、有哪些关键特性、与其他推理框架的差异，以及什么时候该用它。

---

## 1. vLLM 是什么

**vLLM 是一个开源的、高吞吐量的大语言模型（LLM）推理与 serving 引擎。** 它由 Python 编写，核心性能敏感的算子（如 PagedAttention 内核）用 CUDA / Triton 实现，直接跑在 NVIDIA GPU 上（也逐步支持 AMD ROCm / Intel 等后端）。

它的定位很明确：

- **不是**一个模型训练框架，而是把"训练好的权重"高效地跑起来的引擎；
- **不是**一个单次推理的 demo，而是一个面向在线服务（serving）的引擎，强调**吞吐量（throughput）**；
- 对外提供 OpenAI 兼容的 HTTP 服务，也提供 Python 高层 API（`LLM` 类）和离线批处理接口（`LLM.generate`）。

一个直观感受：在相同 GPU 上，vLLM 的推理吞吐通常能达到 naive 实现的 **数倍到十几倍**，这主要来自对 KV cache 内存的管理优化（PagedAttention）和调度优化（continuous batching）。

安装非常简单：

```bash
pip install vllm
```

之后用一行命令就能启动一个 OpenAI 兼容的服务：

```bash
python -m vllm.entrypoints.openai.api_server \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --max-num-seqs 32 \
    --gpu-memory-utilization 0.9
```

---

## 2. 起源：PagedAttention 论文

vLLM 的起点是 2023 年发表于 SOSP（ACM Symposium on Operating Systems Principles，操作系统领域顶会）的论文：

> **"Efficient Memory Management for Large Language Model Serving with PagedAttention"**
> Kwon, Li, Zhuang, Sheng, Zheng, Yu, Gonzalez, Zhang, Stoica（UC Berkeley）

这篇论文提出的核心思想 **PagedAttention**（分页注意力）借鉴了操作系统的**虚拟内存分页**思想，把 KV cache 切分成固定大小的"页（block）"，从而消除了 KV cache 的碎片化和内存浪费。

关键事实链：

- vLLM 最初诞生于 **UC Berkeley 的 LMSYS 实验室**（做 Chatbot Arena 的那个团队）；
- 论文发表后项目独立发展，如今是 **vLLM Project**（vllm-project），由社区维护，并得到 NVIDIA、Anthropic、Red Hat 等公司工程师的持续贡献；
- 论文的 v1 于 2023 年 6 月在 arXiv 公开，SOSP 2023 正式发表，并获得了当年的 Best Paper Award。

> 一句话记忆：**vLLM = 操作系统分页思想 × Transformer 推理**。

---

## 3. 它解决的核心问题

在 vLLM 出现之前，主流 serving 方案存在两个痛点：

### 3.1 KV cache 的内存碎片与浪费

自回归解码时，每个 token 的 KV 需要被缓存。但序列长度是动态变化的（请求分批到达、长短不一），如果按**最大序列长度**预分配连续内存：

- **内部碎片（internal fragmentation）**：为每个请求预留 `max_len` 的连续空间，但实际只用到一小部分，剩余空间白白占着；
- **外部碎片（external fragmentation）**：内存被不同长度请求切成碎片，即使总量充足，也找不到一段连续内存容纳新请求。

论文实测显示，传统方案中 KV cache 内存利用率往往只有 **20%–40%**，即 **60%–80% 的内存被浪费**。这不只是内存不够用的问题——浪费直接导致能同时服务的请求变少，吞吐量随之下降。

### 3.2 静态批处理导致的低吞吐

早期推理框架采用**静态批处理（static batching）**：一批请求要么全部跑完、要么全部失败，批内只要有请求结束就得等慢的请求，GPU 上不断出现空闲 bubble。这让 GPU 利用率很低，尤其在长尾延迟场景。

### vLLM 的对应解法

| 问题 | vLLM 的解法 |
|---|---|
| KV 内存碎片 | **PagedAttention**：KV 按 16-token 的固定块分配，用块表做逻辑→物理映射 |
| 静态批处理 | **Continuous batching**：每步（iteration）动态增删请求，GPU 时刻满载 |
| 共享 prompt 浪费 | **Prefix caching**：相同前缀的 KV 块复用 |
| 长 prompt 拖累延迟 | **Chunked prefill**：prefill 拆块与 decode 交错执行 |

这四个机制就是 vLLM 高性能的骨架，详细原理见《02-核心原理.md》。

---

## 4. 关键特性与里程碑

按时间线整理（以实际发布的 feature 为准）：

- **PagedAttention（2023，v0.1）**：分块管理 KV cache，论文核心贡献。
- **Continuous batching（2023）**：iteration-level 调度，替代静态 batch。
- **OpenAI 兼容 API Server（2023）**：`--api-server`，后来演化为 `vllm.entrypoints.openai.api_server`，成为接入生态的事实标准。
- **Prefix caching（2023–2024，`--enable-prefix-caching`）**：复用公共前缀的 KV 块，对 RAG / 多轮对话收益显著。
- **Chunked prefill（2024，`--enable-chunked-prefill`）**：长 prompt 不再阻塞 decode。
- **量化支持（持续）**：GPTQ、AWQ（weight-only 4bit）、FP8（W8A8）、GGUF 等，`--quantization` 参数可选。
- **张量并行与流水并行（2023–2024）**：`--tensor-parallel-size`、`--pipeline-parallel-size`，单卡放不下时横向扩展。
- **多模态支持（2024）**：`--multi-modal`，支持 LLaVA、Qwen-VL 等视觉语言模型，进入 `vllm/multimodal/`。
- **LoRA / 微调推理（2024）**：`--enable-lora`，支持同一模型服务多个 LoRA 适配器。
- **Speculative decoding（2024，`--speculative-config`）**：draft model + target model 投机采样，无损提速。
- **结构化输出（2025）**：`guided_json` / `guided_regex` 等引导解码。

代码结构上，核心模块位于（以 V1 为主干，≥1.0 的目录布局）：

```
vllm/
  v1/              # V1 主目录：engine（EngineCore）、core（调度/KV cache）、worker、attention 后端
  entrypoints/     # OpenAI 兼容 server（openai/）、离线 LLM（llm.py）、CLI
  engine/          # 历史 V0 的 LLMEngine/AsyncLLMEngine（≤0.11 保留，≥1.0 已删）
  model_executor/  # 模型加载、各架构 Model、TP/PP 切分、量化层
  attention/       # PagedAttention 算子与 flash/triton 等后端
  serving/         # OpenAI 协议转换、tool_parsers
  sampler/         # logits 后处理、采样、guided decoding
  config/          # ModelConfig、CacheConfig、SchedulerConfig、ParallelConfig 等
  spec_decode/     # 投机解码 draft/verify
  distributed/     # NCCL、并行组、KV 传输（PD 分离）
```

> 读法：以 `vllm/v1/` 为主线；V0 路径只对 ≤0.11 的历史版本有意义。

> **V0 与 V1**：vLLM 早期引擎称为 V0，v0.6 起**试验性**引入 V1、v0.7 起逐步成为默认、**v1.0（2025 年末）起 V0 被彻底移除，V1 成为唯一引擎**。V1 重写了调度器与 KV cache 管理（默认开启 prefix caching、更激进的批处理、多步调度），并把引擎前后端拆成 `EngineCore`（独立进程）与前端。教程第 03 章会详细讲 V1 架构；`VLLM_USE_V1` 切换开关仅对 v0.6–v0.9 有效，v1.0+ 已无意义。多数 `--` 参数在 V0/V1 下行为基本一致，个别调度相关 flag 仅对旧版本 V0 生效。

### 4.1 版本演进：V0 与 V1 两代引擎

理解 vLLM 的版本演进，是读懂社区讨论和踩坑日志的关键。用一张时间线整理：

| 版本代际 | 时期 | 调度器 | KV cache | 引擎形态 | 状态 |
|---|---|---|---|---|---|
| **V0** | 2023 – 2024 | `vllm/core/scheduler.py`（`Scheduler`） | BlockManager v1（`block_manager_v1.py`），prefix caching 默认关闭 | 单体 `LLMEngine` / `AsyncLLMEngine` | **已在 vLLM 1.0（2025 末）移除，仅历史参考** |
| **V1** | 2024 末 – 至今 | `vllm/v1/core/sched/scheduler.py`（`SchedulerV1`） | `KVCacheManager`（`vllm/v1/core/`），默认开 prefix caching、引用计数共享 | 前后端拆成 `EngineCore`（独立进程）+ 前端 Client | **唯一引擎（≥1.0）** |

V1 相比 V0 的关键差异：

1. **调度更激进**：`max_num_batched_tokens` 变为动态预算，不再需要手动调 `--max-num-batched-tokens`；连续批处理更密集。
2. **KV cache 默认共享**：prefix caching 默认开启（V0 需 `--enable-prefix-caching`）；block 用引用计数管理，支持跨请求共享公共前缀。
3. **前后端解耦**：调度/执行/KV 管理收进 `EngineCoreProcess`，通过 zmq RPC 与共享内存与前端通信——前端崩溃不再拖垮推理进程。
4. **块管理更紧凑**：V1 的 block 表带"每 block 有效长度"信息，减少部分块浪费与无效 kernel 工作。

**切换方式（历史）**：v0.6–v0.9 可用 `VLLM_USE_V1=0/1` 切换；**v1.0 起 V0 已移除，该开关无效**。遇到调度/内存行为与文档不符时，第一件事仍是确认版本（`vllm --version` + 日志里的 engine 标识）与是否用了 1.0 前的旧参数。

> 教程正文以主流稳定行为主线；遇到"我看到的参数和教程对不上"，多半是 V0/V1 或版本差异，查第 16 章附录 A 的环境变量与参数参考。

### 4.2 版本演进时间线：从 v0.1 到 v1.0

除了"V0/V1 两代引擎"这条主线，再给一张**具体 release 的印象表**（patch 号与功能归属以官方 GitHub Releases / CHANGELOG 为准，这里只求"大概在哪个时期出现了什么"）：

| 版本（近似） | 时间（近似） | 主要引入 / 标志性内容 |
|---|---|---|
| v0.1 | 2023 年中 | PagedAttention 内核、`LLMEngine` / `AsyncLLMEngine`、OpenAI 兼容 API 雏形（论文发布后首个公开版本） |
| v0.2 – v0.3 | 2023 下半年 | continuous batching 完善、GPTQ 量化、张量并行（TP）落地 |
| v0.4 | 2024 年初 | FP8（W8A8）、多模态支持、LoRA 推理、API server 结构成熟 |
| v0.5 | 2024 年中 | 引导解码 / 结构化输出（outlines）、prefix caching 机制增强 |
| v0.6 | 2024 年末 | **V1 引擎试验性引入**、chunked prefill 行为改进、Mamba 等新架构支持 |
| v0.7 | 2025 年初 | V1 逐步成为默认、多模态能力增强、投机解码方案（EAGLE / Medusa）支持变多 |
| v0.8 – v0.9 | 2025 年 | V1 全面默认、显存管理与调度进一步优化、更多硬件后端（CPU / XPU / AMD）覆盖 |
| v1.0 | 2025 年 | 首个稳定大版本，V1 引擎成为唯一主线，长上下文与稳定性增强 |

> 记忆锚点：**v0.6 是 V1 的"分水岭"**——V1 从试验性走向默认，v1.0（2025 末）彻底移除 V0。社区里讨论"调度行为奇怪"，十有八九是版本差异，先 `vllm --version` 确认版本，再查第 16 章附录 A。

> **2026 快照**：本教程以 2024–2025 年稳定特性为主线；v1.0 之后的演进（多步调度默认化、LMCache 集成、PD 分离产品化、推理时缩放与 reasoning 支持）在第 12 章"未来方向"与第 16 章附录 A 更新版中覆盖。看到"我用的参数和教程对不上"，先确认是不是 1.0 前的 V0 参数。

### 4.3 硬件与后端支持矩阵

| 后端 | 状态 | 说明 |
|---|---|---|
| **NVIDIA CUDA** | ✅ 一等公民 | 官方主线，性能与功能最全；PagedAttention 等核心内核针对 CUDA 优化 |
| **AMD ROCm** | ✅ 官方支持 | 通过 `vllm install-roc` 或指定 ROCm wheel 安装；部分新特性会滞后 CUDA |
| **Intel（Gaudi / 部分 XPU）** | ⚠️ 部分支持 | 特定版本 / 官方镜像提供，社区维护，覆盖面有限 |
| **CPU（`--device cpu`）** | ⚠️ 实验性 | 可以跑，吞吐远低于 GPU，常用于开发调试与小规模验证 |
| **Apple MPS（Mac）** | ❌ 不支持 | 官方不提供 MPS 后端；Mac 上建议 llama.cpp 或 CPU 模式 |
| **华为昇腾 / 其它加速卡** | ⚠️ 第三方 | 依赖厂商 / 社区维护的 fork 或插件，跟进速度不定 |

> 注意：vLLM 迭代快，这张表是"快照"；判断"某张卡是否可用、怎么装"以官方 README 的 **Hardware Support Matrix** 章节为准。

### 4.4 社区基准对比：为什么没有"标准答案"

经常有人问"vLLM 比 TensorRT-LLM 快多少？"——这个问题**没有标准答案**，因为性能高度依赖：

- **硬件**：A100 vs H100 vs L40S，显存带宽与算力差异很大；
- **负载形态**：短 prompt 长输出（decode 密集）与长 prompt 短输出（prefill 密集）下，框架优劣可能完全倒挂；
- **版本与配置**：V0/V1、chunked prefill、prefix caching 开关都会改变结果。

**正确的做法**是在"自己的负载 + 自己的卡"上测自己关心的指标（TTFT / TPOT / 吞吐），工具与流程见第 21 章附录 D。官方/社区参考数据源：

| 来源 | 地址 | 说明 |
|---|---|---|
| vLLM 官方 README 的 Performance 章节 | https://github.com/vllm-project/vllm | 部分基准结果，随版本更新 |
| vLLM 论文（SOSP 2023） | 见本文 §2 | 论文里的对比已过时，仅作原理参考 |
| LMSYS gbench | https://github.com/lmsys/gbench | 社区可复现对比工具与报告 |
| 官方 benchmark 脚本 | `vllm/benchmarks/`（第 21 章 §3） | 自测的标准工具 |

> 一句话：**网上任何一个"XX 比 YY 快 N 倍"的数字，先问三件事——什么卡、什么负载、什么版本。** 没有这三要素的对比，仅供参考。

---

## 5. 与其他推理框架的对比

主要竞品/同类框架：

- **Hugging Face TGI（Text Generation Inference）**：Hugging Face 官方 serving，生态好、上手快，支持量化（bitsandbytes、GPTQ、AWQ）。
- **NVIDIA TensorRT-LLM**：基于 TensorRT，追求极致单卡/多卡延迟与吞吐，但构建流程复杂、灵活性差；2025–2026 年持续被 vLLM 的多步调度与更广模型覆盖追赶，性能差距在缩小。
- **llama.cpp**：CPU/消费级 GPU 上的轻量推理，GGUF 量化生态，内存占用极小；新版本增加多模态与共享库（libllama），但并行能力弱、不适合大规模 serving。
- **SGLang**：主打 RadixAttention（前缀树缓存）与结构化生成，与 vLLM 定位最接近；2024–2026 年对 DeepSeek 系做了大量专项优化（DataParallelism、更激进的前缀复用），在长共享前缀的 agent 场景仍有竞争力。
- **LMDeploy**：OpenMMLab 出品，主打 4bit 量化（AWQ）与高性能推理，支持 PyTorch 后端。
- **LMCache**（2024 起，值得单独认识）：不是完整引擎，而是**KV/前缀缓存的跨层复用中间件**，已与 vLLM 深度集成（`--enable-lmcache`）；它把 KV cache 从"引擎内数据结构"变成"可跨请求、跨节点复用的缓存层"，是 PD 分离（第 12 章）的基础设施。
- **xft / LightLLM**：DeepSeek 的 eXascale Flash Transformer（DeepSeek 在线服务自用，开源）与国内的 LightLLM，两者在大规模生产部署上也有真实用户，但生态与文档面远小于 vLLM。

对比表（粗略，按主流版本）：

| 特性 | vLLM | TGI | TensorRT-LLM | llama.cpp | SGLang | LMDeploy |
|---|---|---|---|---|---|---|
| PagedAttention / 分页 KV | ✅ 原创 | ❌ 传统 | ⚠️ 自有块管理 | ❌ | ✅ RadixAttention | ⚠️ 自有实现 |
| Continuous batching | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| 量化支持 | GPTQ/AWQ/FP8/GGUF | bitsandbytes/GPTQ/AWQ | FP8/INT4/INT8 | GGUF 为主 | 依托 vLLM/自研 | AWQ/FP8/INT8 |
| 张量/流水并行 | ✅ ✅ | ✅（部分） | ✅ ✅（最强） | ❌ | ✅ | ✅ |
| OpenAI 兼容 API | ✅ 标准 | ✅ | ❌（需自建） | ❌ | ✅ | ✅ |
| 多模态 | ✅（LLaVA 等） | ⚠️ 有限 | ✅ | ❌ 有限 | ✅ | ⚠️ 有限 |
| LoRA 推理 | ✅ 原生 | ⚠️ 有限 | ⚠️ | ❌ | ✅ | ✅ |
| 框架深度/可定制 | 高（纯 Python 内核可读） | 中 | 低（图编译黑盒） | 低 | 高 | 中 |
| 上手难度 | 低 | 低 | 高 | 极低 | 中 | 中 |

**诚实点评**：

- **TensorRT-LLM** 在"极限吞吐/延迟"上仍是标杆，但工程复杂度高、模型覆盖和社区迭代速度不如 vLLM；vLLM 则用可读的 Python + CUDA 内核换来了开发速度和灵活性，性能差距在快速缩小。
- **SGLang** 在需要复杂前缀复用（如大量共享 system prompt 的 agent 场景）时可能有优势，但生态和模型支持面仍比 vLLM 小。
- **llama.cpp / GGUF** 适合个人电脑、边缘设备，但基本不适用于生产级多用户服务。
- **TGI** 胜在 Hugging Face 生态整合，纯 HF 用户迁移成本最低。

---

## 6. 谁在生产环境使用它

vLLM 已被大量公司部署（以下信息来自公开资料，仅作参考）：

- **OpenAI**：曾公开表示其部分在线推理服务使用了 vLLM（2024 年 SVP 在访谈中提到）。
- **Microsoft**：Azure 及研究团队在多个内部服务与开源工具（如某些推理 pipeline）中采用 vLLM。
- **Together AI**：vLLM 早期核心贡献者所在公司之一，其 GPU 云推理大规模基于 vLLM。
- **Anthropic 相关**：Anthropic 的工程师是 vLLM 项目的活跃贡献者（多位 commit 来自 Anthropic），说明其基础设施与 vLLM 有深度关联。
- 其他：国内外的众多 AI 公司（字节跳动、阿里、MiniMax 等）也在内部大量使用，社区开源项目（如 LangChain、Ray Serve、OpenWebUI）都默认对接 vLLM。

> 注意：公司使用情况随时间变化，以上仅列举有公开依据的例子；判断某个框架是否"生产可用"，更要看其活跃度、issue 响应和版本稳定性。

---

## 7. 什么时候选 vLLM，什么时候不选

### ✅ 推荐选 vLLM 的场景

- **在线多用户服务**：需要高吞吐、低排队延迟，例如 API 网关、聊天机器人、Agent 底座；
- **大量并发 / 长短请求混合**：连续批处理和分页 KV 能充分压榨 GPU；
- **需要 OpenAI 兼容接口**：无缝替换 `openai` SDK；
- **共享前缀多**：RAG、多轮对话、system prompt 固定的场景，prefix caching 收益巨大；
- **需要多卡扩展 / 多模态 / LoRA / 量化**：vLLM 一揽子支持；
- **团队要读源码 / 二次开发**：vLLM 结构清晰，是学习 serving 系统的最佳教材。

### ❌ 不建议选 vLLM 的场景

- **需要极致的单请求延迟**（如 5ms 级）：可能优先考虑 TensorRT-LLM 或编译型方案；
- **个人电脑 / 低端 GPU / CPU 推理**：llama.cpp 更轻；
- **只想快速跑个 demo、不想处理 CUDA 环境**：先用 transformers + TGI；
- **对模型/算子支持有特殊定制需求**：vLLM 对非主流架构的适配可能滞后，编译型框架或自研更可控；
- **显存极小（<4GB）**：vLLM 的 PagedAttention 也需要一定显存做 cache，这时 GGUF 小量化模型更现实。

### 🔍 决策速查表（场景 → 选型）

把上面的推荐 / 不推荐场景浓缩成一张表，方便对着自己的处境快速判断：

| 你的场景 | 首选 | 关键理由 |
|---|---|---|
| 生产级在线服务（高并发、长短混排、OpenAI 兼容） | **vLLM** | 连续批处理 + 分页 KV，生态最全（本教程主线） |
| 极致单请求延迟（5ms 级） | TensorRT-LLM / 编译方案 | 编译内核消除 Python 解释与动态调度开销 |
| 个人电脑 / 低端 GPU / CPU / Mac | llama.cpp | 内存占用极小，GGUF 量化生态成熟 |
| 快速跑 demo、不想碰 CUDA 环境 | transformers + TGI | 上手最快，HF 生态整合好 |
| 大量共享前缀的 agent / RAG 场景 | vLLM（prefix caching）/ SGLang | 前缀复用收益最大，命中率高时吞吐翻倍 |
| 复杂结构化生成（JSON / 文法） | vLLM 引导解码 / SGLang | FSM 在采样期约束输出，零后处理 |
| 需要多卡 / 多模态 / LoRA / 量化全家桶 | **vLLM** | 一揽子支持，`--tensor-parallel-size` 等开箱即用 |
| 深度定制引擎 / 学习 serving 原理 | **vLLM（读源码）** | Python 内核可读，文档与社区活跃 |

> 判断模板：**先看硬件（卡够不够）→ 再看场景（在线还是离线、延迟敏感还是吞吐敏感）→ 最后看生态（要不要 OpenAI 兼容 / LoRA / 多模态）。** 三条都对得上，vLLM 基本就是最优解。

---

## 8. 小结

- vLLM = 开源高吞吐 LLM serving 引擎，Python + CUDA/Triton，出身 UC Berkeley（SOSP 2023 PagedAttention 论文）。
- 核心创新：**PagedAttention（分页 KV）+ Continuous batching（连续批处理）**，解决内存碎片与静态批处理两大痛点。
- 特性全家桶：prefix caching、chunked prefill、投机解码、量化、多卡并行、多模态、LoRA、OpenAI 兼容 API。
- 与 TGI / TensorRT-LLM / llama.cpp / SGLang / LMDeploy 相比，vLLM 在"性能 + 生态 + 可读性"的平衡点上最具优势。

下一章《02-核心原理.md》将深入拆解这些机制背后的原理。
