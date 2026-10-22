# DeepInfer 教程：从生产推理系统到可运行的最小实现

这是一本关于 DeepSeek 开源的 **DeepInfer** 推理框架的中文教程，同时附带一个只依赖 Python 标准库和 NumPy 的**可运行迷你实现**。教程试图同时回答四个问题：

1. 一个大模型推理框架为什么存在，它到底在优化什么？
2. DeepInfer 与 DeepSeek-V4.1-Flash 是如何把这些优化组织成系统的？
3. 如果把规模缩小到一台普通 CPU，哪些机制仍然可以被实现、被测试、被测量？
4. 这些技术选择如何落到成本、竞争、部署、合规、安全、市场和资本回报上？

本书不是 DeepInfer 的官方文档，也不代表 DeepSeek 的立场。生产框架的完整能力依赖 SM100 GPU、Ascend 950 NPU、CUDA 13/CANN 9.2、GCC 13 和 Linux；本仓库的代码是教学实现，不能替代生产引擎。

## 先说明两个 DeepInfer

公开资料里有两个同名项目。本书讲的是 **DeepSeek 于 2026 年 10 月开源的推理框架 DeepInfer**（`deepseek-ai/DeepInfer`），不是 2017 年 MICCAI/SPIE 论文中面向医学影像、以 3D Slicer 和 Docker 模型仓库为核心的 DeepInfer。两者解决的问题、架构和代码都没有关系。阅读旧资料时请先确认这一点。

## 这本书适合谁

- 想从“会调用模型 API”走到“理解推理服务内部”的工程师。
- 准备做推理优化、容量规划、模型部署或 AI 基础设施选型的开发者。
- 需要向团队解释 TTFT、TPOT、KV cache、连续批处理、MoE 专家并行等概念的技术负责人。
- 想读 DeepInfer 源码，但被 C++ 调度器、TileLang kernel 和分布式配置挡在门外的读者。
- 需要把技术指标翻译成成本、TCO、SLA 和部署方案的产品/商业读者。
- 正在把单模型引擎扩展成多模型、多适配器或检索平台的工程师。
- 做 RL 后训练、合成数据或 rollout 基础设施的工程师。
- 做端侧、边缘、浏览器或异构硬件推理的产品/工程团队。
- 需要做容量规划、多区域运营、定价或投资决策的技术负责人。
- 刚接触 Transformer，需要一份从 token 到 logits 的最小背景的初学者。

初学者可以先读第 0 到 4 章和附录 F，再按顺序读第二部分；有系统经验的读者可以直接读第二、三部分；只关心商业与选型的读者可以读第 30 到 32 章，再读第 33 到 37 章的工程与合规，然后读第 47 章的现金流、第 48 章的市场测算和第 49 章的估值融资；如果关心安全与评测，再补第 28、29 章。遇到技术概念回到第二、三部分查。附录 E 提供了完整的实验手册。第 11 章补上了采样、停止条件和输出契约，第 16 章把长上下文与 Agent 负载单独成章；如果你关心线上输出为什么“不稳定”或“首 token 很慢”，可以先读这两章。第 38 到 43 章分别处理多模态、长思维链、离线批处理、多模型/多适配器、检索推理和推理编译器这些专项负载与平台能力；第 44 到 47 章把边界扩展到 RL rollout、端侧、流量工程和财务模型。它们不是“附录式补充”，而是对前文调度、KV、缓存、图和成本模型的重新组合。

## 快速开始

除 NumPy 和 pytest 外，本仓库不需要安装其他第三方包；Python 版本要求 3.10+。Windows、Linux、macOS 都可以运行。

```bash
cd deepinfer-tutorial

# 1. 运行测试（268 个）
python -m pytest -q

# 2. 跑一个离线批量生成
python examples/offline_inference.py

# 3. 跑一个小型吞吐/延迟扫描
python examples/benchmark.py --batch-sizes 1 2 4 8 --max-tokens 16

# 4. 跑一次 roofline 计算
python examples/roofline.py

# 5. 检查文档、链接、示例和测试数量是否一致
python check_docs.py
```

如果只想看引擎能力，可以直接运行：

```bash
python examples/lifecycle.py            # 单个请求的完整生命周期（附录 G）
python examples/chunked_prefill.py      # 分块 prefill 与前缀缓存
python examples/speculative_decoding.py # 目标模型 vs 推测解码
python examples/structured_output.py    # 正则 / JSON Schema / EBNF
python examples/eplb.py                 # 专家并行负载均衡
python examples/parallelism.py          # CP / EP / TP / PD KV 交接
python examples/kv_store.py             # KV block store 的 TTL 与 LRU
python examples/http_server.py          # OpenAI 兼容 HTTP 服务
python examples/chat.py                 # Chat 模板与流式输出
python examples/sampling.py             # 温度 / top-k / top-p / seed / 停止条件
python examples/long_context.py         # 长前缀复用、KV 预算与 SWA 重放
python examples/roofline.py             # FLOPs / 带宽 / 成本模型
```

工程化、专项负载与商业分析相关的示例：

```bash
python examples/manifest.py             # 模型 manifest 与版本校验
python examples/gateway.py              # 鉴权、配额、前缀亲和、drain
python examples/tracing.py              # 请求 span 与延迟归因
python examples/tco.py                  # TCO、单位成本、敏感性与容量规划
python examples/multimodal.py           # 图像 token 预算、embedding 缓存与 EPD 比例
python examples/reasoning.py            # 思考预算、双通道流式与推理成本乘数
python examples/offline_batch.py        # 离线 wave 规划、吞吐与截止时间报告
python examples/multi_model.py          # LoRA 批处理、适配器驻留与模型路由
python examples/embedding.py            # embedding 分桶、向量索引与 rerank
python examples/compiler.py             # 图融合、常量折叠与内存规划
python examples/rl_rollout.py           # 组采样、GRPO 优势、权重版本与部分 rollout
python examples/edge.py                 # 端侧体积、roofline、能耗与云边路由
python examples/traffic.py              # 排队、准入、路由、重试预算与多区域
python examples/finance.py              # NPV、IRR、LTV/CAC、合同与情景分析
```

也可以使用命令行：

```bash
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0
python -m mini_deepinfer.cli chat "Explain prefill and decode." --max-tokens 24
python -m mini_deepinfer.cli serve --port 8000
```

## 目录结构

```text
deepinfer-tutorial/
├── README.md
├── 00-preface.md ... 51-conclusion.md   # 正文（52 章）
├── appendix-a-glossary.md ...           # 附录（10 篇）
├── mini_deepinfer/                      # 可运行的迷你引擎
│   ├── engine.py                        # 调度 -> 执行 -> 采样 -> 回收
│   ├── scheduler.py                     # 连续批处理、chunked prefill、抢占
│   ├── sampling.py                      # 温度、top-k/top-p、采样与 logprob
│   ├── sampling_params.py               # 采样参数校验、seed、停止条件
│   ├── sequence.py                      # 序列状态、RNG、finish_reason
│   ├── tokenizer.py                     # 字节级 tokenizer 与流式解码
│   ├── block_manager.py                 # 分页 KV、前缀哈希、COW
│   ├── cache_engine.py                  # PagedKVCache、KVBlockStore
│   ├── speculative.py                   # DSpark 风格的推测解码
│   ├── structured_output.py             # 正则/JSON/EBNF -> NFA -> mask
│   ├── eplb.py                          # 冗余专家与贪心放置
│   ├── parallel.py                      # CP/EP/TP/PD 的逻辑模拟
│   ├── pipeline.py                      # 异步调度与形状缓存成本模型
│   ├── quantization.py                  # FP8/FP4 量化与 KV 字节计算
│   ├── sparse_attention.py              # 块稀疏注意力机制实验
│   ├── yoco.py                          # SWA 命中判定与有界重放
│   ├── roofline.py                      # FLOPs/带宽/加速比/单位成本模型
│   ├── economics.py                     # TCO、毛利、容量规划、敏感性
│   ├── market.py                        # TAM/SAM/SOM、采用曲线、漏斗、销售产能
│   ├── manifest.py                      # 模型/权重/版本绑定与校验
│   ├── gateway.py                       # 鉴权、配额、前缀亲和、drain
│   ├── tracing.py                       # 请求级 span 与延迟归因
│   ├── vision.py                        # 迷你 ViT / EPD 视觉编码实验
│   ├── multimodal.py                    # 图像 token 预算、embedding 缓存、VE 批处理
│   ├── reasoning.py                     # 思考预算、双通道流解析、推理成本模型
│   ├── offline_batch.py                 # 离线 wave 规划、执行与报告
│   ├── lora.py                          # LoRA、按行适配器批处理、LRU 驻留
│   ├── router.py                        # 模型路由、级联与盈亏接受率
│   ├── embedding.py                     # 双塔编码、分桶、索引、rerank
│   ├── compiler.py                      # 图 IR、折叠、融合、内存规划
│   ├── rl_rollout.py                    # 组采样、GRPO、权重版本、部分 rollout
│   ├── edge.py                          # 端侧内存、roofline、能耗、云边路由
│   ├── traffic.py                       # 排队、准入、路由、熔断、多区域
│   ├── finance.py                       # NPV、IRR、LTV/CAC、合同、情景分析
│   ├── recipe.py                        # 多协议消息归一化与流式事件解析
│   ├── server.py                        # OpenAI 兼容 HTTP/SSE
│   ├── model/                           # 玩具 Transformer 与 V4 机制实验室
│   └── assets/                          # 训练好的玩具权重与词表
├── examples/                            # 28 个可运行示例
├── tests/                               # 268 个 pytest 测试
├── deploy/                              # Dockerfile、Kubernetes 清单与说明
├── ci/                                  # GitHub Actions 示例与说明
├── train_tiny.py                        # 纯 NumPy 训练脚本
└── check_docs.py                        # 文档自检
```

## 正文地图

### 第一部分：从模型到服务

- [第 0 章 前言：如何读这本书](00-preface.md)
- [第 1 章 为什么需要推理框架](01-why-inference-engine.md)
- [第 2 章 DeepInfer 全景](02-deepinfer-overview.md)
- [第 3 章 快速上手与源码地图](03-quickstart-source-map.md)
- [第 4 章 推理系统的性能模型](04-performance-model.md)

### 第二部分：模型、算法与数值

- [第 5 章 DeepSeek-V4.1-Flash 架构总览](05-model-architecture-v41.md)
- [第 6 章 注意力、KV 压缩与有界重放](06-attention-kv-compression.md)
- [第 7 章 MoE 与专家并行](07-moe-expert-parallel.md)
- [第 8 章 推测解码 DSpark](08-speculative-decoding.md)
- [第 9 章 结构化输出](09-structured-output.md)
- [第 10 章 低精度、确定性与 batch invariance](10-low-precision-determinism.md)
- [第 11 章 采样、解码与输出契约](11-sampling-decoding.md)

### 第三部分：引擎内部

- [第 12 章 引擎总体架构](12-engine-architecture.md)
- [第 13 章 调度器](13-scheduler.md)
- [第 14 章 KV cache 系统](14-kv-cache-system.md)
- [第 15 章 并行、通信与 PD 分离](15-parallelism-communication.md)
- [第 16 章 长上下文与 Agent 负载工程](16-long-context-agent.md)
- [第 17 章 异步调度、CUDA Graph 与性能工程](17-async-cuda-graph-performance.md)
- [第 18 章 GPU 执行模型与 kernel 工程](18-gpu-kernels.md)
- [第 19 章 可靠性、运维与容量规划](19-reliability-operations.md)

### 第四部分：动手实现 mini-deepinfer

- [第 20 章 mini-deepinfer 的设计与边界](20-mini-design.md)
- [第 21 章 玩具模型、Tokenizer 与反向传播](21-mini-model-kernels.md)
- [第 22 章 分页 KV 与调度器实现](22-mini-paged-kv-scheduler.md)
- [第 23 章 推测解码与结构化输出实现](23-mini-speculative-structured.md)
- [第 24 章 EPLB、并行模拟、TP 与 KV 分层](24-mini-eplb-parallel-kv.md)
- [第 25 章 HTTP 服务与可观测性](25-mini-http-observability.md)
- [第 26 章 测试、验证与性能实验](26-testing-validation.md)
- [第 27 章 从玩具到生产](27-from-toy-to-production.md)

### 第五部分：安全与评测

- [第 28 章 安全、隐私与多租户](28-security-privacy.md)
- [第 29 章 评测体系与质量保证](29-evaluation-quality.md)

### 第六部分：商业与部署

- [第 30 章 推理经济学](30-inference-economics.md)
- [第 31 章 竞争格局与开源战略](31-competitive-landscape.md)
- [第 32 章 部署选型与 TCO](32-deployment-tco.md)

### 第七部分：工程化与产品化

- [第 33 章 工程化：从可运行到可维护](33-engineering-production.md)
- [第 34 章 模型接入、权重管理与版本治理](34-model-onboarding-versioning.md)
- [第 35 章 云原生部署与 Kubernetes](35-cloud-native-kubernetes.md)
- [第 36 章 推理产品化与商业路径](36-productization-business.md)
- [第 37 章 合规、许可与供应链治理](37-compliance-licensing-governance.md)

### 第八部分：专项负载与平台能力

- [第 38 章 多模态推理工程：从像素到 token](38-multimodal-inference.md)
- [第 39 章 推理模型与长思维链服务](39-reasoning-workloads.md)
- [第 40 章 离线批处理：吞吐型推理服务](40-offline-batch-serving.md)
- [第 41 章 多模型、多适配器与模型路由](41-multi-model-adapters.md)
- [第 42 章 Embedding、Rerank 与检索推理](42-embedding-rerank.md)
- [第 43 章 推理编译器与图优化](43-compiler-graph.md)

### 第九部分：训练侧推理、端侧与流量

- [第 44 章 强化学习推理与 rollout 引擎](44-rl-rollout.md)
- [第 45 章 端侧、边缘与异构推理](45-edge-heterogeneous.md)
- [第 46 章 流量工程、多区域与容量运营](46-traffic-engineering.md)

### 第十部分：商业分析与结语

- [第 47 章 商业分析进阶：现金流、定价与投资回报](47-business-finance.md)
- [第 48 章 商业分析：市场测算与进入策略](48-market-sizing.md)
- [第 49 章 商业分析：估值、融资与资本回报](49-valuation-financing.md)
- [第 50 章 未来方向](50-future-directions.md)
- [第 51 章 结语与路线图](51-conclusion.md)

### 附录

- [附录 A 术语表](appendix-a-glossary.md)
- [附录 B 公式与常数](appendix-b-formulas.md)
- [附录 C API 与配置参考](appendix-c-api-config.md)
- [附录 D 参考资料](appendix-d-references.md)
- [附录 E 实验手册与命令速查](appendix-e-lab-manual.md)
- [附录 F Transformer 推理基础](appendix-f-transformer-basics.md)
- [附录 G 一个请求的完整生命周期](appendix-g-request-lifecycle.md)
- [附录 H 练习提示与参考答案](appendix-h-exercise-answers.md)
- [附录 I 模块、API 与测试矩阵](appendix-i-module-api-index.md)
- [附录 J 生产故障排查手册](appendix-j-troubleshooting.md)

## 玩具模型说明

`mini_deepinfer/assets/tiny_lm.npz` 是一个 2 层、64 维、约 90K 参数的字节级 Transformer，在 `train_tiny.py` 生成的合成语料上训练了几千步。它能够续写教程中的短提示，但**不是**一个有用的语言模型，也不代表 DeepSeek-V4.1-Flash 的能力。它的价值在于：所有引擎机制都在真实的矩阵运算上运行，随机性、KV cache、调度顺序和数值误差都可以被测试。

`draft_lm.npz` 是一个 1 层 draft 模型，用于推测解码示例。`mini_deepinfer/model/v4lite.py` 另外提供了 MoE、Engram、CED 和 mHC 的机制实验室；它们使用随机初始化权重，只验证结构、形状、因果性和路由行为，不参与主生成 checkpoint。

注意：`ModelConfig()` 的默认值（3 层、FFN 192、窗口 32）是给新建模型用的；加载 checkpoint 时以 `config_json` 为准（2 层、FFN 128、窗口 64）。第 21 章和附录 B 会解释这个区别。

## 验证口径

本仓库的“验证”指下面三件事，而不是“看起来能跑”：

1. `python -m pytest -q`：268 个测试全部通过。
2. 28 个示例脚本全部退出码为 0，并且示例中的断言/打印与文档描述一致。
3. `python check_docs.py`：章节文件、README 链接、代码路径、测试数量、示例数量、禁用块引用等检查全部通过。

时间类数字（吞吐、TTFT、TPOT）会随机器负载变化，文档只把它们当作量级示意；确定性的数字（token 数、缓存命中块数、测试数量、示例数量、配置上限）必须和脚本一致。

## 怎么用这本书

建议每章按“读正文 -> 跑对应示例 -> 做练习 -> 记一条笔记”的节奏。技术章节的重点是“为什么这样设计”，实现章节的重点是“代码如何保证不变量”，商业章节的重点是“假设变了，结论怎么变”。如果想先建立一条端到端的基准线，先跑 `examples/lifecycle.py` 并读附录 G；它用 63 个 token 的一次生成，把 tokenizer、调度、分页 KV、前缀缓存、采样和状态回收连成一条可以逐行对照的 trace。如果你在做平台，第 41 到 43 章分别给出多模型/适配器、检索推理和图编译三条扩展路线；它们都附带可运行的 mini 模块和独立测试。附录 E 给出了按实验组织的学习路径，附录 H 给出练习提示和计算题答案，附录 I 是模块/API/测试矩阵，附录 J 是按症状组织的故障排查手册。

如果你要引用书中的数字做决策，先确认三件事：数字是“官方/公开资料”还是“示意”，测量口径是什么，版本是什么。推理性能对模型、精度、batch、上下文长度、互联和软件版本高度敏感，任何脱离上下文的单点数字都不应直接采信。

## 关于公开数字

书中引用的 DeepInfer 生产数字主要来自官方 README、部署 Q&A 和 2026 年 10 月的公开报道，包括公开口径的单个服务集群每天 155 万亿 token（可能跨多个数据中心）、平均 TTFT 约 400 毫秒（P99 约 1 秒）、decode 约 230 token/s（编码场景可达 300）、输入缓存命中率 96%、prefill 命中缓存后每卡吞吐从 6 万提升到 150 万 token/s、3FS KV 存储约 100 PiB、TTL 3 天/7 天。这些是厂商自报数字，未经第三方独立验证；书中用它们说明量级和设计取舍，不作为性能承诺。

## 许可与引用

本仓库的迷你实现使用 MIT License；教程正文使用 CC BY 4.0。DeepInfer 本身使用 Apache License 2.0。引用 DeepInfer 时请使用其官方仓库给出的 BibTeX 条目。

```bibtex
@misc{deepinfer2026,
  title = {DeepInfer: High-performance, large-scale inference framework for DeepSeek models},
  author = {DeepSeek-AI},
  year = {2026},
  publisher = {GitHub},
  howpublished = {\url{https://github.com/deepseek-ai/DeepInfer}},
}
```

<!-- docs-meta: tests=268 examples=28 chapters=52 appendices=10 -->
