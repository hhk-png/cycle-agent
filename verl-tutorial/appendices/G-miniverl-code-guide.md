# 附录 G MiniVerl 代码导读

## G.1 阅读顺序

建议按调用链读：

```text
trainer/main_ppo.py
  -> trainer/ppo_trainer.py::PPOTrainer.init
    -> config.py
    -> data.py
    -> single_controller/worker_group.py
    -> workers.py
    -> rollout.py
    -> rewards.py
    -> reward_model.py（启用模型奖励时）
    -> prm.py（启用过程奖励时）
  -> PPOTrainer.fit
    -> train_step
      -> losses.py
      -> algo/core_algos.py
      -> engine.py
```

## G.2 `autograd.py`

- `Tensor`：data/grad/requires_grad/_backward/_prev；
- `backward()`：拓扑排序 + 反向遍历；
- `_unbroadcast`：广播梯度回原形状；
- 算子：add/sub/mul/div/matmul/sum/mean/reshape/transpose/getitem/exp/log/sqrt/pow/relu/gelu/clip；
- functional：concat/stack/softmax/log_softmax/layer_norm/embedding/gather_last_dim/masked_fill/where/minimum/maximum；
- `no_grad()`。

关键测试：`tests/test_autograd.py`。

## G.3 `nn.py`

- `Module`：parameters/trainable_parameters/state_dict/load_state_dict/train/eval；
- `Linear`/`Embedding`/`LayerNorm`/`CausalSelfAttention`/`MLP`/`Block`；
- `TinyLM`/`TinyValueModel`/`TinyLinearLM`/`TinyLinearValue`；
- `log_probs_from_logits`/`token_log_probs`/`entropy_from_logits`；
- `build_policy`/`build_value`（按 `arch` 选择；`lora_rank>0` 时自动注入 adapter）。

关键测试：`tests/test_nn.py`。

## G.4 `protocol.py`

- `DataProto`：batch/non_tensor_batch/meta_info；
- `__getitem__`/`select_idxs`/`reorder`/`slice`；
- `chunk`/`split`/`concat`/`union`；
- `repeat(interleave)`；
- `pad_to_divisor`/`unpad_dataproto`；
- `pop`/`rename`/`to_dict`/`from_dict`。

关键测试：`tests/test_protocol.py`。

## G.5 `config.py`

- `OptimConfig`/`EngineConfig`/`ModelConfig`；
- `ActorConfig`/`RefConfig`/`CriticConfig`/`RolloutConfig`；
- `DataConfig`/`AlgorithmConfig`/`RewardConfig`/`TrainerConfig`；
- `PPOConfig`：to_dict/to_yaml/apply_overrides/validate/save/copy/set_path；
- 模块级函数：`from_dict`、`load_config(path, overrides)`。

关键测试：`tests/test_config.py`。

## G.6 `data.py`

- `CharTokenizer`：encode/decode/save/load；
- `Sample`/`RLHFDataset`；
- `_BanditDataset`/`_CopyDataset`/`_ArithmeticDataset`/`_GSM8KLiteDataset`；
- `collate_fn`：input_ids/attention_mask/response_mask/prompt_length/answers/uid；
- `make_batches`；
- `preference_pairs`。

关键测试：`tests/test_data_rewards.py`。

## G.7 `single_controller/`

- `decorator.py`：Dispatch/register；
- `worker.py`：Worker/collectives/fail_once；
- `worker_group.py`：ResourcePool/LocalWorkerGroup/_dispatch/_sync_gradients_and_step。

关键测试：`tests/test_dispatch.py`。

## G.8 `engine.py`

- `Optimizer`/`SGD`/`AdamW`；
- `clip_gradients`；
- `BaseEngine`：initialize/train_mode/eval_mode/forward_backward_batch/train_batch/infer_batch/get_gradients/set_gradients/finalize_update/save_checkpoint/load_checkpoint/to/memory_bytes；
- `FSDPEngine`/`MegatronEngine`/`EngineRegistry`；
- `estimate_memory`。

关键测试：`tests/test_engine_parallel.py`。

## G.9 `losses.py`

- `logprob_metric_fn`/`value_metric_fn`；
- `kl_penalty_tensor`；
- `actor_loss_fn`/`critic_loss_fn`；
- `sft_loss_fn`/`dpo_loss_fn`/`distillation_loss_fn`。

关键测试：`tests/test_core_algos.py` + 端到端训练。

## G.10 `algo/core_algos.py`

- masked_mean/masked_sum/masked_whiten；
- GAE/GRPO/RLOO/RF++/ReMax；
- compute_advantage 分发；
- KL 估计；
- agg_loss；
- compute_policy_loss / gspo / cispo；
- value/entropy loss；
- KL controllers；
- filter_groups_for_dapo。

关键测试：`tests/test_core_algos.py`。

## G.11 `workers.py`

- `ActorRolloutRefWorker`：
  - compute_log_prob / compute_ref_log_prob；
  - update_actor（sync_gradients）；
  - sft_train_step / dpo_train_step；
  - get/load state dict；
  - save/load checkpoint；
- `CriticWorker`：compute_values / update_critic；
- `TrainingWorker` 别名。

关键测试：`tests/test_trainer.py`。

## G.12 `rollout.py`

- `SamplingParams`；
- `NaiveRollout`：generate_sequences/update_weights/update_weights_partial/sleep/wake_up/memory_bytes；
- `TPRollout`：TP 布局；
- `AsyncRolloutServer`；
- `build_rollout`。

关键测试：`tests/test_rollout_checkpoint.py`。

## G.13 `sharding.py`

- `SyncStats`；
- `flatten_state_dict/unflatten_state_dict`；
- `fsdp_shard/fsdp_gather`；
- `tp_shard_state_dict/tp_gather_state_dict`；
- `CheckpointEngineManager`：naive/nccl/delta/delta_sharded。

关键测试：`tests/test_engine_parallel.py`。

## G.14 `checkpoint.py`

- `CheckpointManager`：save/load/list_checkpoints/latest/prune/load_config/export_hf_like；
- `write_safetensors`/`read_safetensors`；
- `export_hf_like`。

关键测试：`tests/test_rollout_checkpoint.py`。

## G.15 `async_policy.py`

- `VersionedBatch`；
- `ReplayBuffer`：drop/keep；
- `compute_rollout_correction`。

关键测试：`tests/test_rollout_checkpoint.py`。

## G.16 `agent.py`

- `Tool`/`CalculatorTool`/`ToolRegistry`；
- `AgentTrajectory`；
- `AgentLoop`：generate/tool/mask；
- `AgentLoopManager`：to_dataproto。

关键测试：`tests/test_trainer.py::test_agent_tool_loop`。

## G.17 `metrics.py`

- `RunningMean`/`MetricsAggregator`；
- `compute_data_metrics`/timing/throughput；
- `compute_pass_at_k`/validation；
- `MetricsLogger`。

关键测试：`tests/test_trainer.py`。

## G.18 `trainer/`

- `ppo_trainer.py`：init/fit/train_step/validate/evaluate/save/resume/agent_loop；
- `sft_trainer.py`；
- `dpo_trainer.py`；
- `main_ppo.py`/`main_sft.py`/`main_dpo.py`/`main_eval.py`。

关键测试：`tests/test_trainer.py`。

## G.19 一次训练的数据流

```text
data.py::collate_fn
  -> DataProto(prompts, answers, uid)
  -> PPOTrainer._rollout_batch
    -> DataProto.repeat(n)
    -> NaiveRollout.generate_sequences
      -> response_text, rollout_log_probs, response_mask
  -> RewardManager.run
    -> rm_scores, token_level_rewards
  -> filter_groups_for_dapo (optional)
  -> _balance_batch
  -> actor_group.compute_log_prob
    -> old_log_probs
  -> critic_group.compute_values (optional)
    -> values
  -> _compute_advantage
    -> advantages, returns
  -> _update_worker(critic)
  -> _update_worker(actor)
  -> _sync_weights
```

## G.20 `lora.py`

- `LoRALinear`：冻结 `base`，学习 `A`、`B`，`forward = base(x) + scale * x A B`；
- `apply_lora`：按目标层名注入 adapter，并调用 `freeze_base_parameters`；
- `freeze_base_parameters`：除 adapter 外全部冻结；
- `lora_modules`/`lora_parameters`/`adapter_state_dict`：发现与导出；
- `merge_lora_inplace`：把增量写回基座并解包，供 rollout 副本使用。

关键测试：`tests/test_lora.py`；示例：`examples/17_lora.py`。

## G.21 如何加一个新算法

1. 在 `algo/core_algos.py` 加估计器；
2. 在 `compute_advantage` 分发；
3. 在 `config.validate` 加枚举；
4. 在 `losses.py` 加 loss（如需要），参考 CISPO 的 `use_cispo` 分支；
5. 在 worker/trainer 选择（配置 → batch flag → loss）；
6. 加公式/梯度/端到端测试；
7. 更新文档与 `check_docs.py` 数字。

## G.22 如何加一个新 reward

1. 在 `rewards.py` 加函数；
2. 在 `RewardManager.__init__` 注册字符串；
3. 或在外部用 `module:function` 加载；
4. 加测试；
5. 更新文档。

## G.23 如何加一个新并行模拟

1. 在 `parallel.py` 加类/函数；
2. 与 dense 对照测试；
3. 在 `examples/16_parallel_patterns.py` 展示；
4. 在文档中说明与真实后端的映射。

## G.24 `parallel.py`

- `ProcessGroup`：本地 rank/world size 与 all-reduce 语义；
- `ColumnParallelLinear` / `RowParallelLinear`：TP 的列/行切分；
- `sequence_parallel_mlp`：SP 下的 MLP 等价；
- `ulysses_split` / `ulysses_merge`：序列切分与还原；
- `PipelineSchedule`：1F1B 气泡估算与调度；
- `pipeline_forward`：多 stage 顺序等价；
- `moe_route`：top-k、容量、丢弃、加权合并；
- `kv_cache_bytes`：KV cache 的字节估算。

关键测试：`tests/test_engine_parallel.py`、`examples/16_parallel_patterns.py`。

读法：先看 `ColumnParallelLinear` 的切分轴，再看 `RowParallelLinear` 的 all-reduce；两者组合就是 Megatron MLP。`PipelineSchedule.bubble_ratio()` 对应第 12.4 节的公式。

## G.25 `rewards.py`

- 规则奖励：`exact_match_reward`、`bandit_reward`、`math_reward`、`format_reward`、`length_penalty`；
- 工具：`extract_boxed`、`normalize_number`、`load_custom_reward`；
- `RewardManager`：标量 → 最后一个有效 response token；
- `DAPORewardManager`：overlong / 长度惩罚；
- `RewardLoopManager`：线程池、超时、重试、失败计数。

关键测试：`tests/test_data_rewards.py`、`examples/13_custom_reward.py`、`examples/15_math_reward.py`。

读法：先看 `RewardManager.run` 如何找 `valid[-1]`，再看 `DAPORewardManager` 如何根据 `response_length` 和 `max_length` 加惩罚。多轮场景的 observation mask 在 `agent.py`，不在 reward 里。

## G.26 `transfer_queue.py`

- `TransferQueue`：有界队列、版本、样本级 `put/get/try_get`；
- `put(data, version, produced_at)`：写入并附带生产版本；
- `get(batch_size)`：按样本取用，剩余样本回队；
- `try_get`：非阻塞；
- `snapshot` / `restore`：未消费样本与计数器；
- `max_batches` + `drop_oldest/block`：背压；
- `stats`：生产/消费/丢弃计数。

关键测试：`tests/test_transfer_queue.py`、`examples/19_transfer_queue.py`。

读法：重点看“部分取用”如何保证不丢样本，以及 snapshot 是否包含消费位置。跨进程/跨节点版本不在 MiniVerl 范围内，但契约（有界、版本、快照）与真实 TransferQueue 一致。

## G.27 PD 分离 rollout

- `PDDisaggregatedRollout`：继承 `NaiveRollout`，采样逻辑复用；
- 统计 `pd/prefill_tokens`、`pd/kv_transfer_bytes`、`pd/transfer_ms`；
- `_pd_kv_bytes_per_token`：按层数、KV heads、head_dim、`disaggregation.kv_bytes_per_element` 估算；MiniVerl 没有单独的 `num_kv_heads` 字段，默认用 `num_heads`；
- 带宽按 `nixl`/`mooncake`/`naive` 取常数。

关键测试：`tests/test_rollout_checkpoint.py`（PD 与 naive 同 seed 一致）、`examples/18_pd_disaggregation.py`。

读法：先看 `_pd_kv_bytes_per_token` 的公式，再看 `transfer_ms = bytes / bandwidth`。它只算传输账，不模拟真实 prefill/decode 调度；第 15.12 节给出了完整成本模型。

## G.28 `tools.py`

- `ToolSchema`：OpenAI function schema 的最小表示，`from_callable` 从签名推断参数；
- `ToolResult`：`text` + step `reward` + `metrics`；
- `ToolInstance`：每条轨迹一份状态，`create` 拒绝重复 id；
- `BaseTool`：`create/execute/calc_reward/release` 生命周期，保留 legacy `__call__`；
- `CalculatorTool`：AST 白名单求值，拒绝名称/调用/属性；
- `FunctionTool`：包装普通函数，异常转 `error: <Type>`，超时转 `timeout`（协作式）；
- `ToolRegistry`：合并 native/function 工具，重名抛 `ToolCollisionError`，导出 OpenAI schemas。

关键测试：`tests/test_tools_registry.py`、`examples/23_tool_registry.py`。

读法：先看 `BaseTool.create` 的实例表，再看 `ToolRegistry.execute` 如何在未传 `instance_id` 时自动 create/release。`agent.py` 只依赖 `registry.call`，所以工具层可以独立演进。

## G.29 路由回放：`parallel.py` 的 `replay_*`

- `moe_route(..., replay_top_idx, replay_mask)`：用记录值替换 top-k，支持行级 mask；
- `record_moe_routes(result, with_accepted=False)`：复制 `top_idx`；`with_accepted=True` 时连 `accepted` 一起记录；
- `replay_moe_routes(layer_logits, recorded_routes, ...)`：逐层重放，层数不一致抛 `ValueError`；
- `router_replay_report(recorded, replayed)`：返回 `exact_match`、`mismatched_layers`、`first_mismatch`、`mismatched_tokens`、`mismatched_assignments`、`shape_mismatched_layers`。

关键测试：`tests/test_router_replay.py`、`examples/24_router_replay.py`。

读法：先看 `moe_route` 里 `replay_top_idx` 的形状/范围校验，再看 `replay_moe_routes` 的层数检查。它对应真实 verl 的 `RouterReplay`/`RouterReplayAction`（R2/R3），细节见附录 V。

## G.30 `reward_model.py` 与 `trainer/main_rm.py`

- `PreferencePair` / `PreferenceBatch`：偏好数据契约，chosen/rejected 各自 padding；
- `build_preference_pairs`：把 bandit/算术/数学样本转成受控负例；
- `TinyRewardModel`：linear / mlp / transformer 三种 backbone，mean 或 last pooling；
- `bradley_terry_loss`：稳定 `log_sigmoid`，支持 margin 与 label smoothing；
- `pairwise_metrics` / `expected_calibration_error`：accuracy、margin、ECE；
- `RMTrainer`：AdamW + 早停 + best state + `save`；
- `load_reward_model` / `ModelRewardManager`：检查点重载、批量打分、scatter 到最后一个 response token、`without_shaping`；
- `trainer/main_rm.py`：读取与 PPO 相同的 `data`/`trainer` 配置树，RM 字段在 `reward.reward_model`。

关键测试：`tests/test_reward_model.py`、`examples/25_reward_model.py`。

读法：先看 `bradley_terry_loss` 的方向（chosen - rejected），再看 `RMTrainer._score_batch` 的 length normalize，最后看 `ModelRewardManager.run` 如何复用 `RewardManager` 的 token 位置契约。它对应第 17.16 节与附录 X.11；真实 verl 的 reward loop 服务见 X.7。

## G.31 `prm.py` 与 `trainer/main_prm.py`

- `StepSample` / `StepBatch`：步骤标签与**前缀**样本；`group_index` 把前缀映射回轨迹；
- `build_step_samples`：gold 轨迹切步、`local`/`suffix` 标签、按单字符字母数字构造反例；
- `step_prefixes` / `collate_step_samples` / `make_step_batches`：只让第 $k$ 步看到前缀，按整条轨迹分批；
- `step_bce_loss`：`softplus(z) - y*z` + mask；
- `aggregate_step_probs`：`last/first/min/max/mean/product`，乘积在 log 空间；
- `step_metrics`：step accuracy、Brier、trajectory accuracy、mean score；
- `PRMTrainer`：AdamW + 早停 + checkpoint（权重、步长、标签模式、聚合、tokenizer）；
- `load_prm` / `ProcessRewardManager`：边界校验、均匀切分、逐步打分、聚合、scatter、overlong shaping、`outcome_reward_fn` 组合；
- `trainer/main_prm.py`：配置字段在 `reward.prm`，与 PPO 共用 `data`/`trainer` 树。

关键测试：`tests/test_prm.py`、`examples/26_process_reward.py`。

读法：先看 `step_prefixes` 的“只看前缀”不变量，再看 `step_bce_loss` 的稳定形式；随后看 `ProcessRewardManager.score_steps` 如何把 `step_boundaries` 校验为“严格递增且覆盖最后一个 response token”，最后看 `run` 如何同时写 `rm_scores`（PPO 消费）与 `prm_step_scores`（诊断）。它对应第 17.17 节与附录 K；真实 verl 没有内置 PRM 训练器，通常通过自定义 reward function/server 接入 reward loop。
