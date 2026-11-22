# 第 26 章 推理与生成

## 26.1 训练与推理的差异

训练：

- 一次处理整条序列；
- 需要保存激活/梯度；
- 并行维度为吞吐优化。

推理：

- 自回归逐 token 生成；
- 需要 KV cache；
- 延迟/显存/吞吐的权衡不同；
- 通常用 TP/EP，少用 PP（或 prefill/decode 分离）。

mini 的 `generation.py` 实现 PP=1 的全量重算采样（没有 KV cache），用于验证
“模型能生成合法 token”；`generation_kv.py` 提供教学版 KV cache，但它仍不是
生产推理引擎（没有分页/连续批处理/prefix cache）。

## 26.2 采样

```python
logits = stage(input_ids=ids, return_logits=True).logits[:, -1, :]
logits = gather_vocab(logits)       # TP 词表分片拼接
if temperature == 0: argmax
else:
    logits /= temperature
    top_k / top_p
    probs = softmax(logits)
    next_token = multinomial(probs)
ids = cat(ids, next_token)
```

要点：

- 输出层是词表并行，采样前要 all-gather 各 rank 的 logits；
- `temperature=0` 是 greedy；
- top-k 把低于阈值的 logits 置 `-inf`；
- top-p 按累积概率裁剪，注意排序后 scatter 回原索引；
- `eos` 后把该 batch 的后续 token 固定为 eos，避免继续生成；
- `generator` 固定 seed 可复现。

## 26.3 没有 KV cache 的代价

每生成一个 token，都重新计算整条序列的 attention：

```text
第 t 步计算量 ∝ t²（无 cache）
第 t 步计算量 ∝ t（有 cache）
```

短序列/演示没问题；长序列会非常慢。生产推理必须：

- 缓存每层 K/V；
- 只计算新 token 的 Q；
- 用 PagedAttention/连续批处理管理显存；
- chunked prefill 降峰值；
- prefix caching 复用系统提示；
- speculative decoding 提速。

## 26.4 TP 推理

TP=2 的生成：

- 每个 rank 持有部分 heads/词表；
- 每步 forward 仍然做 TP all-reduce；
- 最后 gather logits 采样；
- 所有 rank 必须同步采样（相同随机数/相同 logits），否则下一步输入不一致。

mini 的 `generate` 在每个 TP rank 上运行，但 `generate()` 本身**不广播
RNG**：它接受调用方传入的 `generator`。CLI 在每个 rank 上用相同 seed 构造
generator，因此结果一致；如果调用方传入不同 seed 或 `generator=None`
（依赖全局 RNG 状态），TP rank 可能采样出不同 token，下一步输入就会分叉。
生产实现通常在 rank 0 采样后 broadcast token，从机制上消除 RNG 偏差，这比
“假设两边 RNG 同步”更稳健。

## 26.5 PP 推理

PP>1 时，每生成一个 token 都要走完整 pipeline：

```text
stage0 forward → stage1 forward → ... → sample → 把新 token 送回 stage0
```

这会有严重的流水线气泡；生产用：

- 多 batch 的 in-flight 请求填 pipeline；
- prefill/decode 分离；
- 把 PP 主要用于 prefill 或大模型多机部署；
- KV cache 按 stage 分布。

mini 直接拒绝 PP>1 的生成。

## 26.6 生成配置

```bash
python -m mini_megatron.cli generate \
  --config configs/tiny.json \
  --checkpoint checkpoints/tiny \
  --prompt "1,2,3,4" \
  --max-new-tokens 16 \
  --temperature 0.8 \
  --top-k 20 \
  --top-p 0.95
```

- prompt 是 token id 列表（不是文本）；生产会接 tokenizer；
- `--checkpoint` 可以是 checkpoint 根目录（读 latest）或 step 目录；
- TP>1 时用 `scripts/run_distributed.py --mode generate`，并确保序列长度/词表
  分片与 checkpoint 匹配：

```bash
python scripts/run_distributed.py --world-size 2 \
  --config configs/tiny_tp2.json --mode generate \
  --checkpoint checkpoints/tiny_tp2 --prompt 1,2,3,4 --max-new-tokens 8
```
- 生成阶段会把 sequence_parallel 关闭（自回归长度不保证能被 TP 整除）。

## 26.7 推理侧的前沿

- **chunked prefill**：把长 prompt 分块，降低峰值显存；
- **prefix caching**：复用相同前缀的 KV；
- **prefill-decode 分离**：不同并行策略/硬件；
- **speculative decoding**：小模型草稿 + 大模型验证；
- **batch-invariant MoE**：不同 batch 组合下路由一致；
- **expert tensor parallel**：MoE 推理的专家切分；
- **FP8/FP4 KV cache**：降显存；
- **CUDA Graph**：降 kernel 启动开销。

Megatron Core 0.20 的推理更新包括 CUDA Graph、chunked prefill、prefix
caching、speculative decoding、prefill-decode 分离等。

注意 **GDN/GDP（Gated DeltaNet / Gated Delta Product）是模型结构**，属于
SSM/线性注意力/混合模型家族，不是“动态推理”模式；它们需要 decode 侧的
状态更新，但把“支持 GDN/GDP”列成一种推理调度是分类错误（34.1 有同样的
提醒）。引用推理功能时，把“模型结构支持”和“推理执行模式”分开写。

## 26.8 mini 的局限

- `generation.py` 无 KV cache；`generation_kv.py` 有单请求 KV cache；
- 只支持 PP=1；
- 不支持 CP；
- 无 tokenizer；
- 生成路径未接入分页、批量调度、连续批处理和 prefix cache；仓库另有独立的
  `inference/paged_cache.py`（BlockAllocator + block table，见 26.9），
  但它没有接进 `generation_kv.py`；
- 采样逻辑简单。

它适合验证：

- 模型前向/词表并行是否正确；
- checkpoint 是否能加载；
- 生成 token 是否在词表范围；
- TP 下 logits gather 是否正确。

## 26.9 教学版 KV cache：`generation_kv.py`

`mini_megatron/generation_kv.py` 把“只算新 token”的核心路径实现出来：

```python
from mini_megatron.generation_kv import generate_with_cache

out = generate_with_cache(
    stage,
    torch.tensor([[1, 2, 3]]),
    max_new_tokens=16,
    temperature=0.0,
)
```

内部流程：

```text
prefill(prompt) -> logits + 每层 (K, V)
decode(next_token, cache, position_offset=past_len)
    -> 只投影新 token
    -> K/V 与历史 cache 拼接
    -> attention 的 q_start=past_len, kv_len=past_len+1
    -> 采样下一个 token
```

实现要点：

- cache 形状 `[b, n_kv_local, past_len, head_dim]`，TP 下按 KV head 切；
- RoPE 必须用**全局位置** `past_len`，不是 0；
- `SelfAttention.forward` 返回 `(out, present)`，`TransformerBlock`/`Stage`
  把每层 cache 串起来；
- PP=1、CP=1；SP 下序列长度不保证能被 TP 整除，生成时关闭；
- 测试 `test_generation_kv.py` 逐步比较 cached logits 与全量前向，并验证
  `generate_with_cache` 与 `generate` 的 greedy 输出完全一致。

它仍然不是生产推理引擎：

- 每层单独 `torch.cat`，没有 PagedAttention；
- 单请求，没有连续批处理；
- 没有 prefix cache/chunked prefill/speculative；
- 没有 CUDA Graph/量化 KV。

但“cache 的正确性”是可以严格验证的：如果位置、拼接顺序或 TP head 切分错了，
cached logits 会立刻与全量前向分叉。

### 26.9.1 MLA 的 cache 不是 K/V

`parallel/mla.py` 展示了另一种 cache 语义：存的是
`(c_kv [b,s,d_c], k_pe [b,1,s,d_R])`，不是完整 K/V。decode 时：

```text
c_kv_full = cat(past_c_kv, new_c_kv)
k_nope, v = kv_b_proj(c_kv_full).split(...)
k_pe_full = cat(past_k_pe, new_k_pe)   # 已旋转过，不能再 RoPE
scores = q_nope @ k_nope^T + q_pe @ k_pe_full^T
```

三个关键点：

1. RoPE 只作用于**新** token 的 `k_pe`；对 cached `k_pe` 再旋转会破坏历史；
2. `kv_b_proj` 每步对完整 `c_kv` 重新解压，用计算换显存；
3. 吸收（absorption）是另一种推理路径：把 `W_UK` 吸进 query、`W_UV` 吸进
   output，直接对 `c_kv` 做 attention；训练权重不要预先吸收。

`tests/unit/test_mla.py` 用 prefill+逐 token decode 与全量前向对比，误差
约 1e-7；`scripts/mla_demo.py` 打印 cache 比例。生产 MLA 还要处理 TP/CP、
分页 cache、FP8 KV 和吸收 kernel，不能把这个教学实现直接当推理引擎。

### 26.9.2 独立的分页 KV cache：`inference/paged_cache.py`

连续 `torch.cat` 的问题是：每个请求的 cache 必须是连续内存，长度变化就要
重新分配/拷贝，多请求并发时碎片严重。分页 KV cache 把逻辑序列映射到固定大小
的物理 block：

```text
逻辑位置 p -> block_table[p // block_size] 的第 (p % block_size) 个槽
```

mini 的 `PagedKVCache` 实现了数据结构与分配器（`BlockAllocator`、
`append/gather/free`、层锁步、碎片统计），但不含 paged-attention kernel。
三个不变量、33-token/31% 碎片例子、与 dense cache 的等价测试，统一放在
40.3.1；本节只强调它的**训练/生成接口**：`generation_kv.py` 的连续 cache
与 `PagedKVCache` 可以互相校验，但两者尚未合并成调度器；连续批处理、
prefix sharing 的引用计数和 paged kernel 属于生产实现（见 40.4–40.7）。

## 26.10 本章小结

- 推理的核心是 KV cache、批处理、显存/延迟权衡。
- mini 的全量重算生成适合短序列；`generation_kv.py` 演示了 cache 语义，
  但没有分页/连续批处理，仍不能用于生产长序列。
- TP 推理要 gather 词表 logits，并保证各 rank 采样一致。
- PP 推理气泡大，生产用 prefill/decode 分离/连续批处理。
- 推理优化是 2026 的竞争焦点之一。

下一章讨论测试与数值等价性：怎样证明并行实现是对的。
