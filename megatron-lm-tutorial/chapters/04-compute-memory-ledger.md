# 第 04 章 计算与显存账本

## 4.1 为什么先算账

> 阅读提示：本章的公式会引用 DP/TP/PP/CP/EP、ZeRO、MLA 等概念。第一次
> 读可以只看 4.2–4.4 与 4.6 的结论，把 4.5/4.7 当作查表；第 5–10 章会
> 逐个展开。

并行策略不是拍脑袋选的。给定模型、序列长度、卡数和显存，应该先估算四件事：

1. 参数、梯度、优化器状态需要多少显存；
2. 激活和注意力矩阵需要多少显存；
3. 每步需要多少计算量，理论最短时间是多少；
4. 每种并行方案引入多少通信量。

这一章给出**够用的一阶模型**。忽略 kernel 细节和碎片，但足以做方案筛选。

## 4.2 参数量

对 decoder-only Transformer，设 `h=hidden`，`L=层数`，`V=词表`，`s=序列长度`。

**标准 MHA + FFN=4h 的 GPT**，每层参数约：

- QKV 投影：`3h²`（GQA 时 K/V 更少）
- 输出投影：`h²`
- MLP：`8h²`
- 合计约 `12h²`（不含 bias/norm）

再加上词表嵌入 `Vh`（若权重绑定则只算一次）和位置编码。总参数量：

```text
N ≈ 12 L h² + Vh + 4hL + 2h（norm 等小项）
```

每个 pre-norm block 有两个 LayerNorm，每个有 weight+bias 共 `2h` 个参数，
所以是 `4hL`；最后一个 final norm 再算 `2h`。很多文章漏掉这个因子，但在
`h` 很大、`L` 很多时它仍只是千分之一量级，不影响数量级判断。

**SwiGLU** 把 MLP 从 2 个矩阵变成 3 个矩阵（gate/up/down），若 FFN 维度仍为
`4h`，则 MLP 约 `12h²`，每层约 `16h²`。这就是为什么 LLaMA 类模型在相同
hidden/层数下参数更多。

**GQA/MQA** 只改变 K/V 的投影维度：若 query heads 为 `n_h`，KV heads 为
`n_kv`，head_dim 为 `d`，则 K/V 投影为 `2·n_kv·d·h`，而不是 `2h²`。

例子：

| 模型 | h | L | 标准 GPT 参数（约） |
|---|---:|---:|---:|
| 125M | 768 | 12 | 0.085B（12Lh²） |
| 1.3B | 2048 | 24 | 1.2B |
| 7B | 4096 | 32 | 6.4B |
| 70B | 8192 | 80 | 64B |

实际值会因词表、GQA、bias、tied embedding 而不同；上表只用于数量级判断。

## 4.3 训练显存

### 4.3.1 状态显存

混合精度 + Adam 的每参数字节数：

| 项 | 字节 |
|---|---:|
| bf16 权重 | 2 |
| bf16 梯度 | 2 |
| fp32 master weight | 4 |
| Adam m | 4 |
| Adam v | 4 |
| **合计** | **16** |

如果使用 fp32 梯度或额外 copy，会更高；如果使用 SGD 或 Adafactor，会低一些。
ZeRO-1 把优化器状态按 DP 切分，ZeRO-2 再切梯度，ZeRO-3/FSDP 再切参数。
Megatron 的 distributed optimizer 类似 ZeRO-1/2 的组合，用 reduce-scatter /
all-gather 换取显存。

70B 模型：

```text
16 B/param × 70e9 ≈ 1.12 TB
```

80 GB × 8 = 640 GB，仍不够。**ZeRO/分布式优化器只切优化器状态，权重和
梯度仍然复制**：即使 DP=8 + ZeRO-1，每卡仍要放 `4 × 70 = 280 GB` 的
bf16 权重+梯度，再加 `12 × 70 / 8 = 105 GB` 状态，约 385 GB/卡（见
11.7）。所以必须先 TP/PP 切权重/梯度，再叠加 ZeRO；单靠“把状态切到更多
卡”解决不了 70B 的 80 GB 显存问题。8-bit Adam/Muon 只能降低状态项，
同样不能替代 TP/PP。

### 4.3.2 激活显存

粗略地，每层需要保存的激活包括：

- QKV 投影输出；
- attention 输出；
- MLP 中间激活（SwiGLU 的 gate/up）；
- dropout mask、残差；
- 反向需要的 LayerNorm 输入等。

不要背“每 token 每层几十 KB”这种数字：它强烈依赖 `h`、`s`、head 数和是否
重算。Megatron 论文给出的量级是每层

```text
激活 ≈ s · b · h · (34 + 5 · n_h · s / h)   字节
```

其中 `n_h` 是 attention head 数，`34` 是逐元素中间量的经验常数，`5·n_h·s/h`
来自 `s×s` 的 attention 中间量。代入 `h=4096, s=4096, n_h=32, b=1`，每层约
`0.79 MB/token`（乘 `s` 后是每层约 3.2 GB），远大于“几十 KB”；而
`h=64, s=32, n_h=4` 的 tiny 模型每层约 `64×(34+5×4×32/64) ≈ 2816 B`，即约
2.8 KB/token。**先代公式，再实测**。

工程上更可靠的做法是：

1. 用 `torch.cuda.max_memory_allocated()` 实测一个 micro-batch；
2. 用 activation checkpointing 把“保存激活”换成“重算”；
3. 用序列并行/上下文并行把序列维切小；
4. 用 TP 把每层宽度切小（但 LayerNorm/残差等复制激活不一定变小）。

**激活重计算**（gradient checkpointing）通常用约 30% 额外前向计算，换掉大部分
激活存储。它是长序列/大 batch 的第一手段，但会改变训练速度，需重新测吞吐。
重算的粒度（整层/attention/MLP）和是否重算 attention 的 `s×s` 中间量，决定
了节省与额外计算的比值。

### 4.3.3 注意力矩阵

显式 attention 的 score 矩阵大小是 `b · n_h · s²`。bf16 下：

```text
score 显存 ≈ 2 · b · n_h · s² 字节
```

`s=8192, b=1, n_h=32` 时约 4.3 GB（单层单次），而且 softmax 还需要临时张量。
这就是 FlashAttention 的动机：不物化 `s×s` 矩阵，用分块 online softmax 计算。
mini 为了展示 mask/CP，故意物化；生产必须用 fused attention。

### 4.3.4 KV cache（推理）

自回归推理时缓存历史 K/V：

```text
KV bytes = 2 · L · n_kv · d · s · b · bytes_per_element
```

GQA 的 `n_kv` 远小于 `n_h`，这是 LLaMA 类模型推理省显存的关键；DeepSeek-V2/V3
走的是另一条路——MLA 不缓存完整 K/V，而是缓存低秩 latent `c_KV` 和一个共享
RoPE key，`KV bytes/token ≈ d_c + d_R`（见 30.5）。两者都把 KV cache 从
`O(n_h·d)` 降下来，但压缩方式和推理实现不同。

长上下文推理的瓶颈往往不是权重，而是 KV cache；chunked prefill、prefix caching、
KV 量化、PagedAttention 都围绕它。

## 4.4 计算量

### 4.4.1 训练 FLOPs

对参数量 `N`、训练 token 数 `D`：

```text
训练 FLOPs ≈ 6 N D
```

其中前向约 `2ND`，反向约 `4ND`。注意力部分每 token 每层约 `4·n_h·d·s`（QK 与 AV 各 `2·d·s`，因果掩码可近似
减半但 kernel 通常按上界计）。因此更实用的写法是按 token：

```text
FLOPs/token ≈ 6N + 4 · L · n_h · d · s
```

当 `s` 较小（≤2K）时，`6ND` 足够；
当 `s` 到 32K/128K 时，注意力的 `O(s²)` 项不可忽略，需要单独算。

### 4.4.2 吞吐与 MFU

设 GPU 峰值算力为 `P`（bf16 dense），实际吞吐为 `R` token/s，则：

```text
MFU = (6 N R) / (P · GPU数)
```

MFU 是“模型 FLOPs 利用率”，不包含通信、数据加载、优化器。厂商常报告 40%–50%
的 MFU（H100、bf16、特定模型/并行配置）；实际项目受限于网络、故障、数据管线，
端到端可能低不少。看到“XX% MFU”要问：模型多大、序列多长、精度、是否含通信、
是否用稀疏/FP8。

### 4.4.3 一个 7B 模型的例子

`N=7e9`，`D=1e12` token：

```text
训练 FLOPs ≈ 6 × 7e9 × 1e12 = 4.2e22
```

H100 SXM 的 bf16 **dense** 峰值约 `989 TFLOP/s`（带稀疏性约 `1979 TFLOP/s`，
做 MFU 估算只能用 dense）。8 卡约 `7.91 PFLOP/s`；若 MFU=40%，有效
`3.16 PFLOP/s`，需要：

```text
4.2e22 / 3.16e15 ≈ 1.33e7 秒 ≈ 154 天
```

这就是为什么 7B 模型通常用几十到几百张卡训练，而不是 8 张。实际还要加通信、
重算、故障恢复开销。

> 容易混淆的数字：`495 TFLOP/s` 是 H100 的 TF32 Tensor Core dense 峰值，
> 不是 BF16。BF16/FP16 dense 约 `989 TFLOP/s`，FP8 dense 约 `1979 TFLOP/s`。
> 报 MFU 时必须同时写清精度和 dense/sparse 口径。

### 4.4.4 MoE 与 MLA 的账本差异

**MoE**：总参数和激活参数要分开。

```text
N_total  ≈ N_dense + E · N_expert
N_active ≈ N_dense + k · N_expert          # 每 token 只激活 k 个专家
FLOPs    ≈ 6 · N_active · D
state    ≈ 4 · N_total / (tp · pp)          # bf16 权重 + 梯度（DP 复制）
         + 12 · N_total / (tp · pp · d_z)  # fp32 master + Adam m/v
```

以 DeepSeek-V3 为例，总参数 671B、激活 37B：训练 FLOPs 用 37B 算，显存/通信
要按 671B 的专家状态算。把两者混用，会同时高估算力、低估显存。

**MLA**：参数量与 MHA 同量级（甚至多一个低秩投影），但推理 KV 完全不同：

```text
MHA KV/token/layer = 2 · n_h · d_h · bytes
MLA KV/token/layer = (d_c + d_R) · bytes
```

训练 FLOPs 仍可用 `6ND` + attention 项；推理显存要按 MLA 的
`d_c+d_R` 算。注意 `k_R` 是共享单 head，不是 `n_h·d_R`。这就是为什么
“参数量差不多”的两个模型，推理并发能力可能差一个数量级。

## 4.5 通信量的一阶估算

### 4.5.0 集合通信原语的字节账

先约定：`N` 是**全局张量元素数**，`bpe` 是每元素字节数，`d` 是组大小，
`B` 是单向有效带宽（字节/秒），`α` 是单次消息延迟。ring 实现的每卡发送量：

| 原语 | 每卡发送元素数 | 说明 |
|---|---:|---|
| all-reduce | `2N(d-1)/d` | reduce-scatter + all-gather 两阶段 |
| reduce-scatter | `N(d-1)/d` | 只有第一阶段 |
| all-gather | `N(d-1)/d` | 只有第二阶段 |
| all-to-all | `N(d-1)/d` | 每卡有 `N` 个元素要分发 |
| broadcast | 根发送 `N`，其余接收 `N` | 总线流量 `N` |

> 单位提醒：上表除 all-to-all 外，`N` 都是**全局**元素数；all-to-all 行的
> `N` 是**每卡输入**元素数（每卡把 `N` 个元素分给 `d` 个目的地，发送
> `N(d-1)/d`）。若按全局 `N_global` 定义，则每卡发送
> `N_global(d-1)/d²`。两种写法差一个 `d`，混用会把 EP 通信量高估 `d` 倍。

因此 bf16 下 all-reduce 每卡约 `4N(d-1)/d` 字节，reduce-scatter/all-gather
各约 `2N(d-1)/d` 字节；**reduce-scatter + all-gather 才等于 all-reduce**。
这解释了 DP 公式里的因子 2。注意：SP 的 Row 前向用 reduce-scatter 替代
all-reduce，确实把**这一步**减半；但 Column 前向还多了一次 all-gather，
前向总通信与普通 TP 同一量级（见 7.6），不能概括为“SP 省一半通信”。

时间用 α-β 模型粗估：

```text
T ≈ α · steps + bytes / B
steps(ring all-reduce) ≈ 2(d-1)
steps(ring reduce-scatter/all-gather) ≈ d-1
```

例：张量字节数 `1 GB, d=8, B=300 GB/s, α=5 μs`，bf16 all-reduce 每卡
`2 × 1 GB × 7/8 = 1.75 GB`，传输约 5.8 ms，延迟约 `2×7×5μs=70 μs`；
大消息是带宽受限，小消息（如每层 norm 的几百 KB）则可能被 `α` 主导。这也
是为什么“把很多小 all-reduce 合并/重叠”比单纯提高带宽更有效。

注意 `algbw = N·bpe/T` 与 `busbw = algbw × 2(d-1)/d`（all-reduce）口径不同；
比较不同并行度时用 busbw，报“链路带宽”时要说清是单向还是双向。

### 4.5.1 数据并行

设参数量为 `N`、每元素 `bpe` 字节（bf16 为 2）。每步梯度 all-reduce 的
“发送+接收”总量约为 `2·N·bpe` 字节；使用 ring all-reduce 时每卡约

```text
2 · N · bpe · (d-1)/d   字节
```

bf16 下即 `4N(d-1)/d` 字节。梯度压缩/分桶/异步可以减少或隐藏，但改变收敛
需谨慎。把 `N` 当成“字节数”还是“参数个数”是这类公式最常见的差 2–4 倍
错误。

### 4.5.2 张量并行

- Column Parallel：前向若需要完整输出，要 all-gather；Megatron 通常让下游
  Row Parallel 消费分片，避免显式 gather。
- Row Parallel：前向 all-reduce 一次。每卡发送量约
  `2·b·s·h·bpe·(t-1)/t` 字节（bf16 即 `4bsh(t-1)/t`）；`t` 很大时趋近
  `4bsh`，不再随 TP 增大而下降。
- 序列并行：Column 前向 all-gather、反向 reduce-scatter；Row 前向
  reduce-scatter、反向 all-gather。合起来前向是 `AG + RS`、反向是
  `RS + AG`，总通信与普通 TP 的两次 all-reduce 同一量级，**不是无条件
  减半**；但激活显存从 `b·s·h` 降到 `b·s·h/t`（只对真正被序列切分的
  激活成立）。mini 为了可读性把 `RS` 写成 `all_reduce + chunk`，语义等价、
  通信量更大（7.3）。
- 反向：Column 需要 grad_input all-reduce；Row 的 grad_input 本地即可。

**一个常见 bug**：只做 Row 的 forward all-reduce，忘了 Column 的 backward
grad_input all-reduce。loss 前向可能完全正确，但梯度少了一半贡献，训练会“能跑
但不对”。本教程的 `_ColumnParallelLinearFn` 专门处理这一点，并在测试中对比梯度。

### 4.5.3 流水线并行

每个 micro-batch 在相邻 stage 间传一次激活，反向传一次梯度。设每步有 `m`
个 micro-batch，则每步总通信量约

```text
2 · m · b · s · h · bpe · (p-1)   字节
```

（`p-1` 个 stage 边界；每个 micro-batch 每个边界前向传激活、反向传梯度）。
它可以和下一个小计算重叠。气泡比例（1F1B，无 interleaving）：

```text
bubble ≈ (p - 1) / (m + p - 1)
```

`m` 是 micro-batch 数。增大 `m` 或使用 interleaved/VPP 可以降低气泡，但增加
激活内存和调度复杂度。

### 4.5.4 上下文并行

CP 把序列切到多卡。理想 ring attention 只传 KV 的块，通信量随 `s` 线性增长；
mini 的 all-gather KV 会把每层完整 KV 复制到所有 CP rank，内存/带宽都不优，
仅用于正确性教学。生产 CP 需要专门的 ring/load-balanced 调度。

### 4.5.5 专家并行

EP 的通信是**不规则**的：每个 token 被路由到 k 个专家，只有跨 rank 的部分
需要 all-to-all。一阶估算：

```text
dispatch ≈ tokens · k · (2h + 4) · (ep-1)/ep    # bf16 hidden + fp32 weight
combine  ≈ tokens · k · 2h · (ep-1)/ep
```

以全局 `tokens=1M, k=2, h=7168, ep=8`、且 `ep=dp`（整个全局 batch 在该
EP 组内）为例，一次 MoE 层往返的**聚合**字节约 50 GB，但每 rank 只发/收
约 6.3 GB；算时间必须用后者（见 10.6）。若 `dp>ep`，每个 EP 组只处理
`ep/dp` 的全局 token，per-rank 字节按此缩放。
它不像 DP 的 all-reduce 那样与参数量成正比，而是与 **token 数 × 专家数** 成
正比；长上下文/大 batch 下会迅速成为瓶颈。评估 EP 时除了平均带宽，还要看
最热专家的尾延迟。

## 4.6 用一张表选择并行维度

| 瓶颈 | 首选 | 次选 | 注意 |
|---|---|---|---|
| 参数/优化器状态放不下 | ZeRO/FSDP、DP | PP | 通信与重分片 |
| 单层 GEMM 太大/激活太高 | TP | PP | TP 不超过 NVLink 域 |
| 层数多、跨节点 | PP | DP | 气泡、p2p 延迟 |
| 序列太长、attention 爆显存 | CP、FlashAttention | 激活重算 | mask、loss 归一化 |
| MoE 专家太多 | EP | TP/PP | all-to-all、负载均衡 |
| 吞吐低但显存够 | 增大 micro-batch、优化 kernel | 减少并行度 | 先测 MFU/通信占比 |

## 4.7 mini 中的对应实现

- 参数分片：`ColumnParallelLinear` / `RowParallelLinear` / `VocabParallelEmbedding`；
- 激活分片：`sequence_parallel` 与 `scatter/gather`；
- 重算：`ModelConfig.activation_checkpointing`，在 `TransformerBlock.forward` 中
  调用 `torch.utils.checkpoint`；
- 状态分片：`ZeRO1Optimizer`；
- 序列切分：`context_parallel_size` 与 `CoreAttention` 的 CP 分支；
- 专家切分：`expert_model_parallel_size` 与 `ExpertParallelMoELayer` 的
  all-to-all dispatch/combine（见 25.6–25.8）；
- 流水线扩展：`pipeline/interleaved_exec.py` 的 VPP 执行器；
- 计算/显存测量：`benchmark` 命令与 `tokens_per_sec`；
- 一阶代价模型：`analysis.py` 与 `cli estimate`（见 4.8）。

这些实现都不追求极致性能，但保留正确的“账本关系”。当你把 mini 的 hidden 从
64 改到 4096、层数从 2 改到 32，就能直观看到显存/时间如何变化。

## 4.8 把账本变成命令

公式容易抄错。mini 把这一章的模型实现为 `mini_megatron/analysis.py`，并提供 CLI：

```bash
python -m mini_megatron.cli estimate \
  --config configs/tiny.json --tokens 1e12 --gpu h100 --mfu 0.4
```

输出包括：

- 总参数/激活参数（MoE 两者不同）；
- 每卡状态显存（TP/PP/ZeRO 后）；
- 单 micro-batch 激活、in-flight 峰值、单层 score 矩阵、KV cache；
- 训练 FLOPs、GPU-hours；
- DP/TP/PP/CP 每步通信量。

```text
parameters: 98.94K total, 98.94K active
state / rank: 1.51 MB
activation / rank: 352.00 KB per micro-batch
activation peak / rank: 352.00 KB (1 in flight)
attention scores (one layer): 16.00 KB
KV cache: 32.00 KB
estimated peak / rank: 1.85 MB (device 80.00 GB)
```

> 单位说明：估算器内部用 1024 进制，打印的 `KB/MB/GB` 实际是 KiB/MiB/GiB。
> 与厂商标称的十进制 GB（10^9 字节）比较时会差约 7%；做采购/显存预算时
> 应统一口径。

估算器刻意把假设暴露成参数/注释：

- 激活用 Megatron 论文的每层 `s·b·h·(34 + 5·n_h·s/h)`（Megatron 论文用
  `a` 表示 head 数，本书统一为 `n_h`）字节量级，再乘层数 `L`（注意不要漏
  `L`）；峰值还要乘 in-flight micro-batch 数（本实现的 1F1B≈`max(1, min(m, pp-1))`，
  GPipe=`m`）；
- MoE 的 FLOPs 用激活参数，显存用总参数；
- 通信量是每 optimizer step 的一阶和，不含重叠；
- 硬件峰值必须按精度/dense/sparse 口径选择。

它不能替代 profiling，但能在开跑前拦掉“明显放不下/算不完”的方案。

## 4.9 本章小结

- 参数量只是起点；训练显存约 16 B/param，激活和 attention 另算。
- 训练 FLOPs 约 `6ND`，长序列要加注意力项；MFU 要问口径。
- 每种并行都有通信代价；TP 的 backward all-reduce 最容易被漏掉。
- 选并行方案先看瓶颈：状态、激活、序列、层数还是专家数。

下一章从数据并行开始，进入并行维度的逐个拆解。
