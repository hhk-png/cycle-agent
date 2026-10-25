# 第 43 章 推理编译器与图优化

本章导读：模型代码是“一堆张量运算”，GPU 执行的是“一串 kernel”。中间隔着一层编译器：它把运算组织成图，做常量折叠、死代码消除、算子融合、内存规划和 kernel 选择，最后交给运行时执行。第 17 章讲 CUDA Graph 如何省 launch 开销，第 18 章讲单个 kernel 怎么写；本章补上中间这一层，解释“为什么同样的数学，编译后的执行计划能差几倍”，并用 `mini_deepinfer/compiler.py` 把图优化、融合和内存规划跑成可测试的代码。

## 43.1 为什么需要图编译器

Eager 执行模式下，每个 PyTorch 或 NumPy 运算都是一次独立的调用：读输入、计算、写输出，然后下一个运算再读一遍。对单个大矩阵乘，这种开销可以忽略；对 decode 这种“每步几十到上百个小算子”的负载，开销会累积成主要成本。具体有四类浪费。

第一是 **kernel launch 和框架调度**。每个算子都要经过 Python、框架、驱动，最后到 GPU；算子越小，固定开销占比越高。第二是 **HBM 往返**。逐元素操作如果把中间结果写回 HBM，下一个算子再读回来，带宽会被反复消耗。第三是 **内存分配**。每个中间张量都申请一块显存，峰值内存高，分配器压力大。第四是 **没有跨算子视角**。单个算子不知道前后文，无法选择更合适的 layout、tile 或融合方式。

图编译器把这些问题放在一起解决：先建图，再做与硬件无关的优化，再做与硬件相关的 lowering，最后生成执行计划。它不一定生成机器码；很多推理框架的“编译”只是生成一张调度表或一组融合 kernel 的调用序列。关键不是“有没有编译器”，而是“有没有在图的层面做决策”。

## 43.2 IR：把模型变成可分析的数据

一个最小的图 IR 包含四样东西：节点（算子）、边（张量依赖）、属性（shape、dtype、layout、eps、axis 等）、输入输出。节点按拓扑序排列，每个张量只被定义一次，这就是 SSA（静态单赋值）形式。SSA 让“这个张量从哪里来、到哪里去”变成可追踪的，死代码消除和内存规划都依赖它。

`mini_deepinfer/compiler.py` 的 `Op` 就是一个 SSA 节点：`name` 是算子类型，`inputs` 是输入张量名，`output` 是输出张量名，`attrs` 是属性。`Graph` 在构造时校验三件事：每个输入在使用前已经定义、每个输出只定义一次、所有 graph output 都有生产者。这个校验看起来简单，却能抓住“图拼接时漏了一个节点”这类错误。

真实编译器还有更复杂的 IR。高层 IR 保留 `matmul`、`softmax`、`attention` 这样的语义算子；低层 IR 可能降到 `tile_load`、`mma`、`copy`、`barrier` 这样的硬件原语。中间还会有 layout 和 dtype 的传播。DeepInfer 的 TileLang kernel 更接近低层 IR：它显式描述 tile、流水线和共享内存；而 CUDA Graph 捕获的是已经 lowering 完的 kernel 调用序列。理解这三层的关系，读代码时就不会把“图优化”和“kernel 优化”混为一谈。

## 43.3 图级优化：先做不改变数学的简化

图级优化的第一原则是“不改变数值语义”。常见的 pass 有四种。

**常量折叠**：如果一个算子的所有输入都是编译期常量，就直接算出结果，替换成常量节点。权重是运行时输入，通常不会折叠；但 shape 计算、mask、位置编码表、量化 scale 这些可能是常量。`constant_fold()` 用一个已知值表遍历图，把所有输入都已知的节点求值并替换成 `const`。示例里两个常量相加再乘一个未使用的结果，折叠后只剩一个 `const` 节点，值为 `[4.0, 6.0]`。

**死代码消除**：从 graph output 反向遍历，只保留对输出有贡献的节点。训练图里常见的 dropout、辅助损失、未使用的分支，在推理图里都可以删掉。`dead_code_elimination()` 用一个 `needed` 集合反向传播；它必须在融合之后再做一次，因为融合会把一些中间节点变成无人使用的节点。

**公共子表达式消除**：如果两个节点计算完全相同的表达式，只保留一个。它需要把算子、输入、属性规范化后做哈希。CSE 对共享的 norm、RoPE 表、重复的 mask 很有用，但要注意副作用和随机性；推理图通常没有副作用，做起来比训练图简单。

**代数化简**：`x * 1`、`x + 0`、`transpose(transpose(x))`、`reshape(reshape(x))` 可以消掉；`matmul` 和 `add` 在满足结合律时可以重排，但浮点加法不满足结合律，重排会改变数值结果，必须谨慎。推理编译器通常只做“逐位安全”的化简，或者把重排限制在容差可接受的范围内。

这些 pass 的共同点是“不需要硬件信息”。它们可以在任何目标上运行，也应该在任何目标上运行。先做这些，再做融合和 layout，能避免在错误的图上做昂贵的搜索。

## 43.4 融合：把多个算子变成一个 kernel

融合是推理编译器最有价值的优化，因为它同时减少 launch、HBM 往返和中间内存。融合有几种粒度。

**逐元素融合**：`add`、`mul`、`silu`、`cast` 这类算子，输入输出形状相同，可以串成一个 kernel，读一次、算一串、写一次。`swiglu` 就是 `silu(x) * gate` 的融合，`fuse_swiglu()` 在图上匹配这个模式。

**归约融合**：RMSNorm、LayerNorm、softmax 需要沿某个维度归约，通常和后续的 cast、RoPE、KV store 融合。DeepInfer 的 `fused_norm_rope_hadamard_cast_store_kvcache` 就是这类融合：一个 block 处理一个 token，hidden 只读一次，中间结果留在寄存器，最后直接写 KV。

**GEMM epilogue 融合**：矩阵乘之后常接 bias、激活、量化、残差；把这些放进 GEMM 的 epilogue，输出只写一次。DeepGEMM 的 FP8/FP4 路径就是这种结构。

**注意力融合**：FlashAttention 把 QK^T、softmax、PV 融合成一个 tiled kernel，不把完整的分数矩阵写回 HBM；FlashMLA 进一步融合 MLA 的投影、RoPE 和输出变换。第 18 章已经展开。

融合不是越多越好。融合后的 kernel 可能寄存器压力过大、occupancy 下降，或者共享内存不够，反而变慢；融合还会降低代码复用，让不同 shape 的组合爆炸。生产编译器会用代价模型决定“融不融”，而不是见到模式就融。

`fuse_rmsnorm_matmul()` 和 `fuse_swiglu()` 是两个保守的例子。前者只在 `rmsnorm` 的输出恰好被一个 `matmul` 使用时才融合；如果 norm 的输出还被别的算子使用，它必须保留，因为中间结果被共享。后者只在 `silu` 的输出恰好被一个 `mul` 使用时才融合。这个“单消费者”条件是融合正确性的基础，也是很多真实编译器 pattern matcher 的第一条规则。

## 43.5 内存规划：峰值比总和更重要

一个图里有几十个中间张量，如果每个都单独分配，峰值内存可能远高于“同时活跃”的张量总和。内存规划的目标是让生命周期不重叠的张量共享 buffer，把峰值压到接近理论下界。

做法是活跃性分析。每个张量有一个定义点和一个最后使用点；在这段区间内它是活跃的，区间不重叠的张量可以共享同一块内存。`plan_memory()` 按定义顺序遍历张量，释放已经过期的 slot，选择“能装下且最小”的空闲 slot；没有合适的就新开一个。它不保证全局最优，但保证两条不变量：活跃区间重叠的张量不会共享 slot；总分配量不超过所有张量大小之和。

示例里的 11 节点图，所有张量大小之和是 10,624 字节，规划后的峰值是 8,320 字节，节省约 21.7%。这个数字不大，因为图很小、很多张量同时活跃；真实模型的中间激活更多，复用空间更大。反过来，如果图里有一个巨大的权重和一个短暂的 mask，规划器不会让它们共享，因为权重的生命周期覆盖整个图。

真实内存规划还要处理对齐、碎片、in-place 和 graph memory pool。对齐影响向量化和 TMA；碎片会让“理论能放下”变成“实际分配失败”；in-place 可以省内存，但要求读写不重叠；CUDA Graph 需要固定的内存池，规划器要把地址固定下来。这些约束比“活跃性分析”复杂得多，但核心思想相同：先知道每个 buffer 活多久，再决定谁和谁共享。

## 43.6 布局、量化与 dtype 传播

图优化不只是“删节点”和“合节点”，还要决定“数据怎么排”。同一个 `matmul`，输入是 row-major 还是 column-major，输出要不要转置，scale 是 per-tensor 还是 per-block，都会影响 kernel 的选择和性能。

布局传播是一个约束满足问题：每个算子对输入输出 layout 有偏好，相邻算子之间的 layout 转换有成本。编译器要在“插入转换”和“选一个次优 kernel”之间取舍。TMA 和 Tensor Core 对 swizzle、对齐、连续维度有要求；一个看起来无害的 transpose 可能让后续 GEMM 无法用 TMA，性能差几倍。

量化 pass 更复杂。它要把 FP16/BF16 的权重和激活转成 FP8/FP4，插入 scale，传播 dtype，选择反量化的位置。scale 的粒度要和 kernel 对齐：per-tensor 简单但精度差，per-block 精度好但需要额外的 layout 变换。DeepInfer 的 `transform_sf_into_required_layout` 就是在做 scale 的布局重排。量化 pass 还必须保证数值语义：哪些算子可以在低精度算，哪些必须升到 FP32 累加，哪些需要在输出前反量化。

dtype 传播的一个常见错误是“只在权重上改 dtype，激活还是 FP16”。这样 GEMM 可能是 FP8 输入、FP16 累加，带宽省了，但计算没省；更糟的是，scale 的布局和 kernel 假设不一致，结果会错。量化是一个端到端的 pass，不是改一个字段。

## 43.7 kernel 选择与自动调优

同一个算子可能有多个 kernel 实现：小 batch 用 GEMV，大 batch 用 GEMM；短序列用非 split-K，长序列用 split-K；不同 GPU 架构用不同的 tile。编译器要按 shape、dtype、layout、硬件和当前 batch 选择一个实现，这就是 kernel dispatch。

Dispatch 可以查表，也可以用代价模型。查表简单、可预测，但组合爆炸；代价模型灵活，但需要校准。生产系统通常两者结合：常见 shape 查预调好的表，罕见 shape 用代价模型或默认实现。自动调优（autotune）在首次遇到某个 shape 时跑几个候选，选最快的，把结果缓存到磁盘；代价是首次延迟和编译时间。

`mini_deepinfer/pipeline.py` 的 `DecodePerfCurve` 是这种思想的简化版：按 batch size 存实测 step time，用分段线性插值估算未见过的 batch。编译器的 autotune cache 类似，只是 key 更复杂（shape、dtype、layout、硬件、kernel 版本）。

kernel 选择还要和融合一起考虑。一个融合 kernel 在某个 shape 下可能比“两个单独 kernel”慢；编译器要用同一套代价模型比较“融合方案”和“非融合方案”，而不是默认融合更好。

## 43.8 CUDA Graph 与图编译不是一回事

很多人把 CUDA Graph 和“图编译”混为一谈。两者有关联，但不是同一层。

图编译器处理的是“算子级计算图”：它决定融合、内存、kernel。CUDA Graph 处理的是“已经 lowering 完的 kernel 调用序列”：它把这些调用捕获成一张图，减少 launch 和驱动开销。图编译器可以在 CUDA Graph 之前或之后运行；DeepInfer 先用编译器和手写 kernel 生成执行计划，再用 CUDA Graph 捕获 decode 的固定形状。

CUDA Graph 的约束会影响编译器的决策。捕获要求静态形状、固定地址、无动态控制流；如果编译器生成的 kernel 在运行时才决定分支，Graph 捕获会失败。生产系统通常为 decode 捕获多张按 batch size 区分的图，为 prefill 保留 eager 执行；编译器要能把“哪些形状需要捕获”作为输入，而不是假设所有图都能捕获。

图编译器的缓存也要和 CUDA Graph 的缓存对齐。模型、精度、并行配置、kernel 版本、驱动版本变化时，编译产物和 Graph 都要失效；只失效其中一个，会读到不匹配的执行计划。第 34 章的 manifest 可以承担这个绑定。

## 43.9 正确性验证：优化后的图必须和参考实现一致

图优化最容易出的问题不是崩溃，而是“结果稍微不对”。融合改变了浮点运算的顺序，内存复用可能读到未初始化的 buffer，量化 pass 可能把不该降精度的算子降了。验证要分层。

第一层是**参考解释器**。保留一个不优化、逐算子执行的路径，对同一组输入比较输出；容差按 dtype 定，FP32 可以很严，FP8/FP4 要放宽。第二层是**随机形状测试**。固定的几个 shape 覆盖不了边界；随机生成 batch、序列长度、head 数，检查形状推导和 padding。第三层是**数值路径测试**。同一个数学，融合前后、量化前后、不同 layout 前后，结果要在容差内一致；如果差异超出容差，要能定位到哪个 pass。第四层是**缓存失效测试**。改了模型或 kernel 版本，旧的编译缓存不能再用；否则会出现“本地能跑、线上结果不同”的问题。

`mini_deepinfer/compiler.py` 的测试就是前两层的最小版本：`optimize()` 前后对同一组输入比较所有输出，`test_memory_plan_reuses_slots_and_respects_liveness` 检查活跃区间重叠的张量不共享 slot，`test_fusion_requires_single_consumer` 检查共享中间结果不会被错误融合。

## 43.10 生产工具链与 DeepInfer 的位置

推理编译器的生态可以分成几类。

**TorchInductor / Torch.compile**：从 PyTorch 图出发，做融合、内存规划、Triton 代码生成；适合通用模型和快速迭代，对极端 shape 的控制不如手写 kernel。

**TensorRT / TensorRT-LLM**：NVIDIA 的推理编译器加运行时，做层融合、精度校准、kernel 选择和 engine 序列化；性能强，但模型适配和自定义算子成本高。

**XLA / OpenXLA**：面向 TPU 和加速器的编译器，强调 whole-program 优化和 layout 传播；在 JAX 生态里常用。

**MLIR / TVM**：编译器基础设施，提供多层 IR 和可扩展的 pass；适合自研编译器和特定硬件后端，学习曲线陡。

**TileLang / Triton**：更接近 kernel DSL，负责“一个融合 kernel 怎么写”，而不是整张图的调度；DeepInfer 大量使用 TileLang 写 fused kernel，同时用手写的 C++/CUDA 调度器组织执行。

DeepInfer 不是一个通用图编译器。它的模型结构固定，很多融合是手写的；它的“编译”更多体现在 kernel 生成、CUDA Graph 捕获和形状特化上。理解这一点很重要：不要指望把任意模型丢给 DeepInfer 就能自动优化；它的性能来自“为 V4.1 手工设计的执行计划”，而不是“通用编译器自动发现”。通用编译器适合多模型平台，专用引擎适合单模型极致优化，两者解决的是不同问题。

## 43.11 mini-deepinfer 的实现

`mini_deepinfer/compiler.py` 提供 `Op`、`Graph`、`constant_fold`、`dead_code_elimination`、`fuse_rmsnorm_matmul`、`fuse_swiglu`、`plan_memory`、`estimate` 和 `optimize`。`build_mlp_graph()` 构造一个包含两种融合模式的 11 节点图。

运行：

```bash
python examples/compiler.py
```

一次运行的输出包括：原始图 11 个节点；融合后 9 个节点，模式是 `fused_rmsnorm_matmul` 和 `swiglu`；优化前后所有输出在 1e-6 内一致；op reduction 约 18.2%；launches 从 11 降到 9；FLOPs 15,488，访存字节 15,488；内存峰值 8,320 字节，所有张量之和 10,624 字节，节省约 21.7%；常量折叠把两个常量相加再乘的结果化成 `[4.0, 6.0]`，死代码消除后只剩一个 `const` 节点。这些数字是确定性的，但只对应这个玩具图，不代表真实模型的优化率。

mini 没有实现 CSE、代数化简、layout 传播、量化 pass、autotune 和代码生成。它证明的是“图 IR、模式匹配、活跃性分析和参考对照”这套方法的骨架；真实的编译器要把这些 pass 扩展到几十种算子和多种硬件。

## 43.12 故障模式

**融合改变数值**。把 `x * a + b` 融成 FMA 可能改变舍入；把 softmax 的减最大值和 exp 融合，可能改变溢出边界。防护是保留参考路径，按 dtype 设容差，并对边界值做测试。

**共享中间结果被错误融合**。一个 norm 的输出被两个 matmul 使用，融合进其中一个会让另一个读到未定义的中间量。防护是检查消费者数量，`fuse_rmsnorm_matmul` 的单消费者条件就是例子。

**内存复用读到脏数据**。两个张量生命周期不重叠，但复用同一块 buffer 时没有同步；GPU 异步执行下，前一个 kernel 可能还没写完。防护是把复用点绑定到 stream 顺序，或在需要时插入 event。

**量化 pass 漏掉反量化**。权重存成 FP4，GEMM 却按 BF16 读，结果完全错误。防护是 dtype 传播和 kernel 断言，启动时校验 scale 布局。

**编译缓存过期**。模型或 kernel 版本变了，缓存还指向旧执行计划。防护是把 manifest fingerprint 写进缓存 key，版本变化时整体失效。

**CUDA Graph 捕获了动态分支**。捕获时走的分支被固化，运行时 batch 变了就读错数据。防护是按 shape 捕获多张图，或在 kernel 里用 mask 处理动态部分。

**autotune 拖慢首请求**。首次遇到新 shape 时跑几十个候选，TTFT 飙升。防护是离线预调、后台调优和默认 fallback。

## 小结

图编译器把模型从“逐算子调用”变成“可分析的执行计划”：SSA IR 让依赖和生命周期可见，常量折叠和死代码消除做安全的简化，融合减少 launch 和 HBM 往返，内存规划压低峰值，layout/量化/kernel 选择决定硬件利用率，CUDA Graph 在更低层省 launch。优化必须和参考实现对照，融合要考虑消费者数量和寄存器压力。DeepInfer 更偏“专用引擎加手写 kernel”，通用编译器更偏“多模型平台”；理解两者的边界，才能判断一个优化该放在哪一层。mini 用 11 节点的图演示了完整链路，但没有复制生产编译器的规模。下一章进入训练侧推理：RL rollout 引擎如何把同一套 kernel、KV 和并行机制放进训练循环。

## 练习

1. 画出 `build_mlp_graph()` 的 11 个节点和依赖边，标出哪两个模式可以融合，为什么另外两个 norm 不能融合。
2. 解释常量折叠和死代码消除的先后顺序。如果先做 DCE 再做常量折叠，会漏掉什么？
3. 一个融合 kernel 让 launch 从 20 降到 10，但 occupancy 从 50% 降到 25%，端到端可能变快还是变慢？给出判断条件。
4. 用 `plan_memory()` 的输出，找出哪两个张量的生命周期不重叠、复用了同一个 slot；再构造一个反例，让贪心分配不是最优。
5. 设计一个量化 pass 的测试：FP8 权重、FP16 激活、FP32 累加，如何验证“反量化融进 GEMM”没有改变结果？
6. 比较 TorchInductor、TensorRT 和 TileLang 的分工：谁负责图、谁负责 kernel、谁负责运行时？各写一句话。
