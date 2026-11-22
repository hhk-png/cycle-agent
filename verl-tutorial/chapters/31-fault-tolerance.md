# 第 31 章 容错、弹性、可观测性与故障排查

## 31.1 RL 训练的故障面

RL 比 SFT 多出很多故障面：

- rollout 引擎 OOM/超时；
- 工具/沙箱失败；
- reward 函数异常；
- 权重同步 hang；
- Ray actor 崩溃；
- NCCL timeout；
- 数据队列空/重复；
- checkpoint 写失败；
- 异步 staleness 失控。

这些故障中，有些会抛异常，有些只会让训练“变慢/变差”。

## 31.2 Ray 层的容错

verl 用 Ray actor 运行 worker。Ray 提供：

- actor 自动重启（取决于 `max_restarts`，**verl v0.9.1 默认没有配置**，所以实际行为通常是任务失败而不是原地恢复）；
- placement group 重调度；
- 任务重试；
- 对象存储。

要真正启用 actor 重启，必须在 Ray actor 选项中显式设置 `max_restarts`/`max_task_retries`，并让 worker 能重建状态；否则应把“失败 → 从 checkpoint 续训”作为默认恢复路径。RL 还需要额外处理：

- actor 重启后重新 `init_model`；
- 重新加入 NCCL 进程组；
- 恢复 optimizer/step；
- 重新同步 rollout 权重；
- 处理 in-flight 数据。

`WorkerGroup.start_worker_aliveness_check` 会监控 worker；死亡时主进程收到信号。生产环境应配合：

- 监控（Ray dashboard、Prometheus）；
- 日志聚合；
- 自动重启策略；
- 共享存储上的 checkpoint。

## 31.3 NCCL timeout

常见原因：

- 多机网络/IB 配置错误；
- 某个 rank 卡住/OOM；
- 进程组创建顺序不一致；
- `nccl_timeout` 太短；
- 防火墙/端口。

排查顺序：

1. 看哪个 rank 最后打印；
2. 检查 `NCCL_DEBUG=INFO`；
3. 检查 IB/RoCE 状态；
4. 检查 GPU 是否掉卡；
5. 缩小到单机复现；
6. 调大 timeout 只是缓解，不是修复。

## 31.4 rollout 故障

- vLLM/SGLang server 未就绪：健康检查、重试；
- 请求超时：超时/取消/重试；
- KV cache OOM：降并发/长度；
- 权重更新失败：版本校验、回滚；
- 输出为空/乱码：tokenizer/模板/权重。

Agent 多轮还多一层：

- 工具超时；
- 解析失败；
- observation 过长；
- 轮次超限。

## 31.5 reward 故障

- 规则函数异常：返回 0 + 记录；
- 沙箱超时：取消 + 惩罚；
- reward model 服务不可用：重试/降级；
- 奖励全 0/全 1：数据/验证器问题。

RewardLoop 要记录：

- 失败率；
- 重试次数；
- 延迟分布；
- 分数分布。

## 31.6 checkpoint 故障

- 写盘失败：重试/换路径；
- checkpoint 不完整：校验 hash/文件数；
- 恢复后 loss 跳变：优化器状态缺失；
- world size 变化：reshard；
- 异步保存不一致：暂停/快照。

## 31.7 可观测性

三类信号：

1. **指标**：reward、length、kl、clipfrac、grad_norm、throughput；
2. **日志**：配置、异常、样例、生成；
3. **追踪**：timing、profiler、NCCL、Ray。

v0.9.1 的 `global_profiler` 支持：

- nsys；
- torch profiler；
- torch memory snapshot；
- NPU profiler；
- precision debugger。

配置：

```yaml
global_profiler:
  tool: torch
  steps: [10, 20]
  save_path: outputs/profile
```

RL-Insight 是 verl 生态的在线可观测性项目，连接训练指标、RL 状态与服务 dashboard。

## 31.8 调试方法论

遇到“训练不收敛”，按以下顺序排查：

1. **数据**：打印 prompt/response/reward/mask；
2. **奖励**：在固定样本上回放 reward；
3. **优势**：均值/方差/是否全 0；
4. **log-prob**：rollout 与 actor 是否一致；
5. **比率**：clipfrac/ppo_kl 是否异常；
6. **梯度**：grad_norm 是否爆炸/为 0；
7. **权重同步**：rollout 是否用新权重；
8. **超参**：lr/clip/KL/batch；
9. **并行**：TP/PP/DP 是否影响数值；
10. **异步**：staleness/IS 是否失控。

## 31.9 最小复现

把问题缩小到最小配置：

- 单卡、单 worker；
- 小模型（0.5B）；
- 小数据（100 条）；
- 关闭 remove padding/动态 batch；
- 关闭异步；
- 固定 seed；
- 打印每一步的中间量。

MiniVerl 就是为“最小复现”设计的：它能在 CPU 上打印每个字段。

## 31.10 监控清单

生产环境至少监控：

```text
GPU 利用率、显存、温度、掉卡
Ray actor 状态、重启次数
rollout 队列长度、延迟、超时率
reward 失败率、分数分布
训练 loss、kl、clipfrac、grad_norm
权重同步字节数/耗时/版本
checkpoint 成功率/大小
验证指标/生成样例
```

## 31.11 MiniVerl 的故障注入

MiniVerl 的 `Worker.fail_once()` 可以注入一次失败；`tests/test_dispatch.py::test_failure_injection` 验证 group 会抛出异常。它没有自动重启，但演示了：

- 失败如何传播；
- 为什么需要重试/监控；
- 为什么 worker 状态要可恢复。

## 31.12 部署拓扑：Slurm、Kubernetes 与 Ray

verl 自己不规定集群调度器，它假设“Ray 已经能在目标资源上启动”。生产上有三种常见形态：

| 形态 | 适合 | 控制面 | 主要风险 |
|---|---|---|---|
| 单机多卡 | 开发/小模型 | 本地 Ray | 显存与端口冲突 |
| Slurm | 学术集群/超算 | `sbatch` + Ray | 作业排队、节点异构 |
| Kubernetes | 云上平台/多租户 | KubeRay/RayJob | 网络、存储、抢占 |

一个典型的 K8s 拓扑：

```text
[Ray head pod] ── [controller process]
      │
      ├── [actor worker pods]      GPU, FSDP/Megatron
      ├── [critic worker pods]     GPU（可选）
      ├── [rollout server pods]    GPU, vLLM/SGLang
      ├── [reward server pods]     GPU/CPU（可选）
      └── [shared PVC / object store]  parquet + checkpoints
```

关键设计点：

- **gang scheduling**：actor、critic、rollout 必须同时拿到资源，否则 Ray 会启动一半后卡住；K8s 侧用 gang scheduler 或 KubeRay 的 `RayCluster` 最小副本策略；
- **共享存储**：数据与 checkpoint 放 PVC/对象存储，不要放在某个 worker 的本地盘；
- **网络**：多机 NCCL 需要 RDMA/IB 或 RoCE；K8s 下通常要 hostNetwork、大页、正确的 MTU 与 NCCL 插件；
- **端口与发现**：Ray head 的 GCS 端口、dashboard、worker 端口要固定；跨节点用 head 的稳定 DNS；
- **资源隔离**：训练与 rollout 的 GPU 显存配额分开配置，colocate 时用 sleep/wake 而不是让两个进程抢显存。

Slurm 下可以用 `srun` 启动 Ray head/worker，再提交训练命令。v0.9.1 仓库提供了 `examples/tutorial/slurm/ray_on_slurm.slurm` 作为起点；多机配置与 Ray 调试见 `docs/start/multinode.rst` 与 `docs/start/ray_debug_tutorial.rst`。

部署顺序建议固定为：

1. 存储挂载与数据校验；
2. Ray head/worker 就绪；
3. 训练 worker 初始化模型；
4. rollout server 健康检查；
5. controller 开始采样；
6. 监控与日志管道确认。

把顺序写进启动脚本，比在故障时靠记忆排查可靠得多。

## 31.13 弹性、抢占与优雅退出

云上竞价实例和共享集群都会抢占。RL 作业要能在被抢占时“少丢一点”：

- **按 step 保存 checkpoint**，而不是按 epoch；RL 的 step 很贵，丢 10 步可能等于几小时；
- **SIGTERM 处理**：收到信号后停止采样、等待 in-flight update、保存、退出；不要直接 `kill -9`；
- **幂等恢复**：checkpoint 里要包含 step、optimizer、RNG、数据游标、rollout 版本；恢复后从下一个未完成 step 继续；
- **drain 模式**：先从调度器摘除，再让 worker 完成当前 step；
- **重试预算**：Ray actor 的 `max_restarts` 要有限制，避免坏配置无限重启烧钱；
- **RTO/RPO**：明确“最多丢多少 step、多久恢复”，并把它写进平台 SLO。

一个常见的错误是只保存模型权重，不保存数据加载器的位置。恢复后数据从头开始，训练分布会重复，指标看起来“突然变好”或“突然变差”。另一个错误是 checkpoint 写到一半被抢占，留下不完整的目录；用临时目录 + 原子 rename 可以避免。

## 31.14 故障树：从症状到根因

下面的表不是“万能答案”，而是一个排查顺序：每一行先给**可观测证据**，再给验证动作，最后才是修复。没有证据就改超参，通常是在制造第二个 bug。

| 症状 | 先看什么 | 可能的根因 | 验证动作 |
|---|---|---|---|
| reward 长期不变 | reward 分布、`reward/error_rate` | parser 不匹配、数据全对/全错、reward 返回 0 | 固定 100 条样本回放 reward |
| reward 涨、验证不涨 | 训练/验证长度、样例 | reward hacking、数据泄漏、过拟合 | 人工抽查 + 独立 parser |
| response length 爆炸 | `response_length/p99`、长度惩罚 | 长度奖励过高、缺少 overlong 惩罚 | 关掉长度项做消融 |
| KL 突然升高 | `actor/ppo_kl`、`ratio_max` | lr 太大、old logprob 不一致、权重不同步 | 固定 batch 对比 rollout/actor logprob |
| clipfrac 接近 1 | `actor/pg_clipfrac`、epochs | 更新太猛、约束解码 mask 不一致 | 降 lr/epochs，检查 allowed tokens |
| advantage 全 0 | `advantage_std`、组内 reward | 组内无差异、分组 key 错、过滤过度 | 打印一个 batch 的 uid/reward |
| rollout 输出不变 | `sync/version`、sync 字节 | 权重未同步、只同步部分 tensor、sleep 后旧副本 | 固定 prompt 对比更新前后输出 |
| 训练吞吐骤降 | timing 分解、GPU 利用率 | 长尾、NCCL、数据加载、reward 服务排队 | 分阶段计时，看 p99 |
| 周期性 OOM | 显存曲线、rollout 并发 | KV cache 峰值、checkpoint gather、内存碎片 | 降并发/开 sleep，抓 memory snapshot |
| 恢复后曲线跳变 | checkpoint 内容、数据游标 | 优化器/RNG/数据位置未恢复 | 对比恢复前后同 batch 的 loss |
| NCCL hang | 最后打印的 rank、`NCCL_DEBUG` | 拓扑/端口/掉卡/进程组顺序 | 单机复现，缩小 rank 数 |

三层归因：

1. **数据层**：prompt/response/mask/reward 是否合理？先打印，不要先看 loss。
2. **算法层**：advantage、ratio、KL、entropy、grad_norm 是否符合预期？
3. **系统层**：rollout 与 actor 的 logprob 是否一致？权重版本是否一致？队列是否重复/丢样本？

一个实用原则：**先证明“信号是对的”，再证明“优化是对的”，最后才证明“系统是快的”。** 顺序反了，就会在错误的层浪费几天。

## 本章小结

- RL 的故障面比 SFT 多：rollout、reward、同步、队列、异步。
- Ray 提供基础容错，但 RL 需要恢复模型/优化器/权重/队列。
- NCCL timeout 要查根因，不要只调大 timeout。
- 可观测性要覆盖指标、日志、追踪与样例。
- 部署拓扑决定故障模式：gang scheduling、共享存储、NCCL 网络与抢占处理要在启动脚本里固定。
- 恢复的完整性取决于 checkpoint 是否包含数据游标、RNG 与 rollout 版本，而不只是模型权重。
- 调试顺序：数据 → 奖励 → 优势 → log-prob → 比率 → 梯度 → 同步 → 超参。

## 延伸阅读

- Ray 容错文档：<https://docs.ray.io/en/latest/ray-core/fault_tolerance.html>
- `verl/utils/profiler/`
- RL-Insight：<https://github.com/verl-project/rl-insight>
- 附录 J：部署与集群运维（Slurm/K8s/Ray、存储、网络、抢占）
- 下一章：性能工程：吞吐、MFU、显存与调参方法论。
