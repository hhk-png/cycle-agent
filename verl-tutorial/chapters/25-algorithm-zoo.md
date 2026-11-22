# 第 25 章 GRPO/DAPO/GSPO/Dr.GRPO/CISPO 的实现差异

## 25.1 三层可组合的算法空间

verl 把算法拆成三层：

1. **优势估计器**：`algorithm.adv_estimator`；
2. **策略损失**：`actor.policy_loss.loss_mode`；
3. **奖励/数据流**：`reward.reward_manager`、`filter_groups`、`rollout.n`。

因此“DAPO”不是一个单独的类，而是：

```yaml
algorithm:
  adv_estimator: grpo          # DAPO 仍使用 GRPO 优势估计器
  filter_groups:
    enable: true
actor_rollout_ref:
  actor:
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
    loss_agg_mode: token-mean
  rollout:
    n: 8
reward:
  reward_manager:
    name: dapo                 # v0.9.1 是对象，不是裸字符串
  reward_kwargs:
    max_resp_len: 8192
    overlong_buffer_cfg:
      enable: true
      len: 512
      penalty_factor: 1.0
```

理解这一点，才能读懂 recipe 里的配置。

## 25.2 策略损失注册表

`verl/trainer/ppo/core_algos.py` 用 `@register_policy_loss(name)` 注册：

| 名称 | 含义 |
|---|---|
| `vanilla` | 标准 PPO clip |
| `gspo` | 序列级比率 |
| `sapo` | smooth advantage policy optimization |
| `gpg` | 组策略梯度 |
| `clip_cov` | clipping covariance |
| `kl_cov` | KL covariance |
| `geo_mean` | 几何平均 |
| `dro` | direct reward optimization（二次 log-ratio 惩罚） |
| `cispo` | clipped IS policy optimization |
| `dppo_tv` / `dppo_kl` | decoupled PPO 变体（TV/KL 信任域） |
| `bypass_mode` | 直接使用 rollout logprob，再分派到 PPO-clip 或 REINFORCE |

注意：`compute_policy_loss_reinforce` 是源码里的一个函数，但 v0.9.1 **没有**注册名为 `reinforce` 的 `loss_mode`；REINFORCE 路径由 `bypass_mode` 的 `loss_type=reinforce` 进入。看到博客里写 `loss_mode=reinforce` 时，先回源码确认版本。

MiniVerl 实现了 `vanilla`、`gspo`、`cispo` 与 `dapo`（vanilla + clip-higher 的别名），并在 `actor_loss_fn` 中按 `policy_loss.loss_mode` 选择。CISPO 的实现与验证见 25.13；完整注册表与每个模式的语义见 25.14。

## 25.3 GRPO vs Dr.GRPO

GRPO：

$$
A_i=\frac{r_i-\mu}{\sigma+\epsilon}.
$$

Dr.GRPO：

$$
A_i=r_i-\mu.
$$

区别：

- GRPO 放大低方差组的差异；
- Dr.GRPO 不做 std 归一，避免在组内差异很小时把噪声放大；
- Dr.GRPO 通常配 token-level loss，减少长度偏置。

配置：

```yaml
algorithm:
  adv_estimator: grpo
  norm_adv_by_std_in_grpo: false
actor_rollout_ref:
  actor:
    loss_agg_mode: token-mean
```

verl 的 `compute_grpo_outcome_advantage` 用 `norm_adv_by_std_in_grpo` 控制。

## 25.4 DAPO

四个改动：

1. clip-higher：`clip_ratio_low=0.2`、`clip_ratio_high=0.28`；
2. dynamic sampling：`filter_groups.enable=true`；
3. token-level loss：`loss_agg_mode=token-mean`；
4. overlong penalty：`reward.reward_manager.name=dapo` + `reward.reward_kwargs.overlong_buffer_cfg.*`（第 22.8 节）。

DAPO 的目标是让长链推理模型在有限步数内学到更长的正确推理。它不是一个全新的数学目标，而是 GRPO + 四个工程改进。

## 25.5 GSPO

GSPO 用序列级比率：

$$
\rho_i=\exp\left(\frac{1}{|y_i|}\sum_t\log\frac{\pi_\theta}{\pi_{\text{old}}}\right).
$$

优点：

- 长序列上方差小；
- 同一序列的 token 共享比率；
- 对 token 级噪声不敏感。

缺点：

- 序列内不同 token 的重要性差异被抹平；
- 需要额外的 mask/长度处理。

verl 的 `compute_policy_loss_gspo` 用“有效 token 平均”得到序列比率，但把它作为 **stop-gradient 常数**：数值上是序列比率，梯度仍只通过每个 token 的 `log pi`。MiniVerl 的实现复现了这一点（`log_prob - log_prob.detach()` 保留 score-function 梯度），而不是直接对 `exp(mean(log ratio))` 求导——后者会得到另一个目标。

## 25.6 CISPO

CISPO 的核心是“裁剪重要性比率，把裁剪后的比率 detach 成纯权重，梯度仍通过策略的 score function”：

$$
\mathcal{L}
=-\mathbb{E}\left[\operatorname{sg}\left(\operatorname{clip}(\rho,1-\epsilon_{\text{low}},1+\epsilon_{\text{high}})\right)\cdot A\cdot\log\pi_\theta\right].
$$

它试图避免 PPO 在比率极端时的梯度爆炸，同时保留策略梯度；与 PPO 不同，比率越界不会把损失“钉死”在一个常数上。具体实现以 `compute_policy_loss_cispo` 为准，推导与测试见 25.13。

## 25.7 VAPO / PRIME / SPPO

- **VAPO**：value-based augmented PPO，包含价值预训练、长度自适应 GAE、正样本增强等；适合推理任务极限性能。
- **PRIME**：隐式过程奖励，在线训练 reward model；需要额外的模型与数据流。
- **SPPO**：自博弈偏好优化，用迭代偏好目标替代 PPO；更接近 DPO 家族。

这些算法在 verl 中通常以 recipe 形式提供（`verl-recipe`），而不是主库的一个开关。使用前先看 recipe 的 `REQUIRED_VERL.txt`，它记录了兼容的 verl commit/tag。

## 25.8 蒸馏

在线蒸馏（on-policy distillation）用 teacher 的 logits 作为监督：

$$
\mathcal{L}_{\text{distill}}=\mathrm{KL}\left(\pi_{\text{teacher}}\Vert\pi_{\text{student}}\right).
$$

verl 的 `distillation` 配置支持：

- teacher 模型列表；
- distillation loss 类型；
- teacher key（按 data_source 选择 teacher）；
- teacher server 资源。

它常与 PPO 损失组合：

$$
\mathcal{L}=\mathcal{L}_{\text{PPO}}+\lambda\mathcal{L}_{\text{distill}}.
$$

## 25.9 DPO / SFT

- **SFT**：交叉熵，不需要 rollout/reward；
- **DPO**：偏好对，直接用 chosen/rejected 的 log-prob 差；
- **ORPO/SimPO**：DPO 的变体，改变参考模型或损失。

verl 主库提供 SFT 与蒸馏；DPO/偏好变体主要在 recipe 或扩展中实现。它们与 `verl/trainer/sft_trainer.py` 共享同一套 Engine/Worker 抽象。MiniVerl 的 `SFTTrainer`/`DPOTrainer` 演示了这条路径。

## 25.10 算法选择矩阵

| 目标 | 推荐 | 关键配置 |
|---|---|---|
| 稳定 PPO 基线 | PPO/GAE | `adv_estimator=gae` |
| 推理模型、无 critic | GRPO | `n>=8`, `use_kl_loss` |
| 长链推理、探索 | DAPO | clip-higher + filter + token loss |
| 超长序列 | GSPO | 序列级比率 |
| 低方差奖励 | Dr.GRPO/RLOO | 不做 std 归一 |
| 有 teacher | 蒸馏 + PPO | `distillation.enabled` |
| 偏好数据 | DPO | chosen/rejected |
| 冷启动 | SFT | 交叉熵 |

## 25.11 MiniVerl 对照

- `mini_verl/algo/core_algos.py`：优势估计、KL、policy loss、value loss；
- `mini_verl/losses.py::actor_loss_fn`：组合 KL/entropy/policy loss；
- `mini_verl/trainer/ppo_trainer.py::_compute_advantage`：按 `adv_estimator` 分发；
- `mini_verl/trainer/sft_trainer.py` / `dpo_trainer.py`：SFT/DPO。

MiniVerl 实现了 CISPO 的完整单机训练流（配置、loss、梯度、端到端测试与示例见 25.13）；VAPO/PRIME/SPPO/DPPO/DRO 等仍只保留配置字段与文档说明，未知的 `loss_mode` 会在 `config.validate()` 提前报错，而不是静默回退到 vanilla。

## 25.12 Recipe 工程：从论文到可复现配置

verl 主库提供“算法组件”，`verl-recipe` 提供“论文级配方”。读 recipe 不是复制一个 shell 脚本，而是回答四个问题：

1. 它相对基线改了哪些配置？
2. 它是否附带代码 patch（新 advantage/loss/reward）？
3. 它假设了什么数据/模型/硬件？
4. 它用什么指标证明有效？

### 常见 recipe 与主库组件的关系

| Recipe | 主库组件 | 你真正要改的 |
|---|---|---|
| DAPO | GRPO + clip-higher + filter_groups + token-mean + overlong | 配置为主 |
| GSPO | `policy_loss.loss_mode=gspo` | 配置为主 |
| CISPO | `policy_loss.loss_mode=cispo` | 配置 + 可能的 mask 细节 |
| DPPO | `dppo_tv`/`dppo_kl` loss | 配置 + 参考实现 |
| GDPO | `algorithm.adv_estimator=gdpo` + `gdpo_reward_keys` | 配置为主（reward 需返回分维度字典） |
| GMPO | `policy_loss.loss_mode=geo_mean` | 配置为主 |
| GPG | `adv_estimator=gpg` + `loss_mode=gpg` | 配置为主（去掉 critic/KL） |
| PRIME/SPPO/VAPO | 额外模型/数据流/价值预训练 | recipe 代码 + 资源 |

示例入口：`examples/cispo_trainer/README.md`、`examples/dppo_trainer/README.md`、`examples/gdpo_trainer/README.md`、`examples/gmpo_trainer/README.md`、`examples/gpg_trainer/README.md`、`examples/grpo_trainer/README.md`、`examples/mtp_trainer/README.md`。

### 读 recipe 的顺序

```text
README（主张/结果/硬件）
  -> run_*.sh（模型、数据、并行、超参）
  -> 配置 diff（相对 ppo_trainer.yaml）
  -> 代码 patch（如果有）
  -> REQUIRED_VERL.txt（兼容的 verl 版本/commit）
```

`REQUIRED_VERL.txt` 很关键：recipe 往往依赖某个 commit 的 API，直接用最新 main 跑会“配置能解析、行为已变化”。复现时先 checkout 到要求的版本，再跑 recipe。

### 复现的最小实验

1. **基线**：同模型、同数据、同硬件，跑主库 PPO/GRPO；
2. **recipe**：只换 recipe 的配置/patch，其他不变；
3. **消融**：逐个关掉 recipe 的改动（clip-higher、filter、token-loss、overlong），看哪个贡献最大；
4. **多 seed**：至少 2～3 个 seed，报告均值±标准差；
5. **样本效率**：同时报告“达到同样验证精度用了多少生成 token”，而不只是墙钟时间。

如果 recipe 没有给出基线/消融，只给了一条曲线，把它当“工程参考”而不是“科学结论”。

### 常见陷阱

- **配置键已改名**：recipe 用的是旧版本字段，Hydra 会忽略未知键或报错；先对照 `_generated_ppo_trainer.yaml`。
- **数据预处理不同**：同一 benchmark 的 prompt 模板/答案抽取不同，结果不可比。
- **硬件假设**：recipe 的 TP/PP/EP、batch、序列长度是按它的卡数调的；换硬件要重算显存与吞吐。
- **隐藏的代码 patch**：有些 recipe 需要替换 `core_algos.py` 或 worker；只复制 YAML 会得到“看起来一样但算错”的训练。
- **过时的依赖**：vLLM/SGLang/Megatron 的版本敏感度高于主库；用 recipe 自带的环境说明。

### 把 recipe 变成团队资产

- fork 一份 recipe，固定 commit 与数据版本；
- 把配置 diff 写成文档，而不是散落在 shell 历史里；
- 加一个 10 步 smoke test，进 CI；
- 记录失败案例（哪些改动无效/有害），比只记录成功更有价值。

## 25.13 损失与目标的补遗：GPG、SAPO、DRO、DPPO、OPD、OTB、SPIN

主库的损失注册表比教程正文列出的更长。这一节做三件事：给出一张“名字—入口—改动—适用”的索引；把 CISPO 从公式落到可测试的代码；给出新增一个 loss 时的检查清单。

### 名字不等于算法

同一个算法家族常常同时出现在三个位置：

- `algorithm.adv_estimator`：只决定优势怎么估；
- `actor.policy_loss.loss_mode`：决定策略损失怎么裁剪/加权；
- `verl-recipe` 的 shell/README：决定数据、reward、采样和超参。

例如 “GPG” 在优势估计器表里是 `gpg`，在策略损失表里也可能有同名 loss；“DAPO” 则根本不是单个枚举值，而是 GRPO + 四个改动的组合。**看到一个名字，先确认它在哪一层，再谈它改了什么。** 只改一个字段就宣称“复现了某算法”，是 recipe 复现里最常见的错误。

### 一张索引表

| 名称 | 主要入口 | 核心改动 | 想解决的问题 | MiniVerl |
|---|---|---|---|---|
| GPG | `adv_estimator=gpg` / `loss_mode=gpg` | 组归一化优势 + 简化策略梯度，不做 PPO 式 min/clip | 去掉 clip 的额外偏差，做低开销基线 | 未实现 |
| SAPO | `loss_mode=sapo` | 对优势/比率做平滑加权，替代硬 clip 的突变 | 大比率样本导致的目标跳变 | 未实现 |
| DRO | `loss_mode=dro` | Direct Reward Optimization：直接对 `log pi * A` 做奖励优化，并加二次 `log-ratio` 惩罚（`dro_beta`） | 用软信任域替代硬 clip，降低策略突变 | 未实现 |
| DPPO | `loss_mode=dppo_tv` / `dppo_kl` | 用 TV/KL 信任域替代（或补充）比率裁剪 | 解耦“更新幅度”和“比率形状” | 未实现 |
| OPD | `distillation` / teacher loop | 在 student 自己采样的轨迹上，用 teacher 分布做 on-policy 监督 | 离线蒸馏的分布错配 | 部分（loss 有，完整 loop 无） |
| OTB | 优势/损失层 | token 级最优基线，降低稀疏奖励的方差 | 长序列 credit assignment | 未实现 |
| SPIN | DPO 家族 recipe | 用“真实答案 vs 当前模型生成”构造偏好对，迭代自博弈 | 没有奖励模型时利用标注数据 | 未实现（DPO loss 可表达） |
| SPPO | 偏好优化 recipe | 迭代自博弈偏好目标，替代 PPO | 稳定偏好对齐 | 未实现 |

表中“未实现”只表示 MiniVerl 不提供可运行的训练流；真实 verl 的可用性取决于版本与 recipe。**不要根据表名直接写配置**：先在源码里搜 `register_policy_loss` / `register_adv_est`，确认枚举值与参数，再对照 `_generated_ppo_trainer.yaml`。未知字段在 Hydra 里可能被忽略，最后得到的是“配置能跑、算法没生效”。

### CISPO：把公式落到代码

CISPO 的目标在 25.6 已给出：

$$
\mathcal{L}_{\text{CISPO}}
=-\mathbb{E}_t\left[\operatorname{sg}\left(\operatorname{clip}(\rho_t,1-\epsilon_{\text{low}},1+\epsilon_{\text{high}})\right)\cdot A_t\cdot \log\pi_\theta(y_t\mid s_t)\right].
$$

三个细节决定实现是否正确：

1. **`sg` 必须真的停梯度**。如果直接写 `clip(ratio) * A * log_prob`，梯度会同时穿过 ratio 和 log-prob，得到的是另一个目标。MiniVerl 用 `clipped_ratio.detach()`，再用 `* log_prob` 保留 score-function 梯度。
2. **没有 `min` 分支**。PPO 在比率越界时把损失钉在裁剪值上；CISPO 的权重被裁剪，但梯度仍然按 `log pi` 流动。这正是它在极端比率下不“零梯度”的原因。
3. **mask 与全局归一化仍由 `agg_loss` 负责**。CISPO 不改变 token/sequence aggregation 的语义；DP 下仍要用全局有效 token 数。

MiniVerl 的实现：

```python
# mini_verl/algo/core_algos.py::compute_policy_loss_cispo
negative_approx_kl = (log_prob - old_log_prob).clip(-20.0, 20.0)
ratio = negative_approx_kl.exp()
clipped_ratio = clip(ratio, 1.0 - low, 1.0 + high)
pg_losses = -clipped_ratio.detach() * advantages * log_prob
pg_loss = agg_loss(pg_losses, mask, loss_agg_mode, ...)
```

配置与入口：

```yaml
actor_rollout_ref:
  actor:
    policy_loss:
      loss_mode: cispo
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
```

```bash
python -X utf8 -m mini_verl.trainer.main_ppo \
  actor_rollout_ref.actor.policy_loss.loss_mode=cispo \
  trainer.total_training_steps=40
```

测试固定了三件事（`tests/test_core_algos.py`）：

- 比率 $e^{10}$、`clip_ratio=0.2` 时，权重被裁到 1.2，loss 为 $-1.2\log\pi$，而不是 PPO 的 $-1.2$；
- 对 `log_prob` 的梯度等于 $-1.2A$，证明 `detach` 没有把梯度一起掐掉；
- mask 为 0 的位置不贡献 loss，分母使用传入的全局 token 数。

端到端示例 `examples/20_cispo_training.py` 在同一个 bandit 任务上分别跑 vanilla 与 CISPO，并先在固定 batch 上打印两种 loss 的差异——bandit 太容易，两个算法都会到 100%，固定 batch 才是观察目标函数差异的地方。

### 新增一个 loss 的检查清单

1. **定义比率**：token 级、序列级还是层级？分母用 `old_log_prob` 还是行为策略的 `rollout_log_probs`？
2. **谁停梯度**：裁剪后的权重、baseline、teacher logits，哪些是常数？
3. **mask 语义**：padding、prompt、tool observation 分别是否参与；GAE 的递推是否要穿过 mask=0 的位置。
4. **aggregation**：`token-mean`/`seq-mean-*` 是否与算法假设一致；DP 下是否全局归一。
5. **数值边界**：`clip(-20, 20)` 的 log-ratio、`clip_ratio_c`、除零与全过滤 batch。
6. **指标**：至少记录 ratio 均值、clipfrac、KL、entropy；否则曲线异常时无法定位。
7. **测试**：固定数值的公式对照 + 有限差分/梯度方向 + mask 用例 + 一个端到端学习用例。
8. **配置校验**：未知 `loss_mode` 必须提前报错，不能静默回退 vanilla。

满足这八条，才算“实现了一个算法”，而不只是“加了一个 if 分支”。

## 25.14 v0.9.1 策略损失注册表：12 个模式的语义与配置

`verl/trainer/ppo/core_algos.py` 里 `@register_policy_loss(name)` 是唯一事实源。v0.9.1 注册了 12 个名字；名字相同不代表目标相同，很多“算法”其实是**优势估计 + 损失形状 + 数据流**的组合。下表把每个 `loss_mode` 的数学形式、配置字段和常见误用放在一起。

| `loss_mode` | 源码函数（v0.9.1） | 目标/形状 | 关键字段 | 常见误用 |
|---|---|---|---|---|
| `vanilla` | `compute_policy_loss_vanilla` | $\min(\rho A,\operatorname{clip}(\rho,1-\epsilon_l,1+\epsilon_h)A)$；$A<0,\rho>C$ 时再截断 | `clip_ratio_low/high`、`clip_ratio_c` | 以为 `clip_ratio` 同时控制上下界；实际上下界可分开 |
| `gspo` | `compute_policy_loss_gspo` | 序列级比率 $\rho_i=\exp(\frac{1}{\lvert y_i\rvert}\sum_t\log\frac{\pi_\theta}{\pi_{old}})$，在序列级裁剪 | `clip_ratio`、`loss_agg_mode` | 仍按 token 级 ratio 理解；序列内 token 共享同一比率 |
| `sapo` | `compute_policy_loss_sapo` | 用平滑门控（`tau_pos`/`tau_neg`）替代硬 clip，正负优势走不同温度 | `policy_loss.tau_pos`、`tau_neg` | 把它当成“换个 clip 系数”；门控函数改变了梯度形状 |
| `gpg` | `compute_policy_loss_gpg` | $-\log\pi_\theta(a_t\mid s_t)A_t$，没有 ratio、没有 clip | `adv_estimator=gpg` | 只改 `loss_mode=gpg` 而 adv 仍用 GRPO；GPG 的 alpha 缩放来自其优势函数 |
| `clip_cov` | `compute_policy_loss_clip_cov` | 标准 PPO + 对“协方差落在 $[lb,ub]$ 且未被正常 clip”的 token 随机置零 | `clip_cov_ratio/lb/ub` | 以为它会提高稳定性就无脑开；置零比例是显式超参 |
| `kl_cov` | `compute_policy_loss_kl_cov` | 标准 PPO + 对高协方差 token 加 `ppo_kl_coef * abs(KL)` | `kl_cov_ratio`、`ppo_kl_coef` | 与 `use_kl_loss` 的全局 KL 混淆；这是 token 级选择 |
| `geo_mean` | `compute_policy_loss_geo_mean` | 对裁剪后的 log-ratio 做序列级几何平均（GMPO），损失按序列平均 | `clip_ratio_low/high` | 以为它走 `agg_loss`；实现是 `torch.mean` 序列损失 |
| `dro` | `compute_policy_loss_dro` | $-\left(\log\pi_\theta A-\frac{\beta}{2}(\log\pi_\theta-\log\pi_{old})^2\right)$ | `policy_loss.dro_beta > 0` | 把 DRO 当成“distributionally robust”；这里是 Direct Reward Optimization |
| `cispo` | `compute_policy_loss_cispo` | $-\operatorname{sg}(\operatorname{clip}(\rho))\,A\log\pi_\theta$，裁剪权重停梯度 | `clip_ratio_low/high` | 忘了 `detach`；否则梯度同时穿过 ratio，目标变了 |
| `dppo_tv` | `compute_policy_loss_dppo_tv` | 用 token 概率的 TV 距离做信任域；TIS 截断 ratio 后乘 `log_prob` | `clip_ratio_low/high` 被解释为散度阈值、`clip_ratio_c` | 继续按 PPO 的 ratio 裁剪理解 `clip_ratio` |
| `dppo_kl` | `compute_policy_loss_dppo_kl` | 同上，但信任域用 KL 距离 | 同上 | 与 `kl_cov` 混淆：DPPO 是信任域，kl_cov 是 token 选择 |
| `bypass_mode` | `compute_policy_loss_bypass_mode` | 令 `old_log_prob = rollout_log_prob`；再按 `loss_type` 分派到 PPO-clip 或 REINFORCE | `policy_loss.rollout_correction.loss_type` | 以为它自己是一个新目标；它只是 off-policy 数据的入口 |

三条读表规则：

1. **先看 adv，再看 loss**。`gpg` 在优势估计器和损失注册表里都出现，但两处做的是不同的事；只配一半通常得到的是“名字对了、目标没变”。
2. **`clip_ratio` 在不同 loss 里含义不同**。`vanilla/gspo/cispo` 是比率边界；`dppo_tv/dppo_kl` 是散度阈值。照抄配置会得到完全不同的信任域。
3. **不是所有 loss 都走 `agg_loss`**。`geo_mean` 用序列级 `torch.mean`，`dapo` 是 `vanilla` + token-mean + clip-higher 的组合；全局归一的口径要逐项核对。

配置示例（CISPO 与 DRO）：

```yaml
actor_rollout_ref:
  actor:
    policy_loss:
      loss_mode: cispo          # 或 dro
      # dro 时必须设置：
      # dro_beta: 0.1
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
```

一个最小的源码核对脚本（不启动 Ray，只读注册表）：

```bash
python - <<'PY'
from verl.trainer.ppo import core_algos
print(sorted(core_algos.POLICY_LOSS_REGISTRY))
print([e.value for e in core_algos.AdvantageEstimator])
PY
```

它输出的两个列表，就是你在写配置前应该对照的“可用名字”。如果某个名字不在里面，Hydra 可能仍接受字符串，但训练会在取 loss 函数时才报错——这类错误最好在启动前暴露。

## 本章小结

- 算法 = 优势估计器 + 策略损失 + 数据/奖励流，不是单个开关。
- GRPO/Dr.GRPO 的差别在 std 归一；DAPO 在 clip/采样/归约/长度惩罚；GSPO 在序列级比率。
- 蒸馏、DPO、SFT 是不同目标，不应与 PPO 混为一谈。
- recipe 是算法实验的主要载体，使用前检查 `REQUIRED_VERL.txt`。
- CISPO 裁剪的是重要性权重，梯度仍走 score function；MiniVerl 已实现并有公式/梯度/端到端测试。
- GPG/SAPO/DRO/DPPO/OTB/SPIN 等变体先确认所在层与源码枚举，再谈复现；不要用一个字段代替算法。
- MiniVerl 用同一套分层结构实现核心算法。

## 延伸阅读

- `verl/trainer/ppo/core_algos.py`
- `verl-recipe`：<https://github.com/verl-project/verl-recipe>
- 下一章：多轮对话、工具调用与 AgentLoop。
