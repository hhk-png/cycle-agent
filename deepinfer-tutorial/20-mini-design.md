# 第 20 章 mini-deepinfer 的设计与边界

本章导读：在写代码之前，先明确 mini-deepinfer 要证明什么、不证明什么。本章给出设计目标、模块划分、保真度矩阵和关键取舍。读完本章，你应该能判断某个 DeepInfer 机制在 mini 里对应哪段代码、差距在哪里，以及为什么这些差距是规模问题还是能力问题。

## 20.1 设计目标

mini-deepinfer 有四个目标。

**第一，机制完整。** DeepInfer 的核心机制——分页 KV、连续批处理、chunked prefill、抢占、前缀缓存、推测解码、结构化输出、EPLB、CP/EP/PD、异步调度、KV store、roofline 估算——都要有可运行的对应物。不是每个都做到生产级，但每个都要能跑、能测、能观察。这里要区分“独立可运行”和“接在主引擎热路径上”：异步调度在 `pipeline.py` 里是一个可运行的成本模型，KV store 在 `cache_engine.py` 里是独立的 `KVBlockStore`，二者都没有接进 `LLMEngine.step`；`EngineConfig` 里的 `enable_async_schedule`、`enable_kv_store` 等开关因此只是兼容命名或预留位，不会改变引擎行为（附录 C 会逐项标注）。

**第二，依赖最小。** 只依赖 Python 标准库、NumPy 和 pytest。没有 PyTorch、没有 CUDA、没有编译步骤。这样读者可以在任何一台笔记本上复现，不会被环境问题挡在门外。

**第三，数字可验证。** 测试要覆盖确定性、批不变性、分块一致性、前缀缓存一致性、推测贪心一致性、结构化输出合法性、EPLB 改善、CP/EP 数值等价、roofline 公式。文档里的确定性数字要和脚本对得上。

**第四，边界诚实。** 每个模块的文档和章节都要说明它不是什么。mini 不是生产引擎，不能替代 DeepInfer，不追求 GPU 性能。把差距写清楚，比假装“完整实现”更有价值。

这四个目标有优先级：正确性 > 可读性 > 覆盖度 > 性能。如果为了性能牺牲可读性，就不写；如果为了覆盖度牺牲正确性，就不做。这个排序决定了后面所有的取舍。

## 20.2 非目标

mini 明确不做以下事情：

- 不支持 552B 模型或 1M 上下文；
- 不实现 FlashMLA、DeepGEMM、DeepEP、TileLang 等真实 kernel；
- 不做多进程 NCCL/HCCL 通信；
- 不做 CUDA Graph 捕获和 GPU profiling；
- 不做 3FS、GDR、跨机 KV 传输；
- 不做生产级鉴权、限流、多租户、故障转移；
- 不把异步调度、KV store、并行组接进主引擎的热路径（它们只有独立模块或成本模型）；
- 不训练一个有用的语言模型。

这些不是“以后补上”的待办，而是规模、硬件和工程投入的边界。书中会把它们放在第 27 章的“从 toy 到生产”清单里，说明补齐需要什么。

非目标里最容易被误解的是“不训练一个有用的语言模型”。mini 的权重是训练过的，能续写短提示，但它的语言能力很弱。它的作用是“让引擎机制在真实矩阵上运行”，不是“提供一个可用的聊天模型”。如果你用它的输出质量来评价引擎，会得出错误的结论。

## 20.3 模块划分

```text
mini_deepinfer/
├── config.py             EngineConfig / ModelConfig / ParallelConfig
├── sampling_params.py    温度、top-k/p、seed、stop、so_args
├── sequence.py           Sequence / RequestOutput / 状态机
├── tokenizer.py          字节级 tokenizer、Chat 模板、流式解码
├── block_manager.py      分页、哈希、引用计数、LRU
├── cache_engine.py       PagedKVCache、KVBlockStore
├── scheduler.py          prefill/decode、chunk、LCFS、抢占
├── engine.py             step、采样、输出、指标
├── llm.py                批量生成、流式回调
├── sampling.py           分布、top-k/p、采样
├── speculative.py        draft/verify/confidence/变长验证
├── structured_output.py  NFA、regex/JSON/EBNF、mask、回滚
├── eplb.py               冗余专家、贪心放置
├── parallel.py           CP/EP/TP/PD、负载均衡器
├── errors.py             异常类型与错误语义
├── pipeline.py           ZOS 成本模型、形状缓存、性能曲线
├── quantization.py       FP8/FP4 量化、误差、KV 字节
├── sparse_attention.py   块稀疏注意力与 indexer 选择
├── yoco.py               SWA 命中判定与有界重放
├── roofline.py           FLOPs/字节/加速比/单位成本模型
├── economics.py          TCO、毛利、容量规划、敏感性
├── manifest.py           权重/tokenizer/模板/引擎版本绑定
├── gateway.py            鉴权、配额、前缀亲和、drain
├── tracing.py            请求级 span 与延迟归因
├── vision.py             迷你 ViT、patch -> token、aligner
├── multimodal.py         图像 token 预算、embedding 缓存、VE 批处理、EPD 比例
├── reasoning.py          思考预算、双通道流解析、推理容量与成本模型
├── offline_batch.py      离线 wave 规划、执行与报告
├── lora.py               LoRA、按行适配器批处理、LRU 驻留
├── router.py             模型路由、级联与盈亏接受率
├── embedding.py          双塔编码、分桶、向量索引、rerank
├── compiler.py           图 IR、折叠、融合、内存规划
├── rl_rollout.py         组采样、GRPO、权重版本、部分 rollout
├── edge.py               端侧内存、roofline、能耗、云边路由
├── traffic.py            排队、准入、路由、熔断、多区域
├── finance.py            NPV、IRR、LTV/CAC、合同、DCF、倍数与情景分析
├── market.py             TAM/SAM/SOM、采用曲线、漏斗、销售产能
├── recipe.py             多协议消息归一化、流式事件解析
├── server.py             HTTP/SSE
├── metrics.py            指标与 Prometheus 文本
├── cli.py                命令行
├── corpus.py             合成语料
└── model/
    ├── ops.py            RMSNorm、RoPE、attention、softmax
    ├── tiny_transformer.py  2 层 toy 模型、paged forward
    ├── training.py       手写反向传播、Adam
    └── v4lite.py         MoE/Engram/CED/mHC 机制实验室
```

依赖方向是单向的：`engine` 依赖 `scheduler`、`cache_engine`、`model`、`sampling`；`scheduler` 依赖 `block_manager`、`cache_engine`；`model` 的数值核心只依赖 `ops` 和 `config`，接口层还会用到 `cache_engine` 和 `sequence`。没有循环依赖，方便单独测试。

模块和章节的对应关系是：`sampling.py`、`sampling_params.py`、`sequence.py` 见第 11 章；`tokenizer.py`、`model/` 见第 21 章；`block_manager.py`、`cache_engine.py`、`scheduler.py` 见第 22 章；`speculative.py`、`structured_output.py` 见第 23 章；`eplb.py`、`parallel.py`、`yoco.py` 见第 24 章；`server.py`、`metrics.py` 见第 25 章；`manifest.py`、`gateway.py`、`tracing.py` 的工程化用法见第 33、34、35 章；`roofline.py`、`economics.py` 见第 4、30 章；`recipe.py`、`vision.py`、`multimodal.py` 见第 9、16、38 章；`reasoning.py` 见第 39 章；`offline_batch.py` 见第 40 章；`lora.py`、`router.py` 见第 41 章；`embedding.py` 见第 42 章；`compiler.py` 见第 43 章；`rl_rollout.py` 见第 44 章；`edge.py` 见第 45 章；`traffic.py` 见第 46 章；`finance.py` 见第 47、49 章；`market.py` 见第 48 章。不要把“模块树里有”误当成“这一部分会逐行讲”。

`roofline.py` 是后来加的“性能模型模块”，它不依赖引擎，只依赖数学。这样你可以在不加载模型的情况下算 FLOPs、KV 字节、推测加速比和单位成本。它对应第 4 章和第 18 章的公式，是“用代码验证公式”的入口。

`economics.py`、`manifest.py`、`gateway.py`、`tracing.py` 是工程化模块，也不依赖引擎的热路径。它们分别对应第 30 章的单位经济、第 34 章的版本治理、第 25 章的服务与网关边界、第 33 章的可观测性。把它们放在核心引擎之外，是为了保持“引擎只负责生成 token”的边界；服务层的问题在服务层解决，不要让引擎承担租户、配额和部署的复杂度。

## 20.4 保真度矩阵

下面逐项说明“DeepInfer 有什么、mini 有什么、差距是什么”。这不是打分，而是让读者知道每个机制的验证强度。

**分页 KV**：DeepInfer 用 C++ block manager + GPU paged cache + 128 token block + 128 字节对齐；mini 用 Python block manager + NumPy 数组 + 16 token block。逻辑（哈希、引用计数、COW、LRU）一致；差距是 kernel 间接寻址、对齐、显存容量和性能。

**前缀缓存**：DeepInfer 有 3FS 持久化、TTL、跨机目录、GDR swap-in；mini 有内容哈希、block 共享、LRU、本地 `KVBlockStore`。mini 证明了命中/未命中的正确性，不证明跨机延迟和容量。

**连续批处理**：DeepInfer 用 C++ scheduler + DP 投票 + LCFS + 抢占；mini 用 Python scheduler + 单 rank + 相似策略。mini 证明了 prefill/decode 切换、chunk、抢占、重算；没有 DP 投票和 MTP varlen。

**推测解码**：DeepInfer 用 DSpark + Confidence Head + GPU varlen kernel + CUDA Graph；mini 用 1 层 draft + draft 概率代理 confidence + Python 变长验证。mini 证明了接受/拒绝、贪心一致性、逻辑长度回退（拒绝位置的 K/V 不物理擦除，由后续写入覆盖），以及“推测解码 + 结构化输出”在贪心下的 grammar 掩码与回退组合；没有训练好的 confidence head 和真实 GPU 加速。

**结构化输出**：DeepInfer 用 xgrammar + 完整 JSON Schema + EBNF + tokenizer bitmask + 回滚；mini 用自写 NFA + JSON 子集 + bounded EBNF + 字节级 mask。mini 证明了编译、mask、trigger/bound、回滚；语言子集和 tokenizer 复杂度不同。

**MoE/EPLB**：DeepInfer 有 384 专家、top-6、DeepEP all-to-all、Mega MoE、权重 shuffle；mini 有 8 专家、top-2、逻辑 all-to-all、贪心 EPLB。mini 证明了路由、共享专家、副本分配、不均衡改善；没有真实通信和 kernel 融合。

**并行**：DeepInfer 有 DP/EP/CP/TP、Wide-EP、PD/EPD、负载均衡器；mini 有 CP/EP/TP 的逻辑分片、PD KV 交接、分桶/双路径启发式。mini 证明了分片、all-gather/all-reduce、dispatch/combine 和交接的数值等价；没有 NCCL/HCCL、跨机、自动扩缩容。

**多模态**：DeepInfer 有 DeepSeek-ViT、2D-RoPE、3×3 pixel-unshuffle、EPD 分离和跨机 embedding 传输；mini 有迷你 ViT、图像 token/KV 预算、内容寻址 embedding 缓存、分辨率分桶批处理和 EPD 比例模型。mini 证明了“像素到 token”的成本链和缓存/批处理策略；没有真实视觉塔、图像预处理和跨机传输。

**推理负载**：DeepInfer 的推理模型有思考 token、reasoning 通道和思考预算；mini 有模型无关的 `ReasoningBudget`、双通道流解析器和容量/成本乘数模型。mini 证明了预算记账、标记跨 chunk 切分和成本量级；没有绑定具体模型的强制结束思考逻辑，也没有把预算接进采样循环。

**离线批处理**：DeepInfer 的开源入口是离线推理，生产侧有分片、重试和调度；mini 有 `BatchItem`、wave 规划、执行和吞吐/截止时间报告。mini 证明了批次组成、优先级排序和报告口径；没有检查点、背填、跨进程恢复和结果清单持久化。

**异步/CUDA Graph**：DeepInfer 有 ZOS、CUDA Graph、decode perf cache、profiler；mini 有成本模型、形状缓存、性能曲线。mini 证明了重叠的收益模型；没有真实 GPU 流水线。

**低精度/确定性**：DeepInfer 有 FP8/FP4、batch-invariant kernel、deterministic sampling；mini 的主生成路径是 float32，另有 `quantization.py` 的 FP8/FP4 参考量化和 `sparse_attention.py` 的块稀疏实验，用 per-seq RNG 和确定性测试验证“批不变”和“固定 seed”的逻辑。量化没有接入主生成路径。

**模型**：DeepInfer 是 552B + 196B 的 V4.1-Flash；mini 是 90K 参数的 2 层 toy。mini 的 `v4lite.py` 用随机权重演示 MoE/Engram/CED/mHC 的结构，`vision.py` 演示 patch -> token，`recipe.py` 演示多协议消息与流式事件；它们都不训练、不追求语言质量，也没有接入主生成路径。

**性能模型**：DeepInfer 用实测的 decode perf cache、profiler、roofline 分析；mini 用 `roofline.py` 的解析公式和 `pipeline.py` 的成本模型。mini 能算“应该快多少”，不能测“实际快多少”。

**SWA/YoCo**：DeepInfer 有 block KV 与 SWA state 的分离、命中判定、140 token 有界重放和跨机 state 传输；mini 有 `yoco.py` 的命中判定与重放成本模型，以及 `tiny_transformer.py` 的 SWA 层。mini 证明了“命中不等于可直接 decode”和“重放长度有下界”，没有实现真实的重放 kernel、跨机 state 缓存和 decoder 的 compact 布局。

**多适配器与路由**：DeepInfer 只服务一个模型，本身不提供 LoRA 或多模型路由；mini 的 `lora.py` 和 `router.py` 验证低秩更新、按行分组、rank padding、LRU 驻留和约束路由的数学与状态，不验证真实 kernel、适配器换入延迟或线上质量校准。

**检索推理**：DeepInfer 不覆盖 embedding/rerank；mini 的 `embedding.py` 验证归一化、长度分桶、flat/桶索引和 retrieve-then-rerank 的接口，不验证语义质量、HNSW/PQ 或分布式索引。

**图编译**：DeepInfer 有手写融合 kernel、TileLang 和 CUDA Graph，但没有通用图编译器；mini 的 `compiler.py` 验证 SSA 图、常量折叠、DCE、两种融合模式和活跃性内存规划，不验证 layout 传播、量化 pass、autotune 或代码生成。

**工程化**：DeepInfer 有 C++ 构建、wheel、容器、CI、manifest、网关、Prometheus/OpenTelemetry；mini 有 `pyproject.toml`、`manifest.py`、`gateway.py`、`tracing.py`、`check_docs.py` 和 `deploy/` 的最小版本。mini 验证的是“这些控制点的语义和接口”，不是“生产级的并发、持久化和安全”。

保真度矩阵的用法是“读到一个机制时，先找到它的验证强度”。如果一个机制是“逻辑一致、性能留白”，你可以相信 mini 的测试，但不能相信它的时间数字；如果一个机制是“只做结构演示”，你只能相信形状和因果性，不能相信数值质量。

## 20.5 为什么用 NumPy

NumPy 的矩阵乘法在 CPU 上足够快，能跑几十个 token 的生成；它的 API 简单，读者不需要理解 GPU 内存层次；它没有自动微分，迫使训练代码手写反向传播，这对理解 Transformer 的计算和显存很有帮助。

代价是没有 GPU kernel、没有低精度、没有并行。mini 用逻辑模拟和成本模型来弥补，而不是假装有 GPU。

NumPy 还有一个好处是可复现。同一个 seed、同一台机器，结果逐位一致；没有 CUDA 的非确定性、没有 cuBLAS 的算法选择、没有多 stream 竞争。这让测试可以断言“逐 token 相等”，而不是“容差内相等”。第 10 章的确定性讨论在 mini 上几乎是免费的。

## 20.6 为什么保留玩具模型

如果 mini 只用一个“返回固定 token”的假模型，KV cache、attention、采样和推测解码都没有真实的数值行为，测试会变成“测一个 mock”。玩具模型虽然小，但它是真正的 Transformer：有 RoPE、GQA、RMSNorm、SwiGLU、causal mask、softmax。所有引擎机制都在真实矩阵上运行，数值误差、KV 读写、批不变性都是可观察的。

玩具模型的训练语料是合成的，输出只对教程里的短提示勉强可读。这不是缺陷，而是边界：它证明引擎能驱动一个真实模型，不证明模型有用。

玩具模型还有一个“可解释”的优点：2 层、64 维、4 个 query 头、2 个 KV 头，任何一个矩阵的形状都能手算；出了问题可以用 `forward_full` 和 `forward_paged` 对比，快速定位是模型、KV 还是调度。生产模型没有这个便利。

## 20.7 代码风格

mini 的代码优先可读性：

- 类型注解齐全，但不过度使用泛型；
- 每个模块顶部有一段 docstring，说明它对应 DeepInfer 的哪个部分；
- 复杂逻辑（COW、抢占、回滚、EPLB）用注释解释“为什么”，而不是“是什么”；
- 错误用明确的异常类型，不静默吞掉；
- 测试命名描述行为，不描述实现。

这不意味着 mini 是“伪代码”。它没有 `pass` 占位，所有示例都能跑，所有测试都断言真实结果。可读性和可运行性并不矛盾。

一个具体的风格例子是 `_preempt`：它只有几行，但注释解释了“不要重新 pin 前缀”的原因。这个注释比代码本身更重要，因为它是从真实 bug 里总结出来的。读 mini 时，先读这些注释，再看实现。

## 20.8 如何扩展

如果你想在 mini 上做实验，建议从这些方向开始：

- 把 block size 从 16 改成 128，观察前缀命中和 COW 的变化；
- 给 scheduler 加一个优先级队列，比较 LCFS 和 FIFO 的尾延迟；
- 给 model 加 MoE 训练（当前 `v4lite` 只做推理），观察路由不均衡；
- 给 `KVBlockStore` 加一个后台 GC 线程，模拟 3FS 的 TTL；
- 给 server 加 `/admin/drain`，练习 graceful shutdown；
- 给 pipeline 加一个真实的线程池，测量 Python 下的重叠效果；
- 给 `roofline.py` 加一个从 trace 读取实测时间的入口，把“模型估算”和“实测”对比。

第 26 章会给出测试和实验的方法；第 27 章会给出从 toy 到生产的路线。如果你想把 20 到 27 章串成一条可执行的线，按下面的节奏跑：读完 21 到 22 章运行 `examples/lifecycle.py`，对照附录 G 的 trace 看 token、block 和缓存命中；读完 23 章运行 `examples/speculative_decoding.py` 和 `examples/structured_output.py`；读完 24 章运行 `examples/parallelism.py`、`examples/eplb.py` 和 `examples/kv_store.py`；读完 25 章运行 `examples/http_server.py`。每一步都先预测输出，再运行验证，预测错了就回到对应章节找原因。

## 20.9 依赖图与测试映射

20.3 节的模块树回答“有什么文件”，但没有回答“谁依赖谁”。把依赖画出来，才能判断一个模块能不能单独测试，以及改一处会不会触发循环导入。mini 的主依赖链是：

```text
config, errors
    |
    v
model/ops -> model/tiny_transformer -> engine
    ^                                  ^
    |                                  |
sequence, sampling_params -> sampling  |
    |                                  |
block_manager -> cache_engine -> scheduler
    |
    v
llm -> server/cli
```

箭头表示“被依赖”。`model/ops` 只依赖 NumPy，`tiny_transformer` 依赖 `ops`、`config`、`cache_engine` 和 `sequence`；`scheduler` 依赖 `block_manager`、`cache_engine`、`sequence` 和 `config`；`engine` 把 scheduler、model、sampling 和 tokenizer 串起来；`llm`、`server`、`cli` 是最外层。外围模块（`manifest`、`gateway`、`tracing`、`economics`、`market`、`finance`、`traffic` 等）不依赖引擎热路径，只依赖标准库和 NumPy，所以可以单独导入、单独测试。

`mini_deepinfer/__init__.py` 用 `__getattr__` 做惰性重导出，是为了避免“导入包就触发整条依赖链”。例如 `from mini_deepinfer import EngineConfig` 只加载 `config.py`，不会加载引擎；`from mini_deepinfer import LLMEngine` 才加载 `engine.py`。这个设计让测试可以只导入一个模块，也避免了模块之间的循环导入。代价是 IDE 的静态补全可能看不到这些名字，读代码时要以 `__getattr__` 的分支为准。

测试映射可以按“机制 -> 测试文件 -> 断言”来读。分页、前缀哈希、COW、LRU 在 `tests/test_block_manager.py`，断言的是命中 token 数、引用计数、COW 后内容不变；调度、chunked prefill、抢占在 `tests/test_engine.py`，断言的是输出一致、所有请求完成、`preemptions >= 1`；推测解码在 `tests/test_speculative.py`，断言的是贪心输出与目标模型逐 token 一致；结构化输出在 `tests/test_structured_output.py`，断言的是正则 fullmatch、JSON 可解析、非法转义被拒绝；EPLB 在 `tests/test_eplb.py`，断言的是 `moe_lb` 改善且负载守恒；CP/EP/TP/PD 在 `tests/test_parallel.py`，断言的是与单卡参考的数值等价；HTTP 在 `tests/test_server.py`，断言的是状态码、SSE 终止和 usage 字段。

直接覆盖和间接覆盖要分开看：`config.py` 的交叉校验现在由 `tests/test_config.py` 参数化覆盖，`metrics.py` 的“最近一个 decode step”口径由 `tests/test_metrics.py` 覆盖；`errors.py` 的异常类型散落在各测试的 `pytest.raises` 里；`cli.py`、`corpus.py`、`llm.py` 没有专门测试，属于“示例覆盖、单元留白”。这不是说它们不重要，而是说读测试报告时不要把“268 个测试通过”理解成“每个模块、每个分支都有直接断言”。第 26 章和附录 I 会给出更完整的测试矩阵和补测建议。

## 小结

mini-deepinfer 是一个“机制完整、规模缩小、边界诚实”的教学实现。它用 NumPy 和玩具模型验证了 DeepInfer 的核心逻辑，但没有复制生产级的 kernel、分布式、存储和运维。保真度矩阵是读后面代码章节的地图：知道每个模块对应什么、差距在哪，就不会把“mini 能跑”误当成“生产可用”。

## 练习

1. 从保真度矩阵里选三个你最关心的机制，说明 mini 验证了什么、没有验证什么。
2. 如果要把 block size 从 16 改成 128，哪些测试会受影响？哪些逻辑不需要改？
3. 为什么 mini 选择手写反向传播，而不是引入一个自动微分库？这对理解推理系统有什么帮助？
4. 用 20.8 节的扩展方向，设计一个不超过一天的实验，写出假设、改动和验证指标。
