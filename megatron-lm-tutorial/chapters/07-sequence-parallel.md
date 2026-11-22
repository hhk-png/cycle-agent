# 第 07 章 序列并行：让 LayerNorm 也不再复制

## 7.1 张量并行没有解决什么

TP 把线性层的权重切开了，但每个 rank 仍然持有：

- 完整的 LayerNorm 输入/输出；
- 完整的 Dropout mask；
- 完整的残差流；
- 完整的 attention 序列（除非 CP）。

对长序列，这些激活可能比权重还大。序列并行（Sequence Parallel, SP）的核心
想法是：**在 TP 组内，把激活沿序列维切开**，让 LayerNorm/Dropout/残差只处理
`1/t` 的 token；线性层需要完整序列时再临时 all-gather，算完 Row Parallel 后
reduce-scatter 回去。

这不会减少 GEMM 的计算量，但显著降低激活显存，并让逐元素算子也参与并行。

## 7.2 布局变化

普通 TP：

```text
X: [b, s, h]            # 每个 TP rank 都有一份完整 X
Column: [b, s, h/t]
Row:    [b, s, h]       # all-reduce 后完整
```

序列并行：

```text
X: [b, s/t, h]          # 每个 TP rank 持有连续的一段序列
Column: 先 all-gather 成 [b, s, h]，输出 [b, s, h/t]
Row:    输出 [b, s, h]，再 reduce-scatter 成 [b, s/t, h]
LayerNorm/Dropout/残差: 直接在 [b, s/t, h] 上做
```

关键不变量：

- **每个 token 的 hidden 维是完整的**，所以 LayerNorm 不需要通信；
- 序列块按 TP rank 连续切分（`torch.chunk` 的顺序）；
- all-gather/reduce-scatter 必须作用在 **序列维**，不是 hidden 维。

## 7.3 前向/反向的四个集合通信

一个 Transformer block 在 SP 下的通信：

```text
输入 X [b, s/t, h]
  │
  ├─ Column Parallel: all-gather 序列 → [b, s, h] → GEMM → [b, s, h/t]
  │
  ├─ Attention/activation → Row Parallel
  │     GEMM → [b, s, h] → reduce-scatter 序列 → [b, s/t, h]
  │
  └─ 残差 + LayerNorm 在 [b, s/t, h] 上
```

反向：

- Column 的 all-gather 对应 split（把梯度按序列块切回）；
- Row 的 reduce-scatter 对应 all-gather（把梯度拼回完整序列）；
- Column 的 `grad_input` 还要跨 TP all-reduce（第 06 章）；
- Row 的偏置、LayerNorm 参数是复制的，梯度要跨 TP 求和。

## 7.4 为什么容易写错

### 错误 1：reduce-scatter 切错维度

在 mini 的 `[b, s, h]` 布局下，Row Parallel 的输出是 `[b, s, h]`。序列并行要得到
`[b, s/t, h]`，应当是：

```python
reduced = all_reduce(out, group=tp_group)
local = torch.chunk(reduced, t, dim=1)[rank]
```

如果误用“对最后一维 reduce-scatter”，会得到 `[b, s, h/t]`，残差相加时序列长度
对不上，报错很直接。更隐蔽的是：某些实现把张量转置成 Megatron 的
`[s, b, h]` 布局，reduce-scatter 的 dim 变成 0；照抄代码时一定要确认布局。

### 错误 2：忘记复制参数的梯度

SP 下 LayerNorm 权重、Row Parallel 偏置在 TP 组内是复制的，但每个 rank 只看到
`1/t` 的 token。它们的梯度必须跨 TP 求和。mini 用参数 hook：

```python
param.register_hook(lambda grad: all_reduce(grad, group=tp_group))
```

如果不做，loss 前向仍然正确，但这些参数的梯度只包含本地序列块，训练会慢慢
偏离参考实现。`tests/parallel_equiv.py` 的 `tp_bias` 用例会检查 LayerNorm 和
out_proj bias 的梯度。

### 错误 3：Embedding 的输入被提前切了

`VocabParallelEmbedding` 在 SP 下应当是：

```text
完整 input_ids → 本地词表 lookup → all-reduce hidden → scatter 序列
```

而不是先把 `input_ids` 按序列切给各 rank，再 lookup。虽然后者在某些实现里也可
工作（如果每个 rank 的词表分片能覆盖本地 token），但它改变了 embedding 权重的
梯度语义，且和词表并行交叉熵的 token 统计容易错位。mini 保持“embedding 看到
完整 ids，输出再 scatter”。

## 7.5 与 TP、CP 的组合

- **SP + TP**：标准组合，SP 的 size 就是 TP size。
- **SP + PP**：可以。PP 传输的激活是 sequence-sharded 的 `[b, s/t, h]`，最后
  stage 的 LM head 需要 gather 序列。
- **SP + CP**：mini 不支持。两者都切序列，需要定义二维序列网格、attention 的
  两级通信和 loss 归一化；生产 Megatron 支持这些组合，但实现复杂。
- **SP + DP**：正交；DP 只影响样本。

## 7.6 性能收益与代价

收益：

- LayerNorm/Dropout/残差激活降为 `1/t`；
- 长序列时激活显存明显下降；
- 逐元素算子的并行度提高。

代价：

- Column 前向多一次 all-gather（把序列分片聚合成完整序列）；
- Row 前向用 reduce-scatter 替代 all-reduce：ring 下 reduce-scatter 的每卡
  发送量约为 all-reduce 的**一半**，但它只把结果留在本地序列分片。mini 为
  可读性写成 `all_reduce + chunk`，语义相同、通信量更大；
- 反向的 Column 输入梯度在 mini 里是 `split + TP all-reduce` 两步；生产实现
  把这两步融合成一次 reduce-scatter。Row 的反向是 all-gather。前向+反向的
  总通信量与普通 TP 同一量级，不是无条件减半；
- 小序列/小模型时，通信开销可能超过收益；
- 需要保证序列长度能被 TP 整除。

经验：当 `s` 较大（≥2K）、`t` 在 NVLink 域内、激活是瓶颈时，SP 通常值得开。
短序列/小模型不必强上。

## 7.7 mini 代码路径

`mini_megatron/parallel/layers.py`：

```python
class ColumnParallelLinear:
    def forward(self, x):
        if self.sequence_parallel:
            x = gather_from_sequence_parallel_region(x)
        return _ColumnParallelLinearFn.apply(...)

class RowParallelLinear:
    def forward(self, x):
        out = F.linear(x, self.weight, None)
        if self.sequence_parallel:
            out = reduce_scatter_to_sequence_parallel_region(out)
        ...
```

`state.py` 中的自定义 autograd：

- `_GatherFromSequenceParallelRegion`：forward all-gather 序列，backward split；
- `_ReduceScatterToSequenceParallelRegion`：forward all-reduce + chunk 序列，
  backward all-gather。

`transformer.py` 中：

- `TransformerBlock` 给 LayerNorm 参数注册 TP all-reduce hook；
- `TransformerStage` 的 final norm 也注册 hook；
- 第一个 stage 的 position embedding 在序列维 chunk 后相加。

## 7.8 一个验证方法

构造一个 `TP=2, SP=true` 的 stage，加载 TP=1 的完整权重（`sharding.py`），
对比：

1. loss；
2. q_proj/out_proj 的梯度；
3. LayerNorm 权重和 Row Parallel bias 的梯度；
4. 最后一位 token 的 logits。

如果 1 对但 2/3 错，通常是 reduce-scatter 维度或复制参数 hook 的问题；如果 1
就错，先检查 all-gather 的序列顺序和位置编码。

## 7.9 本章小结

- SP 在 TP 组内按序列切激活，降低 LayerNorm/Dropout/残差的显存。
- 关键是序列维的 all-gather/reduce-scatter，不是 hidden 维。
- 复制参数（norm、bias）的梯度必须跨 TP 求和。
- SP 与 CP 的组合在 mini 中不支持，生产中需要二维序列网格。
- 小序列不一定值得开 SP，要用 profiling 判断。

下一章进入流水线并行：把层切到不同 GPU，并用 micro-batch 填满气泡。
