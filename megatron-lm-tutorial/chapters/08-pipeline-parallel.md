# 第 08 章 流水线并行：GPipe、1F1B 与气泡

## 8.1 为什么需要流水线

TP 把一层切开，但层数很多、跨节点时，TP 的 all-reduce 会成为瓶颈。流水线并行
（PP）换一个维度：**把不同的层放到不同 GPU 上**。Stage 0 算前几层，把激活发给
Stage 1；Stage 1 算后几层，依此类推。

朴素实现的问题是 GPU 空转：Stage 0 在算时，Stage 1 只能等；Stage 1 在算时，
Stage 0 又空闲。解决办法是把一个 global batch 切成多个 **micro-batch**，让不同
stage 同时处理不同 micro-batch。

## 8.2 GPipe：先全部前向，再全部反向

GPipe 的调度最简单：

```text
for mb in 1..m:
    forward(mb)       # 所有 stage 依次处理
for mb in m..1:
    backward(mb)
```

优点：实现简单，梯度累积的语义清晰。  
缺点：必须保存 `m` 个 micro-batch 的激活，显存随 `m` 线性增长；前向和反向
之间有明显空档。

在 mini 中对应 `GPipeSchedule.forward_backward()`：

```python
for mb in range(n):
    self._forward_step(mb)
for mb in reversed(range(n)):
    self._backward_step(mb)
```

## 8.3 1F1B：前向一个，反向一个

1F1B（one forward one backward）在 warmup 之后交替执行：

```text
warmup:   F0 F1 ... F(w-1)
steady:   B0 F(w) B1 F(w+1) ...
cooldown: B... B(m-1)
```

这样每个 stage 同时只保留约 `pp-1` 个 micro-batch 的激活（m 很小时取 `m`；
把 `pp` 当作上界也可以），而不是 `m` 个，显存显著下降；反向的梯度也能更早
开始流动，通信与计算更容易重叠。

mini 的 `OneForwardOneBackward`：

```python
warmup = min(n, max(1, pp_size - pp_rank - 1))
for mb in range(warmup):
    self._forward_step(mb)
for i in range(n - warmup):
    self._backward_step(i)
    self._forward_step(i + warmup)
for i in range(n - warmup, n):
    self._backward_step(i)
```

### 为什么 warmup 公式要加 `max(1, ...)`

经典公式是 `pp_size - pp_rank - 1`，对最后一个 stage 会得到 0。注意：**最后
一个 stage 的 warmup=0 本身是合法的**——它只是没有下游要等，理论上可以在
收到第一个激活后立刻反向。mini 加 `max(1, ...)` 是因为上面的循环结构写成了

```text
for i in range(n - warmup):
    backward(i); forward(i + warmup)
```

当 `warmup=0` 时第一轮会先 `backward(0)`，而 `forward(0)` 还没发生。也就是
说，`max(1)` 是对**循环写法**的补偿，不是流水线依赖的要求。生产实现通常把
最后一个 stage 单独处理（先 warmup 0 次，再按 `backward(i); forward(i)` 的
顺序），或把 forward/backward 拆成两个显式队列。理解这一点，才能在改写
schedule 时不引入“反向先于前向”的死锁。

### 气泡比例

无 interleaving 的 1F1B，气泡近似：

```text
bubble ≈ (p - 1) / (m + p - 1)
```

例如 `p=8, m=32`，气泡约 18%；`m=128` 时约 5%。增大 `m` 能降低气泡，但激活
和通信次数增加。生产会用 interleaved/VPP（virtual pipeline）把一层拆成多个
chunk，进一步降低气泡。

## 8.4 点对点通信

PP 的通信是相邻 stage 之间的点对点：

- 前向：stage `i` 把激活发给 `i+1`；
- 反向：stage `i+1` 把梯度发给 `i`。

mini 的 `PipelineCommunicator` 使用：

```python
comm.isend(tensor, dst=next_global_rank, tag=1000 + mb)
comm.recv(tensor, src=prev_global_rank, tag=1000 + mb)
```

三个细节：

1. **global rank**：`pp_rank` 是 PP group 内的局部 rank，不能直接当全局 rank
   用。`group [0,2]` 的 local rank 1 是 global rank 2，不是 1。mini 早期版本
   直接用 `rank+1`，在 `PP×DP` 时死锁；修复方式是用
   `comm.get_global_rank(group, local_rank)`。
2. **tag**：前向和反向使用不同 tag，且把 microbatch id 编进 tag，避免不同
   micro-batch 的消息串线。
3. **异步 send**：`isend` 返回 work，在反向使用该激活前 `wait`，避免发送缓冲
   被覆盖；也可以用双缓冲进一步重叠。

## 8.5 激活形状与布局

PP 传输的激活形状由并行配置决定：

```text
[b, s, h]                    # 普通 TP/PP
[b, s/t, h]                  # + sequence parallel
[b, s/cp, h]                 # + context parallel
[b, s/(tp·cp), h]            # 若同时开 SP 和 CP（mini 不支持）
```

mini 的 `BaseSchedule` 计算 `local_seq`：

```python
local_seq = train.seq_len // cp_size
if sequence_parallel:
    local_seq //= tp_size
```

如果这个形状和实际 stage 输出不一致，会出现 recv buffer 大小错误或静默截断。
生产实现会传递 shape/dtype 元数据或使用固定配置，避免“刚好能跑”的隐式假设。

## 8.6 损失与梯度累积

PP 下只有最后一个 stage 能算 loss。其他 stage 收到的是上游梯度。mini 在
`_backward_step` 中：

- 最后一个 stage：`loss * (1/num_microbatches)`，反向；
- 其他 stage：收到 `grad_output`，对 `hidden_states` 反向；
- 如果有 MoE aux loss，也按 `1/num_microbatches` 缩放并一起反向；
- 每个 stage 的输入若需要继续向上一级传梯度，使用 `pipeline_input.grad`。

`_finish_stats` 在 PP group 内 all-reduce loss/aux，让所有 stage 都能记录同一个
全局 loss。注意这不是把不同 stage 的 loss 相加；非最后 stage 的 loss 为 0，
all-reduce 只是广播。

## 8.7 显存与调度

1F1B 的峰值激活大致与 `p` 成正比，而不是 `m`；这是它相对 GPipe 的核心优势。
但 1F1B 仍然需要保存每个 in-flight micro-batch 的输入/输出和计算图。进一步
优化：

- **activation checkpointing**：每个 block 只存输入，反向重算；
- **VPP/interleaving**：把每个 stage 的层分成多个 virtual chunk，让一个 stage
  在等待时处理另一个 chunk；
- **zero-bubble**：把反向拆成输入梯度和权重梯度，用空闲 slot 填充；
- **通信重叠**：p2p send/recv 与 GEMM 异步。

Megatron 的 `get_forward_backward_func()` 会根据配置选择 non-interleaved、
interleaved、flexible 等 schedule。mini 实现了 non-interleaved 的 GPipe/1F1B，
以及一个可执行的 interleaved GPipe（`pipeline/interleaved_exec.py`）；调度
顺序和依赖关系与生产一致，但没有 zero-bubble/重叠。

### 8.7.1 VPP 的时间线

VPP 把每个 stage 的层切成 `v` 个 chunk，chunk 按 `chunk % p` 分配给 rank：

```text
rank 0: chunk 0, p, 2p, ...
rank 1: chunk 1, p+1, 2p+1, ...
```

一个 micro-batch 仍然按 `0..p·v-1` 的顺序走完所有 chunk。好处是：当 rank 0
的 chunk 0 在等下游梯度时，它可以先算 chunk p。气泡从：

```text
(p-1) / (m + p - 1)
```

降到：

```text
(p-1) / (m·v + p - 1)
```

代价：

- 每个 rank 要维护更多 chunk 的激活/调度状态；
- 层数必须能被 `p·v` 整除；
- p2p 消息更多、tag/顺序更复杂；
- 负载不均时，chunk 之间的依赖更容易放大尾延迟。

### 8.7.2 zero-bubble

1F1B 的反向包含两部分：

```text
B = B_weight + B_input
```

- `B_input` 算输入梯度，必须尽快传给上游；
- `B_weight` 算权重梯度，可以延后。

Zero-bubble 把 `B_weight` 填到流水线气泡里，理论上把气泡降到接近 0，代价是：

- 权重梯度要延迟/额外存储；
- 调度器更复杂；
- 对显存/通信重叠要求更高。

具体有两个常用变体：

| 变体 | 拆分方式 | 气泡（理想、无通信） | 额外代价 |
|---|---|---|---|
| 1F1B | 不拆 | `(p-1)/(m+p-1)` | 基线 |
| ZB1P | `B = B_input + B_weight`，`B_weight` 延后 | 约 `(p-1)/m` | 每个 stage 多存一份延迟的权重梯度/激活 |
| ZB2P | 进一步把 `B_input` 拆成依赖上游/不依赖上游两段 | 约 `(p-1)/(2m)` | 调度槽位与 buffer 再增加，通常需要更多显存 |

这些公式假设每个 micro-batch 的计算量相等、通信可忽略、`m` 足够大；真实
系统还要加上 p2p 延迟、stage 不均、重算和 optimizer 时间。直觉是：1F1B 的
气泡来自“warmup/drain 的 `p-1` 个槽位”，zero-bubble 用**可延后的权重梯度**
填这些槽位，所以 `m` 越大、`p` 越小，收益越有限；反之 `p` 大、`m` 小
（例如长上下文导致 micro-batch 数少）时收益明显。

判断是否值得：

```text
预期收益 ≈ (bubble_1F1B - bubble_ZB) / (1 - bubble_1F1B)
显存余量  ≥ 额外延迟 buffer + 调度状态
```

如果模型已经 OOM，或 `m ≈ p` 导致调度器的依赖非常紧，zero-bubble 可能
得不偿失。生产实现（Megatron 的 zero-bubble schedule 选择）还需要与
interleaved/VPP、激活重算、通信 overlap 一起验证；mini 只实现 GPipe、
1F1B 与 interleaved GPipe，**没有 zero-bubble 执行器**。本章给出公式与
判断方法，代码层面的调度参考见 `pipeline/interleaved.py` 的依赖校验。

### 8.7.3 mini 的 VPP 规划器与执行器

`mini_megatron/pipeline/interleaved.py` 提供 chunk 映射、依赖校验和气泡估算；
`mini_megatron/pipeline/interleaved_exec.py` 把同一个布局真正跑起来（interleaved
GPipe），并已用 2 进程对比 PP=1 的 loss/梯度验证。你可以用它验证：

- `chunk % p` 的归属；
- 每个 chunk 的层范围；
- 一个 forward/backward trace 是否满足依赖；
- VPP 气泡是否按公式下降；
- 环形 p2p 和 `(mb,chunk)` 激活管理是否正确（见 21.11）。

生产 schedule 的 warmup/cooldown、interleaved 1F1B、zero-bubble 和通信重叠
仍以 `megatron/core/pipeline_parallel/schedules.py` 为准。

## 8.8 PP 与 DP/TP 的组合

- **PP + DP**：每个 PP stage 有独立的 DP 组；梯度只在同一 stage 的 DP 副本间
  all-reduce（不同 stage 参数不同，不能互相同步）。
- **PP + TP**：每个 stage 内部做 TP；p2p 传输的是 TP 后的激活。
- **PP + CP**：每个 stage 的序列再按 CP 切；p2p 传 `[b, s/cp, h]`。
- **PP + MoE**：stage 边界要和专家层对齐，避免一个专家跨 stage 切开。

mini 的 `init_model_parallel` 按固定 rank 布局同时建四组 group；PP group 是
`[pp_idx]` 变化、其他维固定的 rank 集合。`PipelineCommunicator` 用 PP group
的 global ranks 寻址。

## 8.9 常见故障

| 现象 | 原因 | 排查 |
|---|---|---|
| 卡在 recv | src/dst 用了局部 rank | 打印 `global_ranks`，确认 group |
| 收到错误 micro-batch | tag 冲突/未编码 mb | 给每个 mb 唯一 tag |
| loss 不下降 | 梯度没传回上一个 stage | 检查 `pipeline_input.grad` 和 send |
| 显存随 m 线性涨 | 用了 GPipe 而非 1F1B | 切换 schedule/减小 m |
| 最后一个 stage OOM | LM head/CE 太大 | TP 词表、activation checkpoint、chunked CE |
| 气泡很大 | m 太小/PP 太深 | 增大 grad accumulation，VPP |

## 8.10 mini 验证

`tests/parallel_equiv.py --case pp` 会：

1. 在 TP=1/PP=1 下建完整模型，算 loss 和梯度；
2. 用 `sharding.py` 把权重切到 PP=2 的两个 stage；
3. 用 1F1B 跑一个 micro-batch；
4. 比较 loss 和第 0/1 层的 q_proj 梯度。

结果应与参考实现一致（误差 ~1e-8）。`test_distributed_training_smoke` 覆盖
七组 2 进程配置；`TP=2+PP=2`、`PP=2+DP=2`、`CP=2+PP=2` 等 4 进程组合在
`scripts/verify.py` 的多进程矩阵里。GPipe 与 1F1B 比较的是**最终 step** 的
loss/grad_norm，不是逐步 history（见 21.9）：两种 schedule 的中间激活顺序
不同，逐步比较没有意义。

## 8.11 非均匀层切分、flexible PP 与双向调度

前面的推导默认“每段层数相同、每段耗时相同”。真实模型很少满足这个假设：
第一段有 embedding，最后一段有 LM head/CE，中间层还可能有 MoE 与 dense
交替。层数均匀不等于耗时均匀。

### 8.11.1 为什么需要非均匀切分

设 stage `i` 的耗时 `t_i`。管道稳态吞吐由 `max_i t_i` 决定，而不是平均
耗时；总时间近似：

```text
T ≈ Σ_i t_i + (m - 1) · max_i t_i
```

把 embedding/CE 单独放在一段，或让 MoE 层集中在某段，都会把 `max_i t_i`
抬高。非均匀切分的目标是让**每段耗时接近**，而不是每段层数相等。三种常见
做法：

| 做法 | 具体做法 | 代价 |
|---|---|---|
| 首/尾段预留 | 第一段少放几层（embedding 贵），最后一段同理 | 需要知道各层相对耗时 |
| 按耗时切 | 用 profile 的每层时间做前缀和，按等分位切 | profile 可能随 batch/序列变化 |
| flexible layout | 用布局字符串描述每段层数/层型 | 依赖框架支持，配置可读性差 |

Megatron 的常见入口包括首/尾段层数参数（如
`--decoder-first-pipeline-num-layers` / `--decoder-last-pipeline-num-layers`）
和 flexible pipeline layout（如 `--pipeline-model-parallel-layout` 一类的
布局描述）。**具体 flag 名与布局语法随版本变化，以当前源码的
`arguments.py` 与 pipeline 测试为准**；不要从博客里抄一个布局字符串就上
大规模。

### 8.11.2 非均匀切分的三个不变量

无论层数怎么分，下面三条不能变：

1. **层号连续**：stage `i` 的最后一层必须紧接 stage `i+1` 的第一层；
   embedding 只在第一段，final norm/LM head 只在最后一段。
2. **p2p 仍是邻居**：非均匀不改变拓扑，只改变每段的工作量；tag/全局 rank
   的规则与均匀切分相同。
3. **loss 只在最后一段**：如果 CE 被拆到单独 stage，反向仍要从 CE 所在段
   把梯度传回语言模型段。

非均匀切分最容易引入的 bug 是“层号映射错一位”：某层同时出现在两个 stage，
或中间缺一层。用 `sharding.py` 的完整 state_dict 切片做一次逐键对齐，比看
loss 可靠得多。

### 8.11.3 zero-bubble 与非均匀切分的关系

zero-bubble 的 B/W 拆分、ZB1P/ZB2P 气泡公式和代价已在 **8.7.2** 展开，
这里不重复。它与非均匀切分的交集只有一点：**zero-bubble 的收益依赖各 stage
的耗时均衡**。如果某个 stage 因为层数多/embedding 重而成为瓶颈，延后的
权重梯度会在该 stage 排队，零气泡的理论收益被 straggler 吃掉。因此做
non-uniform/flexible PP 时，要同时看“每段前向时间”和“每段可填气泡的 W
工作量”，不能只按层数均分。mini 没有 zero-bubble 执行器；源码阅读时先找
“backward 是否分成 input-grad/weight-grad 两个队列”，再看 scheduler 的
warmup/cooldown 如何填 `W`。

### 8.11.4 DualPipe 与 Megatron 的对应关系

DeepSeek-V3 技术报告描述的 **DualPipe** 是另一种思路：每个设备同时持有
管道两端的一部分层，前向/反向按两个方向配对，使一个方向的 warmup 与另一个
方向的 cooldown 重叠。它减少气泡，也改变激活的存放位置。

需要区分三件事：

| 名称 | 是什么 | 与 Megatron 的关系 |
|---|---|---|
| VPP/interleaved | 每个 rank 持有多个 chunk，按 chunk 轮转 | Megatron 有成熟实现 |
| zero-bubble | 拆分 B/W 反向填气泡 | Megatron 有 ZB1P/ZB2P 等实现 |
| DualPipe | 双向、成对 chunk 的调度 | DeepSeek 报告的模型/系统协同设计；Megatron 没有一一对应的公开 flag，不能假设“开某个开关就是 DualPipe” |

判断某个框架“支持 DualPipe”时，至少要看到：调度器的伪代码/实现、chunk
到设备的映射、以及一个可复现的气泡/显存对比。只有博客里的一张示意图，
按 36.1 的分级只能算 L3/L4，不能作为选型依据。

## 8.12 本章小结

- PP 切层，micro-batch 填气泡；GPipe 简单但费激活，1F1B 更省显存。
- p2p 必须用全局 rank 和唯一 tag；这是分布式死锁的高发区。
- 只有最后一个 stage 算 loss，其他 stage 靠上游梯度反向。
- 气泡随 p 增大、随 m 减小；VPP/zero-bubble 是生产优化方向。
- 层数均匀不等于耗时均匀；非均匀/flexible PP 要让每段耗时接近。
- DualPipe 是双向调度，不等于 VPP 或 zero-bubble；支持声明要看实现与数据。

下一章进入上下文并行：当序列长到单卡放不下时，怎样切“时间”。
