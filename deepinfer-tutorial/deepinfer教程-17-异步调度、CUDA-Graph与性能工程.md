# 第 17 章 异步调度、CUDA Graph 与性能工程

本章导读：decode 的每一步都有 CPU 调度、GPU forward、采样、后处理。同步执行时，GPU 会在 CPU 阶段空转。本章解释 DeepInfer 的 zero-overhead scheduling（ZOS）如何把 CPU 和 GPU 重叠，CUDA Graph 如何消除 kernel launch 开销，以及怎样用 benchmark 和 trace 得到可信的数字。

## 17.1 同步 step 的空洞

一个朴素的 decode step 是：

```text
schedule(N) -> forward(N) -> sample(N) -> post-process(N) -> schedule(N+1)
```

`schedule` 和 `post-process` 是纯 CPU 逻辑，`forward` 和 `sample` 在 GPU 上。`post-process(N)` 需要等 `sample(N)` 的结果从 GPU 拷回 CPU，所以中间有一次同步。同步之后，GPU 空闲，等 CPU 做完后处理和下一步调度。

用时间线表示：

```text
GPU:  [ forward N ][ sample N ] idle [ forward N+1 ] ...
CPU:      [schedule N]      [post N][schedule N+1]
```

空洞的大小取决于 CPU 逻辑的复杂度。请求多、结构化输出多、MTP 信息多时，`schedule` 和 `post` 可能占 step 时间的 10% 到 30%。对 decode 这种每步只有几毫秒的负载，这是很大的浪费。

具体的时间预算放在 17.8 节，用一组完整的 schedule/forward/sample/post 数字算给你看；这里先建立直觉：CPU 段有多长，GPU 的空洞就有多长，重叠的收益上限就是这段 CPU 时间。

## 17.2 ZOS 的重叠

Zero-overhead scheduling 的目标是让 GPU 在 step 之间没有空洞。做法是把流水线错开：

```text
GPU:  [ forward N ][ sample N ][ forward N+1 ][ sample N+1 ] ...
CPU:  [schedule N+1][post N-1][schedule N+2][post N]
```

在 GPU 跑 `forward(N) -> sample(N)` 时，CPU 同时做 `schedule(N+1)`、`launch_forward(N+1)`、`post-process(N-1)`、`launch_sample(N+1)`。这样 GPU 的队列里始终有下一步的工作，CPU 的延迟被藏到 GPU 计算后面。

DeepInfer 的 `LLMEngine.step()` 里能看到这个结构：`_execute_model_async_step` 把 metadata 传给 worker，`post_process` 在 sampler 之后回调，`DeferredRelease` 把资源释放推迟到下一次 `pre_sampler_sync`。`DEEPINFER_ZERO_OVERHEAD_SCHEDULING` 默认开启，`EngineArgs` 会根据 engine type 设置它。

ZOS 的难点不是“开一个线程”，而是保证依赖关系。`schedule(N+1)` 需要知道 `post-process(N)` 之后的序列状态，但 `post-process(N)` 还没执行。DeepInfer 的调度器用“乐观估计”处理一部分信息：它先按预算调度，等 `sample(N)` 的结果回来再修正。MTP varlen 更复杂：`schedule(N+1)` 时第 N 步的 confidence 还没返回，CPU 只能固定总预算，具体的每请求验证长度由 GPU 上的 TileLang kernel 重新计算。

可以把 ZOS 看成“带预测的流水线”。预测错了怎么办？调度器要能回滚或修正：例如序列已经结束，但调度器已经给它排了下一步；这时要用“完成序列列表”把它从 running 里移除，并释放资源。预测的代价是复杂度，收益是 GPU 利用率。

## 17.3 CUDA Graph

即使 CPU 调度被藏起来，每个 kernel 的 launch 仍有开销。Decode 一步可能调用几十到上百个 kernel，每个 launch 几微秒，累积起来可能占 step 时间的很大一部分。CUDA Graph 把一组 kernel 调用捕获成一张图，之后用一个 `graph.replay()` 启动，减少 launch 开销。

DeepInfer 的 `CudaGraphRunner` 按 batch size 捕获多张图。因为 CUDA Graph 的形状是静态的，batch size 不同就要不同的图；`max_batch_size` 是 `max_num_seqs × (1 + nextn)`，图按离散的 batch size 捕获，运行时选最接近的图并 pad。`CudaGraphData` 里保存 `input_metadata_source`、`input_metadata_device`、`graph` 和 `graph_memory_pool`。

Decode 用 CUDA Graph，prefill 通常不用，因为 prefill 的序列长度和 chunk 形状变化大，捕获成本高、复用率低。DFlash 的 draft 前向单独捕获一张图，`DFlashCudaGraphRunner` 把 broadcast、metadata 构建、draft 层、采样、pool 写入打包成一个 replay 单元。

CUDA Graph 的代价是显存和启动时间。捕获时要预留图内存池，图越多显存越大；启动时要跑 warmup，服务冷启动会变慢。DeepInfer 用 `decode_perf_cache` 缓存不同 batch 的实测耗时，避免每次启动都重新 profiling。

CUDA Graph 的约束要分 host 侧和 device 侧。host 侧不能有会改变 launch 数量或形状的控制流，不能 synchronize，不能绕过 graph pool 做会改变地址的分配；device 侧的分支不会被“固化”——捕获的是 kernel 的启动参数和依赖关系，kernel 内部的 `if (batch > 8)` 在 replay 时会按当时的输入重新执行。真正静态的是 grid/block 形状和 kernel 参数地址，所以 batch 变化要靠 pad 到固定形状或按 batch size 分图捕获，而不是靠 kernel 内部的分支兜底。把“device 分支也会固化”写进设计文档，会导致团队做出错误的规避方案（例如为了分支去捕获大量图），这一点要特别注意。

## 17.4 Decode 性能曲线

`ModelRunner.profile_decode_perf` 会按 batch size 逐个 replay CUDA Graph，测 `graph.replay()` 的耗时，做 warmup、多次测量、去掉最大最小后取平均。结果存成 `(batch_size, ms)` 曲线，调度器用它估算“多验证一个 draft 要多少时间”“当前 batch 下最优验证长度是多少”。

这条曲线是 DSpark 变长验证的基础。没有它，confidence 再准也无法把“接受概率”换算成“吞吐收益”。生产系统会把曲线缓存到磁盘，按模型、并行配置、硬件版本索引。

曲线通常不是线性的。小 batch 时，固定开销主导，时间随 batch 增长很慢；大 batch 时，计算和 KV 读取主导，时间快速增长。调度器要在曲线上找“每 token 成本最低”的点，而不是“单步时间最短”的点。

## 17.5 Profiling 与 benchmark 的正确姿势

DeepInfer **官方仓库**的 `examples/benchmark.py` 把 prefill 和 decode 解耦（本教程仓库的同名脚本参数不同，只测 CPU 玩具引擎的批处理趋势）：

- prefill 的 `--prefill_batch_size`、`--non_cached_tokens` 只影响 chunk 和 suffix；
- decode 的 `--decode_num_requests` 只影响 decode batch；
- prefill 需要的请求数 `N` 由 profiling 的 chunk 数反推，上限 512；
- decode 的输出长度由内部 step 数常量决定；
- 两个阶段唯一的耦合是 decode 复用 prefill 产生的共享前缀 KV。

测量窗口有 warmup、稳态采样、去掉与 MoE LB stats flush 冲突的区间。`PROFILE=1` 时，prefill 和 decode 各跑一个 profiler section，导出时首尾拼成一条 trace；trace 里固定包含 2 个 full-chunk prefill step 和 3 个稳态 decode step。注意 trace 里的 step time 包含 profiler 开销，bsz=1 时可能膨胀接近 50%，只能用来看 kernel 组成，不能用来推导 SPS。profiler 退出后还有 7% 到 11% 的残留减速，所以测量不能排在 profiler 之后。

这些细节说明，性能数字的可信度取决于测量方法。一个没有 warmup、没有稳态窗口、没有排除 GC/flush 干扰的 benchmark，数字可能差 20% 以上。上面提到的“2 个 prefill step、3 个 decode step、bsz=1 膨胀接近 50%、profiler 残留 7% 到 11%”是某次具体构建的测量现象，不是框架常数；换版本、换驱动、换 profiler 配置都会变，引用时要带上这些上下文。

一个可复现的 benchmark 至少要记录：硬件型号和数量、驱动/CUDA 版本、模型和精度、并行配置、batch 和长度分布、warmup 步数、测量步数、是否开启 ZOS/CUDA Graph/前缀缓存/推测解码、指标的分位数。缺少任何一项，别人都无法复现你的结论。

## 17.6 用 mini-deepinfer 观察重叠

mini 没有 GPU，但 `mini_deepinfer/pipeline.py` 提供了一个成本模型：

```python
timings = StageTimings(schedule_ms=1.2, forward_ms=8.0, sample_ms=0.8, post_ms=0.6)
result = simulate_zero_overhead(100, timings)
```

它计算同步总时间 `steps × (cpu + device)` 和重叠后的稳态时间 `max(cpu, device) × steps + min(cpu, device)`，给出加速比和 bubble 比例。这个模型不精确，但能把“为什么要重叠”讲清楚。

`ShapeSpecializedRunner` 是 CUDA Graph 的 CPU 类比：按 `(batch_size, q_len)` 缓存准备好的闭包，第一次 miss 时 capture，之后 hit 直接 replay。`DecodePerfCurve` 用分段线性插值估算不同 batch 的 step time，和 DeepInfer 的 decode perf cache 思路一致。

运行 `python examples/offline_inference.py` 可以看到 `steps=24`、`decode_steps=23`，说明 4 个请求在 1 个 prefill step 后被连续批处理；这就是 ZOS 想要重叠的那种“每步都有活干”的状态。

## 17.7 性能优化的顺序

面对一个慢的推理服务，建议按下面的顺序排查：

1. **测量**：先确认 TTFT、TPOT、吞吐、P99，分清是 prefill 还是 decode 慢。
2. **利用率**：看 GPU SM 利用率、HBM 带宽、NVLink 带宽、CPU 利用率。GPU 空转先查调度/同步，HBM 打满查 batch/量化，SM 低查 kernel 形状。
3. **batch 分布**：batch 太小固定开销高，太大尾延迟差；扫 Pareto 曲线找 SLO 内的工作点。
4. **KV**：命中率、block 碎片、COW、换出频率；长上下文看 SWA 重放和稀疏索引。
5. **通信**：all-to-all 的尾延迟、EPLB 不均衡、PD 传输。
6. **CPU**：schedule/post-process 是否成为瓶颈；ZOS 是否开启；Python 对象是否过多。
7. **kernel**：launch 开销、是否用上 CUDA Graph、低精度 GEMM 是否融合。

不要一上来就换 kernel。很多“GPU 慢”的问题其实是 batch 太小、CPU 调度太长或 KV 命中率太低。

一个更具体的排查方法是“打点计时”：在 schedule、forward、sample、post 四个阶段各加一个 GPU event 或 CPU 计时，连续跑 100 步，看哪一段的占比最大。如果 schedule 占比高，优化调度器或开 ZOS；如果 forward 占比高且 HBM 打满，优化 batch/量化/KV；如果 sample 占比高，看结构化输出 mask 或 logprobs。

## 17.8 一个 decode step 的时间预算

把一次 decode step 拆成时间片，可以看到 ZOS 和 CUDA Graph 各自省了什么。

假设同步实现的预算是：CPU 调度 1.2 毫秒、CPU 准备输入 0.4 毫秒、GPU forward 8.0 毫秒、GPU sample 0.8 毫秒、CPU 后处理 0.6 毫秒。同步总时间是 11.0 毫秒，其中 CPU 2.2 毫秒、GPU 8.8 毫秒，GPU 在 CPU 阶段空转 2.2 毫秒。用 `pipeline.py` 复算时写成 `StageTimings(schedule_ms=1.2, prepare_input_ms=0.4, forward_ms=8.0, sample_ms=0.8, post_ms=0.6)`；`StageTimings.cpu_ms` 会把准备输入计入 CPU 侧，`simulate_zero_overhead(100, timings)` 得到的同步总时间是 1100 毫秒，而不是默认 `prepare_input_ms=0` 时的 1060 毫秒。

ZOS 把 CPU 的调度和后处理挪到 GPU 计算后面，稳态周期变成 `max(2.2, 8.8) = 8.8` 毫秒。这里要区分两个口径：单步**延迟**从 11.0 降到 8.8 毫秒，降幅 20%；**吞吐**变成 `11.0/8.8 ≈ 1.25` 倍，提升 25%。同一个优化，按延迟说和按吞吐说得到的百分比不同，写 benchmark 时要注明是哪一个。如果 CPU 时间涨到 10 毫秒（例如结构化输出 mask、大量请求、复杂 MTP），稳态周期变成 10 毫秒，GPU 反而等 CPU；这时优化 CPU 比优化 kernel 更重要。

CUDA Graph 省的是 kernel launch。假设一步有 80 个 kernel，每个 launch 3 微秒，总 launch 开销 240 微秒；Graph 把 80 次 launch 变成一次 replay，省掉大部分 CPU 开销和驱动调度。对 8.8 毫秒的 step，240 微秒约 2.7%；kernel 越多、越小，收益越大。

通信是第三块。EP 的 all-to-all 如果不在计算后面重叠，会直接加到 step 时间里；如果重叠，step 时间由 `max(compute, communication)` 决定。Wide-EP 的收益来自权重流量下降，但前提是通信被藏住；如果通信暴露，EP 越大可能越慢。

这个预算的用途是判断“下一步该优化什么”。如果 GPU forward 占 80%，优化 kernel 或量化；如果 CPU 调度占 30%，开 ZOS 或把调度下沉到 C++；如果 launch 占 10% 以上，用 CUDA Graph；如果通信占 20%，优化 all-to-all 或调整 EP。不要在没有预算的情况下凭感觉优化。

## 17.9 ZOS 的依赖修正与失败恢复

ZOS 不是“开一个线程把调度扔到后台”。它是一条带预测的流水线：CPU 在 GPU 执行第 N 步时，已经排好了第 N+1 步。预测对了，GPU 没有空洞；预测错了，必须有修正机制，否则序列状态会错位。

常见的预测错误有四类。

**序列提前结束。** `schedule(N+1)` 时，CPU 以为序列还在 running，给它排了一个 decode slot；但 `sample(N)` 发现它输出了 EOS，`post(N)` 把它标记为完成。修正方法是：调度器维护“上一步完成的序列”列表，在真正执行前把已完成的序列从 running 里移除，并释放它的 block/state。DeepInfer 的 `process_outputs` 和 `free_finished_seqs` 就是做这件事。

**接受长度不确定。** 推测解码的第 N 步可能接受 0 到 k 个 draft，CPU 在 `schedule(N+1)` 时还不知道实际接受数。DeepInfer 用“乐观推进 + 按实际接受数回填”：先按 `seqlen_q` 排下一步，等 `accepted_token_ids` 回来再修正 `cache.num_tokens`。如果修正后发现 KV 不够，就要回滚或抢占。

**Confidence 还没返回。** MTP varlen 需要第 N 步的 confidence 来决定第 N+1 步验证多少 draft；但 confidence 是 GPU 算出来的，CPU 不能等。DeepInfer 把“总预算”和“每请求长度”分开：CPU 决定这一步最多验证多少，GPU 在拿到 confidence 后再分配具体长度。这样既不阻塞流水线，也不会超预算。

**执行失败。** kernel 崩溃、CUDA error、worker 进程退出、通信超时，都会让“已经排好的下一步”无法执行。修正方法是：把错误传播到 engine，标记受影响的序列为失败，释放资源，并让 worker 重新初始化。ZOS 下错误可能发生在异步回调里，必须有一个集中的错误通道，不能只在 `step()` 的返回值里报错。

调试 ZOS 的一个实用方法是“同步对照”：用同一个 prompt、seed 和配置，分别跑同步模式和 ZOS 模式，比较逐 token 输出。如果不一致，先检查“完成序列是否被重复调度”“接受数是否回填正确”“KV 长度是否和逻辑长度一致”。这三种错误在同步模式下不会出现，在 ZOS 下却很容易复现。

ZOS 的收益上限是 CPU 时间。如果 `schedule + post` 只占 step 的 10%，ZOS 最多省 10%；如果 CPU 时间超过 GPU 时间，ZOS 也救不了，反而会让预测更复杂。第 17.8 节的时间预算可以用来判断“值不值得开 ZOS”，而不是默认所有场景都开。

## 小结

异步调度把 CPU 的调度/后处理藏到 GPU 计算后面，CUDA Graph 把 kernel launch 开销压到一次 replay，decode 性能曲线把 batch 和 step time 连起来，profiling 方法决定数字是否可信。DeepInfer 的 ZOS 不是简单的多线程，而是带依赖修正的流水线；CUDA Graph 也不是万能的，prefill 和形状多变的场景要谨慎。mini 用成本模型和形状缓存演示了同样的思想。

## 练习

1. 设 schedule=1.2 ms、forward=8 ms、sample=0.8 ms、post=0.6 ms，计算同步和重叠的每步时间、100 步的总时间与加速比。
2. 解释为什么 CUDA Graph 更适合 decode 而不是 prefill。
3. 一个 benchmark 没有 warmup，测出来的 TTFT 偏低还是偏高？为什么？
4. 用 17.7 节的顺序，为一个“TTFT 正常但 TPOT 高”的服务设计排查步骤，写出每一步要看什么指标。
