# 第 28 章 性能剖析与调优实验

## 28.1 先定义指标

不同目标对应不同指标：

| 目标 | 指标 |
|---|---|
| 最快训练完成 | time-to-train、tokens/s |
| 最高硬件效率 | MFU、TFLOP/s/GPU |
| 最低成本 | $/1M tokens、GPU-hours |
| 最低延迟推理 | TTFT、TPOT、p99 |
| 最大吞吐推理 | tokens/s/GPU、batch throughput |

不要用“step time 变短了”作为唯一结论；并行度改变会改变每步处理的 token 数。
统一换算到 tokens/s 或 time-to-train。

## 28.2 mini 的 benchmark

```bash
python -m mini_megatron.cli benchmark --config configs/tiny.json --steps 5
```

输出 history（loss/lr/grad_norm/tokens_per_sec）。注意：

- CPU 单线程性能不代表 GPU；
- 小模型 kernel 启动/通信占比高；
- 同步实现没有通信重叠；
- 目的是观察“配置变化趋势”，不是绝对性能。

## 28.3 剖析工具

生产：

- **Nsight Systems**：时间线，看 GEMM/通信/数据加载/气泡；
- **Nsight Compute**：kernel 级 roofline/占用率；
- **PyTorch profiler**：算子/内存；
- **NCCL debug**：通信拓扑/带宽；
- **DCGM**：GPU 利用率/温度/功耗/ECC；
- **Megatron 日志**：每 stage 的 timers、MFU、通信时间。

先看时间线，再看 kernel。不要一上来就优化一个只占 2% 的算子。

### 28.3.1 常用命令

```bash
# 前提：以下命令需要 CUDA 配置；本仓库的 tiny*.json 都是 CPU 配置，
# 直接跑 nsys/ncu 只会看到 CPU 算子。把 --config 换成你的 GPU 配置。

# Nsight Systems：抓时间线。cudaProfilerApi 模式要求程序里显式调用
# torch.cuda.cudart().cudaProfilerStart()/cudaProfilerStop()；mini 没有这些
# 调用，也不发 nvtx range，所以这里做全量抓取（短跑几步即可）。
nsys profile -t cuda,nvtx,osrt,cublas,cudnn,nccl \
  -o profile python -m mini_megatron.cli train --config <gpu-config>

# 如果要缩小抓取窗口，先在训练循环里插入：
#   torch.cuda.nvtx.range_push("profile-range") ... range_pop()
# 再加 --capture-range=nvtx --nvtx-capture=profile-range；没有匹配的 range
# 会得到一个空 profile。

# Nsight Compute：单个 kernel 的 roofline/占用率（同样需要 GPU）
ncu --set full -k regex:gemm -c 5 python -m mini_megatron.cli benchmark \
  --config <gpu-config> --steps 5

# PyTorch profiler：算子/显存
python -c "
import torch
from torch.profiler import profile, ProfilerActivity
with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
             record_shapes=True, profile_memory=True) as prof:
    ...  # 跑一步
print(prof.key_averages().table(sort_by='cuda_time_total', row_limit=20))
"

# 启动器冒烟（Windows/CPU 的 portable 后端，不是 NCCL）
python scripts/run_distributed.py --world-size 2 \
  --config configs/tiny_tp2.json --steps 2

# 真实 NCCL 拓扑/带宽（GPU 集群；不要用 tiny config 推断 NCCL 行为）
nvidia-smi topo -m
all_reduce_perf -b 8 -e 8G -f 2 -g 8
# tiny_tp2 在 world=2（TP=2,DP=1）和 world=4（TP=2,DP=2）都合法；
# world=8 时 DP=4，需把 global_batch_size 调到 8 的倍数，或换配置
NCCL_DEBUG=INFO torchrun --standalone --nproc_per_node=2 \
  -m mini_megatron.cli train --config configs/tiny_tp2.json
```

### 28.3.2 时间线怎么读

按“谁在等谁”读，而不是按颜色读：

1. **GPU 空闲段**：是数据加载、PP 气泡、还是 collective 等待？
2. **GEMM 之间的空隙**：kernel launch 开销、Python 开销、同步点；
3. **通信与计算是否重叠**：all-reduce 是否与下一个 GEMM 并行；
4. **每个 stage 的负载**：是否有一个 stage 明显更慢；
5. **step 之间的边界**：优化器/日志/checkpoint 是否阻塞。

先解决“大块空闲”，再优化单个 kernel。一个常见的 20% 提升来自消除数据加载
空档或 PP 气泡，而不是把某个 GEMM 快 2%。

## 28.4 一个调优流程

1. **基线**：固定模型/数据/并行度，测 tokens/s、峰值显存、MFU；
2. **找瓶颈**：GEMM 占比？通信？数据加载？气泡？
3. **单变量实验**：
   - micro-batch ↑；
   - TP 2/4/8；
   - PP 2/4；
   - SP 开/关；
   - checkpointing 开/关；
   - bf16/FP8；
4. **记录**：配置、tokens/s、显存、loss、稳定性；
5. **验证收敛**：性能提升不能以 loss 变差为代价；
6. **端到端**：加数据加载/评估/保存/故障恢复。

## 28.5 常见瓶颈与处理

| 现象 | 可能原因 | 处理 |
|---|---|---|
| GPU 利用率低、GEMM 短 | micro-batch 太小 | 增大 micro-batch/累积 |
| 通信占比高 | TP 跨 IB/消息太小 | TP 留 NVLink、bucket、重叠 |
| PP 气泡大 | m 太小/PP 太深 | 增大 m、VPP、减 PP |
| 数据加载空档 | CPU/存储瓶颈 | prefetch、更多 worker、本地缓存 |
| 显存碎片 OOM | 变长/临时张量 | expandable segments、重算 |
| loss 抖动 | 数据/精度/并行变化 | 固定 seed、fp32 复现、调 LR |
| 专家负载不均 | 路由塌缩 | aux/bias、capacity、监控 |

## 28.6 并行度实验设计

以 4 卡为例，固定模型/数据：

```text
A: TP=4, PP=1, DP=1
B: TP=2, PP=2, DP=1
C: TP=2, PP=1, DP=2
D: TP=1, PP=2, DP=2
```

记录：

- tokens/s；
- 峰值显存；
- 通信时间占比；
- loss（前 100 步）。

如果 B 比 A 快，说明 TP 跨度过大/PP 气泡可接受；如果 C 比 B 快，说明 DP 的
梯度通信比 PP 气泡更便宜。结论依赖模型/拓扑，不能照搬。

## 28.7 通信分析

- TP all-reduce：看 NCCL 消息大小/次数；小消息多时用 bucket/融合；
- DP all-reduce：分层（节点内 NVLink + 节点间 IB）；梯度压缩需谨慎；
- PP p2p：看 send/recv 与计算是否重叠；延迟敏感；
- CP：看 all-gather/ring 的带宽；
- EP：看 all-to-all 的负载均衡/尾延迟。

一个经验：**通信时间占比超过 20% 时，优先检查拓扑映射和消息大小**，而不是
直接换并行度。

## 28.8 性能与正确性的权衡

- 异步 all-reduce 可能改变数值顺序；
- FP8/FP4 可能影响收敛；
- 重算增加计算但降显存；
- 更大 batch 提高吞吐但可能影响超参；
- checkpoint 异步保存可能与训练写冲突。

每次优化都要跑收敛对比，不能只看 tokens/s。

### 28.8.1 CUDA Graph 与 torch.compile

**CUDA Graph** 把一串 kernel launch 录成图，减少 CPU launch 开销，适合
“shape 固定、重复执行”的训练 step。代价：

- shape/地址必须稳定；动态 batch/变长序列需要重新捕获；
- 显存分配要预预留；
- 与通信/checkpoint/调试工具的交互复杂；
- 出错时栈更难读。

**torch.compile** 做图捕获/算子融合，适合减少 Python/launch 开销；在大模型
分布式训练里，它与 TP/PP/自定义 autograd 的兼容性需要逐项验证。两者都不是
“打开就快”：先用 profile 证明 launch/碎片是瓶颈，再引入。

## 28.9 本章小结

- 指标要统一到 tokens/s、MFU、$/tokens 或延迟。
- 先时间线后 kernel；先找瓶颈再调参。
- 单变量实验，记录配置/性能/显存/loss。
- 通信占比高先查拓扑/消息大小。
- 性能优化不能牺牲收敛/正确性。

下一部分进入生产 Megatron 栈：Core 代码地图、模型家族、数据、集群、可观测性。
