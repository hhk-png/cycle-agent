# 第 18 章 Attention 实现：RoPE、GQA 与 CP

`mini_megatron/parallel/attention.py` 包含三个部分：位置编码、核心注意力、
张量并行自注意力。

## 18.1 RoPE

RoPE 把位置信息编码成旋转：

```text
q' = q cos(θ_pos) + rotate_half(q) sin(θ_pos)
k' = k cos(θ_pos) + rotate_half(k) sin(θ_pos)
```

其中 `θ_pos` 由 `inv_freq = base^(-2i/d)` 与位置相乘得到。旋转后的 `q·k` 只
依赖相对位置，因而支持外推（配合 NTK/YaRN 等方法）。

实现要点：

- `cos/sin` 缓存按 `max_seq_len` 构建；
- 非 interleaved（GPT-NeoX 风格）用 `rotate_half` 分成前后两半；
- interleaved 用奇偶配对；
- CP 下必须用**全局位置**，rank 1 的起点不是 0；
- cache 可能被移到不同 device，`_build_cache` 在 device 变化时重建。这里有一
  个容易漏的边界：旧 cache 可能比当前请求的 `seq_len` 更长，重建时必须用
  “实际缓存长度”而不是 `seq_len` 去 reshape。mini 曾在 interleaved 分支踩过
  这个坑（CPU prefill → CUDA decode 时 shape mismatch），现在由
  `tests/unit/test_rope_cache.py` 覆盖。

mini 的 `RotaryEmbedding`：

```python
inv_freq = 1 / (base ** (arange(0, d, 2) / d))
freqs = outer(positions, inv_freq)
emb = cat((freqs, freqs), dim=-1)
cos, sin = emb.cos(), emb.sin()
```

`_build_cache` 会检查 `device`，避免 CUDA 上把 CPU cache 与 GPU position ids
混用。

### 18.1.1 RoPE 外推与长上下文位置编码

RoPE 的每个维度对 `i=0..d/2-1` 有一个角频率和一个波长：

```text
ω_i = base^(-2i/d)
λ_i = 2π / ω_i = 2π · base^(2i/d)
```

`d=128, base=10000` 时，`λ_0≈6.3` 个 token，`λ_63≈5.4×10^4` 个 token。
也就是说，同一组 head 里既有“几 token 转一圈”的高频维，也有“几万 token
才转一圈”的低频维。训练长度 `L_train` 只覆盖了低频维相位空间的一小段；
直接外推到 `L > L_train` 时，低频维进入训练时没见过的相位，attention
logits 会漂移——这就是“RoPE 能外推”与“RoPE 不用改就能外推”的区别。

工业上有四类改法，核心都是**改变不同维度的有效波长**：

| 方法 | 变换 | 是否要继续训练 | 适用 | 主要风险 |
|---|---|---|---|---|
| 位置插值 PI | `p' = p / s`，所有波长拉长 `s` 倍 | 通常要（少量） | 2K→8K/32K | 高频分辨率下降，短序列能力退化 |
| NTK-aware | `base' = base · s^(d/(d-2))` | 可零样本，微调更好 | 4K→32K/128K | 公式/实现不统一，长序列仍需验证 |
| YaRN / NTK-by-parts | 按波长分段插值 + attention 温度 | 通常要 | 32K→128K/256K | 超参多，训练/推理必须一致 |
| LongRoPE | 逐维非均匀缩放 + 渐进扩展 | 要 | 128K→1M+ | 搜索成本高，权重/配置绑定 |
| ALiBi（非 RoPE） | 按距离加线性 bias | 从头训/微调 | 外推基线 | 长文检索能力可能弱于 RoPE |

几个容易混淆的点：

1. **PI 与 NTK 不是一回事**。PI 把所有维度等比例压缩；NTK 让高频维几乎不变、
   低频维拉长，因而在短序列上更不容易退化。把两者混写成“NTK 就是位置除以
   factor”会在实现时差一个 `base` 变换。
2. **动态 NTK 会改 cache**。`base` 随当前长度变化时，`inv_freq/cos/sin` 必须
   重建；如果沿用旧 cache，位置编码会与训练/推理配置不一致。mini 的
   `RotaryEmbedding._build_cache` 只处理长度/设备变化，没有处理 `base` 变化，
   生产实现要把“配置变更”也纳入 cache key。
3. **CP 下缩放的是全局位置**。rank 1 的 token 位置仍是 `s/cp .. 2s/cp-1`，
   不是本地 `0..s/cp-1`；RoPE 缩放必须作用在全局位置上，再切片。
4. **训练/推理必须用同一套 scaling**。训练用 `factor=4`、推理忘配，长上下文
   会“能加载但检索崩”；这类问题在 4K 评测上看不出来，必须在目标长度上测。
5. **缩放不能修复数据/训练不足**。把 4K 模型硬拉到 1M，即使位置编码数值不
   溢出，模型也没有见过那么长的依赖；需要长文数据课程与评测（见 31.7、34.4）。

一个可执行的验收顺序：

```text
1. 固定 checkpoint，比较训练长度内的 loss（缩放不应破坏短序列）；
2. 在 2×/4× 训练长度上测语言建模 perplexity；
3. 跑 needle-in-a-haystack / RULER 的检索子集；
4. 检查 retrieval head 的 attention 是否真的落到目标 token；
5. 最后才上端到端长文任务，并与同算力的 dense 基线对比。
```

mini 只实现普通 RoPE（`rotary_base`/`rotary_interleaved`），不包含 scaling。
一个合适的练习是：给 `RotaryEmbedding` 加 `scaling="linear"` 与
`scaling_factor`，让 `p' = p/factor`，然后用 `test_rope_cache.py` 的固定
输入验证 `factor=2` 时位置 8 的编码等于原实现位置 4 的编码；再加一个
`factor=1` 的回归，确保默认路径逐元素不变。生产参数名（如
`--rotary-base`、`--rotary-scaling-factor`、`--position-embedding-type`）
会随版本漂移，以当前 `transformer_config.py` 为准。

## 18.2 GQA

GQA 的 Q 有 `n_h` 个头，K/V 只有 `n_kv` 个：

```python
q: [b, n_h, s, d]
k: [b, n_kv, s, d]
v: [b, n_kv, s, d]
k = k.repeat_interleave(n_h // n_kv, dim=1)
v = v.repeat_interleave(n_h // n_kv, dim=1)
```

TP 下：

- `n_h % tp == 0`；
- `n_kv % tp == 0`（mini 的要求）；
- 每个 rank 的 q/k/v 投影只输出本地 head；
- attention 后 out_proj 做 Row Parallel。

如果 `n_kv < tp`，生产实现会复制 KV head 或切 head_dim；mini 在配置校验中
拒绝，避免“看起来能跑但 head 对错”。

## 18.3 CoreAttention

```python
scores = q @ k.transpose(-2, -1) * (1/sqrt(d))
scores = scores.float()
scores = scores.masked_fill(~causal, -inf)
probs = softmax(scores, dim=-1).to(q.dtype)
out = probs @ v
```

为什么用 fp32 softmax：大模型 attention logits 的动态范围大，bf16 softmax 容易
溢出/精度不足。生产 FlashAttention 内部也用 fp32 累加/softmax。

### 因果 mask

普通 TP：

```text
q_positions = 0..s-1
k_positions = 0..s-1
mask[i,j] = j <= i
```

CP：

```text
q_positions = cp_rank*s_local .. (cp_rank+1)*s_local-1
k_positions = 0..s_global-1
mask[i,j] = k_positions[j] <= q_positions[i]
```

mini 显式构造这两个向量，不依赖 `torch.triu` 的本地三角，避免 CP 错位。

### padding mask

`CoreAttention` 支持可选的 `attention_mask`（`[b, s]`，0 表示 padding），把它
扩展到 `[b,1,1,s]` 后 mask；MLA 模块也支持同样的接口。但**训练主链并没有
接它**：`Trainer` 使用按固定长度切分的 `SyntheticTokenDataset`/
`BinTokenDataset`，`BaseSchedule._forward_step` 不传 `attention_mask`，
`data/packing.py` 的 document mask 也只是独立的“规划/教学”模块，尚未接入
训练循环。因此：

- 单测可以验证 mask 的数值语义；
- 真正训练时，padding/文档边界不会自动生效；
- 生产 Megatron 的 packed-sequence 路径会把 `packed_seq_params` 一路传到
  attention/CE，并在 loss 上做 token 级归一化，复杂度远高于这里。

这也是“模块能跑”和“训练链正确”之间的一个典型差距：看到一个 `attention_mask`
参数，先确认调用方真的传了它。

`data/packing.py` 把语义写全了，但只是独立模块：

- **文档拼接**：多个短文档拼成 `s` 长度的序列，减少 padding；
- **position reset**：每个文档从 0 重新计数，RoPE 不能跨文档连续；
- **loss mask**：只对目标文档的 token 计 loss，文档边界/分隔符不计；
- **block-diagonal mask**：文档 i 的 token 不能看到文档 j 的 token，否则等于
  把无关上下文喂给 attention。

`pack_documents` / `iter_document_masks` 有单测，但 `Trainer` 的
`_forward_step` 没有把 mask 传到 `CoreAttention`。接入时要同时改三处：数据
（返回 position_ids/mask）、attention（接受 mask）、CE（按有效 token 归一化），
只改一处会“能跑但语义错”。

## 18.4 SelfAttention 的 TP 实现

```python
q = self.q_proj(x)   # Column Parallel, 本地 heads
k = self.k_proj(x)
v = self.v_proj(x)
q = q.view(b, q_seq, n_h // t, d).transpose(1, 2)
...
q, k = rotary(q, k, position_ids)
if cp > 1:
    k = gather_cp(k)
    v = gather_cp(v)
out = core_attention(q, k, v, ...)
# transpose 后 head 维不连续，必须先 contiguous 才能合并 head 与 d
out = out.transpose(1, 2).contiguous().view(b, q_seq, -1)
return self.out_proj(out)   # Row Parallel
```

### 序列并行的形状 bug

在 SP 下，输入 `x` 的形状是 `[b, s/t, h]`，但 q_proj 会先 all-gather 成
`[b, s, h]`，所以 `q` 的序列长度是 `s`，不是输入的 `s/t`。mini 早期用
`b, s, _ = x.shape` 去 reshape q，报：

```text
shape '[2, 16, 2, 16]' is invalid for input of size 2048
```

修复是使用 `q_seq = q.size(1)`，而不是输入的 `s`。这个 bug 只在 SP 下出现，
普通 TP 测试覆盖不到。

### CP 的 KV gather

CP 下 q 是本地序列块，k/v 需要全局：

```python
k = _gather_cp_sequence(k)
v = _gather_cp_sequence(v)
```

`_GatherContextParallel` 的 forward 是 all-gather（dim=2），backward 是
all-reduce + chunk。为什么 backward 要 all-reduce？因为每个 CP rank 用同一份
完整 K/V 计算不同 Q 块的 attention，而全局 loss 是各 Q 块之和；本地 K/V 的
梯度必须累积所有 Q 块的贡献。只 chunk 会丢掉其他 rank 的贡献（见第 09 章）。

### 18.4.1 ring attention：另一种不物化全局 K/V 的参考

`parallel/ring_attention.py` 是独立的单进程参考，不接进 `SelfAttention`。
它模拟 `cp` 个 rank，把 K/V 切成块在环上轮转，用 online softmax 累积：

```python
m_i, l_i, acc = -inf, 0, 0
for step in range(cp):
    j = (r - step) % cp              # K/V 块倒序流动
    if causal and j > r: continue    # 整块未来，跳过
    s = q_r @ k_j.T / sqrt(d) + block_mask(j, r)
    m_new = max(m_i, s.max(-1))
    alpha = exp(m_i - m_new)
    p = exp(s - m_new)
    l_i = l_i * alpha + p.sum(-1)
    acc = acc * alpha + p @ v_j
    m_i = m_new
out = acc / l_i
```

它验证三件事：全局因果 mask（query 的全局位置，不是本地 `0..s/cp`）、
`m/l/acc` 的 online 更新顺序、以及反向可微。`scripts/ring_attention_demo.py`
在 cp=1/2/4/8 上与 dense 的误差约 `3.6e-7–4.8e-7`；朴素连续切分下
`blocks_per_rank=[1..cp]`，暴露 8× 负载不均。生产实现会把 KV 块轮转换成
真实 P2P、加 load-balanced mask，并与计算重叠；mini 的 `SelfAttention`
仍走 all-gather KV，因为它的目标是让训练主链可验证，而不是追求 CP 显存。

## 18.5 为什么不用 SDPA/FlashAttention

mini 用显式 matmul 是为了：

- 展示 `s×s` score 矩阵和 mask；
- 方便 CP 的全局位置；
- 与参考实现逐元素对比。

生产必须用 FlashAttention/cuDNN/TE fused attention：

- 不物化 `s×s`；
- online softmax；
- 与 CP/TP 的通信融合；
- 支持变长/稀疏 mask。

## 18.6 数值与调试

- softmax 用 fp32；
- 如果整行被 mask（例如 padding 全 mask），`softmax(-inf)` 会产生 NaN。
  mini 的 `CoreAttention` 用 `probs = torch.nan_to_num(probs, nan=0.0)` 把
  这种行输出为 0（MLA 同样处理），所以不要求每行都有一个有效 key；更稳妥
  的做法仍是让 padding query 行至少能看到自己，但不要依赖 mask 值“恰好”
  不产生全 `-inf`；
- dropout 在 attention probs 上，训练/推理要切换 `model.train/eval`；
- CP 的 position ids 必须是全局位置；
- GQA 的 repeat_interleave 会复制 K/V，显存增加；生产 kernel 通常避免显式复制。

## 18.7 测试

- `test_forward_backward_and_gqa`：GQA 前向/反向；
- `parallel_equiv.py --case tp`：TP attention 的 loss/梯度；
- `--case cp`：CP 的 logits 与 q/k/v/norm 的**逐元素**梯度差（`k_grad_diff`/
  `v_grad_diff` 也进入 `grad_diff`，不再只比范数）；
- `tests/unit/test_mla.py`：MLA 的因果性、latent cache 等价、padding mask、
  两条 query 路径反向（见 30.5.5）；
- 训练冒烟：TP+PP、CP+PP。

## 18.8 本章小结

- RoPE 在 CP 下必须用全局位置。
- GQA 的 KV head 切分有整除约束。
- CoreAttention 用 fp32 softmax 和显式全局 mask。
- CP 的 K/V all-gather 反向必须跨 CP 求和。
- mini 的显式 attention 是教学实现，生产用 fused kernel。

下一章把 block/stage 拼起来，并解释 PP 的层切分。
