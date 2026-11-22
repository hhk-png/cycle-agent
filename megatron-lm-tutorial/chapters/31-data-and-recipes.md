# 第 31 章 数据管线与训练配方

## 31.1 数据是规模化训练的第一瓶颈

当模型/并行调好之后，训练速度往往被数据管线限制：

- tokenize 速度跟不上 GPU；
- 存储 IO 抖动；
- 数据混合权重不合理；
- 重复/低质量数据导致 loss 下降但能力不涨；
- 序列长度分布与 packing 不匹配；
- 数据加载在 step 边界造成空档。

Megatron 的数据层提供 indexed dataset、blending、packing、sampler；生产还需要
自己的清洗/去重/质量过滤。

## 31.2 数据格式

常见流程：

```text
raw text/jsonl
  → 清洗/去重/质量过滤
  → tokenizer
  → uint16/uint32 token shard
  → index（文档边界/长度/来源）
  → blend（按权重采样）
  → pack（拼成固定长度）
  → batch（DP 分片 + prefetch）
```

- uint16 适合 vocab < 65536；更大词表用 uint32；
- index 记录每个 shard 的 token 数/文档边界；
- pack 把多个短文档拼进一条序列，附带 document mask，避免跨文档 attention；
- 变长序列可用 THD（total tokens, heads, dim）布局和序列 packing。

## 31.3 数据混合

预训练数据通常不是单一来源，而是按权重混合：

```text
web, code, math, books, multilingual, ...
```

混合权重影响能力分布。常见做法：

- 按 token 比例而不是文档数；
- 对高质量数据上采样；
- 训练中后期调整权重（curriculum）；
- 监控各来源的 loss/梯度；
- 避免某个来源重复过多导致记忆/退化。

DeepSeek/LLaMA 等模型的报告会给出数据配比，但复现时还要看清洗/去重策略。

## 31.4 Packing 的配方视角

Packing 的**实现细节**（块对角 mask、position reset、loss mask、THD 布局、
mini 的 `PackedSequenceDataset`）在 **38.6 节**，这里只讨论它如何改变训练
配方。

不 packing 时，每条样本 padding 到 `max_len`；短样本的 padding 也被算进
FLOPs。设文档长度分布为 `l`，padding 浪费率为：

```text
waste = 1 - E[l] / max_len
```

例如平均长度 700、`max_len=4096`，约 83% 的计算花在 padding 上。Packing
把多条文档拼进同一条 `seq_len`，把浪费压到“最后一条 packed 序列的尾部”，
代价是 document mask 和 position reset。

对配方的影响：

| 决策 | 不 packing | packing |
|---|---|---|
| 每步有效 token | 受 padding 浪费 | 接近 `global_batch × seq_len` |
| loss 口径 | 只在有效 token 上平均 | 必须用 document/loss mask，否则跨文档预测会污染 loss |
| 序列长度课程 | 直接改 `seq_len` | 改 packed 长度，短文档被拼在一起，长度分布被“平滑” |
| 评测可比性 | 与训练一致 | 评测若不解 pack，loss 不可直接比 |
| 数据混合 | 按样本/文档 | 建议按 token；否则长文档主导 |

一个常见的配方错误：**训练用 packing，评测用 padding**，然后拿两者的 loss
直接比较。正确做法是评测也按 token 加权，或固定评测集的 padding 方式。

Packing 还会改变“一个 step 看到多少条文档”：同样 `global_batch × seq_len`
下，packing 后文档数可能多几倍。若数据混合按文档数配比，实际 token 配比会
偏移；这就是为什么 38.8 强调“混合权重按 token 而不是 step/文档”。

## 31.5 Sampler 与断点续训的配方含义

Sampler 的**契约与恢复字段**（DP 不重复、TP/PP/CP 相同、epoch 边界、RNG/
offset 保存）见 **38.8 节**。这里只保留两个影响配方的点：

- **有效 batch 会随并行度变化**：`global_batch` 固定时，DP 越大，每卡
  micro-batch 越小；若 sampler 按“每卡样本数”切分，改变 DP 会改变数据顺序。
- **恢复后的前若干步不可直接与中断前比较**：数据顺序、混合权重版本、
  tokenizer 版本任一变化，loss 都会跳；恢复演练要固定这些版本。

mini 按 step 派生随机索引，不保存精确的 epoch/offset；下面的设计 A/B 是
生产 sampler 的两种常见实现，供替换时参考。

### 31.5.1 一个可恢复 sampler 的设计

把“全局 step”映射到“样本索引”时，常见两种设计：

**设计 A：按 step 派生种子（mini 的做法）**

```text
seed_step = base_seed + step × 100003 + sample_offset
global_indices = randint(0, N, (micro_batch × accum × dp,), generator=seed_step)
rank_indices = global_indices[dp_rank × per_rank : (dp_rank + 1) × per_rank]
```

`sample_offset` 用来在**同一个 step** 内区分多个 batch（例如评估的多次采样用
`i × 101`）。注意 seed **不含 dp_rank**：所有 DP rank 采样同一个全局 batch，
再各取连续切片；这样并集不变量成立，DP 与单进程的 loss 可比。漏掉
`sample_offset` 会让同一 step 的重复采样得到同一批索引。

优点：无状态、恢复简单；缺点：不是真正的 epoch shuffle，样本可能重复/漏掉，
长期训练统计上不均匀。

**设计 B：预生成 epoch 排列 + 位置指针**

```text
perm = randperm(N, seed=base_seed + epoch)
rank_indices = perm[dp_rank::dp_size]
每个 step 消费 rank_indices[offset : offset + micro_batch]
offset 跨 checkpoint 保存
epoch 结束时 epoch += 1, perm 重新生成
```

优点：每个 epoch 覆盖全量、无重复；缺点：要保存 `epoch/offset`，且 `N` 不能
被 `dp_size` 整除时最后一段要处理。

无论哪种，都要明确：

- `drop_last`：最后一个不完整 batch 是否丢弃；
- 样本是否可重复（有放回/无放回）；
- packing 后“样本”是文档还是 packed 序列；
- 数据混合按 token 还是按样本；
- 恢复时 `step` 与 `offset` 的对应关系。

### 31.5.2 断点续训的验收

```text
[ ] 恢复后第一个 batch 与中断前最后一个 batch 不重复
[ ] 不跳过任何 shard（抽样对比）
[ ] epoch 边界正确
[ ] 混合权重的版本一致
[ ] loss 与中断前 10 步的均值可比
```

数据顺序问题通常不会立刻报错，只会让 loss 曲线“怪怪的”；把它纳入恢复演练
（附录 H）。

## 31.6 训练配方

一个 LLM 预训练配方通常包括：

| 项 | 典型范围 | 说明 |
|---|---|---|
| 模型 | 0.1B–数百 B | 结构/宽度/深度 |
| token 数 | 10×–100× 参数 | Chinchilla 之后常见 20×+ |
| batch | 百万–数千万 token | 随模型/学习率调整 |
| LR | 1e-4–3e-4（AdamW） | 与 batch/模型相关 |
| warmup | 0.1%–5% | 大模型更保守 |
| weight decay | 0.1 | 只对 2D 权重 |
| β2 | 0.95 | LLM 常用 |
| 序列长度 | 2K→32K→128K | 课程学习 |
| 精度 | bf16/FP8 | 逐步验证 |
| 并行 | TP/PP/DP/CP/EP | 按拓扑 |

这些不是固定公式。scaling law 能给出计算最优的 `N/D` 比例，但数据质量、架构、
训练稳定性会改变实际最优。

## 31.7 长上下文课程

从 4K 直接训 128K 通常不稳定/低效。常见课程：

```text
4K（主体预训练）→ 8K → 32K → 128K → 1M（可选）
```

每个阶段不是“把 seq_len 调大”，而是同时改数据、并行、位置编码和评测。

### 31.7.1 一个可执行的课程表

| 阶段 | 序列长度 | token 预算 | 位置编码 | 并行 | 数据 | 通过条件 |
|---|---|---:|---|---|---|---|
| 主体 | 4K–8K | 大部分（>90%） | 原始 RoPE base | TP/PP/DP | 通用网页/代码/书 | 短任务 loss/评测不退化 |
| 扩展 1 | 32K | 5%–10% | base 放大或 YaRN | +CP=2–4 | 长文档/论文/代码库 | 32K needle ≥ 基线 |
| 扩展 2 | 128K | 1%–3% | 继续缩放 | CP=4–8 | 书/仓库/多文档 | 128K 长文 QA 提升 |
| 极限 | 1M | <1% | 稀疏/混合注意力 | CP=8+ / ring | 超长文档/视频转写 | 1M 评测 + 短任务不崩 |

比例是经验值，不是定律；关键是**长序列数据占比要小**，否则会牺牲短任务
能力，且训练成本急剧上升。

### 31.7.2 每阶段要改什么

1. **位置编码**：RoPE base 放大（如 10k→500k）或 YaRN/NTK 缩放。不要在
   训练中途随意改；改之前存 checkpoint，改之后先跑短评测。
2. **并行**：CP 随序列增长；CP 的 all-gather/ring 通信要重新测。TP 通常不变，
   但长序列下激活显存会迫使 activation checkpointing。
3. **数据**：长文档要按文档边界 packing，避免跨文档 attention；同时保留
   短序列数据，防止灾难性遗忘。
4. **评测**：needle-in-a-haystack、长文 QA、代码库理解、长文摘要；同时跑
   短任务回归（MMLU/代码/数学），防止“长上下文能力换短任务能力”。

### 31.7.3 失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 切到 32K 后 loss spike | 位置编码/attention scale 不稳 | 降 LR、加 warmup、检查 RoPE |
| 长上下文评测不升 | 数据太短/跨文档 mask 错 | 检查 packing/document id |
| 短任务退化 | 长序列数据比例过高 | 降长序列比例、混短数据 |
| OOM 但参数没变 | 激活/attention 矩阵随 s² 增长 | activation checkpointing/CP/稀疏 |
| 1M 吞吐崩 | CP 通信/attention 二次项 | 稀疏注意力/ring/分层 CP |
| needle 过拟合 | 评测数据泄漏进训练 | 去污染、换模板/位置 |

### 31.7.4 回滚策略

长上下文课程必须可回滚：

- 每个阶段保留独立 checkpoint 和数据配置；
- 长序列阶段的学习率通常更低、warmup 更长；
- 如果短任务评测下降超过阈值，回滚到上一阶段并降低长序列比例；
- 不要把“1M 可用”作为单一成功指标；要同时看短任务、吞吐、显存。

Megatron 2026 roadmap（按 34.4 的 L3 分级：路线图信号，不是发布承诺）把
1M 上下文描述为课程 + 稀疏注意力 + CP 调度的组合，三者缺一不可；只改
`seq_len` 得到的是 OOM 或退化，不是长上下文能力。引用具体功能时按 36.1
的 L1–L4 标注发布状态，并按附录 J 给出证据等级。

## 31.8 评估与数据质量

训练 loss 低不等于能力强。需要：

- 领域评测（代码/数学/多语言/长文）；
- 去污染（benchmark 数据不能进训练集）；
- 数据质量分类器；
- 人工抽检；
- 训练中定期 eval（但 eval 也会过拟合）。

## 31.9 后训练与预训练配方的接口

SFT/LoRA/DPO/GRPO 的完整内容见第 39 章。从**预训练配方**的角度，只需记住
三件事，它们会决定后训练是否顺畅：

1. **特殊 token 要在预训练词表里**。chat template 的角色标记
   （`<|user|>`、`<|assistant|>`）如果在预训练后才加入，embedding 是随机
   初始化的，SFT 要额外学；更糟的是 tokenizer 版本变化会让旧 checkpoint
   的 token id 错位。
2. **基础模型的能力覆盖决定后训练上限**。后训练能激发/对齐能力，但不能
   凭空创造预训练没见过语言/代码/领域知识。数据混合要在预训练阶段就覆盖
   目标场景。
3. **checkpoint 要可追溯**。后训练团队需要知道基础模型的 tokenizer、数据
   版本、训练步数、并行配置；否则 SFT 的 loss/评测无法归因。

一个常见工程约定：

```text
base checkpoint + tokenizer hash + chat template + 数据版本
   → SFT/LoRA → 偏好优化 → 评测
```

后训练的 batch 小、序列长、训练/推理交替，系统瓶颈与预训练不同（rollout、
KV cache、adapter 管理）；这些在第 39 章展开。

## 31.10 mini 的数据

- `SyntheticTokenDataset`：可预测算术序列，便于观察 loss 下降；
- `BinTokenDataset`：uint16 memmap；
- `BatchProvider`：按 DP rank 采样，保证 TP/PP 副本一致；
- `PackedSequenceDataset`（新增）：文档边界、position reset、loss mask、
  block-diagonal mask 的教学实现；
- 不支持真实 tokenizer/blending/质量过滤。

它的目的是验证“数据切分/累积/恢复”的契约，不是训练一个有用模型。

## 31.11 本章小结

- 数据管线的吞吐/质量决定大规模训练的上限。
- 格式、索引、混合、packing、sampler、断点续训要一起设计。
- 训练配方没有万能值；scaling law 只是起点。
- 长上下文需要课程和并行/注意力协同。
- mini 只保留分布式数据契约。

下一章讨论集群、故障恢复与运维。
