# 第 20 章 参考模型与 KL 控制

## 20.1 为什么需要参考模型

RL 优化的是奖励，不是“像人类”。如果没有约束，策略会：

- 利用奖励函数的漏洞（reward hacking）；
- 输出分布漂移，语言能力退化；
- 在数学/代码任务上学会“猜答案”；
- 忘记 SFT 学到的格式与安全行为。

参考模型 $\pi_{\text{ref}}$ 通常是 SFT 后的模型。KL 惩罚让策略不要偏离太远：

$$
\mathrm{KL}(\pi_\theta\Vert\pi_{\text{ref}})
=\mathbb{E}_{\pi_\theta}\left[\log\frac{\pi_\theta(y\mid x)}{\pi_{\text{ref}}(y\mid x)}\right].
$$

## 20.2 参考模型什么时候构建

verl 的 `need_reference_policy(config)` 判断：

- `actor.use_kl_loss=true`；
- `algorithm.use_kl_in_reward=true`。

只有需要时才构建 ref，否则省一份模型显存。v0.9.1 的 `PPOTrainer.init` 会根据这个标志决定是否创建 ref worker。

MiniVerl 的 `ActorRolloutRefWorker` 在 role 包含 `ref` 或 `use_kl_loss` 时构建 ref，并加载 actor 的初始权重；之后 ref 冻结。

## 20.3 两种 KL 放置

### in-reward KL

```text
token_rewards = token_rewards - kl_coef * kl(old_log_prob, ref_log_prob)
```

然后做 GAE/GRPO。优点：

- KL 直接影响优势；
- 不需要在 loss 里再加一项；
- 适合 PPO。

缺点：

- KL 与奖励尺度耦合；
- 自适应控制器调的是 `kl_coef`，影响 advantage 尺度。

### KL loss

```text
loss = pg_loss + kl_coef * mean(kl_penalty)
```

优点：

- 与奖励解耦；
- 适合 GRPO；
- 可以直接用低方差 KL。

缺点：

- 需要额外的 ref 前向；
- 与 entropy/其他正则一起调。

verl 默认 `use_kl_in_reward=false`、`actor.use_kl_loss=false`；RLHF 常开 in-reward KL，GRPO 常开 KL loss。

## 20.4 KL 估计量：k1/k2/k3 与未实现的 full

| 名称 | 公式 | 方差 | 偏差 |
|---|---|---|---|
| k1 | $\log p-\log q$ | 高 | 无偏 |
| k2 | $\frac12(\log p-\log q)^2$ | 低 | 有偏（不是 KL） |
| k3 | $e^{\log q-\log p}-(\log q-\log p)-1$ | 低 | 近似无偏 |
| full | 对词表求和 | 最低 | v0.9.1 未实现（`NotImplementedError`） |

verl 的 `kl_penalty` 支持 `kl`/`abs`/`mse`/`low_var_kl`，并接受别名 `k1`/`k2`/`k3`；`full` 会直接抛 `NotImplementedError`。部分版本还支持 `+` 后缀的 straight-through。默认值要分入口看：`algorithm.kl_penalty`（in-reward KL）默认 `kl`；`actor.kl_loss_type`（KL loss）默认 `low_var_kl`。

MiniVerl 的 `losses.kl_penalty_tensor` 实现了可微的 k1/k2/k3/abs；`core_algos.kl_penalty` 实现 numpy 版本用于指标/奖励。

## 20.5 自适应 KL 控制器

固定系数的问题是：训练早期 KL 很小，惩罚无关紧要；后期 KL 爆炸，惩罚又太晚。自适应控制器根据当前 KL 调整系数：

```python
error = clip(current_kl / target_kl - 1, -0.2, 0.2)
coef *= 1 + error * n_steps / horizon
```

verl 的 `AdaptiveKLController` 与 MiniVerl 的实现一致。

参数：

- `kl_ctrl.type=fixed|adaptive`；
- `kl_ctrl.kl_coef`；
- `kl_ctrl.target_kl`；
- `kl_ctrl.horizon`。

## 20.6 ref 的显存与并行

ref 是一个完整模型，通常与 actor 共享并行策略：

- FSDP：ref 也分片；
- Megatron：ref 也用 TP/PP；
- ref 不需要优化器/梯度，显存约为 actor 的 1/3～1/2；
- ref 前向可以 micro-batch/动态 batch；
- ref 从不更新，可以 offload 到 CPU（代价是延迟）。

verl 的 `ref.log_prob_micro_batch_size_per_gpu`、`ref.log_prob_use_dynamic_bsz`、`ref.log_prob_max_token_len_per_gpu` 控制这些。

## 20.7 ref log-prob 的对齐

ref 与 actor 必须：

- 同一 tokenizer；
- 同一 chat template；
- 同一 attention mask；
- 同一约束解码 mask；
- 同一 dtype（至少数值接近）。

如果 ref 用了不同的 mask，KL 会被错误放大。MiniVerl 的 `compute_ref_log_prob` 与 `compute_log_prob` 共用 `logprob_metric_fn` 和 `allowed_token_ids`，保证对齐。

## 20.8 KL 的监控

真实 verl 的关键指标：

- `actor/ppo_kl`：当前策略与 old 策略的 KL；
- `actor/kl_loss`：KL loss 项（`use_kl_loss=true` 时）；
- `actor/reward_kl_penalty`：in-reward KL 的大小；
- `kl_coef`：自适应控制器的当前系数（会被加上 `actor/` 前缀）。

MiniVerl 额外记录 `data/kl_old_ref`（old 与 ref 的 KL），它不是 v0.9.1 的指标名；对照日志时不要混用。

经验：

- KL 突然上升：lr 太大、数据分布变化、reward 尺度变化；
- KL 长期为 0：约束没生效（mask/ref 未加载）；
- KL 为负：估计量/实现错误（k1 可以负，但平均应接近正）。

## 20.9 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| KL 一直 0 | ref 未加载/use_kl 未生效 | 检查 `need_reference_policy` |
| KL 爆炸 | lr/奖励尺度 | 降 lr、加 KL、检查 mask |
| 显存不够 | ref 与 actor 同规模 | 降 micro batch、offload、用 GRPO |
| 训练变慢 | ref 前向 | 动态 batch、减少 ref 频率（近似） |
| 输出退化 | KL 太弱 | 增大 coef、用 k3、检查 reward |

## 20.10 MiniVerl 对照

- `mini_verl/workers.py::ActorRolloutRefWorker.compute_ref_log_prob`
- `mini_verl/losses.py::kl_penalty_tensor`
- `mini_verl/algo/core_algos.py::kl_penalty` / `FixedKLController` / `AdaptiveKLController`
- `mini_verl/trainer/ppo_trainer.py::_compute_advantage`（in-reward KL）
- `mini_verl/losses.py::actor_loss_fn`（KL loss）

## 20.11 resume 时 ref 应该是什么

ref 的语义是“训练开始时的策略”，不是一个会随 actor 更新的副本。因此：

- 初始化时：ref 从 actor 的初始权重复制；
- 训练中：ref 冻结，不接收梯度、不进入优化器；
- resume 时：actor 从 checkpoint 恢复，ref 仍应保持初始权重；
- 如果 checkpoint 里单独保存了 ref，加载它；如果没有，就从初始 actor 重建，绝不能把 resume 后的 actor 拷进 ref。

把 actor 拷进 ref 是一个很隐蔽的 bug：KL 项会突然变成 0，策略漂移约束消失，训练看起来“更自由”，但几轮后 entropy/长度可能失控。MiniVerl 的 `ActorRolloutRefWorker.load_actor_state_dict` 只加载 actor；`tests/test_trainer.py::test_resume_does_not_overwrite_reference_model` 专门锁住这个不变量。

## 本章小结

- ref 用于约束策略漂移，只在需要 KL 时构建。
- KL 可以放奖励里或 loss 里；k3 是低方差默认。
- 自适应控制器按 target KL 调整系数。
- ref 的 mask/tokenizer/dtype 必须与 actor 对齐。
- 监控 ppo_kl、kl_old_ref、kl_loss 是发现训练异常的第一手段。

## 延伸阅读

- `verl/trainer/ppo/core_algos.py::kl_penalty`
- `verl/workers/engine_workers.py::compute_ref_log_prob`
- 下一章：主循环逐行：从 prompt 到一次参数更新。
