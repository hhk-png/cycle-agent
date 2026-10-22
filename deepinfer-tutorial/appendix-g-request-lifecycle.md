# 附录 G 一个请求的完整生命周期

本附录把前面所有机制串成一次可复现的追踪。我们不讨论新的设计，只让一个请求从字符串开始，经过 tokenizer、调度器、分页 KV、模型前向、采样和状态回收，最后变成一个 `RequestOutput`；然后再发一个完全相同的请求，观察前缀缓存如何改变工作量和 block 分配。配套脚本是 `examples/lifecycle.py`，输出只包含确定性字段，因此可以逐行对照。

## G.1 为什么需要一次端到端追踪

按章节读这本书，很容易记住一堆彼此独立的机制：分页、前缀哈希、chunked prefill、采样、状态机。但线上问题几乎从不只属于一个机制。一个“首 token 变慢”的请求，可能同时涉及 tokenizer 的字节长度、调度器的 prefill 预算、前缀命中的 block 对齐、KV 写入位置和采样后的停止判断。只读单章，很难建立这些量之间的因果链。

端到端追踪的价值是给你一条“基准线”：在一个极小、完全确定的输入上，记录每一步的状态变化和数字。之后读任何一章，你都可以问“这个机制会改变基准线里的哪个字段”。例如，分页 KV 改变的是 `blocks` 和 `cached_blocks`，前缀缓存改变的是 `prefix_hit` 和 `q_len`，推测解码改变的是每步推进的 token 数，结构化输出改变的是采样时的 mask 和可能的失败状态。

这条基准线还有一个作用：它是验证的锚点。改动调度器或 block manager 后，先跑这个脚本，确认 token 序列、block 数、命中数没有意外变化，再跑完整测试。它比“看输出像不像”严格，又比读 trace 便宜。

## G.2 实验设置

脚本使用 `EngineConfig(num_blocks=32, block_size=16, max_num_seqs=4, max_num_batched_tokens=128, max_model_len=128)`，加载 `assets/tiny_lm.npz` 的 2 层玩具模型，采样参数为 `temperature=0`、`max_tokens=4`、`seed=1`。prompt 是：

```text
DeepInfer is an inference engine for large language models.
```

这个 prompt 的 UTF-8 编码是 59 字节，字节级 tokenizer 因此产生 59 个 token。选它有两个原因：长度超过 3 个 block（48 token），能演示“整块命中、尾块重算”；又短于 `max_num_batched_tokens=128`，第一次 prefill 可以在一个 step 内完成，不必引入 chunk 的复杂度。

脚本不打印 TTFT、TPOT 等墙钟指标，因为这些数字随机器变化；它打印 token 数、block id、命中数、finish reason 和 block manager 计数，这些在相同代码和相同权重下是确定的。墙钟实验由 `examples/benchmark.py` 和 `examples/offline_inference.py` 负责。

## G.3 第一步：字符串变成 token

脚本先打印字节数和 token 数，两者都是 59。这个相等不是巧合，而是字节级 tokenizer 的定义：每个字节就是一个 token，没有 BPE 合并，也没有 `<unk>`。如果换成生产 tokenizer，同一个 prompt 可能只有十几个 token，`prompt_tokens`、KV 字节、prefill FLOPs 都会随之改变。这也是为什么比较不同模型的“每 token 成本”时，必须先确认 tokenizer，而不能只看字符数。

tokenizer 的输出是 `prompt_token_ids`。在 `add_request` 里，它和 `SamplingParams` 一起被封装成 `Sequence`。此时序列还没有分配任何 KV block，状态是 `WAITING`，`block_table` 为空，`prefix_cache_hit` 为 0。这是分页 KV 的第一个设计点：分配是惰性的，请求进入系统不等于立刻占用显存。第 22 章的实现会在调度时才调用 `match_prefix` 和 `allocate`，这样等待队列可以很长，而显存只被真正要执行的请求占用。

`Sequence` 同时保存 prompt token、输出 token、采样参数、随机数生成器、停止条件和可选的文法 matcher。它是跨 step 的长期状态；调度器每一步只为它生成一个轻量的调度项。第 12 章强调的“长期状态与 step 快照分离”，在这个小实现里表现为 `Sequence` 和 `ScheduledSeq` 的分工。

## G.4 第二步：第一次 step 做完了 prefill

第一次调用 `engine.step()`，调度器发现等待队列里有一个未完成的 prompt，于是安排一次 prefill。脚本打印的行是：

```text
   1 prefill     59   60 [0, 1, 2, 3]   0 34               -
```

逐列解释。`q_len=59` 表示这一步为这个序列计算了 59 个 query token，也就是整个 prompt；`len=60` 是 prefill 之后序列的总长度，因为采样出了一个输出 token；`blocks=[0,1,2,3]` 是分配的四块物理页，`ceil(59/16)=4`；`hit=0` 表示没有前缀可复用；`output=34` 是第一个输出 token 的 id，对应字节 `"`。

为什么 59 个 token 需要 4 个 block，而不是 3.6875 个？因为 block 是最小分配单位，最后一个 block 只用了 11 个槽位，剩余 5 个槽位留给后续 decode。这个“内部碎片”是分页的代价，也是为什么 block size 是一个需要权衡的参数：block 越小，尾部浪费越少，但页表更长、元数据更多、kernel 的间接寻址开销更大。生产 DeepInfer 用 128 token 的 block，mini 用 16，原因就在这里。

prefill 的 forward 会把 59 个 token 的 K/V 写入这四块页，并在最后一个位置得到 logits。采样器按 `temperature=0` 做贪心选择，得到 token 34。此时序列从 `WAITING` 变成 `RUNNING`，`num_output_tokens=1`，首 token 的时间被记录到 `SequenceMetrics`。TTFT 的“第一 token”不是 prefill 完成的时刻，而是采样出 token 并写入输出的时刻；如果采样或结构化 mask 很慢，它会直接进入 TTFT。

一个容易忽略的细节是：prefill 步的 `q_len` 是 59，但这一步的“输出 token”只有 1 个。指标里 `prefill_tokens` 记录 59，`decode_tokens` 不记录这次采样。因此后面看到 `decode_tokens=3` 时，不要以为总输出是 3；总输出是 4，首 token 是在 prefill 步产生的。这个记账口径在比较不同引擎的吞吐时必须对齐，否则会把“prefill 产出的首 token”漏掉或重复计算。

## G.5 第三到第五步：decode 只推进一个 token

接下来的三次 `step()` 都是 decode：

```text
   2 decode       1   61 [0, 1, 2, 3]   0 34,32            -
   3 decode       1   62 [0, 1, 2, 3]   0 34,32,115        -
   4 decode       1   63 []             0 34,32,115,97     length
```

每次 `q_len=1`，序列长度加一，block table 不变，因为第四块还有空位。到第 4 步，输出 token 数达到 `max_tokens=4`，序列被标记为 `FINISHED_LENGTH_CAPPED`，`finish_reason` 是 `length`。状态一旦变成 FINISHED，`postprocess` 会释放这个序列的全部 block，所以这一行打印出的 `blocks` 是空列表。这不是“块消失了”，而是“块已经归还给池子”；`free_blocks` 仍然是 32，`cached_blocks` 变成 3，因为前三个满块被前缀缓存保留了下来。

这里的 `finish_reason` 值得单独说明。`length` 表示达到 `max_tokens` 或 `max_model_len`；`stop` 表示 EOS 或 stop 字符串；`abort` 表示用户取消；`structured_output` 表示文法无法继续。生产引擎还有 `preempted`、`ignored`、`ve_failed` 等状态。每一种状态对应不同的资源动作和客户端语义：`length` 通常是正常完成，`abort` 不该计费，`structured_output` 要让调用方决定重试还是降级。第 12 章的状态机和第 25 章的 HTTP 错误映射都建立在这个字段上。

decode 的每一步只计算一个新 token 的 Q，但 attention 要读取从位置 0 到当前位置的所有 K/V。这就是第 1 章说的“decode 把重算变成读取”：计算量随上下文线性增长，但不需要重算历史 token 的 K/V。block table 让 attention 可以通过页表间接寻址，而不要求 KV 在物理上连续。第 18 章会解释，真实 kernel 的瓶颈往往不是这些乘加，而是把分散的页读进 SRAM 的带宽。

## G.6 第六步：第二个相同请求命中前缀

第二个请求的 prompt 完全相同。`add_request` 返回时，状态是 `WAITING`，但 `block_table` 已经是 `[0, 1, 2]`，`prefix_cache_hit=48`。这说明前缀匹配发生在准入阶段，而不是第一次 forward 之后。48 等于 3 个满块乘以 16；prompt 有 59 个 token，最后 11 个 token 不足一个块，没有被缓存，因此第二次仍然要重算这 11 个 token。

第二次的第一次 step 因此变成：

```text
   1 prefill     11   60 [0, 1, 2, 4]  48 34               -
```

`q_len` 从 59 降到 11，`hit` 从 0 变成 48，block table 里前三个块直接挂上，第四个块是新分配的 id 4。第一个请求用过的块 3 没有被复用，因为它只被部分填充，且已经随序列释放；池子把它作为普通空闲块处理，而前缀缓存只保留“满块”的内容哈希。这个细节解释了为什么“命中长度总是 block_size 的整数倍”，也解释了为什么 block size 会直接影响缓存命中率：block 越大，尾部的不可命中部分越多。

第二个请求的四步输出与第一个完全相同，脚本最后打印 `identical greedy output: True`。这不是因为缓存“记住了答案”，而是因为贪心采样在相同 logits 上必然选同一个 token，而前缀缓存只复用 K/V、不改变数值。如果缓存实现有 bug，例如哈希碰撞或 COW 复制错误，输出就会分叉；这正是 `test_prefix_cache_hit_and_same_output` 要抓的问题。

累计的 block manager 统计是：

```text
allocations=5 frees=8 cache_hits=3 cache_misses=1 cached_blocks=3 free_blocks=32
```

第一个请求分配 4 块、释放 4 块；第二个请求分配 1 块、释放 4 块（3 个共享块加 1 个新块），所以总分配 5、总释放 8。`cache_hits=3` 是块级命中次数，不是请求级命中次数；`cache_misses=1` 是第一次请求的首次匹配。`cached_blocks=3` 是当前仍被前缀缓存持有的满块数。`free_blocks=32` 说明所有块都可分配，没有序列再引用它们；但“可分配”不等于“空”：这 3 个缓存块仍然驻留在 HBM 里，`resident_blocks` 会是 3。旧版实现把缓存块算成 free，会让占用看起来是 0；现在 `free_blocks` 和 `resident_blocks` 分开，既能看到可分配容量，也能看到真实占用。没有泄漏的判断要看“释放后引用计数为 0、没有序列的页表还指向它”，而不是只看 `free_blocks`。

## G.7 把追踪映射回全书

同一条 trace 可以按字段拆回各章。

`utf8 bytes` 和 `tokens` 属于 tokenizer 与序列长度，见第 21 章和附录 F。`status=WAITING`、`RUNNING`、`FINISHED_LENGTH_CAPPED` 属于序列状态机，见第 12 章。`q_len=59` 与 `q_len=11` 的差异属于调度和前缀匹配，见第 13、14、22 章。`blocks=[0,1,2,3]` 和 `[0,1,2,4]` 属于分页、引用计数和 COW，见第 14、22 章。`output=34` 属于采样，见第 11 章。`finish_reason=length` 属于停止条件与输出契约，见第 11、25 章。`cache_hits`、`free_blocks` 属于指标与运维，见第 19、25 章。`identical greedy output` 属于正确性与批不变性，见第 10、26 章。

如果你只记一件事，记这个因果链：prompt 长度决定块数，块对齐决定可命中长度，可命中长度决定第二次的 `q_len`，`q_len` 决定 prefill 时间和 TTFT；而 block 的释放与缓存保留决定显存能否被后续请求复用。这条链上的任何一环出错，都会表现为“慢”或“结果不对”，但根因可能在完全不同的模块。

## G.8 可以继续做的实验

把 `block_size` 从 16 改成 8，再跑一次。你会看到第一个请求需要 8 块，第二个请求命中 56 个 token（7 个满块），只剩 3 个 token 重算；`cached_blocks` 和 `allocations` 也会变化。这个实验直接展示了 block size 对命中率和元数据量的双向影响。

把 `enable_prefix_cache=False`，第二个请求的 `prefix_hit` 会变成 0，`q_len` 回到 59，但输出仍然相同。这说明缓存是性能优化，不是正确性条件；关掉它只应改变速度，不应改变 token。

在第一个请求还没结束时加入第二个请求，观察 `max_num_seqs=4` 下的连续批处理：两个序列会在同一个 decode step 里各推进一个 token，`q_len` 变成 2，block table 分别增长。这个实验对应第 13 章的 running 队列和第 22 章的批处理循环。

把 `temperature` 改成 0.8 并固定 `seed`，重复两次，输出仍然一致；换一个 seed，输出改变。这个实验把“确定性”拆成两件事：给定 seed 的可复现性，以及 batch 组成无关性。第 10 章会解释为什么后者需要 batch-invariant kernel，而不是只固定 seed。

## 小结

一个请求的生命周期可以压缩成六个动作：编码、入队、prefill、逐 token decode、结束、回收。分页 KV 让显存按需分配，前缀缓存让重复前缀只算一次，状态机让每种结束原因都有明确的资源动作，指标把这条链路变成可观察的数字。附录 G 的 trace 是全书最小的端到端基准线；读任何机制时，回到这条 trace，问它会改变哪个字段，比记住术语更有用。

## 练习

1. 用 `examples/lifecycle.py` 跑一遍，记录 `allocations`、`frees`、`cache_hits`、`cached_blocks`，并解释为什么 `frees` 大于 `allocations`。
2. 把 `block_size` 改成 8，预测第二个请求的 `prefix_hit` 和 `q_len`，再运行验证。预测错了，说明你对“整块命中”的理解还有缺口。
3. 关闭前缀缓存，确认输出不变、`q_len` 变化。为什么“输出不变”是缓存正确性的必要条件，而不是充分条件？
4. 在 trace 里找到 TTFT 对应的时刻。它是 prefill 开始、prefill 结束，还是首 token 写入输出的时刻？如果采样器很慢，TTFT 会受什么影响？
5. 设计一个实验，让两个请求在同一个 decode step 里被调度，并打印这一步的 `q_len` 和两个序列的 block table。
