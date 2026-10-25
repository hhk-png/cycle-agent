# 附录 F Transformer 推理基础

本附录给第一次接触大模型推理的读者补上最小必要的背景。它不追求覆盖 Transformer 的全部变体，只解释“一个 token 如何变成下一个 token 的概率”，以及推理引擎为什么要在这些步骤上做优化。读完本附录，你可以回到第 1、5、6 章，把 prefill/decode、KV cache、GQA、RoPE、采样等概念放回具体计算。

## F.1 从文本到 token

模型不直接处理字符串，而是处理整数 token id。tokenizer 负责把文本切成 token，并映射成 id；decode 负责把 id 还原成文本。

现代 tokenizer 通常是 BPE 或 Unigram：它从字符/字节开始，反复合并高频片段，得到一个几千到几十万大小的词表。常见的英文单词可能是一个 token，中文一个汉字可能是 1 到 3 个 token，代码里的缩进和符号也有专门的 token。

tokenizer 有三个容易忽略的细节。

第一，**特殊 token**。BOS、EOS、system、user、assistant、tool、image 等控制 token 有固定 id，只能由 chat template 插入，不能由用户文本产生。否则用户可以伪造角色，绕过系统提示。mini 的字节级 tokenizer 用 `encode_special` 区分这两种情况。

第二，**字节回退**。任何文本都应该能被编码，不会出现 `<unk>`。字节级 tokenizer 把每个字节映射成一个 token；BPE tokenizer 通常也有字节回退，保证罕见字符可编码。

第三，**流式解码**。一个 UTF-8 字符可能被拆成多个 token；如果每来一个 token 就解码，可能得到替换字符。流式输出要维护字节缓冲，只解码完整的 UTF-8 前缀。mini 的 `StreamingDetokenizer` 就是这个逻辑。

## F.2 embedding 与位置

token id 经过 embedding 表变成一个向量：`x = E[token_id]`，形状是 `[d_model]`。一个长度为 `T` 的序列，embedding 后是 `[T, d_model]`。

Transformer 本身对顺序不敏感，所以需要位置信息。早期用绝对位置编码，现代模型多用 RoPE（旋转位置编码）：它把 query/key 的维度两两分组，按位置旋转一个角度。RoPE 的好处是相对位置可以通过旋转的差表示，外推到更长上下文更方便；缺点是长上下文外推需要调频率或插值。

mini 的 `ops.py` 里可以看到 RoPE 的实现：对每一对维度 `(2i, 2i+1)`，用 `theta^(-2i/d)` 算频率，再按位置乘以角度，做二维旋转。反向传播时旋转方向相反。

## F.3 注意力：Q、K、V

每一层注意力的输入是 `[T, d_model]` 的 hidden states。通过三个线性投影得到：

```text
Q = X W_q    [T, H, d_h]
K = X W_k    [T, H_kv, d_h]
V = X W_v    [T, H_kv, d_h]
```

其中 `H` 是 query 头数，`H_kv` 是 KV 头数，`d_h` 是每个头的维度，通常 `H × d_h = d_model`。

注意力分数是 query 和 key 的点积，除以 `sqrt(d_h)` 做缩放：

```text
scores[t, s] = Q[t] · K[s] / sqrt(d_h)
```

然后加 causal mask：位置 `t` 只能看 `s <= t`，不能看未来。mask 通常把非法位置的分数设成 `-inf`，softmax 后权重为 0。

最后用 softmax 权重对 V 加权求和：

```text
P = softmax(scores + mask)
O[t] = sum_s P[t, s] V[s]
```

多头注意力的每个头独立做上述计算，最后拼接并过一个输出投影 `W_o`。

**MHA、MQA、GQA** 的区别在 K/V 的头数。MHA 每个 query 头有自己的 K/V；MQA 所有 query 头共享一组 K/V；GQA 把 query 头分组，每组共享一组 K/V。KV 头越少，KV cache 越小，但表达力可能下降。DeepSeek-V4.1 用 64 个 query 头共享 1 个 KV 头，属于极端的共享；MLA 则用低秩 latent 进一步压缩 KV。

**为什么 decode 受带宽限制**：prefill 时 `T` 很大，矩阵乘是 `[T, d] × [d, d]`，算术强度高；decode 时 `T=1`，变成矩阵向量乘，每个权重元素只参与一次乘加，算术强度低，时间主要花在读权重和 KV 上。

## F.4 FFN、RMSNorm 与残差

注意力之后是 FFN。现代 LLM 常用 SwiGLU：

```text
h = RMSNorm(x)
a = h W_1
g = h W_3
x = x + (silu(a) * g) W_2
```

`W_1` 和 `W_3` 把维度从 `d_model` 升到 `ffn_dim`，`W_2` 降回 `d_model`。`silu(x) = x * sigmoid(x)`。

RMSNorm 是：

```text
rms = sqrt(mean(x^2) + eps)
y = x / rms * w
```

它比 LayerNorm 少一个均值中心化，计算更简单，数值也稳定。

残差连接 `x = x + f(x)` 让梯度能直接流过深层网络。V4.1 的 mHC 用多个 residual stream 和可学习混合替代单一残差，表达力更强，但 kernel 布局更复杂。

## F.5 prefill 与 decode 的形状

一次生成分两个阶段。

**prefill** 输入整个 prompt，形状 `[T_prompt, d_model]`。所有 token 并行计算，attention 是 `[T, T]` 的下三角矩阵。输出是每个位置的 logits，但通常只用最后一个位置来采样第一个 token。

**decode** 每次输入一个新 token，形状 `[1, d_model]`。query 只有一行，但需要和所有历史 token 的 K/V 做 attention。历史 K/V 被缓存，所以 decode 的计算量是 `O(T)`，不是 `O(T^2)`。

这就是 KV cache 的作用：把“重新算历史 K/V”变成“读历史 K/V”。没有 KV cache，生成 1000 个 token 要把前缀算 1000 次；有了 KV cache，每个 token 只算一次 K/V。

## F.6 KV cache 的字节数

对一层、一个 KV 头、head 维度 `d_h`、每个元素 `b` 字节的模型，每个 token 的 KV 是：

```text
2 × d_h × b
```

乘上层数 `L` 和 KV 头数 `H_kv`：

```text
KV_bytes_per_token = 2 × L × H_kv × d_h × b
```

例如 `L=40, H_kv=1, d_h=128, b=1`（FP8），每 token 是 `2 × 40 × 1 × 128 × 1 = 10240` 字节，约 10 KB。128K 上下文就是 1.28 GB 左右。这个数字直接决定单卡能放多少并发、是否需要换出、前缀缓存能省多少。

压缩、低精度、SWA 会改变这个公式。DeepSeek-V4.1 公开的全局 KV 约 890 字节/token，是压缩、FP4、SWA 和 indexer 共同作用后的结果，不能直接用稠密公式算。

## F.7 logits 与采样

模型最后一层输出 `[T, vocab_size]` 的 logits。对最后一个位置，采样过程通常是：

1. 除以温度 `T`：`logits /= temperature`；
2. softmax 得到概率分布；
3. 可选 top-k：只保留概率最高的 k 个 token；
4. 可选 top-p：在概率上保留累计概率达到 p 的最小集合；
5. 重新归一化；
6. 从分布中采样一个 token。

`temperature=0` 通常等价于 argmax（贪心）。温度越高，分布越平，输出越随机；温度越低，分布越尖，输出越确定。

采样之后，token 被追加到序列，下一次 decode 用它作为输入。停止条件包括 EOS、stop token、stop 字符串、最大长度。

## F.8 一个最小的 NumPy 前向

下面这段代码不是完整模型，只展示单头注意力的形状和计算。mini 的 `model/ops.py` 和 `model/tiny_transformer.py` 是它的完整版本。

```python
import numpy as np

def softmax(x, axis=-1):
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=axis, keepdims=True)

def attention(x, wq, wk, wv, wo):
    T, d = x.shape
    q = x @ wq              # [T, d]
    k = x @ wk              # [T, d]
    v = x @ wv              # [T, d]
    scores = q @ k.T / np.sqrt(d)   # [T, T]
    mask = np.triu(np.ones((T, T), dtype=bool), k=1)
    scores = np.where(mask, -np.inf, scores)
    probs = softmax(scores, axis=-1)
    return (probs @ v) @ wo
```

这段代码每次调用都重算整个前缀，没有 KV cache，也没有多头、RoPE、RMSNorm。它的价值是让你看清“QK^T -> mask -> softmax -> V”的主线；真实模型只是把这条主线并行化、分块、融合，并缓存历史 K/V。

## F.9 一次前向的形状账本

公式容易记，形状容易错。这一节用 mini-deepinfer 的 checkpoint 把一次前向的每个张量写出来，再算一遍参数量和 KV。你可以在纸上跟着画，遇到任何推理代码都用同样的方法核对。

checkpoint 的配置是：2 层，`d_model=64`，4 个 query 头，2 个 KV 头，`head_dim=16`，FFN 128，词表 264，`max_seq_len=512`，第一层使用窗口 64 的 SWA。输入是 59 个 token，所以 `T=59`。

embedding 之后，hidden states 的形状是 `[59, 64]`。每一层先做 RMSNorm，形状不变；然后三个投影：`W_q` 把 64 维映射到 `4×16=64` 维，`W_k` 和 `W_v` 各映射到 `2×16=32` 维。把输出拆成头，得到 Q `[59, 4, 16]`、K `[59, 2, 16]`、V `[59, 2, 16]`。注意 Q 的总维度仍然是 64，K/V 的总维度只有 32；GQA 省的是 K/V 的投影和缓存，不是 Q。

RoPE 作用在 Q 和 K 的每个头上，形状不变。attention 分数由 Q 和 K 做点积得到：每个 query 头要和所有 key 位置比较，所以分数矩阵是 `[4, 59, 59]`；缩放因子是 `sqrt(16)=4`。causal mask 把上三角置为负无穷，softmax 之后每一行之和为 1。用这些权重对 V 加权，得到每个头的 context `[59, 16]`，四个头拼回 `[59, 64]`，再过一个 `W_o` 投影回 64 维。

残差之后是 FFN。SwiGLU 有两个升维矩阵 `W_1` 和 `W_3`，都把 64 维映射到 128 维；两者逐元素相乘后，再由 `W_2` 降回 64 维。所以 FFN 的中间张量是 `[59, 128]`，最终输出仍是 `[59, 64]`。第二层重复同样的过程，只是没有 SWA 窗口。最后 RMSNorm，再和 token embedding 共享的 `lm_head` 相乘，得到 `[59, 264]` 的 logits；推理只取最后一个位置 `[264]` 去采样。

现在算参数量。token embedding 是 `264×64=16,896`。每层 attention 的 Q 投影是 `64×64=4,096`，K 和 V 各 `64×32=2,048`，O 投影 `64×64=4,096`，合计 12,288。每层 FFN 是三个 `64×128` 矩阵，合计 `3×8,192=24,576`。两个 RMSNorm 各 64 个参数，合计 128。一层约 36,992 个参数，两层 73,984；加上 embedding、最终的 norm 和（共享的）输出头，总量约 90K，和文档里的“约 90K 参数”一致。这个账本的好处是：如果某个实现的参数量对不上，通常不是“大约”的问题，而是某个投影的输入输出维度写反了，或者 GQA 的头数用错。

再算 KV。每层每个 token 存 K 和 V，各 `2×16=32` 个 float32，也就是 `32×4×2=256` 字节；两层就是 512 字节/token。59 个 token 约 30 KB。`EngineConfig` 的默认是 256 个 block、每块 16 token，全部装满时是 `256×16×512=2,097,152` 字节，正好 2 MiB；`examples/lifecycle.py` 为了演示方便用 32 个 block，对应 `32×16×512=262,144` 字节。运行 `engine.stats()["kv_cache_bytes"]` 时，先确认你用的是哪套配置，两个数都对，但不是同一个配置。这个等式是检查分页 KV 实现的最快方法：如果 `memory_bytes()` 和“block 数 × block size × 每 token 字节”对不上，说明有一层在重复计算或漏算。

最后看计算量。decode 一个 token 的线性层约 `2×90K≈180K` FLOP，attention 约 `2×2×59×64≈15K` FLOP；两者在同一量级。也就是说，玩具模型的 decode 既不是明显的算力瓶颈，也不是明显的带宽瓶颈；它跑得慢主要是因为 Python 和 NumPy 的固定开销。生产模型完全不同：40 层、16B 激活参数、FP8 权重，每 token 线性层约 32 GFLOP，权重要读 16 GB，带宽时间比算力时间大两个数量级。这个对比说明，**同一个“decode”在不同规模下是不同的物理问题**，不能把玩具上的优化直觉直接搬到生产。

形状账本还能解释几个常见 bug。第一，如果 K/V 的头数写成 4 而不是 2，KV 字节会翻倍，但输出形状可能仍然“看起来对”，只有数值和显存对不上。第二，如果 `head_dim` 和 `d_model/H` 不一致，Q 拼接后的维度就不是 64，`W_o` 会报形状错误；有些实现用 `view` 而不是 `reshape`，会得到一个更难查的 stride 错误。第三，如果 SWA 窗口作用在错误的层，第一层的 attention 会看到超过 64 个 token，输出仍然能跑，但和参考实现对不上。形状账本不能替代测试，但它能在写测试之前告诉你“应该是什么形状”。

## F.10 常见混淆

**“参数量等于每 token 计算量”**：只对稠密模型近似成立。MoE 的参数量很大，但每 token 只激活 top-k 个专家；Engram 的参数量更大，但按哈希查表，不参与稠密矩阵乘。

**“KV cache 是模型的一部分”**：KV cache 是运行时状态，不是权重。它随请求增长、随请求结束释放，可以被前缀缓存共享，也可以被换出到存储。

**“batch 越大越好”**：batch 提高吞吐，但会增加单步时间、KV 占用和尾延迟。最优 batch 取决于 SLO、上下文长度和硬件。

**“温度 0 就是确定性”**：不同框架对 temperature 的处理不同，浮点误差、top-k 并列、batch 归约顺序都可能让输出变化。严格确定性需要 batch-invariant kernel 和固定随机数路径。

**“缓存命中就能直接 decode”**：命中长度必须是 block 的整数倍，SWA 状态可能缺失，decoder 侧可能还需要重放。缓存命中只是“少算一部分”，不是“完全不用算”。

## 小结

Transformer 推理的主线是：token -> embedding -> 多层 attention/FFN -> logits -> 采样。prefill 并行处理 prompt，decode 逐 token 生成；KV cache 把历史 K/V 留下来，避免重复计算；GQA/MQA/MLA 压缩 KV；RoPE 提供位置；采样把 logits 变成 token。理解这些形状和公式，后面的调度、KV 管理、kernel 优化才有落脚点。

## 练习

1. 一个 32 层、`d_model=4096`、8 个 KV 头、head 维度 128、FP16 KV 的模型，每 token KV 是多少字节？64K 上下文是多少？
2. 写出 causal mask 的作用。如果去掉 mask，prefill 的输出会有什么问题？
3. 解释 prefill 和 decode 的算术强度为什么不同。用一个具体矩阵形状算一算。
4. 温度、top-k、top-p 分别如何改变采样分布？温度 0 时为什么通常用 argmax？
5. 用 F.8 的代码跑一个 4 token 的序列，打印 attention 矩阵，确认它是下三角的。
