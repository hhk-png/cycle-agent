# 附录 S verl v0.9.1 源码索引：从名字到调用链

> 本附录是“查字典”用的，不是线性阅读材料。当你已经知道要做什么，但不确定该改哪个文件、哪个配置字段、哪个 RPC 时，先在这里定位，再跳到对应章节。

## S.1 三条定位原则

1. **先找入口，再找实现。** `verl/trainer/main_ppo.py` 只负责 Hydra/Ray 初始化；真正的循环在 `verl/trainer/ppo/v1/trainer_base.py`。从入口往下读，比按目录漫游快。
2. **配置字段优先于类名。** 同一个行为可能由多个后端实现；配置字段（如 `actor_rollout_ref.rollout.name`）是稳定接口，类名会随版本变化。
3. **测试是行为事实。** 文档可能滞后，测试不会。定位一个行为的期望时，先看 `tests/` 下同名测试。

## S.2 入口与调用链

```text
python -m verl.trainer.main_ppo
  └─ main_ppo.py::run_ppo
       ├─ validate_config / auto_set_device
       ├─ ray.init(...)
       └─ TaskRunnerV1.run
            ├─ tq.init(config.transfer_queue)
            ├─ PPOTrainer(config).init()
            ├─ AgentLoopManagerTQ.create(...)
            └─ PPOTrainer.fit(agent_loop_manager)
                 └─ fit -> step -> _step_once
```

| 文件 | 关键符号 | 作用 | 章节 |
|---|---|---|---|
| `verl/trainer/main_ppo.py` | `run_ppo`、`TaskRunnerV1` | Hydra 入口、Ray 初始化、选择 V1 trainer | 5、21 |
| `verl/trainer/main_ppo_v0.py` | legacy `TaskRunner` | 旧版 trainer（保留兼容） | 5、附录 H |
| `verl/trainer/ppo/v1/trainer_base.py` | `PPOTrainer`、`fit`、`step`、`_step_once`、`register_trainer` | V1 主循环与可扩展点 | 21 |
| `verl/trainer/ppo/v1/trainer_sync.py` | `PPOTrainerSync` | sync 模式：colocate、无 partial rollout | 21、27 |
| `verl/trainer/ppo/v1/trainer_colocate_async.py` | `PPOTrainerColocateAsync` | 训练/rollout 同卡异步 | 27 |
| `verl/trainer/ppo/v1/trainer_separate_async.py` | `PPOTrainerSeparateAsync` | 独立 rollout 资源池 | 27 |
| `verl/trainer/ppo/v1/replay_buffer.py` | `ReplayBuffer`、`ReplayBufferAsync` | 采样、staleness、DAPO 过滤 | 21、22、27 |
| `verl/trainer/ppo/v1/agent_loop_tq.py` | `AgentLoopManagerTQ` | TransferQueue 版多轮生成 | 26、27 |
| `verl/trainer/main_generation_server.py` | 批量生成入口 | 启动 standalone rollout server，生成 parquet | 15、24 |
| `verl/trainer/main_eval.py` | 离线评测入口 | 读取已生成 parquet，按 data_source 打分（不加载 checkpoint） | 24 |

`fit` 的外层顺序：`val_before_train` → 循环 `step` → 按 `save_freq` 保存 → 按 `test_freq` 验证 → 记录指标 → 清理 TransferQueue 的 KV。`step` 内部再按 `parameter_sync_step` 拆成多次 `_step_once`（第 21.11 节）。

## S.3 协议、控制面与数据面

| 文件 | 关键符号 | 作用 | 章节 |
|---|---|---|---|
| `verl/protocol.py` | `DataProto`、`DataProtoFuture` | 张量/非张量/元信息三部分契约 | 9 |
| `verl/single_controller/base/decorator.py` | `Dispatch`、`register`、`make_nd_compute_dataproto_dispatch_fn` | RPC 分发模式 | 4、10 |
| `verl/single_controller/base/worker.py` | `Worker` | rank/world_size、collective 封装 | 10 |
| `verl/single_controller/base/worker_group.py` | `WorkerGroup`、`ResourcePool` | worker 生命周期与资源池 | 10 |
| `verl/single_controller/ray/base.py` | `RayWorkerGroup`、`RayResourcePool` | Ray 实现、placement group | 10 |
| `verl/utils/transferqueue_utils.py` | TransferQueue 客户端封装 | 生产/消费数据面 | 27 |
| `verl/trainer/config/transfer_queue/transfer_queue.yaml` | `storage_backend`、`SimpleStorage`、`MooncakeStore` | 队列后端配置 | 27 |
| `docs/data/transfer_queue.md` | 控制面/数据面/API | TransferQueue 设计文档 | 27 |

`Dispatch` 在 v0.9.1 注册了 `ONE_TO_ALL`、`DP_COMPUTE`、`DP_COMPUTE_PROTO`、`DP_COMPUTE_PROTO_WITH_FUNC`、`DP_COMPUTE_METRIC`。前三个是教程主线；后两个是带函数/指标的变体。

## S.4 训练引擎与 worker

| 文件 | 关键符号 | 作用 | 章节 |
|---|---|---|---|
| `verl/workers/engine/base.py` | `BaseEngine`、`BaseEngineCtx`、`EngineRegistry` | 引擎契约、上下文切换、注册表 | 11、12、附录 M |
| `verl/workers/engine/spec.py` | `ShardSpec` | 参数分片描述（mesh/placements/HF 映射） | 14、附录 M |
| `verl/workers/engine/fsdp/transformer_impl.py` | `FSDPEngine` | FSDP/FSDP2 实现 | 11 |
| `verl/workers/engine/fsdp/fsdp_turbo_impl.py` | FSDP Turbo | 通信/显存优化路径 | 附录 Q |
| `verl/workers/engine/megatron/transformer_impl.py` | `MegatronEngine` | TP/PP/CP/EP | 12 |
| `verl/workers/engine/megatron/delta_export.py` | delta export | 稀疏权重导出 | 14 |
| `verl/workers/engine/automodel/transformer_impl.py` | Automodel | HF AutoModel 后端 | 附录 M |
| `verl/workers/engine/veomni/transformer_impl.py` | VeOmni | 多模态/统一后端 | 附录 M |
| `verl/workers/engine/torchtitan/transformer_impl.py` | TorchTitan | 原生 PyTorch 并行 | 附录 M |
| `verl/workers/engine/mindspeed/transformer_impl.py` | MindSpeed | Ascend NPU | 附录 I、M |
| `verl/workers/engine_workers.py` | `ActorRolloutRefWorker`、`TrainingWorker` | actor/ref/critic/rollout 的 RPC 聚合 | 5、10、14 |
| `verl/workers/config/actor.py` | `ActorConfig`、`PolicyLossConfig` | actor 配置 dataclass | 7、18 |
| `verl/workers/config/rollout.py` | `RolloutConfig`、`SamplingConfig` | rollout 配置 | 15、16 |
| `verl/workers/config/engine.py` | `EngineConfig` | 引擎配置 | 11、12 |

`BaseEngine` 的方法可以分成五组：生命周期（`initialize`、`train_mode`、`eval_mode`）、计算（`forward_backward_batch`、`train_batch`、`infer_batch`）、优化器（`optimizer_zero_grad`、`optimizer_step`、`lr_scheduler_step`）、权重导出（`get_per_tensor_param*`）、checkpoint（`save_checkpoint`、`load_checkpoint`）。新增后端时，这五组都要实现，缺一组就会在某个 trainer 路径上崩。

## S.5 算法、损失与 rollout correction

| 文件 | 关键符号 | 作用 | 章节 |
|---|---|---|---|
| `verl/trainer/ppo/core_algos.py` | `AdvantageEstimator`、`ADV_ESTIMATOR_REGISTRY`、`POLICY_LOSS_REGISTRY` | 优势/损失注册表 | 3、25 |
| `verl/trainer/ppo/core_algos.py` | `compute_gae_advantage_return`、`compute_grpo_outcome_advantage`、`compute_rloo_outcome_advantage`、`compute_opo_outcome_advantage`、`compute_gdpo_outcome_advantage`、`compute_optimal_token_baseline_advantage` | 14 个估计器 | 3 |
| `verl/trainer/ppo/core_algos.py` | `compute_policy_loss_vanilla/gspo/sapo/gpg/clip_cov/kl_cov/geo_mean/dro/cispo/dppo_tv/dppo_kl/bypass_mode` | 12 个损失模式 | 25 |
| `verl/trainer/ppo/core_algos.py` | `kl_penalty`、`agg_loss`、`compute_value_loss` | KL、归约、critic loss | 2、18、19、20 |
| `verl/trainer/ppo/rollout_corr_helper.py` | `compute_rollout_correction_and_rejection_mask` | IS/RS 修正 | 27 |
| `verl/trainer/config/algorithm/rollout_correction.yaml` | `rollout_is`、`rollout_rs`、`bypass_mode` | rollout correction 配置 | 27 |
| `verl/trainer/distillation/losses.py` | `compute_online_dpo_loss` 等 | 蒸馏/online DPO | 28 |
| `verl/trainer/ppo/metric_utils.py` | `compute_data_metrics`、`compute_timing_metrics` | 指标口径 | 24 |
| `verl/trainer/ppo/padding_utils.py` | packing/padding 工具 | remove padding | 13 |

注册表是唯一事实源。新增算法时，先决定它属于 `adv_estimator` 还是 `policy_loss.loss_mode`，再注册函数；不要用一个字段同时表达两个概念（第 25.13 节的反例）。

## S.6 Rollout、Reward、Checkpoint、Agent

| 文件 | 关键符号 | 作用 | 章节 |
|---|---|---|---|
| `verl/workers/rollout/base.py` | `BaseRollout`、`get_rollout_class` | rollout 抽象与工厂 | 15 |
| `verl/workers/rollout/replica.py` | `RolloutReplica` | 单副本生命周期 | 15 |
| `verl/workers/rollout/router.py` | router | 请求路由/负载均衡 | 15 |
| `verl/workers/rollout/llm_server.py` | LLM server client | 独立 server 模式 | 15、24 |
| `verl/workers/rollout/vllm_rollout/vllm_rollout.py` | vLLM rollout | vLLM 集成、权重更新 | 15 |
| `verl/workers/rollout/sglang_rollout/sglang_rollout.py` | SGLang rollout | SGLang 集成 | 15 |
| `verl/workers/rollout/trtllm_rollout/trtllm_rollout.py` | TensorRT-LLM rollout | TRT-LLM 集成 | 15 |
| `verl/workers/rollout/hf_rollout.py` | HF rollout | Transformers fallback | 15 |
| `verl/workers/rollout/naive/naive_rollout.py` | naive rollout | 无引擎采样 | 15、37 |
| `verl/workers/reward_manager/abstract.py` | `AbstractRewardManager` | 奖励管理接口 | 17 |
| `verl/workers/reward_manager/naive.py` | `NaiveRewardManager` | 规则奖励 | 17 |
| `verl/workers/reward_manager/dapo.py` | `DAPORewardManager` | overlong 惩罚 | 17、22 |
| `verl/workers/reward_manager/prime.py` | PRIME manager | 隐式过程奖励 | 25 |
| `verl/experimental/reward_loop/reward_loop.py` | `RewardLoopManager` | V1 reward loop | 17 |
| `verl/experimental/reward_loop/reward_model.py` | reward model worker | 模型奖励 | 17 |
| `verl/checkpoint_engine/base.py` | `CheckpointEngine`、`CheckpointEngineManager` | 权重同步 | 14 |
| `verl/checkpoint_engine/nccl_checkpoint_engine.py` | NCCL 后端 | 全量 broadcast | 14 |
| `verl/checkpoint_engine/delta_checkpoint_engine.py` | delta 后端 | 稀疏同步 | 14 |
| `verl/checkpoint_engine/mooncake_checkpoint_engine.py` | Mooncake 后端 | RDMA/KV 传输 | 14、附录 J |
| `verl/utils/checkpoint/checkpoint_manager.py` | checkpoint manager | 训练状态保存/恢复 | 23 |
| `verl/model_merger/fsdp_model_merger.py` | FSDP merger | 分片 → HF | 23 |
| `verl/model_merger/megatron_model_merger.py` | Megatron merger | TP/PP → HF | 23 |
| `verl/experimental/agent_loop/agent_loop.py` | `AgentLoopBase`、`AgentLoopManager` | 多轮/工具轨迹 | 26 |
| `verl/experimental/agent_loop/tool_agent_loop.py` | tool loop | 工具调用 | 26 |
| `verl/experimental/fully_async_policy/` | Rollouter/Trainer/MessageQueue/Synchronizer | 完全异步 | 27 |
| `verl/experimental/one_step_off_policy/` | one-step off-policy | 一步延迟 | 27 |
| `verl/experimental/teacher_loop/` | teacher loop | on-policy 蒸馏 | 28 |

## S.7 配置字段到源码的映射

| 配置字段 | 读取位置 | 说明 |
|---|---|---|
| `trainer.v1.trainer_mode` | `trainer_base.py::__init__` | `sync`/`colocate_async`/`separate_async` |
| `trainer.v1.<mode>.parameter_sync_step` | `trainer_base.py::__init__`、`step` | 一个 global step 内的 local update 数 |
| `trainer.v1.sampler.max_off_policy_threshold` | `replay_buffer.py` | staleness 上限 |
| `algorithm.adv_estimator` | `trainer_base.py::_compute_advantage` | 优势估计器名字 |
| `algorithm.norm_adv_by_std_in_grpo` | `core_algos.py` | GRPO/Dr.GRPO 切换 |
| `algorithm.filter_groups.*` | `replay_buffer.py` | DAPO 动态采样 |
| `algorithm.rollout_correction.*` | `rollout_corr_helper.py` | IS/RS 修正 |
| `actor_rollout_ref.actor.policy_loss.loss_mode` | `core_algos.py::get_policy_loss_fn` | 损失模式 |
| `actor_rollout_ref.actor.loss_agg_mode` | `core_algos.py::agg_loss` | 归约口径 |
| `actor_rollout_ref.actor.use_kl_loss` | `engine_workers.py`/loss | 训练侧 KL |
| `algorithm.use_kl_in_reward` | `trainer_base.py::_compute_advantage` | 奖励侧 KL |
| `actor_rollout_ref.rollout.name` | `workers/rollout/base.py` | vllm/sglang/trtllm/hf/naive |
| `actor_rollout_ref.rollout.mode` | rollout replica | sync/async |
| `actor_rollout_ref.rollout.n` | trainer/replay buffer | 组采样数 |
| `actor_rollout_ref.rollout.gpu_memory_utilization` | vLLM/SGLang 引擎 | KV cache 占比 |
| `actor_rollout_ref.rollout.checkpoint_engine.backend` | `checkpoint_engine/base.py` | naive/nccl/delta/... |
| `reward.reward_manager` | `workers/reward_manager/registry.py` | naive/dapo/prime/... |
| `reward.custom_reward_function.path/name` | reward manager | 自定义奖励 |
| `transfer_queue.enable` | `main_ppo.py`、`tq.init` | 启用数据面 |
| `trainer.use_v1` | `main_ppo.py` | 是否走 V1 |

一个字段是否真的生效，按四步查：

```bash
# 1. 配置里有没有
grep -rn "parameter_sync_step" verl/trainer/config
# 2. 代码里有没有读
grep -rn "parameter_sync_step" verl/trainer/ppo/v1
# 3. 有没有被 OmegaConf.select 到
grep -rn "select(self.config" verl/trainer/ppo/v1/trainer_base.py
# 4. 打印最终配置
python -m verl.trainer.main_ppo --cfg job
```

第 4 步的 `--cfg job` 会打印 Hydra 解析后的配置；如果字段不在输出里，它就没有进入 trainer。

## S.8 文档地图

| 主题 | 文档 | 对应章节 |
|---|---|---|
| 安装 | `docs/start/install.rst` | 6 |
| 快速开始 | `docs/start/quickstart.rst` | 6 |
| 多机 | `docs/start/multinode.rst` | 31、附录 J |
| Agentic RL | `docs/start/agentic_rl.rst` | 26 |
| 算法 | `docs/algo/*.md`（grpo、dapo、gpg、opo、otb、dppo、dro、opd、spin、sppo、rollout_corr） | 3、25 |
| 高级特性 | `docs/advance/*.rst`/`*.md`（attention、rope、mtp、dynamic CP、FSDP Turbo、Megatron Lite、skip、determinism、trace、RL-Insight） | 附录 Q |
| 性能 | `docs/perf/*.rst`（best_practices、perf_tuning、rollout_kv_offload、nsight、torch_profiling） | 32 |
| 数据面 | `docs/data/transfer_queue.md` | 27 |
| Worker/Engine | `docs/workers/engine_workers.rst`、`model_engine.rst` | 11、12、附录 M |
| 单控制器 | `docs/single_controller.rst` | 4、10 |

## S.9 用 grep 在 90 秒内定位一个行为

```bash
# 找某个 RPC 的定义与调用
grep -rn "def update_weights" verl/workers verl/checkpoint_engine

# 找某个配置字段的所有读取点
grep -rn "gpu_memory_utilization" verl/ | head -20

# 找某个算法的注册与实现
grep -n "register_policy_loss\|register_adv_est" verl/trainer/ppo/core_algos.py

# 找某个类的所有子类
grep -rn "class .*BaseEngine" verl/workers/engine

# 找测试中的行为断言
grep -rn "test_.*update_weights\|def test_.*delta" tests/ | head -20

# 看一个文件的公开符号
grep -n "^class \|^def " verl/trainer/ppo/v1/trainer_base.py
```

定位顺序建议：**配置字段 → 读取点 → 实现类 → 测试**。如果前三步都找不到，可能这个字段是别的模块通过 `OmegaConf.select` 间接读取的；用第 2 条命令搜全仓库，而不是只搜 trainer。

## S.10 版本与路径的边界

- 本索引以 v0.9.1 的源码树为准；路径与符号会随版本变化，升级后先用 `git diff` 看目标文件是否还在。
- `verl/workers/fsdp_workers.py`、`verl/workers/megatron_workers.py`、`verl/workers/sharding_manager/` 是 0.3～0.5 的历史路径，v0.9.1 已删除；旧博客里的这些路径需要换算到 `engine_workers.py` + `engine/`。
- `recipe/` 是 `verl-recipe` 的 submodule；源码归档里可能是空目录，算法 recipe 不在主仓库。
- `docs/` 与源码可能不同步；以源码与测试为准，文档用于理解意图。

## S.11 工具、模型与传输的源码索引

前三节按“训练主链”组织；这一节按“扩展边界”组织。当你要接工具、接模型、或改权重传输时，从这里开始。

### 工具与 Agent

| 文件 | 关键符号 | 作用 | 章节/附录 |
|---|---|---|---|
| `verl/tools/base_tool.py` | `BaseTool`、`create/execute/calc_reward/release` | 每轨迹实例的工具生命周期 | 26、T |
| `verl/tools/function_tool.py` | `FunctionTool`、`function_tool`、`FUNCTION_TOOL_REGISTRY` | 普通函数 → 工具，schema 推断 | T |
| `verl/tools/tool_registry.py` | `initialize_tools_from_config`、`load_all_tools` | native + function 合并、重名检查 | T |
| `verl/tools/schemas.py` | `OpenAIFunctionToolSchema`、`ToolResponse` | OpenAI schema 与多模态返回值 | T |
| `verl/experimental/agent_loop/` | `AgentLoopBase`、`AgentLoopWorker`、`AgentLoopManager` | 多轮轨迹 | 26 |
| `verl/utils/reward_score/sandbox_fusion/` | 沙箱客户端 | 远程代码执行 | T、N |

### 模型与权重

| 文件 | 关键符号 | 作用 | 章节/附录 |
|---|---|---|---|
| `verl/models/registry.py` | `ModelRegistry`、`load_model_cls` | 架构名 → Megatron 模型类 | U |
| `verl/models/weight_loader_registry.py` | `get_weight_loader`、`get_weight_saver` | HF ↔ Megatron 权重映射 | U |
| `verl/models/mcore/loader.py` | `load_state_dict_to_megatron_gptmodel` | HF state_dict → 并行分片 | U |
| `verl/models/mcore/saver.py` | `merge_megatron_ckpt_gptmodel*` | 分片 → HF checkpoint | U |
| `verl/models/mcore/config_converter.py` | `hf_to_mcore_config_*` | HF config → TransformerConfig | U |
| `verl/models/mcore/registry.py` | `supported_vlm`、`get_mcore_forward_fn` | VLM/dense 前向选择 | U |

### 权重传输与资源分离

| 文件 | 关键符号 | 作用 | 章节/附录 |
|---|---|---|---|
| `verl/checkpoint_engine/base.py` | `CheckpointEngine`、`CheckpointEngineRegistry` | send/receive/get weights | 14、W |
| `verl/checkpoint_engine/delta_checkpoint_engine.py` | `DeltaCheckpointEngine` | 稀疏 delta 同步 | 14、W |
| `verl/checkpoint_engine/nixl_checkpoint_engine.py` | `NIXLCheckpointEngine` | 弹性/异构传输 | W |
| `verl/checkpoint_engine/mooncake_checkpoint_engine.py` | `MooncakeCheckpointEngine` | Mooncake 传输 | W |
| `verl/experimental/separation/ray_trainer.py` | `SeparateRayPPOTrainer` | 分离资源池 trainer | 15、27、W |
| `verl/experimental/separation/engine_workers.py` | `DetachActorWorker` | detach/restore 模型 | W |

### 路由回放与量化同步

| 文件 | 关键符号 | 作用 | 章节/附录 |
|---|---|---|---|
| `verl/utils/megatron/router_replay_patch.py` | `RouterReplay`、`RouterReplayAction` | 记录/重放 top-k | 16、V |
| `verl/utils/megatron/router_replay_utils.py` | R3 mask、layer map | hash/learned 层对齐 | V |
| `verl/utils/veomni/router_replay.py` | VeOmni 版 replay | 多后端一致性 | V |
| `verl/utils/vllm/vllm_quant_utils.py` | 量化出站权重流 | prepare/finalize 分派 | V |
| `verl/utils/vllm/vllm_fp8_utils.py` | block FP8 | dense/MoE FP8 | V |
| `verl/utils/vllm/vllm_fp4_utils.py` | MXFP4 | routed experts | V |

定位建议：扩展边界的问题，先用“配置字段 → registry 键 → loader/saver → 测试”四步。例如“新模型为什么不能训练”，先看 `ModelRegistry.get_supported_archs()`，再看 `get_weight_loader(arch)`，最后看 `tests/models/`。

## 延伸阅读

- 第 5 章：仓库地图与七条主线。
- 第 21 章：V1 主循环逐行。
- 第 25 章：算法注册表与 12 个损失模式。
- 附录 M：引擎后端与新增后端清单。
- 附录 P：二次开发流程。
- 附录 Q：高级特性与文档入口。
- 回到第 1 章：重新建立“系统问题 → verl 抽象”的主线。
