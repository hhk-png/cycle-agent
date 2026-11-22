# 第 34 章 前沿：HybridModel、DeepSeek 风格 MoE/MLA、1M 上下文、FP4 与 Muon

> **第 V 部分导读（34–36）**
> 本部分讨论 2026 的前沿与采用判断：HybridModel、DeepSeek 风格 MoE/MLA、
> 1M 上下文、FP4、Muon 与 Bridge。前置是 10/12/13/30；读完应能区分 L1–L4
> 信号、为新技术设计影子实验与回滚条件，并不被 roadmap/厂商数字带偏。


本章以 2026 年 11 月的公开 release notes/roadmap 为基线。具体功能可能已在
`dev` 分支或下一个版本中变化，阅读时以当前源码为准。

## 34.1 Megatron Core 0.20 的关键词

公开信息中，0.20 线附近的关键词包括（具体是否进入某个补丁号，以 release
notes 和源码开关为准）：

- **训练**：selective FP32 execution（含 FP32 logits）、distributed Muon 的
  改进、DeepSeek 风格 MoE 的 NVFP4 recipe、MoE/专家 GEMM 优化；
- **推理**：CUDA Graph、chunked prefill、prefix caching、speculative
  decoding、prefill-decode 分离、batch-invariant MoE；
- **数据**：DP-balanced sequence packing、THD batch fetching；
- **多模态**：vision/video inference、media-affinity routing、multi-turn
  prefix stitching、audio RoPE；
- **精度**：2D MXFP8、NVFP4 quant fusion、FP8 参数 gather。

注意两个常见混淆：

1. **GDN/GDP（Gated DeltaNet / Gated Delta Product）是模型结构**，属于
   SSM/线性注意力/混合模型家族，不是“动态推理”模式。它们可以用于推理，
   但把它列在“推理功能”里是分类错误。
2. **“DeepSeek-V3 NVFP4 训练”是框架 recipe，不是原论文的训练事实**。
   DeepSeek-V3 原训练是 H800 + FP8(E4M3)；NVFP4 是后续在 Blackwell 上做
   复现/移植时的 recipe。引用时要把“模型原来怎么训”和“框架能怎么训”分开。

以下条目按 36.1 的发布分级（L1–L4）和附录 J 的证据分级（E1–E4）理解；
“有开关”不等于“生产可用”。这些功能共同指向一个趋势：**训练与推理的边界在消失**。同一个模型/并行栈要
同时服务预训练、后训练、推理和部署，Megatron Core 正在把推理原语纳入核心。

## 34.2 HybridModel

HybridModel 把 Transformer、MoE、SSM/Mamba、混合注意力等不同层类型统一到
一个模型/调度框架中。基础支持在 0.1x 系列已经出现，但“能建模型”和“能在
大规模上高效调度”是两件事。作为**方向性信号**，roadmap 中的相关项包括：

- HybridModel 的 EP overlap 与 Megatron FSDP 集成；
- 分组 HybridStack 调度；
- HybridEP 的零拷贝/小 token/超稀疏/paged-stash 健壮性；
- 某些新 VL 模型的 PP 支持；
- VPP 调度与 pipeline 边界组合；
- 新模型家族的迁移 recipe。

这些条目会随 roadmap 调整；不要把“roadmap 出现”当成“已发布”。判断方法
仍然是：release notes 找到功能名 → `transformer_config.py` 找到开关 →
代码里找到消费路径 → 跑通一个最小例子。

为什么重要：

- 不同层类型的计算/通信模式不同，统一调度很难；
- MoE 的 all-to-all 与 TP/PP 的 overlap 需要全局规划；
- 多模态/长序列让“一个模型一种并行”不再适用。

## 34.3 DeepSeek-V3 与后续版本

DeepSeek 系列把以下技术推到大规模：

- 细粒度 MoE + 共享专家；
- MLA（低秩 KV 压缩）；
- FP8 训练；
- aux-free 路由/偏置调整；
- 多 token prediction（MTP）；
- 大规模 EP/all-to-all。

框架侧的能力是“recipe 化”的：DeepSeek 风格 MoE/MLA 的并行配置、FP8 训练、
active MTP 层的低精度量化、以及后续在 Blackwell 上的 NVFP4 recipe。Megatron
Core **0.20.0 的 release notes 明确写到 DeepSeek-V4 由新一代 HybridModel
类支持**（对应 #6403/#6404/#6405/#7469），Megatron Bridge 0.7 也提供了
`DeepSeekV4HybridModelProvider`/`DeepSeekV4TransformerConfig`。这属于
**L1（已发布）+ E2（官方发布说明级证据）**；具体结构、层调度（CSA/HCA）、
训练细节和并行配置仍要以官方技术报告、仓库 release notes 和 recipe 为准。
本书不根据二手文章推断未公开的 Flash 变体或结构细节。一个可核对的判断链是：

```text
模型技术报告 → HF 权重结构 → Bridge mapping/recipe → 数值对齐脚本 → 并行配置
```

任何一环缺失，都只能算“结构上有支持”，不能宣称“复现了训练”。V4 的细节
（例如 hash routing、ClampedSwiGLU、mHC、MTP 的层数）在 0.20 的不同补丁/
recipe 中可能仍在变化，引用时要带 tag/commit。

### 34.3.1 DualPipe 与 Megatron 调度器的映射

DeepSeek-V3 报告的 DualPipe 常被误读成“Megatron 的某个 VPP 开关”。实际上
它是**双向、成对 chunk** 的调度：

```text
设备同时持有靠近输入和靠近输出两端的层
方向 A 的 warmup 与方向 B 的 cooldown 重叠
方向 B 的反向把权重梯度填进方向 A 的气泡
```

它与 Megatron 的三种能力相关但不等价：

| 能力 | 解决的问题 | Megatron 侧线索 | 证据级别 |
|---|---|---|---|
| VPP/interleaved | 减少 uniform 1F1B 的气泡 | `--num-layers-per-virtual-pipeline-stage`、interleaved schedule | L1/E2–E3 |
| zero-bubble | 用 W 梯度填气泡 | ZB1P/ZB2P 类调度（49.8） | L1–L2/E2–E3 |
| DualPipe | 双向 chunk 配对 | 没有公开的一一对应 flag；需要看具体实现/recipe | L3–L4/E1–E2 |

因此，“Megatron 支持 DeepSeek-V3 的 pipeline”这句话至少要说清楚：是支持
它的 MoE/MLA **模型结构**，还是复现了它的**调度**。前者有 mapping/recipe 可
验证，后者需要调度器代码与气泡/显存数据。不要把模型结构支持当成调度支持。

### 34.3.2 MTP：辅助头、loss 与并行布局

MTP（Multi-Token Prediction）在主干 next-token 目标之外，增加 `D` 个“预测
未来第 `k` 个 token”的辅助深度。DeepSeek-V3 用的是 `D=1`（除主头外再预测
一个未来 token），但结构上是可扩展的。它常被误认为“投机解码的小技巧”；
实际上它是一个**训练目标 + 额外的 transformer 深度**，会改变显存、通信和
流水线负载。

**结构与 token 对齐。** 设主干在位置 `i` 的 hidden 为 `h_i^0`，主头预测
`t_{i+1}`。第 `k` 层 MTP（`k=1..D`）做：

```text
e_i^k   = Emb(t_{i+k})                         # 未来 token 的 embedding
u_i^k   = Linear_k([RMSNorm(h_i^{k-1}); RMSNorm(e_i^k)])   # 2h -> h
h_i^k   = TransformerBlock_k(u_i^k)
logits_i^k = Out(h_i^k)                        # 与主模型共享输出头
loss_k  = CE(logits_i^k, t_{i+k+1})
L_total = L_main + (λ/D) · Σ_k loss_k
```

三个容易写错的点：

1. **token 偏移**：第 `k` 层输入的是 `t_{i+k}`、预测的是 `t_{i+k+1}`。如果
   错位一格，辅助 loss 会“偷看”标签或学到恒等映射，训练 loss 看起来正常，
   但推理时草稿全错。
2. **共享权重**：embedding 与输出头通常与主模型共享，MTP 只新增每层的
   `2h→h` 投影和一个 transformer block。共享意味着梯度会累加，反向时不能
   把辅助 loss 的图提前释放（同 19.6 的 MoE aux 问题）。
3. **loss 权重**：`λ` 太大会拖慢主任务，太小则辅助头学不动。要做消融，并
   分别记录 `L_main` 与每个 `loss_k`，不要只看总 loss。

**计算与显存账。** 设主干 `L` 层、hidden `h`、序列 `S`、batch `B`。每层 MTP
增加一个 block 的前向/反向，训练 FLOPs 增量约为 `D/L`（不含投影和 CE）；
`D=1, L=61` 时约 1.6%。但显存增量不是 1.6%：

- 每个 MTP block 的激活要保存（或重算），约一个 block 的量；
- 辅助 logits 是 `[B,S,V]`，如果一次算 `D` 个深度，峰值是 `D` 份词表大小
  的 logits；生产实现通常逐深度计算/立即反传，避免同时驻留；
- 共享输出头不增加参数，但 `D` 个 CE 会增加 vocab-parallel 的通信次数。

**并行放置。** MTP 天然放在**最后一个 pipeline stage**：它需要主干 hidden
和输出头，而输出头本来就在最后一段。这带来两个后果：

- 最后一段的负载增加 `D` 个 block；均匀 PP 会把气泡和 straggler 集中到
  最后一段，应该按 8.11 的非均匀切分给最后一段少分主层；
- 如果 MTP block 是 MoE，它还会在最后一段引入 all-to-all，和主模型的 EP
  通信竞争；需要单独核算 expert 放置。

TP 下 MTP block 与普通 block 相同（Column/Row 切分、两次 all-reduce）；
CP 下最麻烦的是 **`t_{i+k}` 的跨 rank 偏移**：位置 `i+k` 可能属于下一个 CP
rank。两种做法：

```text
A. 先做一次跨 CP 的 token shift（p2p 或 all-gather 边界 k 个 token），
   再构造 e_i^k；简单，但每层多一次小消息。
B. 在数据侧预先生成 shifted labels/embeddings 并随 batch 传入；
   通信少，但数据管线和 packing 的文档边界要对齐。
```

如果 packing 了多个文档，`t_{i+k}` 不能跨文档；MTP 的 label 也必须遵守
document mask，否则辅助头会学习“跨文档续写”。

**推理侧。** MTP 头可以丢弃（只保留主模型），也可以当投机解码的草稿头
（40.5.4）。当草稿头时要注意：

- 草稿的因果链必须与目标模型一致：第 `k` 层草稿依赖前 `k-1` 层的输出，
  不能并行假设独立；
- 共享 embedding/输出头降低了额外显存，但每个 draft step 仍要跑 MTP
  block，收益取决于接受率 `α` 与 block 的延迟；
- 训练时的 teacher forcing 输入是真实未来 token，推理时是草稿自己采样
  的 token，分布不一致会拉低接受率；需要用目标模型生成的数据做校准。

**框架支持怎么核对。** Megatron Core 在
`megatron/core/transformer/multi_token_prediction.py` 提供 MTP 实现：
`MultiTokenPredictionBlock`、`MultiTokenPredictionLayer`、
`MultiTokenPredictionBlockSubmodules`、`MTPLossAutoScaler`、
`process_mtp_loss`，以及 `mtp_on_this_rank`/`get_mtp_layer_offset`/
`get_mtp_num_layers_to_build` 等放置辅助函数；CP 输入由
`MultiTokenPredictionInputs` 准备。类名/开关/checkpoint 命名仍会随版本漂移，
按 29.4.1 的方法核对：

```text
[ ] release tag/commit 与 recipe 对得上
[ ] 找到 MTP 模块的定义与消费点（不是只有 config 字段）
[ ] 确认 embedding/输出头是否共享、loss 权重怎么传
[ ] 确认 PP 放置（是否强制最后 stage）与 CP 的 token shift 实现
[ ] 用固定权重做“主头 logits 不变 + 辅助头形状/偏移正确”的数值测试
```

如果任何一项缺失，只能算“结构上支持”，不能对外宣称“复现了某模型的 MTP
训练”。这也是 36.1 的 L1–L4 分级在模型结构类声明上的具体用法。

## 34.4 1M 上下文

长上下文的技术栈：

- 位置编码外推（RoPE scaling/YaRN/NTK）；
- 稀疏/混合注意力（滑动窗口 + 全局 token + indexer）；
- CP/ring attention；
- 数据课程（4K→…→1M）；
- 激活/ KV 显存优化；
- 评测（needle、长文推理、代码库）。

公开 roadmap（按 36.1 为 L3 信号，不是发布承诺）提到：

- 64K→1M 上下文训练课程验证；
- load-balanced CP indexer 与 CP 通信 overlap；
- 降低 indexer/混合注意力显存；
- 16K dense / 64K sparse / 1M sparse 的端到端收敛验证。

1M 不是单一开关，而是模型、数据、系统、评测的共同工程。

### 34.4.1 稀疏/线性注意力：四条路线与并行账本

把上下文从 32K 推到 1M，只靠 CP 会让每层的 `O(s²)` 计算和 KV 读写仍然
线性/平方增长。生产上通常同时改注意力结构。四条路线要分开看：

| 路线 | 代表 | 计算 | KV/状态 | 训练方式 | 并行影响 |
|---|---|---|---|---|---|
| 滑动窗口 | Mistral 类 | `O(s·w)` | rolling cache `O(w)`（若只保留窗口） | 从头训/继续预训练 | CP 按块均分即可；全局信息靠少数层/全局 token |
| 全局 + 局部稀疏 | Longformer/BigBird | `O(s·(w+g))` | 全局 token 的 KV 仍需 `O(s)` | 从头训/微调 | 稀疏 mask 要按块生成；CP 负载不均 |
| 学习型稀疏/indexer | NSA、MoBA 类 | `O(s·w + s·k)` | 压缩/分块，通常仍 `O(s)` | 需要预训练 | indexer 的 top-k 是离散、batch-dependent；CP/TP 要切 indexer |
| 线性注意力/SSM | Mamba、GLA、RWKV | `O(s)` | 常数状态 | 从头训 | CP 不能 all-gather KV，要按序列传状态；PP/TP 需专用 kernel |
| KV 压缩（非稀疏） | GQA、MLA | 仍 `O(s²)` | `O(s·d_c)` | 从头训/继续预训练 | 只改 cache 布局，不改 attention 的并行语义 |

四个容易踩的坑：

1. **“稀疏”不等于“便宜”**。滑动窗口把计算降到 `O(s·w)`，但若每层仍保留
   完整 KV（为了全局检索），显存并不按 `w` 降；反过来，只保留窗口 KV 会
   丢失窗口外信息。要分别算 **计算账**、**KV 账**、**质量账**。
2. **indexer 破坏 batch invariance**。top-k 选择依赖当前 batch 的分数分布；
   同一请求在不同 batch 组成下可能选到不同块，导致评测/缓存/投机解码不
   一致。生产实现要么固定路由，要么使用 batch-invariant kernel。
3. **CP 的负载不再均匀**。因果 + 块稀疏时，不同 CP rank 负责的 query 块看到
   的有效 KV 块数不同；要按“块对”而不是“连续区间”切分，否则最慢 rank
   决定尾延迟。ring attention 的 load-balanced causal mask 是同一类问题。
4. **稀疏 mask 的顺序错会静默降低质量**。mask 生成、block table、RoPE 位置
   三者只要有一个用了本地索引，loss 仍会下降，但长文检索会坏；必须用
   needle/RULER 这类“位置敏感”的评测，而不是只看验证 loss。

一个务实的迁移顺序：

```text
32K dense（基线，确认数据/并行/评测都正确）
  → 64K/128K：先上 CP + RoPE scaling，保持 dense attention
  → 128K/256K：滑动窗口/全局 token，验证检索不降
  → 1M：学习型稀疏/indexer 或 hybrid SSM，算力/显存/质量三条线同时验收
```

每一步都要保留一个 dense/短上下文回退路径。稀疏结构一旦在训练早期引入，
排查“是数据、是 mask、还是 indexer”会比 dense 难一个量级。

### 34.4.2 1M 上下文的系统验收

长上下文不是“能跑通一次前向”，而是一组必须同时满足的约束：

```text
[ ] 位置编码：目标长度上的 perplexity 不崩，短序列不退化
[ ] 注意力：目标长度下不 OOM；CP/稀疏的负载 p99 可接受
[ ] 数据：长文/代码库/多轮数据占比足够，且训练课程覆盖目标长度
[ ] 评测：needle、RULER、长文 QA、长代码至少各有一项，且有污染检查
[ ] 推理：KV cache/分页/prefix cache 在目标并发下满足 TTFT/TPOT
[ ] 回退：短上下文路径仍可用，长上下文失败时可降级
```

其中“短序列不退化”最容易被忽略：位置插值/NTK 参数配错时，4K 评测可能
正常，32K 检索却崩；反之，过度拉伸也会让 4K 的局部能力下降。验收必须同时
看两端，而不是只看目标长度。

## 34.5 FP4 与 Blackwell

Blackwell（B200/GB200/GB300）把 FP4 推向训练/推理：

- NVFP4：4 位浮点 + 块缩放；
- MXFP8/MXFP4：微缩放格式；
- 2D MXFP8 量化；
- FP4 参数 gather / grouped-expert kernel 融合；
- selective FP32 保留敏感路径。

需要区分：

- **推理 FP4**：权重/激活量化，追求吞吐/显存；
- **训练 FP4**：需要梯度/优化器状态/数值稳定性，难度更高；
- **QAT/PTQ**：量化感知训练 vs 训练后量化。

厂商 TFLOPs 数字通常在 FP4/稀疏/特定 shape 下测得，不能与 bf16 dense 直接比。

## 34.6 Muon 与新优化器

Muon 用 Newton–Schulz 正交化动量，可能在大模型上优于 AdamW。Megatron Core
distributed Muon 的实现涉及按 TP/专家/参数分片、跨 rank 的矩阵正交化与
Triton SYRK 等 kernel 细节（具体支持矩阵以当前源码为准）。2026 的
Emerging-Optimizers 库也在推动 Muon/混合优化器。

未解问题：

- 与 MoE/MLA/长上下文的交互；
- 混合参数（embedding/norm）的优化器分配；
- 分布式正交化的通信成本；
- 大规模收敛/超参迁移；
- 与 FP8/FP4 的数值兼容。

## 34.7 推理与训练的融合

2026 的关键词：

- prefill-decode 分离（NCCL/NIXL KV 传输、SSM state handoff）；
- chunked prefill、prefix caching、speculative decoding；
- batch-invariant MoE；
- CUDA Graph；
- 多模态推理；
- 与 vLLM/TensorRT-LLM 的互操作。

这意味着 Megatron Core 不再只是“训练框架”，而是训练/推理共用的一套并行与
模型抽象。对平台团队来说，可以减少“训练一套、推理一套”的重复工程。

## 34.8 确定性与可复现

工程上已有一些团队在特定配置下做到 bitwise-deterministic pretraining，但
这不是“打开一个开关”的通用能力，也不是所有硬件/并行组合都能做到。完全
确定性需要：

- 固定 kernel/reduction 顺序；
- 固定并行度/拓扑；
- 关闭非确定性算法；
- 固定数据/RNG；
- 接受性能损失。

对合规/科研复现有价值，但大规模生产通常追求统计等价。

## 34.9 功能索引：从关键词到代码与故障

下表是“看到一个功能名，去哪里找、先坏在哪里”的索引。参数名以 Megatron Core
0.20.x 为基线，版本漂移时以当前源码为准。

| 功能 | 常见参数/入口 | 代码区域 | 先坏在哪里 |
|---|---|---|---|
| HybridModel | `hybrid_layer_pattern`、层类型列表 | `transformer/` 的 hybrid stack | PP 各 stage 层类型不均，负载倾斜 |
| MLA | `multi_latent_attention`、q/kv LoRA rank | `transformer/multi_latent_attention.py` | RoPE 与吸收顺序、latent 切分 |
| FP8 | `fp8`、`fp8_recipe`、amax history | Transformer Engine 接入层 | scale 溢出/欠流、loss spike |
| FP4/NVFP4 | `--fp4-format e2m1`、NVFP4 recipe（名称以 `arguments.py` 为准） | TE/Blackwell kernel | 收敛退化、无回退路径 |
| VPP | `virtual_pipeline_model_parallel_size` | `pipeline_parallel/schedules.py` | 层数不能被 `pp·v` 整除、激活暴涨 |
| Zero-bubble | schedule 选择 | pipeline schedules | 权重梯度延迟、显存/依赖错 |
| Distributed Muon | optimizer 选择、NS steps | `optimizer/` | 正交化通信、混合参数分组 |
| CP indexer | CP 算法/稀疏注意力 | `transformer/` + CP | indexer 显存、负载不均 |
| Chunked prefill | inference 参数 | `inference/` | KV cache 分页、TTFT/TPOT 权衡 |
| PD 分离 | prefill/decode role | inference serving | KV 传输带宽、故障域 |

使用方式：先用“代码区域”定位实现，再用“先坏在哪里”设计验收用例。不要只看
release note 的功能列表；每个功能都有一个最先暴露问题的维度。

## 34.10 判断清单：一个新功能是否值得跟进

面对 2026–2027 的任何新功能，按顺序问：

1. **它解决的是正确性、性能还是规模问题？** 正确性问题优先，性能问题要看
   端到端，规模问题要看集群。
2. **它改变了哪些数值语义？** 精度、reduction 顺序、路由、mask、RNG。
3. **它的失败模式是什么？** 有没有回退路径/影子实验/checkpoint 兼容性。
4. **它有 100B+ 或同等规模的可复现证据吗？** 没有就只做小规模验证。
5. **它和现有并行布局如何组合？** TP/PP/DP/CP/EP 的组是否正交。
6. **它的运维成本是多少？** 新依赖、新监控、新故障域。

这六个问题比“支持不支持”更能决定是否采用。

## 34.11 本章小结

- 0.20 的主线是 HybridModel、MoE/DeepSeek、长上下文、FP4、推理一体化。
- 1M 上下文需要模型/数据/系统/评测协同。
- FP4 训练仍在早期，厂商数字要看清口径。
- Muon 是新变量，但分布式/混合参数/收敛仍需验证。
- 训练与推理的边界正在消失。

下一章讨论 Megatron Bridge 与生态互操作。
