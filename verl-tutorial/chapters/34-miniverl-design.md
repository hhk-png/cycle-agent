# 第 34 章 MiniVerl 设计：范围、确定性与映射

## 34.1 目标与非目标

MiniVerl 的目标：

1. 用纯 Python + NumPy 复现 verl 的**系统契约**：DataProto、dispatch、worker group、engine、rollout、权重同步、PPO/GRPO 主循环；
2. 每一步可单步调试、可打印、可测试；
3. 在 CPU 上几分钟内完成端到端训练；
4. 代码结构与真实 verl 一一对应，读完能迁移。

非目标：

- 训练真实大模型；
- 追求吞吐/显存效率；
- 复现 vLLM/SGLang 的推理性能；
- 支持所有后端/多模态。

MiniVerl 是“可运行的架构说明书”，不是生产框架。

## 34.2 模块地图

```text
mini_verl/
├── autograd.py          # 反向模式自动微分
├── nn.py                # Linear/Embedding/LayerNorm/Attention/Transformer
├── protocol.py          # DataProto
├── config.py            # PPOConfig + key=value 覆盖
├── data.py              # CharTokenizer + 合成数据集 + collate
├── rewards.py           # 规则奖励、RewardManager、RewardLoop
├── reward_model.py      # 偏好对/BT loss/校准/检查点/ModelRewardManager
├── losses.py            # actor/critic/sft/dpo/kl/distill loss
├── engine.py            # BaseEngine/FSDPEngine/AdamW/显存估算
├── parallel.py          # TP/PP/MoE/Ulysses 模拟
├── sharding.py          # FSDP/TP shard + CheckpointEngine
├── rollout.py           # NaiveRollout/TPRollout/PDDisaggregatedRollout/AsyncRolloutServer
├── transfer_queue.py    # 有界、版本化、可快照的生产/消费队列
├── workers.py           # ActorRolloutRefWorker/CriticWorker
├── async_policy.py      # ReplayBuffer + rollout correction
├── tools.py             # ToolSchema/BaseTool/FunctionTool/ToolRegistry
├── agent.py             # AgentLoop + CalculatorTool
├── checkpoint.py        # CheckpointManager + optimizer state + safetensors
├── metrics.py           # 指标/pass@k/日志
├── algo/core_algos.py   # GAE/GRPO/RLOO/RF++/ReMax/KL/policy loss
├── single_controller/   # Worker/WorkerGroup/Dispatch
└── trainer/             # PPO/SFT/DPO/RM/EVAL + CLI
```

## 34.3 与真实 verl 的映射

| 真实 verl | MiniVerl | 说明 |
|---|---|---|
| `verl/protocol.py::DataProto` | `protocol.py::DataProto` | 三部分结构 |
| `single_controller/base/decorator.py` | `single_controller/decorator.py` | Dispatch 枚举 |
| `single_controller/ray/base.py` | `single_controller/worker_group.py` | 本地 dispatch |
| `workers/engine_workers.py` | `workers.py` | Actor/Critic |
| `workers/engine/base.py` | `engine.py::BaseEngine` | 训练引擎 |
| `workers/engine/fsdp` | `engine.py::FSDPEngine` | dense replica |
| `workers/rollout/vllm_rollout` | `rollout.py::NaiveRollout` | 简化采样 |
| `workers/rollout/*_pd_replica.py` | `rollout.py::PDDisaggregatedRollout` | P/D + KV 传输账 |
| `utils/transferqueue_utils.py` | `transfer_queue.py::TransferQueue` | 有界/版本化队列 |
| `trainer/ppo/v1/trainer_base.py` | `trainer/ppo_trainer.py` | 主循环 |
| `trainer/ppo/core_algos.py` | `algo/core_algos.py` | 算法 |
| 外部 RM 训练管线 | `reward_model.py` | 偏好对/BT/校准/服务，教学版 |
| `checkpoint_engine/` | `sharding.py::CheckpointEngineManager` | 同步 |
| `model_merger/` | `checkpoint.py` | 导出 |
| `tools/` | `tools.py` | schema/生命周期/注册表 |
| `experimental/agent_loop/` | `agent.py` | 多轮 |
| `experimental/fully_async_policy/` | `async_policy.py` | 队列/IS |
| `utils/megatron/router_replay_*` | `parallel.py::replay_*` | 记录/重放/层序校验 |

## 34.4 确定性

MiniVerl 的设计原则：

- 所有随机数用显式 seed；
- 不用线程/进程（除 RewardLoop 的线程池）；
- 不用 GPU/非确定性 kernel；
- 模型初始化按 seed 固定；
- 数据 shuffle 按 seed 固定；
- 采样按 rng 固定。

因此：

- 同一配置、同一 seed，结果可复现；
- 测试可以断言精确数值；
- 训练曲线不会因硬件差异漂移。

真实 verl 的确定性要难得多（GPU kernel、NCCL、continuous batching），第 16 章讨论过。

## 34.5 性能取舍

MiniVerl 的速度来自“模型小”：

- bandit 用线性策略（约 6k 参数）；
- 默认 1 个 worker、1 个 episode 几十步；
- 40 步训练约 1～10 秒。

如果换成 Transformer（64 dim、2 层），速度会慢一个数量级；如果用真实 7B 模型，CPU 不可行。

这不是缺陷，而是教学设计：**把系统正确性与模型能力解耦**。

## 34.6 任务设计

MiniVerl 提供四个合成任务：

| 任务 | prompt | answer | 用途 |
|---|---|---|---|
| `bandit` | `=A` | `x` | 四动作 contextual bandit，快速验证 RL |
| `copy` | `ax=` | `ax` | 序列复制 |
| `arithmetic` | `3+4=` | `7` | 简单算术 |
| `math` | 模板应用题 | `\boxed{n}` | 规则数学奖励 |

bandit 使用受限解码（`allowed_chars="xyzw"`）和线性策略，使 GRPO 在几十步内可学到 100%。这是为了验证 PPO/GRPO 实现，而不是展示模型能力。

## 34.7 一次运行的组件

`PPOTrainer(config).init()` 会构造：

1. `CharTokenizer`
2. `RLHFDataset` + `make_batches`
3. `LocalWorkerGroup(ActorRolloutRefWorker)`
4. `LocalWorkerGroup(CriticWorker)`（如果 GAE）
5. `NaiveRollout` / `TPRollout` / `PDDisaggregatedRollout`
6. `CheckpointEngineManager`
7. `RewardManager` / `DAPORewardManager`
8. `MetricsLogger`
9. `CheckpointManager`（模型 + 优化器状态）
10. 可选 `ReplayBuffer`（异步）与 `TransferQueue`（示例/扩展）

`fit()` 循环调用 `train_step()`。

## 34.8 验证方式

```bash
python -m pytest tests/ -q          # 250 passed
python -X utf8 examples/run_all.py  # 25 examples
python -X utf8 check_docs.py        # 文档自检
```

测试覆盖：

- autograd 有限差分；
- 算法公式；
- DataProto；
- dispatch；
- FSDP/TP shard；
- rollout/checkpoint（含 PD、delta 部分更新、优化器恢复）；
- PPO/GRPO/CISPO 端到端；
- SFT/DPO/Agent/异步；
- 独立评测（checkpoint 重载、pass@k）；
- TransferQueue 部分取用/背压/快照；
- 奖励模型（BT 数值稳定、mask、校准、检查点、PPO 接入）。

## 34.9 如何扩展 MiniVerl

建议的扩展点：

1. 新任务：在 `data.py::build_dataset` 加分支；
2. 新算法：在 `algo/core_algos.py` 加估计器，在 `compute_advantage` 分发；
3. 新 loss：在 `losses.py` 加函数，在 worker 选择；
4. 新后端：在 `engine.py` 加 Engine；
5. 新 rollout：在 `rollout.py` 加类；
6. 新并行：在 `parallel.py` 加模拟；
7. 新 reward：`load_custom_reward("module:function")`；
8. 新工具：继承 `BaseTool` 或加 `@function_tool`，注册到 `ToolRegistry`；
9. 新路由策略：在 `moe_route` 上加 replay/记录分支，或扩展 `router_replay_report`；
10. 新数据面：实现 `TransferQueue` 的跨进程/共享内存版本；
11. 新 rollout 拓扑：在 `PDDisaggregatedRollout` 上接入真实的 KV 传输后端；
12. 新奖励模型：在 `reward_model.py` 加 backbone/pooling/损失变体，并同步 `ModelRewardManager` 的 checkpoint 元数据；
13. 新 PRM：在 `prm.py` 加步骤切分器/标签策略/聚合方式，并同步 `ProcessRewardManager` 的边界与 checkpoint 元数据（现有实现见 17.17）。

每次扩展都要加测试，并更新 `check_docs.py` 的硬数字（如果文档提到测试数）。

## 34.10 覆盖矩阵：verl 能力 × MiniVerl 实现级别

“包含 verl 的全部内容”在单机 NumPy 教学实现里不可能逐项等价。诚实的做法是把每项能力标成四个级别之一：

- **实现**：有可运行的代码路径，测试断言其数学/契约；
- **模拟**：保留了接口、数据形状与调度语义，但底层是单进程/NumPy；
- **文档**：只在教程中解释，MiniVerl 没有对应代码；
- **范围外**：需要真实 GPU/集群/模型，教学实现不覆盖。

| verl 能力 | MiniVerl 级别 | 对应位置 | 说明 |
|---|---|---|---|
| DataProto / TensorDict | 实现 | `protocol.py` | 三部分结构、chunk/concat/pad/reorder |
| Hydra 配置 | 模拟 | `config.py` | dataclass + key=value 覆盖，无 `${}` 插值 |
| Dispatch（5 种模式） | 实现 | `single_controller/` | 单进程 dispatch，含 padding/unpadding |
| Ray WorkerGroup / placement group | 模拟 | `single_controller/worker_group.py` | `ResourcePool` + 同进程 worker |
| FSDP/FSDP2 参数分片 | 模拟 | `engine.py`、`sharding.py` | dense replica + `fsdp_shard/gather` |
| Megatron TP/PP | 模拟 | `parallel.py` | 列/行并行、1F1B 时间槽 |
| CP / Ring Attention | 文档 | 第 12 章 | 未实现 |
| EP / MoE routing | 模拟 | `parallel.py::moe_route` | softmax top-k + capacity |
| Router replay (R2/R3) | 实现 | `parallel.py::replay_moe_routes` | 记录/重放、行级 mask、层序校验 |
| 工具 schema / 生命周期 | 实现 | `tools.py` | OpenAI schema、create/execute/release |
| 函数工具 / 注册表 | 实现 | `tools.py` | 签名推断、重名拒绝、异常/超时封装 |
| Ulysses 序列并行 | 模拟 | `parallel.py` | 切分/合并 + MLP 等价 |
| vLLM / SGLang rollout | 模拟 | `rollout.py` | 自回归采样、temperature/top-k/top-p |
| Prefix cache | 模拟 | `rollout.py` | 按前缀复用 KV，含失效逻辑 |
| PD 分离 | 模拟 | `rollout.py::PDDisaggregatedRollout` | prefill/KV 传输账，不模拟调度 |
| 权重同步（naive/nccl/delta） | 模拟 | `sharding.py` | 字节账 + delta 部分更新 |
| TransferQueue | 模拟 | `transfer_queue.py` | 单进程有界/版本化/快照 |
| PPO/GRPO/DAPO/GSPO/CISPO | 实现 | `algo/core_algos.py`、`losses.py` | 公式、mask、端到端 |
| RLOO/RF++/ReMax | 实现 | `algo/core_algos.py` | 优势估计器 |
| GDPO/OPO/OTB/GPG/SAPO/DPPO/DRO | 文档 | 第 3、25 章 | 配置层拒绝未知 `loss_mode` |
| SFT / DPO / 蒸馏 | 实现 | `trainer/`、`losses.py` | 含 mask 与全局归一 |
| LoRA | 实现 | `lora.py` | 冻结、注入、合并、state_dict |
| QLoRA / FP8 / 量化 | 文档 | 第 30 章 | 只有显存账，无低精度 kernel |
| 多模态 VLM/Omni | 范围外 | 第 29 章 | 需要视觉编码器与真实模型 |
| Reward model 训练 | 实现 | `reward_model.py`、`trainer/main_rm.py` | 偏好对、BT 稳定损失、校准、检查点；不是语义 RM |
| Reward model 服务 | 模拟 | `ModelRewardManager` | 进程内打分/scatter；无 server/router/多租户 |
| Process reward model（PRM） | 实现 | `prm.py`、`trainer/main_prm.py` | 步骤标签、前缀 BCE、六种聚合、边界校验、与 verifier 组合；不是语义 PRM |
| PRM 服务 | 模拟 | `ProcessRewardManager` | 进程内逐步打分/scatter；无 server/router/多租户 |
| 步级 GAE / 密集过程奖励 | 文档 | 第 19.12 节 | `ProcessRewardManager` 只做轨迹聚合；token 广播需自己接 |
| AgentLoop / 工具调用 | 实现 | `agent.py` | 单轮/多轮、observation mask |
| 异步 RL / rollout correction | 模拟 | `async_policy.py` | replay buffer + IS，非真并发 |
| Checkpoint / 断点续训 | 实现 | `checkpoint.py` | 模型 + 优化器 + scheduler 游标 |
| HF/safetensors 导出 | 实现 | `checkpoint.py` | `export_hf_like` 往返 |
| 独立评测 / pass@k | 实现 | `trainer/main_eval.py` | checkpoint → 分组采样 |
| 容错 / 弹性 | 文档 | 第 31 章 | 只有 `fail_once` 故障注入 |
| Profiling / MFU | 模拟 | `metrics.py` | 墙钟、token/s、参数量估算 |
| 多机 / NCCL / 抢占 | 范围外 | 第 31、32 章、附录 J | 单进程无法覆盖 |

这张表也是“哪些结论可以直接迁移”的边界：标为“实现”的契约可以照搬到生产；标为“模拟”的语义可以照搬，但性能和故障行为不能；标为“文档/范围外”的部分，必须回到真实 verl 的源码与集群验证。

## 34.11 代码规模与阅读顺序

MiniVerl 当前约 1.08 万行 Python（`mini_verl/`），测试约 3700 行，示例约 1600 行。不要从头读到尾；按依赖顺序读：

```text
1. autograd.py -> nn.py                 # 梯度与模型
2. protocol.py -> config.py -> data.py  # 数据契约
3. algo/core_algos.py -> losses.py      # 公式
4. engine.py -> parallel.py -> sharding.py
5. single_controller/ -> workers.py
6. rollout.py -> rewards.py -> reward_model.py -> prm.py
7. trainer/ppo_trainer.py               # 主循环
8. trainer/main_eval.py / main_sft.py / main_dpo.py
9. agent.py / async_policy.py / transfer_queue.py
```

每读一个模块，先看它的 `__all__` 与测试文件，再看实现。测试文件往往比文档更精确地定义了“这个模块保证什么”。

## 本章小结

- MiniVerl 的目标是系统契约与可验证性，不是性能。
- 模块与真实 verl 一一对应，便于迁移阅读。
- 确定性来自显式 seed、无并发、无 GPU。
- bandit 任务是为 RL 正确性设计的“最小可学任务”。
- 覆盖矩阵把每项 verl 能力标成实现/模拟/文档/范围外；迁移结论只在对应级别内成立。
- 250 个测试、26 个示例、文档自检构成验证闭环。

## 延伸阅读

- 附录 G：MiniVerl 代码导读
- `README.md`
- 下一章：自动微分与 TinyTransformer。
