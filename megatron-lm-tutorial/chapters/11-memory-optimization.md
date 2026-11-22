# 第 11 章 显存优化：激活、状态与参数

## 11.1 显存由什么组成

训练时 GPU 显存大致分为：

```text
总显存 = 参数 + 梯度 + 优化器状态 + 激活 + 临时 buffer + 通信桶 + 碎片
```

不同并行维度攻击不同的项：

- TP/PP/EP：切参数、梯度、优化器状态、激活；
- SP/CP：切激活；
- ZeRO/FSDP：切梯度/优化器状态/参数；
- activation checkpointing：用计算换激活；
- offload：把状态/激活搬到 CPU/NVMe；
- 量化：降低参数/激活/状态的字节数。

## 11.2 激活重计算

前向时不保存每个 block 的中间激活，反向时重新算一遍：

```python
def forward(x):
    if training and checkpointing:
        return checkpoint(self._forward_impl, x, use_reentrant=False)
    return self._forward_impl(x)
```

收益：激活显存大幅下降。  
代价：额外前向计算，通常 20%–40% 的 step time；重算的 block 不能有随机性
（dropout 的 RNG 要保存/恢复，`torch.utils.checkpoint` 会处理一部分）。

策略：

- **全量**：每个 block 都 checkpoint；
- **选择性**：只 checkpoint 大激活的 block（attention/MLP）；
- **周期性**：每 N 层 checkpoint 一层；
- **offload**：把激活搬到 CPU，反向再取回。

Megatron 支持 `recompute_granularity`、`recompute_method`、`recompute_num_layers`
等开关；生产还会与 PP/SP/CP 组合，按 stage 配置。

## 11.3 ZeRO/FSDP 的四个阶段

| 阶段 | 切什么 | 显存收益 | 通信 |
|---|---|---|---|
| ZeRO-1 | 优化器状态 | 大 | 参数更新后 all-gather/广播 |
| ZeRO-2 | 优化器状态 + 梯度 | 更大 | 梯度 reduce-scatter + 参数更新后 all-gather/广播 |
| ZeRO-3 / FSDP | + 参数 | 最大 | 前向 all-gather、反向 reduce-scatter |
| HSDP | 节点内 FSDP + 节点间复制 | 平衡 | 分层通信 |

所有 ZeRO 阶段在参数更新后都必须让副本看到新参数：ZeRO-1/2 用
all-gather/广播，ZeRO-3/FSDP 用下一次前向的 all-gather。只写“梯度
reduce-scatter”而漏掉参数同步，会让人以为 ZeRO-2 不需要通信。

Megatron 的 distributed optimizer 与 ZeRO-1/2 类似，但针对 TP/PP 的参数布局
做了优化。flat 版本用 reduce-scatter + all-gather，通信形状接近 ZeRO-2；
但**是否真的省掉一份梯度显存，取决于是不是释放了完整梯度 buffer**。mini 的
`DistributedOptimizer` 保留完整 `grad_flat`，只额外持有 `grad_shard`，所以
内存上仍是 ZeRO-1，不能按 ZeRO-2 的账本估算；生产路径要用
`optim_grads`/`optim_grads_params` 才会在执行完 reduce-scatter 后丢弃完整
梯度。FSDP v2 在 2026 roadmap 中继续补齐 TP/EP/PP、HSDP、CUDA Graph 等。

### 为什么不能无脑上 ZeRO-3

ZeRO-3 每个 layer 前向都要 all-gather 参数，反向再 reduce-scatter。对超大模型
它能把参数显存降到 `1/d`，但：

- 通信次数多、延迟敏感；
- 小 GEMM 时 all-gather 开销占比高；
- 与 TP/PP 组合时参数布局复杂；
- checkpoint 重分片更困难。

因此大规模训练常见的是 **TP+PP+DP+ZeRO-1/2**，而不是纯 ZeRO-3。

## 11.4 Offload

把优化器状态或激活放到 CPU/NVMe：

- **优化器 offload**：step 时把状态搬到 GPU，更新后搬回；PCIe 带宽是瓶颈；
- **激活 offload**：前向写回 CPU，反向取回；它**需要高带宽**（PCIe Gen5 /
  NVLink-C2C），低带宽下传输会吃掉收益，甚至比不 offload 更慢；
- **参数 offload**：推理/低吞吐训练可用。

offload 的收益取决于 PCIe/NVLink-C2C 带宽和计算/通信重叠。它不是“免费显存”：
如果 GPU 计算很快而 PCIe 很慢，step time 会被传输主导。判断公式是

```text
offload 是否划算 ⇔ 每 step 传输字节 / 有效带宽 < 节省的显存所换来的并行收益
```

在长序列场景，激活量大、传输次数多，offload 往往只在 CPU-GPU 高带宽
（如 Grace Hopper/GB200 的 NVLink-C2C）或 NVMe 直连方案下才值得。

## 11.5 精度与显存

| 精度 | 字节/参数 | 说明 |
|---|---:|---|
| fp32 | 4 | 主权重/优化器状态常用 |
| bf16/fp16 | 2 | 训练计算/权重 |
| fp8 | 1 | TE 训练/推理，需 scaling |
| fp4 | 0.5 | Blackwell NVFP4，需块缩放 |

混合精度训练通常保留 fp32 master weight 和 Adam 状态，所以总状态仍是 16 B/param
左右；FP8 主要降低计算和激活/权重的瞬时占用，不会自动把优化器状态变成 1 B。

## 11.6 碎片与 OOM 排查

OOM 不一定是“理论显存不够”，常见原因：

- 分配器碎片：大量不同大小的临时张量；
- 峰值叠加：all-gather buffer + 激活 + 通信桶同时存在；
- 长序列的 attention 临时矩阵；
- checkpoint 保存时的额外 copy；
- 评测/生成时的 KV cache。

排查顺序：

1. `torch.cuda.memory_summary()` 看 reserved/allocated；
2. 记录每个 step 的 `max_memory_allocated`；
3. 二分关闭功能（SP/CP/recompute/优化器）定位峰值；
4. 用 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 缓解碎片；
5. 降低 micro-batch 或序列长度，确认是否线性；
6. 检查是否有未释放的计算图（`retain_graph`、累计 loss）。

### 11.6.1 用 memory snapshot 定位“谁吃了显存”

`memory_summary()` 只给聚合数字；要看时间线上的分配/释放和调用栈，用
PyTorch 的 memory snapshot（API 带下划线，属于调试接口，版本间可能变化）：

```python
import torch

torch.cuda.memory._record_memory_history(max_entries=100_000)
# ... 跑 1–2 个 step（最好包含 OOM 前的那一步）...
torch.cuda.memory._dump_snapshot("mem_snapshot.pickle")
torch.cuda.memory._record_memory_history(enabled=None)
```

把 pickle 拖到 `https://pytorch.org/memory_viz`（或离线打开该页面）可以看到：

- **allocated vs reserved**：reserved 远大于 allocated 就是碎片/缓存；
- **峰值时刻**：哪个 op/调用栈同时持有最多 block；
- **segment 大小与空闲块**：是否有“大块被小块卡住”的碎片；
- **泄漏**：step 结束后仍存活的分配（通常是计算图/缓存没释放）。

读图时先找 **峰值 step**，再沿时间轴看峰值前 10–20 个分配；常见元凶是
all-gather/reduce-scatter 的连续 buffer、attention 的 `s×s` 临时量、
checkpoint 保存的 CPU/GPU copy、以及未释放的 loss 累加图。snapshot 本身
有开销，只在复现环境开，不要常驻生产。

## 11.7 一个显存预算例子

7B 模型，bf16 训练，DP=8，ZeRO-1，TP=1，PP=1：

```text
参数 2B/param × 7B = 14 GB（每卡复制）
梯度 2B/param × 7B = 14 GB
优化器状态 12B/param × 7B / 8 ≈ 10.5 GB
```

仅状态约 38.5 GB/卡，还没算激活。80 GB 卡在序列 2K、batch 1 时可能刚好；
序列 8K 或 batch 变大就会 OOM。此时可以：

- TP=2/4 切参数/激活；
- ZeRO-1 增大 DP（更多卡分摊状态）；
- activation checkpointing；
- 序列并行/CP。

如果换 70B：完全不切分是 `16 × 70 = 1.12 TB/卡`；在 DP=8 + ZeRO-1 下是
`4×70 + 12×70/8 = 280 + 105 = 385 GB/卡`（权重/梯度仍复制）。80 GB 卡无论
哪种都放不下，必须 TP+PP+ZeRO 组合。

### 11.7.1 激活显存的一阶公式

Megatron 论文给出的量级（fp16、无重算）：

```text
activation ≈ s · b · h · L · (34 + 5 · n_h · s / h) bytes
```

其中 `n_h` 是 attention head 数。代入 `s=4096, b=1, h=4096, L=32, n_h=32`：

```text
每层每 token 因子 ≈ 34 + 5·32·4096/4096 = 194
activation ≈ 4096 · 1 · 4096 · 32 · 194 ≈ 1.04e11 bytes ≈ 104 GB（97 GiB）
```

注意这里是**十进制 GB**（1 GB = 1e9 B）；按 GiB 计约 97 GiB。这就是长序列
必须开 activation checkpointing/SP/CP 的原因。公式是数量级，
真实值取决于 fused kernel、临时 buffer 和 autograd 保存策略；用它做方案
筛选，再用 `torch.cuda.max_memory_allocated()` 校准。

### 11.7.2 offload 的带宽账

假设把 70B 的优化器状态（约 840 GB fp32）offload 到 CPU。先明确**每卡实际
持有多少**：如果不做 ZeRO/DP 分片，单卡搬 840 GB；DP=8 + ZeRO-1 时每卡只有
约 105 GB：

```text
PCIe Gen5 x16 单向 ≈ 64 GB/s（理论上限，实际更低）
DP=1：一次 step 读出+写回 ≈ 2 × 840 GB → 1680/64 ≈ 26 s
DP=8：每卡 ≈ 2 × 105 GB → 210/64 ≈ 3.3 s（但仍然与计算串行）
```

通用式子是 `2 × 840/d_z GB ÷ 带宽`，`d_z` 是优化器状态的切分倍数；漏掉
`d_z` 会把时间高估 `d_z` 倍。如果 step 的 GPU 计算只有几秒，offload 会把
训练变成“PCIe 受限”。NVLink-C2C（Grace-Hopper/Blackwell）带宽高得多，才
可能让 CPU offload 变得实用。结论：**offload 是否值得，先算每卡传输时间，
再看能否与计算重叠。**

### 11.7.3 MoE 训练的额外显存

稠密模型的激活公式不包含 MoE 的临时 buffer。MoE 的 OOM 往往不是“专家参数
太大”，而是 dispatch/combine/capacity padding/grouped GEMM workspace 叠加。
设每个 EP rank 每个 micro-batch 有 `T` 个 token，`k` 个专家，`E` 个专家，
`ep` 个 EP rank，`e_local=E/ep`，`cf` 是 capacity factor：

```text
capacity = ceil(T · ep · k / E · cf)          # 每个专家的上限
dispatch/combine payload ≈ T · k · (h·bpe + 4)  # hidden + fp32 路由权重
metadata                 ≈ T · k · 2 · 8        # token id + local expert id
capacity padding         ≈ e_local · capacity · (h·bpe + 4)
expert MLP 激活          ≈ e_local · capacity · (h + 2·ffn) · bpe
combine 输出             ≈ T · k · h · bpe
```

以 `h=7168, ffn=2048, E=256, ep=8, T=125k, k=2, bf16, cf=1.25` 为例：

| buffer | 量级 |
|---|---:|
| dispatch payload（每 rank 发送） | `250k × 14,340 B ≈ 3.6 GB` |
| capacity（`capacity≈9,766`，32 个本地专家） | `32 × 9,766 × 14,340 B ≈ 4.5 GB` |
| expert MLP 激活（含 padding） | `32 × 9,766 × 11,264 × 2 B ≈ 7.0 GB` |
| combine 输出 | `250k × 7,168 × 2 B ≈ 3.6 GB` |

这些 buffer 是**每个 MoE 层**的峰值，不是全模型；但它们会与 in-flight
micro-batch、PP 的 stage 激活、通信双缓冲叠加。几个降低峰值的做法：

- **dropless + split sizes**：不 padding 到最大容量，按实际 token 数分配；
- **capacity 只用于 GEMM，不用于激活**：padding 区不保存中间激活；
- **dispatch/combine 与 GEMM 分块流水线**：用较小 chunk，避免整层 buffer
  同时驻留；
- **activation checkpointing**：重算 expert MLP，省掉 `(h+2ffn)` 激活；
- **共享专家与本地专家分开算**：shared expert 的激活不随 `k` 增长，但每层
  都常驻，别漏进“专家稀疏”的账本。

一个常见误判是“专家参数按 EP 切了，所以显存一定够”。参数只是静态项；
MoE 的动态 buffer 与 `T·k` 成正比，长序列/大 batch 下会先撞墙。把上面的
四项加进 `estimate` 的思路，比只算 `16 B/param` 更接近真实 OOM 点。

## 11.8 mini 的显存优化

- `activation_checkpointing`：`TransformerBlock.forward` 使用
  `torch.utils.checkpoint`；
- `ZeRO1Optimizer`：优化器状态按参数张量分片；
- `sequence_parallel`：激活按序列切；
- `context_parallel_size`：序列切到 CP；
- `fp8_simulation`：数值模拟，不省显存（生产 TE 才省）；
- `benchmark` 命令可观察 tokens/s 随配置变化。

mini 没有实现 offload、FSDP、CUDA Graph，但保留了“优化器状态分片”和“激活
重算”两条主线，足以做对照实验。

## 11.9 本章小结

- 显存是参数、梯度、状态、激活、临时 buffer 的总和；不同并行维度攻击不同项。
- activation checkpointing 用计算换显存，是最通用的手段。
- ZeRO/FSDP 用通信换状态/参数显存；不能无脑上 ZeRO-3。
- offload 受 PCIe 带宽限制，不是免费午餐。
- OOM 先看峰值和碎片，再做功能二分。

下一章讨论精度：bf16、FP8、FP4 怎样在保持收敛的同时提高吞吐。
