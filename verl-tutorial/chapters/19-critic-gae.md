# 第 19 章 Critic、GAE 与 value 训练

## 19.1 什么时候需要 critic

`need_critic(config)` 根据算法判断：

- `adv_estimator=gae`：需要；
- `remax`：需要 baseline，但通常不用 critic（用 greedy reward）；
- `grpo/rloo/reinforce_plus_plus`：不需要；
- `critic.enable=true`：强制启用。

PPO 的 critic 与 actor 同规模，显存/算力开销大。GRPO 的流行很大程度上是因为“去掉 critic 后，7B～32B 模型能在有限集群上跑起来”。

## 19.2 Critic 的结构

v0.9.1 没有名为 `CriticWorker` 或 `ValueModel` 的类。critic 复用通用的 `TrainingWorker`，价值头由引擎提供：

```text
FSDPEngineWithValueHead / MegatronEngineWithValueHead
  -> Transformer trunk
  -> value head (hidden -> 1)
  -> values (B, T)
```

训练时：

- 输入与 actor 相同的 `input_ids`；
- 输出每个 token 的价值；
- 只对 response 位置计算损失；
- 与 actor 共享 tokenizer/模型配置（通常从同一个 HF 模型初始化）。

worker 侧的 RPC 是 `infer_batch`（算 value）与 `train_mini_batch`（更新 critic）；`_compute_values` / `_update_critic` 是 trainer 的方法，不是 worker 方法。MiniVerl 的 `CriticWorker` / `TinyValueModel` 是教学命名，对应真实 verl 的 `TrainingWorker` + `*WithValueHead` 引擎。

## 19.3 值损失

$$
\mathcal{L}_V=\frac12\mathbb{E}_t\left[\max\left((V_\phi(s_t)-\hat R_t)^2,\;(\operatorname{clip}(V_\phi(s_t),V_{\text{old}}-\epsilon,V_{\text{old}}+\epsilon)-\hat R_t)^2\right)\right].
$$

注意：

- `V_old` 是更新前的价值，不是当前前向；
- `returns` 是 GAE 回报，不是原始奖励；
- mask=0 的位置不参与；
- 值损失通常比策略损失大几个数量级，需要独立的 lr/clip。

verl 的 `critic.cliprange_value=0.5`、`critic.optim.lr=1e-5` 是常见起点；如果 critic 明显欠拟合，可以升到 1e-4（通常与 actor 相当或略大）。

## 19.4 GAE 的计算顺序

1. 用 critic 对 batch 做前向，得到 `values`；
2. 用 reward manager 得到 `token_level_rewards`；
3. 用 `compute_gae_advantage_return` 计算 `advantages` 与 `returns`；
4. 更新 critic；
5. 用同一组 `advantages` 更新 actor。

顺序不能反：actor 更新会改变策略，但不影响 GAE（GAE 用的是旧 values 与奖励）。

## 19.5 critic warmup

`trainer.critic_warmup` 表示前 N 步只更新 critic、不更新 actor。目的：

- 让 value 先接近回报；
- 避免早期优势噪声过大；
- 稳定 PPO。

代价：浪费 N 步的生成/训练。通常设为 0～10。

## 19.6 值归一化

值的目标尺度可能很大（例如奖励 0/1，但折扣回报累积到几十）。常见处理：

- 对 returns 做 running mean/std 归一化；
- 对 values 做裁剪；
- 用 reward scaling；
- 用 GAE 的 lambda 控制方差。

verl 没有全局的 value normalization 开关；需要在 reward/算法层处理。MiniVerl 的 `compute_gae` 不做归一化，测试用固定数值验证公式。

## 19.7 多轮与 mask

多轮轨迹的 value 也只在 assistant token 上有意义：

- observation token 的 value 不应参与损失；
- GAE 要能穿过 observation span；
- 最后一个 assistant token 的 bootstrap 为 0。

verl 的 `response_mask` 与 GAE/REINFORCE++ 的 mask 处理覆盖了递推；critic 的 `infer_batch` 返回完整 token 的 value，mask 在 value loss 与 advantage 计算处生效，而不是在 RPC 里提前把 observation 的 value 置零。

## 19.8 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| vf_loss 不降 | lr 太小/目标尺度大 | 调 lr、归一化 returns |
| vf_loss 爆炸 | 奖励尺度过大/数值问题 | 裁剪、降 lr |
| advantages 方差异常 | GAE lambda/gamma 不合适 | 调 lambda、检查 mask |
| actor 不学 | critic 太差/优势全负 | critic warmup、检查 reward |
| critic OOM | 与 actor 同规模 | 用 GRPO、offload、共享 trunk |

## 19.9 MiniVerl 对照

- `mini_verl/nn.py::TinyValueModel`
- `mini_verl/workers.py::CriticWorker`
- `mini_verl/losses.py::critic_loss_fn`
- `mini_verl/algo/core_algos.py::compute_gae_advantage_return`
- `mini_verl/engine.py::value_metric_fn`

MiniVerl 的 GAE 与上游一样，在 mask=0 处冻结 `lastgaelam`/`next_value`（测试 `test_gae_freezes_across_interior_mask` 固定了这一语义）；它额外支持 `truncated` bootstrap，这是与 v0.9.1 的差异。`examples/02_ppo_gae.py` 打印了 `vf_loss` 与 `pg_loss`，可以看到 critic 先收敛、actor 后跟上。

## 19.10 一个数值算例：critic 到底在拟合什么

沿用 2.10 的轨迹：$\gamma=1.0$、$\lambda=0.95$，终止奖励 1 在最后一个 token。critic 的当前预测与 GAE 回报如下：

| $t$ | $V(s_t)$ | $A_t$ | $\hat R_t=A_t+V(s_t)$ | $\frac12(V-\hat R)^2$ |
|---|---|---|---|---|
| 0 | 0.200 | 0.732 | 0.932 | 0.268 |
| 1 | 0.300 | 0.666 | 0.966 | 0.222 |
| 2 | 0.500 | 0.490 | 0.990 | 0.120 |
| 3 | 0.800 | 0.200 | 1.000 | 0.020 |

这张表说明三件事：

1. **critic 的目标不是原始奖励**，而是 GAE 回报 $\hat R_t$。最后一个 token 的目标恰好是终止奖励，前面的 token 是“折扣后的未来奖励”。
2. **越靠后的 token 损失越小**，因为 $V$ 已经接近 1；越靠前的 token 需要 critic 学会“向前传播”价值。
3. **actor 与 critic 用的是同一组 $A_t$**。如果先更新 actor 再算 GAE，优势对应的策略已经变了；正确顺序是先算 old values/GAE，再更新 critic 与 actor。

`tests/test_core_algos.py` 用固定数组锁定 GAE 的数值；把这张表抄进单测，是检查实现是否“算对”而不是“能跑”的最快方法。

## 19.11 值函数的初始化、截断与多轮 bootstrap

### 初始化

value head 通常从 0 附近的小随机值开始。如果初始化过大，早期 $V$ 的尺度与回报不匹配，value loss 会爆炸，优势也会被噪声主导。常见做法：

- value head 用比 trunk 更小的初始化尺度；
- critic 使用独立的学习率（常与 actor 相当或略大；太大时 value loss 会震荡）；
- 先跑若干步 critic warmup，再解冻 actor。

### returns 归一化

回报的尺度随任务变化很大：0/1 奖励的回报在 [0,1]，带长度惩罚或模型奖励时可能到几十。直接回归会让 value loss 的量级不可控。工程上常用：

```text
running_mean, running_var = update(returns)
target = (returns - running_mean) / sqrt(running_var + eps)
```

推理时再把 value 乘回去。注意：归一化必须用**全局**统计量，不能每个 rank 各算各的；否则 DP 下的 value 目标不一致。

### 截断时的 bootstrap

如果轨迹因为 `max_response_length` 被截断，理想目标是：

$$
\hat R_T = r_T + \gamma V(s_T),
$$

而不是 $r_T$。但 **v0.9.1 的 `compute_gae_advantage_return` 没有 `done`/`truncated` 参数**：它从序列末尾以 `nextvalues=0` 开始，一律按终止处理。上游把“截断”当作 reward shaping 问题（DAPO overlong penalty）或 partial rollout 问题，而不是 GAE 的 bootstrap 开关。

MiniVerl 做了一个扩展：rollout 输出带 `truncated` 标志，`compute_gae_advantage_return(..., truncated=...)` 对最后一步使用 critic value 做 bootstrap，DAPO overlong penalty 也读这个标志。这是教学实现与真实 verl 的差异；对照数值时要先确认用的是哪一套。实践中最容易犯的错是：rollout 把截断样本标成 `done=True`，critic 却用 0 bootstrap，导致 value 系统性偏低，优势整体偏移。

### 多轮与 observation

多轮轨迹里，critic 对每个 token 都输出 value，但只有 assistant token 的 value 参与损失。GAE 仍然要在完整 token 序列上递推：

```text
assistant: 调用工具        mask=1
tool:      7              mask=0
assistant: 所以答案是 7    mask=1
```

如果直接把 observation 从序列里删掉，第二个 assistant span 的奖励无法通过 GAE 回到第一个 span；如果 observation 参与 value loss，critic 会去拟合“工具输出的价值”，而这部分动作并不是策略产生的。正确做法是：**mask 控制哪些位置进入损失和 TD 误差，但不把 observation 从时间轴上删掉**；跨越 mask=0 的位置时，`lastgaelam` 与 `nextvalues` **冻结**（不乘 $\gamma$ 或 $\gamma\lambda$，也不置零），这样后续 assistant token 的奖励在倒推时穿过 observation 而不衰减。源码写的是 `lastgaelam = lastgaelam_ * mask + lastgaelam * (1 - mask)`，不是 `lastgaelam *= gamma*lam`。

### 与 actor 的耦合

critic 太强或太弱都会出问题：

- critic 太弱：优势噪声大，actor 学不动；
- critic 太强：优势过拟合到 value 的误差，策略被错误信号带偏；
- critic 与 actor 共享 trunk：省显存，但两个目标的梯度会互相干扰。

verl 默认使用独立的 critic（常从 reward model 或 base model 初始化），而不是共享 trunk。部分 RL 设置会共享底层若干层以省显存，但在线 RL 中要谨慎：actor 更新会改变表示，critic 的旧 value 很快失效，两个目标的梯度也会互相干扰。一个折中是只共享底层、value head 独立；或者干脆用 GRPO 去掉 critic。

## 19.12 步骤级 GAE 与过程奖励

17.17 节的 PRM 给出每一步的分数 $s_k$。如果只把聚合分数 scatter 到最后一个 token，它仍然是一条轨迹奖励；如果想把过程信号真正写进优势，就需要**步骤级 GAE**。这一节给出公式与工程边界。

### 从 token 级到步骤级

设第 $k$ 步覆盖 token 区间 $[a_k, b_k]$，步骤奖励为 $r_k$（可以来自 PRM、工具反馈或结果奖励的分配），步骤级价值为 $V_k = V(s_{b_k})$。步骤级 GAE 与 token 级同构：

$$
\delta_k = r_k + \gamma V_{k+1} - V_k,\qquad
A_k = \delta_k + \gamma\lambda A_{k+1}.
$$

得到 $A_k$ 后，有两种广播方式：

- **整步广播**：第 $k$ 步的所有 token 都用 $A_k$。实现简单，但同一步内 token 的重要性被抹平。
- **步内再分配**：在步内用 token 级 TD 误差或注意力权重再分配 $A_k$。更细，但需要额外的 per-token value 或归因信号。

如果步骤奖励是稀疏的（只有部分步骤有 $r_k$），未标注步骤的 $r_k$ 设为 0；GAE 会把后续步骤的奖励倒推回来，这正是它比“只给最后一步”更好的地方。

### 与 token 级 GAE 的关系

两种做法不是互斥的：

| 方案 | 优点 | 代价 |
|---|---|---|
| token 级 GAE + 聚合轨迹奖励 | 兼容现有 PPO/GRPO，改动最小 | 过程信息被压成一个标量 |
| 步骤级 GAE | 信用分配与语义步骤对齐 | 需要步骤边界、步骤级 value/reward、mask |
| 混合 | 步骤信号做 shaping，token 级 GAE 做最终优势 | 两套 $\gamma/\lambda$ 要对齐，调参复杂 |

混合方案里，步骤奖励必须先通过 potential-based shaping 转成 token 奖励，否则最优策略会改变：

$$
F(s,a,s')=\gamma\Phi(s')-\Phi(s).
$$

一种常见做法是令 $\Phi(s_{b_k}) = \sum_{j\le k} \gamma^{k-j} r_j$ 的折扣累积，或直接用 PRM 的 logit 作为势函数；具体选择要在验证集上确认“加了 shaping 之后，真实指标是否仍然提升”。

### 工程边界

1. **步骤边界与 mask 必须一致**：步骤不能跨过 `response_mask=0` 的 observation；跨轮 Agent 的步骤应按 turn 切（26.12、26.16）。
2. **步骤奖励要归一化**：不同任务的 $r_k$ 尺度可能差几个数量级；按任务/长度分桶做 running normalization，且用全局统计量。
3. **长度偏置**：步骤越多，$\sum r_k$ 越大；要么用均值，要么在 GAE 里对 $\gamma$ 做长度校正。只加不减会让策略偏向“多写步骤”。
4. **终止与截断**：最后一步被截断时，$V_{K+1}$ 应使用 bootstrap value，而不是 0（19.11）。
5. **critic 的粒度**：步骤级 GAE 需要步骤级 value。可以复用 token 级 critic 在 $b_k$ 的 value，也可以单独训一个步骤 value head；后者更贵，但 credit assignment 更干净。

MiniVerl 目前只实现“聚合后 scatter 到最后一个 token”，没有步骤级 GAE；`ProcessRewardManager` 保留了 `prm_step_scores`，可以作为实现步骤级 GAE 的输入。对应练习见附录 F 的 Lab 19。

## 本章小结

- GAE/PPO 需要 critic；GRPO/RLOO 不需要。
- 值损失用旧 values 做裁剪，目标是 GAE 回报。
- critic warmup、值归一化、独立 lr 是稳定训练的关键。
- 多轮轨迹的 value 只在 assistant token 上有效，但 GAE 要穿过 observation 递推。
- 截断样本要 bootstrap，否则 value 与优势会系统性偏移。
- MiniVerl 用 TinyValueModel + critic_loss_fn 复现这条链。
- 步骤级 GAE 与 token 级 GAE 同构，但需要步骤边界、步骤奖励归一化与截断 bootstrap；PRM 分数可直接作为步骤奖励。

## 延伸阅读

- `verl/trainer/ppo/core_algos.py::compute_gae_advantage_return`
- `verl/workers/utils/losses.py::value_loss`
- 下一章：参考模型与 KL 控制。
