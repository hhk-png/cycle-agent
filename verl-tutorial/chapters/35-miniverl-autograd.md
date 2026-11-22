# 第 35 章 自动微分与 TinyTransformer

## 35.1 为什么自己写 autograd

MiniVerl 不能依赖 PyTorch（CPU 环境、安装成本、教学透明度）。自己实现一个最小反向模式自动微分，可以：

- 看清每个算子的前向/反向；
- 用有限差分验证梯度；
- 解释 PPO loss 如何回传到 logits；
- 避免“调库黑盒”。

`mini_verl/autograd.py` 约 500 行，支持训练 TinyTransformer 所需的所有算子。

## 35.2 计算图

`Tensor` 保存：

```python
data          # np.ndarray
requires_grad
grad
_prev         # 父节点集合
_backward     # 局部反向闭包
```

一次运算：

```python
out = Tensor(data, requires_grad=..., _children=(a, b), _op="add")
out._backward = lambda: (accumulate(a, ...), accumulate(b, ...))
```

`backward()`：

1. 从输出出发做拓扑排序；
2. 反向遍历，调用每个节点的 `_backward`；
3. 梯度累加到 `.grad`。

这是标准的 reverse-mode AD，与 micrograd 同构，只是算子用 NumPy 向量化。

## 35.3 广播

`x + b` 中 `b` 可能被广播。反向时要把梯度求和回原形状：

```python
def _unbroadcast(grad, shape):
    while grad.ndim > len(shape):
        grad = grad.sum(axis=0)
    for axis, size in enumerate(shape):
        if size == 1 and grad.shape[axis] != 1:
            grad = grad.sum(axis=axis, keepdims=True)
    return grad
```

没有这一步，`(B,T,D) + (D,)` 的反向会形状错误。

## 35.4 关键算子

### matmul

```python
out = a @ b
ga = g @ b.swapaxes(-1, -2)
gb = a.swapaxes(-1, -2) @ g
```

支持 2D/3D/batched。

### sum/mean

`sum` 的反向是广播；`mean` 再除以元素数。axis 可以是 int/tuple，keepdims 要正确处理。

### softmax / log_softmax

softmax 反向：

$$
\frac{\partial L}{\partial x}=y\odot(g-\langle g,y\rangle).
$$

log_softmax 反向：

$$
\frac{\partial L}{\partial x}=g-\exp(y)\sum g.
$$

### layer_norm

对最后一维归一化：

$$
\hat x=\frac{x-\mu}{\sqrt{\sigma^2+\epsilon}},\qquad y=\gamma\hat x+\beta.
$$

反向要分别算 `dgamma`、`dbeta`、`dx`。MiniVerl 的实现显式写出，测试用有限差分验证。

### embedding / gather

embedding 反向用 `np.add.at` 散射；gather 反向用 flat index 散射。二者都是稀疏梯度，不能用普通切片赋值。

### masked_fill / where / clip

mask 位置不传梯度；`where` 只对选中分支传梯度；`clip` 在边界外梯度为 0。

## 35.5 注意力

`CausalSelfAttention`：

```python
qkv = c_attn(x)
q, k, v = split(qkv)
q = reshape(q, (B, T, H, Dh)).transpose(0, 2, 1, 3)
scores = q @ k.transpose(0, 1, 3, 2) / sqrt(Dh)
scores = masked_fill(scores, causal_mask, -1e9)
attn = softmax(scores, -1)
y = (attn @ v).transpose(0, 2, 1, 3).reshape(B, T, D)
return c_proj(y)
```

反向由各个算子组合自动完成。测试 `test_causal_mask_hides_future_tokens` 验证：

- 改变最后一个 token 不影响前面的 logits；
- 改变最后一个 token 会影响最后一个 logits。

## 35.6 TinyTransformer

`TinyLM`：

```text
tok_emb + pos_emb
  -> N × Block(ln -> attn -> residual; ln -> mlp -> residual)
  -> ln_f
  -> lm_head
```

`TinyValueModel` 把 `lm_head` 换成 `value_head`（输出 1 维）。

`TinyLinearLM` 是 bandit 专用：

```text
tok_emb -> ln -> lm_head
```

它没有 attention，最后一个 token 的 embedding 直接决定 logits。对四动作 bandit 足够，且 CPU 很快。

## 35.7 log-prob 的对齐

`log_probs_from_logits`：

1. `logits[:, :-1, :]` 预测 `input_ids[:, 1:]`；
2. `log_softmax`；
3. `gather_last_dim` 取目标 token；
4. 前面补一列 0，使位置对齐；
5. 乘 `response_mask`。

这是 PPO 最容易错的地方之一。MiniVerl 的测试 `test_log_probs_from_logits_matches_manual` 用手工 log-softmax 对照。

## 35.8 约束解码 mask

如果 rollout 限制动作集合，训练侧也要对 logits 加同样的 mask：

```python
mask = np.full(vocab, -1e9)
mask[allowed_ids] = 0
logits = logits + mask
```

否则 `old_log_prob` 与 `log_prob` 不在同一分布，PPO 比率失真。MiniVerl 在 `logprob_metric_fn` 与 `actor_loss_fn` 中都传入 `allowed_token_ids`。

## 35.9 有限差分测试

`tests/test_autograd.py` 用中心差分验证：

```python
def finite_difference(fn, array, eps=1e-3):
    for each element:
        plus = fn(array + eps at index)
        minus = fn(array - eps at index)
        grad = (plus - minus) / (2*eps)
```

覆盖 add/mul/matmul/sum/softmax/log_softmax/layer_norm/embedding/gather 等。有限差分是发现“手写反向符号错/广播错”的最有效方法。

## 35.10 为什么不用 PyTorch

- 环境：本教程需要在无 GPU 的机器上运行；
- 教学：手写反向能看清每个算子；
- 测试：NumPy 确定性强；
- 依赖：只依赖 NumPy + PyYAML。

代价：

- 性能差；
- 不支持所有算子；
- 不能直接加载 HF 模型。

MiniVerl 用“小模型 + 少算子”换取“完全可解释”。

## 35.11 Tensor 的生命周期：`_make`、拓扑排序与梯度累加

`Tensor` 的核心不是 `data`，而是三条元信息：`requires_grad`、`_prev`（父节点集合）、`_backward`（局部反向闭包）。`_make` 是所有算子的统一入口：

```python
def _make(self, data, parents, backward, op):
    out = Tensor(data=data, requires_grad=any(p.requires_grad for p in parents),
                 _children=parents, _op=op)
    def _run():
        if out.grad is not None:
            backward(out.grad)
    out._backward = _run
    return out
```

三个设计点值得注意：

1. **闭包延迟读取 `out.grad`。** `_run` 在被调用时才看 `out.grad`，而不是在构图时绑定。这允许反向传播从任意输出开始，也允许同一个图多次 `backward()`（每次会重新累加）。
2. **`_prev` 是 `set`，按对象身份去重。** 如果 `y = x * x`，父节点集合只有一个 `x`，但反向闭包会调用两次 `_accumulate(x, ...)`，所以梯度仍然正确累加。用 `list` 也能工作，但会重复遍历同一个节点。
3. **拓扑排序用 `id(node)` 访问。** 递归 `build` 先访问所有父节点，再把当前节点 append 到 `topo`；反向遍历 `reversed(topo)` 保证每个节点在被消费前已经收集了所有下游梯度。

`_accumulate` 是另一个容易写错的点：

```python
def _accumulate(tensor, grad):
    if not tensor.requires_grad:
        return
    if tensor.grad is None:
        tensor.grad = np.zeros_like(tensor.data, dtype=np.float32)
    tensor.grad += grad.astype(np.float32, copy=False)
```

它必须支持“第一次写入”和“后续累加”两种路径。如果直接 `tensor.grad = grad`，多分支图（残差、同一个 embedding 被多处使用）会丢失部分梯度。

叶子节点与非叶子节点的区别只体现在 `requires_grad` 的传播上：`any(p.requires_grad for p in parents)` 让常量不进入图，`no_grad()` 则通过全局标志让新 Tensor 的 `requires_grad=False`。测试 `test_no_grad_blocks_graph` 与 `test_grad_accumulates` 分别固定了这两条语义。

## 35.12 数值稳定：masked softmax、log-softmax 与 LayerNorm

手写反向的数学正确不代表数值正确。MiniVerl 在三处做了显式处理。

### 因果 mask 用 `-1e9` 而不是 `-inf`

```python
causal = np.triu(np.ones((seq, seq), dtype=bool), k=1)
scores = masked_fill(scores, causal[None, None, :, :], -1e9)
attention = softmax(scores, axis=-1)
```

`softmax` 先减去每行最大值，被 mask 的位置在指数后是 `exp(-1e9 - max) ≈ 0`。如果用 `-inf`，`-inf - (-inf)` 会产生 `nan`；`-1e9` 在 float32 下既足够小，又不会触发 `inf-inf`。

### log-softmax 的 shift

```python
shifted = x.data - np.max(x.data, axis=axis, keepdims=True)
logsumexp = np.log(np.sum(np.exp(shifted), axis=axis, keepdims=True))
out = shifted - logsumexp
```

没有 shift 时，logits 稍大就会 `exp` 溢出。shift 不改变数学结果，因为 $\log\sum e^{x_i}=\max x+\log\sum e^{x_i-\max x}$。

### LayerNorm 的 eps 放在方差内

```python
inv_std = 1.0 / np.sqrt(var + eps)
```

如果写成 `sqrt(var) + eps`，当方差接近 0 时梯度会被放大到不可控。`eps=1e-5` 是 Transformer 的常见默认值；改小它会让小方差 token 的梯度更尖锐。

### 熵的计算

策略熵 $H=-\sum_v \pi_v\log\pi_v$ 也要在 log-softmax 空间算：

```python
logp = log_softmax(logits, axis=-1)
entropy = -(np.exp(logp) * logp).sum(axis=-1)
```

直接用 `softmax` 再取 log，会在概率下溢时得到 `log(0)`。MiniVerl 的 `entropy_from_logits` 走的是 log-softmax 路径，测试用固定 logits 对照。

## 35.13 从 logits 到 PPO loss 的梯度路径

把一次 actor 更新拆成九步，能看清每个张量的形状与梯度来源：

```text
input_ids (B,T) 
  -> TinyLM.forward -> logits (B,T,V)
  -> log_softmax -> logp (B,T,V)
  -> gather_last_dim(target_ids) -> token_logp (B,T)
  -> shift + response_mask -> log_prob (B,R)
  -> ratio = exp(log_prob - old_log_prob)
  -> clipped = minimum(ratio*A, clip(ratio)*A)
  -> loss = -sum(clipped*mask) / global_tokens
```

反向时，梯度依次穿过：

1. `loss` 对 `clipped`：`-1/global_tokens`；
2. `clipped` 对 `ratio`：PPO 的 `min` 选择哪一支，梯度就走哪一支；被 clip 的那支梯度为 0；
3. `ratio` 对 `log_prob`：`ratio`；
4. `log_prob` 对 `logits`：`softmax - one_hot(target)`，再乘 mask；
5. `logits` 对 Transformer 参数：常规反向。

第 4 步是策略梯度与交叉熵的公共部分。CISPO 的区别在第 2 步：它把 `clip(ratio)` `detach`，所以梯度不经过 `ratio`，而是直接 `-clip(ratio)*A` 乘到 `log_prob` 上。

一个最小的手工梯度检查（与 `tests/test_autograd.py::test_finite_difference_layer_norm` 同构）：

```python
def numeric_grad(fn, x, eps=1e-3):
    grad = np.zeros_like(x)
    for idx in np.ndindex(x.shape):
        plus = x.copy(); plus[idx] += eps
        minus = x.copy(); minus[idx] -= eps
        grad[idx] = (fn(plus) - fn(minus)) / (2 * eps)
    return grad
```

把它用在 `log_probs_from_logits` 上，可以同时验证 shift、gather 与 mask 三处。有限差分不需要知道反向公式，是对“手写反向”最独立的一种验证。

## 35.14 测试映射：每个算子由谁保证

| 算子/语义 | 测试 | 断言什么 |
|---|---|---|
| 广播加法/乘法 | `test_add_backward_broadcast`、`test_mul_backward_broadcast` | 梯度求和回原形状 |
| 批量 matmul | `test_matmul_batched` | 3D/4D 批量维正确 |
| softmax/log-softmax | `test_softmax_log_softmax` | 与解析梯度一致 |
| LayerNorm | `test_layer_norm`、`test_finite_difference_layer_norm` | 解析反向 + 有限差分 |
| embedding/gather | `test_embedding_gather` | 稀疏散射，重复索引累加 |
| mask/where/clip | `test_masked_fill_where_minmax` | mask 位置零梯度 |
| no_grad | `test_no_grad_blocks_graph` | 不建图、不累加 |
| 梯度累加 | `test_grad_accumulates` | 多分支求和 |
| 因果 mask | `test_causal_mask_hides_future_tokens` | 未来 token 不影响过去 logits |
| log-prob 对齐 | `test_log_probs_from_logits_matches_manual` | shift/gather 手工对照 |
| 约束解码 | `test_token_log_probs_mask` | 只允许动作集合 |

这张表也是扩展 autograd 时的验收清单：新增一个算子，至少要有“解析梯度 + 有限差分 + 一个真实使用场景”三类测试，否则它可能在主循环里静默出错。

## 本章小结

- MiniVerl 用 reverse-mode AD + NumPy 实现可训练的 TinyTransformer。
- 广播、softmax、layer_norm、embedding/gather 是最容易写错反向的算子。
- 因果 mask 测试验证“未来 token 不影响过去”。
- log-prob 对齐与约束解码 mask 是 PPO 正确性的关键。
- `_make` 的延迟闭包与 `_accumulate` 的首次/累加双路径，是多分支图正确性的基础。
- 数值稳定要在 mask（`-1e9`）、log-softmax（shift）、LayerNorm（eps 在方差内）三处显式处理。
- 有限差分是验证手写梯度的黄金标准；每个新算子至少要有解析、差分、场景三类测试。

## 延伸阅读

- `mini_verl/autograd.py`
- `mini_verl/nn.py`
- `tests/test_autograd.py`
- 下一章：协议、配置与单控制器。
