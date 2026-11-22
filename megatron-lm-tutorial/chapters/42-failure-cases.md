# 第 42 章 故障案例与规模化复盘

## 42.1 故障的分类

大规模训练的故障可以分为六类：

| 类型 | 典型表现 | 主要工具 |
|---|---|---|
| 硬件 | 掉卡、Xid、ECC、NVLink 降速 | nvidia-smi、DCGM、dmesg |
| 网络 | NCCL 超时/挂起、带宽下降 | NCCL_DEBUG、ibstat、perf |
| 软件 | 死锁、shape/ dtype 错误 | py-spy、堆栈、单测 |
| 数值 | NaN/Inf、loss spike | 逐层 hook、fp32 复现 |
| 数据 | 坏样本、重复、越界 token | 数据审计、采样检查 |
| 人为 | 配置错、版本漂移、误删 | 配置校验、审计日志 |

本章用 12 个案例说明“症状 → 根因 → 修复 → 预防”的完整链路。案例来自 mini 实现和生产经验的抽象，不是某一次具体事故的记录。

## 42.2 案例 1–4：四个 mini 实现 bug 的“规模化增量”

案例 1–4（PP+DP 死锁、Column Parallel 少 grad_input all-reduce、SP 序列
长度、CP K/V 梯度只 chunk 不 all-reduce）的逐行复现与修复已在
**27.5 节**给出；这里不重复代码，只补它们在规模化训练中的**放大方式**和
运维层面的预防。

| 案例 | mini 现象 | 规模化后的放大器 | 额外预防 |
|---|---|---|---|
| PP+DP 死锁 | 2 卡/4 卡立即卡住 | 千卡下 p2p 超时被调度器杀，日志分散在多个节点 | 用全局 rank 做 `src/dst`；启动时打印 rank 映射；p2p 配超时与心跳 |
| Column 漏 grad_input all-reduce | loss 对、梯度范数 0.68× | 小规模 loss 仍降，几周后才发现效果差，无法归因 | 每层 grad 范数快照；TP 等价性测试覆盖 MLP/embedding/output，不只 q/out_proj |
| SP 序列长度 reshape | 直接抛 shape 错误 | 换 batch/seq/TP 组合才触发，CI 覆盖不全时漏到生产 | 所有 reshape 用 `tensor.size()`；把 `(b,s,tp)` 组合纳入矩阵 |
| CP K/V 梯度少 all-reduce | k/v 梯度 0.85×、norm 0.74× | 长上下文训练“能跑但学不好”，评测掉点 | CP 测试比较 k/v 逐元素梯度；对 all-gather 反向画数据流图 |

共同结论：**loss 对不代表梯度对，梯度对不代表通信顺序对**。规模化后，
错误不会变成“更明显的报错”，而是变成“更贵的沉默”。

## 42.3 案例 5：MoE 路由塌缩

**症状**：MoE 训练几百步后，某个专家接收 90% 的 token；其他专家几乎空转；loss 先降后升；专家 GEMM 出现 OOM/尾延迟。

**根因**：

- 路由器初始化/学习率不合适；
- aux loss 系数太小；
- capacity factor 太小，token 被丢弃后路由更集中；
- FP8 下 router logits 精度不足；
- 数据分布单一。

**修复**：

- router softmax 用 fp32；
- 增加 aux loss/z-loss 或 router bias 调整；
- 调整 capacity factor；
- 监控每专家 token 数/概率均值/梯度；
- 小规模复现路由收敛。

**预防**：

- 训练前做路由均匀性 sanity check；
- 对 MoE 单独做数值/负载测试；
- 不要只看总 loss，要看专家负载分布。

## 42.4 案例 6：checkpoint 静默加载错

**症状**：从 checkpoint 恢复后 loss 跳变，但训练能继续；没有报错。

**根因**：

- 并行布局变了（TP 8→4），但代码用 `strict=False` 加载；
- 参数名不匹配，部分参数保持随机初始化；
- optimizer state 没加载；
- 数据 sampler 从头开始。

**修复**：

- 校验 metadata 的 TP/PP/CP/DP；
- `load_state_dict(strict=True)` 或显式检查 missing/unexpected；
- 校验 optimizer state 的 step；
- 保存/恢复 sampler 位置。

**预防**：

- checkpoint 写 metadata + 参数 hash；
- 恢复后对比前 N 步 loss；
- 定期做“恢复演练”；
- 不同布局用重分片工具，不要静默加载。

## 42.5 案例 7：NVLink 降速导致的 straggler

**症状**：训练吞吐逐渐下降 10–20%；某些 step 特别慢；GPU 利用率周期性掉坑；loss 正常。

**根因**：

- 某条 NVLink 降速/错误；
- 某张卡温度/功耗降频；
- IB 链路拥塞/重传；
- 某个 PP stage 成为瓶颈。

**排查**：

1. `nvidia-smi -q` 看链路/ECC/温度/功耗；
2. DCGM 看 Xid/PCIe/NVLink 指标；
3. Nsight Systems 看每 stage 的 step time；
4. NCCL_DEBUG 看拓扑/带宽；
5. 隔离慢节点，换卡/换线。

**预防**：

- 持续监控每卡/每 stage 的 step time；
- 定期跑 all-reduce microbenchmark；
- 故障节点自动隔离；
- checkpoint 间隔小于故障恢复成本。

## 42.6 案例 8：长上下文 OOM 的“假显存不足”

**症状**：序列从 8K 升到 32K 后 OOM，但理论计算“权重+KV 应该够”。

**根因**：

- attention 的 `s²` 临时矩阵未释放；
- CP all-gather 把完整 KV 复制到每个 rank；
- activation checkpointing 关闭；
- 碎片/峰值叠加；
- 数据 pipeline 的 pinned buffer/临时张量。

**排查**：

1. `torch.cuda.memory_summary()` 看 allocated/reserved；
2. 记录 `max_memory_allocated` 随 step 变化；
3. 二分关闭 SP/CP/recompute/优化器；
4. 用 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`；
5. 检查是否有未释放的计算图。

**修复**：

- 用 FlashAttention/分块 attention；
- 开 activation checkpointing；
- CP 用 ring/分块而不是 all-gather；
- 降 micro-batch/序列长度；
- 修碎片/释放临时张量。

## 42.7 案例 9/10：两个“看起来像死锁”的问题

### 42.7.1 案例 9：数据加载器假死

现象：训练 stable 运行数小时后，所有 rank 的 GPU 利用率周期性掉零，step
time 从 1.2s 涨到 30s，但日志没有报错，NCCL 也没有超时。

排查：

1. 看数据队列深度：长时间为 0；
2. `py-spy dump` 数据 worker：卡在存储 IO/锁；
3. 看存储监控：某个 shard 所在磁盘/对象存储限流；
4. 看 CPU：预处理线程 100%，但产出低。

根因：数据 shard 分布不均，部分 shard 在慢存储上；worker 数不足，prefetch
深度不够，训练被数据饿死。

修复：

- 增加 prefetch/worker；
- 把热数据缓存到本地 NVMe；
- 按 shard 做负载均衡/限速；
- 给数据队列加“低水位”告警。

教训：**不是所有停顿都是死锁**。先看“谁在等”，再看 collective。

### 42.7.2 案例 10：NCCL 超时被误判为死锁

现象：训练在 step 800 报 `NCCL timeout`，重启后同一位置再次发生；团队按
“死锁”排查 collective 顺序，没有发现问题。

排查：

1. `NCCL_DEBUG=INFO`：某条 IB 链路重传/降速；
2. `ibstat`：端口 error 计数增长；
3. 单节点 all-reduce 正常，跨节点异常；
4. 换节点/换链路后消失。

根因：坏链路导致集合通信超时，表现像死锁，实际是网络故障。

修复：

- 隔离坏节点/端口；
- 增加网络健康检查；
- 调整 NCCL 超时/重试策略；
- 在 runbook 中把“NCCL timeout”单列，不要直接归因于代码。

教训：**超时是症状，不是根因**。先区分“逻辑不匹配”和“物理链路慢/丢包”。

## 42.8 案例 11：EP combine 的索引方向写反

**症状**：EP=2 训练 loss 正常下降，但 `tests/parallel_ep.py` 的
`logits_diff` 是 0.025（容差 1e-5），专家梯度也有 1e-4 量级差异；本地
loss 与参考几乎一致。

**根因**：dispatch 时 rank r 收到的是“别人发给 r”的 token；combine 时要把
这些 token 发回**原发送者**。代码在截断返回块时用了
`counts_all[src][rank]`（src 发给 rank 的数量），正确值应是
`counts_all[rank][src]`（rank 发给 src 的数量）。当两个 rank 的流量不对称时，
截断错位，部分 token 的输出被丢弃/错配。

**为什么难发现**：

- 前向大部分 token 仍然对得上，loss 只偏一点；
- 本地 loss 不报错，梯度也不会 NaN；
- 只有对比**全局 logits/梯度**才会暴露。

**修复**：combine 的每个返回块按“自己发给该 peer 的数量”截断；dispatch 的
接收块按“该 peer 发给自己的数量”截断。两者是转置关系。

**预防**：

- EP 等价性测试必须让两个 rank 处理**不同长度/不同路由分布**的数据；
- 同时比较 loss、logits、aux 和梯度，不能只看 loss；
- 在 combine 前后断言 token 总数守恒。

## 42.9 案例 12：VPP 反向路由用错 owner

**症状**：interleaved VPP 反向时 rank 0 抛 `KeyError: (mb, 2)`，或 rank 之间
互相等待直到超时。

**根因**：接收梯度时，梯度的**发送方是 chunk+1**，不是当前 chunk。代码用
`owner(chunk)` 判断本地/远程，把本该从 rank 1 接收的梯度当成本地 buffer，
于是 `pop` 一个不存在的 key；如果改成阻塞 recv，则会等一个永远不会来的
tag。

**修复**：

- 接收 hidden：看 `owner(chunk-1)`（上游）；
- 接收梯度：看 `owner(chunk+1)`（下游）；
- tag 同时编码 `(mb, chunk)`，环形 wrap（rank p-1 → rank 0）也要覆盖。

**预防**：

- 先用 `validate_trace` 校验依赖，再跑真实通信；
- 用 PP=1 参考模型对比**所有层**的梯度，不只 loss；
- 在 `_take_hidden`/`_take_grad` 里打印 `(mb, chunk, src, tag)`，把“谁等谁”
  显式化。

## 42.10 事故响应 runbook

**0–5 分钟：止损**

- 确认影响范围（多少 rank/节点/任务）；
- 停止无效重试，保留现场（日志/堆栈/监控快照）；
- 如果 loss 已 NaN/发散，回滚到最近健康 checkpoint。

**5–30 分钟：定位**

- 看监控：GPU 利用率、step time、NCCL、显存、数据加载；
- 看日志：第一个报错/第一个 NaN/第一个超时；
- 用 py-spy/gdb 看卡住的 rank；
- 缩小到最小复现（world=2/单机）。

**30–120 分钟：修复/绕行**

- 隔离坏节点/坏数据；
- 回滚代码/配置/镜像；
- 调整并行/精度/超参；
- 从 checkpoint 恢复并观察 100 步。

**事后：复盘**

- 根因（不是“重启好了”）；
- 影响（GPU-hours/进度/成本）；
- 改进（测试/监控/流程）；
- 负责人/截止时间。

## 42.11 复盘模板

```text
标题：
日期/持续时间：
影响：任务/GPU-hours/进度/成本
时间线：
  - 事件发生
  - 检测
  - 响应
  - 恢复
根因：
  - 直接原因
  - 根本原因（流程/设计/测试）
修复：
预防：
  - 代码/测试
  - 监控/告警
  - 流程/评审
待办：负责人/截止时间
```

复盘的目标不是追责，而是让同类故障不再发生。

## 42.12 本章小结

- 故障分为硬件/网络/软件/数值/数据/人为六类，排查顺序不同。
- 死锁先查 collective/p2p 顺序与全局 rank。
- 数值问题先用 fp32 复现，再逐层/逐模块定位。
- checkpoint 要校验布局/完整性，不能静默加载。
- straggler/长上下文 OOM 要用监控+时间线定位，不要盲目调并行。
- 数据假死/NCCL 超时看起来像死锁，要先区分“逻辑不匹配”和“资源/链路故障”。
- EP/VPP 的静默 bug 多在索引/路由：combine 方向、owner(c±1)、`(mb,chunk)`
  键；必须用全局 logits/全梯度做回归。
- Runbook/复盘模板能把“救火”变成可积累的工程能力。

下一部分进入商业分析：市场、TCO、竞争与商业化。
