# 第 7 章 Hydra 配置系统与字段地图

## 7.1 为什么配置比代码更难

RL 训练的代码通常只有几百行，配置却有上千个字段。原因是每个字段都对应一个系统折中：

- `rollout.n` 改变生成成本与优势方差；
- `ppo_mini_batch_size` 改变更新次数与显存；
- `gpu_memory_utilization` 决定 rollout 与训练能否共存；
- `tensor_model_parallel_size` 改变权重布局与通信量；
- `loss_agg_mode` 改变优化目标的长度偏置；
- `trainer.v1.trainer_mode` 改变同步/异步语义。

verl 用 Hydra 管理配置。入口 `verl/trainer/main_ppo.py` 上写着：

```python
@hydra.main(config_path="config", config_name="ppo_trainer", version_base=None)
def main(config):
    ...
```

命令行里所有 `key=value` 都会覆盖 YAML。`_generated_ppo_trainer.yaml` 是 flatten 后的参考文件，**不要直接改**；它由 `scripts/generate_trainer_config.sh` 生成。

## 7.2 defaults 组合

`verl/trainer/config/ppo_trainer.yaml` 的开头是：

```yaml
defaults:
  - model_engine: dp
  - actor@actor_rollout_ref.actor: ${model_engine}_actor
  - data@data: legacy_data
  - ref@actor_rollout_ref.ref: ${model_engine}_ref
  - rollout@actor_rollout_ref.rollout: rollout
  - model@actor_rollout_ref.model: hf_model
  - critic@critic: ${model_engine}_critic
  - model@critic.model: hf_model
  - legacy_reward_impl
  - reward@reward: reward
  - algorithm@algorithm.rollout_correction: rollout_correction
  - distillation@distillation: distillation
  - transfer_queue@transfer_queue: transfer_queue
  - _self_
```

这带来两个重要行为：

1. **切换训练后端只改一层。** `model_engine=dp` 时选 FSDP 系列的 actor/critic/ref；改成 `megatron` 时选 Megatron 系列。
2. **同一字段可能来自多个文件。** 例如 `actor_rollout_ref.actor.optim.lr` 来自 `actor/dp_actor.yaml`，`actor_rollout_ref.rollout.n` 来自 `rollout/rollout.yaml`。看最终值时以 `_generated_ppo_trainer.yaml` 或 `print_cfg.py` 为准。

## 7.3 顶层字段地图

v0.9.1 的顶层键。下表的“默认值”是写作时的快照，版本敏感；最终以 `verl/trainer/config/_generated_ppo_trainer.yaml` 与对应 commit 为准。

```text
actor_rollout_ref
├── hybrid_engine
├── nccl_timeout
├── actor
├── ref
├── rollout
└── model
data
critic
reward
algorithm
distillation
transfer_queue
trainer
global_profiler
skip
ray_kwargs
```

### actor_rollout_ref.actor

| 字段 | v0.9.1 默认 | 作用 |
|---|---|---|
| `strategy` | `fsdp` | 训练引擎 |
| `ppo_mini_batch_size` | 256 | 全局 mini-batch（序列数） |
| `ppo_micro_batch_size` | null | 每卡/每 worker 的 micro batch |
| `ppo_micro_batch_size_per_gpu` | null | 每 GPU micro batch |
| `ppo_epochs` | 1 | 每个 mini-batch 的更新轮数 |
| `clip_ratio` | 0.2 | PPO 裁剪 |
| `clip_ratio_low/high` | 0.2/0.2 | 库默认对称裁剪；DAPO recipe 常用 0.2/0.28 |
| `clip_ratio_c` | 3.0 | dual-clip 下界 |
| `entropy_coeff` | 0 | 熵奖励 |
| `use_kl_loss` | false | 是否在 loss 中加 KL |
| `kl_loss_coef` | 0.001 | KL loss 系数 |
| `kl_loss_type` | `low_var_kl` | KL 估计量 |
| `loss_agg_mode` | `token-mean` | 损失聚合 |
| `policy_loss.loss_mode` | `vanilla` | 策略损失：vanilla/gspo/cispo/... |
| `use_rollout_log_probs` | false | 直接用 rollout 记录的 logprob 作为 old |
| `use_dynamic_bsz` | false | 按 token 动态分桶 |
| `ppo_max_token_len_per_gpu` | 16384 | 动态批的 token 上限 |
| `optim.lr` | 1e-6 | 学习率 |
| `optim.clip_grad` | 1.0 | 梯度裁剪 |
| `fsdp_config.param_offload` | false | 参数 offload |
| `fsdp_config.optimizer_offload` | false | 优化器 offload |
| `fsdp_config.reshard_after_forward` | true | FSDP2 前向重分片 |
| `fsdp_config.ulysses_sequence_parallel_size` | 1 | Ulysses SP |
| `use_remove_padding` | 继承 model | 去掉 padding |

### actor_rollout_ref.rollout

| 字段 | v0.9.1 默认 | 作用 |
|---|---|---|
| `name` | `???`（必填） | `vllm`/`sglang`/`hf`/... |
| `mode` | `async` | 同步/异步 server（版本敏感，以生成配置为准） |
| `n` | 1 | 每个 prompt 采样数 |
| `temperature` | 1.0 | 采样温度 |
| `top_k` | -1 | top-k |
| `top_p` | 1 | nucleus |
| `gpu_memory_utilization` | 0.5 | 推理引擎显存占比 |
| `tensor_model_parallel_size` | 2 | 推理 TP |
| `data_parallel_size` | 1 | 推理 DP |
| `max_num_batched_tokens` | 8192 | continuous batching |
| `max_num_seqs` | 256 | 并发序列数 |
| `enable_chunked_prefill` | true | 分块 prefill |
| `enable_prefix_caching` | true | 前缀缓存 |
| `free_cache_engine` | true | 训练时释放 KV cache |
| `logprobs_mode` | `processed_logprobs` | logprob 计算模式 |
| `multi_turn.enable` | false | 多轮 |
| `val_kwargs.temperature` | 0 | 验证贪心 |
| `checkpoint_engine.backend` | `naive` | 权重同步后端 |

### critic

| 字段 | 默认 | 作用 |
|---|---|---|
| `strategy` | fsdp | 引擎 |
| `enable` | null | 是否启用（由算法推断） |
| `ppo_mini_batch_size` | 继承 actor | 更新批大小 |
| `cliprange_value` | 0.5 | 值裁剪 |
| `ppo_max_token_len_per_gpu` | 32768 | 动态批 |
| `model.path` | 必填 | critic 模型 |

### algorithm

| 字段 | 默认 | 作用 |
|---|---|---|
| `adv_estimator` | `gae` | 优势估计 |
| `gamma` | 1.0 | 折扣 |
| `lam` | 1.0 | GAE lambda |
| `norm_adv_by_std_in_grpo` | true | GRPO 标准差归一 |
| `use_kl_in_reward` | false | KL 放奖励 |
| `kl_penalty` | `kl` | KL 估计 |
| `kl_ctrl.type` | fixed | fixed/adaptive |
| `kl_ctrl.kl_coef` | 0.001 | 系数 |
| `filter_groups.enable` | false | DAPO 动态采样 |
| `rollout_correction` | 关闭 | 异步 IS 修正（推导见第 27.6 节） |

**算法三旋钮**：`algorithm.adv_estimator`（优势怎么估）、`actor.policy_loss.loss_mode`（损失怎么裁）、`reward.reward_manager`/`filter_groups`（数据怎么来、怎么滤）。只改其中一个，往往得到的是“半个算法”。

### trainer

| 字段 | 默认 | 作用 |
|---|---|---|
| `total_epochs` | 30 | 数据轮数 |
| `total_training_steps` | null | 覆盖总步数 |
| `n_gpus_per_node` | 8 | 每节点 GPU |
| `nnodes` | 1 | 节点数 |
| `project_name` / `experiment_name` | `verl_examples` / `gsm8k` | 日志/checkpoint；生产应覆盖 |
| `logger` | `[console, wandb]` | 日志后端 |
| `save_freq` | -1 | 保存频率 |
| `test_freq` | -1 | 验证频率 |
| `val_before_train` | true | 训练前验证 |
| `critic_warmup` | 0 | critic 预热步数 |
| `balance_batch` | true | 长度均衡 |
| `resume_mode` | auto | 断点续训 |
| `default_local_dir` | checkpoints/... | checkpoint 目录 |
| `use_v1` | true | V1 trainer |
| `v1.trainer_mode` | `sync` | sync/colocate_async/separate_async（完整键名是 `trainer.v1.trainer_mode`） |
| `v1.separate_async.parameter_sync_step` | 4 | 每几次本地更新同步 |
| `v1.sampler.max_off_policy_threshold` | 8 | staleness 上限 |
| `v1.sampler.max_off_policy_strategy` | drop | 过期样本处理 |

### data

| 字段 | 默认 | 作用 |
|---|---|---|
| `train_files` / `val_files` | 示例路径 | parquet |
| `prompt_key` | prompt | prompt 字段 |
| `max_prompt_length` | 512 | prompt 截断 |
| `max_response_length` | 512 | response 上限 |
| `train_batch_size` | 1024 | 全局 prompt batch |
| `gen_batch_size` | null | 生成批 |
| `val_batch_size` | null | 验证批 |
| `filter_overlong_prompts` | false | 过滤超长 prompt |
| `truncation` | error | 截断策略 |
| `return_raw_chat` | true | 返回原始 chat |
| `image_key` / `video_key` / `audio_key` | images/... | 多模态 |

## 7.4 命令行覆盖

Hydra 的覆盖语法：

```bash
python3 -m verl.trainer.main_ppo \
  actor_rollout_ref.actor.optim.lr=5e-7 \
  actor_rollout_ref.rollout.n=8 \
  algorithm.adv_estimator=grpo \
  trainer.total_epochs=5 \
  trainer.logger='["console","wandb"]'
```

列表要用引号，字符串不要加引号（Hydra 会解析）。`+key=value` 用于添加新键，`~key` 删除键（以 Hydra 版本为准）。`key=null` 表示空值。

MiniVerl 的 CLI 支持同样的 `key=value` 形式，但不支持 `${...}` 插值与 Hydra 的 grouped defaults。它会在启动时调用 `PPOConfig.validate()`，把明显不合法的组合提前报错。

## 7.5 配置组合的四个典型配方

### 7.5.1 单卡 0.5B 调试

```yaml
trainer:
  n_gpus_per_node: 1
  nnodes: 1
  val_before_train: false
actor_rollout_ref:
  rollout:
    name: vllm
    tensor_model_parallel_size: 1
    gpu_memory_utilization: 0.4
  actor:
    ppo_micro_batch_size_per_gpu: 1
critic:
  ppo_micro_batch_size_per_gpu: 1
```

### 7.5.2 8 卡 7B GRPO

```yaml
actor_rollout_ref:
  actor:
    ppo_mini_batch_size: 64
    ppo_micro_batch_size_per_gpu: 4
    use_kl_loss: true
  rollout:
    n: 8
    tensor_model_parallel_size: 2
    gpu_memory_utilization: 0.5
algorithm:
  adv_estimator: grpo
trainer:
  n_gpus_per_node: 8
```

### 7.5.3 32B Megatron + EP

```yaml
model_engine: megatron
actor_rollout_ref:
  actor:
    strategy: megatron
    megatron:
      tensor_model_parallel_size: 4
      pipeline_model_parallel_size: 2
      expert_model_parallel_size: 8
  rollout:
    tensor_model_parallel_size: 4
```

具体字段名以 `verl/trainer/config/actor/megatron_actor.yaml` 为准；v0.9.1 的 `engine`/`megatron` 配置在 `verl/workers/config/` 下有 dataclass 定义。

### 7.5.4 异步分离

```yaml
trainer:
  nnodes: 1
  n_gpus_per_node: 8
  v1:
    trainer_mode: separate_async
    separate_async:
      parameter_sync_step: 4
    sampler:
      max_off_policy_threshold: 8
      max_off_policy_strategy: drop
```

## 7.6 配置校验

v0.9.1 的 `main_ppo.py` 在启动前调用：

```python
validate_config(
    config=config,
    use_reference_policy=need_reference_policy(config),
    use_critic=need_critic(config),
)
```

它会检查：

- `use_kl_loss` 是否需要 ref；
- `adv_estimator` 是否需要 critic；
- batch 是否能被 mini-batch 整除；
- TP/PP 是否超过可用 GPU；
- rollout 后端是否安装；
- Megatron/FSDP 的必填字段。

MiniVerl 的 `PPOConfig.validate()` 覆盖了其中一部分：

- `train_batch_size * rollout.n` 必须能被 `ppo_mini_batch_size` 整除；
- `rollout.n >= 1`；
- loss/estimator/trainer_mode/policy_loss/reward_manager/model.arch/kl_penalty/resume_mode 的枚举值；
- TP 不超过 GPU 数；
- KL 控制器类型。

嵌套配置里的未知键会触发 warning（而不是静默丢弃），例如把 `loss_agg_mode` 拼成 `loss_agg_modee`。这是为了抓“改了配置但行为没变”的假象。

## 7.7 常见配置错误

1. **把 `ppo_mini_batch_size` 当成“每卡 batch”。** 它通常是全局序列数；真正的每卡 batch 是 `ppo_micro_batch_size_per_gpu`。
2. **`rollout.n` 与 `train_batch_size` 的乘积忘记算。** GRPO 的总序列数是两者乘积。
3. **`use_kl_loss=true` 但没配 ref。** 会多建一份模型；如果显存不够，先确认是否真的需要。
4. **`gpu_memory_utilization` 太高。** vLLM 会预分配大部分显存，训练侧 OOM。
5. **`free_cache_engine` 与异步模式冲突。** 异步生成需要 KV cache 常驻，释放策略要按模式调整。
6. **`loss_agg_mode` 与算法不匹配。** DAPO/DrGRPO 通常用 token-level；序列级目标要显式改。
7. **`filter_groups.metric` 指向不存在的字段。** 只会静默地不过滤或全过滤。

## 7.8 MiniVerl 配置对照

MiniVerl 的 `mini_verl/config.py` 定义了：

- `OptimConfig`
- `EngineConfig`
- `ModelConfig`
- `ActorConfig` / `RefConfig` / `CriticConfig`
- `RolloutConfig`
- `DataConfig`
- `AlgorithmConfig` / `FilterGroupsConfig` / `KLControlConfig`
- `RewardConfig`
- `TrainerConfig`
- `ActorRolloutRefConfig`
- `PPOConfig`

字段名尽量与 verl 对齐，但为了 CPU 可运行做了简化：

- 没有 Hydra 插值；
- 没有 grouped defaults；
- `data.dataset` 取代 `train_files`（合成数据）；
- `rollout.name` 只支持 `naive`/模拟；
- 部分多模态/后端字段未实现，但保留在配置结构中，便于对照。

附录 A 给出逐字段对照表。

## 本章小结

- verl 用 Hydra grouped defaults 组合 actor/critic/rollout/model/algorithm。
- `_generated_ppo_trainer.yaml` 是最终值的参考，不要手改。
- 最重要的字段是 batch 层级、rollout.n、TP/PP、KL、loss aggregation 与 trainer mode。
- 配置错误往往不报错，而是表现为 OOM、吞吐低或曲线异常。
- MiniVerl 用 dataclass + `key=value` 覆盖，保留了 verl 的字段命名。

## 延伸阅读

- 配置文档：<https://verl.readthedocs.io/en/latest/examples/config.html>
- `verl/trainer/config/ppo_trainer.yaml`
- `verl/trainer/config/_generated_ppo_trainer.yaml`
- 下一章：数据准备与 RLHFDataset。
