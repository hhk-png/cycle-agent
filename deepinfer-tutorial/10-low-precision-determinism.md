# 第 10 章 低精度、确定性与 batch invariance

本章导读：低精度不是“把 dtype 改小”这么简单，它涉及 scale 布局、反量化融合、硬件支持和数值误差。更隐蔽的是确定性问题：同一个请求在不同 batch 组成下可能得到不同输出，导致评测不可复现、线上问题无法复现。本章解释 DeepInfer 如何定义和测试 batch-invariant 与 deterministic sampling，并给出 mini 的对应实验。

## 10.1 精度格式速览

常见的推理精度格式：

- **FP16/BF16**：2 字节，BF16 动态范围大、尾数少，训练和推理都常用。
- **FP8 E4M3**：1 字节，4 位指数、3 位尾数，适合权重和激活；需要一个 scale 把张量映射到可表示范围。
- **FP8 E5M2**：1 字节，5 位指数、2 位尾数，动态范围更大、精度更低。
- **FP4 E2M1**：0.5 字节，2 位指数、1 位尾数，只有 16 个可表示值；必须配合 per-block scale。
- **MXFP4**：每 32 个值共享一个 ue8m0 scale（2 的幂），是 OCP 的微缩放格式。
- **NVFP4**：每 16 个值共享一个 E4M3 scale，精度比 MXFP4 高一些，但 scale 开销更大。

DeepSeek-V4.1-Flash 的公开资料说，主 KV 用 per-16 E4M3 scale（类似 NVFP4，但没有全局 scale），indexer KV 用 per-32 ue8m0（MXFP4），MoE 路由专家权重也用 per-32 ue8m0。DeepInfer 的 `derive_main_kv_cache_num_per_channels` 里，主压缩 KV 每 16 个通道一个 scale，SWA 每 32 个通道一个 scale。

用字节数算一下：FP16 的 1 个值占 2 字节；FP8 占 1 字节；FP4 占 0.5 字节，但如果每 16 个值加 1 字节 scale，平均是 0.5625 字节；MXFP4 每 32 个值加 1 字节，平均 0.53125 字节。scale 的开销看起来小，但在 KV 这种“每 token 每层”的结构里，会直接放大总容量。附录 B 的 `kv_bytes_per_token` 公式把这些项加在一起；第 4 章的估算直接用了压缩后的实测口径，没有再逐项展开 scale。

## 10.2 scale 与 kernel 融合

量化的基本公式是 `x_q = round(x / s)`，反量化是 `x ≈ x_q × s`。如果先反量化成 BF16，再做 GEMM，那么你只是把存储变小了，计算和带宽并没有省；真正省带宽的是“量化权重直接参与低精度 GEMM，累加在更高精度”。

这要求 kernel 把反量化和矩阵乘融合。DeepGEMM 的 FP8/FP4 GEMM 会在加载权重时应用 scale，累加器用 FP32；FlashMLA 的 KV 读取会在 attention 内做反量化；TileLang 的 fused norm+rope+cast+store 把多个逐元素操作合成一个 kernel，减少中间张量。

scale 的布局也会影响性能。per-tensor scale 最简单，但精度差；per-channel/per-block scale 精度好，但需要额外的内存访问和布局变换。DeepInfer 的 `transform_sf_into_required_layout` 就是在把 scale 排成 kernel 需要的 TMA 对齐布局。

这里要说明 mini 的边界：mini 的**主生成路径**始终是 float32，`quantization.py` 只提供 FP8/FP4 的参考量化和误差实验，没有接进 decode，也没有 batch-invariant kernel。所以 mini 的确定性测试不能替代真实的低精度验收；第 20.4 节的保真度矩阵会把这条边界再列一次。

一个常见的性能陷阱是“反量化后再做一次 kernel launch”。如果反量化单独跑一个 kernel，它要把整个权重写回 HBM，下一层 GEMM 再读一遍，带宽收益被吃掉两次。融合的做法是在 GEMM 的 tile 加载阶段反量化，权重只从 HBM 读一次，反量化的结果留在寄存器/SRAM。

用字节算一遍就很清楚。假设每个权重值用 FP4 存储，平均 0.5625 字节（含 per-16 scale）。融合路径每读一个值只搬 0.5625 字节；非融合路径先把 FP4 读进来（0.5625 字节），反量化成 BF16 写回（2 字节），GEMM 再读 BF16（2 字节），合计 4.5625 字节。也就是说，非融合的 FP4 路径比直接用 BF16 权重（2 字节/值）还多搬一倍多。量化省下的显存是真的，但如果没有融合，带宽收益会变成带宽损失。这也是为什么“把权重存成 FP4”本身不是优化，只有“FP4 存储 + 融合反量化的 GEMM”才是。

## 10.3 低精度的误差从哪里来

量化误差主要有三类：

1. **表示误差**：FP4 只有 16 个值，尾数只有 1 位，接近 0 的数和大的数都容易失真。
2. **scale 误差**：一个 block 共用一个 scale，block 内动态范围大时，小值会被压成 0。
3. **累加误差**：低精度乘法的结果在 FP32 累加，通常没问题；但如果累加顺序随 batch 变化，结果会有微小差异。

对 LLM 来说，“哪个张量更敏感”没有统一答案：权重量化最直接、风险相对可控；激活动态范围大、离群值多，往往是量化误差的主要来源；KV 量化还会影响长上下文召回。评估不能只看 perplexity，要看任务指标、长上下文和结构化输出，并按权重、激活、KV 分别做消融。DeepInfer 仓库的 tests/models/test_offline_inference.py 会用固定的 PPL baseline 做逐样本比较，任何数值变化都要显式刷新 baseline。

误差还会被“路由”放大。MoE 的 top-k 选择对分数很敏感，量化误差可能让某个 token 的 top-k 专家发生变化，进而改变整层输出。这类误差在 perplexity 上可能看不出来，但在具体任务上会表现为“偶尔答错”。评估低精度时，要同时看路由一致性、KV 召回和结构化输出合法率。

## 10.4 batch invariance 是什么

Batch invariance 指：同一个请求，在不同的 batch 组成下，输出完全一致。这听起来应该天然成立，但实际上很多 kernel 的归约顺序、split-k、动态 batch 调度会随 batch 改变，导致浮点结果有 1e-6 到 1e-3 的差异；在 greedy 解码时，这个差异可能让 argmax 翻转，输出就变了。

DeepInfer 提供 `DEEPINFER_BATCH_INVARIANT=1`（仅 NVIDIA）来开启 batch-invariant 推理。它要求 kernel 在不同 batch 下使用相同的归约顺序和相同的数值路径。代价是可能牺牲一些性能，因为不能根据 batch 动态选择最优的 split-k 策略。

mini-deepinfer 的实现天然 batch-invariant：每个序列独立做前向，矩阵乘法的形状只取决于该序列，不取决于 batch；采样用每个序列自己的 RNG。测试 `test_batch_invariance` 用同一组 prompt 和 seed，比较批量生成与逐条生成，结果逐字符一致。

严格来说，batch invariance 有三种强度。第一种是“逐位一致”，要求所有浮点运算的顺序完全相同；第二种是“容差内一致”，logits 在 1e-6 内，greedy 输出相同；第三种是“统计一致”，分布相同但单次输出可能不同。生产系统通常追求第二种，因为逐位一致会限制 kernel 的优化空间。

## 10.5 deterministic sampling

Deterministic sampling 指：相同的输入 token、相同的采样参数、相同的 seed，输出完全一致。它和 batch invariance 是两个正交的轴，不是“谁更强”。一个实现可以每次运行都用全局 RNG、同 seed 得到相同结果，但换一个 batch 组成就变了（deterministic 但不 batch-invariant）；也可以开启 batch-invariant kernel、却用系统随机源做 seed，两次运行结果不同（batch-invariant 但不 deterministic）。要同时满足两者，既需要固定的浮点归约顺序，也需要每序列独立、可复现的 RNG 路径。

DeepInfer 的文档给出的开启方式是：

```bash
DEEPINFER_BATCH_INVARIANT=1 DEEPINFER_ENABLE_MTP_VARLEN=0 DEEPINFER_USE_NEXTN_DECODE=0
```

并在 `SamplingParams` 里传固定的 `seq_seed`。为什么要关掉 MTP varlen？因为可变长验证会让每步接受的 token 数随 confidence 变化，虽然数学上仍可复现，但随机数的消耗顺序会变，难以保证跨版本一致。关掉 `MTP_VARLEN` 只是把验证长度固定下来；如果目标是“每步只前进一个 token”的纯目标模型路径，还要把 `DEEPINFER_USE_NEXTN_DECODE` 设为 0，否则 nextn 默认仍是 5，推测解码仍在运行。三个开关要一起看，不能只关一个就宣称“已经关掉推测”。

`SamplingParams` 会校验 seed 是否落在 uint64 范围内，超出就抛 `ConfigError`，而不是悄悄截断；`effective_seed` 是校验后按 uint64 掩码得到的值。`seq_seed` 是每个序列独立的随机种子。mini-deepinfer 用 `np.random.default_rng(effective_seed)` 为每个序列创建 RNG，采样时只从这个 RNG 取数，因此 batch 顺序、并发数、调度顺序都不会影响结果。

一个容易忽略的点是“随机数消耗顺序”。如果采样实现先对 top-k 排序、再按排序后的索引取随机数，那么 top-k 的并列顺序会改变随机数对应的 token。要保证跨版本可复现，必须固定排序规则（例如按概率降序、概率相同按 token id 升序），并把随机数的消耗次数固定下来。

## 10.6 温度 0 的陷阱

很多人以为 `temperature=0` 就是贪心，实际上不同框架的处理不一样。DeepInfer/vLLM 的做法是：当 temperature 小于 `1e-5` 时，把它设为 `1e-5`，同时把 top_p 设为 1、top_k 设为 -1，然后采样器走 greedy/argmax 分支。也就是说，结果确实是 argmax，而不是“几乎确定性的随机采样”；钳位只是为了保持参数合法和数值路径统一。

mini-deepinfer 也把温度钳到 `1e-5`，但用显式的 `greedy` 属性判断，然后在 `distribution` 里直接返回 one-hot。两者在语义上都是 argmax，差别只在“谁来做短路”：真实框架在 sampler 内部判分支，mini 在分布函数里判。书中比较输出时，只比较同一实现内的结果，不跨实现做逐 token 断言。

还有一个陷阱是“temperature 很小但不是 0”。在 `1e-5` 附近，softmax 的数值可能溢出或下溢，top-p 的累积和也可能因为浮点误差而多保留/少保留一个 token。生产实现通常会在采样前做数值稳定化，并在测试里覆盖边界值。

## 10.7 如何测试

正确的测试顺序是：

1. **单实现确定性**：同一 seed、同一输入，跑两次，输出一致。
2. **batch invariance**：批量 vs 逐条，输出一致。
3. **chunked prefill 一致性**：完整 prefill vs 分块 prefill，logits 或输出一致。
4. **前缀缓存一致性**：有缓存 vs 无缓存，输出一致。
5. **推测解码一致性**：贪心推测 vs 纯目标贪心，逐 token 一致。
6. **低精度 baseline**：固定 PPL/任务指标，数值变化必须显式确认。

DeepInfer 仓库的 tests/models/test_batch_invariant.py 和 test_offline_inference.py 覆盖了这些；mini-deepinfer 的 `tests/test_engine.py` 覆盖了前五项。

测试设计要注意“控制变量”。例如验证前缀缓存一致性时，必须固定 prompt、seed、max_tokens、block size 和调度参数；如果 prefix cache 改变了 block 分配，可能间接改变 chunk 边界，进而改变数值路径。这类耦合是“测试看起来通过，但换了配置就失败”的常见原因。

## 10.8 性能与正确性的取舍

Batch invariance 和 deterministic sampling 通常会牺牲一点性能：不能按 batch 动态选 kernel，不能使用依赖 batch 的快速路径，可能要多做一次同步。DeepInfer 把它们做成可选开关，而不是默认开启，因为在线服务更看重吞吐，评测和调试更看重可复现。

一个实用的策略是：生产默认关闭 batch invariance，但在评测、回归、A/B 实验和故障复现时开启；对需要严格可复现的客户或合规场景，默认开启并接受性能损失。这个决策应该写进服务等级文档，而不是藏在环境变量里。

还有一个折中方案是“按请求开启”：对需要复现的请求（例如评测、审计、调试）使用确定性路径，对普通流量使用快速路径。这要求引擎在调度时把两类请求分开，避免互相影响 batch 组成。实现复杂度更高，但能把性能损失限制在需要的请求上。

## 10.9 硬件差异

不同硬件的低精度支持不同。SM100 有 FP4 的 tensor core 指令和 TMA，Ascend 950 有自己的低精度格式和通信库；同一段 kernel 代码在两个平台上可能需要不同的 scale 布局和累加顺序。这就是为什么 DeepInfer 要有平台抽象层，而不是把 CUDA 代码直接写死。

即使在同一厂商的不同代 GPU 上，FP8 的舍入模式、denormal 处理、累加器宽度也可能不同。跨硬件比较输出时，不要期望逐位一致；应该比较容差内的 logits、greedy 输出的一致率（允许个别 argmax 翻转）和任务指标。第 10.4 节已经说过，1e-6 的差异就可能翻转 argmax，所以“greedy 逐 token 相等”不能作为跨硬件验收条件。如果业务要求跨硬件可复现，应用层能做的是固定算法路径、固定 seed、固定归约策略和容差，并记录硬件/驱动版本；它无法让不同硬件的浮点结果逐位相同。真正需要逐位一致的场景，只能限定在同一硬件、同一软件栈、同一 batch 组成下，或者把关键计算移到软件模拟的确定性路径上，接受性能损失。

## 10.10 低精度的验收矩阵

低精度上线不能只看“显存省了多少”。一个实用的验收矩阵有六行。

**数值一致性**：同一输入、同一 seed，低精度和 BF16 的 logits 在容差内一致；greedy 输出尽可能一致。容差要按任务定，不能用一个 `1e-2` 掩盖所有问题。

**任务质量**：在固定评测集上比较准确率、pass@k、长上下文召回、结构化输出字段准确率。平均分不变不代表所有子集不变；要按任务类型、长度、语言分组看。

**路由一致性**：MoE 的 top-k 选择对分数敏感，低精度可能让某些 token 的专家路由变化。要统计路由一致率和每个专家的 token 分布；如果热点专家变化很大，即使平均分不变，尾延迟也会变。

**长上下文召回**：KV 量化对远距离信息的损伤可能在小上下文评测里看不出来。要用“大海捞针”、多跳推理、位置偏差测试，覆盖开头、中间、结尾。

**结构化输出**：mask 之后合法率可能仍然 100%，但字段值可能变差。要同时看合法率、字段准确率、完成率和重复率。

**确定性**：开启 batch invariance 和固定 seed 后，同一请求在不同 batch 组成下输出一致；跨硬件不要求逐位一致，但要在容差内一致。如果业务要求跨硬件可复现，必须在应用层做约束，不能依赖底层 kernel。

每一行都要有 baseline、阈值和回归测试。低精度是一个“用质量换成本”的交易，交易是否划算取决于业务，不取决于单项 benchmark。

## 10.11 量化工作流

把模型从 BF16 降到 FP8/FP4 不是一个开关，而是一条工作流。

**第一步，选格式。** 权重、激活、KV 可以分别选精度。权重通常最先量化，因为收益直接、风险可控；KV 量化影响长上下文召回，要单独评测；激活量化对 kernel 和校准要求最高。格式要和硬件、kernel 对齐：SM100 的 FP4 和 Ascend 950 的低精度格式不完全一样。

**第二步，选粒度。** per-tensor 最简单但精度差；per-channel/per-block 精度好，但 scale 布局更复杂。DeepInfer 的主 KV 每 16 个通道一个 E4M3 scale，indexer KV 每 32 个通道一个 ue8m0 scale，MoE 专家权重用 MXFP4；这些粒度不是随意选的，而是和 kernel 的加载、TMA 对齐、累加精度绑定。

**第三步，校准。** 用代表性数据估计 scale，数据要覆盖长度分布、语言、领域和 batch 形状。校准集太小或太偏，会让某些层的动态范围估计错误，表现为长尾输入质量下降。PTQ（训练后量化）成本低，QAT（量化感知训练）质量好但需要训练资源；推理框架通常只负责 PTQ 和加载。

**第四步，验证。** 按第 10.10 节的验收矩阵跑数值、任务、路由、长上下文、结构化输出和确定性。任何一项退化都要决定“接受、调 scale、换粒度、放弃”。

**第五步，灰度。** 先在离线流量或小租户上跑，观察错误率、P95 延迟、显存、路由分布和质量指标；再逐步放量。回滚要能切回 BF16/FP8 权重，且 KV 格式兼容。

**第六步，监控。** 量化后的模型可能在不同输入分布下表现不同；要持续监控输出长度、重复率、拒答率、结构化输出合法率和用户反馈。量化不是一次性发布，而是一个持续的质量监控对象。

这条工作流的每一步都可能成为瓶颈。一个常见的失败是“只跑了 perplexity 就上线”，结果长上下文召回掉了几个点；另一个是“校准集只用了短文本”，长文档的 scale 失真。量化省下的显存和带宽是真实的，但质量损失也是真实的，必须用评测和灰度来平衡。

## 小结

低精度通过减少字节数来缓解 decode 的带宽瓶颈，但收益取决于 scale 布局和 kernel 融合；batch invariance 和 deterministic sampling 保证优化不会悄悄改变输出，代价是性能灵活性。DeepInfer 用环境变量和固定 seed 提供这些保证，mini-deepinfer 用独立 RNG 和逐序列前向天然满足。测试要覆盖确定性、批不变性、chunked prefill、前缀缓存和推测解码五条路径。下一章把 deterministic sampling 展开成完整的采样与输出契约，包括温度、top-k/top-p、停止条件、logprobs 和流式 UTF-8。

## 练习

1. 解释为什么“把 KV 存成 FP4 再反量化成 BF16 做 attention”可能没有性能收益。
2. 为什么 batch invariance 在 greedy 解码下比在随机采样下更容易被观察到？
3. 设计一个测试，验证“开启前缀缓存不改变输出”。你需要固定哪些变量？
4. 比较逐位一致、容差内一致、统计一致三种强度，分别适合什么业务场景？
