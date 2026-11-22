# 第 40 章 推理系统：KV cache、连续批处理与 PD 分离

## 40.1 推理的指标与训练完全不同

训练看 tokens/s、MFU、GPU-hours；推理看的是：

| 指标 | 含义 | 影响 |
|---|---|---|
| TTFT | Time To First Token | 首字延迟，prefill 主导 |
| TPOT / ITL | Time Per Output Token / Inter-Token Latency | 解码速度 |
| throughput | tokens/s（全局/每 GPU） | 成本 |
| goodput | 满足 SLO 的有效吞吐 | 真实业务指标 |
| p50/p99 | 分位延迟 | 用户体验/SLA |
| 并发数 | 同时在途请求 | 显存/吞吐 |

一个常见错误：只看平均 tokens/s。线上 SLO 通常由 p99 决定；一个长尾请求可能拖垮整批。

## 40.2 KV cache 的账本

自回归解码时，每层缓存历史 K/V：

```text
KV bytes = 2 × L × n_kv × d × s × b × bytes_per_element
```

其中：

- `2`：K 和 V；
- `L`：层数；
- `n_kv`：KV head 数（GQA 远小于 Q head）；
- `d`：head_dim；
- `s`：当前序列长度；
- `b`：batch/并发序列数。

例子：`L=80, n_kv=8, d=128, s=8192, b=32, bf16`：

```text
2 × 80 × 8 × 128 × 8192 × 32 × 2 ≈ 85.9 GB ≈ 80 GiB
```

这还没算权重/激活/临时 buffer。长上下文推理的显存瓶颈通常不是模型权重，而是 KV cache。

### 40.2.1 GQA/MLA 的收益

- MHA：`n_kv = n_h`，KV cache 最大；
- GQA：`n_kv = n_h / group_size`，KV cache 降为 `1/group_size`；
- MQA：`n_kv = 1`，最省但质量可能下降；
- MLA：把 K/V 压缩到低秩 latent，进一步降 cache，但增加投影计算。

### 40.2.2 KV cache 的并行切分

- TP：按 head 切，每个 rank 只存本地 head 的 K/V；
- PP：按层切，每个 stage 存自己层的 K/V；
- CP：按序列切，需要 ring/all-gather；
- EP：MoE 的专家不改变 KV cache，但可能影响 batch/路由。

## 40.3 PagedAttention 与显存管理

朴素 KV cache 为每个请求预分配 `max_seq_len`：

- 短请求浪费大量显存；
- 长请求可能 OOM；
- 碎片导致利用率低。

PagedAttention 把 KV cache 分成固定大小的 block（如 16 tokens），用页表映射逻辑序列到物理 block：

```text
逻辑 block 0 → 物理 block 7
逻辑 block 1 → 物理 block 2
...
```

优点：

- 按需分配，减少浪费；
- 支持 prefix sharing（多个请求共享相同前缀的物理 block）；
- 支持 beam search/并行采样共享 KV；
- 便于抢占/换出。

代价：

- 间接寻址/kernel 复杂；
- block 大小影响碎片/吞吐；
- 与 TP/PP 的布局要匹配。

### 40.3.1 一个可运行的 paged cache

`mini_megatron/inference/paged_cache.py` 实现了 block 分配、append、gather 和
free 的完整语义：

```python
cache = PagedKVCache(
    num_layers=80, num_kv_heads=8, head_dim=128,
    block_size=16, num_blocks=4096,
)
seq = cache.create_sequence()
# k/v: [1, heads, tokens, dim]
cache.append(seq, layer, k, v)     # 按需分配 block
k_all = cache.gather_k(seq, layer) # [heads, tokens, dim]
cache.free_sequence(seq)           # block 回到共享池
```

关键不变量（测试 `tests/unit/test_paged_cache.py` 覆盖）：

- **逻辑位置到物理 block 的映射**：`pos // block_size` 选 block，
  `pos % block_size` 选槽位；
- **跨 block 边界**的 gather 与 dense cache 逐元素相等；
- **部分块**（最后一个 block 未填满）只返回有效 token；
- **层锁步**：同一 token chunk 必须写完所有层，长度才对读者可见；
- **释放后可复用**：`free_sequence` 把 block 还回 free list，不泄漏；
- **耗尽即报错**：block 不够时抛 `MemoryError`，不越界写。

碎片可以量化：

```text
fragmentation = 1 - 已用 token 数 / (已分配 block 数 × block_size)
```

例如 block_size=16、一个 33-token 请求：需要 3 个 block（48 槽），
碎片率 `1 - 33/48 ≈ 31%`。block_size 越小，尾部碎片越少，但页表/kernel
的间接寻址开销越大；16/32/64 是常见折中。

与 PagedAttention kernel 的关系：本模块只提供**数据结构和布局**。生产
kernel 要在一次 attention 计算中按 block table 读取 K/V、处理 partial
block、并与 GQA/MLA 的 head 布局配合；数值契约仍然是“gather 出来的
K/V 与 dense 一致”。

**连续批处理**（40.4）在此基础上做调度：请求结束时释放 block，新请求可以
立即复用；prefill 与 decode 可以混批；prefix cache 则让多个请求的相同前缀
指向同一批物理 block（需要引用计数/写时复制）。这些是 paged cache 之上的
策略层，不是数据结构本身。

## 40.4 连续批处理与 chunked prefill

### 40.4.1 Static batching

一次固定 batch，所有请求一起 prefill/decode，等最慢的结束。短请求浪费。

### 40.4.2 Continuous batching

每个 decode step 动态加入新请求/移除完成请求：

```text
step 1: [A, B, C]
step 2: [A, B, C, D]   # D 新加入
step 3: [A, C, D]      # B 完成
```

优点：GPU 利用率高、吞吐大；缺点：调度/显存管理复杂。

### 40.4.3 Chunked prefill

长 prompt 的 prefill 会占满 GPU、阻塞 decode。把它切成块：

```text
prefill chunk 1 → decode 其他请求 → prefill chunk 2 → ...
```

好处：

- 降低 TTFT 的长尾；
- 让 decode 请求不被长 prefill 饿死；
- 更平滑的显存峰值。

代价：

- 调度复杂；
- chunk 边界要处理 attention mask/位置；
- 可能增加总计算量。

### 40.4.4 Prefix caching

多个请求共享 system prompt/长文档前缀：

- 只算一次前缀的 KV；
- 后续请求复用；
- 需要 hash/引用计数/淘汰策略。

对 RAG/多轮对话/批量评测收益很大。

### 40.4.5 流式推理与 attention sink

无限流（客服会话、持续监控、长对话）不能把 KV cache 无限增长。**StreamingLLM**
的做法是只保留两类 token 的 KV：

```text
[Sink 个初始 token] + [最近 W 个 token]     # 中间 token 的 KV 被淘汰
```

为什么保留开头几个 token：attention 存在 **sink 现象**——大量 query 的
attention 质量会集中到序列最前面的少数 token（即使它们语义上不重要）。
如果把它们也淘汰，softmax 的归一化基准被破坏，输出会突然退化。实践上通常
保留 4 个左右的 sink token，再加一个滑动窗口（如 1K–4K）。

工程要点：

- **淘汰粒度与分页对齐**：按 block 淘汰，保留 sink block + 最近 block；
- **位置编码**：逻辑位置继续增长（绝对 RoPE）还是滚动（相对窗口）要明确；
  训练/推理不一致会直接影响质量；
- **prefix cache 交互**：sink/窗口的 KV 仍可复用，但引用计数和 block 回收
  要覆盖“被淘汰但仍被其他请求共享”的情况；
- **不是长上下文能力的替代品**：它只保证流式场景的显存有界，不恢复被淘汰
  区间的检索能力；需要长程记忆时，仍要检索、摘要或训练长上下文模型。

验收时不要只看“能一直生成”：要测淘汰边界前后的 perplexity/答案一致性、
sink 数/窗口大小的敏感性，以及流式吞吐与显存是否真的恒定。

## 40.5 投机解码

### 40.5.1 基本思想

用小模型/草稿头快速生成 `k` 个候选 token，大模型一次前向验证：

```text
draft:   y1 y2 y3 y4
target:  验证 y1..y4，接受前缀，拒绝处重新采样
```

### 40.5.2 接受率与加速

设接受率为 `α`，草稿成本为 `c`（相对大模型），理论加速：

```text
speedup ≈ (1 - α^(k+1)) / ((1 - α)(1 + k·c))
```

直觉：

- `α` 越高、草稿越便宜，加速越大；
- `k` 太大会浪费验证计算；
- 分布必须匹配（否则接受率低/质量下降）。

### 40.5.3 变体

- **Medusa**：多个解码头；
- **EAGLE**：特征级草稿；
- **lookahead**：n-gram/检索草稿；
- **speculative sampling**：保证与目标分布一致（拒绝采样）。

工程问题：

- 草稿模型/头的显存；
- 验证 batch 的构造；
- 与连续批处理/PD 分离的结合；
- 接受率监控。

### 40.5.4 投机解码的训练侧：Medusa、EAGLE 与 MTP

投机解码不只是推理技巧，它需要**训练一个草稿头/草稿模型**。

**Medusa**：在目标模型最后一层后接 K 个解码头，第 i 个头预测 `t+i` 个 token：

```text
loss = Σ_i CE(head_i(h_t), token_{t+i})
```

推理时用 tree attention 一次验证多条候选路径。训练时目标模型可以冻结，只训
头；也可以联合微调。

**EAGLE**：草稿模型不直接看 token，而是看目标模型的 hidden state + 下一个
token，预测“下一个 hidden state”，再解码成 token。它需要目标模型的中间层
输出作为训练数据，训练/推理的 hidden 分布必须一致。

**MTP（Multi-Token Prediction）**：DeepSeek-V3 在每个训练位置加辅助头，预测
未来 2 个 token。它既是训练目标（提高数据效率），也可以在推理时当草稿头。
MTP 的 loss 通常加权小于主 next-token loss，避免干扰主任务。结构、token
偏移、PP/CP 放置与 loss 账见 34.3.2；Megatron Core 的实现在
`core/transformer/multi_token_prediction.py`。

训练侧的坑：

| 问题 | 后果 | 处理 |
|---|---|---|
| 草稿头在目标分布外训练 | 接受率低 | 用目标模型生成的数据训练 |
| 训练/推理的 chat template 不一致 | 草稿分布错位 | 固定 template/tokenizer |
| tree attention mask 错 | 验证结果与目标不一致 | 单元测试 mask/位置 |
| 草稿头过拟合训练集 | 域外接受率崩 | 混合领域数据、监控 α |
| MTP 权重太大 | 主任务 loss 变差 | 小权重 + 消融 |

投机解码的收益完全取决于**接受率 α**。α 在目标领域下降时，加速会消失甚至
变负；所以生产系统要按领域/请求类型监控 α，并支持动态关闭。

## 40.6 Prefill/Decode 分离（PD 分离）

Prefill 是计算受限（大矩阵），decode 是带宽受限（GEMV + KV 读写）。把两者放在同一组 GPU 会互相干扰。

PD 分离：

```text
Prefill 集群 → 传输 KV → Decode 集群
```

优点：

- 各自选择最优并行/硬件；
- 隔离长 prompt 对 decode 的影响；
- 独立扩缩容。

挑战：

- KV cache 传输（NIXL/RDMA/NVLink）；
- 调度/路由；
- 故障恢复；
- 一致性/安全。

Megatron Core 0.20 的推理更新包含 prefill-decode 分离、KV 传输、chunked prefill、prefix caching、speculative decoding 等，说明训练/推理栈在融合。

### 40.6.1 KV 传输的算术

PD 分离的核心成本是“把 prefill 产生的 KV 送到 decode 节点”。以
LLaMA-70B 类模型（80 层、8 个 KV head、head_dim 128、bf16）为例：

```text
每 token 每层 KV = 2 · n_kv · d · bytes = 2 · 8 · 128 · 2 = 4 KB
每 token 全模型   = 4 KB × 80 = 320 KB
32K token 请求    = 320 KB × 32768 ≈ 10.5 GB
```

一个 32K 请求要搬约 10 GB；如果 decode 集群有 100 个并发请求在流动，
传输量就是 TB 级。这决定了 PD 分离是否划算：

```text
传输时间 = KV bytes / 有效带宽
若 传输时间 > prefill 节省的时间，PD 分离就是负收益
```

因此 PD 分离适合：长 prompt、KV 可压缩（MLA/GQA/量化）、prefill 与 decode
比例稳定、网络有 RDMA/NVLink 且能重叠。短 prompt/小模型往往不值得。

### 40.6.2 调度器与 KV 管理

PD 分离不是“两个集群 + 一根网线”，还需要：

| 组件 | 职责 | 代表系统/思路 |
|---|---|---|
| 全局调度 | 决定请求去哪个 prefill/decode 实例 | DistServe、Splitwise 的 goodput 优化 |
| KV 传输 | RDMA/NVLink、分块/压缩、失败重传 | NIXL、Mooncake 的 KV store |
| KV 池化 | 把 KV 当分层缓存（GPU/CPU/SSD） | Mooncake、LMCache 类方案 |
| 前缀复用 | 相同 system prompt/文档的 KV 复用 | prefix cache + 路由亲和 |
| 弹性 | prefill/decode 独立扩缩容 | 需要请求重路由/状态迁移 |

调度目标不是“最大吞吐”，而是 **SLO 下的 goodput**：满足 TTFT/TPOT 的
请求比例。一个常见错误是用总 tokens/s 评估 PD 分离，结果 decode 的 TPOT
尾延迟已经超标。

## 40.7 推理并行策略

| 维度 | 作用 | 注意 |
|---|---|---|
| TP | 切 heads/KV/权重 | 延迟敏感，留 NVLink |
| PP | 切层 | 每 token 走完整 pipeline，气泡大 |
| EP | 切专家 | MoE 推理必需，all-to-all |
| DP | 多副本 | 吞吐扩展 |
| CP | 长序列 | KV 切分/通信 |

推理 TP 的特点：

- 每步都有 all-reduce，延迟敏感；
- KV cache 按 head 切，显存下降；
- 采样要在 rank 间同步（相同 token）；
- 通常 1–8 卡 TP，跨节点用 PP/多副本。

### 40.7.1 MoE 推理：专家张量并行与 batch invariance

MoE 推理比训练更苛刻，因为 decode 的 batch 小、延迟预算紧：

- **专家张量并行（ETP）**：每个专家的 GEMM 再按 TP 切，减少单卡显存；
  但 all-to-all 与 all-reduce 叠加，延迟更高。
- **all-to-all 的尾延迟**：decode 每步都要 dispatch/combine，热门专家的
  排队会直接变成 TPOT 抖动。
- **capacity/padding**：推理通常 dropless，用动态 buffer；padding 到固定
  容量会浪费显存/带宽。
- **热门专家复制**：按请求分布复制少数专家，减少单点排队；需要路由表
  支持“同一专家多个副本”。
- **batch invariance**：同一请求在不同 batch 组成下必须得到相同输出。
  如果路由/容量依赖 batch 统计，输出会变，导致评测、缓存、投机解码不一致。
  生产实现会固定路由或使用 batch-invariant kernel。
- **KV cache**：MoE 只替换 FFN，attention 的 KV cache 仍按层/头管理；
  EP 不影响 KV 的切分，但 PP 会把层分到不同 stage。

vLLM 的 fused MoE、TensorRT-LLM 的 expert parallel、Megatron Core inference
的 batch-invariant MoE 都在解决同一个问题：**小 batch 下，专家 GEMM 的
kernel 启动和 all-to-all 延迟比 FLOPs 更贵**。

容量规划时，MoE 推理要分别估算：

```text
权重显存 = 总专家参数 × bytes / (TP × EP)
KV 显存  = 2 · L · n_kv · d · bytes · tokens / TP
激活/临时 = dispatch buffer + grouped GEMM workspace
```

三者中任何一个先撞墙，都会限制并发；不要只看权重。

### 40.7.2 Wide EP、专家复制与 EPLB

当专家数到数百（DeepSeek 风格）时，EP 可能跨越多个节点，`ep` 远大于单机
GPU 数，这就是 **wide EP**。它带来的问题不是“能不能 all-to-all”，而是：

- **专家放置**：哪些专家放在同一节点/同一 rail，决定 all-to-all 的跳数；
- **负载不均**：热门专家收到更多 token，尾延迟由最热专家决定；
- **专家复制**：给热门专家做副本（expert replication），路由表把 token
  分到副本；副本的梯度/权重同步要额外处理；
- **EPLB（expert parallel load balancer）**：按在线 token 分布动态调整
  专家→rank 的映射，减少热点；重排需要搬运专家权重，成本高，通常离线/
  周期性做；
- **batch invariance**：动态 EPLB 会改变路由，同一请求在不同时间可能得到
  不同 expert 组合；对评测/缓存/蒸馏不友好，需要固定映射或版本化。

容量规划时，wide EP 的 all-to-all 量级近似（与 10.6 同口径）：

```text
聚合 dispatch ≈ tokens · k · (2h + 4) · (ep-1)/ep  字节   # bf16 hidden + fp32 weight
聚合 combine  ≈ tokens · k · 2h · (ep-1)/ep        字节
每卡每方向    ≈ 聚合 / ep                          # 近似对称时
```

注意 `(ep-1)/ep` 已经描述“有多少 pair 跨 rank”；**不要再额外除以 `ep`**，
否则会低估约 `ep` 倍。`tokens` 是全局 token 数；若用本地 token 数，结果
是每 rank 量级。padding、split sizes、压缩会改变常数，但不会改变
“与 token 数 × k 成正比”的量级。不要只用平均带宽估，必须看 p99 尾延迟和
热点专家的排队。

### 40.7.3 PP 推理与 KV 的分 stage 放置

训练用 PP 切层，推理也会用 PP 把大模型放进多卡；但推理的 PP 与训练的 PP
有三点不同：

1. **batch 小、每步都要走完整 pipeline**。decode 每生成一个 token，hidden
   state 要从 stage 0 流到最后一个 stage；PP 气泡在 `batch=1` 时最严重。
2. **KV cache 按层分 stage**。每个 stage 只保存自己那些层的 K/V：
   `KV_stage = 2 · L_stage · n_kv · d · s · b · bytes`，再按 TP 切 head。
   `L_stage` 不均时，KV 显存和 attention 计算都会倾斜。
3. **prefill 与 decode 的并行选择不同**。prefill 是计算受限，适合 TP/PP；
   decode 是带宽受限，PP 的流水线空转更贵，通常优先 TP + 多副本。

KV 的分 stage 放置带来几个工程约束：

- **请求的 KV 分散在所有 stage**。一个请求的完整上下文不在单卡上；做
  prefix cache/PD 分离时，KV 传输要按层收集，metadata 要记录
  `layer → stage/rank` 映射，不能只按“一个请求一个文件”搬。
- **PP 与 PD 分离组合时，两端的 PP 布局要一致或可重分片**。prefill 侧
  PP=4、decode 侧 PP=2，KV 的层分组不同，必须按层重排；否则“能传但读错层”。
- **连续批处理要在每个 stage 维护 batch 状态**。不同请求的 decode 位置不同，
  每个 stage 都要有对应的 KV block table；调度器要保证同一请求的所有 stage
  使用同一套 block 映射。
- **故障域按 stage 放大**。一个 stage 挂掉，它负责的层 KV 全丢；请求要么
  重算 prefill，要么从其他副本恢复。PP 越深，单点故障的影响面越大。
- **stage 负载不均**。embedding/output 层、MoE 层、attention 层的计算/显存
  不同；按层数平均切会让某些 stage 成为瓶颈。推理侧要按实测的 per-stage
  latency 和 KV 字节数切，而不是只按 `L/pp`。

一个实用的选择顺序：

```text
模型能单节点 TP 放下 -> TP + 多副本，不上 PP
单节点放不下 -> PP 跨节点；prefill 侧可深 PP，decode 侧尽量浅 PP
需要 PD 分离 -> 先统一 PP 布局与 KV metadata，再做 KV 传输/池化
```

Megatron Core 的 inference 把 PP/TP/EP 与 KV cache 放在同一套并行抽象里；
vLLM/TensorRT-LLM 也有各自的 PP 实现。跨引擎部署时，**KV 的层映射和
block 布局**比权重转换更容易出错，验收要用固定 prompt 做 greedy 对齐。

## 40.8 量化推理

| 精度 | 权重 | KV cache | 特点 |
|---|---|---|---|
| BF16 | 2 B | 2 B | 基线 |
| FP8 | 1 B | 1 B | H100+，需 scaling |
| FP4/NVFP4 | 0.5 B | 0.5 B | Blackwell，块缩放 |
| INT8/INT4 | 1/0.5 B | 可选 | 需校准/内核 |

量化收益：

- 权重显存下降；
- 带宽受限的 decode 提速；
- 更大 batch/更长上下文。

风险：

- 精度下降（尤其长尾/数学/代码）；
- 校准数据偏差；
- KV cache 量化的累积误差；
- 不同 kernel 的数值差异。

### 40.8.1 KV cache 量化的实现细节

权重量化只影响一次矩阵乘；KV 量化会影响**每个 decode step**，误差会在整个
上下文上累积。因此 KV 量化的粒度比权重更讲究。

常见布局：

| 方案 | K 的粒度 | V 的粒度 | 说明 |
|---|---|---|---|
| per-tensor | 整个 cache 一个 scale | 同左 | 最简单，长尾误差大 |
| per-head | 每个 KV head 一个 scale | 同左 | GQA 下常用 |
| per-token（V） | — | 每个 token/head 一个 scale | V 的异常值多在 token 维 |
| per-channel（K） | 每个 channel 一个 scale | — | K 的异常值集中在少数 channel |
| group-wise | 每 32/64/128 个元素一组 | 同左 | INT4/FP4 常用，兼顾精度与元数据 |

一个常用的组合是 **K per-channel + V per-token**（KIVI 风格）：它不需要
校准数据，按当前上下文动态统计 `amax`。显存账本为：

```text
kv_bytes ≈ 2 · tokens · n_kv · d_head · bits/8   # K 和 V 各一份
         + tokens · n_kv · (bits_scale/8)      # per-token V scales
         + n_kv · d_head · (bits_scale/8)       # per-channel K scales
```

分页 cache 下还要多一层：scale 存在 block 里还是单独的张量？如果 block 大小
和 group 大小不对齐，一个 group 可能跨两个物理 block，gather 时要么先反量化
成 dense（失去省显存的意义），要么让 kernel 同时读两个 block 的 scale。
工程上通常让 `block_size` 是 group size 的整数倍。

验收不能只看“能不能跑”：

1. **固定 prompt 的 logits 差**：量化前后同一位置的 top-1/top-5 变化率；
2. **长上下文**：4K/32K 的 needle/检索准确率，而不是只看短 prompt 的 loss；
3. **贪心解码分叉点**：从第几个 token 开始与 FP16 输出不同；分叉太早说明
   K 的 per-channel 粒度不够；
4. **显存实测**：`kv_bytes` 必须按上面的公式核对，不能只看权重节省；
5. **吞吐**：反量化/scale 读取可能让 decode 变慢，尤其在 batch 小、带宽
   没打满时。

FP8 KV 在 Hopper/Blackwell 上已有 kernel 支持；INT4 KV 更多出现在研究/
特定推理引擎中。无论哪种，**训练-推理一致性**（40.12）都要重新跑：训练时
FP16/BF16 的 attention 与推理时量化 KV 的 attention 不是同一个数值函数。

## 40.9 容量规划示例

假设：

- 70B 模型，BF16 权重 140 GB；
- 8×H100 80GB，TP=8；
- 每卡权重 17.5 GB；
- KV cache：`L=80, n_kv=8, d=128, bf16`。**全模型**每 token 每层
  `2×8×128×2 = 4096 B`，80 层合计 `327,680 B ≈ 0.31 MB/token`；
- TP=8 把 KV 头切到每卡 `n_kv/TP = 1` 个，因此**每卡**每 token 只有
  `2×80×1×128×2 = 40,960 B ≈ 40 KB`；
- 每卡剩余显存 50 GB（扣除激活/临时）。

于是：

```text
4K 请求：40 KB × 4096 ≈ 160 MB/卡
并发 ≈ 50 GB / 160 MB ≈ 312 个请求（理论上限）

32K 请求：40 KB × 32768 ≈ 1.25 GiB ≈ 1.34 GB/卡
并发 ≈ 50 GB / 1.34 GB ≈ 37 个请求（理论上限）
```

实际要留碎片、prefill 峰值和调度余量，工程经验上只能用到理论值的
50%–70%（随 kernel、块大小、请求长度分布变化，必须实测；不是产品承诺）。
这也
说明为什么长上下文推理必须用 GQA/MLA、KV 量化、prefix cache、PD 分离：
32K 上下文的 KV 已接近每卡权重的 10%，而 4K 时它还只是零头；再叠加并发，
KV 就会超过权重。

## 40.10 mini 的生成实现与 KV cache 扩展

mini 原有 `generation.py` 每步重算整条序列，复杂度 `O(t²)`；新增的
`mini_megatron/generation_kv.py` 提供教学版 KV cache：

- 每层缓存 `[b, n_kv_local, past_len, d]`；
- decode 时只输入新 token；
- RoPE 使用全局位置 `past_len`；
- TP 下按 head 切分，采样前 gather 词表 logits；
- 与全量前向的 greedy 输出做等价性测试。

它仍然是单请求、PP=1 的实现；生产还需要：

- PagedAttention/连续批处理；
- prefix cache；
- chunked prefill；
- 多请求调度；
- CUDA Graph；
- 量化 KV。

但 KV cache 的核心语义（只算新 token、缓存 K/V、位置正确）在 mini 中可以验证。

## 40.11 推理性能剖析

用 Nsight Systems 看：

- prefill/decode 的时间占比；
- kernel 启动开销；
- KV cache 读写；
- attention 的 tile 效率；
- all-reduce/all-to-all；
- 批处理空隙。

常见优化：

- 增大 batch（decode 带宽受限，batch 越大越划算）；
- 连续批处理；
- prefix caching；
- chunked prefill；
- speculative decoding；
- FP8/FP4；
- CUDA Graph；
- kernel fusion（RMSNorm+residual、RoPE、MLP）。

## 40.12 训练-推理一致性

训练和推理是两套代码路径，最容易出现“训练 loss 很好，上线效果崩”的问题。
至少核对：

| 项 | 训练 | 推理 | 不一致的后果 |
|---|---|---|---|
| Tokenizer | 训练 tokenizer | 同版本文件 | 乱码/语义漂移 |
| Chat template | 训练时模板 | 推理时模板 | 角色/边界错 |
| RoPE | base/缩放/位置 | 同参数 | 长上下文退化 |
| Attention mask | causal/padding | 同语义 | 看到 padding/未来 |
| 精度 | bf16/FP8 | FP8/INT4 | 分布偏移 |
| 输出层 | 全词表 | TP gather/量化 | 采样错 |
| EOS/stop | 训练数据 | 推理配置 | 不停止/早停 |
| KV cache | 无（训练） | 有 | 位置/长度 bug |

验证方法：

1. **Teacher forcing 对齐**：同一 prompt，训练代码和推理代码算出的 logits
   在容差内一致；
2. **Greedy 对齐**：KV cache 解码与全量重算的 greedy 输出逐步一致（mini 的
   `test_generation_kv.py` 就是这类测试）；
3. **采样分布对齐**：固定 seed，比较 top-k/top-p 的分布；
4. **端到端评测**：同一 checkpoint 在训练框架与推理引擎上跑同一 benchmark；
5. **长上下文**：在最大长度附近测，RoPE/position 的问题只会在长序列暴露。

注意：批量推理可能改变归约顺序/kernel，logits 不保证逐位一致；用“统计一致 +
贪心一致”作为验收，而不是要求 bitwise。

## 40.13 本章小结

- 推理指标是 TTFT/TPOT/throughput/goodput，不是 MFU。
- KV cache 是长上下文推理的显存核心；GQA/MLA/量化/分页都在围绕它。
- 连续批处理、chunked prefill、prefix cache 提升吞吐/降低长尾。
- 投机解码用草稿+验证换加速，接受率决定收益。
- PD 分离让 prefill/decode 各自优化，但引入 KV 传输/调度复杂度。
- 训练-推理一致性要逐项核对 tokenizer/template/RoPE/mask/精度/stop。
- mini 的 KV cache 扩展验证语义，不追求性能。

下一章讨论评测、可复现性与实验管理。
