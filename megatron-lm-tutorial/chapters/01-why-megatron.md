# 第 01 章 为什么需要 Megatron-LM

## 1.1 单卡训练的三个硬墙

### 第一堵墙：参数与优化器状态

一个参数量为 `N` 的模型，在混合精度训练中通常需要：

| 内容 | 精度 | 字节/参数 |
|---|---|---:|
| 模型权重 | bf16/fp16 | 2 |
| 梯度 | bf16/fp16 | 2 |
| fp32 主权重 | fp32 | 4 |
| Adam 一阶矩 | fp32 | 4 |
| Adam 二阶矩 | fp32 | 4 |
| 合计 | | **16** |

70B 模型仅状态就需要约 **1.12 TB**。即使使用 ZeRO-1/FSDP 把优化器状态切到
64 张卡上，每张卡仍要保存权重和梯度；再叠加激活、临时 buffer、通信桶，80 GB
卡依然紧张。**参数分片不是可选项，而是规模化的入口。**

### 第二堵墙：算力与利用率

Transformer 的训练计算量近似为 `6ND`（前向 `2ND`，反向 `4ND`，每 token 每参数
6 次浮点运算），注意力部分还要加 `O(s^2)` 项。单卡算力固定时，扩大模型意味着
训练时间线性增长；而矩阵维度变大后，GPU 的 Tensor Core 才更容易吃满。并行
不是为了“把任务分给更多卡”这么简单，而是为了**让每个 GEMM 仍然足够大、让
通信占比足够低**。

### 第三堵墙：单点故障与实验迭代

大规模训练的真实故障率远高于教材描述：GPU 掉卡、NVLink 降速、IB 交换机抖动、
存储限流、任务被抢占。一个不能从 checkpoint 恢复、不能改变并行度的训练系统，
在千卡规模上几乎不可用。Megatron 的检查点、弹性与重分片能力，和生产算子同样
重要。

## 1.2 并行的五个维度

Megatron 把“切模型”拆成五个正交维度：

| 维度 | 切什么 | 主要通信 | 典型放置 |
|---|---|---|---|
| 数据并行 DP | 样本 | 梯度 all-reduce / reduce-scatter | 跨节点、跨副本 |
| 张量并行 TP | 单层内的权重矩阵 | all-reduce / all-gather / reduce-scatter | 节点内 NVLink |
| 流水线并行 PP | 层 | 激活/梯度点对点 | 跨节点 |
| 上下文并行 CP | 序列 | KV/注意力通信 | 长序列、跨节点 |
| 专家并行 EP | MoE 专家 | all-to-all | 节点内/跨节点 |

实际训练通常是 3D/4D 组合：`TP × PP × DP × CP`，MoE 再叠加 EP。作为量级
示例，1.7 会用一个 462B/10T 的配方算 GPU-hours——那不是某个公开训练事实，
而是用来展示这些维度如何组合与取舍的算例。

> 关键直觉：**TP 切的是“宽度”，PP 切的是“深度”，DP 切的是“数据”，CP 切的是
> “时间/序列”，EP 切的是“稀疏容量”。**

## 1.3 为什么不用朴素的模型并行

把每一层直接放到不同 GPU 上（naive layer parallelism）看似简单，但有两个致命
问题：

1. **GPU 空转**：同一时刻只有一张卡在算当前层，其余卡等待，利用率接近
   `1/p`。
2. **通信粒度差**：每层都要传一次激活，无法把计算与通信重叠。

Megatron 的贡献之一，是把“层内并行”做成**可微的算子级切分**：Column/Row
Parallel Linear 让一次前向只引入一次 all-reduce，且梯度仍然正确；流水线调度
则用 micro-batch 填充气泡，让不同 stage 同时工作。这两件事把“能切开”变成了
“切得动、跑得快”。

## 1.4 Megatron 的历史与 2026 年的位置

- **2019**：Megatron-LM 论文提出张量并行，8.3B 参数，验证了层内模型并行。
- **2021–2022**：Megatron-Turing NLG 530B（2021 年训练、2022 年公布），
  证明数千 GPU 训练可行。
- **2022–2023**：Megatron Core 抽离为库；序列并行、分布式优化器、MoE、
  上下文并行逐步进入主线。
- **2024–2025**：Transformer Engine FP8、DeepSeek 风格 MoE/MLA、长上下文、
  分布式检查点成熟；NeMo 与 Megatron Bridge 承担 recipes 与 HF 互转。
- **2026**：Megatron Core 0.20.x。重点从“支持某个模型”转向
  **HybridModel、FP4/Blackwell、百万上下文、训练/推理一体化、Muon 等新优化器**。

需要区分四个名字：

| 名字 | 定位 |
|---|---|
| **Megatron-LM** | NVIDIA 的 GitHub 仓库/研究工程，包含 Core、训练脚本、示例 |
| **Megatron Core** | 可复用的并行训练库（`pip install megatron-core`） |
| **Megatron Bridge** | Hugging Face ↔ Megatron 的转换、recipes、SFT/LoRA，属于 NeMo 生态 |
| **NeMo** | NVIDIA 的端到端训练/推理平台，使用 Megatron Core 作为后端之一 |

很多文章把四者混为一谈。实际选型时：要改并行算子，读 Core；要转换 HF 权重，
用 Bridge；要企业级训练平台，看 NeMo；要做研究复现，Megatron-LM 仓库的
`examples/` 最直接。

## 1.5 Megatron 与其他方案的差异

| 方案 | 强项 | 典型短板 |
|---|---|---|
| Megatron Core | 极致的大规模训练性能、TP/PP/EP/CP 组合、NVIDIA 硬件协同 | 学习曲线陡、依赖 TE/集群、推理生态需另接 |
| PyTorch FSDP | 原生、易用、与 PyTorch 生态一致 | 超大模型/长序列的极致性能与拓扑感知不如 Megatron |
| DeepSpeed | ZeRO 系列、易上手、生态广；Megatron-DeepSpeed 曾用于 MT-NLG 530B 的 3D 并行，DeepSpeed-MoE 也有大规模实践 | 纯 DeepSpeed 栈在 TP/PP/EP/CP 的组合深度、与 NVIDIA 新硬件/kernel 的同步上，通常不如 Megatron Core 主线；两条栈的“3D 并行”不能简单等同 |
| JAX/MaxText | XLA 编译、TPU 生态、函数式 | NVIDIA GPU 上的算子/社区生态不同 |
| NeMo | 企业平台、数据/实验管理、Bridge | 抽象层多，深入排障需要理解 Megatron |

没有“绝对最好”的框架，只有与**模型结构、集群拓扑、团队能力、交付时间**匹配的
方案。教程后面的商业分析会展开这张表。

## 1.6 一个反直觉的事实：并行度不是越大越好

假设 TP=8 已经能把单层放进 NVLink 域，继续增大 TP 到 16 往往不会更快：

- 每个 GEMM 的 `M/N/K` 变小，Tensor Core 利用率下降；
- all-reduce 的通信量随 TP 增大而增加；
- LayerNorm、Dropout 等逐元素算子的并行收益很低；
- 跨 NVLink 域后，TP 通信会落到 IB，延迟和带宽都变差。

因此生产配置通常遵循：

1. 先让 **TP 不超过单个 NVLink 域**（例如 8 卡）；
2. 用 **PP** 扩展到多节点；
3. 用 **DP** 填充剩余并行度；
4. 长序列再引入 **CP**；
5. MoE 用 **EP** 替代部分 TP。

这不是教条，而是后面性能建模章节要量化的经验起点。

## 1.7 一个数量级账本：GPU-hours 才是通用货币

把模型规模、token 预算和硬件放在一起，可以得到一张“训练要花多少”的表。
按 `FLOPs ≈ 6ND`、H100 SXM BF16 dense `989 TFLOP/s`、MFU=40% 估算：

| 模型 | token | tokens/param | 训练 FLOPs | GPU-hours（40% MFU） | 1000 卡时间 |
|---|---:|---:|---:|---:|---:|
| 7B | 1T | 143 | 4.2e22 | ~29.5k | ~1.2 天 |
| 70B | 2T | 28.6 | 8.4e23 | ~590k | ~24.6 天 |
| 462B | 10T | 21.6 | 2.77e25 | ~19.5M | ~812 天 |

三行的 token/param 比例并不相同：7B/1T 是明显“过训练”（远超 Chinchilla 的
约 20 tokens/param），70B/2T（28.6）仍是偏重数据的配方，462B/10T（21.6）
才接近 Chinchilla 的计算最优比例。比较成本时必须同时看参数和 token，不能只
按参数量线性外推。462B/10T 的数字用于展示量级，不代表某个真实配方。

这张表解释了三件事：

1. **规模不是线性增长**：参数量 × token 数同时增长，GPU-hours 是乘积级；
2. **MFU 直接等于钱**：40% → 30% 会让时间/成本增加约 1/3；
3. **并行度不是目的**：TP/PP/DP/CP/EP 的组合，最终都要落到“能否把
   GPU-hours 压到预算内、把失败/重试控制在可接受范围”。

这也是为什么后面每一章都在算账：显存账、通信账、气泡账、TCO 账。

## 1.8 本章小结

- 规模化的核心矛盾是显存、算力利用率、通信和可靠性，而不是“模型代码不会写”。
- Megatron 用 TP/PP/DP/CP/EP 五个维度组合解决这些矛盾。
- Megatron-LM、Core、Bridge、NeMo 是不同层次的产品，选型时要分清。
- 并行度存在收益递减，合理配置取决于拓扑与模型形状。

下一章给出 Megatron 的全景架构，以及本教程 mini 实现与生产代码的对应关系。
