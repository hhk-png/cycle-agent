# 第 20 章 词表并行交叉熵

## 20.1 问题

LM head 输出 `[tokens, V]` 的 logits。TP 下每个 rank 只有 `V/t` 个词表：

```text
logits_r: [tokens, V/t]
labels:   [tokens]
```

要计算：

```text
loss = mean_t [ -log softmax(logits_t)[label_t] ]
```

完整推导（全局 max/sum、目标 logit、归一化）见 [附录 A](A-math.md) 的 A.2。难点：

- softmax 的分母需要全局词表；
- 目标 logit 只在一个 rank 的 vocab 范围内；
- `ignore_index` 的 token 不能计入分母/梯度；
- CP 下 loss 是全局 token 平均，不是各 rank 平均的再平均。

## 20.2 前向

### 1. 全局 max

```python
work = logits.float()          # log-sum-exp 始终在 fp32 做
max_local = work.max(dim=-1).values
max_global = all_reduce(max_local, op=MAX, group=tp)
```

### 2. 全局 sum_exp

```python
exp_local = exp(work - max_global.unsqueeze(-1))
sum_local = exp_local.sum(-1)
sum_global = all_reduce(sum_local, op=SUM, group=tp)
logsumexp = max_global + log(sum_global.clamp_min(1e-30))
```

### 3. 目标 logit

```python
valid = labels != ignore_index
target_mask = valid & (labels >= vocab_start) & (labels < vocab_end)
# ignore_index（如 -100）和非本 rank 的 label 会越界，先 clamp 再 gather
local_target = (labels - vocab_start).clamp(0, vocab_local - 1)
target_local = work.gather(1, local_target.unsqueeze(1)).squeeze(1)
target_local = target_local.masked_fill(~target_mask, 0)
target_global = all_reduce(target_local, op=SUM, group=tp)
```

### 4. 逐 token loss

```python
token_loss = (logsumexp - target_global) * valid.to(work.dtype)
```

### 5. 全局平均

```python
local_sum = token_loss.sum()
local_count = valid.sum()
if cp > 1:
    all_reduce([local_sum, local_count], group=cp)
loss = global_sum / global_count
```

`ignore_index` 的 token 在 `valid` 中为 0，不进入分子/分母。

## 20.3 反向

对本地 logits 的梯度：

```text
dL/dlogits_r = (softmax_global(logits_r) - onehot(label_r)) / global_count
```

其中：

- `softmax_global` 用全局 max/sum 归一化；
- `onehot` 只在目标 token 落在本 rank 的词表范围时非零；
- `ignore_index` 的 token 梯度为 0；
- `global_count` 是 CP 组内所有有效 token 数。

实现：

```python
probs = exp(logits - max_global) / sum_global
grad = probs * valid
grad.scatter_add_(1, local_target, -target_mask)
grad *= grad_output / global_count
```

## 20.4 常见错误

### 错误 1：对本地 loss 做平均

TP 下每个 rank 看到**相同**的 token/label，词表分片只是 logits 的列切分，
log-sum-exp 和目标 logit 已经在 TP 组内归约成全局值；把 TP 的局部 loss
再平均一次是另一种错误（重复平均），但不会因为 token 数不同而错。

真正的“平均值的平均”发生在 **CP/跨 token 分片**：每个 CP rank 只持有
不同的 token 切片，切分不等、padding、ignore_index 都会让各 rank 的有效
token 数不同。正确做法是先把各 rank 的 loss **sum** 和有效 token **count**
分别 all-reduce，再相除。必须用全局 sum/global count（9.4、20.6）。

### 错误 2：目标 logit 用 `gather` 时越界

`labels - vocab_start` 对不落在本 rank 的 token 可能是负数/越界。必须先
`masked_fill` 或 clamp，再 gather；否则会索引错误或读到错误值。

### 错误 3：忽略 `ignore_index`

`valid` 必须同时用于 loss 和梯度，否则 padding token 会污染 loss/梯度。

### 错误 4：CP 的全局 max/sum 只对 TP 做

CP 下每个 rank 的 token 不同，但词表分片相同；全局 max/sum 只需要在 TP 组内
（因为每个 token 的 logits 被 TP 切分）。CP 组内要做的是 token 统计的
sum/count。两者不能混。

### 错误 5：softmax 用 bf16

大 logits 下 bf16 的 exp/sum 容易溢出/精度不足。mini 在
`_VocabParallelCrossEntropy.forward` 里把 logits cast 到 fp32 再做
max/exp/log；backward 也基于 fp32 的 softmax，最后把梯度 cast 回 logits 的
dtype。`test_bf16_logits_compute_in_fp32_and_return_matching_grad_dtype`
专门锁住这个契约（loss 与梯度 dtype 都是 bf16，数值按 fp32 参考对齐）。

## 20.5 z-loss

z-loss 用于稳定 softmax 分母：

```text
L_z = coeff * logsumexp²
```

它惩罚过大的 logsumexp，常见于 MoE/大模型训练。mini 的 CE 支持
`z_loss_coeff`，反向加上：

```text
2 * coeff * logsumexp * softmax
```

注意 z-loss 的系数/归一化口径要和主 loss 一致，否则会改变有效学习率。

## 20.6 与 TP/CP 的 group

- `tp_group`：all-reduce max/sum/target；
- `cp_group`：all-reduce token sum/count；
- `pp`：只有最后一个 stage 有 logits/labels；
- `dp`：不同样本，各自算 loss，日志时再平均。

## 20.7 mini 代码

`mini_megatron/parallel/cross_entropy.py`：

```python
class _VocabParallelCrossEntropy(torch.autograd.Function):
    @staticmethod
    def forward(ctx, logits, labels, tp_group, cp_group, ignore_index, z_loss):
        ...
    @staticmethod
    def backward(ctx, grad_output):
        ...
```

`vocab_parallel_cross_entropy(logits, labels)` 把任意形状的 logits/labels
flatten 成 `[N, V/t]` / `[N]`，返回标量 loss。

## 20.8 测试

- `test_matches_torch_with_ignore_index`：world=1 与 `F.cross_entropy` 对比；
- `test_gradient_matches_torch`：梯度逐元素对比；
- `test_all_ignored_is_zero`：全 ignore 时 loss=0、梯度=0；
- `test_z_loss_is_finite`；
- 分布式训练冒烟：TP=2、CP=2、TP+PP、CP+PP。

注意：CE 的**逐元素**单测目前只在 world=1 下与 `F.cross_entropy` 对比；
TP=2/CP=2 的覆盖来自端到端训练冒烟和 loss 等价性，不是 CE 模块本身的
梯度单测。若要加一个直接的 TP/CP CE 等价性测试，最小做法是：固定同一份
logits/labels，分别用 world=1 的参考和 TP=2 的 vocab shard 计算，再对比
loss 与每个 shard 的梯度。这个缺口不影响现有结论，但值得在扩展时补上。

## 20.9 本章小结

- 词表并行 CE 的关键是全局 log-sum-exp、目标 logit 的本地掩码、全局 token 平均。
- CP 只影响 token 统计，不影响词表 max/sum 的 TP 组。
- `ignore_index` 必须同时作用于 loss 和梯度。
- z-loss 可以稳定 softmax，但系数要调。
- 这个模块的 bug 往往表现为“loss 能降但数值慢慢偏”，必须做梯度对比。

下一章拆解流水线调度与 p2p 通信。
