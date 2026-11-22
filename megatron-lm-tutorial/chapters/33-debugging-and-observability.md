# 第 33 章 调试、数值稳定性与可观测性

## 33.1 三类问题

分布式训练的问题通常落在三类：

1. **挂起/死锁**：某个 collective/p2p 永远等不到；
2. **数值异常**：NaN/Inf/loss spike/梯度爆炸；
3. **性能退化**：吞吐下降、通信/数据加载变慢。

每一类有不同的工具和排查顺序。不要用“重启试试”作为唯一手段。

## 33.2 挂起/死锁

常见原因：

- 不同 rank 走了不同的 collective 顺序（条件分支）；
- p2p 的 src/dst 用了局部 rank；
- tag 冲突/未匹配；
- 某个 rank OOM/异常退出，其他 rank 在等它；
- NCCL 超时/网络分区；
- 数据加载卡住。

排查：

1. 设置 `TORCH_DISTRIBUTED_DEBUG=DETAIL`（PyTorch）；
2. `NCCL_DEBUG=INFO` 看拓扑/连接；
3. `py-spy dump` / `gdb` 看各 rank 栈；
4. 打印每个 rank 的 `(rank, group, op, tag, shape, step)`；
5. 缩小到 2 rank 复现；
6. 用 `comm` 超时（mini portable 有 180s）快速失败。

mini 的 Queue 超时会抛 `queue.Empty`，并打印栈；生产 NCCL 可能挂很久，需要
watchdog/心跳。

### 33.2.1 Watchdog 与超时

生产训练应至少有三层超时：

| 层 | 机制 | 作用 |
|---|---|---|
| 单次 collective | NCCL timeout / `TORCH_NCCL_BLOCKING_WAIT` | 避免永久挂起 |
| 训练 step | 心跳/step 超时 | 检测数据/计算停滞 |
| 任务级 | 调度器/看门狗 | 杀掉僵死任务并重启 |

注意：超时不是越短越好。网络抖动/大 checkpoint 可能造成短暂停顿；超时太短
会误杀。经验做法是记录 P99 step time，把超时设为它的数倍。

### 33.2.2 NCCL Flight Recorder 与“先取证再杀”

挂起时最忌讳的操作是直接 `kill -9`：所有 rank 的调用栈、未完成的 collective
和最后几个算子都会一起消失。正确顺序是**先取证，再决定是否杀**。

**第一层：进程栈。** `py-spy dump --pid <pid>` 不需要暂停进程；`gdb -p` 或
`faulthandler` 适合看 C++/Python 混合栈。对每个 rank 抓一次，按
`(rank, group, op, tag)` 对齐，才能看出谁在等谁。

**第二层：NCCL Flight Recorder。** PyTorch 2.x 的 Flight Recorder 在环形
缓冲区里记录每次 collective 的序号、group、算子、形状和耗时；超时时可以把
所有 rank 的缓冲区落盘，离线对齐。常用环境变量（名称/默认值随版本变化，
上生产前用当前版本文档核对）：

```bash
# 每个 rank 记录 1M 条（按内存预算调整；0 表示关闭）
export TORCH_NCCL_TRACE_BUFFER_SIZE=1048576
# 超时或手动触发时落盘（触发方式随版本，以文档为准）
export TORCH_NCCL_DUMP_ON_TIMEOUT=1
# 落盘路径前缀；最终文件名会带 rank
export TORCH_NCCL_DEBUG_INFO_TEMP_FILE=/tmp/nccl_trace_rank_
# 记录 C++ 栈（开销更大，复现期再开）
export TORCH_NCCL_TRACE_CPP_STACK=0
```

离线分析时，把各 rank 的 dump 按“同一个 collective 序号”对齐，重点看：

- 哪个 rank 先进入集合通信、哪个最后进入（等待差）；
- 是否有一个 rank 根本没调用同一个 op（条件分支/异常退出）；
- 同一个 group 的 op 顺序是否在某个 rank 上不同；
- 超时前最后一个完成的 op 与第一个未完成的 op。

这比 `NCCL_DEBUG=INFO` 的文本日志更结构化；两者可以同时开，但 `INFO` 在
大集群上会产生巨量日志，建议只在小规模复现时开，并限制
`NCCL_DEBUG_SUBSYS=INIT,COLL,NET`。

**第三层：硬件与驱动。** 很多“NCCL 挂起”其实是链路降速或 GPU 掉卡：

```bash
nvidia-smi -q -d ECC,XID          # ECC 错误、Xid
nvidia-smi --query-gpu=index,temperature.gpu,utilization.gpu,memory.used \
  --format=csv
dmesg -T | grep -i -E 'xid|nvrm|link'   # 需要 root/权限
ibstat / ibv_devinfo              # IB 端口状态
ethtool -S <iface> | grep -i -E 'err|drop'  # RoCE 丢包
```

Xid 13/31/43/48/79 等通常指向 GPU/驱动/显存/链路问题；单卡 Xid 会让整个
collective 卡住，但其他 rank 的日志里只看到“在等”。把 `nvidia-smi -q` 的
输出和 rank 映射一起保存，才能把“哪个物理 GPU”与“哪个 rank”对上。

**第四层：可控复现。** 如果只能在特定 world/step 复现，缩小到最小拓扑，
固定数据与 seed，打开 `TORCH_DISTRIBUTED_DEBUG=DETAIL` 和
`TORCH_NCCL_BLOCKING_WAIT=1`（或 `TORCH_NCCL_ASYNC_ERROR_HANDLING` 的新
等价配置），让错误尽早以异常形式抛出，而不是无限等待。

一个实用的值班顺序：

```text
1. 确认训练是否真的停（step 是否前进，GPU 利用率是否归零）
2. 抓所有 rank 的栈 + Flight Recorder（不要先杀）
3. 看是“全停在同一个 collective”还是“部分 rank 在跑”
4. 对照 nvidia-smi / dmesg / IB 计数，排除硬件
5. 用 2 rank 或单节点复现；能复现再二分代码/配置/数据
6. 保存现场后，才重启并从最近 checkpoint 恢复
```

## 33.3 NaN/Inf

定位顺序：

1. 关闭 FP8/bf16，用 fp32 复现；
2. 检查 attention mask 是否有整行 `-inf`；
3. 检查 CE 的 ignore/0 token；
4. 检查数据越界/空样本；
5. 检查梯度 norm 是否在某个 step 爆炸；
6. 检查 MoE 路由是否 NaN/全选一个专家；
7. 检查 checkpoint 是否加载错（missing keys）。

工具：

- `torch.autograd.set_detect_anomaly(True)`（慢，但能定位反向 NaN）；
- 逐层 hook 检查 activation/grad 的 min/max；
- 记录每层 grad norm；
- 保存异常 step 的输入/输出。

## 33.4 Loss spike

处理流程：

1. 记录 spike 前后的 lr/grad norm/data；
2. 回滚到最近 checkpoint；
3. 跳过可疑数据 shard；
4. 降低 lr/增加 warmup；
5. 检查精度/并行度变化；
6. 对 MoE 检查路由/aux；
7. 复现时固定 seed/并行度。

有些 spike 是“良性”的（loss 短暂上升后恢复），有些会导致不可逆退化。需要
根据梯度/权重统计判断，而不是只看 loss。

## 33.5 可观测性

生产训练至少监控：

- loss/grad norm/lr；
- tokens/s、MFU；
- 每卡显存/利用率/温度/功耗；
- NCCL 通信时间/带宽；
- 数据加载时间/队列深度；
- 每 stage 的 step time；
- MoE 专家负载；
- 检查点耗时/大小；
- 故障/重启次数。

日志要带 `step/rank/stage/event` 结构化字段，便于聚合和告警。

### 33.5.1 日志 schema

```json
{
  "ts": "2026-11-22T10:00:00Z",
  "run_id": "run-20261122-a",
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

不要只打 `print(loss)`：没有 step/rank/时间戳的日志无法聚合，也无法做告警。

### 33.5.2 告警设计

本章不另立一套阈值。**唯一权威表在附录 H.4.1**，这里只给映射到优先级的
摘要，避免两处数字打架：

| 告警 | 对应 H.4.1 的“严重/警告” | 优先级 |
|---|---|---|
| loss NaN/Inf，或连续 100 步退化 | 严重 | P0 |
| 训练无进展（step 不前进） | 严重（挂起） | P0 |
| loss 连续 20 步上升 >5% | 警告 | P1 |
| tokens/s 下降 >30% 或归零 | 严重 | P1 |
| grad_norm >50× 基线或持续为 0 | 严重 | P1 |
| GPU 温度 >85°C 或 ECC 错误 | 严重 | P1 |
| 显存 90%–95%（警告）/ OOM（严重） | 警告/严重 | P1/P2 |
| checkpoint 失败或校验不通过 | 严重 | P1 |
| 数据队列长时间为空 | 严重 | P2 |

告警要带 runbook 链接和最近变更（代码/配置/数据/节点），否则值班人员只能
从头排查。阈值要按模型/硬件校准；上表只用于“先报警，再判断”。

## 33.6 从告警到定位：三个分叉

具体的“症状 → 命令 → 修复”清单在附录 D，故障案例在第 42 章，生产决策树在
附录 H.5。本节只讲**如何用观测信号决定往哪查**。

### 分叉 1：所有 rank 都停，还是只有一部分？

- **所有 rank 利用率归零、都在 collective**：大概率是死锁/超时/group 不匹配。
  查 group 创建顺序、tag、global rank、最近是否改了并行度。
- **部分 rank 在跑、部分在等**：查 p2p 的 src/dst/tag、数据加载、straggler。
- **只有 CPU 忙、GPU 闲**：数据管线/检查点/日志阻塞。

### 分叉 2：可复现，还是偶发？

- **固定 step/固定数据可复现**：数值/代码 bug。用 1 卡 vs 多卡、fp32、关闭
  dropout 逐层对比。
- **随机 step/随机节点偶发**：硬件/网络/温度/ECC。查 NCCL 日志、IB 计数器、
  Xid、ECC、NVLink 降速。
- **只在特定 world/并行度复现**：group/rank 映射或 reduction 顺序问题。

### 分叉 3：最近有变更，还是“一直如此”？

- **有变更**：先回滚到上一个绿色 checkpoint/配置，做二分（代码/配置/数据/
  节点）。
- **无变更但逐渐变差**：容量/泄漏/数据漂移/存储写满/网络退化。看长期趋势，
  不是单点。

三个分叉的组合决定下一步：

```text
全停 + 可复现 + 有变更     → 代码/配置回归
全停 + 偶发 + 无变更       → 网络/NCCL/节点
部分停 + 可复现 + 有变更   → p2p/调度/数据 shard
部分停 + 偶发 + 无变更     → straggler/硬件
```

这张表不能替代证据，但能避免“一上来就 `py-spy` 所有 rank”的盲目排查。

## 33.7 确定性调试

如果问题只在多卡出现：

- 固定 seed；
- 关闭 dropout；
- 用相同权重/数据对比 1 卡/多卡；
- 用 `parallel_equiv.py` 的方法；
- 检查 reduction 顺序/精度；
- 用 `TORCH_DISTRIBUTED_DEBUG`。

如果问题只在某个 step 出现：

- 保存该 step 的输入；
- 逐层对比 forward/backward；
- 检查 optimizer state；
- 检查数据 sampler。

## 33.8 本章小结

- 挂起先看 collective/p2p 顺序、rank/tag、NCCL 状态。
- NaN 先用 fp32 复现，再逐层/逐模块定位。
- loss spike 要记录/回滚/跳数据/调 lr，并检查精度/MoE。
- 可观测性要覆盖计算、通信、数据、显存、故障。
- 排障 runbook 能显著缩短恢复时间。

下一部分进入前沿：HybridModel、DeepSeek 风格 MoE/MLA、1M 上下文、FP4、
Muon。
