# 第 18 章 GPU 执行模型与 kernel 工程

本章导读：前面几章反复说“decode 受带宽限制”“融合能减少 HBM 往返”“CUDA Graph 能省 launch 开销”，但这些都是结论。本章回到硬件，解释一条 kernel 在 GPU 上到底怎么执行、为什么 tiling 能改变瓶颈、FlashMLA/DeepGEMM/DeepEP 各自融合了什么、TileLang/Triton/CUDA 的取舍是什么，以及怎样用 profiler 判断一个 kernel 是算力受限、带宽受限还是延迟受限。读完本章，你应该能读懂一个推理 kernel 的设计意图，而不是逐行猜索引。

## 18.1 GPU 的基本执行单位

一块 GPU 由若干个 SM（Streaming Multiprocessor）组成。每个 SM 里有 CUDA core、Tensor Core、寄存器堆、共享内存/L1、调度器。线程以 32 个为一组组成 warp，warp 内的线程执行同一条指令（SIMT）；多个 warp 可以在 SM 上交替执行，用延迟隐藏来填满流水线。

一次 kernel launch 会把 grid 切成 block，block 分配到 SM 上，block 内的线程再分成 warp。block 的数量、每个 block 的线程数、寄存器用量、共享内存用量共同决定 occupancy，也就是一个 SM 能同时驻留多少 warp。Occupancy 高不一定快：如果 kernel 受 HBM 带宽限制，更多 warp 也只是排队等数据；如果 kernel 受延迟限制（例如大量小算子、分支、同步），更高 occupancy 才能藏住延迟。

GPU 的存储层次可以简化成四级：寄存器、共享内存/L1、L2、HBM。寄存器和共享内存的带宽比 HBM 高一个到两个数量级，但容量小。kernel 优化的核心动作，就是把重复访问的数据从 HBM 搬到共享内存/寄存器，把中间结果留在片上，减少 HBM 往返。第 4 章的 roofline 说“搬多少字节决定时间”，本章说的是“怎么搬”。

一个常被忽略的资源是寄存器。寄存器用量过高会降低 occupancy，甚至导致 spill（溢出到本地内存，实际上还是 HBM）。一个看似“只多了几个临时变量”的改动，可能让 occupancy 从 50% 掉到 25%，性能反而下降。读 kernel 时不要只看算法，还要看寄存器、共享内存和 occupancy 的权衡。

## 18.2 内存合并与 tiling

HBM 的访问以 cache line / sector 为单位。如果一个 warp 的 32 个线程访问连续地址，硬件可以把它们合并成少数几次事务；如果每个线程访问相隔很远的地址（strided access），就会产生大量独立事务，有效带宽下降几倍。这就是 memory coalescing。

矩阵乘法的朴素实现通常是“每个线程算一个输出元素，循环读 A 的一行和 B 的一列”。B 的列访问是 strided，A 的行会被不同线程重复读，HBM 流量远大于理论值。Tiling 把矩阵切成小块，每个 block 负责一个 tile：先把 A、B 的 tile 搬到共享内存，再在片上做乘加。这样每个元素从 HBM 读一次，被 tile 内多个线程复用，HBM 流量接近理论下界。

用公式看，朴素 GEMM 的 HBM 流量约 `2 × M × N × K` 个元素（每个乘加都读一次），tiled GEMM 可以降到约 `M × K + K × N + M × N`（每个输入读一次，输出写一次）。当 `M=N=K=8192` 时，前者约 1.1e12 个元素，后者约 2e8 个元素，差 5000 倍。当然，真实 kernel 不会这么糟，但 tiling 的收益方向是明确的。

Tiling 的代价是边界处理和共享内存容量。tile 太大，共享内存放不下；tile 太小，复用率低、启动开销高。一个 GEMM kernel 的 tile 形状（`BM × BN × BK`）通常要针对硬件调优：BM/BN 决定输出 tile 的大小，BK 决定每次从 HBM 搬多少。第 18.3 节会讲更高级的异步拷贝和 warp specialization。

## 18.3 现代 GEMM：TMA、warp specialization 与持久化

Hopper/Blackwell 之后的 GEMM kernel 通常包含四个技巧。

**异步拷贝/TMA**：用 TMA（Tensor Memory Accelerator）把多维 tile 从 HBM 直接搬到共享内存，不占用线程的寄存器，也不需要每个线程算地址。TMA 支持多维、边界填充和 swizzle，适合 attention/GEMM 的 tile 布局。

**Warp specialization**：把 warp 分成不同的角色，例如 producer warp 负责发 TMA 拷贝，consumer warp 负责 Tensor Core 计算，epilogue warp 负责写回。不同角色用 barrier 同步，形成流水线。这样一个 warp 等数据时，另一个 warp 可以计算，延迟被藏住。

**Persistent kernel**：不再“一个 grid 处理一个矩阵”，而是启动固定数量的 block，让它们循环处理多个 tile。这样可以复用已加载的权重、减少 launch 开销、更好地控制 L2 命中。对 decode 这种小 batch、多次调用的场景，persistent kernel 的收益尤其明显。

**Split-K**：当 `M` 和 `N` 很小、`K` 很大时（例如 decode 的 GEMV），一个输出 tile 的计算量不足以填满 GPU。Split-K 把 `K` 切成多段，不同 block 并行计算部分和，最后用原子加或第二个 kernel 归约。代价是额外的归约和精度问题；收益是把小矩阵的并行度拉起来。

DeepGEMM 的 FP8/FP4 GEMM 把这些技巧和 scale 布局结合在一起。它不只是“低精度矩阵乘”，而是把反量化、scale 应用、累加和 epilogue 融合进一个 kernel。读这类 kernel 时，先看它的 tile 形状、scale 的排布、TMA 的 swizzle，再看主循环；不要一上来就逐行读汇编。

## 18.4 Attention：online softmax 与 Flash 系列

标准 attention 要先算完整的 `Q × K^T` 分数矩阵，再做 softmax，再乘 V。分数矩阵的大小是 `Q × K`，对 32K 上下文就是 10 亿个元素；写回 HBM 再读回来，带宽成本远超计算。FlashAttention 的核心是 online softmax：把 K/V 分成 tile，边算边更新 softmax 的分子、分母和输出，不把完整分数矩阵写回 HBM。

online softmax 的递推可以写成：

```text
m_new = max(m_old, max(scores_tile))
l_new = l_old * exp(m_old - m_new) + sum(exp(scores_tile - m_new))
o_new = o_old * exp(m_old - m_new) + exp(scores_tile - m_new) @ V_tile
```

最后输出 `o / l`。这个递推的数值稳定性来自“始终减去当前最大值”；它的正确性来自“重新缩放之前的累加结果”。FlashAttention 的 tiling 把 `Q` 也切成 tile，外层循环 Q、内层循环 K/V，每个 Q tile 只保留自己的 `m/l/o`。

FlashMLA 在此基础上针对 MLA 的 latent 结构做融合，但“融合”的范围要说准确：Q/KV 的线性投影通常仍是独立的 GEMM；FlashMLA 的 attention kernel 融合的是 Q 的 norm、RoPE、core attention 的 tiling 与 online softmax、输出侧的 inverse O-RoPE 和 cast。这样 Q/K/V 的投影结果可以留在寄存器/共享内存附近，减少中间张量写回 HBM。decode 时 `Q` 只有一行，但 `K/V` 很长，FlashMLA 用 split-K 把 KV 分块并行，最后归约。官方说开源版比内部版少一个手调 SASS 优化，decode kernel 慢 5% 到 10%，但端到端差距最多 5%，因为 attention 只是 step 的一部分。

Paged attention 是另一个维度：K/V 不是连续的，而是按 block table 间接寻址。kernel 要先根据页表把逻辑位置映射到物理 block，再做 attention。这会增加地址计算和随机访问，但换来按需分配和前缀共享。DeepInfer 用 128 token 的 block，同时要求页表/地址按 128 字节对齐；前者是逻辑分页粒度，后者是访存对齐，单位不同但共同决定间接寻址能不能走向量化/TMA。

变长 batch 是第三个维度。一个 batch 里每个序列的 `seqlen_q` 和 `seqlen_k` 不同，kernel 不能假设统一形状。常见做法是把所有序列拼成一条“总 token 轴”，用 `cu_seqlens` 记录每段的起止，再在 kernel 里按段处理。这就是 varlen attention；MTP/推测解码的验证也用它来一次处理多个 draft。

## 18.5 MoE kernel：grouped GEMM 与 all-to-all 重叠

MoE 的一层在 kernel 层面是“按专家分组的 GEMM”：每个专家有自己的权重，收到的 token 数不同。如果每个专家单独 launch 一个 GEMM，小专家会浪费大量 launch 和 tile 填充；如果所有专家拼成一个大 GEMM，又要求 token 按专家排序。

Grouped GEMM 的做法是：先按专家把 token 排序，记录每个专家的 token 数和偏移，然后一次 launch 一个 kernel，让不同 block 处理不同专家的 tile。每个 block 根据偏移找到自己的专家权重和 token 段。这样既避免了多次 launch，又保持了每个专家的矩阵形状。

DeepEP 的 all-to-all 负责把 token 从“来源 rank”发到“专家所在 rank”，再把结果发回。它和 Mega MoE 配合，把 dispatch、专家 GEMM、combine 融合成一个流水线：一边接收 token，一边计算，一边发送结果。通信和计算重叠的关键是“分块流水线”：不要等所有 token 都到齐再算，而是每到一个 chunk 就开始算。

MoE kernel 的难点是负载不均衡。某个专家收到的 token 特别多，它的 GEMM 会成为尾延迟；其他专家早早算完，GPU 空转。EPLB 在“放置”层面缓解这个问题，grouped GEMM 在“kernel”层面用动态 tile/persistent block 缓解。两者要配合，不能只靠一个。

## 18.6 逐元素融合：RMSNorm、RoPE、量化与 KV 写入

Decode 的每步都有大量逐元素操作：RMSNorm、RoPE、残差加、SiLU、量化 cast、KV cache store。如果每个操作单独 launch 一个 kernel，每个 kernel 都要把张量从 HBM 读一遍、写一遍，带宽被反复消耗，launch 开销也累积。

融合的思路是把“读同一份数据、做多个逐元素变换、写回”的操作合成一个 kernel。例如 `fused_norm_rope_hadamard_cast_store_kvcache` 把 RMSNorm、RoPE、Hadamard 变换、量化 cast、KV cache store 放在一个 kernel 里：输入 hidden 只读一次，中间结果留在寄存器/共享内存，最后直接写 KV。这样既省带宽，又省 launch。

融合的边界是“有没有跨 token/跨 head 的依赖”。RMSNorm 需要沿 hidden 维归约，RoPE 是逐对旋转，量化是逐元素，KV store 是按位置写；它们可以在一个 block 内完成，因为一个 block 处理一个 token 的所有 head。如果融合涉及全局归约或跨 block 通信，就会变复杂，通常要拆成两个 kernel。

融合还会改变数值路径。把 BF16 的中间结果留在寄存器里，可能比写回 HBM 再读回更精确（少了舍入），也可能因为寄存器精度不同而有差异。做低精度/确定性测试时，要覆盖融合前后的输出，不能假设“数学一样，结果就一样”。

## 18.7 Stream、Event 与 CUDA Graph

一个 CUDA stream 是一串按顺序执行的操作；不同 stream 可以并发。Event 用来在 stream 之间同步，或者测量时间。推理引擎通常有多个 stream：主计算 stream、KV 拷贝 stream、通信 stream。把不依赖的操作放到不同 stream，可以让拷贝、通信和计算重叠。

CUDA Graph 是把一串操作（包括 kernel launch、memcpy、event）捕获成一张图，之后一次 replay。它省掉的是“每次 launch 的 CPU 开销”和“驱动层的调度开销”，不是 GPU 执行时间。对 decode 这种“每步几十到上百个 kernel、每个 kernel 几微秒”的负载，launch 开销占比很高，Graph 的收益明显。

Graph 的约束在第 17 章讲过：静态形状、无动态控制流、无同步。除此之外，Graph 还会固定内存地址，所以输入/输出要用固定的 buffer，或者用 graph 的 update 机制改指针。DeepInfer 的 `CudaGraphData` 保存 `input_metadata_source` 和 `input_metadata_device`，就是为了在 replay 前把数据拷到固定地址。

## 18.8 编译器与 DSL：CUDA、Triton、TileLang

写 kernel 有三条常见路线。

**CUDA C++**：最底层，能精确控制寄存器、共享内存、warp、TMA、Tensor Core。性能上限最高，但开发慢、可移植性差、容易写出难以维护的索引。FlashMLA、DeepGEMM 的底层部分通常是 CUDA/CUTLASS。

**Triton**：用 Python 写 tile 级程序，编译器负责生成 CUDA。它的抽象是“block 程序 + 张量块”，适合 attention、GEMM、逐元素融合。开发效率高，性能接近手写 CUDA，但对极端优化的控制不如 CUDA。

**TileLang**：面向 tile 的 DSL，强调显式布局、流水线和硬件特性，适合把多个算子融合成一个 kernel。DeepInfer 用 TileLang 写了一批 fused kernel，例如 norm+RoPE+cast+store。它的优势是“能表达 tile 级流水线”，代价是学习曲线和编译器成熟度。

选型的判据不是“哪个语言高级”，而是“这个 kernel 的瓶颈是什么”。如果瓶颈是 HBM 带宽，融合和 tiling 最重要，Triton/TileLang 足够；如果瓶颈是 Tensor Core 的指令级调度、TMA 的 swizzle、寄存器的精细控制，CUDA/CUTLASS 更合适；如果只是写一个原型验证数学，NumPy/PyTorch 最快。

## 18.9 用 profiler 判断瓶颈

Nsight Compute（ncu）是分析单个 kernel 的常用工具。它能看到：SM 利用率、Tensor Core 利用率、HBM 吞吐、L2 命中、occupancy、warp stall 原因、指令吞吐。判断瓶颈的顺序可以简化成三步。

第一步，看 HBM 吞吐。如果接近峰值带宽，kernel 是带宽受限，优化方向是减少字节（融合、量化、复用）或提高算术强度（更大 tile、更大 batch）。

第二步，看 Tensor Core 利用率。如果 HBM 没满、Tensor Core 也没满，可能是延迟受限：occupancy 太低、依赖链太长、同步太多、寄存器 spill。看 stall reason，常见的是“long scoreboard”（等内存）、“barrier”（等同步）、“not selected”（warp 太多但资源不足）。

第三步，看 launch 和尾部。如果 kernel 本身很快，但 step 时间很长，可能是 launch 开销、stream 同步、小 kernel 太多。这时 CUDA Graph、融合、persistent kernel 更有效。

Nsight Systems（nsys）用来看整条时间线：CPU 在干什么、GPU 在干什么、stream 之间有没有空洞、通信和计算有没有重叠。ZOS 的收益、CUDA Graph 的收益、all-to-all 的尾延迟，都更适合用 nsys 看。

一个常见的误判是“看到 GPU 利用率 100% 就以为没问题”。利用率高可能只是“很多小 kernel 在排队”，实际 HBM 和 Tensor Core 都没打满。要看具体单元的吞吐，而不是一个总的 utilization 数字。

## 18.10 mini-deepinfer 的对照

mini 没有 GPU，它的 attention 是 `einsum + softmax`，GEMM 是 NumPy 的 `@`，逐元素操作是独立的 NumPy 调用。它对应的是“朴素实现”，不是“融合实现”。你可以用 `examples/roofline.py` 看到朴素 attention 和 Flash 式 attention 的带宽差距：在 32K 上下文、64 头、128 维的例子里，朴素实现要把分数矩阵写回再读回，多搬约 139.6 GB；按 3 TB/s 算，多出约 46.5 毫秒的**访存时间**，瓶颈从算力变成带宽。这里 46.5 是时间，不是字节数；把 GB 和时间混着说，是性能讨论里很常见的一类单位错误。

这个对照的价值是让你理解“为什么生产 kernel 要这样写”。mini 的代码证明数学是对的；生产 kernel 的代码证明在硬件上跑得快。两者不是替代关系，而是“参考实现”和“优化实现”的关系。第 27.3 节的可复用清单给出了“哪些逻辑能迁移、哪些性能路径必须重写”的映射方向。

## 18.11 读一个 kernel 的方法

拿到一个不熟悉的 kernel，按下面的顺序读：

1. **接口**：输入输出的 shape、dtype、layout、scale 布局。先确认它算什么。
2. **并行划分**：grid/block 怎么切，一个 block 负责哪个 tile，warp 怎么分工。
3. **数据搬运**：哪些数据从 HBM 到共享内存，哪些留在寄存器，有没有 TMA/异步拷贝。
4. **主循环**：tile 的循环顺序，K 维怎么累加，有没有 split-K。
5. **epilogue**：怎么写回，有没有融合量化/激活/残差。
6. **边界**：变长、mask、padding、对齐怎么处理。
7. **同步**：barrier、event、stream 在哪里，有没有不必要的同步。

不要从第一行索引开始读。先建立“这个 kernel 的骨架”，再去看细节。大多数性能问题出在并行划分和数据搬运，而不是某一个索引表达式。

## 18.12 一个融合 kernel 的写法

下面是一段 TileLang 风格的伪代码，展示 `fused_norm_rope_hadamard_cast_store_kvcache` 的结构。它不是可编译的 TileLang，而是把“一个 block 处理一个 token 的所有 head”这个映射写清楚。

```text
# 伪代码：一个 block 负责一个 token，block 内按 head 分 warp
@kernel
def fused_norm_rope_quant_store(
    X:        [T, d_model],        # 输入 hidden
    W_norm:   [d_model],           # RMSNorm 权重
    W_q, W_k, W_v: [d_model, d_head * H],
    cos, sin: [T, d_head],         # RoPE 表
    K_cache:  [num_blocks, block_size, H_kv, d_head],
    V_cache:  [num_blocks, block_size, H_kv, d_head],
    block_table: [max_blocks],     # 逻辑页 -> 物理页
    positions: [T],
    Q_out:    [T, H, d_head],      # 供下一个 attention kernel 读取
):
    token = block_id.x              # 一个 block 负责一个 token
    lane  = thread_id.x             # block 内线程按 head/维度分工
    x = load(X[token, :])           # 只读一次 hidden
    rms = sqrt(sum(x * x) / d_model + eps)
    h = x / rms * W_norm

    q = h @ W_q                     # 投影可以是独立的 GEMM
    k = h @ W_k
    v = h @ W_v
    q = rope(q, cos[token], sin[token])
    k = rope(k, cos[token], sin[token])

    # 量化 + 打包 + scale 写入，反量化融进后续 attention/GEMM
    q8, q_scale = quantize(q)
    k8, k_scale = quantize(k)

    # 按页表写入 KV cache；写之前确保目标 block 已 COW
    phys = block_table[positions[token] // block_size]
    offset = positions[token] % block_size
    store(K_cache[phys, offset], k8, k_scale)
    store(V_cache[phys, offset], v)

    # q8 写入 Q_out，下一个 attention kernel 直接读，不回到 HBM 的中间张量
    Q_out[token] = q8
```

这段伪代码里有三个真正的融合点，外加一个容易说过头的点。第一，RMSNorm 的输出 `h` 留在片上，后面的 RoPE 和量化直接用它，不需要把 `h` 写回 HBM 再读一次。第二，RoPE 在寄存器里做，不需要单独 launch。第三，量化 cast 和 KV store 合并，scale 直接写到 kernel 要求的布局。

容易说过头的是 QKV 投影。如果 `h @ W_q` 是一次完整的 GEMM，它通常由独立的 GEMM kernel 完成，`h` 必须从 HBM 读入；这时融合发生在“GEMM 之后”的 norm/RoPE/量化/store 链上，省掉的是这些中间结果的往返，不是投影本身。只有当投影被切成 per-token、per-head 的小矩阵向量乘，并且和 norm 放在同一个 block 里时，`h` 才能一直留在寄存器；这种写法适合 decode 的小 batch，不适合 prefill 的大 GEMM。伪代码把两种写法混在同一个函数里，是为了展示数据流，不是声称一个 kernel 能同时做好大 GEMM 和逐元素融合。读真实 kernel 时，先确认它的边界画在哪里：是“GEMM + epilogue”，还是“epilogue only”。

真实的 TileLang/CUDA 实现还要处理：tile 的边界、head 的并行划分、TMA 的多维拷贝、scale 的对齐、页表的间接寻址、COW 的同步、不同 dtype 的分支。这些细节不能靠伪代码代替，但伪代码能让你在读真实 kernel 时知道“哪一段对应哪个数学步骤”。

如果你用 Triton 写一个原型，结构会更接近：

```python
@triton.jit
def fused_kernel(X, W, K, V, ...):
    token = tl.program_id(0)
    cols = tl.arange(0, BLOCK_D)
    x = tl.load(X + token * D + cols)
    rms = tl.sqrt(tl.sum(x * x) / D + EPS)
    h = x / rms * tl.load(W + cols)
    ...
```

Triton 的 `program_id` 对应 block，`tl.arange` 对应 tile 的列，`tl.load/store` 对应 HBM 访问。它的抽象层次比 CUDA 高，适合写 attention/GEMM/逐元素融合的原型；如果要精确控制 TMA、warp specialization 和寄存器，还是要回到 CUDA/CUTLASS。

## 18.13 SM100 与 Ascend 950 的差异

DeepInfer 支持 NVIDIA SM100 和华为 Ascend 950。同一套框架要跑在两种硬件上，kernel 层必须有平台抽象；但“支持”不等于“性能对等”。

**指令与精度**：SM100 有 FP4 Tensor Core、TMA、异步拷贝和成熟的 CUTLASS 生态；Ascend 950 有自己的矩阵单元、低精度格式和 CANN 库。FP4 的编码、scale 布局、舍入模式可能不同，同一份权重在两个平台上需要不同的打包和反量化路径。

**内存层次**：NVIDIA 的 HBM + L2 + 共享内存/寄存器层次，和 Ascend 的片上存储层次不完全一样。tiling 的大小、double buffer 的深度、TMA 的用法都要按平台调；把一个平台调优的 tile 直接搬到另一个平台，可能 occupancy 或带宽利用率很差。

**互联**：NVIDIA 用 NVLink/NVSwitch，Ascend 用 HCCL 和自家的互联；all-to-all 的拓扑、消息大小、拥塞行为不同。Wide-EP 的 EP 规模在两种平台上不能直接照搬。

**软件栈**：CUDA 13 和 CANN 9.2 的编译器、驱动、性能工具不同。NVIDIA 有 Nsight Compute/Systems，Ascend 有对应的 profiling 工具；判断瓶颈的方法类似，但指标名称和采集方式不同。

**确定性**：公开资料说 batch-invariant 和 deterministic sampling 只在 NVIDIA 上生效。跨硬件可复现时，不能假设两个平台逐位一致；要在容差内比较 logits，并在应用层做约束。

对读者的意义是：读 DeepInfer 的 kernel 代码时，先看它有没有平台分支、scale 布局有没有按平台切换、通信库是不是可替换。一个“支持多硬件”的框架，真正的成本在抽象层和测试矩阵，而不是一句 README 声明。

端侧是硬件差异的另一个极端。手机 NPU、Apple Silicon、x86 CPU 和浏览器 WebGPU 的内存层次、算子覆盖和功耗约束都不同；同一个 4-bit 模型在不同设备上的 tok/s 可能差几倍。第 45 章会把端侧的 roofline、量化体积、能耗和云边路由展开；本章的 tiling、融合和 occupancy 仍然适用，但“带宽优先”比“Tensor Core 利用率”更常成为瓶颈。

## 小结

GPU kernel 的优化围绕三件事：减少 HBM 往返、提高并行度、隐藏延迟。Tiling 和融合减少搬运，split-K/persistent kernel 提高并行度，warp specialization/异步拷贝/occupancy 隐藏延迟。FlashMLA、DeepGEMM、DeepEP、TileLang 都是这些原则在具体算子上的实现。判断瓶颈要用 profiler 看 HBM、Tensor Core、occupancy 和 stall reason，而不是只看一个利用率数字。mini 的 NumPy 实现是数学参考，不是性能参考。

## 练习

1. 一个 8192³ 的 FP8 GEMM，输入输出各读/写一次，计算它的理论 HBM 流量；再算朴素实现（每个乘加读一次）的流量，比较差距。
2. 解释 online softmax 为什么不需要保存完整的分数矩阵，它的递推里 `m` 和 `l` 各起什么作用。
3. 一个 decode kernel 的 HBM 吞吐只有峰值的 30%，Tensor Core 利用率也很低，最可能的原因是什么？给出三个排查方向。
4. 比较 CUDA、Triton、TileLang 的适用场景，各写一句“什么时候选它”。
