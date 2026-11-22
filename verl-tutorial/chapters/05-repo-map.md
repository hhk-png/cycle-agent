# 第 5 章 verl v0.9.1 仓库地图

## 5.1 版本基线

本教程以 **verl v0.9.1**（2026 年 11 月）为事实基线。它是一个已经明显“平台化”的版本：

- 仓库从 `volcengine/verl` 迁移到 `verl-project/verl`；
- 默认 trainer 是 V1（`trainer.use_v1=true`）；
- 训练后端抽象成 `BaseEngine` + `EngineRegistry`；
- 权重同步抽象成 `checkpoint_engine`；
- 数据生产与消费通过 TransferQueue 解耦；
- `fully_async_policy`、`one_step_off_policy` 保留在 `verl/experimental`；TransferQueue 本体是外部 Ascend/TransferQueue 包，verl 侧的集成/桥接层在 `verl/utils/transferqueue_utils.py`，配置在 `verl/trainer/config/transfer_queue/transfer_queue.yaml`；
- `recipe/` 已迁移到独立仓库 `verl-recipe`，以 submodule 形式挂载；
- 生态扩展到 verl-omni（多模态/扩散）、verl-vla（机器人）、verl-SpeCo（投机解码）、RL-Insight（可观测性）、uni-agent（Agent 框架）。

读旧教程时要注意：v0.3～v0.5 时代的 `verl/workers/fsdp_workers.py`、`megatron_workers.py`、`sharding_manager` 已被删除，迁移到 `verl/workers/engine_workers.py` + `verl/workers/engine/`。如果你在旧博客里看到这些路径，它们对应的是历史版本。

## 5.2 顶层目录

```text
verl/
├── verl/                      # 核心库
│   ├── protocol.py            # DataProto
│   ├── single_controller/     # Worker, WorkerGroup, dispatch, Ray 后端
│   ├── trainer/               # 入口、配置、PPO/V1 trainer、核心算法
│   ├── workers/               # Engine、EngineWorker、Rollout、Reward
│   ├── checkpoint_engine/     # 权重同步后端
│   ├── model_merger/          # FSDP/Megatron checkpoint 合并
│   ├── models/                # HF/MCore 模型 patch（RoPE/attention/MTP）
│   ├── plugin/platform/       # CUDA/ROCm/NPU 平台抽象
│   ├── tools/                 # Agent 工具基类与注册
│   ├── utils/                 # 数据集、序列、监控、设备、TransferQueue 桥接
│   ├── third_party/           # torch/vLLM 兼容 shim
│   ├── version/               # 版本号
│   └── experimental/          # agent_loop, fully_async, one_step, reward_loop, ...
├── examples/                  # 可复制的训练脚本与数据预处理
├── tests/                     # 单元/集成/e2e 测试
├── docs/                      # Sphinx 文档
├── recipe/                    # git submodule -> verl-recipe
├── scripts/                   # 工具脚本（配置生成、模型合并等）
└── pyproject.toml
```

## 5.3 入口与配置

### 入口

- `verl/trainer/main_ppo.py`：PPO/GRPO 主入口，Hydra 配置，初始化 Ray，选择 V1 或 legacy trainer。
- `verl/trainer/main_ppo_v0.py`：legacy trainer，v0.9.1 仍保留但已标记 deprecated。
- `verl/trainer/main_generation_server.py`：独立生成服务入口。
- `verl/trainer/main_eval.py` 等评测入口（视版本而定）。

### 配置

- `verl/trainer/config/ppo_trainer.yaml`：主配置，使用 Hydra defaults 组合子配置。
- `verl/trainer/config/_generated_ppo_trainer.yaml`：flatten 后的参考配置，**只读**，由脚本生成。
- `verl/trainer/config/actor/`：`actor.yaml`、`dp_actor.yaml`、`megatron_actor.yaml`、`torchtitan_actor.yaml`、`veomni_actor.yaml`。
- `verl/trainer/config/critic/`：对应的 critic 配置。
- `verl/trainer/config/rollout/rollout.yaml`：采样、TP、prefix caching、multi-turn、agent loop。
- `verl/trainer/config/data/legacy_data.yaml`：数据字段。
- `verl/trainer/config/algorithm.py` 与 `algorithm/rollout_correction.yaml`：优势估计、KL、过滤、rollout correction。

v0.9.1 的配置有一个重要变化：`model_engine: dp` 作为 defaults 的一层，通过 `${model_engine}_actor` 选择 FSDP/Megatron 等实现。因此同一个 `actor_rollout_ref.actor` 下可以切换 `strategy` 与 `engine`，而不需要改整份 YAML。

## 5.4 单控制器层

```text
verl/single_controller/
├── base/
│   ├── worker.py           # Worker, rank/world_size, dispatch 注册
│   ├── worker_group.py     # WorkerGroup, ResourcePool
│   └── decorator.py        # Dispatch, register, nd-compute dispatch
└── ray/
    ├── base.py             # RayWorkerGroup, RayResourcePool, placement group
    └── ...
```

关键 API：

- `@register(dispatch_mode=Dispatch.ONE_TO_ALL)`
- `@register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)`
- `make_nd_compute_dataproto_dispatch_fn(mesh_name="actor")`
- `RayWorkerGroup.execute_all_sync(...)`
- `RayResourcePool.get_placement_groups()`

MiniVerl 的 `single_controller/` 是这套 API 的本地版本。

## 5.5 训练引擎

v0.9.1 把“模型怎么算”抽象成 Engine：

```text
verl/workers/
├── engine/
│   ├── base.py             # BaseEngine：forward/backward, optimizer, checkpoint, export
│   ├── spec.py             # ShardSpec：参数分片描述
│   ├── fsdp/               # FSDP / FSDP2 / FSDP-Turbo
│   ├── megatron/           # Megatron-LM（TP/PP/CP/EP）
│   ├── automodel/          # HuggingFace AutoModel
│   ├── veomni/             # VeOmni
│   ├── torchtitan/         # TorchTitan
│   └── mindspeed/          # Ascend MindSpeed
├── engine_workers.py       # ActorRolloutRefWorker, TrainingWorker
└── config/                 # 各后端的 dataclass 配置
```

`EngineRegistry` 根据 `(model_type, backend, device)` 选择实现。`TrainingWorker` 是一个“引擎 + 优化器 + profiler”的通用 worker，actor/ref/critic/reward/SFT/DPO 都可以复用它。

`BaseEngine` 的方法契约、`ShardSpec` 的字段、六种后端的取舍与“新增一个后端要做什么”，见附录 M。

`ActorRolloutRefWorker` 是 hybrid worker：

- `actor`：训练引擎；
- `ref`：只读引擎（可选）；
- `rollout`：vLLM/SGLang/TRT-LLM/HF；
- `checkpoint_engine`：权重同步。

对应的 RPC：

- `init_model`
- `compute_log_prob` / `compute_ref_log_prob`
- `update_actor`
- `infer_batch`（critic 算 value）/ `train_mini_batch`（critic 更新）
- `update_weights`
- `save_checkpoint` / `load_checkpoint`

critic 没有独立的 `CriticWorker` 类：它映射到通用 `TrainingWorker`，价值头由 `FSDPEngineWithValueHead` / `MegatronEngineWithValueHead` 提供；`compute_values` / `update_critic` 是 trainer 的方法，不是 worker RPC。

## 5.6 Rollout

```text
verl/workers/rollout/
├── base.py                 # BaseRollout, get_rollout_class
├── vllm_rollout/           # vLLM（sync/async server, weight transfer）
├── sglang_rollout/         # SGLang（async server, delta loader）
├── trtllm_rollout/         # TensorRT-LLM
├── hf_rollout.py           # HF Transformers fallback
├── naive/                  # 简单 rollout
├── llm_server.py           # LLM server client/manager
├── router.py               # 请求路由
└── schemas.py              # 采样请求/响应 schema
```

v0.9.1 的 rollout 默认 `mode: async`，`name` 必须显式指定（`vllm`/`sglang`/`hf`/...）。`rollout.n`、`temperature`、`top_k/top_p`、`gpu_memory_utilization`、`tensor_model_parallel_size`、`free_cache_engine`、`enable_prefix_caching` 都在这里。

Agent 多轮 rollout 不在 `workers/rollout` 里，而在 `verl/experimental/agent_loop/`：

- `agent_loop.py`：`AgentLoopBase`、`AgentLoopWorker`、`AgentLoopManager`；
- `single_turn_agent_loop.py`：单轮；
- `tool_agent_loop.py`：工具调用；
- `tool_parser.py`：工具调用解析。

## 5.7 算法与 trainer

```text
verl/trainer/ppo/
├── ray_trainer.py          # legacy / 共享工具
├── core_algos.py           # 优势、KL、策略损失、值损失、aggregation
├── metric_utils.py         # 指标
├── padding_utils.py        # padding / 变长批
├── rollout_corr_helper.py  # rollout importance sampling
└── v1/
    ├── trainer_base.py     # PPOTrainer 基类、fit/step
    ├── trainer_sync.py     # 同步 colocate
    ├── trainer_colocate_async.py
    ├── trainer_separate_async.py
    ├── replay_buffer.py    # DAPO 过滤、异步采样
    └── agent_loop_tq.py    # TransferQueue 版 AgentLoopManager
```

`core_algos.py` 的函数名就是算法地图：`compute_gae_advantage_return`、`compute_grpo_outcome_advantage`、`compute_rloo_outcome_advantage`、`compute_reinforce_plus_plus_outcome_advantage`、`compute_remax_outcome_advantage`、`compute_policy_loss_vanilla`、`compute_policy_loss_gspo`、`compute_value_loss`、`kl_penalty`、`agg_loss`。

## 5.8 Reward

v0.9.1 的 reward 分成两套：

- `verl/workers/reward_manager/`：legacy reward manager（`naive`、`dapo`、`prime`、`batch`，另有 `abstract` 基类）；
- `verl/experimental/reward_loop/`：V1 的 reward loop（`naive`、`dapo`、`gdpo`、`rate_limited`、`remote`，含 registry、模型奖励 server、限流、路由）。

`reward.reward_manager.name` 选择规则/模型奖励（它是嵌套对象，不是裸字符串）；`reward.custom_reward_function.path/name` 加载自定义函数；`reward.reward_model.enable` 启用模型奖励。

## 5.9 Checkpoint 与模型合并

```text
verl/checkpoint_engine/
├── base.py                  # CheckpointEngine, Manager
├── nccl_checkpoint_engine.py
├── delta_checkpoint_engine.py
├── hccl_checkpoint_engine.py
├── kimi_checkpoint_engine.py
├── mooncake_checkpoint_engine.py
└── nixl_checkpoint_engine.py

verl/model_merger/
├── base_model_merger.py
├── fsdp_model_merger.py
└── megatron_model_merger.py
```

训练 checkpoint 是分片的（FSDP 或 Megatron），不能直接给 HF/vLLM 用。`model_merger` 把分片合并成 HF 格式；`output_validation.py` 检查输出是否完整。MiniVerl 的 `checkpoint.py` 演示了同样的“分片 → 合并 → 导出 safetensors”流程。

## 5.10 experimental：未来功能的孵化器

- `agent_loop/`：多轮、工具、异步 server；
- `fully_async_policy/`：Rollouter/Trainer/MessageQueue/ParameterSynchronizer 四件套；
- `one_step_off_policy/`：一步 off-policy，rollout 与训练分离；
- `separation/`：资源分离实验；
- `reward_loop/`：V1 reward loop（含 reward manager、reward model、router）；
- `teacher_loop/`：teacher model 管理与 on-policy 蒸馏；
- `agent_loop/`：`AgentLoopManagerTQ` 是 TransferQueue 版的多轮生产器。

TransferQueue 不在 `experimental/`：本体是外部 `TransferQueue` 包（pyproject 里的 git 依赖），verl 侧只保留桥接层 `verl/utils/transferqueue_utils.py`；配置在 `verl/trainer/config/transfer_queue/transfer_queue.yaml`，文档在 `docs/data/transfer_queue.md`。VLA 也不在核心仓库，而是独立项目 verl-vla。

这些模块的 README 通常比主文档更接近实现，值得优先读。

## 5.11 测试与脚本

```text
tests/
├── trainer/ppo/test_core_algos_on_cpu.py
├── workers/rollout/...
├── checkpoint_engine/...
├── model_merger/...
├── experimental/agent_loop/...
└── special_e2e/...
scripts/
├── generate_trainer_config.sh
├── print_cfg.py
└── legacy_model_merger.py
```

测试目录是“行为事实”的最好来源。比如 `test_core_algos_on_cpu.py` 固定了 GAE/GRPO 的数值，`test_sglang_rollout_sharding_manager.py` 固定了权重布局转换的期望。

## 5.12 版本演进：从 0.3 到 0.9

| 阶段 | 关键变化 |
|---|---|
| 0.1～0.3 | HybridFlow、FSDP/Megatron、vLLM/SGLang、PPO/GRPO |
| 0.4～0.5 | DAPO、GSPO、多轮、VLM、LoRA、更多后端 |
| 0.6～0.7 | V1 trainer、TransferQueue、engine 抽象、checkpoint engine |
| 0.8 | fully async、one-step off-policy、reward loop、更多硬件 |
| 0.9 | 默认 V1、engine worker 统一、生态扩展（omni/vla/SpeCo/RL-Insight） |

迁移时最需要注意的是：

- 旧 worker 路径被删除；
- `trainer.use_v1` 默认 true；
- rollout `name` 必填；
- `algorithm.filter_groups`、`rollout_correction` 等新字段；
- checkpoint 格式与合并脚本变化。

附录 H 给出更细的迁移表。

## 5.13 怎么读这份仓库

建议按“调用链”读，而不是按目录读：

1. `main_ppo.py` → `TaskRunnerV1.run` → `PPOTrainer.init/fit`；
2. `PPOTrainer._step_once` → `replay_buffer.sample` → `actor_rollout_wg.compute_log_prob`；
3. `ActorRolloutRefWorker.compute_log_prob` → `TrainingWorker.infer_batch` → `BaseEngine.forward_backward_batch`；
4. `EngineRegistry.new` → `FSDPEngine`/`MegatronEngine`；
5. `checkpoint_manager.update_weights` → `CheckpointEngine` → rollout server；
6. `AgentLoopManager.generate_sequences` → vLLM/SGLang server → 轨迹。

每读一层，问三个问题：数据形状是什么？dispatch 切的是哪一维？失败时谁会重试？

## 5.14 容易被略过的目录：plugin、tools、models 与 version

主干目录之外，还有几个目录在排障和二次开发时经常用到：

| 目录 | 内容 | 什么时候看 |
|---|---|---|
| `verl/plugin/platform/` | `PlatformBase`、`PlatformCUDA`、`PlatformNPU`、`PlatformROCm`、`PlatformRegistry`（`get_platform`/`set_platform`） | 移植到新硬件、查设备相关分支 |
| `verl/tools/` | `BaseTool`、`FunctionTool`、`ToolType`、`get_tool_class`、`initialize_tools_from_config` | 自定义 Agent 工具、工具参数校验 |
| `verl/models/mcore/` | Megatron Core 的模型 patch（含 MTP patch） | Megatron 路径的模型定义/权重映射 |
| `verl/models/transformers/` | HF 模型 patch（RoPE、attention、多模态 position） | 训练/推理 logits 对不上时 |
| `verl/third_party/` | 对 torch/vLLM 的极薄兼容层 | 版本兼容问题，不要在这里加业务逻辑 |
| `verl/version/version` | 版本号（v0.9.1） | 脚本/CI 读取版本 |
| `verl/utils/` | dataset、device、attention、profiler、checkpoint、transferqueue、skip 等 | 几乎所有“工具函数在哪”的问题 |
| `docs/` | Sphinx/Markdown 文档（start/algo/advance/perf/workers/data） | 理解设计意图；行为以源码/测试为准 |

两个容易误判的点：

1. **`verl/models/` 不是“模型实现”**。它主要是对 HF/MCore 模型的 patch 与并行适配；真正的模型结构来自 transformers/Megatron Core。改模型行为前先确认是 patch 层还是上游模型层。
2. **`verl/third_party/` 不是 vendored 代码**。它只是兼容 shim；把业务逻辑放进去会在升级时被覆盖。

`docs/` 的组织方式也值得记住：`docs/algo/` 是算法说明，`docs/advance/` 是高级旋钮，`docs/perf/` 是性能，`docs/workers/` 是引擎/worker，`docs/data/` 是 TransferQueue。附录 S 给出了文档到章节的完整映射。

## 5.15 四条“边界目录”：models、tools、checkpoint_engine 与 separation

如果把 verl 看成一台机器，主干是 trainer/worker/engine；真正决定“能不能接新东西”的，是四条边界：

| 边界 | 目录 | 回答的问题 | 对应附录 |
|---|---|---|---|
| 模型边界 | `verl/models/` | 新架构怎么变成并行模型、权重怎么映射 | 附录 U |
| 工具边界 | `verl/tools/` | 模型怎么调用外部能力、轨迹怎么记 | 附录 T |
| 传输边界 | `verl/checkpoint_engine/` | 训练权重怎么可靠地送到推理侧 | 附录 W |
| 资源边界 | `verl/experimental/separation/` | 训练与推理怎么拆成独立资源池 | 附录 W |

### 模型边界

`verl/models/registry.py` 的 `ModelRegistry` 只列了四个 Megatron 架构（Llama、Qwen2、Mistral、Apertus），但它属于**legacy 路径**：v0.9.1 默认 `use_mbridge=True`，模型/配置/权重转换由 Megatron-Bridge 负责，`verl/models/mcore/readme.md` 已把逐模型转换路径标为 deprecated。`ModelRegistry` 的调用入口在 v0.9.1 里没有实际 call site。

`verl/models/weight_loader_registry.py` 的 loader 只覆盖 Llama/Qwen2；saver 注册表列了 Mixtral、Qwen-MoE、Qwen2.5-VL、DeepSeek-V3，但其中四个函数在 `saver.py` 里是直接 `raise NotImplementedError` 的桩，只有 dense 的 `merge_megatron_ckpt_gptmodel` 是完整实现。**注册表里有名字不等于能用**，读这个目录时要看函数体，而不是只看键。

判断一个模型能不能用 Megatron：先看默认 bridge/provider 是否支持；只有显式关闭 bridge 时才回到 legacy registry。

### 工具边界

`verl/tools/` 有四个模块（加上 `__init__.py` 共五个文件），契约完整：

- `base_tool.py`：`create/execute/calc_reward/release` 生命周期；
- `function_tool.py`：`@function_tool` 装饰器与 schema 推断；
- `tool_registry.py`：`initialize_tools_from_config`、`load_all_tools`、native + function 合并与重名检查；
- `schemas.py`：OpenAI function schema 与 `ToolResponse`。

它和 `verl/experimental/agent_loop/` 的分工是：tools 定义“能做什么”，agent_loop 定义“什么时候做、结果怎么进轨迹”。上游没有名为 `ToolRegistry` 的类；那个名字是 MiniVerl 的教学封装。

### 传输边界

`verl/checkpoint_engine/` 不是“保存 checkpoint 的目录”，而是**权重传输层**：

- `base.py`：`CheckpointEngine` 的 `send_weights/receive_weights`；`get_weights` 在 `CheckpointEngineWithCache` 上（v0.9.1 无具体子类）；
- `nccl/hccl/nixl/mooncake/kimi`：不同通信库与拓扑；Ascend 的 HCCL 实现注册在 `nccl` 键下；
- `delta_checkpoint_engine.py`：只传变化字节；
- `delta_sync/`：编码、稀疏 gather。

真正的训练 checkpoint 保存仍在 `verl/utils/checkpoint/`，两者不要混淆。前者服务“每步更新推理权重”，后者服务“断电恢复”。

### 资源边界

`verl/experimental/separation/` 有三个模块（加上 `__init__.py` 共四个文件），却是 fully async 与 one-step off-policy 的地基：

- `ray_trainer.py`：`SeparateRayPPOTrainer` 改资源池与角色映射；
- `engine_workers.py`：`DetachActorWorker` 把模型 detach 到 CPU、restore 回 GPU；
- `utils.py`：`create_resource_pool_manager`、`create_role_worker_mapping`。

它支持的策略包括 FSDP、FSDP2、VeOmni、Megatron；不同策略的分片表示不同（flat tensor vs DTensor），所以 detach/restore 必须按策略选 handler。

这四条边界有一个共同点：**它们都不是算法，但算法能不能跑取决于它们**。读 verl 时，先把这四条边界的契约弄清楚，再看 PPO 主循环，会少走很多弯路。

## 本章小结

- v0.9.1 是 V1 时代：Engine、EngineWorker、TransferQueue、checkpoint engine 成为主干。
- 旧版 `fsdp_workers.py`、`megatron_workers.py`、`sharding_manager` 已被删除，读旧资料要换算路径。
- 入口、配置、单控制器、引擎、rollout、算法、reward、checkpoint 是七条主线。
- models、tools、checkpoint_engine、separation 是四条扩展边界；接模型/工具/传输/资源时先看它们。
- 按调用链读源码，比按目录读更有效。

## 延伸阅读

- 仓库：<https://github.com/verl-project/verl>
- Engine Worker 文档：`docs/workers/engine_workers.rst`
- Model Engine 文档：`docs/workers/model_engine.rst`
- 附录 T/U/V/W：四条扩展边界的深入说明。
- 下一章：安装、硬件与第一次 PPO/GRPO。
