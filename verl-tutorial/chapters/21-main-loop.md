# 第 21 章 主循环逐行：从 prompt 到一次参数更新

## 21.1 入口

真实入口：

```bash
python3 -m verl.trainer.main_ppo <overrides>
```

`main_ppo.py`：

1. 读取 Hydra 配置；
2. `auto_set_device(config)`；
3. `validate_config(...)`；
4. `run_ppo(config, TaskRunnerV1)`；
5. `ray.init(...)`；
6. `TaskRunnerV1.run(config)`；
7. 构造 `PPOTrainer`，`init()`，`fit(agent_loop_manager)`。

MiniVerl 入口：

```bash
python3 -m mini_verl.trainer.main_ppo
```

对应 `main_ppo.py::main` → `PPOTrainer(config).init().fit()`。

## 21.2 fit 的外层循环

V1 的 `fit`：

```python
if val_before_train:
    validate()
if val_only:
    return
while current_epoch < total_epochs and global_steps <= total_steps:
    metrics = {}
    batch = step(metrics, timing)
    if save_freq and step % save_freq == 0:
        save_checkpoint()
    if test_freq and step % test_freq == 0:
        validate()
    log(metrics)
    global_steps += 1
```

MiniVerl 的 `fit` 结构相同，但用 `total_training_steps` 控制，且没有 TransferQueue/异步线程。

## 21.3 step 的九个阶段（同步在 step 之外）

V1 的 `_step_once` 到 actor update 就结束，**不包含权重同步**：

```python
# 1. sample
batch = replay_buffer.sample(...)

# 2. optional colocated reward
if reward_loop_manager.reward_loop_worker_handles is None:
    batch = _compute_reward_colocate(batch)

# 3. balance batch
batch = _balance_batch(batch)

# 4. old_log_prob
batch = _compute_old_log_prob(batch)

# 5. optional ref_log_prob
if use_reference_policy:
    batch = _compute_ref_log_prob(batch)

# 6. optional values
if use_critic:
    batch = _compute_values(batch)

# 7. advantage
batch = _compute_advantage(batch)

# 8. optional critic update
if use_critic:
    batch = _update_critic(batch)

# 9. actor update
if critic_warmup <= global_steps:
    batch = _update_actor(batch)
```

外层 `fit()` 的顺序是：

```text
step()                  # 内部按 parameter_sync_step 循环 _step_once
  -> save_checkpoint()  # 如果到 save_freq（在 on_step_end 之前）
  -> on_step_end()      # PPOTrainerSync 在这里 update_weights
  -> validate()         # 如果到 test_freq
  -> log(metrics)
```

所以“同步权重”不是第 10 个 phase，而是 `on_step_end` 钩子；`PPOTrainerSync.on_step_end` 才调用 `checkpoint_manager.update_weights`。`colocate_async`/`separate_async` 会覆盖这个钩子，同步时机不同（第 27 章）。

MiniVerl 把同步放进了 `train_step` 末尾，顺序上等价于“每个 step 结束同步一次”：

```python
data = rollout.generate_sequences(...)
data = reward_manager.run(data)
data, balance_metrics = _balance_batch(data)
data = actor_group.compute_log_prob(data)
if use_ref: data = actor_group.compute_ref_log_prob(data)
if use_critic: data = critic_group.compute_values(data)
data = _compute_advantage(data)
if use_critic: _update_worker(critic_group, ...)
_update_worker(actor_group, ...)
_sync_weights()          # MiniVerl 在 train_step 内；真实 V1 在 on_step_end
```

## 21.4 数据形状的变化

以 bandit 为例：

```text
prompt batch:      (16, P)          # 16 个 prompt，P=max_prompt_length
after repeat(n=8): (128, P)
rollout:           input_ids (128, P+R), response_mask (128, R)
reward:            rm_scores (128,), token_level_rewards (128, R)
old_log_prob:      (128, R)
ref_log_prob:      (128, R)（可选）
values:            (128, R)（可选）
advantages:        (128, R)
```

bandit 例子里 R=1，所以看起来像“每行只有一个 response token”；真实任务中 R 是 `max_response_length`，prompt 与 response 在 `input_ids` 里拼在一起，而 `response_mask` 只覆盖 response 段。

注意：

- `train_batch_size` 是 prompt 数；
- `rollout.n` 是每个 prompt 的采样数；
- `ppo_mini_batch_size` 是序列数（不是 prompt 数）；
- `global_num_tokens = response_mask.sum()` 是所有序列的有效 token 数。

## 21.5 一次更新的时间都花在哪

典型 7B、8 卡、512/512 序列的耗时分布（量级）：

```text
gen           30%～50%
old_log_prob   5%～10%
ref            5%～10%
values         5%～10%
update_critic 10%～20%
update_actor  10%～20%
weight sync    1%～10%
```

长尾任务中 `gen` 占比更高；大模型/长序列中 `update_actor` 与 `weight sync` 更显著。

优化顺序：

1. 减少生成长尾（async、partial rollout、长度均衡）；
2. 提高 rollout 吞吐（vLLM/SGLang、prefix cache、chunked prefill）；
3. 降低训练显存/通信（FSDP2、remove padding、dynamic bsz）；
4. 减少权重同步（delta、sleep/wake 重叠）。

## 21.6 指标的含义

一次 step 后应记录：

```text
data/reward_mean, data/reward_std, data/accuracy
data/response_length_mean/max/min
data/advantage_mean/std
actor/pg_loss, actor/pg_clipfrac, actor/ppo_kl, actor/entropy
critic/vf_loss, critic/vf_clipfrac
timing/gen, timing/old_log_prob, timing/ref, timing/values,
timing/update_critic, timing/update_actor, timing/update_weights
perf/tokens_per_s, perf/mfu
```

诊断顺序：

1. reward 是否在动；
2. response length 是否爆炸；
3. advantage 是否全 0/方差过大；
4. clipfrac/ppo_kl 是否异常；
5. 哪个 timing 是瓶颈。

## 21.7 验证循环

`_validate`：

- 从 val dataloader 取 batch；
- 用贪心/低温度生成；
- 计算 reward；
- 记录 accuracy/pass@k；
- 记录生成样例（`log_val_generations`）。

MiniVerl 的 `validate` 用 `SamplingParams(temperature=0, do_sample=False)`，并计算 `val/accuracy`、`val/pass@1`。

验证不参与梯度，但会占用 rollout 引擎。异步模式下要避免验证与训练争抢资源。

## 21.8 checkpoint 与恢复

V1 在 `on_step_end` 之后按 `save_freq` 保存：

- actor/critic 的模型、优化器、scheduler；
- trainer state（global step、epoch）；
- 配置与 tokenizer；
- TransferQueue 的队列状态（如果支持）。

恢复时：

1. 找到最新 checkpoint；
2. 加载模型/优化器；
3. 恢复 global step；
4. 重新初始化 rollout 并同步权重；
5. 重建数据队列。

MiniVerl 的 `CheckpointManager` 保存 `actor.npz`、`critic.npz`、`config.yaml`、`tokenizer.json`、`trainer_state.json`；`_maybe_resume` 加载并 `_sync_weights`。

## 21.9 异常与退出

常见异常：

- rollout OOM；
- NCCL timeout；
- reward 函数异常；
- checkpoint 写失败；
- 数据队列为空。

V1 在 `finally` 中关闭 tracking 与 TransferQueue；Ray actor 默认没有配置自动重启，死亡通常导致任务失败。生产环境要有：

- 超时；
- 重试；
- 断点续训；
- 监控告警。

## 21.10 MiniVerl 的 train_step 逐行

```python
# 1. 取 prompt / 异步 buffer
if async: data, async_metrics = buffer.sample(...)
else: data = _rollout_batch(_next_train_batch())

# 2. reward
data = reward_manager.run(data)

# 3. DAPO 过滤
if filter_groups.enable: data = filter_groups_for_dapo(...)

# 4. 长度均衡
data, balance_metrics = _balance_batch(data)

# 5. 全局 token 数写进 meta
data.meta_info["global_num_tokens"] = int(data.batch["response_mask"].sum())

# 6. old log-prob
data = actor_group.compute_log_prob(data)

# 7. ref log-prob
if use_ref: data = actor_group.compute_ref_log_prob(data)

# 8. values
if use_critic: data = critic_group.compute_values(data)

# 9. advantage（可含 in-reward KL）
data = _compute_advantage(data)

# 10. critic update
if use_critic: _update_worker(critic_group, data, ...)

# 11. actor update
_update_worker(actor_group, data, ...)

# 12. weight sync
_sync_weights()

# 13. metrics
```

这段代码与真实 V1 的差异主要在：

- 没有 TransferQueue；
- 没有 Ray 异步 RPC；
- 没有真正的 rollout server；
- reward loop 是线程池而不是独立 worker；
- checkpoint engine 是 Python 拷贝。

但顺序、数据契约、指标口径是对齐的。

## 21.11 `parameter_sync_step`：一个 global step 里的多次局部更新

V1 的 `step()` 不是“取一个 batch、更新一次”。它先读取：

```python
self.parameter_sync_step = self.config.trainer.v1.get(self.trainer_mode, {}).get(
    "parameter_sync_step", 1
)
assert train_batch_size % parameter_sync_step == 0
sample_batch_size = train_batch_size // parameter_sync_step
```

然后在一个 global step 内循环 `parameter_sync_step` 次，每次跑完整的 `_step_once`（sample → reward → balance → old/ref logp → values → advantage → critic → actor），最后才在 `on_step_end` 里做一次权重同步。

```text
global step t
├── local update 0: 取 B/S 个 prompt，生成，更新 actor/critic
├── local update 1: 取 B/S 个 prompt，生成，更新 actor/critic
├── ...
├── local update S-1
└── on_step_end: checkpoint_manager.update_weights(t)
```

这带来四个容易忽略的语义：

1. **`train_batch_size` 仍是外层 step 的 prompt 数**。每个子步只取 `B/S` 个 prompt；`rollout.n` 再乘一次。把 `B` 当成“每次 optimizer step 的样本数”会高估 on-policy 程度。
2. **权重同步频率是每个 global step 一次，不是每个 local update 一次**。S 越大，同一 global step 内后几个子步用的 rollout 权重越旧；这是用 off-policy 换同步开销。`sync` 模式默认 `parameter_sync_step=1`；`separate_async` 的默认值是 4。
3. **LR scheduler 的 horizon 要乘 S**。源码在初始化时把 `optim.total_training_steps` 改成 `total_training_steps * parameter_sync_step`；否则 cosine/warmup 会在训练结束前就走完。自己写 trainer 时最容易漏这一行。
4. **指标按样本数加权聚合**。`MetricsAggregator.add_step_metrics(..., sample_count=...)` 用的是每个子步的真实样本数（排除 padding）；不是简单平均 S 次。DAPO 过滤后子步样本数不同，简单平均会偏。

`parameter_sync_step` 的调参直觉：

| 现象 | 调整 | 代价 |
|---|---|---|
| 权重同步耗时占比高 | 增大 S | 后几个子步的样本更旧 |
| `off_policy`/staleness 指标变差 | 减小 S | 同步更频繁 |
| 训练吞吐上不去、GPU 等同步 | 增大 S + 异步 | 需要 rollout correction 或 staleness 过滤 |
| 曲线在 global step 内抖动 | 检查子步 batch 是否太小 | 可能需要增大 `B` 或减小 S |

MiniVerl 的对应字段是 `trainer.parameter_sync_step`，但语义不同：它在**每 N 个 global step 之后**同步一次，而不是把一个 step 拆成 N 个子步；因此 MiniVerl 的 LR horizon 只设为总 global step 数，不乘 S。读 MiniVerl 的 `train_step` 时不要把两者当成同一个东西；第 38 章会给出代码级对照。

## 本章小结

- 一次 step 的顺序是 sample → reward → balance → old/ref logp → values → advantage → critic → actor → sync。
- `train_batch_size` 是 prompt 数，`ppo_mini_batch_size` 是序列数，`rollout.n` 是二者之间的乘数。
- 指标诊断顺序：reward → length → advantage → clip/kl → timing。
- 验证与 checkpoint 是主循环的一部分，不是附属脚本。
- V1 的 `parameter_sync_step` 把一个 global step 拆成多次 local update，权重只在 `on_step_end` 同步一次；LR horizon 与指标聚合都要按子步修正。
- MiniVerl 的 `train_step` 是 V1 `_step_once` 的可运行缩小版；它的 `parameter_sync_step` 是“每 N 个 global step 同步”，与 V1 的拆步语义不同。

## 延伸阅读

- `verl/trainer/ppo/v1/trainer_base.py::_step_once`
- `mini_verl/trainer/ppo_trainer.py::train_step`
- 下一章：动态批、长度均衡、DAPO 过滤与过采样。
