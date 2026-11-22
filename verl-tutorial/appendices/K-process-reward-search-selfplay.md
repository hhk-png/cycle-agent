# 附录 K 过程奖励、树搜索与自我博弈

> 正文第 17 章讲了 reward 的分类，第 26 章讲了多轮 Agent，第 41 章列了未来方向。这一篇把“稀疏奖励之后怎么办”展开成可落地的设计：PRM、MCTS/beam、自博弈与数据飞轮。

## K.1 稀疏奖励的信用分配问题

结果奖励只有一个标量：

```text
R(τ) = 1 if answer correct else 0
```

一条 8K token 的轨迹里，可能只有最后 10 个 token 与答案有关，中间 7990 个 token 都在“推理”。策略梯度把同一个 $R$ 分配给所有 token，方差随长度增长。三种缓解路线：

1. **更好的基线**：GRPO/RLOO/GAE（正文第 2、3、19 章）；
2. **更密的奖励**：PRM、步骤验证器、工具反馈；
3. **更好的探索**：树搜索、self-play、拒绝采样。

这三条不是互斥的。生产系统通常同时用：GRPO 做基线、PRM 做 shaping、best-of-n 做数据筛选。

## K.2 PRM：定义、训练与聚合

### 形式化

把响应切成 $K$ 步 $s_1,\dots,s_K$，PRM 输出每步分数：

$$
r_k=f_\phi(q,s_1,\dots,s_k).
$$

损失是逐步二元交叉熵：

$$
\mathcal{L}_{\text{PRM}}
=-\frac1K\sum_{k=1}^K
\left[y_k\log\sigma(r_k)+(1-y_k)\log(1-\sigma(r_k))\right].
$$

关键是 $y_k$ 的语义。同一个损失至少对应两种不同目标：

- $y_k^{\text{past}}$：**过去正确性**——到第 $k$ 步为止，前缀是否仍然正确（与参考解一致，或没有出现已知错误）。人工标注通常接近这一种。
- $y_k^{\text{future}}$：**未来可解性**——从第 $k$ 步的中间状态继续采样，最终能否成功。蒙特卡洛 rollout 估计的是这一种。

两者不能混用：前者适合定位“哪一步开始错”，后者适合判断“还有没有救”。MiniVerl 的 `suffix` 标签是 $y_k^{\text{past}}$ 的廉价近似（一旦某步错，后续都标 0），`local` 则只标出错的那一步。

### 标签从哪来

| 来源 | 目标 | 成本 | 偏差 |
|---|---|---|---|
| 人工逐步标注 | 过去正确性 | 高 | 质量高，但规模小，标注者间不一致 |
| 蒙特卡洛 rollout | 未来可解性 | 中 | 依赖采样策略与 $M$，方差大 |
| 强模型评判 | 两者皆可 | 中 | 继承评判模型的偏差与位置偏好 |
| 规则/工具验证 | 过去正确性 | 低 | 只适用于可验证步骤 |

蒙特卡洛做法：从第 $k$ 步的中间状态继续采样 $M$ 条，成功率作为 $y_k$ 的软标签。它天然与当前策略分布对齐，但计算量大；$M$ 太小会把“没采样到成功”误判成“不可解”。生产上通常用分组采样（同一 prompt 的轨迹一起估计）并记录置信区间。

### 步骤切分与 token 映射

PRM 的很多“模型不收敛”其实是边界错了。切分方式必须在训练、推理、评估三处一致：

| 方式 | 做法 | 风险 |
|---|---|---|
| 分隔符 | 换行、`Step k:`、`<|step|>` | 模型不遵守格式时边界缺失 |
| 固定长度 | 每 $T$ 个 response token 一步 | 切点与语义无关，但最稳定 |
| 轮次/工具 | 一次 assistant 输出 + 一次 observation | 需要处理并行工具调用与截断 |
| 模型切分 | 强模型/解析器标注后人工抽检 | 成本高，边界最接近语义 |

切完要做三件事：

1. **用 token 下标而不是字符下标**保存 span；`decode` 再 `encode` 可能改变长度。
2. **把分数映射回 token**：可以广播到整步，也可以只放该步最后一个 token；前者是密集奖励，后者接近轨迹奖励。边界要按有效 response token 计数，不能按原始列下标，否则 observation/padding 会让标签整体错位。
3. **处理截断**：被 `max_response_length` 截断的轨迹没有完整最后一步；丢掉或显式标成负步骤，不能当完整轨迹。

### 聚合与使用

推理时把步骤分数聚合成一个标量，或直接作为密集奖励：

```text
score = r_K                      # 最后一步，接近 ORM
score = r_1                      # 第一步，方向性判断
score = min_k r_k                # 保守，惩罚任一失败
score = max_k r_k                # 乐观，通常只用于诊断
score = mean_k r_k               # 平滑，但会稀释单点失败
score = sum_k log sigmoid(r_k)   # 每步都不能错；log 空间防下溢
```

把 $r_k$ 加到第 $k$ 步的 token 奖励上时，要注意 **potential-based shaping** 的条件：

$$
F(s,a,s')=\gamma\Phi(s')-\Phi(s)
$$

只有这种形式的 shaping 不改变最优策略。直接加 PRM 分数通常会改变最优策略——这可能正是我们想要的（更重视过程），也可能是 reward hacking 的来源。

## K.3 树搜索：best-of-n、beam 与 MCTS

### best-of-n

采样 $n$ 条完整轨迹，用 verifier/PRM 选最高分：

$$
\hat\tau=\arg\max_{\tau_i} R(\tau_i).
$$

优点是实现简单、可并行；缺点是只探索，不利用中间信号。它适合做**数据筛选**：把选中的轨迹拿去 SFT 或 RL，而不是直接改策略。

### beam search

在每个步骤保留 top-$b$ 个前缀，继续展开。它比 best-of-n 更省算力，但需要：

- 可切分的步骤边界；
- 每个前缀的价值估计（PRM 或 rollout）；
- 去重与长度归一化。

beam 容易产生“局部看起来好、整体不连贯”的轨迹，因为每一步独立打分。

### MCTS

MCTS 在树上做四件事：

```text
selection   -> 用 UCT 选子节点
expansion   -> 展开一个新节点
simulation  -> rollout 到终止（或用 value 估计）
backup      -> 把回报回传，更新访问次数与价值
```

UCT：

$$
a^*=\arg\max_a\left(Q(s,a)+c\sqrt{\frac{\ln N(s)}{N(s,a)}}\right).
$$

$Q$  exploitation，第二项 exploration；$c$ 控制探索强度。对 LLM：

- 状态是 token 前缀；
- 动作是下一个 token 或一个步骤；
- rollout 用当前策略生成到终止；
- value 可以用 critic、PRM 或最终 verifier。

MCTS 的成本远高于采样：每个节点都要一次前向/生成。它更适合**推理时增强**或**小规模数据生成**，不适合每个 step 都对全 batch 做搜索。

### 搜索轨迹怎么用于训练

三种方式：

1. **行为克隆**：把搜索找到的高分轨迹做 SFT；
2. **拒绝采样微调（ReST/STaR 风格）**：生成 → 过滤 → 训练 → 再生成；
3. **搜索增强 RL**：把树节点/访问次数作为额外特征，或把搜索价值作为优势估计。

第二种最常用，也最容易工程化：它把“搜索”当成数据生成器，训练仍然用现成的 SFT/RL。

## K.4 自我博弈与自奖励

自博弈的基本循环：

```text
policy π_t 生成候选
  -> 评判/对手/verifier 打分
  -> 用打分数据更新 π_{t+1}
  -> 重复
```

三种形态：

| 形态 | 例子 | 主要风险 |
|---|---|---|
| 自评判 | 模型给自己的答案打分 | 自我偏好、分数膨胀 |
| 对抗 | 生成者 vs 判别者 | 模式崩溃、非平稳 |
| 协作 | planner/executor/verifier | 信用分配、角色固化 |

工程难点：

- **非平稳性**：对手一变，旧数据的最优性失效；需要 staleness 阈值或重要性修正；
- **模式崩溃**：双方陷入固定套路，多样性下降；
- **奖励膨胀**：自评判分数越来越高，但真实能力不涨；
- **计算成本**：每轮要生成多份数据，且评判也要时间。

一个稳妥的起点是“自博弈只用于数据生成，不直接进 RL 梯度”：先生成多样化轨迹，过滤后混入训练集，观察验证指标是否真的提升。

## K.5 数据飞轮

把上述能力串起来：

```text
种子数据
  -> 策略采样 / 搜索 / 自博弈
  -> verifier + PRM + 人工抽查过滤
  -> 去重、难度分层、配比
  -> SFT / RL 训练
  -> 多基准评测 + 回归测试
  -> 新种子数据
```

每一环的工程要求：

- **可追溯**：每条样本记录来源模型、版本、采样参数、分数、过滤原因；
- **可复现**：同一批种子、同一版本、同一评测脚本能复现；
- **去污染**：训练集与评测集做 n-gram/embedding 去重；
- **难度控制**：太简单没有梯度，太难全是负样本；用 pass rate 分层；
- **多样性**：按题目类型、长度、工具使用、语言分布监控；
- **回归**：新数据训练后，旧能力不能掉。

飞轮最怕“指标单一”：只优化一个 benchmark，数据会迅速过拟合到它的分布。至少保留一个 held-out 集合，定期人工评估。

## K.6 在 verl 中落地

verl 现有能力可以拼出大部分链路：

| 需求 | 现有组件 | 说明 |
|---|---|---|
| 多轮/工具 | `verl/experimental/agent_loop/agent_loop.py` | AgentLoopWorker/Manager |
| 模型奖励 | `verl/experimental/reward_loop/reward_model.py` | 判别式原生；生成式需 `custom_reward_function` 解析（X.7） |
| 自定义规则奖励 | reward function + reward manager | 见第 17 章 |
| 优势/损失 | `verl/trainer/ppo/core_algos.py` | GAE/GRPO/DAPO 等 |
| 训练循环 | `verl/trainer/ppo/v1/trainer_base.py` | V1 单控制器 |
| 多轮文档 | `docs/advance/agent_loop.rst` | 官方说明 |
| 奖励文档 | `docs/advance/reward_loop.rst` | 官方说明 |

树搜索不是 verl 的一等公民。现实的实现方式：

1. 在 controller 侧写一个 `SearchRunner`，管理前缀树与并发；
2. 每个节点通过 rollout server 生成；
3. 用 PRM/verifier 打分；
4. 把选中的轨迹转成 `DataProto`，交给现有 trainer；
5. 训练与搜索解耦，搜索可以离线跑。

这样做的代价是数据协议要扩展（节点 id、父子关系、访问次数），但不需要改 PPO 核心。

## K.7 MiniVerl 的 PRM 与扩展练习

MiniVerl 最初只覆盖“采样 → 奖励 → 优势 → 更新”的主链路；现在 **PRM 已经从练习变成实现**，位置是 `mini_verl/prm.py`：

| 组件 | 作用 | 对应正文 |
|---|---|---|
| `StepSample` / `build_step_samples` | 把 gold 轨迹切成步骤，构造 `local`/`suffix` 标签与反例 | 17.17.1 |
| `step_prefixes` / `collate_step_samples` | 只让第 $k$ 步看到前缀，按轨迹分组 | 17.17.1 |
| `step_bce_loss` | `softplus(z) - y*z` 的稳定 BCE + mask | 17.17.2 |
| `PRMTrainer` | AdamW、step/trajectory 指标、早停、检查点 | 17.17.3 |
| `ProcessRewardManager` | 边界校验、六种聚合、scatter、overlong shaping | 17.17.4 |
| `outcome_reward_fn` | PRM shaping 与 verifier 组合，避免 PRM-only 被 hack | 17.17.5 |

可直接运行：

```bash
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_copy.yaml     # 多步
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_bandit.yaml   # 单动作 / PPO
python -X utf8 examples/26_process_reward.py
python -X utf8 -m pytest tests/test_prm.py -q
```

仍然值得作为练习扩展的两件事：

1. **best-of-n**：在 `rollout.py` 外再包一层选择器，用 `bandit_reward` 或训练好的 PRM 选最高分轨迹，观察策略是否过拟合；关键是同时报告选择前后的分布变化与平均能力，而不是只报最好结果。
2. **自博弈**：让两个 `TinyLinearLM` 分别扮演生成者与判别者，交替训练，记录判别准确率与生成多样性；没有判别器 held-out 集时，自博弈很容易退化成模式崩溃。

每个练习都要配测试：步骤边界、mask、优势归一化、选择前后的分布变化。没有测试的“搜索增强”很容易变成“用验证集调参”。

## K.8 风险清单

- PRM 的步骤切分与推理时不一致；
- shaping 改变了最优策略，但没有在验证集上确认；
- 树搜索的算力成本被低估；
- 搜索数据与训练分布不匹配（搜索用强模型，训练用弱模型）；
- 自博弈导致模式崩溃；
- 数据飞轮把评测集污染了；
- 只报告搜索后的最好结果，不报告平均能力。

## K.9 PRM 的评估：不要只看逐步准确率

逐步准确率高，不代表 PRM 适合做奖励。至少评估六个维度：

| 维度 | 指标 | 回答的问题 |
|---|---|---|
| 步骤判断 | step accuracy、Brier | 单个前缀的 0/1 判断准不准、校准好不好 |
| 轨迹判断 | trajectory accuracy（按 serving 的聚合方式） | 最终喂给 PPO 的分数对不对 |
| 首错定位 | first-error index 的绝对误差 / 命中率 | 能不能指出“从哪一步开始错” |
| 聚合敏感性 | 换 `last/min/mean/product` 后排序变化 | 结论是否依赖聚合选择 |
| 对抗探针 | 高分错误串的比例、扰动前后分数差 | 策略能不能轻易 hack 它 |
| 下游效果 | 固定预算下的 pass@k、best-of-n 提升、RL 曲线 | 它是否真的改善任务能力 |

评估集要按 prompt 分组切分，避免同一道题的变体同时出现在训练与评估；标签来源（人工/蒙特卡洛/规则）要分层报告，因为不同来源的偏差不同。最后一定要有一个**独立 verifier** 作为地面真值；如果用 PRM 当 reward manager 跑 `evaluate()`，pass@k 衡量的是“PRM 认为对”，不是任务真实正确率（见 24.13）。

MiniVerl 的 `step_metrics` 提供了前两类指标；`tests/test_prm.py` 用 `local`/`suffix` 两种标签、显式边界与 verifier 组合覆盖了契约层面。对抗探针与下游 best-of-n 仍属于扩展练习（K.7）。

## 延伸阅读

- PRM800K：<https://github.com/openai/prm800k>
- Tree of Thoughts：<https://arxiv.org/abs/2305.10601>
- ReST：<https://arxiv.org/abs/2308.08998>
- STaR：<https://arxiv.org/abs/2203.14465>
- 正文第 17 章：奖励
- 正文第 26 章：AgentLoop
- 正文第 41 章：未来方向
