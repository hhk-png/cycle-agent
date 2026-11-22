# 第 02 章 全景架构与代码地图

## 2.1 从集群到一行 loss：五层抽象

Megatron 的训练栈可以画成五层。每一层解决不同的问题，也对应不同的排障入口。

```text
┌────────────────────────────────────────────────────────────┐
│ 5. 平台/产品层   NeMo、Megatron Bridge、实验管理、推理服务   │
├────────────────────────────────────────────────────────────┤
│ 4. 训练配方      pretrain.py、SFT/LoRA、数据混合、超参       │
├────────────────────────────────────────────────────────────┤
│ 3. 并行运行时    TP/PP/DP/CP/EP、调度、检查点、优化器         │
├────────────────────────────────────────────────────────────┤
│ 2. 模型组件      TransformerLayer、Attention、MLP、MoE、SSM  │
├────────────────────────────────────────────────────────────┤
│ 1. 算子/硬件     GEMM、FlashAttention、TE FP8/FP4、NCCL/NVLink│
└────────────────────────────────────────────────────────────┘
```

**排障时先定位层**：loss NaN 可能在模型组件（数值）或并行运行时（梯度 reduce）；
吞吐下降可能在算子（kernel）或通信（拓扑）；恢复失败通常在检查点格式/布局。
不要一上来就改并行度。

## 2.2 Megatron Core 的模块地图

> 术语预告：SSM/Mamba 是状态空间模型，HybridModel 是 Transformer + SSM/
> MoE 的混合栈，MLA 是低秩 KV 的注意力；它们的结构细节在第 30、34 章。
> 这里只需知道它们在模块地图中的位置。

截至 2026 年，Megatron-LM 仓库的核心目录大致如下（具体文件会随版本调整）：

```text
megatron/
├── core/
│   ├── parallel_state.py          # 全局进程组与 rank 查询
│   ├── tensor_parallel/           # Column/Row/Embedding/CE/通信
│   ├── pipeline_parallel/         # p2p 通信、调度
│   ├── transformer/               # TransformerLayer、Attention、MLP
│   │   ├── moe/                   # Router、Dispatcher、Experts、aux loss
│   │   └── experimental_attention_variant/
│   ├── distributed/               # 分布式优化器、检查点、FSDP
│   ├── optimizer/                 # Adam、Muon、LR scheduler
│   ├── models/                    # GPT/BERT/T5/Mamba/Hybrid/多模态
│   ├── datasets/                  # 数据加载、packing、blending
│   ├── inference/                 # KV cache、chunked prefill、引擎接口
│   └── fp8_utils.py 等            # 精度与量化工具
├── training/                      # pretrain、arguments、checkpoint 逻辑
├── post_training/                 # SFT、LoRA、蒸馏等
├── legacy/                        # 旧模型实现（逐步迁移）
└── examples/                      # 各模型的可运行 recipe
```

读源码的建议顺序：

1. `parallel_state.py`：先理解 rank 布局与 group；
2. `tensor_parallel/layers.py`：再理解一个线性层怎样被切开；
3. `transformer/transformer_layer.py`：看 block 怎样组合；
4. `pipeline_parallel/schedules.py`：最后看多 micro-batch 如何调度。

直接读 MoE 或 CP 很容易迷失，因为它们是建立在前三层之上的。

## 2.3 mini-megatron 的模块地图

本教程的实现刻意与 Core 的分层同构：

```text
mini_megatron/
├── config.py                 # ModelConfig / ParallelConfig / TrainConfig
├── parallel/
│   ├── comm.py               # 通信抽象：torch.distributed 或 portable
│   ├── state.py              # TP/PP/CP/DP group、rank layout、collectives
│   ├── layers.py             # Column/Row/Embedding、FP8 模拟
│   ├── attention.py          # RoPE、GQA、CoreAttention、CP gather
│   ├── cross_entropy.py      # 词表并行 CE（含 CP 归一化）
│   ├── transformer.py        # TransformerBlock、TransformerStage（支持显式层归属）
│   ├── moe.py                # 专家复制的 TP MoE
│   ├── expert_parallel.py    # EP all-to-all dispatch/combine
│   ├── ring_attention.py     # ring attention 单进程参考
│   ├── ulysses.py            # Ulysses CP 单进程参考（两次 all-to-all）
│   └── mla.py                # MLA 独立参考（latent KV / 解耦 RoPE）
├── pipeline/
│   ├── schedules.py          # GPipe / 1F1B / 单 stage
│   ├── interleaved.py        # VPP 规划/依赖校验/气泡
│   └── interleaved_exec.py   # 可执行的 interleaved GPipe
├── optim/adamw.py            # AdamW、LR、梯度同步、ZeRO-1、Muon
├── data/
│   ├── dataset.py            # 合成数据 / memmap bin
│   └── packing.py            # 文档边界/position reset/loss mask
├── checkpoint.py             # 分 rank 保存与加载
├── checkpoint_reshard.py     # 模型权重 TP/PP 重分片
├── sharding.py               # 全量 state dict 的 TP/PP 切片
├── analysis.py               # 参数/显存/FLOPs/通信估算
├── training.py               # Trainer
├── generation.py             # 全量重算采样
├── generation_kv.py          # KV cache 解码
└── cli.py                    # train / generate / benchmark / info / estimate
```

生产与 mini 的对应关系：

| 生产概念 | mini 文件 | 关键差异 |
|---|---|---|
| `parallel_state` | `parallel/state.py` | EP 只支持 `ep=dp`；无混合 order/FSDP group |
| TE 线性层 | `parallel/layers.py` | 普通 GEMM + 手写 autograd；无 TE/FP8 kernel |
| FlashAttention | `parallel/attention.py` | 显式 score 矩阵；CP 用 all-gather KV |
| CP（ring/Ulysses） | `parallel/ring_attention.py`、`parallel/ulysses.py` | 单进程模拟；ring 物化块级 score，Ulysses 物化全序列 score |
| `pipeline_parallel/schedules.py` | `pipeline/schedules.py` | GPipe/1F1B；无 zero-bubble；p2p 用 Queue/NCCL |
| distributed optimizer | `optim/adamw.py` 的 ZeRO1 | 按参数张量分片，非扁平 buffer |
| distributed checkpoint | `checkpoint.py` | 同布局恢复；`checkpoint_reshard.py` 重分片权重/非 ZeRO Adam |
| MoE EP | `parallel/expert_parallel.py` | padding + Python 循环，`ep=dp`，无 grouped GEMM |
| VPP/interleaved | `pipeline/interleaved.py` + `interleaved_exec.py` | 可执行 interleaved GPipe；未接 Trainer/1F1B |
| KV cache | `generation_kv.py` | 单请求、PP=1；无分页/连续批处理 |
| MLA | `parallel/mla.py` | 独立单进程参考；latent cache/解耦 RoPE；无 TP/CP/FP8 |
| 代价建模 | `analysis.py` | 一阶估算，不含 kernel/碎片细节 |
| Megatron Bridge | 无 | 生产用 Bridge 做 HF 互转 |

## 2.4 一次训练步的数据流

以 `TP=2, PP=2, DP=2` 为例，一次 optimizer step 大致是：

```text
数据加载（每个 DP rank 不同样本，TP/PP/CP 副本相同）
        │
        ▼
Stage 0: Embedding → Layer 0 … Layer k
        │ 激活 send（PP）
        ▼
Stage 1: Layer k+1 … Layer L-1 → final norm → LM head → CE
        │
        ▼
loss.backward()：PP 反向 send 梯度
        │
        ▼
DP all-reduce 梯度（同一 TP/PP/CP 坐标的副本）
        │
        ▼
梯度裁剪 → optimizer.step() → zero_grad
```

注意三个“同/不同”：

- 同一 **DP 组** 的 rank 处理不同样本；
- 同一 **TP/PP/CP 组** 的 rank 处理同一批样本的不同模型/序列切片；
- 同一 **PP 组** 的 rank 参数不同（不同层），所以不能对梯度做 all-reduce。

把这三句话记住，大多数“为什么这里要通信/为什么这里不能 all-reduce”的问题都会
迎刃而解。

## 2.5 rank 布局：所有并行维度的坐标系

Megatron 允许配置 `tensor_model_parallel_size`、`pipeline_model_parallel_size`、
`context_parallel_size`、`data_parallel_size`。mini 使用固定布局：

```text
rank = ((pp_idx * dp_size + dp_idx) * cp_size + cp_idx) * tp_size + tp_idx
```

即 **TP 最快变化，PP 最慢变化**。原因：

- TP 通信最频繁，放在连续 rank 上，便于映射到同一 NVLink 域；
- PP 通信是点对点，跨节点可以接受；
- DP 只做梯度 all-reduce，放在剩余维度。

生产 Megatron 支持可配置 order（`tp-cp-ep-dp-pp` 等）和 EP/DP 重叠，但
“把最频繁的通信放在最快链路”这个原则不变。

`mini_megatron/parallel/state.py` 用五组循环创建 TP/CP/DP/PP/EP group；每个
rank 只保存自己所属的 group 对象。EP 组与 DP 组共享同一组 rank，但用独立的
group key，避免 portable 后端的消息串线。`comm.py` 把
`torch.distributed.ProcessGroup` 和 portable 的 `MiniGroup` 统一成同一套
API。

## 2.6 为什么 mini 需要 portable 通信后端

PyTorch 在 Linux 上通常用 Gloo（CPU）或 NCCL（GPU）。部分 Windows 轮子
（尤其 2.9 系列）在创建 Gloo TCP device 时会报：

```text
RuntimeError: makeDeviceForHostname(): unsupported gloo device
```

这是平台/构建问题，不是教程代码的 bug。为了让多进程测试在 Windows 上也能跑，
`comm.py` 提供了基于 `multiprocessing.Queue` 的 portable 后端：

- 每个 rank 一个 inbox，所有 rank 共享 outboxes；
- collective 用 `(group_key, sequence)` 匹配；
- p2p 用 `(src, tag, sequence)` 匹配；
- 语义与 torch.distributed 对齐，但性能较差，仅用于教学/验证。

Linux 生产路径仍然是 `torch.distributed`：`comm.initialize_torch()`。

## 2.7 本章小结

- Megatron 的训练栈分为算子、模型、并行运行时、训练配方、平台五层。
- Megatron-LM、Core、Bridge、NeMo 是不同层次的工程，读源码先看 Core。
- mini 的文件结构与 Core 同构，便于把概念映射到生产代码。
- rank 布局决定了通信拓扑；“谁能和谁 all-reduce”由 group 决定。
- portable 后端是 Windows 上的验证兜底，不是性能路径。

下一章进入环境与第一轮训练：把 mini 真正跑起来。
