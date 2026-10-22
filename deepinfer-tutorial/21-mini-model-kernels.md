# 第 21 章 玩具模型、Tokenizer 与反向传播

本章导读：本章进入 mini-deepinfer 的模型层。你会看到字节级 tokenizer、2 层 GQA Transformer、RoPE/RMSNorm/SwiGLU、分页 KV 前向，以及一份不依赖自动微分的手写反向传播。最后介绍 `v4lite.py` 里的 MoE、Engram、CED、mHC 机制实验室，以及量化、稀疏注意力和视觉编码实验室。

## 21.1 字节级 tokenizer

`mini_deepinfer/tokenizer.py` 用 256 个字节作为基础词表，再加 8 个特殊 token（pad、bos、eos、user、assistant、end、image、tool），总词表 264。`encode` 把字符串编码成 UTF-8 字节，`decode` 把字节拼回字符串。

这个设计有两个好处。第一，任何文本都能编码，不会出现 `<unk>`；中文一个字符是 3 个字节，模型会把它当成 3 个 token，虽然效率不高，但语义上无损。第二，特殊 token 安全：`encode("<|user|>")` 返回的是普通字节序列，不是控制 id；只有 `encode_special("<|user|>")` 或 `apply_chat_template` 才会产生控制 id。这对应 DeepSeek Recipe 的“防止用户文本注入控制 token”。

`StreamingDetokenizer` 处理流式输出。字节级 tokenizer 可能把一个多字节字符拆成多个 token（例如中文“你”是 3 个字节），如果每来一个 token 就 `decode`，会得到替换字符。`StreamingDetokenizer` 维护一个字节缓冲，只解码完整的 UTF-8 前缀，把不完整的尾部留到下一次。测试 `test_streaming_detokenizer_handles_split_utf8` 覆盖了这个场景。

字节级 tokenizer 的代价是序列变长。一个英文单词可能被拆成 5 个字节，一个中文字符 3 个字节；这会让同样的文本消耗更多 token，推理成本更高。生产 tokenizer 用 BPE/Unigram 把常见子词合并成一个 token，牺牲词表大小换序列长度。mini 选择字节级，是为了让“特殊 token 安全”和“流式解码”这两个机制更容易讲清楚。

## 21.2 模型结构

先区分两个概念：`ModelConfig` 的**默认值**和**已训练 checkpoint 的配置**不是一回事。`config.py` 里的默认值（3 层、`d_model=64`、FFN 192、窗口 32）是给新建模型用的；`assets/tiny_lm.npz` 里保存的 `config_json` 才是实际加载的配置。checkpoint 是 2 层、`d_model=64`、4 个 query 头、2 个 KV 头、`head_dim=16`、FFN 128、最大长度 512、窗口 64、第一层用 SWA，参数量约 90K。读代码和跑示例时，以 checkpoint 的配置为准。

每层的前向：

```text
h = RMSNorm(x)
q = h @ Wq           -> [T, H, D]
k = h @ Wk           -> [T, Hkv, D]
v = h @ Wv           -> [T, Hkv, D]
q, k = RoPE(q, k)
K, V = kv_cache.gather_layer(layer, seq, end)
K, V = expand_kv(K, V, H)   # GQA
ctx = softmax(q K^T / sqrt(D) + mask) V
x = x + ctx @ Wo
h = RMSNorm(x)
x = x + SwiGLU(h)
```

`TinyTransformer._attention` 把 paged KV 的读写放在每层：`kv_cache.write_layer(layer, seq, start, k, v)` 写入当前 chunk 的 K/V，`kv_cache.gather_layer(layer, seq, end)` 读出从位置 0 到 `end` 的所有 K/V。mask 由 `causal_mask(positions, key_len, window)` 生成，第一层窗口 64，其余层全注意力。

`forward_full` 不做 paged KV，直接把当前序列的 K/V 用于 attention，供训练和简单推理使用。`forward_paged` 走分页路径，支持 chunked prefill 和推测解码的 `token_ids` 覆盖。测试 `test_paged_forward_matches_full_forward` 比较两者的 logits，最大误差在 1e-5 以内。

GQA 的实现是 `expand_kv`：把 2 个 KV 头重复成 4 个 query 头。这个重复是逻辑上的，不复制 KV cache；真正的 kernel 会在 attention 里按头索引。mini 用 `np.repeat` 复制，方便阅读，但会增加内存；对 90K 模型无所谓，对生产模型不可接受。

## 21.3 为什么手写反向传播

mini 没有 PyTorch，也没有 autograd。`training.py` 手写了 RMSNorm、RoPE、softmax、attention、SwiGLU 的前向和反向。原因有三：

1. **可复现**：读者只需要 NumPy，不需要安装深度学习框架；
2. **透明**：每个梯度都能对应到一个公式，方便理解显存和计算；
3. **可验证**：可以用有限差分检查每个参数的梯度。

反向传播的关键步骤：

- **RMSNorm**：`y = x/rms * w`，`dx = (w*dy)/rms - x * mean(w*dy*x)/rms^3`；
- **RoPE**：前向是旋转，反向是反向旋转；
- **softmax**：`dscores = probs * (dprobs - sum(dprobs*probs))`；
- **GQA**：前向把 KV 头重复到 query 头，反向把重复的梯度按组求和；
- **SwiGLU**：`act = silu(a) * gate`，`da = dact * gate * silu'(a)`，`dgate = dact * silu(a)`；
- **tied embedding**：输出投影和 token embedding 共享权重，梯度要累加。

`tests` 里没有放有限差分测试（它比较慢），但开发时用一个小配置做过完整检查：每个参数的数值梯度和解析梯度在 1e-5 量级内一致。这个检查是训练能收敛的前提。

手写反向传播还有一个教学价值：它让你看到“推理时不需要的梯度”在训练时占多少显存。一个 2 层模型的激活和梯度可能比权重还大；这也是为什么训练和推理的系统优化方向不同。

## 21.4 训练脚本

`train_tiny.py` 生成合成语料，训练 2 层模型：

```bash
python train_tiny.py --steps 4000 --chars 160000 --out mini_deepinfer/assets/tiny_lm.npz
```

训练使用 Adam、warmup + cosine 学习率、梯度裁剪。每 100 步打印训练/验证 loss，每 500 步生成一个样本。最终 loss 约 0.21 到 0.28，样本能续写教程里的短提示，但经常出现拼写错误。这是 90K 参数模型的极限，不是引擎的问题。

draft 模型是 1 层、同样词表的模型，训练 2200 步，loss 约 0.30：

```bash
python train_tiny.py --layers 1 --steps 2200 --chars 160000 --out mini_deepinfer/assets/draft_lm.npz
```

它的输出比目标模型差，但足以在推测解码里获得 80% 左右的接受率（取决于提示）。当前示例的一次运行接受率是 85.7%，平均接受长度 3.43；这里的 3.43 是“每步接受的 draft 数”，不含目标模型自己额外产出的那 1 个 token，所以每步实际推进的 token 数约为 4.43。重新训练会改变 loss 和接受率，但不会改变推测解码的控制流。

训练脚本不追求语言质量，它的作用是“生成一个能跑的 checkpoint”。如果你重新训练，loss 和样本会变，但引擎行为不变；不要把训练 loss 当成引擎正确性的指标。

还要注意“直接运行脚本”和“书里的显式命令”不是同一组超参数。`train_tiny.py` 的默认值是 2500 步、120000 字符；书里为了复现 0.21 到 0.28 的 loss，显式写了 4000 步、160000 字符。如果你只跑 `python train_tiny.py`，得到的 loss 和样本会不同，这是预期行为。仓库没有保存训练日志，所以书里的 loss 只能当作“这个量级”，不能当作逐位可复现的断言；真正要复现，应该固定随机种子、记录命令和 NumPy 版本。

## 21.5 V4 机制实验室

`mini_deepinfer/model/v4lite.py` 提供四个结构。

**MoE/Engram**：`init_v4lite_weights` 在 dense 权重上增加 gate、共享专家、路由专家和 Engram 表；`build_v4lite_model` 返回一个 `TinyTransformer`，`use_moe=True`、`moe_layers=(1,)`、`use_engram=True`。`forward_full` 会走 `_moe` 和 `_engram`，返回每个专家的 token 计数。测试验证 32 个 token、top-2 时计数总和是 64，并且至少有两个专家被使用。

**CED**：`CEDTransformer` 实现 causal encoder-decoder。前 `n_encoder_layers` 层做完整 causal attention，得到 `H_e`；`ced_k`/`ced_v` 把 `H_e` 投影成全局 K/V；decoder 层把局部 SWA 和全局 K/V 拼起来做 attention。测试验证前缀因果性：输入变长后，前 3 个 token 的 logits 不变。

**mHC**：`MHCResidual` 用两个 residual stream 和一个 2×2 mix 做残差混合，输出形状不变。它只是结构演示，没有训练。

这些实验室的权重是随机初始化的，输出没有语言质量。它们的作用是让你在没有 552B 模型的情况下，观察 MoE 路由、Engram 查表、CED 全局 KV 和 mHC 混合的形状与数值行为。

## 21.6 量化、稀疏注意力与视觉编码实验室

模型层之外，mini 还有三个机制实验室，用来补齐 V4.1 的“外围结构”。

`quantization.py` 实现了 FP8 的对称 per-channel 量化和 MXFP4 风格的 E2M1 块量化。FP4 使用 16 个码字（8 个正数幅值 + 符号位），每 16 个值共享一个 2 的幂 scale。`kv_bytes_per_token` 把层数、KV 头数、head 维度、bit 数和 scale 开销放在一起，方便比较 FP16/FP8/FP4 的 KV 占用。测试检查误差上界和字节单调性。

`sparse_attention.py` 实现块稀疏注意力：indexer 给每个 key block 打分，每个 query 选 top-k 个 block，主 attention 只在选中的 block 上计算。`top_k >= num_blocks` 时它必须和 dense causal attention 数值一致；测试覆盖了这一点，也覆盖了 top-k 子集。它对应 CSA2 的“候选池 + top-k”思路，但没有实现 Full/Reindex/Reuse 的跨层复用。

`vision.py` 是一个迷你 ViT：patchify、线性 patch embedding、可学习位置编码、一层非 causal attention、SwiGLU、aligner。它把 16×16 的图像变成 16 个 token，再投影到 LLM 的 hidden 维度。测试检查 patch 数、确定性、不同图像产生不同 token。它对应 EPD 中的 VE 阶段，但没有 2D-RoPE、pixel-unshuffle 和真正的 DeepSeek-ViT 权重。

这三个实验室都是“结构可运行、性能不追求”。它们让第 5、6、10、15 章的架构、精度和并行讨论有了可执行的对照物，而不是只停留在文字。

## 21.7 代码阅读顺序

建议按这个顺序读模型代码：

1. `ops.py`：RMSNorm、RoPE、softmax、attention；
2. `tiny_transformer.py`：`_attention`、`_run`、`forward_paged`；
3. `training.py`：`forward_train`、`loss_and_grads`；
4. `v4lite.py`：MoE、Engram、CED、mHC；
5. `quantization.py`、`sparse_attention.py`、`vision.py`：外围实验室；
6. `train_tiny.py`：训练循环和 checkpoint 保存。

读 `_run` 时，重点看 `start`/`end` 和 `kv_cache.write_layer`/`gather_layer` 的配合。读 `loss_and_grads` 时，重点看 GQA 的 repeat/sum 和 RoPE 的反向旋转。读 `v4lite.py` 时，重点看 CED 的 global K/V 只投影一次。

## 21.8 数值稳定性的边界

mini 的主生成路径仍然是 float32，`quantization.py` 的 FP8/FP4 只是参考实验室，没有接进 decode；所以很多数值问题被掩盖了。换成 FP16/FP8 后，同样的代码会暴露不同的失败模式。

**softmax 溢出**。如果 logits 很大，`exp` 会溢出；mini 先减最大值，所以稳定。低精度下，减最大值后的残差可能仍然超出范围，需要 kernel 里做分段或缩放。

**RMSNorm 的 eps**。`eps=1e-5` 在 float32 下足够；FP16 下，`x^2` 的累加可能溢出或下溢。生产 kernel 通常用 FP32 累加，或者对输入做缩放。

**RoPE 的频率**。`theta^(-2i/d)` 在长上下文会变得很小，float32 下可能下溢；RoPE 表通常预计算并缓存，用 float32 或更高精度。超过 `max_seq_len` 的位置会越界，mini 只检查 `end <= len(ids)`，不检查 `positions < max_seq_len`；生产引擎要在调度层拒绝超长请求。

**mask 全 False**。如果一个 query 没有任何可见 key，softmax 分母是 0，输出 NaN。mini 的 causal mask 保证每个 query 至少看到自己；稀疏 attention 如果 top-k 选到未来 block，可能全 False。`sparse_attention.py` 在选择前把未来 block 的分数设为 `-inf`，避免这个问题。

**梯度检查**。手写反向传播最容易错；mini 开发时用有限差分检查过每个参数的梯度，但没放进 pytest（太慢）。修改 `training.py` 后应该手动跑一次，否则训练可能“loss 下降但梯度是错的”。

这些边界不是要你在 mini 上全部实现，而是提醒你：同一个数学，在不同精度和硬件上需要不同的数值策略。第 10 章和第 18 章会展开。

## 21.9 量化实验室的数值细节

`quantization.py` 不是 IEEE FP8/FP4 编码器，而是一个“scale 布局 + 误差”的参考实现。理解它的具体约定，才能正确解释测试里的误差上界。

FP8 路径用对称量化：对每个通道（2D 以上张量按最后一维分组）取 `amax`，令 `scale = max(amax/127, eps)`，把 `x/scale` 四舍五入并截断到 `[-127, 127]`，返回 int8 code、scale 和反量化结果。它复用了 E4M3 的数值范围，但没有实现 E4M3 的指数/尾数位布局；所以它能回答“scale 和误差的关系”，不能回答“真实 E4M3 的舍入行为”。对称量化的最大绝对误差是 `scale/2`（四舍五入到最近整数），这个上界可以直接从代码推出来。

FP4 路径用 MXFP4 风格的块量化：每 `block_size`（默认 16）个连续值共享一个 2 的幂 scale，码字是 E2M1 的 16 个值（正负各 8 个：0、0.5、1、1.5、2、3、4、6）。code 的低 3 位是幅值索引，最高位是符号；反量化时先查表，再乘 scale。因为 scale 是 2 的幂，硬件可以用指数移位代替乘法；代价是块内动态范围被压缩，接近 0 的值可能落回 0。`quantization_error` 返回 `max_abs`、`mean_abs` 和 `relative_l2`，测试用它们检查“FP4 误差大于 FP8、但相对 L2 在可接受范围”。

`kv_bytes_per_token` 把层数、KV 头数、head 维度、bit 数、压缩比和 scale 开销放在一起。公式是 `values = 2 × L × H_kv × d_h / compression_ratio`，`data = values × bits/8`，`scales = values/scale_group × scale_bytes`。这个公式和附录 B 的 KV 公式一致，但它假设 K 和 V 的维度相同、scale 按值数均摊；真实格式还有对齐、padding、per-16 与 per-32 混用，所以它只能做量级比较。测试里的“字节数随 bit 数单调下降”是一个弱断言，不能证明真实 kernel 的布局正确。

这三个函数的价值是让你在没有 GPU 的情况下，亲手看到“量化不是免费的”：FP8 把每个值压到 1 字节，但引入 `scale/2` 的误差；FP4 再压一半，误差和块内动态范围的问题更明显；KV 字节公式让你能算“省下的显存”和“损失的质量”之间的交换。第 10 章把这些观察放回生产验收矩阵，第 18 章解释真实 kernel 为什么要融合反量化。

## 小结

mini 的模型层是一个真实的、可训练的 2 层 GQA Transformer，带字节 tokenizer、RoPE、RMSNorm、SwiGLU、分页 KV 和手写反向传播。它证明了引擎机制可以在真实数值上运行，但没有复制生产 kernel 的性能和低精度。`v4lite.py` 用随机权重补上了 MoE、Engram、CED、mHC 的结构演示。下一章进入分页 KV 和调度器的实现细节。

## 练习

1. 解释 `forward_full` 和 `forward_paged` 的区别，为什么两者的 logits 应该一致。
2. 手写 RMSNorm 的反向公式，并用一个小例子验证 `dx` 的符号。
3. 如果要把 toy 模型从 2 层扩到 4 层，训练时间大约怎么变化？显存/内存呢？
4. 为什么 `ModelConfig` 的默认值和 checkpoint 的配置要分开？如果不分开，会出什么问题？
