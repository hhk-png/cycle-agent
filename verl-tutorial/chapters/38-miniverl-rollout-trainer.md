# 第 38 章 MiniVerl 的 Rollout、权重同步与 PPO/GRPO 主循环

## 38.1 NaiveRollout

`mini_verl/rollout.py::NaiveRollout`：

```python
def generate_sequences(self, prompts, sampling=None):
    params = sampling or SamplingParams(...)
    for row in range(batch_size):
        prompt_ids = input_ids[row, :prompt_length]
        generated, log_probs = self._sample_one(prompt_ids, params)
        ...
```

`_sample_one`：

1. 前向当前序列；
2. 取最后一个位置的 logits；
3. 应用 `allowed_chars` mask；
4. temperature/top-k/top-p；
5. 采样；
6. 记录行为策略（temperature/top-k/top-p 之后）的 log-prob；
7. 遇到 EOS/达到上限停止。

输出 DataProto：

```text
input_ids
attention_mask
response_mask
prompt_length
rollout_log_probs
response_length
response_text
```

## 38.2 多采样与 GRPO

`PPOTrainer._rollout_batch`：

```python
repeated = prompts.repeat(n, interleave=True) if n > 1 else prompts
data = self.rollout.generate_sequences(repeated)
```

`interleave=True` 让同一 prompt 的 n 条样本连续排列，`uid` 重复，GRPO 按 uid 分组。

如果顺序错了（例如 `tile`），同一 uid 的样本会混入不同 prompt，优势失真。

## 38.3 sleep/wake

同步 PPO 的流程：

```text
wake -> generate -> sleep -> train -> sync -> wake
```

MiniVerl 的 `NaiveRollout.sleep()`：

- 保存 state dict 到 `_backup`；
- `model=None`；
- 清空 prefix cache（如果 `free_cache_engine`）。

`wake_up()`：

- 按模型配置重建模型；
- 加载 `_backup`；
- `awake=True`。

`update_weights` 在 asleep 时只更新 `_backup`，wake 时生效。这模拟了“训练时释放推理显存、同步时写备份”的生产行为。

## 38.4 CheckpointEngineManager

`mini_verl/sharding.py::CheckpointEngineManager`：

- `naive`：全量；
- `nccl`：全量但时间更短（模拟）；
- `delta`：只发送变化的 tensor；
- `delta_sharded`：按变化元素数估算线缆字节（含索引开销），模拟 element 粒度；适合 LoRA/稀疏更新。

记录：

```text
sync/bytes
sync/tensors
sync/changed_ratio
sync/time_ms
sync/version
```

`PPOTrainer._sync_weights` 每次从 actor rank 0 取 state dict，交给 manager。

## 38.5 PPO/GRPO 主循环

`PPOTrainer.train_step`：

```python
# 1. 取 batch / async buffer
data = _rollout_batch(_next_train_batch())

# 2. reward
data = reward_manager.run(data)

# 3. DAPO 过滤（可选）
if filter_groups.enable:
    data = filter_groups_for_dapo(...)

# 4. 长度均衡
data, balance_metrics = _balance_batch(data)

# 5. 全局 token 数
data.meta_info["global_num_tokens"] = int(mask.sum())

# 6. old log prob
data = actor_group.compute_log_prob(data)

# 7. ref log prob（可选）
if use_ref: data = actor_group.compute_ref_log_prob(data)

# 8. values（可选）
if use_critic: data = critic_group.compute_values(data)

# 9. advantage（可含 in-reward KL）
data = _compute_advantage(data)

# 10. critic update
if use_critic: _update_worker(critic_group, ...)

# 11. actor update
_update_worker(actor_group, ...)

# 12. weight sync
_sync_weights()

# 13. metrics
```

## 38.6 一次 bandit 训练发生了什么

配置（`configs/grpo_bandit.yaml` 示例；dataclass 默认的 `total_training_steps` 不同，以文件为准）：

```yaml
data.dataset: bandit
data.train_size: 64
data.train_batch_size: 16
data.max_prompt_length: 4
data.max_response_length: 1
actor.ppo_mini_batch_size: 64
actor.ppo_micro_batch_size: 64
rollout.n: 8
algorithm.adv_estimator: grpo
trainer.total_training_steps: 40
```

流程：

1. 取 16 个 prompt（`=A`/`=B`/`=C`/`=D`）；
2. repeat 8 → 128 条；
3. rollout 在 `xyzw` 中采样；
4. reward 检查首字符；
5. GRPO 按 uid 分组，组内 8 条；
6. 随机打乱 mini-batch（64 条）；
7. PPO 更新；
8. 30～60 步后验证准确率到 1.0。

初始策略的动作 logits 被校准为均匀（`_calibrate_action_head`），避免随机初始化偏向某个动作导致 GRPO 无探索。

## 38.7 为什么 mini-batch 要 shuffle

如果 `ppo_mini_batch_size` 正好等于 `rollout.n`，每个 mini-batch 只包含一个 prompt 组。模型会在该组上更新，但看不到其他上下文，容易过拟合/崩溃。

MiniVerl 的 `_update_worker` 在 `update_actor` 时按 `actor.shuffle` 打乱整个 batch，再切 mini-batch。这样每个 mini-batch 混合多个上下文。

真实 verl 的 `actor.shuffle` 也控制这一点。

## 38.8 指标

`train_step` 返回：

```text
data/reward_mean/std/max/min
data/accuracy
data/response_length_*
data/advantage_mean/std
actor/pg_loss/clipfrac/ppo_kl/entropy
critic/vf_loss/clipfrac
timing/*
perf/*
train/reward_mean
train/step_time_s
```

`MetricsLogger` 写 `metrics.jsonl`，console 打印关键项。

## 38.9 验证

`validate`：

- 用 `val_batches`；
- `SamplingParams(temperature=0, do_sample=False)`；
- reward；
- `compute_validation_metrics`。

验证时如果 rollout 在 sleep，会先 wake，验证后 sleep。

## 38.10 异步模式

`trainer.trainer_mode=colocate_async`：

- init 时 `_produce_async_batch()` 预生成；
- `train_step` 从 `ReplayBuffer` 采样；
- `bypass_mode=true` 时用 `rollout_log_probs` 作为 old logprob；
- 训练更新后、权重同步前 `_produce_async_batch()` 生成下一批，因此下一批带的是**同步前**的版本，消费时 staleness 非零；
- `parameter_sync_step` 控制权重同步频率；设得越大，旧样本越多。

`separate_async` 在 MiniVerl 里复用同一个 `ReplayBuffer` 与同一组 worker，只是名字上区分“独立 rollout 资源池”的生产语义；它**没有**单独的默认 `parameter_sync_step`。真实 verl 的 separate_async 有独立资源池与同步器，MiniVerl 只模拟版本/staleness 契约。

这里有一个容易误读的语义差：MiniVerl 的 `trainer.parameter_sync_step` 是“每 N 个 global step 同步一次”（`global_steps % N == 0`），而真实 V1 的 `trainer.v1.<mode>.parameter_sync_step` 是“把一个 global step 拆成 N 次 local update，最后同步一次”（第 21.11 节）。两者都降低同步频率，但数据消费、优化器步数与指标聚合的口径不同；对照真实 verl 时不要直接按字段名映射。

在 MiniVerl 里验证 staleness：

```python
# examples/10_async_rollout.py 的核心断言（简化）
trainer = PPOTrainer(config).init()
data, metrics = trainer._async_buffer.sample(...)
assert metrics["async/staleness"] > 0   # 否则 async 退化成了 sync
```

如果 staleness 恒为 0，先检查 refill 是否发生在权重同步之后；MiniVerl 把 `_produce_async_batch()` 放在 `_sync_weights()` 之前，就是为了让下一批带旧版本。

staleness 的版本号现在按 **actor 更新步**计数（`global_steps`），不是权重同步版本；否则 `parameter_sync_step>1` 时同步版本不变，指标会永远看不到 2 步以上的滞后。`max_off_policy_threshold=0` 时，过期样本被全部丢弃后 trainer 会补采一批新数据，而不是抛 `IndexError`。

## 38.11 一个完整运行

```bash
python -X utf8 -m mini_verl.trainer.main_ppo \
  trainer.total_training_steps=30 \
  trainer.val_before_train=true
```

输出（CLI 打印逐 step 日志、`steps=N` 和 history 字典；下面的“友好摘要”来自 `examples/01_quickstart.py`）：

```text
[step 0001] data/reward_mean=0.25 ...
...
steps=30
{'actor/loss': ..., 'data/accuracy': ..., ...}
```

如果只想看最终结论，跑 `python -X utf8 examples/01_quickstart.py`，它会打印：

```text
steps=30
final training reward=1.000
validation accuracy=1.000
```

## 38.12 PD 分离与 TransferQueue 的最小实现

### PDDisaggregatedRollout

`mini_verl/rollout.py::PDDisaggregatedRollout` 继承 `NaiveRollout`，采样逻辑完全复用，只增加三段账：

```python
prompt_tokens = sum(prompts.batch["prompt_length"])
kv_bytes = prompt_tokens * kv_bytes_per_token
transfer_ms = kv_bytes * 8 / bandwidth_gbps / 1e9 * 1000   # Gbps 是 bit/s
```

`kv_bytes_per_token` 由层数、KV heads、head_dim 与 `kv_bytes_per_element` 估算（MiniVerl 没有单独的 KV-head 维度，默认复用 `num_heads`）；带宽按 `nixl`/`mooncake`/`naive` 取不同常数。它不是性能模型，而是提醒你：**PD 拆分的收益 = 两个阶段各自提升的利用率 − KV 传输成本**。当 prompt 很长、链路很快时，传输项可以忽略；当链路是 25 Gbps 时，它可能吃掉全部收益。

`examples/18_pd_disaggregation.py` 打印 `pd/prefill_tokens`、`pd/kv_transfer_bytes`、`pd/transfer_ms`；测试验证它与 naive 采样在相同 seed 下逐 token 一致，确保“拆分拓扑没有改变算法”。

### TransferQueue

`mini_verl/transfer_queue.py` 实现单进程版：

- `put(data, version, produced_at)`：生产者写入；
- `get(batch_size)`：消费者按样本数拉取，支持部分取用与剩余回队；
- `try_get`：非阻塞；
- `snapshot/restore`：恢复未消费样本与计数器；
- `max_batches` + `drop_oldest/block`：背压；
- `stats`：`tq/produced_samples`、`tq/consumed_samples`、`tq/dropped_samples` 等。

它和 `ReplayBuffer` 的区别是：ReplayBuffer 在 trainer 内部按版本过滤，TransferQueue 在生产/消费之间传递样本并携带生产版本。`examples/19_transfer_queue.py` 演示部分取用、容量淘汰与快照恢复。

### 为什么 MiniVerl 不做真并发

真并发需要线程/进程、共享内存、超时与取消；这些会把“算法是否正确”与“调度是否 race”混在一起。MiniVerl 用顺序执行 + 显式队列，保留了**可观察的契约**：

- 样本带版本；
- 队列有界；
- 快照可恢复；
- 消费可部分进行。

等你把契约写对，再换成 Ray actor / 共享内存 / RDMA，语义不变。

## 38.13 CISPO 与独立评测：两条新增的端到端路径

MiniVerl 在核心 PPO/GRPO 之外补了两条可运行路径：一条验证“换损失函数”，一条验证“离开训练循环还能评测”。

### CISPO 的代码路径

CISPO 的入口与 vanilla 完全并列：

```text
config.actor_rollout_ref.actor.policy_loss.loss_mode = "cispo"
  -> workers.update_actor 设置 batch["use_cispo"] = True
  -> losses.actor_loss_fn 选择 compute_policy_loss_cispo
  -> algo/core_algos.compute_policy_loss_cispo
       ratio = exp(log_prob - old_log_prob)
       weight = detach(clip(ratio, 1-low, 1+high))
       loss = agg_loss(-weight * advantage * log_prob, mask, ...)
```

三个容易写错的点都在这一小段里：

1. `use_cispo` 必须从配置传到 worker，再到 `actor_loss_fn`；少一层就会静默走 vanilla。
2. `detach()` 只作用在裁剪后的权重上，不能作用在 `log_prob` 上；否则没有策略梯度。
3. `loss_agg_mode` 沿用配置（默认 `token-mean`），不能像 GSPO 那样强制 `seq-mean-token-mean`。

`tests/test_core_algos.py` 用固定 batch 锁定了 loss 数值与梯度；`tests/test_trainer.py::test_cispo_training_improves` 证明它在 bandit 上能学到 100%；`examples/20_cispo_training.py` 同时打印 PPO 与 CISPO 在越界比率上的损失差。

### 独立评测的代码路径

`mini_verl/trainer/main_eval.py` 不训练，只做四件事：

```text
checkpoint/config.yaml -> PPOConfig
checkpoint/actor.npz   -> actor state
prompt x n             -> rollout.generate_sequences
reward_manager=naive   -> rm_scores -> pass@k
```

关键设计：

- **配置来自 checkpoint**，不是当前工作区；这样三个月后还能复现同一口径。
- **强制 naive reward**，避免 DAPO overlong/格式 shaping 污染能力指标。
- **`interleave=True`**，保证同一 prompt 的 n 条样本连续，`compute_pass_at_k` 才能正确分组。
- **`n >= max(ks)`**，否则 pass@k 无定义；入口会提前报错。

`examples/21_standalone_eval.py` 演示“训练 → 保存 → 加载 → pass@k”；`tests/test_trainer.py::test_evaluate_checkpoint_reloads_config` 用新进程内的新 trainer 验证 checkpoint 可独立加载。

### 边界

- 没有实现批量生成/常驻服务；真实 verl 的 `main_generation_server.py` 是“启动 standalone rollout server → 批量生成 parquet”的脚本，常驻 API、鉴权与限流属于部署层（见 24.13 与附录 J）。
- 评测只支持 MiniVerl 的合成任务与规则奖励；真实 benchmark 的 parser、chat template、污染控制见附录 L。
- CISPO 只实现了 token 级比率版本；序列级/层级变体需要改 ratio 的定义与 aggregation，不能只换一个枚举值。

## 本章小结

- MiniVerl 的 rollout 保留 n 采样、约束解码、log-prob、sleep/wake、版本。
- CheckpointEngineManager 模拟 naive/nccl/delta/delta_sharded。
- train_step 与 V1 `_step_once` 同构。
- mini-batch shuffle 与动作校准是 bandit 能学会的两个关键细节。
- 异步模式用 ReplayBuffer + bypass 模式演示 staleness。
- CISPO 通过 `policy_loss.loss_mode` 选择，权重 detach、score-function 梯度保留，有公式/梯度/端到端测试。
- `main_eval` 从 checkpoint 读配置、强制 naive reward、按题分组算 pass@k，不依赖训练循环。

## 延伸阅读

- `mini_verl/rollout.py`
- `mini_verl/sharding.py`
- `mini_verl/trainer/ppo_trainer.py`
- 下一章：测试工程：单测、差分、不变量、端到端。
