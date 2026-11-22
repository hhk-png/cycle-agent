# 附录 I 硬件、成本与规模参数手册

> 本附录是**带口径的参数表**，不是报价单。GPU 规格随封装（SXM/PCIe/NVL）、
> 功耗限制、驱动/固件和厂商 SKU 变化；云价格随合同、地区、预留/spot 变化。
> 使用前请以厂商数据表和你的合同为准，并记录查询日期。

## I.1 为什么需要一张参数表

并行策略的每个决策都落在具体数字上：

- TP 能不能跨节点，取决于 NVLink 域大小和 IB 带宽；
- 70B 能不能放下，取决于显存、优化器状态和激活；
- 训练要多久，取决于峰值算力、MFU 和 GPU-hours；
- 推理 $/1M tokens，取决于 decode 吞吐和 KV cache 容量。

没有参数表，讨论会退化成“听说 H100 很快”“听说 B200 翻倍”。本附录给出
数量级和公式，并明确哪些数字是**实测/公开论文**，哪些是**估算**。

## I.2 GPU 数量级参数

下表是 dense（非稀疏）峰值，单位按厂商习惯；不同来源可能差 5%–10%。

| GPU | 显存 | HBM 带宽 | BF16 dense | FP8 dense | 节点内互联 | TDP 量级 |
|---|---:|---:|---:|---:|---|---:|
| A100 SXM 80GB | 80 GB HBM2e | ~2.0 TB/s | ~312 TFLOP/s | — | NVLink 3，600 GB/s | 400 W |
| H100 SXM 80GB | 80 GB HBM3 | ~3.35 TB/s | ~989 TFLOP/s | ~1979 TFLOP/s | NVLink 4，900 GB/s | 700 W |
| H200 SXM | 141 GB HBM3e | ~4.8 TB/s | ~989 TFLOP/s | ~1979 TFLOP/s | NVLink 4，900 GB/s | 700 W |
| B200 | 192 GB HBM3e | ~8 TB/s | ~2.2 PFLOP/s 级 | ~4.5 PFLOP/s 级 | NVLink 5 | ~1000 W 级 |
| GB300（B300） | 288 GB HBM3e/GPU | ~8 TB/s/GPU | ~2.2–2.5 PFLOP/s 级 | ~4.5–5 PFLOP/s 级 | NVLink 5，NVL72 域 | ~1400 W 级 |
| AMD MI300X | 192 GB HBM3 | ~5.3 TB/s | ~1.3 PFLOP/s 级 | ~2.6 PFLOP/s 级 | Infinity Fabric | ~750 W |
| Google TPU v5p | 95 GB HBM | ~2.8 TB/s | ~459 TFLOP/s | — | ICI | — |
| AWS Trainium2 | 96 GB HBM | ~2.9 TB/s | ~0.65 PFLOP/s 级 | — | NeuronLink | — |
| 昇腾 910B | 64 GB HBM | ~1.6 TB/s | ~0.4 PFLOP/s 级 | — | HCCS | ~400 W |

**使用注意**：

1. **dense vs sparse**：厂商标称常同时给 dense/sparse 两个数字。MFU 的分母
   必须是所用精度的 **dense** 峰值；稀疏性只在模型/内核真的利用时才算。
2. **PCIe vs SXM/NVL**：PCIe 版本的峰值和互联远低于 SXM；不要拿 SXM 的
   TFLOPs 配 PCIe 的 NVLink 带宽。
3. **FP8/FP4 的“2×/4×”是硬件峰值**，实际能否用取决于 kernel、精度 recipe
   和收敛；不要把峰值当吞吐。
4. **功耗/散热**：TDP 是上限，实际功耗取决于 workload；供电/冷却不足会
   降频，直接吃掉 MFU。
5. **TPU/Trainium/昇腾** 与 CUDA 生态不同，跑 Megatron 需要适配层（如
   MindSpeed），不能按“同参数”直接比较。

## I.3 节点与集群参数

| 形态 | GPU/节点 | 节点内互联 | 节点间 | 典型用途 |
|---|---:|---|---|---|
| 8×H100 SXM | 8 | NVLink/NVSwitch，~900 GB/s | IB NDR 400G/节点 | 7B–70B 训练 |
| 8×H200 | 8 | NVLink 4 | IB NDR | 70B–405B |
| 8×B200 | 8 | NVLink 5 | IB NDR/XDR | 大模型训练/推理 |
| GB200 NVL72 | 72（18 节点） | NVLink 5 域 | IB | 万亿级 MoE/推理 |
| GB300 NVL72 | 72 | NVLink 5 域 | IB | 同上，显存更大 |
| 8×MI300X | 8 | Infinity Fabric | RoCE/IB | ROCm 生态 |
| Ascend 集群 | 8 | HCCS | RoCE | MindSpeed/国产化 |

**集群级参数**：

- 一个 1000 卡 H100 集群的 IT 功耗约 1.0–1.3 MW（按 8–10 kW/节点），
  机房功耗乘 PUE；
- IB 胖树/rail-optimized 的收敛比（oversubscription）决定跨 leaf 带宽；
- 存储：训练数据/检查点的吞吐常被低估；70B checkpoint 约 1 TB/次；
- 故障率：千卡级每周可能有若干次 GPU/链路/节点故障，设计要按“必然发生”
  而不是“概率事件”。

### I.3.1 电力与冷却

```text
机房功耗 ≈ IT 功耗 × PUE
IT 功耗 ≈ GPU 数 × 单卡实际功耗 + CPU/内存/网络/存储
电费 ≈ 机房功耗 × 小时数 × $/kWh
```

数量级：8×H100 节点 IT 功耗约 8–10 kW（GPU TDP 700W ×8 = 5.6 kW，再加
CPU/内存/网卡/风扇约 2–4 kW；只按 0.7 kW/卡估会漏掉主机开销）。PUE
1.2–1.5，1000 卡（125 节点）约 1.0–1.3 MW IT。$/kWh 随地区/合同差异很大
（工业电价常见 $0.05–0.15/kWh，部分市场更高）。按 9 kW/节点、PUE 1.3、
$0.08/kWh，一年 8760 小时：

```text
125 节点 × 9 kW × 1.3 PUE × 8760 h × $0.08/kWh ≈ $1.02M/年
```

这还没算冷却水、配电容量、机房租金和碳成本。**电力不是“运行费”里的小项**；
在电价高或 PUE 差的机房，它可能超过 GPU 折旧。

### I.3.2 网络、存储与故障域

- 节点内：NVLink/NVSwitch，TP/EP 的首选域；
- 节点间：IB NDR 400G 或 RoCE；胖树/rail-optimized 的收敛比决定跨 leaf
  带宽；非阻塞胖树贵，收敛比 2:1/3:1 常见；
- 存储：训练数据读取要能跟上 `tokens/s × bytes/token`；检查点写入要能在
  训练不中断的情况下完成；
- 故障域：单卡、单链路、单节点、单 leaf、单 PDU 都可能故障；设计要能
  “降级运行/快速重启”，而不是假设零故障。

## I.4 云价格的数量级（示例，不是报价）

以下仅用于建模，实际价格按合同/地区/代际浮动。单位为 **$/GPU-hour**。

| 形态 | 示例区间 | 说明 |
|---|---:|---|
| H100 按需 | 2.0–4.5 | 大云/neocloud 差异大 |
| H100 预留/长约 | 1.5–3.0 | 承诺 1–3 年 |
| H100 spot | 0.8–2.0 | 可被回收，需容错 |
| H200 按需 | 3.0–6.0 | 显存/带宽溢价 |
| B200 按需 | 5.0–10.0 | 供给紧、代际新 |
| GB200 NVL72 整机 | 按机柜/合约 | 不是单卡零售 |
| MI300X | 1.5–3.5 | ROCm 生态折扣/风险 |
| 昇腾 910B | 区域/合同定价 | 国产化/合规溢价 |

**有效成本**不是单价：

```text
有效 $/可用 GPU-hour =
    名义单价
  / (1 - 中断率)
  / 利用率
  + 迁移/运维/合规/电力分摊
```

spot 单价 1.0、每天中断 2 次、每次恢复 20 分钟，有效成本可能接近按需的
1.5–2 倍。容量块单价低但闲置率高，同样要按利用率折算。

### I.4.1 合同、融资与折旧

自建 TCO 不是“GPU 单价 × 数量”：

| 项 | 典型量级/口径 | 备注 |
|---|---|---|
| GPU 服务器 capex | 单卡 $25k–40k（代际/封装差异大） | 8 卡节点含 CPU/内存/网卡 |
| 网络 capex | 节点的 10%–30% | IB 交换机/光模块/线缆 |
| 存储 capex | 按 PB 计 | 热/冷分层 |
| 机房/电力改造 | 按 MW 计 | 配电、冷却、消防 |
| 折旧年限 | 3–5 年 | 会计/税务口径不同 |
| 残值 | 10%–30% | 代际更替快，残值不确定 |
| 融资成本 | WACC/租赁利率 | 现金流影响大 |
| 运维人力 | 每 100–500 卡 1 人 | 含平台/网络/存储 |
| 保险/合规/审计 | 按合同 | 常被漏算 |

一个简化的年化成本：

```text
annual = capex / life + power + network + storage + staff + financing + failures
$/GPU-hour = annual / (GPUs × 8760 × utilization)
```

融资/租赁（lease、SPV、vendor financing）改变的是现金流和风险分配，不改变
总算术；做决策时要同时看 NPV/IRR 和“提前终止/残值”情景，而不是只看
$/GPU-hour。

## I.5 训练规模锚点（公开数据）

这些数字来自公开论文/技术报告，用于校准数量级；复现时口径可能不同。

| 模型/系统 | 参数 | token | 硬件 | 公开训练成本/时间 | 备注 |
|---|---:|---:|---|---|---|
| GPT-3 | 175B | 300B | ~10k V100 | 约 3.14e23 FLOPs | 论文估算 |
| Megatron-Turing NLG | 530B | 270B | ~2k A100 | 2021 训练 | 3D 并行 |
| Llama 3 405B | 405B | 15T | ~16k H100 | 约 30.84M H100-hours | 论文披露 |
| DeepSeek-V3 | 671B（37B 激活） | 14.8T | 2048 H800 | 约 2.788M H800-hours | FP8，含 MoE/MLA |
| 70B 示例 | 70B | 2T | 1000 H100 | 约 0.59M GPU-hours @40% MFU | 本书估算 |

用这些锚点做 sanity check：

```text
Llama 3 405B: 6 × 405e9 × 15e12 = 3.645e25 FLOPs
30.84M H100-hours × 989e12 × 3600 = 1.098e26 FLOPs 容量
隐含 MFU ≈ 33%   （含失败/重试/数据等端到端）
```

如果自己的估算和公开数字差一个数量级，先查单位（B vs 参数个数、
小时 vs 秒、dense vs sparse），再查是否漏了 token 数或重算。

## I.6 成本公式速查

训练：

```text
FLOPs ≈ 6 · N_active · D
GPU-hours ≈ FLOPs / (P_dense · MFU · 3600)
成本 ≈ GPU-hours × 单价 × (1 + 失败/重试) + 数据/存储/人力
```

推理：

```text
$/1M output tokens ≈ (GPU $/h) / (decode_tokens/s · 3600) · 1e6 / 利用率
KV bytes ≈ 2 · L · n_kv · d · bytes · tokens
```

通信：

```text
DP 梯度 all-reduce 每卡 ≈ 2 · N · bpe · (dp-1)/dp
TP Row 前向 all-reduce 每卡 ≈ 2 · b · s · h · bpe · (tp-1)/tp
PP p2p 每步 ≈ 2 · m · b · s · h · bpe · (pp-1)
```

### I.6.1 检查点与存储成本

```text
checkpoint_bytes ≈ weights_bytes + optimizer_bytes
                 ≈ 16 B/param × N / 去重因子
retained = checkpoint_bytes × 保留个数
写入时间 ≈ checkpoint_bytes / 有效写带宽
年写入量 ≈ checkpoint_bytes × (训练小时 / 保存间隔)
```

“去重因子”只用于 tied embedding/复制参数被检查点去重的场景；普通分片
checkpoint 的总字节仍是**逻辑全量**，不随 TP/PP/DP 的 shard 数变化，不要
再乘 `shards`。70B、16 B/param：约 1.12 TB/全量检查点（未计 TP/PP 冗余）。每 30 分钟保存
一次、训练 30 天：`1.12 TB × 48 × 30 ≈ 1.6 PB` 写入量。对象存储的 PUT/GET
费用、跨区复制、版本保留策略都要算进去。

### I.6.2 推理的单位经济

```text
$/1M output tokens =
    GPU $/h / (output_tokens_per_s × 3600) × 1e6 / utilization

gross_margin = (price - cost) / price
```

`output_tokens_per_s` 要用**目标 SLO**（TTFT/TPOT/并发）下的 goodput，不是
峰值吞吐。分母的口径要和 `utilization` 一致：若 goodput 已按墙钟时间平均，
不要再除 `utilization`；若按忙碌时间平均，才除以它。prefix cache 命中率、
输入/输出比、batch 组成、KV 量化都会改变分母；同一张卡在不同 workload 下的
$/1M tokens 可以差一个数量级。

## I.7 如何引用本附录

- 写方案时注明：GPU 型号/封装、精度、dense/sparse、MFU 口径、单价来源/
  日期、是否含失败/重试；
- 不要把“示例区间”写成“市场价”；
- 跨代比较用“同精度 dense 峰值 + 同 workload 实测”，不要只看 TFLOPs；
- 对国产/非 NVIDIA 硬件，注明适配层（MindSpeed/ROCm/Neuron）和版本。

参数表会过时，**方法和口径**不会。每次做新方案，先用 I.6 的公式算一遍，
再用实测校准，最后把假设写进文档。

## I.8 参数来源与查询日期

本附录的数字分三类，引用时必须标注类别：

| 类别 | 例子 | 引用方式 |
|---|---|---|
| 厂商数据表 | GPU 显存/带宽/TDP | “厂商数据表，查询于 YYYY-MM-DD” |
| 公开论文/报告 | Llama 3 的 H100-hours、DeepSeek-V3 的 H800-hours | “论文/报告名 + 表/节” |
| 本书估算 | 70B/2T 的 GPU-hours、云价区间 | “本书估算，假设见正文” |

建议在你的方案文档里维护一张自己的 I.8 表：

```text
参数 | 值 | 类别 | 来源/链接 | 查询日期 | 适用 SKU/地区
```

跨代/跨厂商比较时，至少统一：精度、dense/sparse、封装（SXM/PCIe）、
显存/带宽、互联、功耗、软件版本。缺一项，结论就可能翻转。

---

上一附录：[附录 H 生产训练 Runbook 与验收清单](H-production-runbook.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 J 版本、路线图与声明登记表](J-claim-register.md)
