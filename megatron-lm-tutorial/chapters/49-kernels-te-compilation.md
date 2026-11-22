# 第 49 章 Kernel、Transformer Engine 与编译栈

第 14 章说“MFU 是性能的语言”，但没有回答一个关键问题：**同样的并行配置，
为什么两个实现的 MFU 可能差一倍？** 答案通常不在并行算法，而在 kernel：
融合、精度、通信重叠、图捕获和内存布局。这一章从算子层解释 Megatron 的
性能来源，并给出“哪些优化值得做、怎样验证”的清单。

本章的 API 名称以 Megatron Core 0.20.x / Transformer Engine 的公开接口为
基线；版本漂移很快，落地前用 `--help` 和源码核对。

## 49.1 性能差距从哪里来

一个 Transformer block 的计算可以分成两类：

| 类别 | 典型算子 | 受限于 | 优化手段 |
|---|---|---|---|
| 大 GEMM | QKV/O/MLP/专家 | 算力 | tensor core、FP8/FP4、grouped GEMM |
| 逐元素/规约 | LayerNorm、softmax、SwiGLU、dropout、residual | 显存带宽 | kernel fusion、减少中间张量 |
| 通信 | all-reduce、reduce-scatter、all-gather、p2p、all-to-all | 带宽/延迟 | 分桶、异步、分层、与计算重叠 |
| 调度 | micro-batch 循环、p2p 顺序、图启动 | CPU/启动开销 | CUDA Graph、编译、persistent kernel |

朴素 PyTorch 实现通常在大 GEMM 上接近峰值，但在逐元素/规约上被带宽限制，
在通信上完全串行。生产 Megatron 的 MFU 优势，很大一部分来自：

1. **融合**：把 LayerNorm + residual + dropout 合成一个 kernel，少读写显存；
2. **低精度**：FP8/FP4 tensor core，提升峰值算力并降低带宽；
3. **重叠**：让通信藏在计算后面；
4. **图**：减少 kernel 启动和 Python 开销；
5. **布局**：让 GEMM 的 K 维连续、避免 transpose/contiguous。

## 49.2 Transformer Engine：不只是“更快的 Linear”

Transformer Engine（TE）提供的不只是 FP8 GEMM，而是一套**模块替换 + 精度
管理**：

```python
import transformer_engine.pytorch as te

# 替换 nn.Linear / nn.LayerNorm
qkv = te.Linear(hidden, 3 * hidden, bias=False)
norm = te.LayerNorm(hidden)
# 融合 LayerNorm + Linear（常见于 QKV/MLP 入口）
fc = te.LayerNormLinear(hidden, ffn, bias=False)
# 融合 attention（FlashAttention 后端）
attn = te.DotProductAttention(num_attention_heads=..., kv_channels=...)
```

TE 的关键契约：

- **模块内部维护 FP8 的 scale/amax 状态**，不是简单地把权重 cast 成 FP8；
- **forward/backward 的 cast 点由 recipe 决定**，同一层在不同 recipe 下
  数值不同；
- **参数可以是 bf16/fp32，也可以是 FP8**（`fp8_model_init` / FP8 参数），
  后者省显存但需要更谨慎的初始化/优化器处理；
- **融合 kernel 的可用性依赖 GPU 架构、CUDA/cuDNN 版本和 shape**；不可用
  时会回退，性能模型随之变化。

Megatron 通过 `transformer_config.py` 的开关选择 TE 路径（例如
`fp8`、`fp8_recipe`、`fp8_param`、`fp8_model_init`、`tp_comm_overlap`）。
这些开关是**全局契约**：打开后，checkpoint 的 dtype、优化器状态、推理转换
都要跟着变。

### 49.2.1 什么时候不该用 TE

- 目标硬件不支持（老 GPU/非 NVIDIA）；
- 需要 bitwise 复现某个非 TE 参考；
- 模型结构有 TE 未覆盖的算子（自定义 attention/MoE）；
- 调试数值问题时，先用非 TE 路径建立基线，再逐层替换。

## 49.3 FP8/FP4 的 recipe：格式只是第一步

第 12 章讲了 E4M3/E5M2、scale 和数值风险，12.2.3 给出了 delayed/current/
blockwise/MXFP8/NVFP4 的 recipe 对照表；这里不重复，只补“recipe 如何影响
系统”：

- delayed scaling 需要 amax history 与跨 step 状态，恢复/换并行度时要一并
  保存；current scaling 每步多一次 amax 规约，精度更稳但可能被同步拖慢；
- blockwise/MXFP8 的 scale 元数据会改变 kernel 形状与带宽，GEMM 需要专门
  布局；NVFP4 的二级 scale 与反量化路径更复杂，回退路径必须保留。

工程要点：

1. **amax history 是状态**：checkpoint 要保存，否则恢复后 scale 跳变；
2. **FP8 不是“打开就快”**：小 GEMM、非对齐 shape、频繁 cast 可能更慢；
3. **选择性 FP32**：LayerNorm、softmax、loss、router 等敏感路径保留高精度；
4. **回退路径**：FP8/FP4 出问题时能一键回 bf16，且 checkpoint 可转换；
5. **评测要同口径**：FP8 的 loss 曲线与 bf16 不完全可比，要看下游指标。

Megatron 0.20 附近的 `fp8_recipe` 选择、`fp8_param`、selective FP32 等
开关，具体名称/支持矩阵以当前源码为准；本书不把某个 recipe 写成“默认正确”。

### 49.3.1 NVFP4 训练：从格式到验收

NVFP4 不是“把 bf16 除以 16”。以常见的 E2M1 + 两级 scale 为例：

```text
value: 4 bit（E2M1，可表示 0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6）
block: 16 个 value 共享一个 FP8(E4M3) block scale
tensor: 一个 FP32 per-tensor scale 再乘到所有 block 上
```

量化/反量化可写成：

```text
s_block = amax(block) / 6                  # E2M1 的最大可表示值
q = round_to_e2m1(x / (s_block * s_tensor))
x_hat = q * s_block * s_tensor
```

元数据开销约为 `4 + 8/16 = 4.5 bit/value`（再加一个可忽略的 tensor scale），
相对 bf16 的 16 bit 理论上是 3.5× 权重压缩；但激活、梯度、优化器状态是否
也走 FP4，决定了真实显存收益。**不要把权重压缩比当成端到端显存比。**

一个可执行的影子实验协议：

1. **固定基线**：同一初始化、同一数据顺序、同一 seed，先跑 bf16 2k–10k 步，
   保存 loss/grad-norm/update-norm 曲线和 3 个下游评测；
2. **只改精度**：保持 TP/PP/DP、batch、LR 不变，切 FP8，再切 FP4；不要同时
   调 LR 或并行度，否则无法归因；
3. **记录数值状态**：每层 amax、block scale 分布、被 clamp 的比例、NaN/Inf
   计数；MoE 还要记录 router logits 的量化误差与专家负载；
4. **对齐口径**：比较相同 token 数（不是相同 step）、相同评测版本；FP4 的
   loss 曲线允许有小偏差，但不能出现拐点或持续发散；
5. **设回退门**：任一条件触发就回 bf16/FP8——2k 步 loss 差 > 0.02、grad-norm
   比值超出 0.9–1.1、下游评测超出置信区间、出现不可恢复的 spike。

敏感性排序（经验，需按模型验证）：

```text
最敏感：LayerNorm/RMSNorm、softmax、loss/CE、router logits、残差累加
次敏感：attention QK^T、MLP gate/up 的第一层
相对耐受：大 GEMM 的权重与激活、embedding 查表
```

因此生产 recipe 通常是“FP4 GEMM + FP32/FP8 敏感路径 + selective FP32”，
而不是全模型 FP4。checkpoint 要同时保存精度 recipe 与 amax/scale 状态；
回退时先把 FP4 参数反量化回 bf16，再加载到 bf16 模型，不能直接复用 FP4
kernel 的布局。若使用 `fp8_model_init`/FP4 参数，确认 fp32 master weights 与
优化器状态仍在，否则回退会丢精度。

## 49.4 融合 kernel：省的是带宽，不只是 FLOPs

以 LayerNorm 为例，朴素实现：

```text
mean = x.mean(-1)        # 读 x
var = ((x-mean)**2).mean(-1)  # 再读 x
y = (x-mean)/sqrt(var+eps) * w + b  # 再读 x，写 y
```

至少 3 次读 + 1 次写（4 趟）。融合 kernel 一次读入、片上计算、一次写出
（2 趟），**总访存降到约 1/2**；如果只统计读带宽（3 读 → 1 读），才是约
1/3。对 `h=8192`、每层两个 norm 的模型，这不是小数。

常见融合：

| 融合 | 省什么 | 备注 |
|---|---|---|
| LayerNorm/RMSNorm + residual | 一次读写 | pre-norm 结构常见 |
| bias + activation（SwiGLU/GELU） | 中间张量 | gate/up 可融合 |
| attention softmax + dropout + mask | score 矩阵读写 | FlashAttention 的核心 |
| cross-entropy + log-softmax | logits 中间量 | 词表并行时更复杂 |
| rotary embedding | 多次 reshape/读写 | 与 QKV 布局耦合 |
| grouped GEMM | kernel 启动 + 小 batch | MoE 必需 |
| optimizer step | 多趟读写 | fused AdamW/Muon |

**如何判断是否值得**：用 Nsight Compute 看 kernel 是 compute-bound 还是
memory-bound。memory-bound 的逐元素 kernel 融合收益最大；已经 compute-bound
的大 GEMM，融合收益有限。

## 49.5 CUDA Graph：消灭启动开销

训练一个 step 可能有几百到几千个 kernel。Python + CUDA 启动开销在小模型/
小 batch 下占比很高。CUDA Graph 把整个 step 的 kernel 序列捕获成一张图，
后续 replay 只付一次启动成本。

```python
# 示意：捕获一个静态 shape 的 step
g = torch.cuda.CUDAGraph()
with torch.cuda.graph(g):
    loss = model(input_ids, labels)
# 之后：copy 输入 -> g.replay() -> 读 loss
```

约束：

- **shape/地址静态**：输入 buffer 要预分配，不能每步新建张量；
- **控制流静态**：动态路由、变长 sequence、数据相关分支会导致捕获失败；
- **RNG/通信**：dropout、NCCL 调用需要图安全/捕获支持；
- **MoE**：专家路由是数据相关的，通常只能捕获 dense 部分，或用专门的
  graph-safe dispatcher；
- **调试困难**：图里的 kernel 不能单步，出错要看 capture 前的 eager 路径。

Megatron 的 CUDA Graph 支持按 scope（full iteration / attention / MLP 等）
开启，具体参数以 `--cuda-graph-impl`/`--cuda-graph-scope` 一类开关为准。
建议顺序：先 eager 跑对 → 再捕获 attention/MLP → 最后整步；每步都做数值
对比（graph replay 与 eager 的 logits/loss）。

## 49.6 torch.compile：图优化与它的边界

`torch.compile` 用 TorchDynamo 抓图、Inductor 生成 kernel，能自动融合逐元素
算子、减少 Python 开销。它和 Megatron 的关系是**互补而非替代**：

- 它不懂 TP/PP/EP 的集合通信语义，分布式 wrapper 可能造成 graph break；
- 动态 shape（变长序列、MoE 路由）会触发重新编译；
- 自定义 autograd Function（mini 的并行层）需要能被 trace；
- 编译时间/缓存管理在千卡任务上是运维问题。

实用建议：

```python
model = torch.compile(model, dynamic=False)  # 先固定 shape
# 或只编译热点 block
block = torch.compile(block, fullgraph=False)
```

验收：编译前后 logits/loss 一致（容差内）、graph break 数量、step time、
显存峰值。不要用“编译成功”代替“数值正确”。

## 49.7 通信重叠：把等待变成计算

第 14 章给了重叠效率模型。这里补实现层：

### 49.7.1 TP 的异步 all-reduce

Row Parallel 的 all-reduce 在关键路径上。做法：

```text
GEMM -> all_reduce(async) -> 下一个 GEMM（不依赖结果）-> wait -> 使用
```

前提是**下一个 GEMM 不依赖 all-reduce 的输出**。常见是把两个 micro-batch
的 GEMM/通信交错；单 micro-batch 内很难重叠。

生产 Megatron 的 `--tp-comm-overlap` / userbuffers 把 all-reduce 与 GEMM
融合/流水化，具体支持依赖 TE/硬件。

### 49.7.2 PP 的 p2p 重叠

1F1B 中，stage 在等上游激活时可以算下一个 micro-batch 的 forward；在等下游
梯度时可以算下一个 backward。调度器要保证：

- p2p 的 send/recv 与本地计算用不同 stream；
- 依赖关系正确（不能提前用未到的激活）；
- 失败时能定位是哪一对 rank/哪个 tag。

### 49.7.3 EP 的 all-to-all 重叠

把 token 分块：

```text
dispatch(chunk 0) -> expert GEMM(chunk 0) 同时 dispatch(chunk 1)
                  -> combine(chunk 0) 同时 GEMM(chunk 1)
```

难点是动态 shape、空专家、索引一致性。DeepEP 一类库提供 normal/low-latency
两种模式；训练用大 batch 的 normal，decode 用小 batch 的 low-latency。

### 49.7.4 重叠的验收

```text
[ ] 数值：重叠前后 logits/loss 一致（容差内）
[ ] 正确性：集合通信顺序在所有 rank 一致，无 hang
[ ] 性能：Nsight 上通信与 GEMM 时间窗口确实重叠
[ ] 回退：开关关闭后行为与基线一致
```

## 49.8 Zero-bubble 与调度器

第 8 章给了 ZB1P/ZB2P 的气泡公式。实现层的关键是：

- 把 backward 拆成 `B_input` 与 `B_weight`；
- `B_weight` 可以延迟，但要额外存激活/权重梯度；
- 调度器要保证 `B_input` 尽快传给上游，否则上游会等；
- 与 interleaved/VPP、激活重算、p2p 重叠组合时，依赖图更复杂；
- Megatron 通过 `get_forward_backward_func` 选择 schedule，具体支持矩阵
  以 `pipeline_parallel/schedules.py` 为准。

判断是否值得，用第 8 章的公式先算气泡占比；如果 `p` 很小或 `m` 很大，
收益有限，不如把工程时间花在数据/通信上。

## 49.9 一个 Triton 练习：RMSNorm

下面是一个教学版 RMSNorm kernel（需要 GPU + Triton；mini 的 CPU 环境不
执行它）。它展示“融合 + 一次读写”的基本形态：

```python
import triton
import triton.language as tl

@triton.jit
def rmsnorm_kernel(X, W, Y, stride, N, eps: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    mask = cols < N
    x = tl.load(X + row * stride + cols, mask=mask, other=0.0).to(tl.float32)
    var = tl.sum(x * x, axis=0) / N
    rstd = 1.0 / tl.sqrt(var + eps)
    w = tl.load(W + cols, mask=mask, other=0.0)
    y = x * rstd * w
    tl.store(Y + row * stride + cols, y, mask=mask)
```

要点：

- 一个 program 处理一行，`BLOCK` 覆盖 `N`（或做循环）；
- 用 fp32 累加，输出 cast 回原 dtype；
- `mask` 处理非 2 的幂的 hidden；
- 真实实现还要考虑 vectorized load、`tl.max_contiguous`、多行/多 block、
  反向 kernel。

练习：把它与 `torch.nn.functional.rms_norm` 对比（GPU 上），测带宽和误差；
再把它接进 mini 的 `TransformerBlock`（需要 GPU），验证 loss 不变。

## 49.10 性能验收：一张可执行的表

| 优化 | 预期收益 | 风险 | 验收 |
|---|---|---|---|
| TE 模块替换 | 中–高 | 数值/兼容性 | logits 对齐、MFU |
| FP8 delayed | 高 | scale/溢出 | loss 曲线、下游评测 |
| FP8 blockwise/NVFP4 | 高 | 收敛/硬件 | 影子实验、回退 |
| 融合 norm/activation | 中 | 低 | kernel 时间、loss |
| CUDA Graph | 中（小模型高） | 动态 shape | replay vs eager |
| torch.compile | 中 | graph break | step time、数值 |
| TP 通信重叠 | 中–高 | 死锁/数值 | Nsight、等价性 |
| PP/EP 重叠 | 高（大规模） | 调度复杂 | 端到端吞吐、故障率 |
| Zero-bubble | 中（p 大/m 小） | 显存/调度 | 气泡、显存、loss |

顺序建议：**先 profile，再优化**；每次只改一项；每项都保留回退开关；
所有优化都要过数值等价性和故障注入。

## 49.11 本章小结

- MFU 差距主要来自融合、低精度、重叠和图，而不是并行维度本身。
- TE 是“模块 + 精度管理”，不是简单的 FP8 Linear；recipe 改变数值语义。
- 融合的收益在带宽，先判断 kernel 是 compute-bound 还是 memory-bound。
- CUDA Graph/torch.compile 有静态 shape 和动态控制流的边界，必须做
  replay/eager 数值对比。
- 通信重叠的前提是独立工作、不同流、无隐藏同步；用 Nsight 验证，不靠猜。
- zero-bubble 是调度优化，收益取决于 `p/m` 和显存余量。
- 任何 kernel 优化都要有回退路径和等价性测试。

下一章把“状态显存”这一条线单独拉出来：分布式优化器与 FSDP 的内部结构、
检查点布局、失败模式和选型。
