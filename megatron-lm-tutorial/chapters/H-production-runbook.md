# 附录 H 生产训练 Runbook 与验收清单

> 第 32/33/42 章讲的是“为什么”和“出过什么事”；本附录讲“谁在什么时候做什么”。
> 它是一份可以直接改造成团队 SOP 的清单，适用于几十卡到几千卡的预训练/后训练
> 任务。阈值是经验值，不是行业标准；请按模型、硬件和业务 SLO 调整。

## H.1 角色与职责

大规模训练不是“一个算法工程师 + 一堆 GPU”，而是一个最小可运行的组织：

| 角色 | 职责 | 关键产出 |
|---|---|---|
| 训练负责人（Owner） | 目标、预算、优先级、最终决策 | 训练计划、验收标准 |
| 平台/Infra | 集群、调度、网络、存储、镜像 | 可复现环境、容量 |
| 性能工程师 | 并行策略、MFU、通信/算子优化 | 配置基线、性能报告 |
| 数据工程师 | 数据版本、混合、packing、污染检查 | 数据卡、token 账本 |
| SRE/值班 | 监控、告警、故障响应、恢复 | 事故记录、恢复时间 |
| 算法/研究 | 模型结构、超参、评测 | 收敛曲线、评测报告 |
| 安全/合规 | 数据/模型/许可证/出口 | 合规签字 |

小团队可以一人多角，但**值班与决策必须有人负责**；否则故障时会出现“大家都
在看日志，没人敢重启”的情况。

## H.2 启动前 48 小时

### H.2.1 配置与代码

```text
[ ] 代码 commit / 镜像 digest 已冻结，训练期间不再改
[ ] 配置 JSON/YAML 已版本化，命令行没有“手改参数”
[ ] TP/PP/DP/CP/EP 与 world size 整除关系已校验
[ ] micro/global batch、grad_accum、seq_len 已确认
[ ] 模型结构（层数/heads/GQA/MoE/MLA）与 checkpoint 匹配
[ ] 初始化/加载路径已确认（从零/继续/微调）
[ ] tokenizer 与词表 padding 已确认
```

### H.2.2 数据

```text
[ ] 数据版本/hash 固定，训练期间不换 shard
[ ] token 总数、doc 数、平均长度、语言/来源分布已核对
[ ] packing/document mask/position reset 已用小样本验证
[ ] 数据混合权重已固化
[ ] 污染检查已跑，报告已归档
[ ] 数据读取带宽能支撑目标 tokens/s（含 prefetch）
[ ] 断点续训的 sampler 状态可保存/恢复
```

### H.2.3 集群与网络

```text
[ ] GPU 型号/显存/驱动/CUDA 一致
[ ] NCCL 版本一致，IB/RoCE 健康检查通过
[ ] 节点内 NVLink/NVSwitch 拓扑符合预期
[ ] 跨节点带宽/延迟 baseline 已测（all-reduce/NCCL test）
[ ] 存储（本地 NVMe/并行文件系统/对象存储）容量与带宽足够
[ ] 调度器（Slurm/K8s）配额、优先级、抢占策略已确认
[ ] 故障节点隔离/替换流程已演练
```

### H.2.4 软件栈

```text
[ ] Python/PyTorch/CUDA/NCCL/TE/apex 版本锁定
[ ] Megatron Core / Bridge 版本与模型 recipe 匹配
[ ] 容器镜像可复现（digest，不是 latest）
[ ] 单机 smoke test 通过（前向/反向/保存/恢复）
[ ] 多机 smoke test 通过（2 节点×8 卡，至少 20 步）
[ ] 确定性/精度/checkpoint 兼容性已验证
```

### H.2.5 观测与告警

```text
[ ] loss/grad_norm/lr/tokens_per_sec 已上报
[ ] 每卡 GPU 利用率/显存/温度/功耗/ECC 已上报
[ ] NCCL 带宽/超时/重传已上报
[ ] 数据加载队列深度/IO 等待已上报
[ ] checkpoint 耗时/大小/成功状态已上报
[ ] 告警通道（IM/电话）与值班表已确认
[ ] 仪表盘链接、runbook 链接已放进训练任务描述
```

## H.3 启动日

### H.3.1 启动顺序

```text
1. 分配节点，确认没有坏卡/坏节点
2. 拉起容器，检查 CUDA/NCCL/IB 可见性
3. 跑 1 步 dry-run（小 batch/短序列），确认 forward/backward
4. 跑 20–50 步真实配置，检查 loss/grad_norm/吞吐
5. 保存一次 checkpoint，立刻做一次恢复演练
6. 确认恢复后 loss 连续，再放开长时间训练
7. 记录 baseline：step time、tokens/s、MFU、峰值显存
```

不要“先跑 10 万步再说”。启动日的 30 分钟验证，能省掉几天后的返工。

### H.3.2 启动检查表

```text
[ ] 所有 rank 的日志时间戳同步（NTP）
[ ] rank 0 的日志包含完整配置
[ ] 每个 stage 的参数量/显存符合预期
[ ] 第一步 loss 在合理范围（与参考/历史 run 对比）
[ ] grad_norm 没有爆炸/为 0
[ ] tokens/s 达到估算的 70% 以上（首日允许优化）
[ ] 没有 NCCL timeout/重传告警
[ ] checkpoint 写入路径可写、容量足够
```

## H.4 训练中监控

### H.4.1 核心指标与经验阈值

| 指标 | 正常 | 警告 | 严重 |
|---|---|---|---|
| loss | 平滑下降/平台 | 连续 20 步上升 >5% | NaN/Inf，或连续 100 步退化 |
| grad_norm | 稳定/缓慢变化 | 突然 >5× 基线 | >50× 基线或持续为 0 |
| tokens/s | 接近 baseline | 下降 10%–20% | 下降 >30% 或归零 |
| MFU | 接近基线 | 下降 >5 个点 | 下降 >10 个点 |
| GPU 利用率 | >85%（计算密集） | 70%–85% | <50% 持续 5 分钟 |
| GPU 显存 | <90% | 90%–95% | OOM/频繁重试 |
| GPU 温度 | <80°C | 80–85°C | >85°C 或降频 |
| NVLink/IB 带宽 | 接近峰值 | 下降 20% | 下降 50%/链路 down |
| 数据队列 | 持续有数据 | 间歇为空 | 长时间为空/IO 卡死 |
| checkpoint | 成功且耗时稳定 | 耗时翻倍 | 失败/校验不通过 |

阈值要按模型和硬件校准；上表只用于“先报警，再判断”。

### H.4.2 日志 schema

建议每条训练日志至少包含：

```json
{
  "ts": "2026-11-22T10:00:00Z",
  "run_id": "gpt-70b-20261122-a",
  "step": 1200,
  "rank": 0,
  "stage": 0,
  "event": "train_step",
  "loss": 2.31,
  "aux_loss": 0.012,
  "grad_norm": 0.87,
  "lr": 1.2e-4,
  "tokens_per_sec": 48211,
  "mfu": 0.41,
  "mem_gb": 71.2,
  "data_wait_ms": 3.1,
  "comm_ms": 8.4,
  "ckpt_ms": 0
}
```

结构化日志的价值在于：可以按 `run_id/step/rank/stage` 聚合，而不是 grep 一
堆非结构化文本。

### H.4.3 告警分级

| 级别 | 定义 | 响应 |
|---|---|---|
| P0 | 训练中断/数据损坏/安全事件 | 立即电话，15 分钟内有人响应 |
| P1 | 吞吐下降 >30% 或 loss 异常但仍在跑 | 30 分钟内响应，2 小时内给结论 |
| P2 | 单节点/单链路降级，有冗余 | 当班处理，24 小时内闭环 |
| P3 | 指标轻微漂移/容量预警 | 记录，迭代处理 |

告警必须带 runbook 链接和“最近一次变更”，否则值班人员只能从头排查。

## H.5 故障分级与决策树

### H.5.1 训练挂起

```text
是否所有 rank 都停？
├─ 是 → 看最后一条 collective/p2p；查 NCCL timeout/网络分区
│        → 查最近变更（schedule/group/tag/数据）
│        → 2 rank 能否复现？
└─ 否 → 看还在跑的 rank 在等谁；查 OOM/异常退出/数据加载
         → py-spy dump 所有 rank
```

处理原则：

1. 先保日志/栈/`nvidia-smi`/NCCL 状态，再重启；
2. 不要盲目 `kill -9` 所有进程，先确认 checkpoint 是否完整；
3. 重启后从最近有效 checkpoint 恢复，观察 100 步；
4. 如果同一 step 反复挂起，固定数据/并行度复现。

### H.5.2 loss NaN/Inf

```text
1. 找第一个 NaN 的 step/rank/layer
2. 用 fp32 + 小 batch 复现
3. 关 FP8/bf16、关 dropout、关重算，逐项排除
4. 检查 attention mask/CE ignore_index/数据越界/MoE 路由
5. 回滚到最近 checkpoint，跳过可疑 shard
6. 修复后加断言：loss/grad_norm/activation 有限
```

如果 NaN 只在多卡出现，优先查并行语义（漏 reduce、切错维度、RNG 不一致），
而不是先怀疑数据。

### H.5.3 loss spike

```text
spike 是否恢复？
├─ 是 → 记录 step/数据/lr/grad_norm；继续观察，必要时跳 shard
└─ 否 → 回滚；降 lr/增 warmup；检查精度/MoE/数据
```

不要用“重启并换 seed”掩盖问题；那会让事故不可复现。

### H.5.4 吞吐下降

```text
1. 对比 baseline：tokens/s、MFU、step time 分解
2. 看 GPU 利用率：是否有一张卡/一个 stage 拖后腿
3. 看通信：NCCL 带宽/重传/超时；是否跨 IB
4. 看数据：队列深度/IO 等待/预处理 CPU
5. 看显存：是否触发重算/碎片/降 batch
6. 看最近变更：代码/配置/数据/节点
```

常见根因：坏卡降频、NVLink 降速、IB 拥塞、数据 shard 在慢存储、某个 rank
负载不均、checkpoint 同步写盘。

### H.5.5 checkpoint 失败/损坏

```text
1. 不要删除旧 checkpoint
2. 校验 metadata/文件大小/hash
3. 尝试从上一个完整 checkpoint 恢复
4. 检查存储配额/权限/网络
5. 如果是异步保存，确认后台线程/进程已完成
6. 恢复后对比 loss/step/optimizer state
```

## H.6 检查点与恢复演练

每次启动新任务，至少做一次“保存 → 杀掉 → 恢复 → 对比”的演练：

```text
[ ] 保存 step N 的完整 checkpoint
[ ] 记录 step N 的 loss/grad_norm/lr/数据位置
[ ] 杀掉所有进程
[ ] 从 step N 恢复，跑到 N+10
[ ] 对比 N+1..N+10 的 loss 与未中断 run 的偏差
[ ] 确认没有重复/跳过数据
[ ] 确认 optimizer step/LR scheduler 一致
[ ] 记录恢复耗时
```

恢复后 loss 不连续时，按以下顺序查：

1. 优化器状态（Adam m/v、step）；
2. 数据 sampler（epoch/offset）；
3. RNG（全局/tracker/sampler）；
4. LR scheduler；
5. 并行布局/reduction 顺序；
6. missing/unexpected keys；
7. 精度/FP8 scale。

## H.7 变更管理

训练期间的高风险变更：

| 变更 | 风险 | 要求 |
|---|---|---|
| 改并行度 | 数值/检查点不兼容 | 重分片 + 等价性验证 |
| 改 batch/seq_len | 收敛/显存变化 | 小规模对比 + 回滚点 |
| 改数据混合 | 分布漂移 | 版本化 + 污染检查 |
| 改精度/FP8 recipe | NaN/loss 偏移 | bf16 对照 + amax 监控 |
| 改代码 | 不可复现 | 冻结 commit/镜像，走评审 |
| 换节点/网络 | 吞吐/死锁 | 先 smoke test |

原则：**一次只改一个变量，改完先跑短程对照，再放大。**

## H.8 成本与容量

训练期间每天至少看一次：

```text
GPU-hours 消耗 vs 计划
tokens/s / MFU vs 基线
故障/重试导致的浪费
checkpoint 存储增长
数据读取/存储成本
闲置/排队时间
```

一个实用的浪费公式：

```text
浪费 ≈ (1 - 实际MFU/目标MFU) × 算力成本
     + 故障重跑 GPU-hours × 单价
     + 闲置 GPU-hours × 单价
```

如果 MFU 长期低于目标 20%，优先做性能剖析，而不是继续加卡。

## H.9 收尾与复盘

训练结束/中止后：

```text
[ ] 保存最终 checkpoint 与配置/数据/环境元数据
[ ] 导出 loss/评测/资源曲线
[ ] 记录最终 token 数、GPU-hours、成本
[ ] 归档日志/告警/事故记录
[ ] 写复盘：目标 vs 结果、偏差原因、下次改进
[ ] 清理临时数据/释放配额（保留可复现所需的最小集合）
[ ] 更新 runbook：把本次新踩的坑写进去
```

## H.10 验收清单（Definition of Done）

一次“可交付”的大规模训练，至少满足：

```text
[ ] 配置/代码/数据/环境可复现
[ ] 单机与多机 smoke test 通过
[ ] 并行等价性/数值验证通过
[ ] 至少一次完整的 checkpoint 恢复演练
[ ] loss/评测达到目标，且与 baseline 可比
[ ] 吞吐/MFU 达到预算假设
[ ] 没有未解释的 NaN/spike/数据跳过
[ ] 监控/告警/runbook 已交付
[ ] 成本在预算内，或偏差有解释
[ ] 安全/合规/许可证检查完成
```

## H.11 本章小结

- 生产训练的核心不是“跑起来”，而是“可观测、可恢复、可复现、可验收”。
- 启动前 48 小时和启动日 30 分钟的检查，回报率最高。
- 监控要覆盖计算、通信、数据、显存、存储和成本。
- 故障按 P0–P3 分级，先保证据再重启。
- 每次新任务都做 checkpoint 恢复演练。
- 变更一次只改一个变量；收尾必须复盘并更新 runbook。

至此，原理、实现、生产、前沿、商业与运维形成了闭环。回到第 52 章，按你的
角色选择下一步的学习/落地路径。

---

上一附录：[附录 G 随机性、确定性与复现手册](G-determinism-rng.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 I 硬件、成本与规模参数手册](I-hardware-cost-parameters.md)
