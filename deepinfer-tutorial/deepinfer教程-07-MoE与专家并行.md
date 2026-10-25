# 第 7 章 MoE 与专家并行

本章导读：MoE 把“参数量”和“每 token 计算量”解耦，但也把推理系统从“算一个稠密模型”变成“路由、分发、计算、回收”四步。本章讲清 top-k 路由、共享专家、EP 全对全、Mega MoE、EPLB 的两阶段算法，以及 Wide-EP 为什么能降低 decode 的每步内存流量。

## 7.1 MoE 的一层在做什么

一个 MoE 层包含一组路由专家、一个共享专家和一个门控。给定 hidden state `h`，门控先算每个专家的分数：

```text
logits = h W_gate
p = softmax(logits)
top = topk(p, k)
weights = p[top] / sum(p[top])
y = shared_expert(h) + sum_i weights_i * expert_{top_i}(h)
```

DeepSeek-V4.1 每层有 1 个共享专家、384 个路由专家，top-6。门控使用 sqrtsoftplus 打分和 noaux_tc 选择，带一个 load-correction bias，避免专家负载过度集中。共享专家每个 token 都算，路由专家只算被选中的 6 个。

MoE 的“参数效率”来自这里：模型总参数可以很大，但每个 token 只激活一小部分。V4.1 的 backbone 约 552B，prefill 每 token 激活约 8B，decode 约 16B。激活参数越少，decode 每步要读的权重越少，带宽压力越小。

路由不是“取最大的 k 个”这么简单。训练时通常要加负载均衡损失或辅助路由（aux loss），防止所有 token 挤到少数专家；推理时则用一个 load-correction bias 调整分数。DeepSeek 的 noaux_tc 选择配合 sqrtsoftplus，目标是在不引入额外训练损失的情况下让路由更平滑。这些细节会影响专家负载分布，进而影响 EPLB 的效果。

MoE 也带来了新的不均衡。一个 batch 里，某些专家可能被很多 token 选中，另一些几乎空闲。如果专家均匀放在 8 张卡上，热点专家所在卡会成为瓶颈，其他卡空转。EPLB 就是为解决这个问题而生。

用公式看，一个 batch 的总专家计算量是 `tokens × top_k`，但每个专家的 token 数取决于路由分布。即使平均每专家 `tokens × top_k / n_experts`，方差也会让某些专家高出 30% 到 100%。`moe_lb = max_rank_load / mean_rank_load` 就是衡量这个偏差的指标。

## 7.2 专家并行

专家并行（EP）把专家分散到多张 GPU。每张卡只持有 `num_physical_experts / ep_size` 个专家。前向时，每个 token 的 top-k 专家可能在不同卡上，所以需要一次 all-to-all：把 token 的 hidden 发给拥有目标专家的卡，算完再把结果发回来。

```text
rank 0: tokens -> dispatch -> rank 0/1/2/3 的本地专家
rank 1: tokens -> dispatch -> ...
        <- combine <-
```

EP 的通信量取决于 `tokens × top_k × hidden_size × dtype`。以 V4.1 的 decode 为例，如果每步 64 个 token、top-6、hidden 5120、BF16，一次 dispatch 的原始数据量约 `64 × 6 × 5120 × 2 ≈ 3.9 MB`，combine 再加一倍。这个数字看起来不大，但 all-to-all 的固定延迟、拥塞和尾延迟会让它在 decode 的几毫秒里占很大比例。

DeepInfer 用 DeepEP 做 all-to-all，用 Mega MoE 把 dispatch、专家 GEMM、combine 融合成一个 kernel，减少中间缓冲和启动开销。官方 Q&A 说 Mega MoE 在 scale-up 域内是通用解：在他们测过的 EP 和 batch 范围内，融合版显著快于未融合版。这个结论有明确边界：它依赖 DeepEP 的 buffer、NVLink/supernode 拓扑和手写 kernel，不能推广成“任何 EP、任何 batch、任何通信库下融合都更快”。换一套通信库或跨机 RDMA，通信可能变成瓶颈，融合的收益会被抵消。原因是 MoE 的每个专家 GEMM 都很小，单独启动会有大量 kernel launch 和访存开销；融合后可以把多个专家的 token 拼成大矩阵，提高 tensor core 利用率。

EP 和 TP 的区别值得强调。TP 切的是同一个矩阵的列或行，每层都要 all-reduce，通信频繁但数据量相对可控；EP 切的是专家，token 通过 all-to-all 去对应专家，通信是“按路由稀疏”的。MoE 适合 EP，因为专家之间没有依赖，切分自然；稠密层适合 TP，因为矩阵乘可以按维度切。

## 7.3 Wide-EP 为什么适合 decode

Decode 每步只有少量 token，但要读所有激活专家的权重。如果 EP 较小，每个 rank 持有的专家多，单步要读的权重就多；如果 EP 很大，每个 rank 只持有少量专家，单步读的权重少，但 all-to-all 的 rank 数多、通信更复杂。

DeepInfer 的选择是：prefill 用 EP2 到 EP8，decode 用 EP64 到 EP128。官方解释说，Wide-EP 大幅降低 MoE 专家部分的每步内存流量，配合良好的 EPLB 能有效提高 TPS。代价是官方推荐的“高效部署单元”变成 64 卡 prefill + 64 卡 decode，对小规模部署不友好；这不是硬性下限，公开资料里也有更小 EP 的部署，只是单卡带宽收益会下降，EPLB 的自由度也更小。

这也是一个典型的 roofline 取舍。Decode 受带宽限制，EP 越大，单卡权重越少，带宽时间越短；但 EP 增大后，all-to-all 的固定延迟和负载不均衡会吃掉收益。最优 EP 取决于模型大小、互联带宽、batch 和 SLO，不存在一个对所有场景都成立的数字。

可以把单步时间写成：

```text
t_step ≈ max(weight_bytes(EP) / BW, alltoall_bytes(EP) / link_bw + latency(EP))
```

第一项随 EP 增大而下降，第二项随 EP 增大而上升。两条曲线的交点就是理论最优 EP。实际系统还会被 EPLB 的精度、rank 间抖动和 batch 大小影响，所以要在目标集群上扫点，而不是照搬 64/128。

## 7.4 EPLB 的两阶段算法

EPLB 要解决两个问题：给热点专家分配副本，以及把物理专家放到合适的 rank 上。DeepInfer 的 `eplb/policy.py` 实现了两阶段贪心。

**第一阶段：冗余副本分配。** 每个逻辑专家初始有一个副本。多出来的冗余副本逐个分配给“当前每副本负载最高”的专家。这样热点专家会获得更多副本，负载被摊薄。代码里的 `rebalance_experts_global` 用 `tokens_per_expert / logical_count` 作为每副本负载，每轮选使最大每副本负载最小的专家复制。

**第二阶段：物理放置。** 所有物理专家按负载降序排列，逐个放到“放上去后最大 rank 负载最小”的 rank 上，受每个 rank 的容量约束。代码里的 `_batch_greedy_indices` 会同时考虑多个历史 step 的负载，目标是最小化各 step 的最大 rank 负载之和。这个贪心不是全局最优，但计算快、可扩展，适合在线重平衡。

DeepInfer 的配置经验是：prefill 从每 GPU 4 个冗余专家起步（向上取整，使总专家数能被 ep_size 整除），统计窗口 100 步，每 1000 步重分配一次；decode 从每 GPU 1 个冗余专家起步，窗口 1000 步，每 10000 步重分配一次。实测 `moe_lb`（最慢 rank / 平均）在 prefill 是 1.045 到 1.074，decode 约 1.09。

为什么第一阶段按“每副本负载”而不是“总负载”？因为复制一个专家后，它的负载会被两个副本分担。一个总负载很高但已经有 4 个副本的专家，每副本负载可能并不高；再给它复制，收益不如给一个总负载中等但只有 1 个副本的专家。按每副本负载排序，才能让每次复制都最大化地降低最大瓶颈。

## 7.5 用 mini-deepinfer 观察 EPLB

本仓库的 `mini_deepinfer/eplb.py` 实现了同样的两阶段思路，去掉了多 step 窗口和权重 shuffle。运行：

```bash
python examples/eplb.py
```

在一个 8 专家、负载 `[1200, 900, 700, 300, 120, 60, 30, 10]` 的例子里，不做复制的基线 `moe_lb` 约 1.87；EP=2、4 个冗余专家时降到约 1.01；EP=4、8 个冗余专家时约 1.08。具体数字会随负载和容量变化，但趋势很清楚：冗余专家把热点摊开，贪心放置把剩余的不均衡压到接近 1。

这个实验也暴露了 EPLB 的代价。副本越多，显存占用越大；重分配需要搬运专家权重，DeepInfer 用 NVLink 的 `symm_mem` 做组内 shuffle；重分配期间不能打断推理，需要双缓冲或短暂停顿。EPLB 不是“免费的平均”，而是用显存和通信换负载均衡。

还有一个反直觉的现象：EP=4 的 imbalance 可能比 EP=2 略差。原因是每个 rank 的容量更小，贪心放置的自由度更低；更多 rank 也意味着更多的 all-to-all 对端和更复杂的尾延迟。生产调参要在 EP 规模、冗余专家数、放置自由度之间找平衡，不能只看 `moe_lb` 一个数。

## 7.6 负载不均衡的度量

最常用的指标是 `moe_lb = max_rank_load / mean_rank_load`。1.0 表示完美均衡，1.1 表示最慢的 rank 比平均多 10% 的负载。另一个指标是 `max/mean` 的尾部，或者 P99 的 rank 负载。

注意，`moe_lb` 只看 MoE 部分，不含 attention、通信和调度开销。端到端性能还要看 all-to-all 的尾延迟、专家 GEMM 的利用率、以及 EPLB 重分配的频率。一个 `moe_lb=1.05` 的系统可能因为 all-to-all 抖动而比 `moe_lb=1.10` 的系统更慢。

还可以看“热点专家的 token 占比”和“每 rank 的 token 方差”。前者告诉你路由是否过度集中，后者告诉你放置是否有效。如果热点专家的 token 占比很高，但 EPLB 已经给了很多副本，说明问题可能在路由本身，而不是放置。

## 7.7 代码路径

- `deepinfer/model_executor/models/v4_modules/moe.py`：`TopKGate`、`FusedExperts`、`ReplicatedSharedExpert`。注意注释里的 logical/extra/physical/local 四种专家计数语义。
- `deepinfer/model_executor/eplb/policy.py`：`rebalance_experts_global`、`_batch_greedy_indices`。
- `deepinfer/model_executor/eplb/shuffle.py`：专家权重 shuffle。
- `deepinfer/model_executor/parallel_utils/communication_op.py`：all-to-all、all-gather。
- `csrc/engine/scheduler.hpp`：MTP varlen 和调度侧对 EP 的假设。
- `deepinfer/model_executor/models/v4_modules/moe.py` 的注释明确写了：physical = logical + extra，必须能被 ep_size 整除；local = physical / ep_size。

读这些代码时，先分清四个计数：逻辑专家（模型定义的专家数）、额外副本（冗余）、物理专家（逻辑 + 额外）、本地专家（每个 rank 持有的物理专家数）。很多“专家数对不上”的 bug 都来自把其中一个当成另一个。

## 7.8 一个 EP64 的算例

把公开配置代进公式，看看 Wide-EP 的收益和代价。

假设 decode 每步每个 rank 处理 64 个 token，top-6，hidden 5120，BF16 激活。一次 dispatch 的原始数据量是 `64 × 6 × 5120 × 2 ≈ 3.9 MB`，combine 再加一倍，约 7.8 MB（推导见 7.2 节）。按整机 400 Gbps（约 50 GB/s）且该请求能独占带宽，传输约 156 微秒；如果 8 张卡共享 400 Gbps、单卡只有 6.25 GB/s，传输约 1.25 毫秒。再叠加 all-to-all 的固定延迟和拥塞，单步通信可能到几百微秒到毫秒级。decode 的一步通常只有几毫秒，这个占比不能忽略。

再看权重。一个路由专家的参数量约 `3 × 5120 × 1536 ≈ 23.6M`；FP4 每参数 0.5 字节，约 11.8 MB。EP64 时每张卡约 6 个本地专家，加上热点副本约 7 个，权重约 71 到 83 MB。按 3 TB/s HBM 带宽，读一遍约 24 到 28 微秒。也就是说，Wide-EP 把单卡权重流量从“几十个专家”压到“几个专家”，但 all-to-all 的传输时间可能超过权重读取时间。

这就是为什么 DeepInfer 要用 Mega MoE 把 dispatch、专家 GEMM、combine 融合，并让通信和计算重叠：如果串行执行，通信会吃掉 Wide-EP 的收益；如果重叠，权重读取和 all-to-all 可以同时进行，单步时间由较慢的一侧决定。公开资料提到，EP64 在几乎所有 batch size 下都优于 EP8，但这是在他们优化过的 DeepEP + Mega MoE + EPLB 栈上；换一套通信库，结论可能不同。

EPLB 的收益也可以用这个算例理解。384 个逻辑专家、EP64、每卡 6 个本地专家时，如果热点专家集中在少数 rank，最慢 rank 的负载可能是平均的 1.5 倍以上；冗余副本和贪心放置把 `moe_lb` 压到约 1.09（公开数字）。1.09 意味着最慢 rank 比平均多 9% 的负载，端到端 step time 至少多 9%；如果 all-to-all 的尾延迟再放大，实际差距更大。

这个算例的数字是示意，不是精确 benchmark。它的用途是让你在选 EP 规模时，同时算权重流量、all-to-all 流量和负载不均衡，而不是只看“EP 越大，单卡权重越少”。

## 7.9 Token-choice、expert-choice 与 capacity factor

前面讲的“每个 token 选 top-k 个专家”叫 **token-choice** 路由。它是最常见的实现，也是 V4.1 的方式，但并不是唯一方式。理解其他路由方式，能帮助你判断一个 MoE 引擎的负载均衡能力来自哪里。

**Expert-choice** 反过来：每个专家选择分数最高的若干个 token。它天然保证每个专家收到相同数量的 token，因此训练时不需要辅助负载均衡损失。代价是“哪个 token 被哪个专家选中”不再由 token 自己决定，自回归生成时会出现因果性问题：一个 token 是否被某个专家选中，可能依赖于它后面的 token。推理阶段很少直接用 expert-choice，但它的思想会影响路由器的设计。

**Token-choice + capacity factor** 是训练和推理都常见的折中。每个专家有一个容量上限：

```text
capacity = ceil(tokens_per_batch × top_k / num_experts × capacity_factor)
```

`capacity_factor` 大于 1 时，专家可以多接一些 token，减少丢弃；小于 1 时，强制均衡但会丢 token。训练时常用 1.0 到 1.25，并把溢出的 token 丢掉或走残差；推理时通常要求 **dropless**，因为丢 token 会直接改变输出语义。DeepInfer 的生产推理没有“丢 token”的选项，负载均衡靠 EPLB 和冗余副本，而不是容量截断。

**Expert-split** 是另一种思路：不把整个专家放在一张卡上，而是把专家的 FFN 再切分到多张卡。它适合“专家很大、单卡放不下”的场景，但会增加层内通信。它与 EP 不互斥：可以先按专家分到 rank，再在 rank 组内切分单个专家。

capacity factor 的数学含义可以用一个例子说明。假设一个 batch 有 8192 个 token、top-6、384 个专家，平均每专家收到 `8192×6/384=128` 个 token；容量因子 1.25 时容量是 160。如果某个热点专家收到 300 个 token，多出的 140 个会被丢弃或走残差；这会让这些 token 的输出和稠密模型不同。推理服务如果要保证质量，不能靠“丢 token”做负载均衡，必须靠放置和副本。

## 7.10 通信后端与拓扑

EP 的 all-to-all 不是一种实现，而是一族实现。选错后端，Wide-EP 的收益会被通信吃掉。

**NVLink / NVSwitch**：同一台机器或同一个 supernode 内的高带宽互联。DeepEP 的低延迟模式适合 decode 的小消息，高吞吐模式适合 prefill 的大消息。NVLink 域的边界决定了 EP 能开多大；跨域 all-to-all 的延迟会明显上升。

**RDMA / 以太网**：跨机通信。它的带宽和延迟取决于网卡、交换机、拥塞控制和拓扑。400 Gbps 是整机口径时，单卡只能分到 50 Gbps；把“整机带宽”当成“单卡带宽”会高估 PD 传输和 all-to-all 的能力。

**分层 all-to-all**：先在 NVLink 组内交换，再跨组交换。它把大消息拆成“组内 + 组间”两段，减少跨机流量，但增加了同步点。DeepEP 和 NCCL 都会根据拓扑选择算法；同一个 EP size 在不同拓扑上的性能可能差几倍。

**共享内存 / 单机模拟**：mini 的 `expert_parallel_moe` 用 Python 字典模拟 inbox，不涉及网络。它验证的是 dispatch/combine 的数学，不是通信性能。读 mini 时不要把 `bytes_sent` 当成真实带宽；它只是下界。

诊断通信瓶颈时，先看“消息大小”和“rank 数”。小消息受固定延迟支配，大消息受带宽支配；rank 数越多，固定延迟和拥塞越明显。`moe_lb` 只反映计算负载，不反映通信尾延迟；一个 `moe_lb=1.05` 的配置可能因为 all-to-all 抖动而比 `moe_lb=1.10` 的配置更慢。第 15.10 节会给出 alpha-beta 模型，把这两项分开算。

## 7.11 贪心放置不是最优：一个反例

EPLB 的第二阶段是“按负载降序，把物理专家逐个放到使放置后最大 rank 负载最小的位置”。这个贪心（LPT，longest processing time first 的一种变体）有不错的近似比，但不是全局最优。注意它和“放到当前最空的 rank”不完全一样：前者会看放下去之后的新峰值，后者只看放之前的负载；mini 的 `_place_greedy` 用的是前者。构造一个反例不需要很大的规模：5 个物理专家、2 个 rank、每个 rank 能放 3 个专家，负载是 `[5, 18, 19, 16, 13]`。

按负载降序处理：19 放到 rank 0（负载 19），18 放到 rank 1（负载 18），16 放到当前较空的 rank 1（负载 34），13 放到较空的 rank 0（负载 32），5 放到较空的 rank 0（负载 37）。最慢 rank 是 37。

穷举所有放置后，最优解把 `[19, 16]` 放在 rank 0（负载 35），把 `[18, 13, 5]` 放在 rank 1（负载 36），最慢是 36。贪心的 37 比最优的 36 多 1，差距不大，但它说明“每步选当前最空”会被后续的大块拖累：16 放下去时 rank 1 看起来更空，放完后却把 13 和 5 推到了 rank 0，形成一个 37 的峰。

这个反例的实际意义不是“贪心不能用”，而是三点。第一，`moe_lb` 的改善是相对的，贪心只能保证接近均衡，不能保证最优；在第 7.4 节的在线场景里，计算速度和可扩展性比 1 个单位的峰值更重要。第二，负载估计的误差可能比算法差距更大：如果 `tokens_per_expert` 的统计窗口太短，贪心会对着噪声优化，反而不如稳定的历史均值。第三，副本分配和放置是耦合的：第一阶段给热点专家复制副本，会改变第二阶段的输入；如果副本分配本身不合理，放置算法再好也救不回来。

想验证这个反例，可以写一个 5 层循环穷举 `2^5=32` 种放置，或者直接改 `mini_deepinfer/eplb.py` 的 `_place_greedy`：把“最小化放置后的峰值”换成“直接选当前负载最小的 rank”，比较两者的 `moe_lb`。这个练习比背算法更能理解“启发式”的边界。第 24.1 节会给出 mini 的两阶段实现和它的参数语义。

## 小结

MoE 把参数量和计算量解耦，代价是路由不均衡和 all-to-all 通信。EP 把专家分散到多卡，Wide-EP 降低 decode 的每步权重流量，EPLB 用冗余副本和贪心放置把热点摊平。DeepInfer 在 prefill 和 decode 使用不同的 EP/EPLB 参数，是因为两个阶段的 batch、带宽和延迟特征不同。mini-deepinfer 的 EPLB 实验说明，即使只是一个贪心算法，也能把 1.87 的不均衡压到 1.01 左右。

## 练习

1. 一个 MoE 层有 64 个路由专家、top-4、hidden 4096、FP8 权重。估算每个 token 的专家权重读取字节数，以及 EP=8 时每卡的平均读取量。
2. 解释为什么 EPLB 的第一阶段要按“每副本负载”而不是“总负载”选择复制对象。
3. 如果 `moe_lb` 从 1.05 降到 1.02，但 all-to-all 延迟增加了 10%，端到端可能变快还是变慢？给出判断条件。
4. 用 `t_step` 的两项模型解释：为什么 decode 的 EP 不是越大越好？
