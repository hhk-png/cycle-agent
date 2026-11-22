# 第 17 章 张量并行层实现

本章逐行解释 `mini_megatron/parallel/layers.py`。生产 Megatron 的
`tensor_parallel/layers.py` 有更多 kernel/通信融合，但核心形状与语义相同。

## 17.1 ColumnParallelLinear

构造：

```python
self.output_size_per_partition = divide(output_size, world)
self.weight = nn.Parameter(torch.empty(local_out, input_size))
```

即权重按 **输出行** 切。前向：

```python
if self.sequence_parallel:
    x = gather_from_sequence_parallel_region(x)
return _ColumnParallelLinearFn.apply(x_use, w, bias, gather_output, tp_group)
```

- 序列并行时先把序列 all-gather 成完整长度；
- `gather_output=True` 时把各 rank 的输出拼起来（mini 很少用，输出层用
  `gather_output=False`）；
- 否则下游 Row Parallel 直接消费本地分片。

### 自定义 autograd

```python
class _ColumnParallelLinearFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, weight, bias, gather_output, group):
        out = F.linear(x, weight, bias)
        if gather_output and world > 1:
            out = gather_from_tensor_parallel_region(out)
        return out

    @staticmethod
    def backward(ctx, grad_output):
        if gather_output:
            grad_output = chunk(grad_output, world, dim=-1)[rank]
        grad_input = grad_output.matmul(weight)
        grad_input = all_reduce(grad_input, group)
        grad_weight = grad_output.reshape(-1, out).T @ x.reshape(-1, in)
        grad_bias = grad_output.reshape(-1, out).sum(0)
        return grad_input, grad_weight, grad_bias, None, None
```

为什么不用普通 `F.linear`？因为普通 autograd 只知道本地权重，算出的
`grad_input` 是**部分和**。必须 all-reduce 才能得到完整输入梯度。

### autocast 兼容

在 autocast 下 `grad_output` 可能是 bf16，而权重是 fp32。mini 在 backward 中：

```python
work_dtype = weight.dtype
grad_output_w = grad_output.to(work_dtype)
grad_input = grad_output_w.matmul(weight).to(x.dtype)
```

否则会报 `expected m1 and m2 to have the same dtype`。生产 TE 的 fused kernel
会内部处理 dtype。

## 17.2 RowParallelLinear

构造：

```python
self.input_size_per_partition = divide(input_size, world)
self.weight = nn.Parameter(torch.empty(output_size, local_in))
```

权重按 **输入列** 切。前向：

```python
out = F.linear(x, weight, None)
if sequence_parallel:
    out = reduce_scatter_to_sequence_parallel_region(out)
elif world > 1:
    out = reduce_from_tensor_parallel_region(out)
if bias is not None:
    out = out + bias
```

- 普通 TP：all-reduce 得到完整输出；
- SP：`_ReduceScatterToSequenceParallelRegion` 在 mini 里的实现是
  **all-reduce + 按序列 `chunk`**，不是真正的 reduce-scatter。结果数值等价，
  但会物化完整序列，通信/显存都不是最优；生产 Megatron 用原生
  reduce-scatter（且张量布局是 `[seq, batch, hidden]`，在 dim 0 上切）；
- 偏置在 reduce 后加一次（普通）或加在本地序列块（SP）。

### SP 偏置的梯度

SP 下 bias 是复制的，每个 rank 只看到 `1/t` 的 token。前向正确，但 bias 梯度
需要跨 TP 求和：

```python
self.bias.register_hook(lambda g: all_reduce(g, group=tp_group))
```

mini 只在 `sequence_parallel and world > 1` 时注册这个 hook。非 SP 路径下，
bias 是在 TP all-reduce **之后**才加的，每个 rank 看到的是完整输出，梯度
天然一致，不需要额外 hook。`tp_bias` 等价性测试专门检查 out_proj bias 和
LayerNorm 权重的梯度。

## 17.3 VocabParallelEmbedding

```python
vocab_start = tp_rank * (vocab // tp)
vocab_end = vocab_start + vocab // tp
mask = (ids >= vocab_start) & (ids < vocab_end)
local_ids = (ids - vocab_start).masked_fill(~mask, 0)
out = embedding(local_ids, weight)
out = out * mask.unsqueeze(-1).to(out.dtype)   # [b,s,h] * [b,s,1]
out = reduce_from_tensor_parallel_region(out)  # 各 rank 的部分和相加
if sequence_parallel:
    out = scatter_to_sequence_parallel_region(out)
```

语义：

- 每个 rank 只保存 `V/t` 行；
- 输入 ids 是完整的（SP 下也是完整 ids，输出再 scatter）；
- 本地 mask 把不属于本 rank 的 token 置零；
- all-reduce 得到完整 hidden；
- SP 再把序列切开。

为什么 `input_ids` 不先切？因为 embedding 权重的词表行是分片的；每个 rank 需要
看到所有 token 才能知道哪些 token 落在自己的词表范围。先切 ids 会让每个 rank
只处理部分 token，权重梯度不完整。生产实现也是这个语义。

## 17.4 MLP

mini 的 SwiGLU：

```python
gate = F.silu(self.gate_proj(x))
up = self.up_proj(x)
return self.down_proj(gate * up)
```

- `gate_proj`、`up_proj` 是 Column Parallel：输出 `ffn/t`；
- `down_proj` 是 Row Parallel：输入 `ffn/t`，输出 `h`；
- gate/up 用两个独立层，保证同一位置的元素在同一个 rank 上；
- **SP 下这两个 Column Parallel 的 `forward` 会各自做一次 sequence
  all-gather**。因为同一个 `x` 被两个层分别 gather，mini 实际做了两次相同的
  all-gather；生产实现通常把 gate/up 融成一个 GEMM（或复用 gather 结果）来
  避免重复通信。这里为了可读性保留了重复，但在性能剖析时要把它算进 TP
  通信量。

如果融合 gate/up 成一个 `[h, 2ffn]` 的 Column Parallel，必须保证每个 rank 的
本地输出同时包含 gate 和 up 的对应块（交错布局），否则逐元素乘会错位。mini
选择两个层，牺牲一个 kernel 换可读性。

GELU 版本：

```python
return self.fc2(F.gelu(self.fc1(x), approximate="tanh"))
```

## 17.5 初始化

- 权重 `normal_(0, init_method_std)`；
- bias 置零；
- 输出层通常按 `1/sqrt(2L)` 缩放（mini 省略）；
- **分片权重按 TP rank 偏移初始化**：Column/Row/Vocab 的本地分片形状相同，
  如果所有 rank 用同一 seed，会得到相同的权重块，拼接后不是“切开的矩阵”，
  而是“同一块复制 tp 次”。mini 在建模前调用
  `set_model_parallel_init_seed(seed)`，分片层使用
  `seed + 10007·(tp_rank+1) + 20011·pp_rank + 7919·layer_counter` 的本地
  generator；
- **复制参数在 TP 组内一致、PP stage 间不同**：position embedding、router
  等复制层使用不带 TP 偏移、但带 PP 偏移的流；norm 的默认初始化本身无随机性；
- DP 副本之间：同一 `(tp_rank, pp_rank)` 的副本得到相同初始化，梯度同步后
  仍然一致；
- 加载检查点时用 `sharding.py` 从全量 state dict 切片。

一个常见误区是“所有 rank 同一个 seed 就对了”。对**复制**参数是对的；对
**分片**参数是错的。生产 Megatron 用 model-parallel RNG tracker 做更完整的
区分（见附录 G），mini 用一个小计数器实现同样的关键性质。

对应的测试是 `tests/unit/test_init.py`：不同 TP rank 的分片必须不同，同一
rank 重置 seed 后必须可复现，同一 rank 的不同层不能重复，复制参数跨 TP 必须
一致。

## 17.6 形状表

| 层 | 输入 | 权重 | 输出 |
|---|---|---|---|
| Column | `[b,s,h]` | `[out/t,h]` | `[b,s,out/t]`（或 gather） |
| Row | `[b,s,in/t]` | `[out,in/t]` | `[b,s,out]`（SP 时 reduce-scatter 后 `[b,s/t,out]`） |
| Embedding | `[b,s]` ids | `[V/t,h]` | `[b,s,h]`（SP 后 scatter 成 `[b,s/t,h]`） |
| LM head（SP gather 序列） | `[b,s/t,h]` → gather → `[b,s,h]` | `[V/t,h]` | `[b,s,V/t]` |
| LM head（不 gather） | `[b,s/t,h]` | `[V/t,h]` | `[b,s/t,V/t]` |

## 17.7 测试

- `test_linear_world_one_matches_torch`：world=1 的基础数值；
- `test_init.py`：TP 分片初始化不重复、可复现、复制参数一致；
- `parallel_equiv.py --case tp`：TP=2 vs TP=1 的 loss/梯度；
- `--case tp_bias`：带 bias + SP 的 norm/bias 梯度；
- 训练冒烟：TP=2、TP+PP、TP+DP。

如果 TP 的 loss 对、梯度错，优先检查：

1. Column 的 `grad_input` all-reduce；
2. gather_output 的 backward split；
3. SP 的 reduce-scatter 维度；
4. 复制参数的 hook；
5. 权重切片的行/列方向。

## 17.8 本章小结

- Column 切输出、Row 切输入；Embedding 切词表。
- Column 的反向 all-reduce 是正确性的关键。
- SP 下 Row 的 reduce-scatter 切序列，偏置/norm 需要梯度 hook。
- GLU 的 gate/up 必须位置对齐。
- 初始化要保证“逻辑模型一致”：分片参数按 TP/PP rank 偏移，复制参数在 TP 组内
  一致；不能简单地让所有 rank 共用一个 seed（见 17.5 与附录 G）。

下一章进入 attention：RoPE、GQA、causal mask 与 CP。
