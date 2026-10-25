# 附录 D 参考资料

本附录列出书中引用的主要公开资料，并给出“先读什么、为什么读”的建议。链接可能随项目更新而变化；如果打不开，优先在官方仓库和论文页面搜索标题。标注“社区解读”的资料用于补充规格和背景，不能替代官方代码和论文。

## D.1 DeepInfer 官方

**GitHub 仓库**：`https://github.com/deepseek-ai/DeepInfer`

README 包含支持模型、特性列表、硬件、构建步骤、离线推理、benchmark 和引用。书中的特性名、环境变量、硬件要求和“未包含的组件”主要来自这里。建议先读 README 的 feature details 和 quick start，再读 `doc/`。

**部署 Q&A**：`https://github.com/deepseek-ai/DeepInfer/blob/main/doc/inference_deployment_qa.md`

包含 E/P/D 架构、集群与存储、EPLB、DP 负载均衡、KV cache 存储、自动扩缩容、故障切换、低精度、kernel、推测解码、异步调度、结构化输出和 DeepSeek Recipe 的详细说明。书中的生产数字（155T token/天、38 美元/GPU 小时、100 PiB、TTL 3/7 天、1.4 MB SWA、64+64 GPU 等）主要来自这份文档。它比 README 更接近“设计文档”，适合在读完第 2、14、15 章后精读。

**示例**：`https://github.com/deepseek-ai/DeepInfer/tree/main/examples`

包含 `offline_inference.py`、`benchmark.py`、`_structured_output.py`、`sweep/` 和 `yoco/`。建议对照第 3 章的命令读，尤其注意 `sweep/` 的扫点和 `yoco/` 的截断参数。

**评测**：`https://github.com/deepseek-ai/DeepInfer/tree/main/eval`

包含 AIME、GPQA、HLE 的评测脚本和 judge。对照第 29 章读，注意 judge 的 prompt、pass@k 的实现和数据加载方式。

**DeepSeek Recipe**：`https://github.com/deepseek-ai/deepseek-recipe`

负责多协议 API（Chat Completions、Responses、Messages）与模型 token 序列之间的编解码，包含特殊 token 防注入、分段并行 tokenization、流式解析。对照第 9、25、28 章读。

## D.2 模型与技术报告

**DeepSeek-V4.1-Flash 模型页**：`https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash`

**技术报告**：`DeepSeek_V41_Tech_Report.pdf`（Hugging Face 模型仓库内）

公开资料中的 552B backbone、196B Engram、40 层 CED、384 routed experts/top-6、1M 上下文、890 字节/token、45T 训练 token 等规格来自技术报告和社区解读。不同来源的个别数字可能有差异，书中已用“公开资料显示”标注。读技术报告时，先看模型结构图和训练目标，再看推理相关的细节（KV 格式、稀疏索引、SWA）。

**DeepSeek-V4.1-Flash-1017 权重**：`https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017`

DeepInfer 默认 checkpoint。下载前确认存储和带宽；权重很大，不要在没有足够磁盘的机器上直接拉。

## D.3 相关论文与项目

**vLLM / PagedAttention**：`https://github.com/vllm-project/vllm`

PagedAttention 论文：Kwon et al., "Efficient Memory Management for Large Language Model Serving with PagedAttention", SOSP 2023。它是第 14、22 章的基础；读论文时重点看“逻辑块 vs 物理块”和“COW”。

**SGLang / RadixAttention**：`https://github.com/sgl-project/sglang`

RadixAttention 用基数树管理前缀缓存，和 DeepInfer 的链式哈希不同但目标相同。对照第 13、14 章读。

**TensorRT-LLM**：`https://github.com/NVIDIA/TensorRT-LLM`

kernel 融合、CUDA Graph、FP8/FP4 的工程参考。对照第 17、18 章读。

**FlashMLA**：`https://github.com/deepseek-ai/FlashMLA`

MLA attention kernel。对照第 6.8 节和第 18.4 节读，重点看 split-K 和 online softmax。

**DeepGEMM**：`https://github.com/deepseek-ai/DeepGEMM`

FP8/FP4 GEMM。对照第 10、18 章读，重点看 scale 布局和 TMA。

**DeepEP**：`https://github.com/deepseek-ai/DeepEP`

MoE all-to-all。对照第 7、15 章读，重点看 dispatch/combine 的 buffer 和低延迟模式。

**TileLang**：`https://github.com/tile-ai/tilelang`

Tile DSL。对照第 18.8 节读，重点看它如何表达 tile 流水线和融合。

**DeepSelect**：`https://github.com/deepseek-ai/DeepSelect`

稀疏选择/索引相关的 kernel，和 CSA2 的 indexer 对照读。

**3FS**：`https://github.com/deepseek-ai/3FS`

分布式文件系统。对照第 14.5 节读，重点看 EC、元数据、GC。

**xgrammar**：`https://github.com/mlc-ai/xgrammar`

文法编译/匹配。对照第 9、23 章读，重点看 bitmask 和 rollback。

**TGI**：`https://github.com/huggingface/text-generation-inference`

HF 生态的服务框架，适合看“引擎 + 服务层”的一体化设计。

**LMDeploy**：`https://github.com/InternLM/lmdeploy`

TurboMind 引擎、量化、多模态，国内社区常用。

**llama.cpp**：`https://github.com/ggml-org/llama.cpp`

CPU/边缘推理和 GGUF 量化，适合看“小设备上的推理工程”。

## D.4 背景阅读

- Roofline 模型：Williams, Waterman, Patterson, "Roofline: An Insightful Visual Performance Model for Multicore Architectures", CACM 2009。
- Continuous batching：Orca 相关论文与 vLLM 文档。
- Speculative decoding：Leviathan et al., "Fast Inference from Transformers via Speculative Decoding", ICML 2023；Chen et al., "Accelerating Large Language Model Decoding with Speculative Sampling", 2023。
- Medusa：Cai et al., "Medusa: Simple LLM Inference Acceleration Framework with Multiple Decoding Heads", 2024。
- EAGLE：Li et al., "EAGLE: Speculative Sampling Requires Rethinking Feature Uncertainty", 2024。
- MoE：Shazeer et al., "Outrageously Large Neural Networks: The Sparsely-Gated Mixture-of-Experts Layer", ICLR 2017；Switch Transformer、GShard 等。
- FlashAttention：Dao et al., "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness", 2022。
- Erasure coding / 3FS：DeepSeek 3FS 文档与论文。
- 推理经济学：云厂商 GPU 定价、AI 推理 API 定价、公开的 GPU 租赁价格。

## D.5 阅读顺序建议

如果你是初学者，按下面的顺序读资料：

1. 本书第 1 到 4 章，建立坐标系；
2. vLLM 的 PagedAttention 论文，理解分页和连续批处理；
3. 本书第 12 到 15 章，理解引擎、调度、KV、并行；
4. DeepInfer 的 README 和部署 Q&A，理解生产系统；
5. 本书第 18 章和 FlashMLA/DeepGEMM 的 README，理解 kernel；
6. 本书第 20 到 27 章，动手实现；
7. 本书第 28 到 32 章，理解安全、评测、成本和部署。

不要一开始就读技术报告的全部细节。先建立“prefill/decode、KV、调度、并行”的骨架，再把模型结构挂上去；否则很容易迷失在缩写里。

## D.6 引用说明

书中的“官方”指 DeepInfer README 或部署 Q&A；“公开资料”指 DeepSeek 模型页、技术报告或可信社区解读；“示意”指本书为了说明量级而构造的数字，不是实测承诺。

如果你要把书中的数字用于生产决策，请回到原始资料核对版本，并在自己的硬件和流量上复测。推理性能对模型、精度、batch、上下文长度、互联和软件版本高度敏感，任何脱离上下文的单点数字都不应直接采信。

## D.7 工程化、部署与商业参考

**Kubernetes 文档**：`https://kubernetes.io/docs/`

重点读设备插件、探针、HPA、PDB、NetworkPolicy、拓扑分布和生命周期钩子。第 35 章的清单基本对应这些文档。

**OpenTelemetry**：`https://opentelemetry.io/docs/`

重点读 trace context、span、collector、采样和语义约定。第 33 章和第 35 章的可观测性部分对应这些概念。

**SPDX / CycloneDX**：`https://spdx.dev/`、`https://cyclonedx.org/`

SBOM 的两种常见格式。第 37 章的供应链部分会用到。

**SLSA**：`https://slsa.dev/`

供应链完整性等级和来源证明。适合用来评估构建流水线的可信度。

**cosign / Sigstore**：`https://www.sigstore.dev/`

镜像和制品的签名与验证。Kubernetes admission controller 可以拒绝未签名镜像。

**NIST AI Risk Management Framework**：`https://www.nist.gov/itl/ai-risk-management-framework`

AI 风险管理的通用框架，适合把模型治理、评测、事件响应组织成一套流程。

**ISO/IEC 42001**：AI 管理体系标准。适合需要向客户或监管证明“有 AI 治理流程”的组织。

**DeepInfer 公开报道**：2026 年 10 月的 TNW、SiliconANGLE、MarkTechPost、AI Business 等报道，以及中文科技媒体对 155 万亿 token/天、400 ms TTFT、230 token/s、96% 缓存命中、3FS 100 PiB 的转述。这些报道可以帮助理解官方数字的背景，但都是二手来源，引用前应回到官方 README/Q&A 核对。

**推理 API 定价**：主流云厂商和模型厂商的公开定价页。比较时注意输入/输出单价、缓存折扣、批处理折扣、上下文分层和区域差异。

## D.8 检索日期

本书主要资料检索于 2026 年 10 月 22 日。DeepInfer 是一个刚开源的活跃项目，README、代码和环境变量可能在此之后变化。阅读时以仓库当前版本为准，并把版本号写进你的实验记录。

## D.9 多模型、检索与编译参考

**LoRA**：Hu et al., "LoRA: Low-Rank Adaptation of Large Language Models", ICLR 2022。第 41 章的数学来源；重点看低秩分解、`alpha/r` 缩放和合并推理。

**S-LoRA**：Sheng et al., "S-LoRA: Serving Thousands of Concurrent LoRA Adapters", MLSys 2024。重点看统一分页、按行批处理和适配器换入换出；对照第 41.3 节读。

**Punica**：Chen et al., "Punica: Multi-Tenant LoRA Serving", 2023。重点看如何把多个适配器的低秩 GEMM 拼成批量计算。

**Faiss**：`https://github.com/facebookresearch/faiss`。Flat、IVF、PQ 的参考实现；对照第 42.4 节读，重点看索引参数和召回率曲线。

**HNSW**：Malkov and Yashunin, "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs", 2016。图索引的代表；注意内存、插入删除和召回率。

**MTEB**：`https://github.com/embeddings-benchmark/mteb`。Embedding 模型的通用评测集；对照第 42.7 节读，注意它不能替代业务数据评测。

**Sentence Transformers**：`https://www.sbert.net/`。Bi-encoder、cross-encoder 和 pooling 的工程参考。

**TorchInductor / Torch.compile**：`https://pytorch.org/docs/stable/torch.compiler.html`。图捕获、融合、Triton 代码生成的参考；对照第 43 章读。

**MLIR**：`https://mlir.llvm.org/`。多层 IR 和 pass 基础设施；适合理解“高层图到低层硬件”的 lowering。

**TVM**：`https://tvm.apache.org/`。张量编译和自动调优的参考；重点看 schedule、autotune 和 target。

**TensorRT**：`https://developer.nvidia.com/tensorrt`。层融合、精度校准、engine 序列化；对照第 43.10 节读。

**Triton**：`https://triton-lang.org/`。Tile 级 kernel DSL；对照第 18.8 节和第 43.10 节读。

**SGLang / RadixAttention**、**vLLM / PagedAttention**：前文已列；如果要做多模型路由，重点看它们的调度器和前缀缓存如何与模型选择解耦。

## D.10 训练侧、端侧、流量与财务参考

**GRPO**：Shao et al., "DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models", 2024。第 44 章的组相对优势来源；重点看组采样、奖励归一化和无需价值模型的动机。

**DeepSeek-R1**：DeepSeek-AI, "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning", 2025。理解推理模型、长思维链和 RL 后训练的工程背景；对照第 39、44 章读。

**RLHF / PPO**：Ouyang et al., "Training language models to follow instructions with human feedback", 2022；Schulman et al., "Proximal Policy Optimization Algorithms", 2017。理解重要性采样比、KL 约束和 PPO 的 clip；第 44 章的 logprob 需求来自这里。

**vLLM / SGLang 的 rollout 支持**：两项目都有 RL 后训练相关的分支或文档；对照第 44 章看它们如何处理权重同步、部分 rollout 和 logprob。

**llama.cpp / GGUF**：`https://github.com/ggml-org/llama.cpp`。端侧 CPU/Metal 推理、k-quants、mmap 和 context shifting 的参考；对照第 45 章读。

**MLC-LLM**：`https://github.com/mlc-ai/mlc-llm`。TVM 驱动的跨平台部署，适合看“一份模型多后端”的编译路径。

**ONNX Runtime**：`https://onnxruntime.ai/`。端侧/边缘的算子覆盖、量化工具和执行提供者；对照第 45 章的运行时选型。

**ExecuTorch**：`https://pytorch.org/executorch/`。PyTorch 模型导出到手机/边缘的官方路径；重点看算子覆盖和动态 shape。

**WebGPU**：`https://www.w3.org/TR/webgpu/`。浏览器推理的 shader 和内存模型；适合演示和隐私敏感场景，但性能和兼容性受限。

**排队论**：Kleinrock, "Queueing Systems, Volume 1: Theory", 1975；Gross and Harris, "Fundamentals of Queueing Theory"。第 46 章的 M/M/1、M/M/c 和 Erlang C 的标准来源。

**SRE / 容量规划**：Google SRE Book 的 "Handling Overload"、"Addressing Cascading Failures" 和 "Managing Load" 章节；对照第 46 章的重试预算、熔断、负载削减读。

**Kubernetes HPA/KEDA**：`https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/`、`https://keda.sh/`。第 35、46 章的扩缩容和队列指标参考。

**公司金融**：Brealey, Myers, Allen, "Principles of Corporate Finance"；Damodaran, "Investment Valuation"。第 47 章的 NPV、IRR、资本成本和情景分析的标准教材。

**SaaS 指标**：Bessemer Venture Partners 的 cloud metrics 文章、OpenView 的 SaaS benchmarks。第 47 章的 LTV/CAC、回收期和 rule of forty 的行业口径；注意不同来源的定义差异。

**推理 API 定价**：主流云厂商和模型厂商的公开定价页。比较时注意输入/输出单价、缓存折扣、批处理折扣、上下文分层、承诺用量折扣和区域差异。

## D.11 市场、GTM 与估值参考

**市场测算**：TAM/SAM/SOM 的自下而上方法在大多数 SaaS/基础设施创业教材里都有；重点是客户数、每客户用量、单价和渗透率四个变量，不要直接引用行业报告的总量。第 48 章给出了可执行公式。

**JTBD**：Clayton Christensen 的 "Jobs to Be Done" 系列，以及后续的实践文章；用来判断客户买的是“token”还是“任务结果”。

**GTM 与销售产能**：Bessemer、OpenView、saas-capital 等机构的公开文章；关注 PLG 与销售驱动的分界、CAC 回收期、magic number、销售 ramp 和配额。口径差异很大，引用时要写清定义。

**SaaS/基础设施估值**：Damodaran 的估值课程和年度数据、公开的 SaaS 倍数报告、云厂商和基础设施公司的财报；第 49 章的 DCF、EV/Revenue、Rule of 40 和 MOIC 都来自这些标准方法。倍数随利率、增长和毛利变化，不能把某一年的倍数当成常数。

**并购与尽调**：公开的并购交易公告、S-1/F-1 文件、尽调清单；重点看收入质量、客户集中度、capex、营运资金和调整后 EBITDA 的口径。

**合规与市场进入**：目标市场的监管机构官网、数据保护指南、AI 法案文本；第 37 章的合规时间表要以官方文本为准，不要引用二手解读的日期。
