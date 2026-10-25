# 第 13 章 调度器

本章导读：调度器决定每个 step 做什么、让谁做、做多少。本章拆开 DeepInfer C++ 调度器的 prefill/decode 分支、DP 组投票、chunked prefill、LCFS、抢占和前缀缓存，并用 mini-deepinfer 的 Python 实现做对照。读完本章，你应该能解释“为什么这一步做 prefill 而不是 decode”以及“显存不够时牺牲谁”。

## 13.1 调度的基本单位是 step

连续批处理把调度粒度从“一个 batch”改成“一个 step”。一个 step 要么是 **prompt step**（做 prefill），要么是 **decode step**（做 decode），DeepInfer 的 `SchedulerOutputs.prompt_run` 标记这一点。

为什么不在一个 step 里同时做 prefill 和 decode？因为两者的输入布局、attention kernel、并行策略不同。混在一起会让 kernel 选择、CUDA Graph、通信都变复杂。chunked prefill 是一个折中：把长 prompt 切成块，分到多个 **prompt step** 里做；每个 step 仍然是 prompt step，只是 `seqlen_q` 变小。它降低单步的激活峰值和延迟，让短请求有机会插队，但它不会把 prefill 变成 decode，也不是 vLLM 式的 mixed batch。mini 的 `Scheduler.schedule()` 在 `_schedule_prefill()` 和 `_schedule_decode()` 之间二选一，正是这个模型。

DeepInfer 的 `EngineType` 有三种：

- `NORMAL`：既做 prompt 也做 decode；
- `PROMPT`：只做 prefill，通常用于 PD 分离的 P 实例；
- `COMPLETION`：只做 decode，用于 D 实例。

`PROMPT` 引擎不会做 decode，`COMPLETION` 引擎没有 prefill 通道，所以它必须配置 `stop_preempted_request=true`，否则被抢占的请求无法重算。这个约束在 `Scheduler` 构造函数里用 `TORCH_CHECK` 强制。

引擎类型的选择决定了调度器的“可用动作集合”。NORMAL 引擎可以在 prefill 和 decode 之间切换；PROMPT 引擎只能排 prefill，做完就把序列交出去；COMPLETION 引擎只能排 decode，遇到显存不足只能停止或转发，不能重算。很多 PD 分离的 bug 来自“在 D 实例上触发了只有 P 实例才有的重算路径”。

## 13.2 prefill 调度

`_schedule_prefill` 从 `waiting` 队列头部取请求，做以下检查：

1. 请求是否已经终止（abort、VE 失败、SO 失败）？如果是，放进 `ignored_seqs`，回传 finish_reason。
2. 请求是否 `schedulable`？如果不是，停止扫描（队头不可调度，后面也不可调度）。
3. 是否超过 `prompt_limit`？超过就标记 `FINISHED_IGNORED`。
4. KV block 是否够？`_can_allocate_with_state` 同时检查主 KV 和 state cache；不够就 `LATER`，停止扫描。
5. 是否超过 `max_num_seqs`？超过就停止。
6. 是否超过 `prefill_max_sum_seqlen_k`？这是一个防止单步 prefill 的 K 总长度过大的限制。
7. 计算 `seqlen_q`：默认是 `num_prompt_tokens`，如果加上它会超过 `max_num_batched_tokens`，就截断成 chunk。
8. 如果有 `chunk_checkpoints`，在 checkpoint 处提前切分。
9. 多模态请求不能在图片中间切；`_adjust_chunk_size` 会向前或向后扩展到图片边界。
10. 如果 chunked prefill 关闭且需要截断，停止。
11. 分配 block，更新状态，加入 `scheduled`。

这段逻辑里有两个容易忽略的点。第一，`prefill_sum_seqlen_k_remain` 是按 `seq->get_len()` 扣减的，不是按 `seqlen_q`；它限制的是这一步所有序列的 K 总长度，防止 attention 的 `sum(seqlen_q × seqlen_k)` 爆炸。第二，`cp_align` 会把 `seqlen_q` 对齐到 `cp_size × 2`，因为 CP 的 mirror 布局需要成对对齐。

用一个小例子走一遍，这里按生产 block size 128 计算（mini 默认是 16，数字会不同）。假设 `max_num_batched_tokens=512`，队头是一个 800 token 的 prompt，chunked prefill 开着。第一步取 512 token，分配 `ceil(512/128)=4` 个 block；序列回到 waiting 队头。第二步取剩下的 288 token，分配 `ceil(800/128)-4=3` 个 block，prefill 完成，进入 running。如果中途有一个 300 token 的请求插队，调度器只在队头允许时才会看到它；这保证了 chunked prefill 的顺序性，但可能让短请求等长请求。第 13.6 节会讨论公平性。

## 13.3 decode 调度与 LCFS

`_schedule_decode` 先把 `prompted` 队列里的序列转入 `running`（PD 分离时，P 实例处理完的序列会进入这个队列），然后按 **LCFS**（Longest Completion First）排序：输出长度最长的序列优先调度。`prompted` 是生产调度器的结构，mini 没有这个队列；mini 在 prefill 完成的 postprocess 里直接把序列放进 `running`，读代码时不要把两者混为一谈。

LCFS 的直觉是：长输出请求已经花了很多计算，应该优先让它完成，避免它在快结束时被抢占；短输出请求可以稍等。DeepInfer 的实现是：

```text
sort running by output_len ascending
while queue not empty and scheduled < max_num_seqs:
    seq = queue[-1]              # 最长输出
    if can_allocate_one_block(seq):
        append_slot(seq); schedule(seq); pop_back
    else:
        victim = queue[0]        # 最短输出
        preempt(victim); pop_front
```

当显存不足时，抢占输出最短的序列。被抢占的序列有两种命运：

- `stop_preempted_request=true`：直接标记 `FINISHED_PREEMPTED`，释放 KV，返回给用户；
- `stop_preempted_request=false`：`reset_for_recompute()`，清空 block table 和 computed token 数，放回 `waiting` 队头，之后重新 prefill。

重算的代价可能很高，但前缀缓存可以让它变便宜：如果被抢占的序列的前缀还在缓存里，重新 prefill 可以命中大部分 block。DeepInfer 的 `reset_for_recompute` 会复位 `cache.num_tokens`、inflight 计数、MTP 状态，但保留输入 token 和输出 token；重算时把 prompt + 已生成 token 一起 prefill。

LCFS 的“最长优先”不是绝对公平。它会让长输出请求占住 running 槽位，短请求可能饿死。DeepInfer 用 `max_num_seqs`、`prefill_any_attempt_interval` 和上层的负载均衡来缓解。调度器的目标不是“每个请求都最快”，而是“在 SLO 下最大化 goodput”。

## 13.4 NORMAL 引擎的 DP 投票

在 DP 部署里，每个 DP rank 有自己的 scheduler。一个 step 是 prompt 还是 decode，必须所有 rank 一致，否则集合通信会对不上。DeepInfer 用两次 all-reduce 投票：

- **ALL 点**：每 `prefill_attempt_interval` 个 decode step，所有 rank 都有 prefill 工作才做 prompt step。这防止一个 rank 干活、其他 rank 陪跑空 prompt step。
- **ANY 点**：每 `prefill_any_attempt_interval` 个 decode step，只要一个 rank 有 prefill 工作，全组就做 prompt step。这是给孤立 rank 的保底，防止它的 prefill 被 ALL 投票无限饿死。
- 两类点重合时按 ANY 算。

第一轮投票决定“想不想做”，第二轮投票确认“有没有真的排出 prefill”。如果所有 rank 都空手（slot 满、KV 不够），就转 decode；否则本 rank 即使没排到，也要陪跑一个空 prompt step，保持步型一致。

这个设计很细，但它是 DP 正确性的基础。没有投票，不同 rank 可能一个在做 prefill、一个在做 decode，all-reduce 的 shape 和语义都会错。

可以把投票理解成一个分布式屏障：ALL 点是“大家都准备好才走”，ANY 点是“至少有一个人准备好就走”。ALL 点保证效率，ANY 点保证公平。两者的间隔参数决定了“效率”和“延迟”的权衡：ALL 点太频繁，空 prompt step 多；ANY 点太频繁，decode 被打断多。

## 13.5 前缀缓存与调度

调度器在 `add_sequence` 时就会做前缀匹配：用 token 序列的完整 block 计算哈希链，查 block manager 的缓存表，命中的 block 直接挂到序列的 block table 上，`num_computed_tokens` 设为命中长度。

命中长度有一个上限：至少留一个 token 不命中，否则序列没有 logits 可采样。DeepInfer 的 `_schedule_prefill` 里，如果 `block_table` 非空，不能直接断定是“前缀命中或 fork”：它也可能是上一轮 chunked prefill 还没算完、块表被保留下来的序列。调度器要同时看 `prefix_cache_hit`、`num_computed_tokens` 和最后一个 block 是否完整；真正的命中/fork 分支才需要分配剩余 block、克隆 state、复制最后一个可能写了一半的 block。

前缀缓存与抢占的交互很微妙。抢占时如果把序列的前缀重新 pin 住，就等于没有释放显存；所以重算路径应该先释放，再在下次 prefill 时重新匹配。mini-deepinfer 的第一版就踩了这个坑：`_preempt` 里调用 `prepare_sequence` 重新命中前缀，结果显存一直不释放，调度器空转。第 22 章会详细讲这个 bug。

前缀缓存还会影响 chunk 边界。前缀命中的长度一定是 block size 的整数倍，所以命中部分不会留下半满的共享块；但未命中的后缀仍然要从命中边界之后开始切 chunk。如果命中 512 token、chunk budget 是 256，第一步只算后缀的前 256 个 token。chunk 的起点由 `num_computed_tokens` 决定，只需要是一个合法的 token 位置，不要求对齐到 block 边界：分页 KV 的写入本来就是按 `block_table[pos // block_size]` 和 `pos % block_size` 跨块进行的，一个 512 token 的 chunk 跨 4 个 block 是正常路径。真正需要对齐的是前缀命中边界（block 对齐）和 CP 的 `cp_size × 2` 边界，不要把这两条约束套到每个 chunk 上。这些细节决定了“命中”能不能真正变成“少算”。

## 13.6 公平性与饥饿

LCFS 偏向长输出，短请求可能饿死。DeepInfer 用几个机制缓解：

- `prefill_any_attempt_interval` 给等待 prefill 的 rank 保底；
- `max_num_seqs` 限制单步并发，避免一个请求占满所有 slot；
- 请求到达时间 `arrival_time` 和 first token latency 被记录，用于监控；
- 生产层的 Prefill Load Balancer 按上下文长度分桶，动态分配引擎配额。

调度器本身不保证公平，公平性是调度器 + 负载均衡器 + 准入控制共同作用的结果。线上如果看到 P99 TTFT 飙升，先看 waiting 队列的长度和队头请求的 prompt 长度，再看 prefill 引擎的配额是否被长请求占满。

一个实用的公平性指标是“等待时间的基尼系数”或“P99 等待 / P50 等待”。如果 P99 等待远高于 P50，说明队列里有长请求在垄断资源。另一个指标是“被抢占请求的重算次数”；如果同一个请求被反复抢占，说明容量不足或 LCFS 过于激进。

## 13.7 PD 分离下的调度

PD 分离把 prefill 和 decode 拆到不同实例。P 实例的调度器只做 prefill，D 实例只做 decode。两者之间需要：

- P 实例把 KV cache 和最后一个 hidden state 传给 D 实例；
- D 实例把序列插入自己的 `prompted` 队列；
- 全局负载均衡器决定请求去哪个 P、哪个 D；
- D 实例的 grace period 和请求转发处理缩容。

DeepInfer 开源了 `PROMPT`/`COMPLETION` 引擎类型和大部分调度逻辑，但没有开源跨实例的 KV 传输和全局路由。第 15 章会讲这些组件的设计。

PD 分离后，调度器多了一个“交接状态”：P 实例算完 prefill 后，序列不是进入 RUNNING，而是进入“等待 KV 传输”的状态；D 实例收到 KV 后，才把它放进 `prompted`。如果传输失败，序列要能回退到 P 或重试。这个状态机比单实例调度复杂得多，也是 PD 分离最容易出故障的地方。

## 13.8 mini-deepinfer 的调度器

mini 的 `Scheduler` 保留了核心策略：

- `_needs_prefill`：没有 running、或队头是未完成的 chunk、或到达 prefill 间隔、或等待过久，就做 prefill；
- `_schedule_prefill`：按 token budget 切 chunk，检查 block capacity，必要时释放 waiting 中其他 chunk 的 block；
- `_schedule_decode`：按输出长度排序，最长优先，显存不足时抢占最短；
- `_preempt`：释放 block、重置序列、放回 waiting，不重新 pin 前缀；
- `postprocess`：更新 computed token、移动状态、注册前缀 block。

它没有 DP 投票、没有 C++ 对象池、没有 MTP varlen，但调度策略的骨架和 DeepInfer 一致。运行 `python examples/chunked_prefill.py` 和测试 `test_preemption_still_finishes_requests` 可以看到分块、前缀命中和抢占的实际行为。

mini 的 `_release_waiting_blocks` 是一个值得注意的取舍：当队头请求分配不到 block 时，它会释放其他 waiting 序列已经占用的 block，让它们从头开始。这牺牲了被释放序列的进度，但保证系统能前进。生产引擎会更精细地选择牺牲谁，例如优先释放 chunk 进度最少的序列。

## 13.9 调度器的调参

调度器有几个关键参数，调参时要一起看：

- `max_num_batched_tokens`：单步 token 预算。太小，prefill 碎片化；太大，decode 被拖慢。
- `max_num_seqs`：单步并发上限。太小，吞吐低；太大，KV 压力大、尾延迟差。
- `prefill_attempt_interval`：多久尝试一次 prefill。太小，decode 被打断；太大，TTFT 变差。
- `prefill_any_attempt_interval`：饥饿保护。太小，空 prompt step 多；太大，短请求饿死。
- `block_size`：页大小。太小，页表长、kernel 间接寻址多；太大，内部碎片多。注意：DeepInfer 生产版的 `resolved_tokens_per_block` 固定为 128，不可调；这一条只适用于自研引擎或 mini 实验（mini 默认 16）。
- `stop_preempted_request`：抢占时是否直接停止。停止保护尾延迟，但会丢请求；重算保请求，但可能雪崩。

调参的顺序应该是：先定 SLO，再定 batch 和并发，再定 prefill 间隔，最后调 block size。DeepInfer 部署时 block size 不是可调项，调参应集中在 batch、并发和 prefill 间隔上。不要反过来“先调一个参数看吞吐”，因为参数之间会互相影响。

## 13.10 调度器的可观测指标

调度器是个黑盒，除非你把它的决策变成指标。下面这些指标能回答“调度器是不是瓶颈”。

**队列指标**：waiting 长度、running 长度、队头等待时间、等待时间的 P50/P95/P99。waiting 持续增长说明 prefill 容量不足或调度被长请求占满；running 很高但吞吐不涨，说明 decode 被 KV 或带宽卡住。

**步型指标**：prompt step 与 decode step 的比例、空 prompt step 的比例、chunked prefill 的平均 chunk 数。空 prompt step 太多，说明 DP 投票或容量 fallback 在空转；chunk 太小，说明 token 预算被切碎，prefill 效率低。

**抢占指标**：抢占次数、被抢占请求的重算次数、重算命中的前缀长度。抢占后重算次数很高，说明 KV 容量不足或 LCFS 过于激进；重算命中前缀很短，说明前缀缓存没有帮上忙。

**公平性指标**：P99 等待 / P50 等待、被饿死请求数、每租户的等待时间。P99/P50 很高，说明长请求在垄断资源；需要调整 `prefill_any_attempt_interval`、`max_num_seqs` 或上层配额。

**SLO 指标**：goodput、TTFT 达标率、TPOT 达标率。吞吐高但 goodput 低，说明系统在“忙碌但无用”的状态；这时应该减少 batch、调整抢占或扩容。

这些指标要和调度参数一起看。单独看 `preemptions` 没有意义：高抢占可能是容量不足，也可能是 LCFS 在保护长请求；要结合 waiting 长度、KV 使用率和 goodput 判断。一个实用的做法是把调度参数、指标和变更记录放在同一张时间线上，出现回归时先看最近改了什么。

## 13.11 两个调度算例

先看 DP 投票的时序。假设 4 个 rank，`prefill_attempt_interval=4`，`prefill_any_attempt_interval=16`。第 4 步，rank 0 有 prefill 工作，rank 1 到 3 没有。ALL 投票问“是不是所有 rank 都想 prefill”，答案是否；ANY 点还没到，所以全组继续 decode。rank 0 的 prefill 请求继续排队。第 8 步，rank 0 和 rank 1 有工作，ALL 仍然否；第 12 步，rank 2 也有工作，但 rank 3 没有，ALL 仍是 0；第 16 步是 ANY 点，只要有一个 rank 有工作就做 prompt step，于是全组切到 prefill，rank 1 到 3 即使没有排到请求，也要陪跑一个空的 prompt step，保持集合通信的形状一致。

这个例子里，rank 0 的请求最多等了 12 个 decode step。如果 decode 一步 10 毫秒，TTFT 多 120 毫秒；如果 `prefill_any_attempt_interval` 设成 64，等待会变成 600 毫秒。ANY 点太频繁，decode 会被空 prompt step 打断；太稀疏，孤立 rank 的请求会被饿死。ALL 点则决定“大家都有活时多久合批一次”，太频繁会让 prefill 碎片化，太稀疏会让 prefill 排队。两个参数要一起调，不能只看一个。

投票的 all-reduce payload 不需要很复杂：每个 rank 发一个“想不想 prefill”的标志，再发一个“实际排出了多少 token”的计数；第一轮归约得到“是否所有/任意 rank 想”，第二轮确认“是否真的排出了工作”。如果所有 rank 都空手（slot 满、KV 不够），就转 decode，避免空转。这个两轮设计比单轮更稳，因为“想”不等于“能”。

再看抢占/重算的成本。序列长度 8000，已经生成 2000，前缀缓存命中 6400 个 token。被抢占后重算，需要重新 prefill 的 token 是 `8000 + 2000 - 6400 = 3600`。按每卡 6 万非缓存 token/s 算，重算约 60 毫秒。如果这个请求还剩 100 个输出 token、每 token 20 毫秒，剩余 decode 是 2 秒，重算只占 3%，应该选择重算，保住请求。如果只剩 5 个 token、每 token 20 毫秒，剩余 decode 只有 100 毫秒，重算 60 毫秒已经超过一半，而且重算会挤占其他请求的 prefill 容量，这时 `stop_preempted_request=true` 可能更合理。判断条件可以写成：

```text
重算划算 ⇔ (1 - prefix_hit) × total_tokens / prefill_rate
          < remaining_output_tokens × tpot + 请求失败的业务代价
```

这个不等式右边的“业务代价”很难量化：一个被中止的请求可能触发用户重试，重试又带来新的排队。生产系统通常把“停止”限制在明确的降级场景，而不是默认策略。第 19 章的 SLO 和第 46 章的准入控制会把这个问题放到更大的队列模型里。

## 小结

调度器的核心是“在正确的 step 做正确的事”：prefill 受 token budget、block capacity、chunk 对齐和多模态边界约束；decode 用 LCFS 和抢占处理显存压力；DP 组用 ALL/ANY 投票保持步型一致；前缀缓存让重算变便宜，但抢占时必须先释放。公平性不是调度器单独能保证的，需要负载均衡和准入控制配合。

## 练习

1. 解释 ALL 点和 ANY 点分别解决什么问题。如果没有 ANY 点，孤立 rank 会发生什么？
2. 一个序列输出长度 100，另一个 10，显存只够一个。LCFS 会抢占谁？如果被抢占的序列前缀缓存命中 80%，重算成本大概是多少？
3. 为什么 chunked prefill 的 chunk 要对齐到 CP 的 `cp_size × 2`？不对齐会破坏什么？
4. 用 13.8 节的状态转移，画出一次“prefill -> decode -> 抢占 -> 重算 -> 完成”的完整路径，标出每一步的 block 变化。
