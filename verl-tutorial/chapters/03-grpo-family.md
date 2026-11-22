# 第 3 章 GRPO 家族与推理模型训练范式

## 3.1 为什么 GRPO 会成为默认选择

PPO 需要一个与策略同规模（甚至更大）的 critic。对 7B、32B、671B 模型，这意味着：

- 额外一份模型参数、梯度、优化器状态；
- 额外的价值训练不稳定；
- critic 的泛化误差会污染优势估计；
- 训练/推理资源更紧张。

GRPO（Group Relative Policy Optimization，DeepSeek-R1 系列使用）去掉 critic，用**同一 prompt 的多个采样**构造基线：

$$
A_i = \frac{r_i-\operatorname{mean}(r_{1:G})}{\operatorname{std}(r_{1:G})},
$$

其中 $G$ 是 `rollout.n`，$r_i$ 是第 $i$ 条响应的终止奖励。优势被广播到该响应的所有有效 token。

这带来三个直接后果：

1. **不需要 critic**，显存和算力显著下降；
2. **需要分组采样**，`rollout.n > 1`，生成成本上升；
3. **组内奖励必须可分**。如果一组里所有样本奖励相同，优势全为零，这条数据不贡献梯度。

## 3.2 verl v0.9.1 中的优势估计器

`verl/trainer/ppo/core_algos.py` 通过注册表暴露了一组估计器：

| 名称 | 关键思想 | 是否需要 critic | 典型场景 |
|---|---|---|---|
| `gae` | TD 误差的指数加权 | 是 | 经典 PPO、VAPO |
| `grpo` | 组内标准化 | 否 | 推理模型默认 |
| `grpo_vectorized` | 向量化 GRPO | 否 | 大规模吞吐 |
| `grpo_passk` | 用 pass@k 构造基线 | 否 | 代码/数学 |
| `rloo` | leave-one-out 基线 | 否 | 低方差 GRPO 替代 |
| `reinforce_plus_plus` | 折扣回报 + 白化 | 否 | 无 critic 的 REINFORCE |
| `reinforce_plus_plus_baseline` | RF++ + 组均值 | 否 | 稳定版 |
| `remax` | greedy 奖励作为基线 | 否 | 需要额外 greedy 采样 |
| `opo` | 最优策略基线 | 否 | 研究性 |
| `gpg` | 组策略梯度 | 否 | 简化 PG |

注意：**GSPO 不是优势估计器**，而是序列级策略损失（`compute_policy_loss_gspo`）。配置里仍写 `algorithm.adv_estimator=grpo`，再通过 `actor.policy_loss.loss_mode=gspo` 启用序列比率。DAPO 同理，它是 GRPO + 动态过滤 + clip-higher + token-level loss + overlong penalty 的组合，不是 `adv_estimator=dapo`。

### GRPO 的实现细节

verl 的 `compute_grpo_outcome_advantage`：

```python
scores = token_level_rewards.sum(dim=-1)
id2score = defaultdict(list)
for i in range(bsz):
    id2score[index[i]].append(scores[i])
for idx in id2score:
    if len(id2score[idx]) == 1:
        id2mean[idx] = torch.tensor(0.0)
        id2std[idx] = torch.tensor(1.0)
    else:
        id2mean[idx] = torch.mean(torch.stack(id2score[idx]))
        id2std[idx] = torch.std(torch.stack(id2score[idx]))
for i in range(bsz):
    scores[i] = (scores[i] - id2mean[index[i]]) / (id2std[index[i]] + epsilon)
scores = scores.unsqueeze(-1) * response_mask
```

三个容易忽略的点：

1. **单样本组的分支**：`mean=0, std=1`，所以单样本组的优势等于原始分数，而不是 0。这与“组内标准化”的直觉相反，但它是 verl 的实际行为。
2. **torch.std 默认无偏**（除以 $G-1$）。用 NumPy 实现时要用 `ddof=1`，否则数值对不上。
3. **`index` 必须唯一标识一个 prompt 组**。如果用 batch 内行号，repeat 之后每组只有 1 条样本，优势会退化成原始奖励。

MiniVerl 的 `compute_grpo_outcome_advantage` 显式实现了这三点，并在测试中固定了单样本组与无偏标准差的期望。

## 3.3 RLOO：leave-one-out 基线

RLOO 对第 $i$ 条样本使用“其余样本的均值”作为基线：

$$
A_i = r_i - \frac{1}{G-1}\sum_{j\ne i}r_j
= \frac{G}{G-1}r_i - \frac{G}{G-1}\bar r.
$$

它不做标准差归一化，因此对奖励尺度更敏感，但避免了 GRPO 在低方差组上的放大效应。verl 的实现与 MiniVerl 的 `compute_rloo_outcome_advantage` 都使用这个等价形式。

## 3.4 REINFORCE++ 与 ReMax

**REINFORCE++** 用折扣回报做优势，再对整批做白化：

$$
G_t=\sum_{l\ge t}\gamma^{l-t}r_l,\qquad
A_t=\frac{G_t-\mu}{\sigma}.
$$

verl 的实现会跳过 mask=0 的 observation span：`running_return` 在这些位置保持不变，使奖励可以穿过工具返回继续传播。这一点对多轮 Agent 训练很关键。

**ReMax** 用 greedy 解码的奖励作为基线：

$$
A_i = R(\tau_i)-R(\tau_{\text{greedy}}).
$$

它需要额外一次 greedy rollout，但基线更稳定。verl 的 `compute_remax_outcome_advantage` 接收 `reward_baselines`；MiniVerl 的 `_remax_greedy_baselines` 对每个 prompt 单独跑一条 greedy rollout 并打分，而不是拿组内最大采样奖励当基线（后者会让最优样本的优势恒为 0）。

## 3.5 DAPO：四个改动

DAPO 在 GRPO 基础上做了四件事：

1. **clip-higher**：`clip_ratio_low=0.2`、`clip_ratio_high=0.28`，鼓励低概率 token 上升。
2. **dynamic sampling**：过滤掉组内奖励全相同的 prompt 组，避免零梯度；必要时重新生成。
3. **token-level policy gradient**：用 `token-mean` 而不是序列级平均，避免长序列被稀释。
4. **overlong reward shaping**：对超过最大长度的响应施加惩罚，而不是静默截断。

verl v0.9.1 用 `algorithm.filter_groups` 配置前三项中的动态采样：

```yaml
algorithm:
  filter_groups:
    enable: true
    metric: reward
    max_num_gen_batches: 0     # 旧版字段；V1 会忽略
    max_inflight_gen_batches: 1
```

`metric` 可以是 `reward`，也可以是 `reward_extra_info` 中的字段（如 `acc`）。V1 的 `ReplayBuffer` 会在采样时执行过滤，并按缺失组数逐 prompt 精确补采；`max_num_gen_batches` 只对旧版/非 V1 路径有意义。MiniVerl 在 `filter_groups_for_dapo` 中实现了同样的“保留非恒定奖励组”逻辑，并在 `PPOTrainer.train_step` 中调用。

## 3.6 GSPO：把比率从 token 级搬到序列级

GSPO（Group Sequence Policy Optimization）认为 token 级重要性比率在长序列上方差过大。它定义序列级比率：

$$
\rho_i=\exp\left(\frac{1}{|y_i|}\sum_{t\in y_i}\log\frac{\pi_\theta(y_t\mid s_t)}{\pi_{\theta_{\text{old}}}(y_t\mid s_t)}\right).
$$

然后对整条序列使用同一个 $\rho_i$：

$$
\mathcal{L}=-\mathbb{E}_i\left[\min\left(\rho_i A_i,\operatorname{clip}(\rho_i,1-\epsilon,1+\epsilon)A_i\right)\right].
$$

verl 的 `compute_policy_loss_gspo` 与 MiniVerl 的 `compute_policy_loss_gspo` 都使用“mask 内 log-ratio 的平均再 exp”。注意分母是有效 token 数，不是序列总长度；否则 padding 会改变比率。

## 3.7 Dr.GRPO、CISPO 与其他变体

- **Dr.GRPO**：去掉 GRPO 的标准差归一化，只减均值；同时用 token-level loss。这样不会在低方差组上放大噪声。
- **CISPO**：对重要性比率做裁剪，并把裁剪后的权重 detach；梯度仍通过 $\log\pi_\theta$（score function）传播。它避免极端比率导致的梯度爆炸，同时保留策略梯度，不是“梯度只走比率”。
- **VAPO**：在 PPO 上加入价值预训练、长度自适应 GAE、正样本增强等工程技巧，追求推理任务上的极限性能。
- **PRIME**：用隐式奖励模型与在线偏好学习，属于另一条路线（过程奖励），不在本章展开。
- **SPPO**：自博弈偏好优化，用迭代的偏好目标替代 PPO。

这些算法在 verl 中不是“换一个 loss 名称”那么简单：有的需要额外的模型（reward model、teacher），有的改变数据流（greedy baseline、pass@k），有的改变 batch 过滤。读配置时要同时看 `algorithm.adv_estimator`、`actor.policy_loss.loss_mode`、`reward.reward_manager` 三处。

## 3.8 组采样与长度偏置

GRPO 家族都依赖 `rollout.n`。这带来两个工程问题：

### 生成成本

组大小 $G$ 线性增加生成 token 数。若平均响应长度 $L$、prompt 数 $P$，生成 token 数为 $P\cdot G\cdot L$。actor 的训练 token 数也是 $P\cdot G\cdot L$（每个 token 都要算 log-prob 和损失）。因此**生成与 actor 训练的 token 量约按 $G$ 放大**；但 PPO 还要为 critic 做前向/反向与优化器更新，净成本不是简单的 $G$ 倍。$G$ 越大，越需要用过滤/过采样把预算花在有区分度的组上。

### 长度偏置

如果按 token 平均，长响应贡献更多 token；如果按序列平均，短响应权重更大。DAPO 选择 token-level，Dr.GRPO 也选择 token-level；这通常更适合长链推理。具体选择要与 `loss_agg_mode` 和 `filter_groups` 一起看。

## 3.9 何时选哪个

| 场景 | 建议 | 原因 |
|---|---|---|
| 有可靠 critic、追求稳定 | PPO/GAE | 价值基线方差低 |
| 7B～32B 推理模型、只有规则奖励 | GRPO/DAPO | 省 critic，组基线够用 |
| 代码/数学、需要探索 | DAPO | clip-higher + 动态采样 |
| 超长序列、token 比率不稳 | GSPO | 序列级比率 |
| 低方差奖励、组内差异小 | RLOO/Dr.GRPO | 避免 std 放大 |
| 有 greedy 预算 | ReMax | 稳定基线 |
| 多轮工具调用 | GRPO + observation mask | 奖励穿过工具 span |

这张表只回答“算法选型”。数据难度、verifier、长度与熵的联合调参，以及从 GRPO 到 DAPO/GSPO/CISPO 的逐组件消融顺序，见附录 Y。

## 3.10 MiniVerl 对照

MiniVerl 在 `mini_verl/algo/core_algos.py` 中实现了：

- `compute_gae_advantage_return`
- `compute_grpo_outcome_advantage`
- `compute_rloo_outcome_advantage`
- `compute_reinforce_plus_plus_outcome_advantage`
- `compute_remax_outcome_advantage`
- `compute_advantage` 分发器
- `filter_groups_for_dapo`

`PPOTrainer` 通过 `algorithm.adv_estimator` 选择估计器；`actor.policy_loss` 目前实现了 vanilla、GSPO、DAPO 的裁剪组合。测试 `tests/test_core_algos.py` 用 36 个用例固定了这些公式的数值行为（其中约 28 个直接覆盖本章的估计器与损失函数）。

## 3.11 组基线的方差分解

GRPO 用组内均值代替 critic，为什么有效？把一条样本的回报写成“题目难度 + 组内波动”：

$$
R_{i}= \mu_{\text{prompt}} + \epsilon_i,\qquad \mathbb{E}[\epsilon_i]=0.
$$

如果 baseline 取全局均值，优势里仍然带着 $\mu_{\text{prompt}}$ 的差异：难题的组整体为负，简单题整体为正。策略梯度虽然对“与动作无关的常数”期望不变，但方差会显著变大。组内均值把 $\mu_{\text{prompt}}$ 精确消掉：

$$
A_i = R_i - \frac{1}{G}\sum_{j=1}^{G}R_j = \epsilon_i - \bar\epsilon.
$$

对独立同分布的组内回报，

$$
\operatorname{Var}(A_i)=\sigma^2\left(1-\frac{1}{G}\right).
$$

$G=8$ 时是 $0.875\sigma^2$，$G=16$ 时是 $0.9375\sigma^2$。注意这个量随 $G$ 增大反而**上升**并趋近 $\sigma^2$：组均值估计得更准，但每条样本自身相对的噪声仍在。**组基线的主要收益不是把方差降到 0，而是去掉题际差异**。这也是为什么“同一 prompt 采 G 条”比“采 G 个不同 prompt 各 1 条”更适合 GRPO：后者根本组不成基线。

一个数值例子：

| prompt | 组内奖励 | 组均值 | 组内优势 |
|---|---|---|---|
| A（简单） | [1, 1, 0, 0] | 0.50 | [+0.50, +0.50, -0.50, -0.50] |
| B（困难） | [0, 0, 0, 0] | 0.00 | [0, 0, 0, 0] |

B 组没有学习信号——这不是 bug，而是“全对/全错组无梯度”的定义。DAPO 的动态采样正是为了把这种组过滤掉，把预算留给有区分度的样本。

### 标准差归一化的副作用

GRPO 还可以再除以组内标准差：

$$
\hat A_i=\frac{R_i-\bar R}{\operatorname{std}(R)+\epsilon}.
$$

它让不同题目的优势尺度接近，但会在低方差组上放大噪声。例如两组：

- 组 1：[1, 0, 0, 0]，均值 0.25，无偏 std=0.50，归一化优势为 [+1.5, -0.5, -0.5, -0.5]；
- 组 2：[1, 1, 0, 0]，均值 0.50，无偏 std≈0.577，归一化优势约为 [+0.866, +0.866, -0.866, -0.866]。

组 1 的单个正确样本得到 +1.5 的优势，而组 2 的单个正确样本只有 +0.866；低方差组的单个成功被相对放大，尽管它的统计置信度并不更高。Dr.GRPO 去掉 std、只减均值，就是为了避免这种“低方差组被过度加权”。实现上还要处理 `std=0` 的组：要么跳过，要么加 `eps`，否则会出现除零或全零优势。

## 3.12 长度偏置的数值演示

“token-level loss 会造成长度偏置”这句话只对了一半。看两条序列：

| 序列 | 长度 | 每 token loss | token-mean 权重 | seq-mean 权重 |
|---|---|---|---|---|
| S1 | 10 | 1.0 | 10/110 = 9.1% | 50% |
| S2 | 100 | 0.1 | 100/110 = 90.9% | 50% |

- `token-mean` 下，长序列贡献了 90.9% 的梯度；
- `seq-mean-token-mean` 下，两条序列各占 50%。

哪种“对”取决于目标：

- 如果目标是“每个 token 都学好”，token-mean 是自然的；但长序列里大量 padding/低信息 token 会稀释短序列；
- 如果目标是“每条轨迹同等重要”，序列级平均更合理；但它会让短序列的单个 token 获得更大权重，方差更高。

DAPO 明确选择 token-level，并在论文里用长度惩罚控制“越长越好”的退化；Dr.GRPO 也用 token-level，但去掉了 std 归一化。真正危险的是**没有意识到自己在用哪种归约**：训练代码里一个 `mean()` 的位置，可能让长度和奖励悄悄耦合。

工程上要同时检查三处：

1. `loss_agg_mode`（章节 2.7 的表）；
2. `filter_groups` / 动态采样是否按长度过滤；
3. 日志里的 `response_length/mean` 与 `reward/mean` 是否同向增长。

如果长度和奖励一起涨，先别庆祝——可能只是模型学会了写更长，而不是更对。

## 3.13 估计器全景：v0.9.1 注册表里的 14 个名字

`verl/trainer/ppo/core_algos.py` 的 `AdvantageEstimator` 枚举在 v0.9.1 有 14 个值，另有 `register_adv_est` 允许注册自定义名字。把它们分成四类，比逐个背名字更有用（GDPO/OPO/Pass@k/OTB 的推导见附录 C.12～C.15）：

| 类别 | 名字 | 基线/回报 | 额外依赖 |
|---|---|---|---|
| 价值基线 | `gae` | critic 的 TD 误差 + GAE | critic、`gamma/lam` |
| 组基线 | `grpo`、`grpo_vectorized`、`rloo`、`rloo_vectorized`、`reinforce_plus_plus_baseline`、`opo`、`gpg` | 同组其余样本或组均值 | `rollout.n > 1`、正确的 `uid` |
| 极值/排序基线 | `remax`、`grpo_passk` | greedy 奖励 / 组内 top-2 差 | 额外 greedy rollout / `n >= 2` |
| 多目标/ token 级 | `gdpo`、`optimal_token_baseline`、`tir_optimal_token_baseline` | 多维奖励解耦 / 每 token 最优基线 | 奖励字典 / `sum_pi_squared` |
| 折扣回报 | `reinforce_plus_plus` | 折扣回报 + 白化 | `gamma`，mask 感知 |

`*_vectorized` 不是新算法，而是同一估计器的向量化实现（少 Python 循环、吞吐更高）。做算法对比时不要把 `grpo` 与 `grpo_vectorized` 当成两个方法；它们的差异应该只在浮点顺序和性能上。

### 3.13.1 GRPO、Dr.GRPO 与 GDPO

GRPO 与 Dr.GRPO 已在 3.7/25.3 给出。GDPO（Group reward-Decoupled normalization Policy Optimization）解决的是**多目标奖励**：当 reward 同时包含 accuracy、format、length 等维度时，先把各维度相加会得到一个被大尺度维度主导的标量。GDPO 的做法是：

1. 对每个奖励维度 $k$ 独立做组内标准化：$A_k=(r_k-\mu_k)/(\sigma_k+\epsilon)$；
2. 加权求和：$A=\sum_k w_k A_k$；
3. 再对整个 batch 做 masked whitening。

配置要求 reward 函数返回字典，并显式列出维度：

```yaml
algorithm:
  adv_estimator: gdpo
  gdpo_reward_keys: [accuracy_reward, format_reward]
  gdpo_reward_weights: [1.0, 0.2]   # 可选
```

如果 `gdpo_reward_keys` 缺失，源码会直接 assert。GDPO 的最后一步是 batch 级 whitening，而不是组内；这会让不同组之间的相对尺度也进入优势，和 GRPO 的“组内自洽”不同。

### 3.13.2 Pass@k 基线：只奖励“比第二名好”

`grpo_passk` 不把组均值当基线，而是取组内最高分与次高分之差：

$$
A_{i^*}=r_{\max}-r_{\text{second}},\qquad A_i=0\ (i\ne i^*),
$$

其中 $i^*$ 是最高分样本。若 `norm_adv_by_std_in_grpo=true`，再除以组内标准差。它对应 pass@k 的目标：只要有一条成功，组就成功；因此只强化“最好的那条”，而不是把整组往均值拉。

两个直接后果：

- 每组至少需要 2 条样本，否则源码抛 `ValueError`；
- 大多数样本的优势为 0，有效梯度样本数远小于 batch size。`n=8` 时每条 prompt 只有 1 条在学，等效 batch 缩小 8 倍；要相应调大 `train_batch_size` 或学习率。

### 3.13.3 OPO：长度加权的最优基线

OPO 用组内的**长度加权平均奖励**作为基线：

$$
b=\frac{\sum_i L_i r_i}{\sum_i L_i},\qquad A_i=r_i-b,
$$

其中 $L_i$ 是第 $i$ 条响应的有效长度。它不做 std 归一化。直觉是：长响应承载更多决策，应该在基线里占更大权重；短响应不该因为“组内只有它短”而被过度惩罚。

OPO 的论文强调 **exact on-policy**：生成必须来自当前策略。对应配置约束是 `ppo_mini_batch_size == train_batch_size`、`use_kl_loss=false`、`entropy_coeff=0`。如果沿用 PPO 的多 epoch mini-batch，OPO 的“精确 on-policy”前提就被破坏，基线也不再最优。

### 3.13.4 GPG：按非零奖励数缩放

GPG 的优势是组内标准化后再乘一个批级系数：

$$
\alpha=\frac{B}{\#\{i:r_i\ne 0\}},\qquad A_i=\alpha\,(r_i-\mu_g).
$$

它把“全对/全错”的组压缩后的信号重新放大，避免稀疏奖励 batch 的梯度消失。GPG 通常配 `loss_mode=gpg`（纯 `-log pi * A`，没有 clip/ratio），并去掉 critic 与 KL。只配优势、不配损失，得到的仍是 PPO 目标，不是 GPG。

### 3.13.5 OTB：token 级最优基线

OTB（Optimal Token Baseline）把基线从“每条轨迹一个标量”细化到“每个 token 一个基线”。对每个 prompt 组：

$$
B_t^*=\frac{\sum_i G_t^{(i)}W_t^{(i)}}{\sum_i W_t^{(i)}+\epsilon},\qquad
W_t=\sum_{j=1}^{t}\left(1-2\pi_j+\sum_v \pi_{j,v}^2\right),
$$

其中 $G_t$ 是 reward-to-go，$W_t$ 是累积“实现能量”（realized energy），用 logit 梯度代理估计。最终优势是 $(G_t-B_t^*)\cdot m_t$。

它的工程依赖比名字看起来重：

```yaml
actor_rollout_ref:
  actor:
    calculate_sum_pi_squared: true      # 必须
  model:
    use_fused_kernels: false            # fused kernel 不吐 logits
algorithm:
  adv_estimator: optimal_token_baseline # 多轮用 tir_optimal_token_baseline
```

`sum_pi_squared` 是每个 token 的 $\sum_v\pi_v^2$，由 actor 的 `compute_log_prob` 额外返回；Ulysses 序列并行下要先 gather/unpad 再算。OTB 的收益在长序列、稀疏奖励、token 级信用分配最明显；短 bandit 上看不出差别。

### 3.13.6 怎么选

| 目标 | 首选 | 原因 |
|---|---|---|
| 通用推理任务、无 critic | `grpo` | 成熟、实现简单、组内自洽 |
| 多奖励维度、尺度差异大 | `gdpo` | 分维度归一，避免大尺度维度淹没其它信号 |
| 代码/数学、只关心是否至少一条成功 | `grpo_passk` | 目标与 pass@k 对齐 |
| 长序列、token 级信用分配 | `optimal_token_baseline` | 每 token 基线，降低方差 |
| 严格 on-policy、追求低策略漂移 | `opo` | 长度加权最优基线 + 无 std 放大 |
| 稀疏奖励、需要放大非零信号 | `gpg` | 批级 alpha 缩放 |
| 需要 critic 的经典 PPO | `gae` | 有价值函数时方差最低 |

最后一条工程建议：**一次只换一个维度**。`adv_estimator`、`loss_mode`、`reward_manager`、`filter_groups` 是四个独立开关；同时改两个，曲线变好/变坏都无法归因。

## 本章小结

- GRPO 用组内基线替代 critic，适合只有终止奖励的推理任务。
- 单样本组、无偏标准差、分组 key 是 GRPO 实现中最容易错的三处。
- RLOO、REINFORCE++、ReMax、GSPO、DAPO 是同一族的不同方差/成本折中。
- DAPO 的四个改动分别作用于裁剪、采样、损失归约和长度惩罚。
- 组基线主要消除题际方差；std 归一化会在低方差组上放大噪声。
- 长度偏置取决于归约方式与过滤策略的组合，必须用日志交叉验证。
- v0.9.1 的 14 个优势估计器分为价值基线、组基线、极值/排序基线与 token 级基线；`*_vectorized` 只是实现变体。
- GDPO/OPO/GPG/OTB 各自有数据或张量依赖（奖励字典、精确 on-policy、`sum_pi_squared`），只改 `adv_estimator` 不足以复现。
- 算法选择要同时看优势估计器、策略损失和 reward manager，不能只看一个字段。

## 延伸阅读

- DeepSeekMath / GRPO：<https://arxiv.org/abs/2402.03300>
- DAPO：<https://arxiv.org/abs/2503.14476>
- GSPO：<https://arxiv.org/abs/2507.18071>
- RLOO：<https://arxiv.org/abs/2402.14740>
- OPO：<https://arxiv.org/pdf/2505.23585>
- GPG：<https://arxiv.org/abs/2504.02546>
- OTB：<https://www.arxiv.org/abs/2602.07078>
- 附录 Y：推理模型 RL 实战手册
- 下一章：HybridFlow 的编程模型。
