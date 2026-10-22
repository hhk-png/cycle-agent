# 第 22 章 分页 KV 与调度器实现

本章导读：本章逐层拆开 mini-deepinfer 的 `BlockManager`、`PagedKVCache` 和 `Scheduler`，解释分页、前缀哈希、COW、chunked prefill、LCFS、抢占和状态回收。重点讲三个实现陷阱：共享块被误判为不可用、抢占时重新 pin 前缀导致显存不释放、chunked prefill 占着块导致队头饿死。

## 22.1 BlockManager 的数据结构

`BlockManager` 持有一个固定长度的 `Block` 列表：

```python
@dataclass
class Block:
    block_id: int
    ref_count: int = 0
    block_hash: int | None = None
    token_ids: tuple[int, ...] = ()
    last_used: float = 0.0
```

`ref_count` 是引用计数；`block_hash` 是链式前缀哈希；`token_ids` 用于校验哈希没有碰撞；`last_used` 用于 LRU 淘汰。

分配时，`_take_free_block` 从 `ref_count == 0` 的块里选 `last_used` 最小的。注意：`ref_count == 0` 不等于“没有内容”。它可能是前缀缓存里的块，仍然带着 hash 和 token ids，可以被复用；如果被重新分配，就要从哈希表里删掉，`evictions` 加一。这个语义是前缀缓存和内存分配共享同一批块的关键。

释放时，`free` 把 `ref_count` 减一；减到 0 时块不立即清空，而是留在缓存里等待复用。只有被重新分配时才会丢失内容。

用数字走一遍。`num_blocks=16`、`block_size=4`，初始所有块 `ref_count=0`。序列 A 写入 10 个 token，需要 3 个块，分配块 0、1、2，`ref_count=1`。序列 B 的前 8 个 token 与 A 相同，前缀匹配只共享**完整块** 0、1，`ref_count` 变成 2；B 的页表是 `[块0, 块1, 块3]`，其中块 3 是新分配的私有块，对应 token 8 到 11。注意“块 3”是物理块 id，“页表第 2 项”是逻辑位置；B 的下一次写入从 token 8 开始，落在页表第 2 项（物理块 3），不会写共享的块 1。正常前缀缓存路径下不会触发 COW，因为共享的两个块都是满块，追加只发生在私有尾块上。

这里要分清三个名词：**逻辑页号**是 token 位置除以 block size 的商（token 8 是第 2 页），**页表项**是 `seq.block_table[2]`，**物理块 id** 是页表项里存的值（这里是 3）。把“第 2 页”说成“块 2”会让人误以为写的是物理块 2，从而看不懂 COW 为什么没有触发。

COW 是防御性路径：如果未来支持共享不完整尾块，或者测试/ fork 场景手工把一个正在追加的块 `share` 出去，写入时才会发现 `ref_count=2`，需要分配新块、复制整块、替换页表。把 COW 想成“共享块被追加时”的保护是对的，但不要把正常前缀匹配的例子说成一定会触发它；这也是 `register_prefix_blocks` 只注册完整块的原因。

## 22.2 前缀哈希与匹配

`compute_block_hash(prev_hash, token_ids)` 用 BLAKE2b 把前一个块的哈希和当前块的 token 打包成一个 64 位整数。链式哈希保证只有完整前缀相同才会命中。

`match_prefix(token_ids)` 从头开始，每 `block_size` 个 token 算一个哈希，先查 `_hash_to_block` 得到候选块，再校验 `block.token_ids == chunk` 防止哈希碰撞；只有校验通过才 `share`，校验失败就删除哈希表项并停止，不会把错误的块挂到页表上。遇到真正的 miss 也停。返回 `(命中 token 数, block id 列表, 最后一个哈希)`。

这里有一个容易写错的点：`lookup` 不能因为 `ref_count == 0` 就认为块无效。前缀缓存的块在没人引用时就是 0，但它仍然有效。第一版实现里写了 `if block.ref_count <= 0 or block.block_hash != hash: miss`，导致前缀缓存永远不命中；测试 `test_prefix_cache_hit_and_same_output` 抓到了这个问题。修复后，`lookup` 只检查哈希是否一致，`share` 允许把 0 引用计数提升到 1。

哈希碰撞的防御是“哈希 + token 校验”。64 位哈希的碰撞概率很低，但不是零；如果两个不同的 token 块哈希到同一个值，只查哈希会读到错误的 KV。`match_prefix` 在命中后比较 `block.token_ids`，不匹配就删除哈希表项并停止。生产系统还会用更长的哈希或内容校验。

## 22.3 PagedKVCache 的读写

`PagedKVCache` 为每层分配 `[num_blocks, block_size, n_kv_heads, head_dim]` 的 K/V 数组。`write_layer(layer, seq, start, k, v)` 把 `[n, Hkv, D]` 写进页表：

```python
pos = start + offset
block_index = pos // block_size
block_offset = pos % block_size
block_id = seq.block_table[block_index]
```

如果目标块的 `ref_count > 1`，说明它被其他序列共享，必须先 COW：分配新块、复制整块 K/V、替换页表、释放旧引用。`_cow_if_shared` 就是这段逻辑。

`gather_layer(layer, seq, end)` 按页表把 `[0, end)` 的 K/V 拼成连续数组。它假设调用方已经通过调度器分配了足够的页；如果页表长度不足，会抛 `OutOfBlocksError`，而不是静默返回较短的 K/V。真实 kernel 不会拼接，而是把页表传给 attention，在 kernel 内间接寻址；mini 的拼接是为了可读性。

`ensure_capacity(seq, target_tokens)` 计算需要多少页，缺多少补多少；如果目标长度不是 block size 的整数倍，且最后一页被共享，就先 COW。这个函数在调度器和写入路径都会调用，保证页表够用。

一个容易忽略的边界是 `target_tokens=0`：`ensure_capacity` 直接返回，不分配任何页。如果序列随后要写第一个 token，`write_layer` 会再次调用 `ensure_capacity`，所以不会越界。但如果调度器在 `target_tokens=0` 时就认为“分配完成”，可能会漏掉页表扩展。mini 在 prefill 调度里保证 `chunk >= 1`，避免这个边界。

## 22.4 Scheduler 的状态

`Scheduler` 维护三个集合：

- `waiting`：还没开始或没完成 prefill 的序列；
- `running`：prompt 已经算完、正在 decode 的序列；
- `_waiting_since`：等待开始的 step 编号，用于饥饿保护。

`add_sequence` 先检查长度限制，然后 `prepare_sequence` 做前缀匹配，把命中的块挂到页表，`num_computed_tokens` 设为命中长度。注意这里最多命中 `length - 1`，保证至少有一个 token 要算，否则没有 logits。

`schedule()` 决定这一步做 prefill 还是 decode：

- 没有 running，做 prefill；
- 队头是未完成的 chunked prefill，继续 prefill；
- 到达 `prefill_attempt_interval`，做 prefill；
- 队头等待超过 `prefill_any_attempt_interval`，做 prefill；
- 否则做 decode。

如果 prefill 因为容量不足排不出任何序列，而 running 非空，就退化成 decode，避免空转。这个 fallback 是第二版加的；第一版在显存紧张时会一直返回空 prompt step。

这个“步型选择”是调度器的核心。它没有全局最优解，只有一组启发式：优先续跑 chunk、定期尝试 prefill、饥饿保护、容量 fallback。每个启发式都对应一个失败模式，去掉任何一个都会在某种负载下出问题。

## 22.5 Chunked prefill

`_schedule_prefill` 按 token budget 切 chunk：

```python
remaining = seq.length - seq.num_computed_tokens
chunk = min(remaining, budget)
target = seq.num_computed_tokens + chunk
self.kv_cache.ensure_capacity(seq, target)
```

如果 `target == seq.length`，chunk 完成，序列在 postprocess 里进入 running；否则序列回到 waiting 队头，下一步继续。一个 step 可以调度多个序列，直到 token budget 用完；`max_num_seqs` 限制的是 running 槽位，只有 chunk 完成的序列才占一个槽位，所以一步可能调度超过 `max_num_seqs` 个未完成的 chunk。这个细节是 chunked prefill 和普通 prefill 在调度计数上的区别。

这里有一个隐蔽的死锁：一个 chunked prefill 序列可能已经占了两页，但还没算完；另一个序列需要这些页才能完成。如果调度器只尝试队头，就会一直失败。mini 的 `_release_waiting_blocks` 在分配失败时释放其他 waiting 序列的块（把它们重置到 0），再重试。这个策略牺牲了被释放序列的进度，但保证系统能前进。生产引擎会更精细地选择牺牲谁。

用 40 token prompt、block size 16、budget 16 走一遍：第一步算 token 0-15，需要 1 页；序列回 waiting。第二步算 16-31，需要 2 页（总共），预算用完；序列回 waiting。第三步算 32-39，需要 3 页，chunk 完成，进入 running。这个过程中，`num_computed_tokens` 从 0 到 16 到 32 到 40，页表从 1 页到 3 页，每一步的 `computed_end` 都和 chunk 的 `end` 一致。

## 22.6 Decode 与 LCFS

`_schedule_decode` 按 `num_output_tokens` 升序排序，然后从尾部（输出最长）开始调度。每个 decode 需要 `q_len = 1 + (speculative_tokens if enable_speculative else 0)` 个位置；如果显存不够，抢占队头（输出最短）的序列。

抢占分两种：

- `stop_preempted_request=True`：标记 `FINISHED_PREEMPTED`，释放块，返回用户；
- `False`：`free_sequence`、`reset_for_recompute`、放回 waiting 队头。

第一版在 `_preempt` 里调用 `prepare_sequence` 重新匹配前缀，结果刚释放的块又被自己 pin 住，显存没有真正释放。测试 `test_preemption_still_finishes_requests` 复现了这个死锁。修复后，`_preempt` 只 reset，不重新匹配；下次 prefill 时再按需命中。

LCFS 的排序在 mini 里是 `sorted(self.running, key=lambda s: s.num_output_tokens)`，然后从尾部取。这个排序每次 decode 都做，复杂度 `O(R log R)`；生产调度器会用堆或增量维护，避免每步排序。mini 为了可读性保留排序，规模小的时候无所谓。

## 22.7 Postprocess 与状态回收

`postprocess(output, results)` 做四件事：

1. 把每个序列的 `num_computed_tokens` 更新为 `result.computed_end`；
2. prefill 完成的序列进入 running，未完成的回到 waiting 队头；
3. decode 完成的序列留在 running，结束的释放块并移除；
4. 注册新完成的前缀块。

`register_prefix_blocks` 不是每步把整个前缀重新哈希一遍，而是**增量注册**。`seq.extra["prefix_registered_blocks"]` 记录已经注册到第几页；每步从这一页开始，用前一页的哈希作为 `prev`，只计算新完成块的链式哈希，再调用 `block_manager.register`。如果前一页已经被淘汰、`block_hash` 变成 `None`，函数会从 0 重新开始，因为链式哈希断了一环就不能接着算。这个细节决定了长上下文 decode 的前缀注册成本是 `O(新增块)`，而不是 `O(总长度)`；如果每步全量重算，decode 的 CPU 时间会随上下文线性增长，ZOS 也藏不住。

这里有一个微妙的点：decode 的 `computed_end` 落后于序列长度一个 token。因为一步 decode 计算的是“最后一个已生成 token”的 K/V，新采样的 token 还没算。调度器用 `num_computed_tokens` 而不是 `length` 来分配页和注册前缀，保证一致。

## 22.8 测试与不变量

mini 的调度器测试覆盖：

- 分块 prefill 和完整 prefill 输出一致；
- 前缀缓存命中后输出一致；
- 抢占后所有请求都能完成；
- 取消请求后 `has_unfinished` 为假；
- 最大长度请求被正确忽略。

几个关键不变量：

- `num_computed_tokens <= seq.length`；
- 页表长度 >= `ceil(num_computed_tokens / block_size)`；
- 共享块的 `ref_count` 等于引用它的序列数；
- 被释放的块 `ref_count == 0`，但可以留在缓存里；
- decode 的 `computed_end` 通常是 `old_length + accepted`，不是 `old_length + verified`；但如果接受的 token 里包含 stop token，序列会提前结束，最终要夹到 `min(computed_end, seq.length)`，否则 KV 记账会超过逻辑长度。

这些不变量比“测试通过”更重要。测试只能覆盖你想到的场景；不变量可以在调试时快速定位问题。

## 22.9 与 DeepInfer C++ 调度器的对照

DeepInfer 的 `scheduler.hpp` 和 mini 的 `scheduler.py` 在策略上对应：

- `_schedule_prefill` 对应 `_schedule_prefill`；
- `_schedule_decode` 对应 `_schedule_decode`，都用 LCFS；
- `_preempt` 对应 `_preempt`，都支持 stop/recompute；
- `postprocess` 对应 `process_outputs` + `free_finished_seqs`；
- 前缀匹配对应 `Sequence.cache.block_table` + `BlockManager::allocate`。

差异在：DP 投票、MTP varlen、CP 对齐、多模态边界、C++ 对象池、状态缓存、结构化输出状态。mini 保留了“策略骨架”，去掉了“生产复杂度”。

## 22.10 一次真实的调试记录

前缀缓存命中率一直是 0。原因是 `lookup` 把 `ref_count == 0` 当成“块无效”，而缓存块在没人引用时正好是 0。单元测试 `test_prefix_cache_match_and_share` 最初也把期望写成 `ref_count == 2`，后来发现 free 之后应该是 1，测试本身也错了。修复后，`cache_hits` 从 0 变成 4，第二次生成的 prefill 步骤从 2 降到 1，输出保持一致。

另一个问题是抢占死锁：`_preempt` 调用 `prepare_sequence` 重新匹配前缀，把刚释放的块又 pin 住，显存一直不释放。调试时打印 `free_blocks`、`running`、`waiting` 的页表，发现 free 一直是 0，才定位到重新 pin。修复后，抢占测试从 `OutOfBlocksError` 变成全部完成，`preemptions=2`。

这两个例子说明，推理系统的 bug 往往不是数学错误，而是资源生命周期错误。测试要覆盖“释放后复用”“共享后写入”“抢占后重算”这些状态转换。

## 22.11 把 block_size 从 16 改成 128

这是一个很好的实验，因为它会同时影响分配、前缀命中、COW、调度和测试。

**容量约束**。`EngineConfig.__post_init__` 要求 `block_size <= max_num_batched_tokens`，并且 `max_model_len <= block_size × num_blocks`。把 block_size 改成 128 后，`max_num_batched_tokens` 至少要是 128；如果还要保持 512 的最大长度，`num_blocks` 至少是 4，但实际要留更多给并发。

**前缀命中粒度**。前缀命中长度一定是 block_size 的倍数；block 从 16 变 128 后，短 prompt 可能一个块都命中不了，长 prompt 的命中长度也会变粗。命中率可能下降，但每个命中的块更大，节省的 prefill 更多；净收益取决于长度分布。

**COW 成本**。一个 block 从 16 token 变成 128 token，COW 复制的数据量变成 8 倍。共享块被频繁追加时，COW 会成为瓶颈；生产引擎会尽量避免共享不完整的尾块，mini 的 `register_prefix_blocks` 只注册完整块，就是这个原因。

**调度粒度**。chunked prefill 的最小 chunk 受 block_size 约束；block 太大，短请求的 chunk 会被浪费，TTFT 可能变差。`_release_waiting_blocks` 释放等待序列的块时，块越大，被释放的工作越多。

**测试**。所有依赖 `block_size=16` 的断言都要改：页数、命中 token 数、COW 块 id、`num_blocks` 容量。第 26 章会讲如何用属性测试减少这种硬编码。

这个实验的结论不是“16 好还是 128 好”，而是“block size 是一个跨层参数”。它同时影响 kernel 对齐、前缀命中、COW、调度和显存碎片；生产值 128 是针对 V4.1 的 kernel 和 SWA 窗口调的，不能随便套到别的模型。

## 小结

分页 KV 和调度器是引擎的核心。BlockManager 用引用计数、链式哈希和 LRU 管理块；PagedKVCache 用页表读写 K/V 并在共享块上 COW；Scheduler 用 waiting/running、chunked prefill、LCFS 和抢占组织 step。三个实现陷阱——缓存块被误判、抢占重新 pin、chunk 占用饿死——都是“逻辑正确但资源管理错误”的典型，测试和显存统计能抓到它们。

## 练习

1. 画出一次 chunked prefill 的页表变化：prompt 长度 40、block size 16、budget 16，分几步完成？每步分配几页？
2. 解释为什么 `lookup` 不能因为 `ref_count == 0` 就返回 miss。
3. 如果 decode 一步验证了 4 个 draft，接受了 2 个，`computed_end` 应该前进多少？新采样的 token 有没有 KV？
4. 用 22.1 节的数字，模拟序列 A/B 的共享和 COW，写出每一步的 `ref_count` 和块 id。
