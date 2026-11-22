# 第 21 章 流水线调度实现

本章拆解 `mini_megatron/pipeline/schedules.py`。生产 Megatron 的
`pipeline_parallel/schedules.py` 有 interleaved、flexible、zero-bubble 等更多
分支，但 1F1B 的依赖关系是共同基础。

## 21.1 类结构

```text
BaseSchedule
├─ SingleStageSchedule   # PP=1
├─ GPipeSchedule         # 全部前向再全部反向
└─ OneForwardOneBackward # 1F1B

build_schedule(stage, parallel, train) -> BaseSchedule
```

`BaseSchedule` 负责：

- 记录 micro-batches、stage、pp rank；
- 计算本地序列长度、激活 shape、dtype；
- `_forward_step` / `_backward_step`；
- `forward_only`（评估）；
- `_finish_stats`（跨 PP 汇总 loss）。

## 21.2 PipelineCommunicator

```python
class PipelineCommunicator:
    def __init__(self, group, rank, world_size, activation_shape, dtype,
                 device=None):
        self.global_ranks = [comm.get_global_rank(group, i) for i in range(world_size)]
```

- `rank` 是 PP group 内的局部 rank；
- `has_prev = rank > 0`，`has_next = rank < world_size-1`；
- `_global(local)` 映射到全局 rank；
- `send_forward`：`isend(tensor, dst=next_global, tag=1000+mb)`；
- `recv_forward`：`recv(buffer, src=prev_global, tag=1000+mb)`；
- `send_backward`：`send(grad, dst=prev_global, tag=2000+mb)`；
- `recv_backward`：`recv(buffer, src=next_global, tag=2000+mb)`。

### 为什么用 tag 编码 mb

如果所有前向消息都用同一个 tag，接收方无法区分 micro-batch；多个 in-flight
消息会串线。tag 编码 mb id 后，每个消息有唯一匹配键。反向同理。

### 为什么 isend 后要 wait

`isend` 返回时数据可能还没发完；如果立即反向并覆盖激活 buffer，会发送错误
数据。mini 在 `_backward_step` 开始前 `wait_forward(mb)`。生产实现用双缓冲/
事件进一步重叠。

## 21.3 forward step

```python
def _forward_step(self, mb):
    ids, labels = self.micro_batches[mb]
    if self.is_first:
        out = self.stage(input_ids=ids, labels=labels if self.is_last else None)
    else:
        hidden = self.communicator.recv_forward(mb)
        hidden.requires_grad_(True)
        out = self.stage(hidden_states=hidden, labels=...)
    out.pipeline_input = hidden
    if not self.is_last:
        self.communicator.send_forward(out.hidden_states, mb)
    self._forwards[mb] = out
```

注意：

- 第一个 stage 不需要 recv；
- 最后一个 stage 不需要 send；
- 非第一个 stage 的输入是叶子张量，`requires_grad_(True)` 以便反向时拿
  `hidden.grad` 传给上一级；
- `labels` 只传给最后一个 stage（其他 stage 不需要算 loss）；
- `out.pipeline_input` 是动态属性，用于 backward 时取 `grad`。

## 21.4 backward step

```python
def _backward_step(self, mb):
    out = self._forwards[mb]
    if not self.is_last:
        self.communicator.wait_forward(mb)
        grad_output = self.communicator.recv_backward(mb)
    else:
        grad_output = None

    scale = 1 / num_microbatches
    tensors = [out.loss * scale] if last else [out.hidden_states]
    grads = [ones] if last else [grad_output]
    if aux.requires_grad: tensors.append(aux * scale); grads.append(ones)
    torch.autograd.backward(tensors, grads)

    if not first:
        send_backward(out.pipeline_input.grad, mb)
```

关键点：

- loss 除以 micro-batch 数，实现梯度累积；
- MoE aux 与主 loss 一起反向，避免共享图被释放；
- 非第一个 stage 用输入叶子张量的 `.grad` 作为给上一级的梯度；
- 最后一个 stage 不需要 `recv_backward`；第一个 stage 不需要
  `send_backward`。其余 stage 既收又发：`recv_backward` 从下一 stage 取
  输出梯度，`send_backward` 把输入梯度交给上一 stage。

## 21.5 1F1B 调度

```python
warmup = min(n, max(1, pp_size - pp_rank - 1))
for mb in range(warmup):
    forward(mb)
for i in range(n - warmup):
    backward(i)
    forward(i + warmup)
for i in range(n - warmup, n):
    backward(i)
```

依赖关系：

- 第一个 stage：没有上游要等，经典公式的 warmup 最大；
- 最后一个 stage：没有下游要等，经典 `warmup = pp_size - pp_rank - 1` 为 0；
- 中间 stage：需要上游激活和下游梯度，warmup 介于两者之间。

但**这段循环写法**在 `warmup=0` 时会先执行 `backward(0)`，而 `forward(0)`
还没发生；`max(1, ...)` 是对循环结构的补偿，不是流水线依赖（详见 8.3）。
如果改成“最后一个 stage 单独先 forward、再 `backward(i); forward(i)`”的
写法，就可以用真正的 warmup=0。如果 `n=0`，直接返回零 loss。

### 一个死锁案例

`PP=2, DP=2`，PP group 是 `[0,2]` / `[1,3]`。早期 `PipelineCommunicator` 用
`dst = rank + 1`（局部 rank），rank 0 会把激活发给 global rank 1（属于另一个
PP group），rank 2 永远等不到。修复为 global rank 映射后，所有组合通过。
调试这类问题的关键是打印每个 rank 的 `(pp_rank, global_ranks, src, dst, tag)`。

## 21.6 GPipe

```python
for mb in range(n): forward(mb)
for mb in reversed(range(n)): backward(mb)
```

- 所有前向的激活都保留，显存随 n 增长；
- 反向顺序与依赖一致；
- 用相同权重/数据时，GPipe 与 1F1B 的最终 loss/grad_norm 应一致；当前回归
  测试只比较最后一个 step，不是逐步 history。

## 21.7 forward only

评估时不反向，但 PP 仍要传激活：

```python
for mb in range(n):
    out = forward_step(mb)
    if last: total += out.loss
wait_all_forward()
all_reduce(total, pp_group)
```

`wait_all_forward` 在所有前向发送完成后调用，避免进程退出时消息未送达。
评估时 `torch.no_grad()` 下 `hidden.requires_grad_(True)` 仍可设置，但不会建图；
如果实现依赖 `.grad`，要确保只在训练路径使用。

## 21.8 形状与 dtype

`activation_shape = (micro_batch, local_seq, hidden)`：

```python
local_seq = seq_len // cp
if sequence_parallel:
    local_seq //= tp
```

如果实际 stage 输出的 shape 不一致，`recv` buffer 会过大/过小。生产实现会传
metadata 或从配置计算；mini 在 `_refresh_shape` 中按 batch 修正 batch 维。

dtype 用 `next(stage.parameters()).dtype`。autocast 下 p2p dtype 可能不匹配；
mini 通过配置禁止 `autocast + PP`，生产需要显式 dtype 协商。

## 21.9 测试

- `parallel_equiv.py --case pp`：PP=2 vs PP=1 的 loss/梯度；
- `tiny_pp2.json` 与 `tiny_pp2_gpipe.json`：**最终 step** 的 loss/grad_norm
  一致（不是逐步 history；两种 schedule 的中间激活顺序不同，逐步比较没有
  意义）；
- `tiny_tp2_pp2.json`：TP+PP；
- `tiny_pp2_dp2.json`：PP+DP（global rank bug 的回归测试）；
- `tiny_cp2_pp2.json`：CP+PP。

## 21.10 VPP/Interleaved 的规划器

`mini_megatron/pipeline/interleaved.py` 不实现生产 1F1B 的全部细节，但把
VPP 最容易混淆的三件事显式化：

1. **chunk 归属**：`rank = chunk % pp_size`，rank 的本地 chunk 是
   `chunk // pp_size`；
2. **层映射**：每个 chunk 持有 `num_layers / (pp*v)` 层；
3. **依赖校验**：`F(mb, chunk)` 必须等上游 chunk；`B(mb, chunk)` 必须等本地
   forward 和下游梯度。

```python
from mini_megatron.pipeline.interleaved import VirtualPipelineLayout, gpipe_interleaved_trace

layout = VirtualPipelineLayout(pp_size=4, chunks_per_rank=2, num_layers=16)
trace = gpipe_interleaved_trace(layout, microbatches=8)
# 校验依赖；生产 1F1B 的顺序更复杂，但依赖约束相同
```

气泡估算：

```text
非 interleaved: (p-1) / (m + p - 1)
VPP:            (p-1) / (m·v + p - 1)
```

例如 `p=8, m=32`：非 interleaved 为 `7/39 ≈ 17.9%`，`v=2` 时
`7/71 ≈ 9.86%`，`v=4` 时 `7/135 ≈ 5.19%`。公式假设各 stage 计算均匀；不均匀
切分、p2p 延迟和 warmup/cooldown 会让实际气泡偏离这个值。
代价是每个 rank 要保存更多 chunk 的激活/调度状态，且模型层数必须能被
`p·v` 整除。

CLI：

```bash
python -m mini_megatron.pipeline.interleaved --pp 4 --chunks-per-rank 2 --layers 16 --microbatches 8
```

生产 Megatron 的 `get_forward_backward_func()` 会处理 warmup/cooldown、激活
内存、p2p 顺序和通信重叠；mini 的规划器只帮助你理解 chunk 映射与气泡。

## 21.11 可执行的 VPP：`pipeline/interleaved_exec.py`

规划器只验证 trace，不跑模型。`InterleavedGPipeExecutor` 把同一个布局真正
跑起来：每个 rank 持有 `v` 个 chunk，micro-batch 依次经过全局 chunk
`0..p·v-1`，通信在 chunk owner 之间进行。

### 21.11.1 构造

```python
from mini_megatron.pipeline.interleaved_exec import InterleavedGPipeExecutor

executor = InterleavedGPipeExecutor(
    config.model, parallel, config.train, chunks_per_rank=2
)
loss, aux = executor.forward_backward(micro_batches)
```

每个 chunk 是一个 `TransformerStage`，但用显式参数覆盖默认的连续切分：

```python
TransformerStage(
    config, parallel,
    layer_indices=list(layout.layers_of_chunk(global_chunk)),
    pp_rank=layout.owner(global_chunk),
    pp_size=pp_size,
    owns_embedding=(global_chunk == 0),
    owns_final=(global_chunk == num_chunks - 1),
)
```

这样 rank 0 可以同时拥有 chunk 0（embedding）和 chunk 2（中间层），rank 1
拥有 chunk 1 和 chunk 3（final/loss）。`TransformerStage` 的 `is_first`/
`is_last` 由 chunk 的全局位置决定，而不是由 rank 决定。

### 21.11.2 调度与环形通信

调度是 **interleaved GPipe**：

```text
for mb in 0..M-1:
    for chunk in 0..p·v-1:
        owner(chunk) 执行 F(mb, chunk)
for mb in M-1..0:
    for chunk in p·v-1..0:
        owner(chunk) 执行 B(mb, chunk)
```

与普通 PP 的关键区别：

- **owner 序列是环形的**：`owner(c) = c % p`，所以 chunk `p-1` 的下一跳是
  rank 0，而不是 rank p。p2p 的 dst 必须按 `owner(c+1)` 计算，不能用
  `rank+1`。
- **buffer 以 `(mb, chunk)` 为键**：同一 rank 可能同时持有多个 chunk 的激活，
  不能只用 mb 索引。
- **tag 编码 `(mb, chunk)`**：前向/反向各一套 tag，避免环形复用串线。
- **local/remote 判定看“发送方”**：接收 hidden 时看 `owner(c-1)`，接收梯度
  时看 `owner(c+1)`。把两者都写成 `owner(c)` 是常见 bug（会导致本地
  `KeyError` 或远程死等）。

### 21.11.3 验证结果

`tests/parallel_vpp.py` 用 world=2、pp=2、v=2、4 层（每 chunk 1 层）对比
PP=1 参考模型：

```bash
python tests/parallel_vpp.py --world-size 2 --out "$TEMP/vpp.json"
```

一次实测：

```json
{"rank": 0, "loss_diff": 0.0, "grad_diff": 0.0, "num_chunks": 2}
```

它比较的是全部本地层、embedding、final norm 和 output layer 的梯度，不只是
loss。这说明环形 chunk 映射、`(mb,chunk)` buffer 和反向路由都是对的。

### 21.11.4 与生产 interleaved 1F1B 的差距

- 这里是 **GPipe 顺序**（全部前向再全部反向），激活显存是 `O(M·v)`；
  生产用 interleaved 1F1B，激活是 `O(p·v)` 级别。
- 没有 warmup/cooldown 的细粒度交错；没有 zero-bubble 的
  `B_input/B_weight` 拆分。
- 没有通信/计算重叠；p2p 是同步 send/recv。
- 没有把 VPP 接进 `Trainer`/checkpoint；它是一个可运行的调度参考实现；
- `tie_word_embeddings` 与多 chunk VPP 组合会显式拒绝（embedding 和 output
  落在不同 chunk，不能共享同一个 `Parameter`）。

即便如此，它已经覆盖了 VPP 最容易错的三件事：chunk 归属、环形通信、按
`(mb,chunk)` 管理激活。把这里的 trace 换成 1F1B 的 warmup/steady/cooldown，
就是生产调度器的骨架。

## 21.12 本章小结

- PP 的核心是 p2p 通信与调度依赖；tag、global rank、wait 是三大坑。
- 1F1B 用 warmup/steady/cooldown 降低激活；经典公式在最后 stage 的 warmup=0，
  `max(1)` 只是当前循环写法的补偿，不是依赖。
- loss 要按 micro-batch 数缩放；aux 要与主 loss 一起反向。
- 评估路径也要正确收尾，避免悬挂 send。
- VPP 的可执行版在 `interleaved_exec.py`：chunk 归属、环形 p2p、
  `(mb,chunk)` 激活是三个关键点；生产在此之上加 1F1B/zero-bubble/重叠。

下一章拆解数据并行与 ZeRO-1。
