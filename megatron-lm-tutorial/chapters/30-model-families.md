# 第 30 章 模型家族与结构差异

Megatron Core 不只支持 GPT。不同模型结构会影响并行切分、位置编码、注意力、
检查点转换。本章给出一张“结构 → 并行影响”的地图。

## 30.1 GPT / decoder-only

主流 LLM 结构：

- pre-norm；
- causal self-attention；
- RoPE（多数）/ learned/ALiBi；
- SwiGLU MLP；
- GQA/MQA；
- tied/untied embedding。

并行影响：

- q/k/v 按 head 切；
- MLP 按 FFN 切；
- embedding/LM head 按词表切；
- RoPE 在 CP 下要全局位置。

mini 就是这一类。

### 30.1.1 结构选择如何改变并行与数值

“GPT/LLaMA 类”内部还有很多会改变参数量、通信和数值稳定性的选择。它们通常
不是并行维度的开关，却会决定同一个 TP/PP 配置能否跑对、账本是否对得上。

| 选择 | 相对基线 | 参数/计算变化 | 并行与数值影响 |
|---|---|---|---|
| LayerNorm → RMSNorm | 去掉均值与 bias | 每个 norm 从 `2h` 降到 `h`；每层省 `2h` | norm 参数在 TP 下复制；无 bias 后少一类梯度；低精度更稳，但均值偏移不再被校正 |
| QK-norm | 在 q/k 上按 head 做 norm | 每层多 `2·d`（或 `2·n_h·d`）参数 | norm 在 head 维局部，TP 天然切分；必须在 RoPE 之前，顺序错会改变长上下文行为 |
| Parallel attention/MLP | 两条支路共享一个 norm | 每层少一个 norm；串行依赖变短 | 一次 norm 的梯度同时进两条支路；TP 通信顺序变化，需重新验证等价性 |
| SwiGLU → GeGLU / ReLU² | 门控激活不同 | 仍是 3 个矩阵或 2 个矩阵 | FFN 宽度与参数量变；TP 切分规则相同，但 kernel/数值不同 |
| Pre-norm → Post-norm | norm 放在残差之后 | 参数量不变 | 深层更难训，依赖 warmup；PP 下梯度尺度更敏感 |
| Learned position → RoPE/ALiBi | 去掉 `L_max·h` 参数 | 位置参数为 0 | learned 在 PP 下只属于第一 stage；RoPE 无参数但要全局位置；ALiBi 无参数但外推/检索取舍不同 |
| Tied embedding | 输入/输出共享权重 | 省 `V·h` | mini/朴素实现要求 embedding 与输出同 stage；生产 Megatron 可跨 PP 逻辑共享，用 `finalize_model_grads` 对 embedding 梯度做 PP 归约/同步更新；优化器状态必须去重，否则一步更新两次 |
| QKV bias / linear bias | 默认无 bias | 每层多 `O(h)` | bias 在 TP 下复制，梯度要归约；很多新模型已去掉 bias |
| Residual scaling / LayerScale | 残差乘 `1/√(2L)` 或可学习缩放 | 少量参数或无参数 | 影响深层数值稳定性，不改变并行布局 |
| Shared expert（MoE） | 一个始终激活的专家 | 多 `N_shared` 参数 | 共享专家在所有 EP rank 复制还是切分，决定显存/通信；路由只决定稀疏专家 |

三个工程结论：

1. **参数账本要按结构分支算**。同为 `h=4096, L=32`，LayerNorm vs RMSNorm、
   GELU vs SwiGLU、tied vs untied，参数量能差几个百分点；`analysis.py` 的
   `dense_layer_parameters` 就是按这些分支写的。抄“12Lh²”做容量规划，遇到
   SwiGLU + GQA 会偏差很大。
2. **数值语义比并行布局更容易漏**。QK-norm 放在 RoPE 前还是后、RMSNorm 的
   eps 放在平方根内还是外、residual scaling 作用在哪条支路，都会让“loss
   能降”但和参考实现不一致。新增结构时，先与单卡参考做逐元素梯度对比，
   再谈 TP/PP。
3. **结构选择会改变“谁复制、谁切分”**。norm/bias/位置编码是复制参数，
   q/k/v/gate/up 是行切，out/down 是列切，embedding 是词表切。换结构时先
   更新这张“参数 → 切分语义”表，再改 checkpoint 重分片；否则加载能成功，
   权重却错位（见 24.5、附录 K）。

mini 的主链支持 LayerNorm、SwiGLU/GELU、RoPE/learned、tied/untied、bias
开关和 pre-norm；**不支持 RMSNorm、QK-norm、parallel block、ALiBi**。这不是
“这些结构不重要”，而是它们不影响 TP/PP/CP 的核心语义；把其中一个加进
mini 是很好的练习：先改 `TransformerBlock` 的 norm 构造，再更新
`analysis.py` 的参数量分支，最后用 `parallel_equiv.py --case tp` 验证
“结构变了，等价性仍然成立”。

## 30.2 BERT / encoder-only

- 双向注意力（无 causal mask）；
- post-norm 或 pre-norm；
- 通常有 NSP/MLM head；
- 序列分类/ token 分类。

并行影响：

- attention mask 是 padding mask，不是 causal；
- 某些实现用 TP 切 attention heads；
- embedding 词表并行；
- 训练目标可能不是 next-token CE。

## 30.3 T5 / encoder-decoder

- encoder 双向、decoder causal；
- cross-attention（Q 来自 decoder，K/V 来自 encoder）；
- relative position bias；
- span corruption 目标。

并行影响：

- encoder/decoder 的层可以分别切 PP；
- cross-attention 的 K/V 来自另一个 stage，p2p/缓存更复杂；
- relative bias 通常复制，不切。

## 30.4 Mamba / SSM / Hybrid

### 30.4.1 状态空间模型

Mamba/SSM 用递推替代 attention：

```text
h_t = A_t h_{t-1} + B_t x_t
y_t = C_t h_t
```

在选择性 SSM 中，`A/B/C` 依赖输入，表达能力更强。训练时用并行 scan/chunked
scan，把递推拆成块内并行 + 块间传递状态；推理时是常数内存/步。

### 30.4.2 并行影响

| 维度 | 影响 |
|---|---|
| TP | 按 hidden/状态维切分；scan 的 reduction 要匹配 |
| PP | 按层切，与 Transformer 类似 |
| CP | 不能像 attention 那样 all-gather KV；需要按序列传递 SSM 状态 |
| DP | 与普通模型相同 |
| 序列并行 | 状态在序列分片间传递，边界状态要通信 |

### 30.4.3 HybridModel

工业界常见做法不是“全 Mamba”，而是 Transformer + SSM 混合（HybridModel）：

- attention 层负责精确的全局检索；
- SSM 层负责长序列/线性成本；
- 层间比例、位置编码、norm 都要调；
- 调度器要同时处理 attention 的 KV/CP 和 SSM 的状态传递。

Megatron Core 有 SSM/HybridModel 支持；2026 roadmap 的 HybridModel 是重点，
包括 EP overlap、分组 HybridStack 调度，以及新模型家族（如 Qwen/DeepSeek
后续版本，以官方发布为准）的迁移。它的工程难点不在
单个 kernel，而在**两种层的并行语义不同**，却要共享同一个 3D 并行布局。

### 30.4.4 选择性 scan 的三种算法形态

把递推写成矩阵形式，才能看清并行化与数值代价。对每个通道：

```text
h_t = a_t ⊙ h_{t-1} + b_t ⊙ x_t          a_t = exp(Δ_t · A)
y_t = c_t^T h_t + d ⊙ x_t
```

三种实现对应三种取舍：

| 形态 | 计算 | 显存 | 适用 |
|---|---|---|---|
| 纯递推 | O(L)，逐 token 串行 | O(1) 状态 | 推理 decode |
| 并行 scan | O(L log L) 工作、O(log L) 深度 | O(L) | 短序列/训练原型 |
| chunked scan | O(L) 工作，块内并行、块间传状态 | O(L/chunk) 状态 | 生产训练 |

chunked scan 的关键是把长序列切成 `C` 大小的块：

```text
块内：用并行 scan 算出“假设初始状态为 0”的输出，以及块末的转移算子
块间：把前一块的最终状态 h 传进来，修正本块输出
```

转移算子是一个对角矩阵（逐通道），所以块间修正只要 `C` 次乘加；这也是
Mamba 训练能接近线性复杂度的原因。实现时要小心两个细节：

1. **块大小必须是 kernel 的编译期常量**（常见 64/128/256）。改变 chunk
   会改变浮点归约顺序，做数值对照时先固定。
2. **状态 dtype**：递推在 fp32 累加、bf16 存储是常见折中；全 bf16 的
   长序列误差会随 `L` 累积。

### 30.4.5 并行语义：SSM 与 attention 的五个不同

| 问题 | attention | SSM |
|---|---|---|
| CP 的跨 rank 依赖 | all-gather/ring 传 K/V | 传**边界状态** `h_{boundary}` |
| 序列 packing | block-diagonal mask | 文档边界重置状态（否则串味） |
| TP 切分 | 按 head/ hidden | 按 hidden + `d_state`；scan 的 reduction 要匹配 |
| 激活显存 | 与 `L²` 或 KV 长度相关 | 与 `L·d_state` 相关，通常更小 |
| 推理 decode | 每步读全部 KV | 每步只读常数大小状态 |

CP 下的 SSM 不能简单“把序列切块后各算各的”：rank `r` 的初始状态必须是
rank `r-1` 的块末状态。两种实现：

- **串行传递**：`cp` 个 rank 依次 send/recv 边界状态，简单但暴露延迟；
- **并行前缀**：把所有块的转移算子做一次 scan，再一次 all-gather 修正，
  延迟更低但要多存一份块末状态。

HybridModel 的调度器必须同时跟踪 attention 的 KV cache 和 SSM 的状态，
这也是 34.2 所说“两种层共享一个并行布局”的具体含义。

### 30.4.6 混合比例怎么定

设总层数 `L`，attention 层占 `r`，其余为 SSM。三个约束共同决定 `r`：

```text
KV cache 预算 ≈ r · L · 2 · n_kv · d_head · seq · batch · bytes
长上下文检索能力 ≈ f(r)（经验上不是线性）
训练吞吐 ≈ 1 / (r · attention_cost + (1-r) · ssm_cost)
```

实践中的经验区间是 `r ∈ [1/8, 1/4]`，但这不是定律：检索密集的任务需要更高
的 attention 比例，纯长文建模可以更低。确定比例的验收不能只看 loss：

- **needle/检索**：固定深度/长度的召回率；
- **状态传递**：CP 从 1 变 4 时，边界状态误差是否放大；
- **packing**：跨文档边界的状态是否被正确重置；
- **cache 账本**：`r` 改变后 KV cache 是否真的按预期下降。

如果这四项里有一项对不上，先别调比例——那通常是实现语义错了，而不是
架构选择错了。

## 30.5 MLA（Multi-head Latent Attention）

### 30.5.1 动机：KV cache 才是长上下文推理的瓶颈

标准 MHA 每个 token、每层缓存：

```text
KV = 2 × n_h × d × bytes
```

`n_h=128, d=128, bf16` 时，每 token 每层 64 KB；80 层、8K 上下文就是
`64 KB × 80 × 8192 ≈ 40 GB`/请求（未计 TP/CP 切分）。并发一上来，KV cache
比权重还大。

MLA 的思路：不缓存完整 K/V，而是缓存一个低秩 latent，推理时再投影。

### 30.5.2 结构

对输入 `x`：

```text
c_KV = W_DKV x            # latent，维度 d_c << n_h·d
K    = W_UK c_KV          # 推理时可按需解压
V    = W_UV c_KV
```

为了保留 RoPE 的位置信息，DeepSeek 还引入一个解耦的 RoPE 分量：

```text
k_R = RoPE(W_KR x)        # 每 token 少量维度
```

缓存只需要 `c_KV` 和 `k_R`：

```text
MLA cache/token/layer ≈ d_c + d_R
```

对比 MHA 的 `2·n_h·d`，如果 `n_h·d=16384`、`d_c=512`、`d_R=64`，cache 约
缩小 50 倍。代价是：

- 需要额外的下投影/上投影；
- 训练时 attention 的 Q/K/V 计算图更复杂；
- 推理时可以用“矩阵吸收”把 `W_UK` 吸进 `W_Q`、`W_UV` 吸进 `W_O`，避免
  显式解压，但与 RoPE 的解耦部分要特殊处理。

### 30.5.3 并行与实现要点

| 维度 | 切分方式 | 注意 |
|---|---|---|
| TP | Q heads 按 head 切；latent 通常复制（很小），也可按维切 | 确保 `W_UK/W_UV` 的切分与 K/V 布局一致；共享 `W_KR` 不能按 head 切 |
| PP | 按层切 | 与普通 Transformer 相同 |
| CP | 按序列切 | latent 也要按序列分片，KV 语义与 GQA 不同 |
| EP | 只影响 MoE FFN | 与 attention 正交 |

常见 bug：

- RoPE 只加在解耦分量上，却误加到完整 K；
- 推理时吸收矩阵后，position/scale 的顺序错；
- TP 切 latent 维时，`W_UK` 的输入维没有同步切；
- checkpoint 转换时把 `c_KV` 当成完整 K/V。

公开材料提到的 0.20 系列 MLA 优化包括 query RoPE dim loading、fused MLA KV
RoPE MXFP8 quantization 等；这些条目未逐条核对到具体 release tag，按 36.1 的 L2/L3
（已合并/路线图）处理。具体参数名/支持矩阵以当前 `transformer_config.py` 为准
（常见开关包括 multi-latent attention、q/kv LoRA rank、qk/v head dim 等）。
MLA + MoE + FP8 是 DeepSeek-V3 及后续版本（若存在）的核心组合，也是“结构
创新必须配套系统实现”的典型例子。

### 30.5.4 形状级实现

把 MLA 写成矩阵，能看清 TP/CP 该切哪一维。设 hidden=`h`，head 数=`n_h`，
content 维度=`d_nope`，value 维度=`d_v`，latent 维度=`d_c`，解耦 RoPE 维度=
`d_R`（DeepSeek-V2：`d_nope=d_v=128`、`d_R=64`、`d_c=512`）。为了书写简洁，
下面假设 `d_nope=d_v=d`，并令 `q_rank` 为可选的低秩 query 维度。

```text
# Query（可选低秩）
c_Q      = x W_DQ^T        W_DQ: [q_rank, h]
Q        = c_Q W_UQ^T      W_UQ: [n_h·(d_nope+d_R), q_rank]
Q_nope, Q_R = split(Q)     Q_nope: [n_h·d_nope], Q_R: [n_h·d_R]

# KV latent
c_KV     = x W_DKV^T       W_DKV: [d_c, h]
K_nope   = c_KV W_UK^T     W_UK: [n_h·d_nope, d_c]
V        = c_KV W_UV^T     W_UV: [n_h·d_v, d_c]

# 解耦 RoPE key：所有 head 共享同一个 key head
k_R      = RoPE(x W_KR^T)  W_KR: [d_R, h]        # 注意不是 n_h·d_R

# 每个 head i
score_i  = (Q_nope_i K_nope_i^T + Q_R_i k_R^T) / sqrt(d_nope+d_R)
O_i      = softmax(score_i) V_i                  # [*, d_v]
y        = concat_i(O_i) W_O^T                   # W_O: [h, n_h·d_v]
```

缓存只需要 `c_KV`（每 token `d_c`）和 `k_R`（每 token `d_R`），而不是完整的
`K/V`（每 token `2·n_h·d_v`）。按上面的数字，每 token 每层从 `32768` 个元素
降到 `576` 个元素；这正是“约 50 倍”的来源。`k_R` 是**一个共享 key head**，
不是 `n_h` 份；把它写成 `n_h·d_R` 会同时算错 cache 大小和吸收矩阵的形状。

实现上，`W_DKV` 与 `W_KR` 通常被拼成一个 `kv_a_proj_with_mqa` 矩阵：
前 `d_c` 行产出 `c_KV`，后 `d_R` 行产出 `k_R`。训练/转换时按 `d_c` 切开，
不要按 head 切；`k_R` 也不参与 head 维的 TP 切分。

**为什么 `k_R` 可以共享**：RoPE 只给 score 注入相对位置，`Q_R_i k_R^T` 中每个
query head 有自己的 `Q_R_i`，共享的 `k_R` 仍能产生逐 head 不同的位置分数。
这与 GQA 的“多个 query head 共享 KV head”是同一类设计，但 MLA 共享的是
RoPE 分量，content 分量仍由 latent 解压得到。

**矩阵吸收**：推理时可以把 `W_UK` 吸进 query、把 `W_UV` 吸进输出投影，让
attention 直接对 `c_KV` 做。对每个 head `i`：

```text
# content 部分：Q_nope_i K_nope_i^T
Q_nope_i = c_Q W_UQ_nope_i^T,  K_nope_i = c_KV W_UK_i^T
⇒ Q'_i = c_Q (W_UQ_nope_i^T W_UK_i)        # [*, d_c]

# value 部分：O_i W_O_i^T
O_i = probs_i V_i = probs_i c_KV W_UV_i^T
⇒ W_O'_i = W_O_i W_UV_i                    # [h, d_c]（逐 head 的块）
```

于是推理可以写成：

```text
Q'_i = c_Q (W_UQ_nope_i^T W_UK_i)
y    = Σ_i [ softmax(Q'_i c_KV^T + Q_R_i k_R^T) c_KV ] W_O'_i^T
```

注意三个容易写错的维度：

1. 吸收后 `Q'_i` 的维度是 `d_c`，必须与 `c_KV` 对齐；只写
   `W_UQ W_DQ` 会得到 `n_h·d_nope`，少乘了 `W_UK`。
2. `W_O'` 是**逐 head** 的 `[h, d_c]` 块；把所有 head 的 `W_UV` 拼成一个
   `[n_h·d_v, d_c]` 再整体右乘 `W_O`，会把不同 head 的输出混在一起。
3. RoPE 部分（`Q_R/k_R`）不能吸收，必须单独计算后加到 score 上；吸收只
   作用于 content 分量。

训练时通常不吸收（显式解压 K/V 更简单、梯度图更清晰），推理时再变换权重。
吸收后的计算图与训练图数值等价，但浮点顺序不同，转换后必须用 logits/loss
对齐验证，不能只看“能生成”。

**TP 切分**：

| 参数 | 切分 | 注意 |
|---|---|---|
| `W_UQ_nope/W_UK/W_UV` | 输出维按 head 切 | 与 Q/K/V 的 head 布局一致 |
| `W_UQ_R` | 输出维按 head 切 | 每个 query head 有自己的 RoPE 分量 |
| `W_DQ/W_DKV` | 通常复制（latent 很小） | 若切 latent，需要 all-gather |
| `W_KR` | **复制，不切** | 所有 head 共享同一个 RoPE key |
| `W_O` | 输入维按 head 切 | Row Parallel，最后 all-reduce |

**CP 切分**：`c_KV/k_R` 按序列切；attention 需要跨 CP 聚合 KV（all-gather 或
ring）。与 GQA 不同，MLA 的 KV 是低秩 latent，通信量更小，但解压/吸收的顺序
不能错。

**检查点转换**：HF 的 MLA 权重名通常是 `kv_a_proj_with_mqa`、`kv_b_proj`、
`q_a_proj`、`q_b_proj`、`o_proj`；Megatron 侧按 `W_DKV/W_UK/W_UV/W_DQ/W_UQ`
映射。转换时要确认：

- `kv_a_proj_with_mqa` 是否把 `c_KV` 和 `k_R` 拼在同一个矩阵里；
- `q_a_proj/q_b_proj` 是否包含 RoPE 分量；
- 输出维的 head 顺序是否与 TP 切分一致。

这些细节错一个，模型能加载、能生成，但 attention 已经坏了——必须用 logits/
loss 对齐验证。

### 30.5.5 mini 中的 MLA：可运行的形状参考

`mini_megatron/parallel/mla.py` 把 30.5.4 的公式写成了一个独立模块
`MLAttention`。它不接入 `TransformerStage`，目的是让形状、RoPE 解耦、
cache 语义可以被单独验证。

```python
import torch
from mini_megatron.parallel.mla import MLAttention

m = MLAttention(
    hidden_size=64, num_heads=8,
    q_lora_rank=32, kv_lora_rank=32,
    qk_nope_head_dim=16, qk_rope_head_dim=8, v_head_dim=16,
    max_seq_len=16,
).eval()
x = torch.randn(2, 16, 64)
pos = torch.arange(16).unsqueeze(0).expand(2, -1)
out, (c_kv, k_pe) = m(x, position_ids=pos, use_cache=True)
# out: [2, 16, 64]; c_kv: [2, 16, 32]; k_pe: [2, 1, 16, 8]
```

关键实现点：

1. `kv_a_proj_with_mqa` 一次产出 `[c_kv | k_pe]`，`k_pe` 只有**一个 head**
   （shape `[b, 1, s, d_R]`），与 30.5.4 的共享 key 一致；
2. RoPE 只作用于新 token 的 `q_pe/k_pe`；cached `k_pe` 已经旋转过，不能
   再转一次；
3. 解码时把 `c_kv` 与历史拼接，再用 `kv_b_proj` 重新解压 `k_nope/v`；这是
   MLA 用“小 cache + 额外 GEMM”换显存的本质；
4. 因果 mask 用全局位置 `q_start..q_start+s_q-1`，因此 prefill 和逐 token
   decode 共用同一段逻辑；
5. 测试覆盖：输出形状、因果性、prefill+decode 与全量前向一致、padding
   mask、全 mask 行输出 0、低秩/直接 query 两条路径的反向。

运行：

```bash
python -m pytest -q tests/unit/test_mla.py
python scripts/mla_demo.py
```

demo 的一次输出（tiny 配置）：

```json
{
  "max_abs_diff_full_vs_incremental": 1.7881393432617188e-07,
  "cache_elements_mla": 1280,
  "cache_elements_mha_equivalent": 8192,
  "cache_ratio_mha_over_mla": 6.4
}
```

这个 demo 的 cache ratio 只有 6.4×，因为 head 数/维度很小；真实
DeepSeek-V2 配置下约 50×。比例取决于 `n_h·d_v` 与 `d_c+d_R` 的比值，不是
常数。

**与生产 MLA 的差距**：没有 TP/CP 切分、没有吸收后的推理 kernel、没有
FP8、没有把 MLA 接进 `Trainer`/checkpoint；`q_lora_rank=None` 与低秩路径
都支持，但只是单进程参考实现。

## 30.6 MoE 模型

- DeepSeek-V3/V4（V4 在 0.20.0 release notes 中列为 HybridModel 支持；
  细节以 recipe 为准）、Qwen MoE、GPT-OSS、Nemotron 等；
- 专家数/共享专家/路由策略差异大；
- EP/TP/PP 组合复杂；
- 推理侧需要 expert tensor parallel / batch-invariant routing。

## 30.7 多模态：把另一种模态塞进同一个并行栈

多模态训练把视觉/音频/视频 encoder 与 LLM 拼在一起：

```text
图像 → ViT/encoder → projector → 视觉 token ┐
文本 → tokenizer → 文本 token ───────────────┴→ LLM
```

### 30.7.1 结构：encoder、projector、LLM

| 组件 | 常见选择 | 并行方式 | 备注 |
|---|---|---|---|
| 视觉 encoder | ViT/CLIP/SigLIP/InternViT | DP/TP，通常冻结或低 LR | 图像 patch 数决定 token 预算 |
| projector | 2 层 MLP / Q-Former / Perceiver | 复制或 TP | 对齐视觉/文本空间 |
| LLM | decoder-only | TP/PP/EP/CP | 与纯文本训练相同 |
| 音频 encoder | Whisper/Conformer | DP/TP | 采样率/帧率决定 token 数 |

一个 448×448 图像、patch=14 的 ViT 产生 `32×32=1024` 个 patch token；视频
再加时间维，`8 帧 × 256 token = 2048`。一张图可能抵上千个文本 token，序列
长度和激活显存会被图像主导。

### 30.7.2 位置编码：M-RoPE 与 3D 位置

文本只有 1D 位置；图像有 `(t, h, w)` 三个坐标。常见做法：

- **M-RoPE**：把 head 维度切成三份，分别编码时间/高/宽；文本 token 的三个
  分量取相同值；
- **可学习 2D/3D 位置**：每个 patch 一个 embedding；
- **相对位置 bias**：在 attention score 上加。

实现要点：

- 位置 id 必须与 **packing 后的 token 布局**一致；图像 token 和文本 token
  混排时，不能简单用 `arange(seq_len)`；
- CP 切序列时，位置 id 要按全局坐标计算（同 RoPE 的 CP 处理）；
- 图像 token 的 attention mask 不能跨图/跨样本泄漏。

### 30.7.3 并行映射：两种布局的拼接

视觉 encoder 和 LLM 的并行需求不同：

- encoder 通常是**计算密集、序列较短**，TP 收益有限，DP 更自然；
- LLM 是**显存/通信密集**，需要 TP/PP/EP/CP；
- projector 很小，通常复制；
- PP 切分时，视觉层放 stage 0 会让 stage 0 明显更重；可以给视觉层单独
  一个 stage，或把部分 LLM 层前移。

一个常见的错误是“encoder 用 TP=8、LLM 用 TP=2”，但两者共享同一个进程组，
导致 encoder 的 all-reduce 跨了不该跨的节点。生产实现通常为 encoder 建独立
的并行组，或在 encoder 内用 DP + 单卡 TP。

### 30.7.4 数据与 loss mask

多模态样本通常是：

```text
[图像 token][文本 token][图像 token][文本 token]...
```

训练目标只应计算**文本 token**（以及可能的特殊 token）：

```python
labels[image_token_positions] = -100
```

否则模型会去“预测图像 patch”，浪费容量且破坏语言能力。需要检查：

- 图像 token 是否被正确 mask；
- 多轮对话中历史图像是否重复计算 loss；
- padding token 与 image token 的 id 是否冲突；
- chat template 的特殊 token 是否在词表中。

数据管线比文本重得多：JPEG 解码、缩放、归一化、视频抽帧都在 CPU/GPU 上，
容易成为吞吐瓶颈。生产做法是预解码/缓存、把图像编码离线化、或用 DALI/
torchvision 的 GPU 解码。

### 30.7.5 训练课程

典型三阶段：

1. **对齐阶段**：冻结 LLM 和 encoder，只训 projector；数据是图文对；
2. **联合微调**：解冻 LLM（低 LR），encoder 可选冻结；
3. **指令微调**：多模态对话/OCR/视频，混入纯文本数据防止语言能力退化。

超参上要为 encoder/projector/LLM 设不同 LR；encoder 的 LR 通常比 LLM 低
1–2 个数量级。冻结 encoder 时要注意 norm/dropout 的 train/eval 状态。

### 30.7.6 推理：预处理也是系统问题

多模态推理的瓶颈往往不在 LLM：

- 图像下载/解码/缩放；
- 视觉 token 的 prefix cache；
- 多轮对话中历史图像的 KV 复用；
- media-affinity routing（把同一图像/视频的请求路由到同一副本，复用缓存）；
- 视频的帧采样与 token 预算。

公开材料提到的 0.20 推理方向包括 vision/video、media-affinity routing、
audio RoPE 等（以 release notes 为准，未逐项核对 tag；发布状态按 36.1 分级）。这些不是“把 encoder
接上”就能得到的能力，而是数据、并行、缓存、调度的共同工程。

### 30.7.7 评测与失败模式

多模态评测（MMMU、MathVista、DocVQA、视频 QA）对预处理极其敏感：分辨率、
归一化、prompt 模板、图像顺序都会改变分数。常见失败模式：

| 现象 | 可能原因 |
|---|---|
| 训练 loss 降但评测差 | 图像 token 未 mask / 评测预处理不一致 |
| OOM 且序列长度超预期 | 图像分辨率/patch 数失控 |
| PP stage 负载严重不均 | 视觉层全在 stage 0 |
| 语言能力退化 | 纯文本数据比例太低 / 联合训练 LR 太大 |
| 多轮图像问答重复/错位 | prefix cache 的 image token 位置错 |

多模态不是“模型结构问题”，而是**数据管线 + 并行布局 + 缓存**的系统问题。

### 30.7.8 视觉编码器的并行账本

把图像 token 数、ViT 参数/FLOPs 和 TP 通信放在一起，才能判断“encoder 该不该
上 TP”。设图像 `H×W`、patch `p`、2×2 merge、`T` 帧：

```text
n_patches = (H/p) · (W/p)
n_img     = n_patches / merge² · T + 1(cls)
```

| 输入 | patch | merge | n_img（未计 cls） |
|---|---:|---:|---:|
| 224×224 | 14 | 1 | 256 |
| 448×448 | 14 | 1 | 1024 |
| 448×448 | 14 | 2 | 256 |
| 896×896 | 14 | 2 | 1024 |
| 8 帧 448² | 14 | 2 | 2048 |

ViT 每 block 参数约 `12h_v²`（attention 4 + MLP 8；SwiGLU 约 16），
每图每 block **前向** FLOPs 约 `24·n_img·h_v²`，训练（前向+反向）约
`72·n_img·h_v²`。TP=`t` 时，每个 block 有两次 row-parallel all-reduce
（attention 输出、MLP down），每卡发送约：

```text
8 · b · n_img · h_v · (t-1)/t   字节（bf16）
```

而每卡训练计算是 `72·b·n_img·h_v²/t` FLOPs。通信/计算的时间比约为：

```text
(t-1) · P / (9 · h_v · B)
```

其中 `P` 是单卡峰值、`B` 是有效互联带宽。`h_v=1024` 时这个比值明显高于
`h=8192` 的 LLM：视觉 encoder 的 hidden 小、序列短，TP 切完后的 GEMM 更容易
变成通信受限。**常见选择是 encoder 用 DP/FSDP（必要时 PP），LLM 用 TP/PP/EP；
两者用独立的并行组**。如果共享同一个 TP 组，encoder 的 all-reduce 会和 LLM
抢同一个 NVLink 域，消息大小却完全不同，调优时很难归因。

视觉 encoder 的 PP 放置也要单独算。设视觉塔占端到端计算的比例为 `f_v`，
LLM 有 `L` 层、PP=`pp`，理想每 stage 占 `1/pp`。stage 0 放视觉塔后，应分到
的 LLM 层数约为：

```text
L0 ≈ L · (1/pp - f_v) / (1 - f_v)
```

例如 `f_v=0.15, pp=4, L=32`，`L0 ≈ 32 × 0.10/0.85 ≈ 3.8` 层；若仍按
`32/4=8` 层平均切，stage 0 会比其它 stage 重约 1.7 倍，气泡和 straggler
都从这里来。若 `f_v ≥ 1/pp`，stage 0 本身已超载，应给视觉塔单独 stage；
否则用不均匀 PP 层数。

### 30.7.9 多模态数据与评测的端到端检查

一个最小的端到端闭环：

```text
原始图像/视频 → 解码/缩放/归一化 → 视觉 token → packing（文本+图像）
  → 训练（loss mask 只算文本）→ 评测（同预处理）→ 线上 bad case
```

每一步都可能引入“训练 loss 正常、评测崩”的问题：

- **预处理版本**：分辨率、插值、归一化 mean/std、颜色空间必须与评测一致；
- **token 预算**：一张 896² 图 merge 后约 1024 token，相当于一篇短文；
  数据配比要按 token 而不是“样本数”；
- **loss mask**：图像 token、padding、系统 prompt 是否计入 loss，必须与
  训练目标一致；
- **评测污染**：DocVQA/OCR 等评测的图片可能出现在训练语料中，要做图像/
  OCR 文本的去重与污染报告；
- **吞吐**：JPEG 解码和 resize 常在 CPU 上成为瓶颈，预解码/缓存/GPU 解码
  要把这部分从关键路径移走。

多模态的“ scaling ”不只是堆 LLM 层：视觉 token 数、encoder 容量、数据质量
和缓存命中率共同决定单位成本。

## 30.8 结构差异对并行的总结

| 结构 | 位置编码 | 注意力 | 并行注意点 |
|---|---|---|---|
| GPT | RoPE/learned | causal | TP heads、CP 全局位置 |
| BERT | learned/relative | 双向 | padding mask、post-norm |
| T5 | relative bias | enc-dec + cross | stage 切分、cross K/V |
| Mamba | 递推 | SSM | 专用 kernel；CP 需按序列传递 SSM 状态，不能直接套 attention 的 KV all-gather |
| Hybrid | 混合 | attn+SSM | 分组调度、EP overlap |
| MLA | RoPE/压缩 | latent KV | latent 维切分、KV cache |
| MoE | 同 GPT | causal | EP/all-to-all、路由 |
| 多模态 | 多模态 RoPE | 变长 | 视觉/文本 token 拼接 |

## 30.9 转换与互操作

不同结构的权重映射、QKV 融合、GQA/MLA 重排、PP/TP 导出，都由 Megatron Bridge
处理。本节只保留结构差异的结论：

- GPT/LLaMA 类：QKV 融合顺序、GQA 的 KV 复制、GLU 的 gate/up 对齐是三大坑；
- MLA：latent/RoPE 分量的拆分与吸收顺序；
- MoE：专家编号、shared expert、TP/EP 布局；
- 多模态：encoder/projector/LLM 三段的命名与并行布局。

完整的转换流程、映射类型、数值校验和失败案例见第 35 章。

## 30.10 mini 的取舍

训练主链只实现 GPT decoder（含稠密/MoE/EP）：

- 不支持 BERT/T5/Mamba/多模态；
- **MLA 有一个独立教学模块** `parallel/mla.py`（`MLAttention`），支持
  低秩 query、latent KV cache、prefill/decode 与 RoPE 解耦分量，但不接入
  `TransformerStage`/Trainer，也没有 TP/CP 切分；
- TP/PP/CP/EP/SP/CE 的语义是通用的；
- 加新模型时，主要改 `TransformerBlock`/attention/embedding，不必重写并行层；
- MLA 需要新增 latent 投影与 KV cache 布局，多模态需要新增 encoder/projector
  和变长位置编码，二者都不能只靠改 attention mask 完成。

MLA 的独立实现和数值测试（`tests/unit/test_mla.py`、`scripts/mla_demo.py`）
用来把 30.5.4 的形状/吸收公式变成可执行代码；它不等于生产 MLA kernel，也
不覆盖 TP/CP 下的 latent 切分。

## 30.11 本章小结

- 不同模型的 attention/mask/位置编码/FFN 结构不同，会改变并行切分。
- GPT 的 TP/PP/CP 经验不能直接套到 Mamba/MLA/多模态。
- MoE 是结构 + 系统的双重问题。
- Bridge 负责 HF 互转，但版本/模型家族要匹配。
- mini 只覆盖 GPT，但并行原语可复用。

下一章讨论数据管线与训练配方。
