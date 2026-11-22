# 第 40 章 MiniVerl 到生产 verl 的差距与迁移

## 40.1 差距地图

| 维度 | MiniVerl | 生产 verl |
|---|---|---|
| 模型 | TinyLinear/Transformer | HF/Megatron 7B～671B |
| 训练后端 | dense replica + 梯度平均 | FSDP/FSDP2/Megatron/... |
| 推理 | 逐条 NumPy 采样 | vLLM/SGLang/TRT-LLM |
| 分布式 | 同进程 rank 模拟 | Ray + NCCL |
| 数据 | 合成数据 | parquet/多模态/多轮 |
| 算法 | PPO/GRPO/DAPO/GSPO/RLOO/RF++/ReMax/SFT/DPO | 同 + VAPO/PRIME/SPPO/... |
| 异步 | 版本化 buffer + 单进程 TransferQueue | 分布式 TransferQueue + fully async |
| DAPO 采样 | 只过滤当前 batch（不补采样） | 过滤 + 逐 prompt 精确补采 + `max_inflight_gen_batches` 限并发 |
| Agent 训练 | 轨迹/mask 演示，未接入 PPO reward 链路 | AgentLoop + 多轮 RL + turn-level 优势 |
| 过程奖励 | PRM 训练/服务 + 轨迹级聚合；步骤级 GAE 仅文档 | 自定义 PRM reward server + 步骤级优势/搜索 |
| TP/PP 布局 | 数值模拟/往返，不是 Megatron 真实布局 | 真实 DTensor/MCore 布局 |
| 容错 | fail_once | 任务失败 + checkpoint 续训、监控（Ray actor 默认不自动重启） |
| 性能 | 教学 | 千卡吞吐 |
| 正确性 | 250 测试 | 大规模 e2e + 硬件验证 |

## 40.2 哪些知识可以直接迁移

- `DataProto` 的三部分结构与 chunk/concat/padding 语义；
- dispatch 模式与 DP_COMPUTE_PROTO；
- actor/critic/ref/reward 的 RPC 形状；
- PPO/GRPO 的计算顺序与 mask 规则；
- 权重同步/reshard 的字节账；
- checkpoint/merge 的生命周期；
- 异步 staleness 与 IS 修正；
- 指标口径与调试顺序；
- PRM 的步骤标签、稳定 BCE、聚合选择与“PRM-only 会被 hack、要与 verifier 组合”的结论。

这些是“架构级”知识，不随模型规模变化。

## 40.3 哪些必须重写

- 模型：MiniVerl 的 TinyTransformer 不能加载 HF 权重；
- 训练：dense replica 不能扩展到 7B；
- 推理：NumPy sampler 没有 KV cache/continuous batching；
- 数据：合成数据要换成 parquet/processor；
- 并行：TP/PP/EP 需要真实 NCCL/DTensor；
- 容错：同进程 fail_once 不是 Ray/NCCL 故障。

## 40.4 迁移路线

### 第一步：在 MiniVerl 上验证算法/数据逻辑

- 用自定义 reward、数据集、算法配置跑通；
- 确认 mask/advantage/loss 的语义；
- 记录指标基线。

### 第二步：在真实 verl 上跑小模型

- 0.5B～1.5B，单卡/8 卡；
- `trainer_mode=sync`；
- `rollout.name=vllm`；
- 关闭 remove padding/动态 batch，先确认正确性；
- 对比 MiniVerl 的指标口径。

### 第三步：加规模

- 7B～32B；
- FSDP2/Megatron；
- TP/PP/EP；
- 动态 batch/remove padding；
- 异步。

### 第四步：生产化

- 监控/告警；
- 容错/断点续训；
- 多租户/配额；
- 成本优化；
- 安全/合规。

## 40.5 代码映射表

| MiniVerl | 生产 verl |
|---|---|
| `PPOTrainer.train_step` | `verl/trainer/ppo/v1/trainer_base.py::_step_once` |
| `ActorRolloutRefWorker.compute_log_prob` | `verl/workers/engine_workers.py::ActorRolloutRefWorker.compute_log_prob` |
| `Engine.forward_backward_batch` | `verl/workers/engine/base.py::BaseEngine.forward_backward_batch` |
| `NaiveRollout` | `verl/workers/rollout/vllm_rollout` |
| `CheckpointEngineManager` | `verl/checkpoint_engine/base.py::CheckpointEngineManager` |
| `filter_groups_for_dapo` | `verl/trainer/ppo/v1/replay_buffer.py` + `filter_groups` |
| `compute_gae` | `verl/trainer/ppo/core_algos.py::compute_gae_advantage_return` |
| `DataProto` | `verl/protocol.py::DataProto` |
| `ProcessRewardManager` | reward loop + 自定义 PRM reward server（无内置 PRM 训练器） |
| `build_step_samples` / `PRMTrainer` | 自定义 PRM 数据集与训练 recipe |

## 40.6 生产检查清单

- [ ] 数据：字段、mask、reward、去重、难度分布
- [ ] 模型：tokenizer/processor 版本、dtype、并行配置
- [ ] 训练：FSDP/Megatron、显存、吞吐、稳定性
- [ ] rollout：vLLM/SGLang、TP、prefix cache、超时
- [ ] 同步：backend、字节数、版本、sleep/wake
- [ ] 算法：advantage/loss/KL/clip、DAPO 过滤
- [ ] 验证：pass@k、生成样例、人工抽查
- [ ] 容错：checkpoint、重启、NCCL timeout
- [ ] 可观测：指标、日志、profiler、告警
- [ ] 成本：GPU 小时、tokens/s、ROI

## 40.7 常见迁移错误

1. 把 MiniVerl 的 `train_batch_size` 语义直接套到 verl（prompt vs 序列）；
2. 忘记 `rollout.n` 对总序列数的影响；
3. 用 MiniVerl 的 mask 逻辑处理 chat template（多轮更复杂）；
4. 以为 `naive` 同步能支撑大规模；
5. 忽略权重同步字节数；
6. 在异步模式下不做 IS 修正；
7. 用训练 reward 代替验证指标。

## 40.8 学习路径

1. 读第 1～5 章：问题、算法与架构；
2. 读第 6～8 章并跑 MiniVerl 的 26 个示例：安装、配置、数据；
3. 读第 9～14 章：协议/控制器/引擎/同步；
4. 跑真实 verl 的 0.5B PPO/GRPO；
5. 读第 15～21 章：rollout/reward/主循环；
6. 读第 22～27 章：动态批/checkpoint/Agent/异步，并做一次小规模 DAPO/多轮实验；
7. 读第 28～30 章：SFT/DPO/多模态/LoRA；
8. 读第 31～33 章：容错/性能/对比；
9. 回到第 34～40 章：把 MiniVerl 当调试工具，并做迁移验收；
10. 读第 41～45 章与附录 R：生态、未来、商业与交付。

## 40.9 迁移验收：差分、指标与灰度

迁移不是“能跑起来”就结束。至少要通过四道验收：

### 1. 数值差分

| 对比 | 方法 | 通过标准 |
|---|---|---|
| MiniVerl ↔ verl（同公式） | 固定 batch，比 advantage/loss | 逐元素接近（浮点误差内） |
| HF ↔ 训练引擎 | 固定 prompt，比 logits | `atol=1e-4` 量级 |
| 训练引擎 ↔ rollout 引擎 | 同 prompt/温度，比 logprob | 差异可解释且稳定 |
| TP/PP ↔ dense | 同权重，比输出 | 误差随规模不增长 |

差分测试要保存“输入 → 期望输出”的 golden 文件，而不是每次重新生成期望值；否则测试会跟着 bug 一起漂移。

### 2. 指标口径

- `train_batch_size` 是 prompt 数还是序列数？
- loss 是 token-mean 还是 seq-mean？
- pass@k 的 n 与温度是否一致？
- reward 是放在最后一个 response token 还是最后一个 assistant token？

口径不一致时，曲线“看起来差不多”，但结论可能相反。把口径写成文档，并在评测脚本里断言。

### 3. 吞吐与资源

- 同模型/同数据/同序列长度下，tokens/s 与 step/s 是否达到预期？
- 显存峰值是否留有余量（建议 ≤85%）？
- 权重同步占 step 时间多少？
- 故障恢复时间（RTO）与丢失步数（RPO）是否可接受？

### 4. 灰度与回滚

上线前用 1%～5% 流量灰度：

- A/B 对比基线模型；
- 监控质量、延迟、成本、安全；
- 准备一键回滚到旧 checkpoint；
- 回滚后验证旧模型指标与之前一致。

灰度不是“跑通推理”的同义词。它要能回答：新模型在真实分布上是否更好？坏案例是否可接受？出问题时多久能切回？

## 40.10 从单机到多机：资源规划

| 阶段 | 典型资源 | 关键约束 | 不要做什么 |
|---|---|---|---|
| 语义验证 | 1×GPU（或 CPU MiniVerl） | 正确性 | 不要上多机 |
| 小模型 | 8×GPU | 显存/吞吐 | 不要开 fully async |
| 中模型 | 8～32×GPU | TP/PP/DP 组合 | 不要同时改算法与并行 |
| 大模型/MoE | 64～512×GPU | EP/网络/存储 | 不要没有 checkpoint 就开跑 |
| 多租户平台 | 多集群 | 隔离/配额/公平 | 不要让一个任务占满队列 |

规划顺序：

1. 用第 32 章的显存/通信公式估算；
2. 先跑单机多卡，确认线性扩展效率；
3. 再加节点，观察 NCCL/IB 与数据加载；
4. 最后才加异步/弹性。

一个经验法则：**当且仅当 rollout 的长尾已经成为可测量的瓶颈时，才引入异步。** 否则异步只会增加调试维度。

## 本章小结

- MiniVerl 与生产 verl 的差距主要在模型、后端、分布式、推理、容错。
- 架构级知识（DataProto/dispatch/主循环/同步/指标）可以直接迁移。
- 迁移路线：MiniVerl 验证语义 → 真实小模型 → 加规模 → 生产化。
- 生产检查清单覆盖数据、模型、训练、rollout、同步、算法、验证、容错、可观测、成本。
- MiniVerl 是调试工具与教学实现，不是生产框架。

## 延伸阅读

- `README.md` 的验证状态
- `VERIFICATION.md`（如果本仓库提供）
- 下一章：verl 生态、版本演进与未来方向。
