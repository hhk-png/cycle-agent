# 第 25 章 MoE 实现：从专家复制到专家并行

本章拆解两个实现：

- `mini_megatron/parallel/moe.py`：**专家复制**的 TP MoE，所有 rank 拥有全部
  专家，适合理解路由、容量和辅助损失；
- `mini_megatron/parallel/expert_parallel.py`：**专家并行**（EP）MoE，专家按
  rank 切分，token 通过真正的 all-to-all 往返，适合理解生产 MoE 的通信与
  梯度语义。

两者共享同一套路由数学。先用复制版把数值跑对，再用 EP 版把系统问题暴露出来，
这是理解 MoE 最稳的路径。

> 阅读顺序建议：10.1–10.4（原理）→ 25.1–25.5（复制版）→ 25.6–25.8（EP）→
> 25.9（验证）→ 25.10（推理与生产差距）。

## 25.1 复制版的结构

```python
class MoELayer(nn.Module):
    def __init__(...):
        self.router = TopKRouter(hidden, num_experts)
        self.experts = ModuleList([
            MLP(hidden, ffn, sequence_parallel=False, ...)
            for _ in range(num_experts)
        ])

    def forward(self, x):
        if self.sequence_parallel:
            x = gather_from_sequence_parallel_region(x)
        ...
        if self.sequence_parallel:
            out = scatter_to_sequence_parallel_region(out)
        return out, aux_loss
```

- **每个 rank 拥有所有专家**；专家的 MLP 仍然是 TP 分片的（gate/up 列并行、
  down 行并行）。
- **路由权重复制**：所有 TP rank 看到相同 token、相同 router，因此 top-k 选择
  一致。
- **SP 下先 gather 序列再路由**：否则不同 TP rank 只看到自己的序列分片，路由
  结果不同；Row Parallel 的 all-reduce 会把不同 token 的专家输出混在一起。
- 每个专家独立处理自己的 token 子集，最后用 `index_add_` 按 token 累加。

复制版的显存代价是 `E × FFN`：70B 稠密模型换成 E=64 的 MoE，参数量可能放大
数倍，单卡放不下。这就是 EP 存在的原因。

## 25.2 路由：前向与梯度

路由只有三行：

```python
logits = self.router(flat)                       # [N, E]
probs = torch.softmax(logits.float(), dim=-1)    # fp32 softmax
topk_probs, topk_idx = torch.topk(probs, k)      # [N, k]
```

三个容易忽略的细节：

1. **softmax 用 fp32**。bf16 下 `exp` 的动态范围不足，专家数很大时（E=256+）
   低概率专家的相对误差会被放大，路由抖动。
2. **top-k 的选择不可导**。梯度只通过被选中专家的概率回传；未被选中的专家
   在当前 token 上没有梯度。`topk_idx` 是离散索引，不能对它求导。
3. **权重是否重新归一化**。Switch 使用原始 softmax 概率；某些实现只对 top-k
   概率重新归一化。两者在 k=1 时等价，k>1 时改变专家输出的尺度，必须与
   推理侧一致。

辅助损失的梯度是另一条路径：

```text
L_aux = α · E · Σ_i f_i · P_i
P_i   = mean_over_tokens(probs[:, i])
f_i   = count_i / (N · k)          # 不可导
```

`f_i` 是计数，停止梯度；`P_i` 对 router logits 可导。因此 `L_aux` 把“路由
概率均值”推向均匀，但不直接惩罚离散选择。这就是为什么 aux loss 只能缓解、
不能完全消除负载不均——真正的分配由 top-k 决定。

## 25.3 容量：一个数值例子

```python
capacity = ceil(N * k / E * capacity_factor)
```

设每个 EP rank 有 `N=1024` 个 token，`E=8`，`k=2`，`cf=1.25`：

```text
单 rank、无 EP：capacity = ceil(1024·2/8·1.25) = 320
EP=4：每个专家收到 4 个 rank 的 token
      capacity = ceil(1024·4·2/8·1.25) = 1280
```

容量是**每个专家**的上限，不是每个 rank 的上限。EP 越大，单个专家看到的
token 越多，容量也越大；这也是 EP 能提高专家利用率的原因之一。

容量溢出有三种策略：

| 策略 | 行为 | 代价 |
|---|---|---|
| Drop（Switch） | 超过 capacity 的 token 丢弃 | 有效 batch 变小，梯度有偏 |
| Dropless | 不设上限，按实际 token 数分组 | 负载不均时显存/尾延迟爆炸 |
| Reroute | 把溢出 token 送给次优专家 | 路由复杂，可能级联溢出 |

mini 用 Drop，且按 token id 排序后保留前 `capacity` 个，保证可复现。生产实现
还会用 capacity padding 把每个专家的 token 数补齐到固定值，以便 grouped GEMM
使用规则的 batch 形状。

## 25.4 复制版的完整前向

```python
logits = self.router(flat)
probs = softmax(logits.float(), -1).to(flat.dtype)
topk_probs, topk_idx = topk(probs, k)

capacity = ceil(N * k / E * cf)
out = zeros_like(flat)
for e, expert in enumerate(experts):
    mask = topk_idx == e
    token_idx = mask.any(-1).nonzero().squeeze(1)
    token_idx = token_idx[:capacity]             # 确定性丢弃
    expert_out = expert(flat[token_idx])
    weight = probs[token_idx, e].unsqueeze(-1)
    out.index_add_(0, token_idx, expert_out * weight)

assignments = bincount(topk_idx.flatten(), minlength=E)
f_i = assignments / (N * k)
p_i = probs.mean(0)
aux = E * sum(f_i * p_i)
```

关键不变量：

- **一个 token 可以出现在多个专家的 `token_idx` 里**（k>1），`index_add_`
  负责累加；
- **capacity 截断发生在专家计算之前**，被截断的 token 对该专家没有输出；
- **aux 使用截断前的分配计数**，与 Switch 论文一致；
- **专家输出的 dtype 必须与残差一致**，否则 `index_add_` 会隐式转换。

`TransformerStage` 把 aux 乘上 `moe_aux_loss_coeff` 放进 `StageOutput`，
schedule 在反向时把主 loss 和 aux 一起 `backward`。不要让 aux 单独反向：
路由图是共享的，单独反向会释放图或重复计算。

## 25.5 为什么复制版不够

复制版的通信只有 TP 的 all-reduce，专家本身不产生跨 rank 通信。它的扩展性
受限于单卡显存：

```text
每卡专家参数 ≈ E · (3 · h · ffn) · bytes / TP
```

以 `h=7168, ffn=2048, E=256, bf16` 为例，单卡（TP=8）专家参数约

```text
256 · 3 · 7168 · 2048 · 2 / 8 ≈ 2.82 GB
```

（只算专家 FFN 的 gate/up/down 三个矩阵，不含 attention/embedding；bf16
下 2 字节/参数。）2.82 GB 本身还能放下，但优化器状态会再乘约 6–8 倍，而且
真实模型的专家数/FFN 更大；因此仍必须把专家分到不同 rank。注意不要把
`2.82 GB` 误写成 `2.8 TB`——这类 1000 倍错误会让整个容量规划失真。

```text
rank 0: experts 0 .. E/ep-1
rank 1: experts E/ep .. 2E/ep-1
...
```

这就是专家并行。它把“专家参数”变成分布式状态，同时把“token 去哪个专家”
变成一次不规则 all-to-all。

## 25.6 EP 的进程组与所有权

mini 的 EP 组是 **DP 维度的一个重新划分**：

```text
world = tp × pp × cp × dp,   ep = dp
rank  = ((pp_idx·dp + dp_idx)·cp + cp_idx)·tp + tp_idx
```

固定 `(pp, cp, tp)`、变化 `dp` 的 rank 组成一个 EP 组。每个 EP rank 拥有
`E/ep` 个专家。选择 `ep = dp` 是为了让梯度语义最简单：每个专家只存在于一个
rank 上，不需要在 EP 组内再平均专家梯度。生产 Megatron 支持 `ep < dp`，此时
专家有副本，需要额外的 DP/EP 梯度 reduction。

创建组的代码在 `parallel/state.py`：

```python
initialize_model_parallel(tp, pp, cp, expert_model_parallel_size=ep)
```

`state.py` 会为每个 `(pp, cp, tp)` 坐标创建一个 EP 组；组对象与 DP 组分开，
避免 portable 后端把两个集合通信的消息序列号混在一起（见 `test_comm.py`）。

### 25.6.1 两个容易被忽略的假设

**1. 专家初始化不在 model-parallel init 流里。** `TransformerStage` 的
TP/PP 分片参数通过 `set_model_parallel_init_seed` 获得确定性的 rank 偏移；
EP 专家层则在 `expert_parallel.py` 里用 `torch.initial_seed()` 派生种子。
这意味着：

- 同一 EP 组内不同 rank 的专家初始化不同（这是对的）；
- 但 `set_model_parallel_init_seed` 的层计数器不覆盖专家层；
- 跨 checkpoint/重跑时，专家权重依赖 `torch.initial_seed()`，不依赖
  `init_method_std` 的全局流。改动初始化顺序会让专家权重变化，必须重新
  验证等价性。

**2. 全局 aux 假设各 EP rank 的 token 数相等。** `f_i` 的分母是
`local_num_tokens * ep * top_k`（`N·k` 的全局口径，`k=top_k`），其中
`local_num_tokens * ep` 等价于“每个 EP rank 的 token 数相同”。当 batch
长度可变、packed 序列或某 rank 被截断时，这个假设不成立，`f_i` 会偏；正确
做法是先 all-reduce 全局 token 数，或按 `send_counts` 计算全局比例。mini
的固定 shape 测试掩盖了这个问题，生产实现要显式处理变长 batch。漏掉
`top_k` 会把 `f_i` 放大 `k` 倍，aux loss 的尺度随之改变，表现为路由
“过度均衡”或梯度异常。

## 25.7 EP 前向：dispatch 与 combine

EP 的一次 MoE 前向分成五步：

```text
1. 本地路由      每个 rank 对自己的 token 做 top-k
2. dispatch     按专家 owner 把 token 发到对应 rank（all-to-all）
3. 本地专家 GEMM 每个 rank 只算自己拥有的专家
4. combine      把专家输出发回 token 的原 rank（反向 all-to-all）
5. 加权合并      按路由权重 index_add 回原 token
```

### 25.7.1 路由与排序

```python
pair_token = arange(N).repeat_interleave(k)   # 每个 (token, expert) 对
pair_expert = topk_idx.reshape(-1)
pair_weight = probs.gather(1, topk_idx).reshape(-1)
owner = pair_expert // experts_per_rank

order = argsort(owner, stable=True)           # 按 owner 连续排列
pair_token, pair_expert, pair_weight = reorder(...)
send_counts = bincount(owner, minlength=ep)
```

稳定排序有两个作用：让每个 peer 的 payload 连续；保证同一输入在不同运行得到
相同顺序（可复现）。`send_counts[d]` 是发给 rank d 的 pair 数。

### 25.7.2 变长消息与 padding

每个 rank 发给每个 peer 的 token 数不同，这是 EP 与 TP 最大的区别。mini 的
做法是：

1. 用 `all_gather` 交换 `send_counts`，得到每个 peer 会收到多少；
2. 用 `all_reduce(MAX)` 得到全局最大发送数 `max_rows`；
3. 把每个 peer 的 payload padding 到 `[max_rows, hidden+1]`；
4. 做等形状的 all-to-all；
5. 收到后按 `recv_counts` 截断。

```python
counts_all = [empty_like(send_counts) for _ in range(ep)]
all_gather(counts_all, send_counts, group=ep_group)
recv_counts = stack([counts_all[src][ep_rank] for src in range(ep)])
max_rows = max(1, all_reduce(send_counts.max(), op=MAX))
```

padding 会浪费带宽：如果某个 peer 收到 0 个 token，仍然要传 `max_rows` 行。
生产实现用 `all_to_all_single` 的 `output_split_sizes` 或分组发送，避免 padding；
mini 选择 padding 是为了在 portable 后端上用同一套形状语义。

### 25.7.3 打包权重与元数据

dispatch 的不只是 hidden state，还有路由权重：

```python
packed = zeros(pairs, hidden + 1)
packed[:, :hidden] = flat[pair_token]
packed[:, hidden] = pair_weight
```

把权重放在同一行，可以让它和 hidden 走同一条 autograd 路径。token id 和
local expert id 是离散元数据，单独用 int64 all-to-all 发送，不参与求导。

### 25.7.4 本地专家计算

```python
capacity = ceil(N * ep * k / E * cf)
expert_out = zeros_like(recv_hidden)
for local_e, expert in enumerate(self.experts):
    selected = (recv_expert == local_e).nonzero()
    selected = selected[argsort(recv_token[selected], stable=True)]
    selected = selected[:capacity]
    out = expert(recv_hidden[selected])
    expert_out.index_copy_(0, selected,
                           out * recv_weight[selected])
```

每个收到的 pair 只属于一个本地专家，所以用 `index_copy` 而不是 `index_add`。
被 capacity 截断的行保持 0，combine 后对原 token 没有贡献。

### 25.7.5 combine 的索引方向

combine 是最容易写错的一步。记住：

- dispatch 时，rank r 收到的是“**别人发给 r**”的 token；
- combine 时，rank r 要把“**r 收到的那些 token**”发回给**原发送者**；
- 因此返回块的截断长度是 `counts_all[自己][peer]`（自己发给 peer 的数量），
  不是 `counts_all[peer][自己]`。

这个方向写反时，loss 仍能下降（因为大部分 token 对上了），但数值会缓慢漂移。
mini 的 EP 等价性测试就是靠 `logits_diff` 抓住这个 bug 的（见 25.9）。

### 25.7.6 autograd

dispatch 和 combine 各是一个自定义 autograd 函数：

```python
class _AllToAllRows(torch.autograd.Function):
    @staticmethod
    def forward(ctx, group, *blocks):
        outputs = [empty_like(b) for b in blocks]
        comm.all_to_all(outputs, blocks, group=group)
        ctx.group = group
        return tuple(outputs)

    @staticmethod
    def backward(ctx, *grad_outputs):
        outputs = [empty_like(g) for g in grad_outputs]
        comm.all_to_all(outputs, grad_outputs, group=ctx.group)
        return (None, *outputs)
```

反向就是反向 all-to-all：前向把 token 从源 rank 送到专家 rank，反向把梯度从
专家 rank 送回源 rank。pad 出来的行梯度也会被送回，但调用方在截断时丢弃。

## 25.8 梯度语义：为什么专家梯度要除以 ep

> 前提：本节和 mini 一样取 `ep = dp`，即整个 DP 组就是 EP 组。若
> `dp > ep`，专家还有 `dp/ep` 个副本，必须先做副本组归约，不能直接套
> `/ep`；见 25.12.1。

假设两个 EP rank，各自做 local mean loss：

```text
L_0 = mean over batch 0,   L_1 = mean over batch 1
```

专家 e 在 rank 0 上，它收到来自两个 rank 的 token。它的梯度是：

```text
dL_0/dW_e + dL_1/dW_e = 2 · d(global mean)/dW_e
```

而稠密参数经过 DP all-reduce 后是 `(dL_0 + dL_1)/2`，即 global mean。如果
专家梯度不除以 ep，专家参数会以 ep 倍的步长更新——训练仍能跑，但专家学习率
实际被放大了。

mini 在 `optim/adamw.py::all_reduce_gradients` 中处理：

```python
if skip_experts:                    # DP 调用
    for p in model.parameters():
        if p.grad is not None and p._is_expert_parallel:
            p.grad.div_(ep)         # 把 sum 变成 global mean
```

同时：

- `all_reduce_gradients` 在 DP 调用时**跳过**专家参数（它们是不同的专家，不能
  相加）；
- CP 调用时**不跳过**（CP 副本持有相同专家切片，梯度应相加）；
- `global_grad_norm` 把 dense 与 expert 分开统计：dense 除以 `dp·cp`，expert
  只除以 `cp`。

还有一个更隐蔽的问题：**未激活专家的 `grad=None` 会破坏集合通信对称性**。
如果专家 e 在 rank 0 有梯度、在 rank 1 没有，两个 rank 执行的 all-reduce 数量
就不同；portable 后端会超时，NCCL 可能挂起或把不同参数的梯度配对。mini 在
MoE 模型的梯度同步前，把所有 `None` 梯度物化为 0，保证每个 rank 以相同顺序
参与 collective。代价是未使用参数也会走一次通信（以及 weight decay），但这是
正确性优先的取舍。

这些细节在普通稠密模型里不存在，是 MoE 训练“看起来能跑但收敛不对”的常见
来源。

## 25.9 EP 的数值等价性验证

`tests/parallel_ep.py` 用 2 个进程做如下对比：

- 参考模型：专家复制的 `MoELayer`，`ep=1`；
- EP 模型：`ep=2`，rank 0 拥有专家 0/1，rank 1 拥有专家 2/3；
- 数据：两个 rank 各处理不同的 batch；
- 参考梯度：在**拼接后的全局 batch** 上计算；
- 专家梯度：EP 侧除以 `ep` 后与全局参考比较。

运行：

```bash
python tests/parallel_ep.py --world-size 2 --out "$TEMP/ep.json"
```

一次实测（Python 3.14.6 / PyTorch 2.9.1+cpu）：

```json
{
  "loss_diff": 0.0,
  "logits_diff": 1.19e-07,
  "aux_diff": 0.0,
  "expert_diff": 4.66e-10,
  "all_grad_diff": 3.73e-08,
  "aux_grad_diff": 1.30e-08,
  "grad_diff": 3.73e-08
}
```

`aux_grad_diff` 是 `(loss + aux).backward()` 后 router 梯度与全局参考的差。
它专门验证 aux 的梯度缩放；只比较 aux 的**数值**是抓不到这个 bug 的。

容差取 `1e-5`。如果 `logits_diff` 正常但 `grad_diff` 大，优先查 combine 的
索引方向和专家梯度缩放；如果 `aux_diff` 大，查全局 `f_i/P_i` 是否只用了本地
统计。

两个可运行配置：

```bash
python scripts/run_distributed.py --world-size 2 --config configs/tiny_ep2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_ep2.json
```

后者验证 TP×EP 的组正交性：TP=2 切专家 GEMM，EP=2 切专家；world=4 时每个
TP rank 在自己的 EP 组里做 all-to-all。

## 25.10 grouped GEMM 与生产优化

mini 用 Python 循环逐个专家做 GEMM。E=4 时没问题，E=256 时会有 256 次 kernel
启动，且每次 batch 很小，GPU 利用率极低。生产实现用 grouped GEMM：

```text
1. 把所有专家的 token 拼成一个 [total_tokens, h] 的 buffer
2. 记录每个专家的 offset
3. 一次 kernel 内按 offset 做多个 GEMM
4. 反向同样按 offset 聚合梯度
```

配套优化：

- **token permutation / unpermutation**：用一次 gather/scatter 完成重排；
- **all-to-all 与专家 GEMM 重叠**：边收边算，隐藏网络延迟；
- **capacity padding**：把每个专家的 batch 补齐到固定大小，便于规则 kernel；
- **shared expert**：DeepSeek 风格，所有 token 都过一个共享专家，路由只决定
  稀疏专家；
- **fine-grained expert**：把 FFN 切成更小的专家，增加路由组合数；
- **FP8/NVFP4 专家 GEMM**：按专家/块做 scaling；
- **batch-invariant routing**：推理侧固定路由，避免 batch 组成改变输出。

这些优化的共同点是：**算法不变，改变数据布局和 kernel 形状**。先用 mini 把
语义跑对，再替换布局，是理解 grouped GEMM 的正确顺序。

### 25.10.1 从 Python 循环到 grouped GEMM

mini 的 `ExpertParallelMoELayer` 在 combine 前对每个本地专家逐个做 MLP。
真实代码不是 `buckets` 字典，而是按 `recv_expert` 选出该专家的 token 行，
排序、按 capacity 截断后用 `index_copy` 写回：

```python
for local_expert, expert in enumerate(self.experts):
    selected = (recv_expert == local_expert).nonzero(as_tuple=False).squeeze(1)
    if selected.numel() == 0:
        continue
    selected = selected[torch.argsort(recv_token[selected], stable=True)]
    if selected.numel() > capacity:
        selected = selected[:capacity]          # 确定性溢出：保留小 token id
    out = expert(recv_hidden.index_select(0, selected))
    expert_out = expert_out.index_copy(
        0, selected, out * recv_weight.index_select(0, selected).to(out.dtype)
    )
```

这个循环是“语义正确、性能差”的版本：每个专家一次 GEMM、Python 层循环、
`index_select/index_copy` 带额外拷贝。生产 grouped GEMM 把多个专家的 GEMM
合成一个 batched/grouped kernel，但上面的“选行 → 截断 → 加权 → 写回”顺序
不能变。

生产要做三件事：

**1. 分组元数据**

```text
sorted_idx:   [total_assigned]        # 按专家稳定排序后的 token 索引
offsets:      [E_local + 1]           # 每个专家在 sorted buffer 中的起止
total_assigned = offsets[-1]
```

`grouped GEMM` 一次 kernel 处理 `E_local` 个形状相同（或按 offset 分段）的
矩阵乘，避免 `E_local` 次启动。反向时同样按 offset 聚合 `dX`/`dW`。

**2. split sizes vs padding**

两种 all-to-all 语义：

| 语义 | 发送 buffer | 优点 | 缺点 |
|---|---|---|---|
| padding | 每个 peer 固定最大 token 数 | shape 静态、实现简单 | 带宽浪费，最热 peer 决定 buffer |
| split sizes | 先交换每个 peer 的 token 数，再变长发送 | 带宽最优 | 需要两次通信/动态 shape |

mini 用 padding，因此全部 rank 的 buffer 形状一致，但容量受最热专家限制。
生产（包括 DeepEP 一类实现）会用 split sizes / 变长 all-to-all，并配合
`all_to_all_single` 的 output split 参数。

**3. 通信与计算重叠**

把 token 按块流水线化：

```text
chunk 0: dispatch ──► expert GEMM ──► combine
chunk 1:            dispatch ──► expert GEMM ──► combine
```

关键是让 dispatch 的接收与上一 chunk 的 GEMM 并行；这要求：

- 把一次大 all-to-all 拆成多个 chunk；
- 用独立 CUDA stream/通信器；
- 保证 combine 的索引在 chunk 间不串；
- 处理“某专家在某 chunk 没有 token”的空桶。

**DeepEP** 是面向 MoE 的高性能通信库，提供 normal/low-latency 两种模式：
normal 模式面向训练的大 batch all-to-all，low-latency 模式面向 decode 的
小 batch、低延迟场景。它的具体 API/支持的硬件以当前仓库为准；本书只把它
当作“EP 通信原语”的一个代表，不把任何性能数字写成承诺。

**为什么 mini 不实现 grouped GEMM**：CPU + 小 E 下 Python 循环足够快，
而 grouped GEMM 的复杂度主要在 CUDA kernel、变长 offset 和反向索引，无法
在 CPU 教学实现中体现收益。mini 保留的是**语义不变量**：稳定排序、offset
一致、combine 索引可逆、空专家零梯度。把这些不变量测对，再替换 kernel，
才是正确的工程顺序。

**aux-free 与 node-limited 的位置**：`parallel/moe_balance.py` 实现了
DeepSeek 风格的 bias 更新与节点约束路由，测试覆盖 bias 方向、选择/门控
分离、全局计数和不可行预算；但它是一个**独立参考模块**，没有接进
`MoELayer`/`ExpertParallelMoELayer` 的训练主链。把两者接起来时要验证：

- bias 更新只改选择、不改门控权重，因此主 loss 没有额外的 aux 梯度；
- `counts` 必须是跨 EP/DP 的全局负载，而不是本地 rank 的计数；
- capacity/drop 与 bias 更新的交互：被 drop 的 token 不应计入负载统计；
- 推理时 routing 必须与训练一致（batch-invariant），否则同一 token 在不同
  batch 下走不同专家。

## 25.11 MoE 推理的特殊问题

训练和推理的 MoE 不是同一套系统：

- **专家并行 decode**：batch 很小、序列很长，all-to-all 的延迟占比更高；
- **capacity**：推理通常不丢弃 token，用 dropless + 动态 buffer；
- **负载均衡**：推理请求的领域分布可能让少数专家过热，需要专家复制/迁移；
- **KV cache**：MoE 只替换 FFN，attention 的 KV cache 仍然按层/头管理；
- **batch invariance**：同一请求在不同 batch 组成下必须得到相同输出，否则
  评测/缓存/投机解码都会出错；
- **量化**：专家 GEMM 的 FP8/FP4 scale 要按专家校准，不能全模型一个 scale。

vLLM/TensorRT-LLM 的 fused MoE kernel 主要解决“小 batch、多专家”的 kernel
启动和显存访问问题；Megatron Core inference 则把 EP/TP/PP 与 KV cache 放在
同一套并行抽象里。

## 25.12 生产差距清单

mini 的 EP 已经“能跑、能对”，但仍是教学实现：

| 能力 | mini | 生产 |
|---|---|---|
| 专家切分 | `ep = dp` | `ep` 可独立于 DP，支持专家副本 |
| dispatch | padding 到全局最大 | split sizes / 分组发送 |
| 专家 GEMM | Python 循环 | grouped GEMM / fused MoE |
| 通信重叠 | 同步 | all-to-all 与 GEMM 重叠 |
| 容量 | 固定 capacity + drop | dropless / 动态容量 / reroute |
| 路由 | `MoELayer` 用 top-k + Switch aux；aux-free bias 与 node-limited 在 `moe_balance.py` 中独立实现、未接入 | aux-free bias、shared expert、fine-grained、batch-invariant |
| 检查点 | 同布局保存 | EP 专家重排、任意 ep 重分片 |
| CP + MoE aux | 只在本地 CP 序列切片上统计 | 跨 CP 的全局 `f_i/P_i` 归约 |
| 推理 | 无 | expert TP、batch-invariant、KV 管理 |

### 25.12.1 `ep < dp`：专家副本的两级梯度

mini 把 EP 定义成 DP 的重新划分（`ep = dp`），因此每个专家只存在一份。
生产 Megatron 允许 `ep < dp`：专家在 EP 组内切分，同时在 `dp/ep` 个
**副本组**里复制。理解这个布局，才能理解“专家副本”的显存/通信/一致性。

设 `dp = ep × r`，`r` 是副本数。rank 可以写成：

```text
rank = ((pp · dp + (ep_idx · r + replica_idx)) · cp + cp_idx) · tp + tp_idx
```

- **EP 组**：固定 `(pp, cp, tp, replica_idx)`，变化 `ep_idx`，大小 `ep`；
- **副本组**：固定 `(pp, cp, tp, ep_idx)`，变化 `replica_idx`，大小 `r`；
- 每个专家在 `r` 个副本组里各有一份；每份只看到本副本组的 batch。

前向/反向的语义：

```text
1. 路由在副本内复制（同一副本组的 rank 看到同一批 token）
2. dispatch/combine 只在 EP 组内 all-to-all（专家不跨副本）
3. 专家梯度先在本副本组内累计（all-reduce over r）
4. 再按**全局**有效 token 数缩放（不是本地 token，也不是简单 `/ep`）
5. 稠密参数仍在完整 DP 组内 all-reduce
```

关键点：

- **专家副本之间必须同步**。如果只在本 EP 组内更新、不做副本 all-reduce，
  同一专家的 `r` 份权重会分叉；checkpoint 加载后“看起来是同一个专家”，
  实际是 r 个不同模型。同步的通信量是 `N_expert/ep` 每 rank，通常远小于
  DP 的稠密梯度 all-reduce。
- **容量按副本组算**。每个专家副本只收到本副本组的 token；总容量约是
  单副本的 `r` 倍。把 `ep=dp` 的容量公式直接套到 `ep<dp` 会低估。
- **放置决定故障域**。副本放在不同节点可以抗单节点故障，但会拉长
  all-to-all/同步路径；放在同一 NVLink 域则快，但故障域重叠。
- **checkpoint 只存一份还是 r 份**。存一份要在加载时广播到所有副本；存 r 份
  要校验 checksum 一致。两种都可以，但 metadata 必须写清 `ep/r` 与专家 id
  映射（见 50.5.1）。
- **负载均衡**：副本组的 token 分布可能不同，热门专家在某个副本上更热；
  EPLB/副本路由要按副本组统计，而不是全局平均。

一个最小算例：`dp=8, ep=2, r=4`，专家 0..E/2-1 在 EP rank 0，E/2..E-1
在 EP rank 1；每个专家有 4 份，分别属于 4 个副本组。一个副本组内，
dispatch 只在 2 个 EP rank 间发生；专家梯度先在 4 个副本间 all-reduce，
再进入全局优化器。mini 不支持这个布局，但它的 `ep = dp` 可以看作 `r=1`
的特例；把 `r` 加回来，主要是多一个副本组和一次专家梯度归约。

## 25.13 本章小结

- MoE = 路由 + 容量 + 专家 GEMM + 辅助损失；复制版足以理解数值。
- EP 把专家变成分布式状态，核心是变长 all-to-all 和索引映射。
- dispatch/combine 的索引方向、容量截断、专家梯度缩放是三个高频 bug。
- mini 用 padding + Python 循环换取可读性；生产用 split sizes + grouped GEMM。
- EP 的验证必须对比“全局 batch”的 loss/logits/aux/梯度，不能只看本地 loss。

下一章讨论推理与生成：从 KV cache 到采样，再到训练/推理的一致性。
