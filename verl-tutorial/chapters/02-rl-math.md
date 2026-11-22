# 第 2 章 RL 后训练的数学基础：从策略梯度到 GAE

## 2.1 把生成写成马尔可夫决策过程

给定 prompt $x$，模型逐 token 生成响应 $y=(y_1,\dots,y_T)$。把它看成一个有限时域 MDP：

- 状态 $s_t=(x,y_{<t})$；
- 动作 $a_t=y_t\in\mathcal{V}$；
- 策略 $\pi_\theta(a_t\mid s_t)=p_\theta(y_t\mid x,y_{<t})$；
- 转移是确定性的：$s_{t+1}=(x,y_{\le t})$；
- 奖励只在终止时给出：$R(\tau)=r(x,y)$，中间奖励为 0；
- 折扣因子通常取 $\gamma=1$，因为语言轨迹没有“越早越好”的时间价值。

轨迹概率为

$$
\pi_\theta(\tau)=p(x)\prod_{t=1}^{T}\pi_\theta(y_t\mid x,y_{<t})
$$

目标是

$$
J(\theta)=\mathbb{E}_{\tau\sim\pi_\theta}\left[R(\tau)\right].
$$

注意两个工程含义：

1. **只有终止奖励**。这使 GAE 在语言模型上退化为“从后往前累加”，但也意味着 credit assignment 必须靠 critic 或 group baseline。
2. **轨迹长度是随机的**。mask 必须精确地标出哪些 token 属于响应、哪些属于 padding 或工具返回。

## 2.2 策略梯度与 baseline

对 $J(\theta)$ 求导，使用 log-derivative trick：

$$
\nabla_\theta J
=\mathbb{E}_{\tau\sim\pi_\theta}\left[R(\tau)\nabla_\theta\log\pi_\theta(\tau)\right]
=\mathbb{E}\left[\sum_{t=1}^{T}R(\tau)\nabla_\theta\log\pi_\theta(y_t\mid s_t)\right].
$$

这就是 REINFORCE。它的方差很大，因为同一条轨迹的所有 token 共享同一个 $R(\tau)$。引入只依赖状态的 baseline $b(s_t)$ 不改变期望：

$$
\nabla_\theta J
=\mathbb{E}\left[\sum_t \left(R(\tau)-b(s_t)\right)\nabla_\theta\log\pi_\theta(y_t\mid s_t)\right].
$$

定义**优势** $A_t = Q(s_t,a_t)-V(s_t)$，其中

$$
V(s_t)=\mathbb{E}\left[R(\tau)\mid s_t\right],\qquad
Q(s_t,a_t)=\mathbb{E}\left[R(\tau)\mid s_t,a_t\right].
$$

优势回答的是“这个动作比当前状态下的平均动作好多少”。PPO 的目标就是用 critic 估计 $V$，用 GAE 估计 $A$。

## 2.3 从回报到 GAE

设 token 级奖励 $r_t$（通常只有最后一个有效 token 非零），价值 $V_\phi(s_t)$。定义 TD 误差

$$
\delta_t = r_t + \gamma V_\phi(s_{t+1}) - V_\phi(s_t).
$$

$n$ 步优势为

$$
A_t^{(n)}=\sum_{l=0}^{n-1}\gamma^l\delta_{t+l}.
$$

GAE 对所有 $n$ 做指数加权平均：

$$
A_t^{\mathrm{GAE}(\gamma,\lambda)}
=\sum_{l=0}^{\infty}(\gamma\lambda)^l\delta_{t+l}.
$$

它有一个等价的递归形式，也是实现中真正使用的形式：

$$
A_t=\delta_t+\gamma\lambda A_{t+1},\qquad A_{T+1}=0.
$$

回报为

$$
\hat R_t=A_t+V_\phi(s_t).
$$

### 实现中的三个坑

1. **padding 不能 bootstrap。** 对长度 $L$ 的响应，最后一个有效 token 的下一个价值必须是 0，而不是 padding 位置的价值。MiniVerl 的 `compute_gae_advantage_return` 先找到每行最后一个 `response_mask=1` 的位置，再从那里往前递推。
2. **prompt token 不参与。** `response_mask` 在 prompt 位置为 0，优势和回报也要乘 mask。
3. **多轮轨迹的 observation token 不参与。** 工具返回的 token 不是模型动作，mask 必须为 0；但后续 assistant token 的奖励在倒推时要能穿过这些 span。verl 的 GAE/REINFORCE++ 实现对这一点有专门处理：mask=0 的位置不产生损失，`lastgaelam` / `running_return` **原样冻结**——不乘 $\gamma$ 或 $\gamma\lambda$，也不置零。源码形式是 `lastgaelam = lastgaelam_ * mask + lastgaelam * (1 - mask)`（REINFORCE++ 的 `running_return` 同构）。

4. **真实 verl 会对优势做白化。** `compute_gae_advantage_return` 最后执行 `advantages = masked_whiten(advantages, response_mask)`：在有效 token 上做零均值/单位方差，`returns = advantages + values` 保持未白化。上面的公式与数值算例都是白化前的 $A_t$；MiniVerl 不做这一步，因此不能直接拿 advantage 的绝对值与真实 verl 对拍。

## 2.4 PPO 的 clipped objective

设行为策略（采样时的策略）为 $\pi_{\theta_{\text{old}}}$，当前策略为 $\pi_\theta$。定义重要性比率

$$
\rho_t(\theta)=\frac{\pi_\theta(y_t\mid s_t)}{\pi_{\theta_{\text{old}}}(y_t\mid s_t)}
=\exp\left(\log\pi_\theta-\log\pi_{\theta_{\text{old}}}\right).
$$

PPO 的 surrogate 目标是

$$
L^{\mathrm{CLIP}}(\theta)
=\mathbb{E}_t\left[\min\left(\rho_t A_t,\;
\operatorname{clip}(\rho_t,1-\epsilon,1+\epsilon)A_t\right)\right].
$$

训练时最小化负目标：

$$
\mathcal{L}_{\text{pg}}=-\mathbb{E}_t\left[\min(\rho_t A_t,\operatorname{clip}(\rho_t,1-\epsilon,1+\epsilon)A_t)\right].
$$

### clip-higher

DAPO 把对称裁剪改成非对称：

$$
\operatorname{clip}(\rho_t,1-\epsilon_{\text{low}},1+\epsilon_{\text{high}}),
$$

通常 $\epsilon_{\text{high}}>\epsilon_{\text{low}}$。直觉是：**不要过早惩罚概率上升的 token**，否则会抑制探索。verl 的 `ActorConfig` 同时暴露 `clip_ratio_low` 与 `clip_ratio_high`；v0.9.1 的默认值都是 `0.2`（对称裁剪），`0.2/0.28` 是 DAPO recipe 的 clip-higher 设置，不是库默认。

### dual-clip

当 $A_t<0$ 且 $\rho_t$ 很大时，普通 PPO 的损失会趋向 $-\infty$，梯度可能爆炸。dual-clip 加一个下界：

$$
\mathcal{L}_t=
\begin{cases}
\max\left(-\rho_t A_t,-\operatorname{clip}(\rho_t,1-\epsilon,1+\epsilon)A_t\right), & A_t\ge 0,\\
\min\left(-cA_t,\max\left(-\rho_t A_t,-\operatorname{clip}(\rho_t,\dots)A_t\right)\right), & A_t<0.
\end{cases}
$$

其中 $c>1$（verl 默认 `clip_ratio_c=3.0`）。MiniVerl 的 `compute_policy_loss` 与 verl 的 `compute_policy_loss_vanilla` 使用同一结构。

## 2.5 值函数损失

critic 学习 $V_\phi(s_t)$。PPO 同样对价值做裁剪：

$$
\mathcal{L}_V=\frac12\mathbb{E}_t\left[\max\left((V_\phi(s_t)-\hat R_t)^2,\;
(\operatorname{clip}(V_\phi(s_t),V_{\text{old}}-\epsilon_v,V_{\text{old}}+\epsilon_v)-\hat R_t)^2\right)\right].
$$

verl 默认 `cliprange_value=0.5`。注意这里的 `values` 是更新前的旧价值，`returns` 是 GAE 回报；二者不能混用。

## 2.6 KL 正则与两种放置位置

RLHF 希望策略不要离参考模型 $\pi_{\text{ref}}$ 太远。KL 有两种放置方式。

### 放在奖励里（in-reward KL）

$$
r_t' = r_t - \beta\,\mathrm{KL}_t,\qquad
\mathrm{KL}_t=\log\frac{\pi_\theta(y_t\mid s_t)}{\pi_{\text{ref}}(y_t\mid s_t)}.
$$

然后对 $r_t'$ 做 GAE。这种方式把 KL 当作每 token 的惩罚，适合 PPO。

### 放在损失里（KL loss）

$$
\mathcal{L}=\mathcal{L}_{\text{pg}}+\beta\,\mathbb{E}_t\left[\mathrm{KL}(\pi_\theta\Vert\pi_{\text{ref}})\right].
$$

GRPO 常用这种方式，因为它没有 critic，KL 直接约束策略更新。

### KL 估计量

| 文献名 | `kl_penalty` 配置值 | 公式 | 特点 |
|---|---|---|---|
| k1 | `kl` | $\log p-\log q$ | 无偏但方差大 |
| — | `abs` | $\lvert\log p-\log q\rvert$ | 更稳健，不是严格 KL |
| k2 | `mse` | $\frac12(\log p-\log q)^2$ | 梯度性质好 |
| k3 | `low_var_kl` | $e^{\log q-\log p}-(\log q-\log p)-1$ | 低方差；`actor.kl_loss_type` 的默认 |
| full | `full` | 对整个词表求 KL | v0.9.1 未实现，会抛 `NotImplementedError` |

注意两个不同的配置入口：`algorithm.kl_penalty` 控制 **in-reward KL**，默认是 `kl`；`actor.kl_loss_type` 控制 **KL loss**，默认是 `low_var_kl`。v0.9.1 的 `kl_penalty()` 同时接受别名：`kl`/`k1`、`mse`/`k2`、`low_var_kl`/`k3`；写 `k1` 也能工作。`full` 虽然在配置注释里出现，但代码路径直接 `raise NotImplementedError`。部分版本还支持 `+` 后缀做 straight-through（以版本为准）。MiniVerl 实现了 `kl`/`abs`/`mse`/`low_var_kl` 的标量版本，并在 `losses.kl_penalty_tensor` 中提供可微版本；`full` 需要词表级 logits，MiniVerl 不实现。

## 2.7 熵、loss aggregation 与长度偏置

### 熵奖励

$$
\mathcal{L}_{\text{ent}}=-\eta\,\mathbb{E}_t\left[\mathcal{H}(\pi_\theta(\cdot\mid s_t))\right].
$$

`entropy_coeff=0` 时不算熵；大于 0 时鼓励探索。注意 entropy 必须在**允许的动作集合**上计算；如果做了约束解码却对全词表算熵，梯度会跑到被 mask 的 token 上。

### 五种 aggregation

同样的 per-token loss 矩阵，不同的归一化会得到不同的优化目标：

| 模式 | 含义 | 何时用 |
|---|---|---|
| `token-mean` | 全局有效 token 平均 | 最常用，长度不敏感 |
| `token-sum` | 全局 token 求和 | 需要与 batch 大小解耦时 |
| `seq-mean-token-sum` | 先每条序列求和，再按序列平均 | 序列级目标 |
| `seq-mean-token-sum-norm` | 上者再除以固定 horizon | 稳定训练后期 |
| `seq-mean-token-mean` | 每条序列内 token 平均，再序列平均 | 长度归一化 |

在 DP 下，正确的 `token-mean` 必须用**全局**有效 token 数归一。verl 的 `agg_loss` 通过 `dp_size`、`batch_num_tokens`、`global_batch_size` 三个参数保证 FSDP/Megatron 下梯度等价；MiniVerl 在 worker 里传入全局 token 数，再由 worker group 对梯度求平均。

如果实现成“每个 rank 各自 token-mean，再平均梯度”，长序列多的 rank 权重会被低估。这是最常见的静默错误之一。

## 2.8 从公式到代码：一次 PPO 更新的顺序

真实 verl 与 MiniVerl 的顺序一致：

1. 采样轨迹，得到 `input_ids`、`response_mask`、`rollout_log_probs`；
2. 计算 `old_log_probs`（同步模式下由训练引擎重算，异步模式下可用 rollout 的记录）；
3. 计算 `ref_log_prob`（如果启用 KL）；
4. 计算 `values`（如果使用 critic）；
5. 计算 token 级奖励，可选减去 in-reward KL；
6. 用 GAE/GRPO/RLOO 等估计 `advantages` 与 `returns`；
7. 更新 critic（可选）；
8. 用 PPO/GSPO loss 更新 actor；
9. 同步权重到 rollout；
10. 记录指标、验证、保存。

第 6 步的估计器决定算法家族，第 8 步的 loss 决定裁剪方式，第 9 步决定系统是否正确。三者可以自由组合，但组合错了不会报错，只会让曲线变差。

## 2.9 MiniVerl 对照

| 数学对象 | MiniVerl 实现 | 真实 verl |
|---|---|---|
| GAE | `mini_verl/algo/core_algos.py::compute_gae_advantage_return` | `verl/trainer/ppo/core_algos.py::compute_gae_advantage_return` |
| GRPO | 同文件 `compute_grpo_outcome_advantage` | 同文件 `compute_grpo_outcome_advantage` |
| PPO loss | `compute_policy_loss` | `compute_policy_loss_vanilla` |
| 值损失 | `compute_value_loss` | `compute_value_loss` |
| KL | `kl_penalty` / `losses.kl_penalty_tensor` | `kl_penalty` |
| aggregation | `agg_loss` | `agg_loss` |
| 控制器 | `trainer/ppo_trainer.py` | `trainer/ppo/v1/trainer_base.py` |

MiniVerl 用 NumPy 标量数组实现同样的公式；因为模型很小，可以把每一步的中间量打印出来。建议在第 19～22 章对照源码阅读。

## 2.10 一个数值算例：从奖励到 GAE

公式很容易“看懂但算错”。下面用一条 4 token 的轨迹把 GAE 走一遍。设 $\gamma=1.0$、$\lambda=0.95$，终止奖励加在最后一个 token：

| $t$ | $r_t$ | $V(s_t)$ | $\delta_t=r_t+\gamma V(s_{t+1})-V(s_t)$ |
|---|---|---|---|
| 0 | 0 | 0.20 | $0+0.30-0.20=0.10$ |
| 1 | 0 | 0.30 | $0+0.50-0.30=0.20$ |
| 2 | 0 | 0.50 | $0+0.80-0.50=0.30$ |
| 3 | 1 | 0.80 | $1+0-0.80=0.20$ |

从后往前累加：

```text
A_3 = 0.20
A_2 = 0.30 + 0.95 * 0.20 = 0.490
A_1 = 0.20 + 0.95 * 0.490 = 0.6655
A_0 = 0.10 + 0.95 * 0.6655 = 0.732225
returns = A + V = [0.932, 0.966, 0.990, 1.000]
```

三个可以直接拿去做单测的结论：

1. 终止奖励会通过 $\delta$ 和递推“向前传播”，但折扣因子是 $\gamma\lambda$，不是 $\gamma$；
2. 最后一个 token 的 `returns` 必须等于终止奖励（当 $V(s_T)$ 的 bootstrap 为 0），否则奖励放错位置了；
3. 如果 `advantages` 在 padding 位置继续递推，pad token 上的 value 会污染整条序列。实现里要么在序列边界把 `lastgaelam` 清零，要么用 mask 把无效位置排除。

MiniVerl 的 `compute_gae_advantage_return` 与真实 verl 都采用“倒序循环 + 序列边界重置”的写法；`tests/test_core_algos.py` 用固定数值锁定上面的结果。读源码时可以对照这三个检查点。

## 2.11 截断、终止与 bootstrap 的边界

RL 里最容易混淆的一对概念是**终止（termination）**与**截断（truncation）**：

- 终止：环境真正结束（答案给出、游戏结束、工具返回最终状态），$V(s_{T+1})=0$；
- 截断：因为 `max_response_length`、超时或轮次上限被迫停止，环境并没有结束。

如果截断也按终止处理（`done=True`、不 bootstrap），估计量会系统性低估回报：模型本来还能继续获得奖励，却被当成“之后什么都没有”。反之，如果终止也 bootstrap，会把“结束后不该存在的价值”算进来。

正确的处理有三种，按实现成本递增：

1. **加 overlong 惩罚**：把截断当成负终止，明确告诉模型“超长是坏结果”。DAPO 的 overlong penalty 属于这一类；
2. **用最后状态 bootstrap**：截断时用 $V(s_T)$ 代替 0，`done=False`；
3. **partial rollout**：不结束轨迹，挂起后继续生成，从根源上减少截断。

需要澄清：**v0.9.1 的 `compute_gae_advantage_return` 没有 `done`/`truncated` 参数**。它从序列末尾以 `nextvalues=0` 开始，把所有序列都按终止处理；上游把截断交给 reward shaping（DAPO overlong penalty）或 partial rollout，而不是 GAE 的 bootstrap 开关。MiniVerl 额外支持 `truncated` 标志并对最后一步用 $V(s_T)$ bootstrap，这是教学扩展，不是上游行为。

多轮 Agent 还有一层：tool observation 的 mask 为 0，但**奖励必须能穿过 observation 传播**。因此 GAE 的递推是在完整 token 网格上做的：mask 决定哪些位置进入损失、哪些位置的 TD 误差为 0，但**不会把该位置从递推中删掉**；跨越 observation 时，`lastgaelam` 原样冻结（不乘 $\gamma\lambda$），这样后续 assistant token 的奖励倒推回来时不衰减。把 observation 从 GAE 里直接删掉，会让工具调用之后的奖励无法回到 assistant token；把 mask=0 当成“乘一次折扣”，则会系统性低估早期 assistant token 的优势。两者都是多轮 RL 常见的静默错误。

## 本章小结

- 语言生成可以写成 token 级 MDP，奖励通常是终止奖励。
- 策略梯度用优势降低方差；GAE 用 TD 误差的指数加权和估计优势。
- PPO 用重要性比率与裁剪约束更新幅度；DAPO 用 clip-higher 和动态采样改进探索。
- KL 可以放在奖励里或损失里；低方差 KL（k3）是 verl 的默认选择。
- loss aggregation 在 DP 下必须全局归一，否则长度偏置会悄悄改变目标。
- 截断不是终止：但 v0.9.1 的 GAE 没有 `done/truncated` 开关，一律按终止处理；截断要靠 overlong 惩罚/partial rollout，MiniVerl 的 `truncated` bootstrap 是扩展。
- 真实 verl 会对 advantage 做 masked whitening，returns 不白化；MiniVerl 不做，数值不可直接对拍。

## 延伸阅读

- Schulman et al., *Proximal Policy Optimization Algorithms*：<https://arxiv.org/abs/1707.06347>
- Schulman et al., *High-Dimensional Continuous Control Using Generalized Advantage Estimation*：<https://arxiv.org/abs/1506.02438>
- John Schulman, *Approximating KL Divergence*：<http://joschu.net/blog/kl-approx.html>
- 下一章：GRPO 家族与推理模型训练范式。
