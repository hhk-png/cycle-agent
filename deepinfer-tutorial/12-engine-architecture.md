# 第 12 章 引擎总体架构

本章导读：把 DeepInfer 拆成 entrypoint、engine、scheduler、worker、model executor、cache 六个层次，解释一次 `step()` 从 Python 到 C++ 再到 kernel 的路径。重点讲清楚三个边界：调度与执行的边界、Python 与 C++ 的边界、计算与通信的边界。

## 12.1 分层图

```text
+-------------------------------------------------------------+
| entrypoint: LLM.generate / add_request / step loop          |
+-------------------------------------------------------------+
| LLMEngine: tokenizer, request pool, output processing       |
|   |- Scheduler (C++): waiting/running, prefill/decode       |
|   |- BlockManager / StateManager: paged KV + SWA state      |
|   |- Worker: driver + CP workers                            |
+-------------------------------------------------------------+
| ModelRunner: input metadata, forward, sample, post-process  |
|   |- model: DeepseekForCausalLM, V4 modules                 |
|   |- CacheEngine: GPU blocks, states                        |
|   |- CUDA Graph runner, DFlash graph runner                 |
+-------------------------------------------------------------+
| kernels: FlashMLA, DeepGEMM, DeepEP, TileLang, sampler      |
+-------------------------------------------------------------+
```

最上面是用户看到的 API，最下面是 GPU/NPU 指令。中间每一层都在解决不同尺度的不确定性问题：entrypoint 处理字符串和协议，engine 处理请求生命周期，scheduler 处理 step 的组成，worker 处理设备和并行，kernel 处理单个算子。

这张图还可以按“状态”来读。`Sequence` 是长期状态，跨 step 存在；`SchedulerOutputs` 是这一步的决策；`SequenceMetadata` 是决策的运行时快照；`InputMetadata` 是设备侧的输入布局；`SamplerOutput` 是这一步的结果。每一层都把上一层的“意图”翻译成下一层能执行的“数据”，同时把状态收敛到更小的范围。读代码时如果迷路，问自己“现在在改哪一层状态”，通常就能回到主线上。

## 12.2 初始化顺序

`LLMEngine.__init__` 的顺序很值得读：

1. 保存四类 config（model、cache、parallel、scheduler）。
2. 加载 tokenizer；如果是多模态模型，创建 `MultimodalProcessor`。
3. 创建 `Counter`、`RequestOutputPool`。
4. `_init_workers()`：为每个 CP rank 启动 worker 进程，driver worker 在当前进程；建立分布式组。
5. `load_model`：加载权重，初始化 DFlash 的 MTP info pool。
6. `_init_cache()`：先 `profile_num_available_blocks` 估算显存，分配 KV blocks，初始化 cache engine，warm up model。
7. 创建 `BlockManager`、`StateManager`、`Scheduler`，把 scheduler 和 multimodal processor 连起来。
8. 创建 `StatsLogger`、`ProfileManager`（可选 HTTP profiling）。

这个顺序不能随便换。比如 cache 初始化必须在 model load 之后，因为要减去权重占用的显存；scheduler 必须在 cache 之后，因为它需要 block manager；DFlash 的 MTP pool 必须在 model load 之后，因为它依赖 MTP 层。

`EngineArgs.create_engine_configs` 还做了一件容易忽略的事：从 `ModelMetadata` 推导 `tokens_per_block`。DeepInfer 的 `resolved_tokens_per_block` 固定为 128，如果用户传了不同的 `tokens_per_block`，会直接断言失败。这说明 block size 不是随便调的，它和 SWA 的 128 窗口、压缩边界都绑定。这里要区分两个“128”：128 token 是 block 的逻辑大小；128 字节是 KV block 或页表缓冲区在显存里的地址对齐要求。页表项本身通常只有 4 或 8 字节，128 字节不是“一个页表项的大小”，而是为了让 TMA、向量化和 cache line 访问高效而对齐的粒度。前者决定一次分配多少 token，后者决定 kernel 能不能用对齐的批量访存，二者不是同一个单位。

初始化的另一个关键是 warmup。第一次 forward 会触发 kernel 编译、CUDA Graph 捕获、内存池分配和 BLAS 线程池初始化；如果不 warmup，第一个请求的 TTFT 会包含这些固定开销。生产系统通常在启动时跑几种典型形状，把 kernel 和 graph 都预热好，再开始接流量。

## 12.3 一次 step 的时序

`LLMEngine.step()` 的主干：

```python
def step(self):
    seq_metadata_list, scheduler_outputs = self.scheduler.schedule(pool)
    # 处理 clone_states / clone_blocks / zero_states（前缀 fork、抢占）
    self.post_schedule(scheduler_outputs)
    # 多模态 fetch
    request_outputs = self._run_workers(
        "execute_model_step",
        driver_kwargs={...},
        is_prompt=scheduler_outputs.prompt_run,
    )
    # 延迟释放上一轮的 metadata / output pool
    return request_outputs
```

实际的 `process_outputs` 不在 `step()` 里同步做，而是在 worker 的 sampler 之后回调 `post_process`。这是零开销调度（ZOS）的一部分：GPU 在跑 forward/sample 时，CPU 可以处理上一步的输出、调度下一步。

`_execute_model_async_step` 把 `seq_metadata_list` 和 `scheduler_outputs` 传给 worker。driver worker 负责准备输入、执行 forward、采样；CP worker 通过 broadcast 接收 metadata，执行相同的 forward。`execute_model_step` 里不能有 CUDA synchronize，否则会打断异步流水线。

把一次 step 的时间线画出来，可以分成四段：CPU 调度、CPU 准备输入、GPU forward、GPU sample。同步实现里这四段串行；ZOS 里 CPU 的调度和后处理被挪到 GPU 计算后面。第 17 章会给出具体的时间模型。

## 12.4 SchedulerOutputs 与 SequenceMetadata

调度器的输出有两部分：

- `SchedulerOutputs`：这一步调度了哪些 `Sequence`，是 prompt 还是 decode，`num_batched_tokens`，完成的序列，忽略的序列，clone/zero 的 state/block 列表，FLOPs 估算。
- `SequenceMetadata`：每个被调度序列的运行时视图，包括 `seqlen_q`、`seqlen_k`、block table、positions、sampling params、MTP 信息、structured output matcher。

`Sequence` 是长期状态，跨 step 存在；`SequenceMetadata` 是 step 级快照，放在对象池里复用。这个分离很重要：调度器改 `Sequence` 的状态，model runner 只读 `SequenceMetadata`，避免 Python/C++ 两边同时改同一份数据。

`SchedulerOutputs` 里的 `clone_states`、`clone_blocks`、`zero_states` 是借用存储的 numpy array，Python 侧必须在下次 `schedule()` 前消费完。这种“零拷贝 + 生命周期约定”在性能代码里很常见，但也很容易写出 use-after-free；DeepInfer 用注释和 `DeferredRelease` 来管理。

为什么不用一个结构同时表示长期状态和 step 快照？因为两者的生命周期不同。`Sequence` 要被调度器反复修改、被输出池引用；`SequenceMetadata` 只活一个 step，可以被池化、被广播、被序列化。合并会让“改一个字段”变成“同步两份数据”，在 C++/Python 边界上尤其危险。

## 12.5 Worker 与并行

`Worker` 是设备侧的执行代理。driver worker 在当前进程，非 driver 的 CP worker 在子进程。`_run_workers` 会把方法名和参数广播给所有 worker，driver 执行后返回结果。

`ModelRunner` 负责：

- `_prepare_input`：把 `SequenceMetadata` 变成 `InputMetadata`，包括 block table、slot mapping、positions、seq_lens、CUDA Graph 选择；
- `execute_main_model`：调用模型 forward，写 KV cache；
- `execute_sample`：准备 sampler 输入，调用 `model.sample`；
- `post_process`：执行 sampler 的 post_funcs，更新 prompt logprobs，flush timer。

`CacheEngine` 管理 `gpu_blocks` 和 `gpu_states`。`gpu_blocks` 是主 KV + index KV 的 paged 存储；`gpu_states` 是 SWA/压缩边界状态的固定 slot。`CacheConfig` 在 profile 之后才有 `num_gpu_blocks`、`bytes_per_block`、`num_state_seqs`、`bytes_of_state_per_seqs`。

Worker 的并行模型要从两个视图看，而不是两套卡。同一批 rank 上，attention 和非专家部分是 DP：每个 DP 副本有自己的 scheduler 和 KV，请求由全局负载均衡器分配；MoE 部分在这些 rank 上做 EP：一次 forward 内通过 all-to-all 把 token 发给专家所在的 rank。CP 则是把序列切分，同一组内的 rank 共享一次 forward、交换 K/V。`EP64DP64` 是同一批 64 卡的两种编号，不是 64×64 张卡；把 DP 和 EP 当成两组独立的卡，会多算一个数量级的 KV 和通信。driver worker 负责协调，非 driver worker 负责执行；这个划分决定了“谁能独立调度、谁必须同步”。

## 12.6 Python 与 C++ 的边界

调度器在 C++ 里，原因不是“C++ 更快”这么简单。调度是每步都要做的热路径，涉及大量小对象、排序、页表操作；Python 的 GIL 和对象开销会让它成为瓶颈。DeepInfer 把 `Scheduler`、`BlockManager`、`Sequence`、`SequenceMetadata`、`RequestOutputPool` 都放在 `csrc/engine/`，通过 pybind 暴露给 Python。

这个边界的代价是调试困难。C++ 侧的异常、生命周期、借用内存不会自动被 Python 的引用计数保护；一旦出错，往往表现为段错误或静默数据损坏。读代码时要特别注意注释里的“借用存储”“调用方必须在下一次 schedule 前消费完”“不要 CUDA synchronize”等约定。

mini-deepinfer 把调度器放在 Python，牺牲性能换可读性。第 13 章会用伪代码对照 C++ 的实现，帮助你在两边切换。

如果你要自己写一个引擎，建议先用 Python 把策略跑通，再把热路径逐步下沉到 C++/Rust。下沉的判据不是“语言快”，而是“这段代码每步都跑、对象分配多、GIL 竞争明显”。调度器、block manager、采样器通常值得下沉；tokenizer、HTTP、配置解析不值得。

## 12.7 计算的边界：forward 与 sample

DeepInfer 把一步拆成 `execute_main_model` 和 `execute_sample` 两个阶段。前者是模型 forward，后者是 sampler。两者之间不能有同步，否则会破坏 ZOS 的重叠。

Sampler 不是简单的 `argmax`。它要处理：

- temperature、top-k、top-p；
- logprobs、top_logprobs、prompt_logprobs；
- 结构化输出的 vocab mask；
- MTP 的多 token 采样和 confidence；
- 停止条件、EOS、first_n_token_no_eos；
- 每个序列的 `seq_seed`。

`SamplerOutput` 里同时有 `sampled_token`（目标模型自己采的）和 `verified_tokens`（推测验证接受的），还有 `predict_tokens`（下一步的 draft）、`num_generated_tokens`、`num_verified_tokens`、`so_status` 等。调度器要根据这些信息决定下一步的 `seqlen_q`。

forward 和 sample 的分离还有一个好处：采样可以在 CPU 上异步做，或者用一个小 kernel 做；forward 的 GPU 时间可以被下一步的调度重叠。代价是采样需要把 logits 从 GPU 拷回 CPU，或者把采样逻辑放在 GPU 上。DeepInfer 的选择是尽量把采样放在 GPU，减少同步。

## 12.8 生命周期与对象池

高频路径上不能频繁分配 Python 对象。DeepInfer 用 `SequenceMetadataPool`、`RequestOutputPool`、`MTPInfoPool` 复用对象；`DeferredRelease` 把释放推迟到下一次 `pre_sampler_sync`，与 GPU 计算重叠。

`RequestOutput` 的生命周期尤其微妙：`step()` 返回的 numpy array 借用 pool 的存储，下一次 step 会被释放。调用方如果要保留，必须 `pool.clone()`。这和很多“返回 Python 对象”的直觉不同，是性能优化的代价。

对象池的通用模式是“预分配 + 复用 + 显式归还”。难点不在分配，而在“什么时候可以安全复用”。如果对象还被 GPU kernel 引用，提前复用会导致数据竞争；如果归还太晚，池会膨胀。DeepInfer 用 `DeferredRelease` 把归还点绑定到 stream 的同步点，保证 GPU 已经消费完。

mini-deepinfer 没有对象池，直接创建 dataclass。它的性能不重要，正确性和可读性更重要。书中在讲对象池时，会明确说这是生产优化，不是教学实现的必需。

## 12.9 序列状态机

DeepInfer 的 `SequenceStatus` 不只是“等待/运行/结束”三个状态。常见状态包括 WAITING、RUNNING、FINISHED_STOPPED、FINISHED_LENGTH_CAPPED、FINISHED_ABORTED、FINISHED_PREEMPTED、FINISHED_IGNORED、FINISHED_SO_FAILED、FINISHED_VE_FAILED。每一次状态转换都对应一个资源动作。

WAITING 表示请求还没完成 prefill，可能已分配部分 block；RUNNING 表示 prompt 算完、进入 decode；FINISHED_STOPPED 是遇到 EOS/stop；FINISHED_LENGTH_CAPPED 是达到 max_tokens 或 max_model_len；FINISHED_ABORTED 是用户取消；FINISHED_PREEMPTED 是显存不足且配置为停止；FINISHED_IGNORED 是 prompt 太长或无法分配；FINISHED_SO_FAILED 是结构化输出失败；FINISHED_VE_FAILED 是视觉编码失败。最后两个是生产/多模态路径的状态，mini 的 `SequenceStatus` 只有前八个，没有 VE 状态；读 mini 时不要去找它。

状态机的关键不变量是：FINISHED 状态必须释放 block/state；WAITING 的序列不能出现在 decode 调度里；RUNNING 的序列必须有完整的 prompt KV；被抢占的序列要么结束，要么回到 WAITING 并清空 KV 计数。违反这些不变量，通常表现为显存泄漏、重复释放或调度器空转。

`postprocess` 是状态转换的集中点。prefill 完成把 WAITING 推到 RUNNING，decode 完成保持 RUNNING 或转 FINISHED，抢占把 RUNNING 拉回 WAITING，abort 可以从任意状态转 FINISHED_ABORTED。读 DeepInfer 的 C++ 代码时，跟着 `process_outputs` 里的 `_check_stop`、`append_token_id`、`free_seq_nolock` 走一遍，就能看清状态和资源的对应关系。

## 12.10 与 mini 的对照

mini 的 `LLMEngine` 约 440 行，但保留了同样的分层：

- `engine.py`：请求生命周期、step、采样、输出；
- `scheduler.py`：waiting/running、prefill/decode、抢占；
- `cache_engine.py`：PagedKVCache、KVBlockStore；
- `model/tiny_transformer.py`：模型 forward；
- `sampling.py`：采样；
- `metrics.py`：指标。

它没有 C++ 调度器、worker 进程、CUDA Graph、对象池、ZOS。第 20 章的保真度矩阵会逐项说明哪些差距是“规模缩小”，哪些是“能力缺失”。

## 12.11 初始化与故障域检查清单

引擎的很多问题不是发生在 steady state，而是发生在启动和故障切换时。上线前可以按下面的顺序检查。

**初始化顺序**：配置校验 → 并行组 → 权重加载 → 显存 profile → KV 分配 → scheduler/block manager → warmup。任何一步失败都要给出可定位的错误，而不是一个空指针或超时。特别是 KV 分配必须在权重加载之后，因为要减去权重、激活、通信 buffer 和 CUDA Graph 预留。

**warmup 覆盖**：最短 prompt、最长 chunk、最大 batch、推测验证形状、结构化输出 mask 形状、多模态输入形状。warmup 不是“跑一次 forward”，而是让所有会在生产出现的 kernel 和 graph 都被捕获。

**对象池边界**：`SequenceMetadata`、`RequestOutput`、`MTPInfo` 的借用存储什么时候归还？如果 GPU 还在读，提前复用会导致数据竞争；如果归还太晚，池会膨胀。DeepInfer 用 `DeferredRelease` 把归还绑定到 stream 同步点，这是一个值得借鉴的模式。

**故障域**：一个 DP rank 挂了，其他 rank 能不能继续？一个 EP rank 挂了，整个 EP 组是不是不可用？一个 P 实例挂了，KV 能不能从 3FS 恢复？一个 D 实例挂了，运行中的请求能不能转发？把每个组件的故障域画出来，才能决定副本数、PDB 和降级策略。

**状态机不变量**：FINISHED 必须释放 block/state；WAITING 不能出现在 decode 调度里；RUNNING 必须有完整 prompt KV；被抢占的序列要么结束，要么回到 WAITING 并清空 KV 计数。这些不变量比“测试通过”更能定位资源泄漏。

## 12.12 从显存预算到 num_gpu_blocks

初始化时最容易被忽略的一步，是把“这张卡有多少显存”翻译成“能放多少个 KV block”。`profile_num_available_blocks` 不是简单地用总显存除以 block 大小，它要逐项扣除。可以用一个可复算的预算式：

```text
可用 KV 显存 = HBM × gpu_memory_utilization
             - 权重
             - 激活峰值
             - CUDA Graph / workspace
             - 通信 buffer
             - 框架与碎片余量
num_gpu_blocks = floor(可用 KV 显存 / bytes_per_block)
```

假设一张卡有 80 GiB，`gpu_memory_utilization=0.9`，权重是 FP8 的 16 GB（约 14.9 GiB），prefill 激活峰值 4 GiB，decode 的 CUDA Graph 和 workspace 6 GiB，EP 通信 buffer 3 GiB，框架与碎片余量 2 GiB。可用 KV 是 `72 - 14.9 - 4 - 6 - 3 - 2 = 42.1 GiB`。如果每块是 128 token、1 个 KV 头、128 维、FP8、40 层，前面算过一块约 1.25 MiB，那么 `num_gpu_blocks ≈ 42.1 × 1024 / 1.25 ≈ 34,500`。这个数字还要减去 state slot 的占用，不能全部给 block KV。

每一项都可能成为“KV 明明够却 OOM”的原因。权重最容易算，但要注意是否含 draft 和 ViT；激活峰值取决于 chunk 大小和是否用 FlashAttention，`T×T` 的分数矩阵如果被 materialize，会从 GiB 级跳到几十 GiB；CUDA Graph 的显存池按捕获形状预留，捕获的 batch 越多，池越大；通信 buffer 取决于 EP/CP 的 in-flight 消息数；碎片余量不是“浪费”，而是分配器在长时间运行后不可避免的 overhead。

这个预算式也解释了为什么 `gpu_memory_utilization` 不是越高越好。调到 0.98，留给激活和碎片的余量变小，短请求可能没事，长 prompt 的 prefill 会突然 OOM；调到 0.8，KV 容量下降，并发和缓存命中率都会受影响。生产系统通常先用一个保守值启动，跑一段真实流量，观察峰值激活和碎片，再逐步调高。第 19.6 节的容量规划会把这个预算和 SLO 一起算。

mini 没有真实的显存 profile，它用 `num_blocks` 直接配置容量；但 `EngineConfig.__post_init__` 会检查 `max_model_len <= block_size × num_blocks`，相当于一个最小的预算校验。读生产代码时，把 `profile_num_available_blocks` 的每一步和上面的减法对应起来，就能知道哪个参数在吃显存。

## 12.13 显存分配器、碎片与工作区

12.12 的预算式回答的是“这张卡有多少显存可以给 KV”，但没有回答“这些显存怎样被分配、复用和归还”。生产推理里很多“明明还有显存却 OOM”的问题，根源不在预算算错，而在分配器。理解分配器，才能读懂 `profile_num_available_blocks` 为什么要留碎片余量，以及为什么 warmup 之后显存占用会“只增不减”。

显存分配有两层。底层是设备分配器，例如 CUDA 的 `cudaMalloc`/`cudaFree`；它们向驱动申请物理页，代价高，而且可能同步整个设备。上层是缓存分配器（caching allocator），例如 PyTorch 的 CUDA allocator 或推理引擎自己的内存池；它一次向设备申请较大的 segment，再在 segment 内切分给张量，释放时只归还给池，不立刻还给驱动。这样做的好处是热路径上的分配接近指针加法，坏处是“池里 reserved 的显存”不等于“当前 allocated 的显存”，两者之间的差额就是潜在的碎片和缓存。

看三个数：`reserved` 是分配器向驱动要到的总显存，`allocated` 是当前张量实际占用的部分，`reserved - allocated` 是池里空闲但尚未归还的块。OOM 的典型情形不是 `allocated` 接近总量，而是请求的大小找不到一个足够大的连续空闲块，即使空闲总量足够。比如一张 80 GiB 的卡 reserved 了 72 GiB，allocated 只有 50 GiB，池里还有 22 GiB 空闲，但最大的连续块只有 8 GiB；这时申请 10 GiB 的激活就会失败。内部碎片来自分配粒度取整（小分配按 512 字节、较大分配按 2 MiB 对齐），外部碎片来自不同大小的块被反复切分和释放后留下的空洞。

推理引擎会用几种办法控制碎片。第一，**把生命周期长的东西预分配**：KV block pool、state slot、block table、通信 buffer 都在启动时按上限分配，step 内不再向分配器要大块。第二，**按大小分桶**：把常见的激活形状放进固定的 workspace，避免每步申请不同大小。第三，**流感知复用**：只有确认某个 stream 已经消费完，才把块还给池；否则会出现“看起来空闲、实际还被 kernel 引用”的悬空复用。第四，**减少分配种类**：同一段 workspace 在 GEMM、reduce、all-to-all 之间复用，按最大值预留，而不是每类算子各留一块。

激活显存是另一个容易被低估的部分。prefill 的 attention 如果显式 materialize 分数矩阵，大小是 `heads × T × T × dtype`；T 到 128K 时，即使 FP16 也会到几十 GiB。FlashAttention 一类算法通过分块 online softmax 避免写出完整的 `T×T`，把激活压到 `O(T × d)`；chunked prefill 则把 T 切成固定预算，进一步限制峰值。反过来，如果为了调试打开了一个会 materialize 分数的实现，原本能跑的长 prompt 会突然 OOM，这不是 KV 不够，而是激活峰值变了。

workspace 的账本要按“峰值”而不是“总和”算。GEMM 的 split-K 归约、top-k 排序、all-to-all 的发送/接收缓冲、量化 scale 的临时张量，不会同时达到峰值；引擎通常复用同一块大 workspace，按所有算子里的最大需求预留。真正需要相加的是“会同时存活”的部分，例如一层 forward 的输入激活、残差和 attention 输出。把不同生命周期的峰值相加，会高估显存；把同时存活的部分漏掉，会在长 prompt 上 OOM。

CUDA Graph 把这个问题放大了。Graph 在捕获时固定张量地址，回放时不再经过 Python 分配器；因此捕获阶段申请的内存会进入一个 graph 私有的内存池，并在 graph 生命周期内保留。捕获的 batch 形状越多，池越大；如果每个形状各用一个池，显存会按形状数量累加；如果多个 graph 共享同一个 pool handle 并保证回放顺序不重叠，池可以复用。工程上的做法是：先 warmup 分配器和 kernel，再捕获一组有代表性的形状（例如 decode 的 1/2/4/8/16/32），把不常见的形状回退到 eager 路径；不要把“所有可能的 batch”都捕获，那会让 graph 池吞掉本应给 KV 的显存。

排查 OOM 时，先分清是哪一类。若是权重或 KV 的静态占用超了，预算式会直接告诉你；若是激活峰值超了，长 prompt 或大 chunk 是嫌疑人；若是碎片，表现为“同样的负载，跑一段时间后开始 OOM”，重启后恢复。可用的手段包括打印 `reserved/allocated` 和最大空闲块、在请求之间（而不是 step 中间）调用一次 `empty_cache`、开启 `expandable_segments` 让分配器用虚拟内存映射减少外部碎片、调整 `max_split_size_mb` 限制大块被切分。`empty_cache` 会同步并归还缓存，不能放在热路径；它适合在 drain 之后、模型切换之前做一次清理。

OOM 的处理策略也要写清楚。可以在准入层拒绝新请求、降低 batch、减少 chunk、把部分 KV 换出，或者返回一个明确的 429/503；但不能捕获 OOM 之后继续用可能已经部分写入的 KV 状态，那会把一个容量问题变成正确性问题。生产引擎通常把 OOM 当作“本 step 失败、请求回滚、实例进入降级”的信号，而不是一个可以静默重试的异常。

mini-deepinfer 没有真实分配器：`PagedKVCache` 在构造时按 `num_blocks × block_size` 预分配 NumPy 数组，`memory_bytes()` 和 `used_bytes()` 分别报告容量和占用。它没有碎片，也没有 CUDA Graph，但保留了同一条不变量：step 内不做大块分配，容量在启动时确定，超限时抛出 `OutOfBlocksError` 而不是让进程崩溃。读生产代码时，把 12.12 的预算式、这一节的分配器和第 14 章的 block manager 连起来，就能解释“为什么配置的 `gpu_memory_utilization` 是 0.9 而不是 1.0”。

## 小结

DeepInfer 的引擎是分层的：entrypoint 处理协议，LLMEngine 处理生命周期，C++ Scheduler 处理 step 组成，Worker/ModelRunner 处理设备执行，CacheEngine 处理分页 KV，kernel 处理算子。一次 step 的关键是“调度输出 -> 输入 metadata -> forward -> sample -> 回写状态”，而 ZOS 把 CPU 和 GPU 的重叠推到了极致。读代码时先抓 `LLMEngine.step` 和 `Scheduler.schedule`，再向下展开。

## 练习

1. 画出 `LLMEngine.step()` 的时序图，标出哪些阶段在 CPU、哪些在 GPU、哪些可以重叠。
2. 解释为什么 `Sequence` 和 `SequenceMetadata` 要分开，如果合并会有什么问题。
3. 为什么 `execute_model_step` 里不能有 CUDA synchronize？如果加了，会破坏什么？
4. 用 12.8 节的对象池模式，设计一个 `RequestOutputPool` 的复用和归还规则，说明什么时候可以安全复用。
