# 第 37 章 硬件、拓扑与通信工程

> **第 VI 部分导读（37–42）**
> 本部分补齐落地专题：硬件/拓扑/通信、数据工程、后训练、推理系统、评测与
> 故障复盘。前置是 14 与 29–33；读完应能做一次 NCCL microbenchmark、设计
> 数据配方、估算 TTFT/TPOT、写一份可执行的故障复盘。可按角色跳读：
> 平台看 37/38/40/41/42，算法看 38/39/41。

## 37.1 为什么并行工程师必须懂硬件

并行策略的每一条规则，背后都是硬件的数字：

- 为什么 TP 通常不超过 8？因为一个 NVLink 域通常就是 8 张 GPU；
- 为什么 PP 可以跨节点？因为每个 micro-batch 只传一次激活，通信量远小于 TP 的每层 all-reduce；
- 为什么 FP8 能提速？因为 Tensor Core 的 FP8 吞吐是 BF16 的两倍；
- 为什么 MoE 的 all-to-all 难？因为它是不规则、动态、对尾延迟敏感的小消息通信。

这一章给出“够用的硬件模型”：不追求芯片微架构细节，而是让你能判断一个并行方案在给定集群上是否合理。

## 37.2 GPU 的执行模型与 roofline

一个 NVIDIA GPU 由若干 SM（Streaming Multiprocessor）组成，每个 SM 包含：

- CUDA Core：通用 FP32/INT32 运算；
- Tensor Core：矩阵乘加（MMA），训练的主力；
- 寄存器、共享内存/L1；
- 访存单元，连接 HBM。

对一个算子，最重要的两个数字是：

```text
计算量 FLOPs
访存量 Bytes
算术强度 I = FLOPs / Bytes
```

Roofline 模型：

```text
可达性能 = min(峰值算力, I × 峰值带宽)
临界算术强度 I* = 峰值算力 / 峰值带宽
```

- `I < I*`：带宽受限（LayerNorm、softmax、逐元素、KV cache 读写）；
- `I > I*`：计算受限（大 GEMM、大 batch 的 attention）。

以 H100 SXM 为例（数量级，具体以数据手册为准）：

```text
BF16 dense 峰值 ≈ 989 TFLOP/s
HBM3 带宽      ≈ 3.35 TB/s
I* ≈ 989e12 / 3.35e12 ≈ 295 FLOP/Byte
```

一个 `M×K×N` 的 GEMM，算术强度约为 `2MNK / (2(MK+KN+MN))`。当 `M=N=K=4096` 时，`I ≈ 1365`，远高于临界值，属于计算受限；当 `M=1`（解码阶段的 GEMV）时，`I ≈ 1`，严重带宽受限。这解释了两个现象：

1. 训练喜欢大 batch、大矩阵；
2. 推理 decode 阶段是“内存带宽游戏”，batch 越大越划算。

> 常见错误：把 H100 的 BF16 dense 峰值写成 495 TFLOP/s。495 是 TF32 Tensor Core 的 dense 数字；BF16/FP16 dense 约 989 TFLOP/s，带稀疏性约 1979 TFLOP/s。做 MFU 估算时必须用 **dense** 峰值，并且确认精度。

## 37.3 GPU 代际与关键数字

下表是 **数量级参考**，不是精确规格；不同封装（SXM/PCIe/NVL）差异很大。

| GPU | 显存 | HBM 带宽 | BF16 dense | FP8 dense | 节点内互联 |
|---|---|---:|---:|---:|---|
| A100 SXM | 80 GB HBM2e | ~2.0 TB/s | ~312 TFLOP/s | — | NVLink 3，600 GB/s |
| H100 SXM | 80 GB HBM3 | ~3.35 TB/s | ~989 TFLOP/s | ~1979 TFLOP/s | NVLink 4，900 GB/s |
| H200 | 141 GB HBM3e | ~4.8 TB/s | ~989 TFLOP/s | ~1979 TFLOP/s | NVLink 4，900 GB/s |
| B200 | 192 GB HBM3e | ~8 TB/s | ~2.2 PFLOP/s 级 | ~4.5 PFLOP/s 级 | NVLink 5 |
| GB300 NVL72 | 288 GB HBM3e/GPU | ~8 TB/s/GPU | ~2.2–2.5 PFLOP/s 级/GPU | ~4.5–5 PFLOP/s 级/GPU | NVLink 5 域，72 GPU |

使用这张表时注意：

- **dense vs sparse**：厂商标称常同时给两个数字，MFU 用 dense；
- **精度**：FP8/FP4 的峰值可能是 BF16 的 2×/4×，但收敛和可用性另说；
- **封装**：PCIe 版本的峰值和互联远低于 SXM/NVL；
- **功耗/散热**：峰值算力只在足够供电和散热下可达，降频会直接吃掉 MFU。

## 37.4 节点内互联：NVLink 与 NVSwitch

一个典型 8-GPU 节点内：

```text
GPU0 ── NVLink ── GPU1
  \                 /
   ── NVSwitch ────
  /                 \
GPU2 ── NVLink ── GPU3
...
```

关键事实：

- NVLink 的带宽远高于 PCIe：H100 的 900 GB/s 是**双向合计**（约 450 GB/s
  每方向），PCIe Gen5 x16 约 64 GB/s 每方向（双向 128 GB/s）。按同口径比较
  约 7 倍，而不是 14 倍——这是硬件章节最常见的单位错误；
- NVSwitch 让 8 张卡两两全带宽互联，而不是只能环形；
- TP 的 all-reduce 每层都发生，必须留在这个域内；
- EP 的 all-to-all 也强烈建议留在这个域内。

GB300 NVL72 把 NVLink 域扩大到 72 张 GPU（36 个 Grace CPU），这改变了“TP/EP 不能超过 8”的经验：

- 更大的 NVLink 域允许更大的 TP/EP；
- 但故障域、供电、散热、调度复杂度同步上升；
- 软件是否能把 72 卡当成一个拓扑单元来调度，是另一回事。

## 37.5 节点间互联：InfiniBand、RoCE 与拓扑

跨节点通信通常走 InfiniBand（IB）或 RoCE：

```text
节点 ── leaf 交换机 ── spine 交换机 ── leaf ── 节点
```

常见概念：

- **fat-tree / rail-optimized**：每张 GPU 对应一条独立的 IB rail，避免拥塞；
- **oversubscription**：leaf 上行带宽小于下行总和，跨 leaf 通信会争用；
- **leaf/spine 跳数**：跨 spine 的延迟和带宽通常更差；
- **RDMA / GPUDirect**：网卡直接读写 GPU 显存，绕过 CPU；
- **NCCL 拓扑感知**：自动选择 ring/tree、IB HCA、NVLink 路径。

并行映射的经验：

| 维度 | 建议位置 | 原因 |
|---|---|---|
| TP | 节点内 NVLink | 每层多次 all-reduce，延迟敏感 |
| EP | 节点内或 NVL72 域 | all-to-all 对带宽/尾延迟敏感 |
| PP | 跨节点 | 每 micro-batch 一次 p2p，量小 |
| DP | 跨节点 | 梯度 all-reduce 可分层/重叠 |
| CP | 视实现 | ring 通信可跨节点，all-gather 较重 |

## 37.6 NCCL：集合通信的实际行为

NCCL 是 NVIDIA GPU 上的集合通信库。理解它比背 API 更重要。

### 37.6.1 常用集合操作

| 操作 | 语义 | 典型用途 |
|---|---|---|
| all-reduce | 所有 rank 求和/取 max，结果广播 | DP 梯度、TP 行并行、梯度范数 |
| reduce-scatter | 求和后按 rank 切分 | ZeRO-2/3、SP |
| all-gather | 收集所有 rank 的分片 | SP、CP、ZeRO-3 参数 |
| broadcast | 一个 rank 发给所有 | 参数同步、采样 token |
| all-to-all | 每个 rank 向每个 rank 发不同数据 | EP/MoE |
| send/recv | 点对点 | PP |

### 37.6.2 Ring all-reduce 的通信量

对 `N` 个 rank、消息大小 `S` 字节：

```text
ring all-reduce ≈ reduce-scatter + all-gather
每 rank 发送量 ≈ 2S(N-1)/N 字节
```

因此 DP 梯度 all-reduce 的每卡通信量约为 `2N_params × 2 bytes × (d-1)/d`（bf16）。当 `d` 很大时，通信量趋近 `4N_params` 字节，和 DP size 无关——这就是为什么“加更多 DP 卡”不会让梯度通信按比例变便宜。

### 37.6.3 算法选择

NCCL 会根据消息大小/拓扑选择：

- **Ring**：大消息、带宽优先；
- **Tree**：小消息、延迟优先；
- **CollNet/NVLS**：利用 NVSwitch 做 in-network reduction；
- **PAT**：all-to-all 的拓扑感知算法。

不要手工绑定算法，除非你已经用 `NCCL_DEBUG=INFO` 和 microbenchmark 验证过。

### 37.6.4 关键环境变量

| 变量 | 用途 |
|---|---|
| `NCCL_DEBUG=INFO` | 打印拓扑、算法、连接 |
| `NCCL_DEBUG_SUBSYS=ALL` | 更细的调试 |
| `NCCL_ALGO=Ring/Tree` | 强制算法（谨慎） |
| `NCCL_PROTO=Simple/LL/LL128` | 协议选择 |
| `NCCL_IB_HCA` | 指定 IB 网卡 |
| `NCCL_SOCKET_IFNAME` | 指定 socket 网卡 |
| `NCCL_NET_GDR_LEVEL` | GPUDirect RDMA 级别 |
| `NCCL_IB_TIMEOUT` | IB 超时 |

### 37.6.5 带宽口径：algbw 与 busbw

NCCL 测试常给两个数字：

```text
algbw = 消息大小 / 时间
busbw = algbw × 修正系数
```

对 all-reduce，`busbw = algbw × 2(N-1)/N`。比较不同并行度/消息大小时，用 busbw 更公平；对外行报告“链路带宽”时，要说明口径。

## 37.7 各并行维度的通信量账本

设 `b` micro-batch，`s` 序列，`h` hidden，`N` 参数量，`d/t/p/c/e` 分别为 DP/TP/PP/CP/EP size，bf16 每元素 2 字节。

| 维度 | 通信 | 每步量级 | 频率 |
|---|---|---|---|
| DP | 梯度 all-reduce | `4N(d-1)/d` 字节/卡 | 每 optimizer step |
| TP | Row 前向 all-reduce | `4bsh(t-1)/t` 字节/层/卡 | 每层每次前向 |
| TP | Column 反向 grad_input all-reduce | `4bsh(t-1)/t` 字节/层/卡 | 每层每次反向 |
| SP | 序列 all-gather/reduce-scatter | 与 TP 相近 | 每层 |
| PP | 激活 + 梯度 p2p | `4bsh` 字节/边界/micro-batch（前向 2bsh + 反向 2bsh） | 每 micro-batch |
| CP | KV all-gather（教学版） | `4bsh(c-1)/c` 字节/层/卡 | 每层 |
| CP | ring attention | 每步 `4bsh/c`，每层合计 ≈`4bsh` | 每层 c-1 步 |
| CP | Ulysses（QKV+O） | `8bsh(c-1)/c²` 字节/层/卡 | 每层两次 all-to-all |
| EP | all-to-all dispatch/combine | 每卡 `O(bsh·k)`；per-peer `O(bsh·k/e)` | 每 MoE 层 |

表里的“字节”默认 bf16（每元素 2 字节）；`4bsh` 即“4 字节 × b×s×h”。
TP/CP 行给的是**每卡发送量**，EP 行同时标了每卡与 per-peer，避免把聚合
流量除以单卡带宽导致 `e` 倍误差（见 10.6）。

几个结论：

1. **TP 的通信频率最高**，所以对延迟最敏感；
2. **DP 的通信量与参数量成正比**，大模型必须用 PP/TP/EP 分担；
3. **PP 的通信量最小**，但气泡和 p2p 延迟是主要成本；
4. **EP 的通信不规则**，平均带宽不是唯一指标，尾延迟更致命；
5. **CP 的通信随序列长度增长**，长上下文必须做通信/计算重叠。

ring attention 的账本容易误读：每个 rank 每步只发本地 KV 块
`2·b·(s/c)·h·bpe`（bf16 即 `4bsh/c`），但要走 `c-1` 步，合计仍约 `4bsh`。
它相对 all-gather 的优势不是“总字节更少”，而是**单条消息更小、KV 显存只留
本地块、通信可与当前块 attention 重叠**。如果只看总字节，会得出“ring 没有
省通信”的错误结论；如果只看单步，又会低估总时间。

Ulysses 的总字节约为 ring 的 `2/c`（每卡 `8bsh(c-1)/c²` vs ring 的
`4bsh(c-1)/c`），但它每层有**两次 all-to-all 的固定延迟**，且要求 `s`/head
可整除、每卡持有全序列视图。账本上省通信不等于端到端更快，选型见 9.2.4。

## 37.8 用 microbenchmark 验证拓扑

不要只信配置，要测。最小实验：

```bash
# NCCL 自带测试（如果集群有）
all_reduce_perf -b 8 -e 8G -f 2 -g 8
all_gather_perf -b 8 -e 8G -f 2 -g 8
alltoall_perf -b 8 -e 8G -f 2 -g 8
```

没有 NCCL tests 时，可以用 PyTorch 写一个最小 benchmark：

```python
import torch, torch.distributed as dist

dist.init_process_group("nccl")
rank, world = dist.get_rank(), dist.get_world_size()
x = torch.randn(64 * 1024 * 1024 // 4, device="cuda")  # 64 MB
for _ in range(10):
    dist.all_reduce(x)
torch.cuda.synchronize()
```

记录：

- 小消息（1 KB–1 MB）的延迟；
- 大消息（64 MB–1 GB）的带宽；
- 跨 leaf/spine 的差异；
- NVLink 与 IB 的差异；
- 不同 NCCL_ALGO/PROTO 的差异。

这些数字决定 TP/EP 能开多大、PP 是否值得跨节点。

### 37.8.1 通信重叠：userbuffers、NVLS 与流编排

“通信与计算重叠”不是把 `all_reduce` 改成异步就完成了。生产实现有三条主要
路径：

| 技术 | 作用 | 代价/限制 |
|---|---|---|
| **Userbuffers** | 在 GPU 显存里划一块通信 buffer，kernel 直接读写，减少拷贝和 launch 开销 | 需要 kernel 配合；buffer 生命周期/同步复杂 |
| **NVLS/NVSwitch reduction** | 交换机内做归约，减少端点流量 | 依赖 NVSwitch/NVL72；算法受限 |
| **异步流 + 双缓冲** | 通信在独立 stream，计算用另一块 buffer | 需要事件/依赖管理；buffer 复用易错 |
| **融合 kernel** | GEMM epilogue 里做 reduce-scatter/all-gather | 与硬件/精度强绑定 |
| **分层通信** | 节点内 NVLink 先归约，再跨节点 IB | 需要拓扑感知的 group |

重叠的正确性条件：

1. 通信的输入 buffer 在通信完成前不能被覆盖（双缓冲/事件）；
2. 依赖通信结果的 kernel 必须 wait，不能“为了重叠”提前读；
3. 反向的梯度累积顺序不能因异步而改变语义（除非接受数值差异）；
4. 多个 stream 的优先级/抢占会影响尾延迟，需要实测。

一个可复现的重叠实验（第 14.8.1 节的扩展）：

```text
T_serial  = T_gemm + T_comm
T_overlap = 实测（通信与 GEMM 同时发起，记录总时间）
overlap_eff = (T_serial - T_overlap) / T_comm
```

如果 `overlap_eff` 接近 0，先查：

- 通信是否真的在独立 stream；
- 是否在 GEMM 前就 `wait` 了；
- buffer 是否被复用导致隐式同步；
- NCCL 是否因为小消息退化成低效算法。

**TP 的重叠**最难：每层都有 all-reduce，且下一层 GEMM 依赖它。常见做法是把
all-reduce 与下一个 micro-batch 的 GEMM 重叠（需要至少两个 in-flight
micro-batch），而不是与同一层的 GEMM 重叠。**EP 的重叠**则是 all-to-all 与
专家 GEMM 重叠，需要把 token 分块流水线化。**PP 的重叠**是 p2p 与相邻
stage 的计算重叠，依赖 warmup/1F1B 的调度。

这些技术在小规模上收益有限；在公开的千卡级案例中，重叠可影响约 20%–40%
的端到端性能（具体取决于模型、拓扑、batch 与实现，不能当作 SLA）。但它们
也会引入“能跑但数值偶发错”的 bug，所以必须先有等价性测试，再做重叠。

## 37.9 三个集群的并行映射示例

### 37.9.1 8×H100 单节点

```text
TP=8, PP=1, DP=1
```

适合 7B–13B 模型的全参数训练。TP 在 NVLink 内，通信便宜；没有 PP 气泡；DP=1 意味着没有梯度 all-reduce。

如果显存不够：

```text
TP=4, PP=2, DP=1
TP=4, PP=1, DP=2 + ZeRO-1
```

### 37.9.2 64×H100（8 节点×8 卡）

```text
TP=8（节点内）
PP=4（跨 4 节点）
DP=2（剩余）
```

适合 70B 级模型。TP 不跨 IB；PP 的 p2p 跨节点但量小；DP 的梯度 all-reduce 跨节点，需要分层/重叠。

### 37.9.3 GB300 NVL72

```text
TP=8/16, EP=8/16, PP=2–4, DP=剩余
```

NVLink 域扩大到 72 卡后，TP/EP 可以更大；但调度器必须能把 72 卡作为一个拓扑单元分配，否则优势会被碎片化吃掉。

## 37.10 mini 的通信抽象与生产差距

mini 的 `parallel/comm.py` 暴露的 API 与 NCCL 语义对齐：

- `all_reduce`、`all_gather`、`reduce_scatter_tensor`、`broadcast`、`send/recv/isend`、`barrier`；
- portable 后端用 Queue 模拟，torch 后端直接调 `torch.distributed`；
- 没有拓扑感知、没有算法选择、没有通信重叠。

这足以验证“通信顺序/依赖/数值”是否正确，但不能用来做性能结论。把 mini 搬到真实集群时，需要替换的是：

1. `comm.py` 的 portable 后端 → NCCL；
2. 同步 all-reduce → bucket/异步/重叠；
3. 显式 all-gather → 融合的 reduce-scatter/all-gather；
4. 单机 Queue → 拓扑感知的 rank 布局。

## 37.11 本章小结

- 并行策略由算力、带宽、互联拓扑共同决定，不能只看参数量。
- BF16 dense 峰值要按代际/封装查表；H100 SXM 约 989 TFLOP/s，不是 495。
- TP/EP 留在 NVLink 域，PP/DP 可以跨节点；EP 的尾延迟比平均带宽更重要。
- NCCL 的 ring all-reduce 通信量是 `2S(N-1)/N`，DP 越大越接近常数。
- 用 microbenchmark 测延迟/带宽，再决定并行映射。
- mini 只验证通信语义，不验证性能。

下一章进入数据工程：模型能力的上限，往往由数据而不是并行度决定。
