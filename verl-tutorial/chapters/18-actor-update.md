# 第 18 章 Actor 更新：PPO loss、KL、entropy 与 loss aggregation

## 18.1 一次 update_actor 经过哪些层

真实 verl 的调用链：

```text
PPOTrainer._update_actor
  -> actor_rollout_wg.update_actor(batch)
    -> ActorRolloutRefWorker.update_actor
      -> TrainingWorker.train_mini_batch
        -> BaseEngine.train_batch
          -> forward_backward_batch
            -> ppo_loss
```

每一层职责不同：

- trainer：切 mini-batch、聚合指标；
- worker：设置 loss 函数、micro-batch、PPO epochs；
- engine：前向/反向、优化器、梯度裁剪；
- loss：策略损失、KL、熵、aggregation。

MiniVerl 用 `ActorRolloutRefWorker.update_actor` + `Engine.forward_backward_batch` + `actor_loss_fn` 复现这条链。

## 18.2 PPO loss 的输入

```text
log_prob        # 当前策略对响应 token 的 log-prob
old_log_prob    # 采样时策略的 log-prob
advantages      # (B, T)，token 级
response_mask   # (B, T)
ref_log_prob    # 可选，用于 KL loss
```

输出：

```text
pg_loss
pg_clipfrac
ppo_kl
entropy
kl_loss
```

## 18.3 计算顺序

`verl/workers/utils/losses.py::ppo_loss` 的核心：

1. 前向得到 logits；
2. 计算当前 log-prob；
3. 计算 policy loss（vanilla/GSPO/...）；
4. 可选 KL loss；
5. 可选 entropy；
6. 按 `loss_agg_mode` 聚合；
7. 反向。

MiniVerl 的 `losses.actor_loss_fn` 顺序相同：

```python
logits = model(input_ids)
log_probs = log_probs_from_logits(logits, input_ids, mask, allowed_ids)
pg_loss, metrics = compute_policy_loss(old, log_probs, advantages, mask, ...)
if use_kl_loss:
    kl_loss = agg_loss(kl_penalty_tensor(log_probs, ref, type) * mask, mask, ...)
    loss += kl_loss * coef
if calculate_entropy:
    # logits[t] predicts token t+1, so shift logits and mask together.
    entropy = entropy_from_logits(mask_logits[:, :-1], mask[:, 1:])
    entropy_loss = compute_entropy_loss(entropy, mask[:, 1:])  # -H
    loss += entropy_loss * entropy_coeff                      # -coeff * H
loss.backward()
```

## 18.4 micro-batch 与 gradient accumulation

显存有限时，一个 mini-batch 要拆成多个 micro-batch：

```text
mini_batch_size = 64
micro_batch_size_per_gpu = 4
gradient_accumulation_steps = 16
```

前 15 个 micro-batch 只 backward，最后一个再 optimizer step。注意：

- 梯度要累加，不能每个 micro-batch 清零；
- loss 归一化要按全局 token 数，不能每个 micro-batch 各自平均；
- 如果使用 FSDP，`no_sync` 可以减少中间 all-reduce。

verl 的 `use_no_sync_for_gradient_accumulation` 控制梯度累积期间的同步：前 N−1 个 micro-batch 在 `no_sync()` 下只累加本地梯度，最后一个 micro-batch 才做 all-reduce / reduce-scatter。它不是“最后一层是否同步”的开关。

MiniVerl 的 `forward_backward_batch(zero_grad=(first))` 模拟梯度累加；worker group 在所有 micro-batch 后统一 all-reduce。

## 18.5 PPO epochs

`ppo_epochs` 表示同一个 mini-batch 被重复用于更新几次。常见设置：

- 1：稳定、计算少；
- 2～4：提高样本利用率，但容易过拟合旧数据；
- 与 `clip_ratio` 一起调：epochs 多时裁剪要更紧。

verl v0.9.1 默认 `ppo_epochs=1`。原因是现代 RL 更依赖新样本，而不是重复旧样本。

MiniVerl 在 trainer 层循环 epochs，每个 epoch 调一次 `update_actor` RPC，因此每个 epoch 都会做一次梯度 all-reduce 与 optimizer step。这与“在一个 RPC 内循环 epochs 再 step”不同，后者会把多个 epoch 的梯度累加后只更新一次。

## 18.6 动态 batch

开 `use_dynamic_bsz=true` 后，verl 不再按固定 micro-batch size 切，而是按 token 预算：

```text
ppo_max_token_len_per_gpu = 16384
```

它把样本按长度排序/打包，使每个 micro-batch 的 token 数接近预算。优点：

- 长序列不会 OOM；
- 短序列不会浪费；
- 吞吐更稳。

代价：

- 每个 micro-batch 的样本数不同；
- loss 归一化更复杂；
- 与 TP/PP 组合时要小心。

## 18.7 约束解码与 log-prob

如果 rollout 用了 allowed_chars/top-k 等约束，训练侧计算 log-prob 时也必须用同样的 mask。否则：

- `old_log_prob` 与 `log_prob` 不在同一个分布；
- 比率 $\rho$ 失真；
- PPO 梯度方向错误。

MiniVerl 在 `logprob_metric_fn` 与 `actor_loss_fn` 中都用 `allowed_token_ids` 对 logits 加 mask。测试 `test_actor...` 与 bandit 训练验证了这一点。

## 18.8 梯度裁剪

`clip_grad=1.0` 是常见默认。它在 optimizer step 前对全局梯度范数裁剪：

$$
g\leftarrow g\cdot\min\left(1,\frac{c}{\lVert g\rVert}\right).
$$

注意：

- 裁剪的是全局范数，不是逐参数；
- 与 FSDP 的 reduce-scatter 顺序要对；
- 梯度范数本身是重要指标，突然变大通常意味着数值问题。

MiniVerl 的 `clip_gradients` 与 `grad_norm` 指标直接对应。

## 18.9 学习率与 scheduler

常见：

- actor lr：1e-6～1e-5；
- critic lr：1e-5～1e-4（比 actor 大）；
- warmup：少量步数；
- cosine/constant。

如果 loss 震荡：

- 降 lr；
- 降 `ppo_epochs`；
- 增大 `clip_ratio` 的保守性（减小上界）；
- 检查 advantage 尺度。

## 18.10 常见失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| pg_loss 一直 0 | advantage 全 0（组内无差异） | 检查 n、reward、filter |
| pg_clipfrac 接近 1 | lr 太大/old logprob 不一致 | 降 lr、检查 mask/约束 |
| ppo_kl 爆炸 | 同上 | 同上 |
| entropy 迅速降到 0 | 过度优化/温度太低 | 加 entropy_coeff、升温度 |
| grad_norm 爆炸 | 数值问题/奖励尺度过大 | 梯度裁剪、归一化奖励 |
| 训练 reward 涨但验证不涨 | 过拟合/奖励 hacking | 早停、换验证集 |

## 18.11 MiniVerl 对照

- `mini_verl/workers.py::ActorRolloutRefWorker.update_actor`
- `mini_verl/losses.py::actor_loss_fn`
- `mini_verl/algo/core_algos.py::compute_policy_loss`
- `mini_verl/engine.py::BaseEngine.forward_backward_batch/finalize_update`
- `mini_verl/single_controller/worker_group.py::_sync_gradients_and_step`

MiniVerl 的 `sync_gradients=True` 把 DP 梯度平均显式化：每个 worker 在自己的 chunk 上 backward，group 平均梯度，再让每个 worker step。真实 verl 在 FSDP 内部完成 all-reduce/reduce-scatter；语义相同。

## 18.12 PPO 稳定性：把四个指标一起看

单看 `pg_loss` 无法判断训练是否健康。至少同时看：

| 指标 | 健康区间（经验） | 异常含义 |
|---|---|---|
| `actor/ppo_kl` | 0.001～0.02，随训练下降 | 过大：更新太猛/old logprob 不一致 |
| `actor/pg_clipfrac` | 0.05～0.3 | 接近 1：几乎全被裁，lr/epochs 太大 |
| `actor/entropy` | 缓慢下降，不为 0 | 迅速到 0：探索消失/温度太低 |
| `grad_norm` | 稳定，偶有尖峰 | 持续爆炸：奖励尺度/数值问题 |
| `actor/ratio_mean` | 接近 1 | 远离 1：行为策略与当前策略脱节 |
| `data/advantage_std` | 非 0，量级稳定 | 0：组内无差异；过大：奖励尺度失衡 |

四个联合诊断：

1. **KL 高 + clipfrac 高 + entropy 下降**：更新过猛。先降 lr、降 `ppo_epochs`、收紧 clip 上界；不要先调 batch。
2. **KL 低 + clipfrac 低 + reward 不涨**：更新太保守或信号太弱。检查 advantage 是否全 0、reward 是否有区分度、lr 是否太小。
3. **entropy 快速到 0 + reward 短期上涨后停滞**：策略塌缩到少数 token。加 entropy bonus、升温度、检查约束解码是否过窄。
4. **grad_norm 尖峰 + KL 尖峰**：通常是数值问题。检查 reward 尺度、mask、dtype、FSDP 梯度同步；必要时开梯度裁剪并复现单个 batch。

一个实用的排查顺序：

```text
reward 分布 → advantage 分布 → ratio 分布 → clipfrac/ppo_kl → entropy → grad_norm
```

如果 reward 分布本身全 0/全 1，后面所有指标都没有意义。先在固定样本上回放 reward，再谈 PPO 超参。

`ratio` 的分布比均值更有信息：均值 1.0 可能掩盖“99% 的 token ratio≈1，1% 的 token ratio=10”。打印 p50/p95/p99，或记录 `actor/ratio_max`，能更早发现异常 token。

## 本章小结

- update_actor 的链路是 trainer → worker → engine → loss。
- PPO loss 的输入是 log_prob、old_log_prob、advantages、mask；KL/entropy 是可选项。
- micro-batch + gradient accumulation 要注意梯度累加与全局归一化。
- 约束解码必须同时作用于 rollout 与训练 log-prob。
- 指标 pg_clipfrac/ppo_kl/entropy/grad_norm 是诊断训练的第一现场。

## 延伸阅读

- `verl/workers/utils/losses.py`
- `verl/trainer/ppo/core_algos.py`
- `verl/workers/config/actor.py`
- 下一章：Critic、GAE 与 value 训练。
