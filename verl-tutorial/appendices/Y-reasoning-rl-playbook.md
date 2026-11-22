# 附录 Y 推理模型 RL 实战手册：数据、奖励、长度与稳定性

第 3 章讲 GRPO 家族的数学，第 22 章讲动态批与过滤，第 25 章讲损失注册表。本附录把它们收束成一份**可执行的推理模型 RL 手册**：给定一个 7B/32B 的 base 或 instruct 模型、一个可验证的任务、若干张 GPU，如何从零跑出一个不塌、不刷分、可复现的实验。

它不重复公式推导，而是回答工程上最容易出错的六个问题：

1. 数据应该选多难？
2. 奖励应该怎么定义才不被骗？
3. 算法从 GRPO 起手，什么时候加 DAPO/GSPO/CISPO？
4. 长度为什么会爆炸/坍缩，怎么控制？
5. 训练塌了，先看哪几个指标？
6. 结果怎么评估才算数？

## Y.1 推理 RL 与普通 RLHF 的区别

| 维度 | 普通 RLHF | 推理 RL（RLVR） |
|---|---|---|
| 奖励 | RM/人类偏好 | 规则验证器（答案/单测） |
| 轨迹长度 | 几百 token | 几千到几万 token |
| 信号密度 | 密集但噪声大 | 稀疏但无偏（0/1） |
| 主要成本 | reward model | rollout 生成 |
| 主要失效 | reward hacking | 全对/全错组、长度爆炸、熵坍缩 |
| 评估 | 偏好胜率 | pass@1/pass@k、avg@k、maj@k |

推理 RL 的核心矛盾是：**奖励是对的，但太稀疏**。一条 8K token 的轨迹只在最后得到一个 0/1，组内又经常全 0 或全 1，优势全为 0，梯度消失。所有工程手段——动态采样、token-level loss、clip-higher、overlong shaping、PRM——本质上都在缓解这个矛盾。

## Y.2 一条最小可复现实验

在调任何算法之前，先固定一条“能跑通、能复现、能比较”的最小链路：

```text
数据：1k～10k 条可验证 prompt，按 prompt 分组划分 train/val
模型：Qwen2.5-7B-Instruct 或同等规模
rollout：vLLM，n=8，temperature=1.0，top_p=1.0
奖励：规则 verifier（答案等价类 + 格式）
算法：GRPO（无 critic，KL loss）
训练：batch=512 prompts，response_length=8k～16k
评估：固定 val 集，pass@1 + avg@8，固定采样参数
```

先把这条链路跑 100～300 step，确认：

- reward 均值从随机水平上升；
- `response_length` 没有单调爆炸；
- `actor/entropy` 没有在几十步内掉到接近 0；
- `kl` 没有持续增大；
- val pass@1 与 train reward 同向。

这五条有任何一条不满足，先别加算法。**大多数“算法不 work”其实是数据/奖励/长度的问题。**

## Y.3 数据：难度校准比数据量更重要

### Y.3.1 用 pass@k 估计每题的难度

对每道题，用 base/参考模型采样 $n$ 次，统计正确次数 $c$。无偏 pass@k 估计：

$$
\widehat{\text{pass@}k}
=1-\frac{\binom{n-c}{k}}{\binom{n}{k}}.
$$

实践中常用 $n=8$ 或 $16$，看 $k=1,4,8$：

| 题目类型 | 现象 | 处理 |
|---|---|---|
| 太简单 | $c=n$（全对） | 过滤或降权；对 GRPO 无优势 |
| 太难 | $c=0$（全错） | 过滤或留作课程后期；对 GRPO 无优势 |
| 可学习 | $0<c<n$ | 保留，优先安排 |

对 GRPO，一道题在组内全对/全错时，组内奖励方差为 0，优势为 0。若每题的独立正确率为 $p$，组大小 $n$，则“有效组”（既非全对也非全错）的概率约为：

$$
P_{\text{valid}}(p)=1-p^n-(1-p)^n.
$$

它在中段最大：$p=0.5$ 时 $P_{\text{valid}}\approx 1-2^{1-n}$。这解释了两个常见现象：

- 数据太简单/太难时，即使 batch 很大，有效样本也很少；
- DAPO 的动态采样不是“锦上添花”，而是在把 $p$ 拉回可学习区间。

一个粗略的筛选规则：保留 $\hat p\in[0.1,0.9]$ 的题；对 $n=8$，这能保证大多数组有梯度。

### Y.3.2 难度不是静态的

策略在变，题目的“难度”也在变。每训练若干 step，用当前策略重新估计 $\hat p$，把已经全对的题降权/移出，把刚进入可学习区间的题加回来。这就是课程学习（curriculum）的数据侧实现。

工程上不要每步重算；按 epoch 或每 N step 重估一次，缓存结果，并记录每道题的 `pass_rate` 历史。

### Y.3.3 数据多样性与污染

- **多样性**：同一知识点不要只留一种问法；否则策略会记住模板而不是推理。
- **污染**：训练集与评测集做 n-gram/embedding 近似去重；评测集加 canary；记录数据版本与下载时间。
- **长度分布**：统计 prompt 长度与答案长度；过长的题会挤占 `max_response_length`，过短的题可能太简单。
- **语言/领域**：按桶统计，避免某一类题主导梯度。

数据检查清单：

```text
[ ] 每题有唯一 id、来源、答案、可验证字段
[ ] train/val 按 prompt 分组，无重叠
[ ] 每题有 pass_rate 估计（当前策略）
[ ] 长度分布已统计，超长题已处理
[ ] 与评测集去重，记录指纹
```

## Y.4 奖励：verifier 是目标函数

### Y.4.1 答案抽取与等价

数学/推理任务的 verifier 通常分三步：

1. **抽取**：从输出中找最终答案（`\boxed{}`、`####`、最后一个数字、代码块返回值）；
2. **归一化**：去空格/千分位/单位/LaTeX 包装，分数/小数/根式等价；
3. **比较**：精确匹配或符号等价（sympy）。

常见错误：

- 抽取规则与 prompt 模板不一致（训练时要求 boxed，verifier 却找最后一个数字）；
- 多答案/多解只取第一个；
- 训练集与验证集用不同 verifier；
- 把“答案正确但格式不对”判 0，导致模型先学格式再学推理（可以给很小的格式分，但不能主导）。

### Y.4.2 代码与工具任务

代码奖励通常来自单测：

```text
生成代码 -> 沙箱执行 -> 跑测试 -> 通过率/是否全过
```

风险：执行任意代码、超时、依赖缺失、测试泄漏。生产上必须用隔离沙箱（附录 N/T），并对超时/崩溃返回 0 且留痕。

### Y.4.3 格式奖励与 overlong

格式奖励是 shaping，不是目标。建议：

$$
R = R_{\text{correct}} + \lambda_f R_{\text{format}} - \lambda_l R_{\text{overlong}},
$$

其中 $\lambda_f$ 很小（例如 0.05～0.1），$\lambda_l$ 用于抑制“靠长度刷分”。

DAPO 的 overlong shaping 在硬上限之内再设一个缓冲区：

```text
expected_len = max_response_length - overlong_buffer.len
exceed = valid_response_length - expected_len
penalty = min(-exceed / overlong_buffer.len * penalty_factor, 0)
```

在 v0.9.1 中，legacy `DAPORewardManager` 通过构造函数接收 `overlong_buffer_cfg`（`verl/workers/reward_manager/dapo.py`）；V1 reward loop 从 `reward.reward_kwargs.overlong_buffer_cfg` 读取（`verl/experimental/reward_loop/reward_manager/dapo.py`）。注意：旧文档里的 `reward_model.overlong_buffer` 不是 v0.9.1 主线的配置键，照抄会得到“配置存在但没人读”的假象。

### Y.4.4 奖励的方差

组内奖励全 0/全 1 时，GRPO 的优势为 0。除了动态采样，还可以：

- 增大 `rollout.n`（组更大，更容易出现混合）；
- 用部分分（步骤/子问题）；
- 用 PRM 提供密集信号（附录 K）；
- 对奖励做按题归一化，但不要改变组内排序。

**不要**为了让奖励“看起来更平滑”而对全 0 组做随机扰动；那是把噪声当信号。

## Y.5 算法：从 GRPO 起手，按失效模式加组件

### Y.5.1 GRPO 作为基线

GRPO 的核心是组内基线：

$$
A_i=\frac{R_i-\operatorname{mean}(R_{1:n})}{\operatorname{std}(R_{1:n})}.
$$

它省掉 critic，适合“同一 prompt 多条采样”的推理任务。配置：

```yaml
algorithm:
  adv_estimator: grpo
  norm_adv_by_std_in_grpo: true
  use_kl_in_reward: false

actor_rollout_ref:
  actor:
    use_kl_loss: true
    kl_loss_coef: 0.001
    kl_loss_type: low_var_kl
    loss_agg_mode: token-mean
  rollout:
    n: 8
```

注意：`norm_adv_by_std_in_grpo=true` 是 GRPO 的默认；Dr.GRPO 把它关掉，并配合 token-level/长度归一化的损失聚合。两者不要混用后再解释“为什么方差不同”。

### Y.5.2 什么时候加 DAPO

DAPO 的四个改动对应四类失效：

| 失效 | DAPO 组件 | 配置 |
|---|---|---|
| 优势全 0（全对/全错组） | 动态采样 | `algorithm.filter_groups.enable=true`、`metric=acc`；V1 用 `max_inflight_gen_batches` 限流 |
| 熵坍缩/探索不足 | clip-higher | `clip_ratio_low=0.2`、`clip_ratio_high=0.28` |
| 长序列被短序列稀释 | token-level loss | `loss_agg_mode=token-mean` |
| 截断样本惩罚不对 | overlong shaping | `reward.reward_kwargs.overlong_buffer_cfg` |

一个可复制的 DAPO 片段：

```yaml
data:
  train_batch_size: 512
  gen_batch_size: 1536
  max_response_length: 20480

actor_rollout_ref:
  actor:
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
    loss_agg_mode: token-mean
  rollout:
    n: 8

algorithm:
  adv_estimator: grpo
  filter_groups:
    enable: true
    metric: acc
    max_inflight_gen_batches: 1   # V1：限制在飞的生成批
    # max_num_gen_batches: 10    # legacy Sync DAPO；V1 ReplayBuffer 会忽略并告警
```

`gen_batch_size` 是“每次取多少 prompt 去生成”，`train_batch_size` 是“过滤后真正训练多少 prompt”。两者不要设成一样，否则过滤后 batch 会缩水。

**版本差异（重要）**：legacy Sync DAPO 用 `max_num_gen_batches` 控制“反复生成直到凑够有效组”的上限；v0.9.1 的 V1 `ReplayBuffer` **忽略这个字段**，只打印 warning，改用 `max_inflight_gen_batches` 限制并发，并用 refill/evict 语义替换被过滤的组。另外，开启 `sync_refill_failed_groups` 时要求 `gen_batch_size=1`。照抄旧 recipe 里的 `max_num_gen_batches=10` 不会报错，但也不会生效——这是典型的“配置存在、行为没变”。

还有一个 V1 约束：`filter_groups` 需要**采样时就能拿到 reward metric**。因此它要求使用规则奖励，或把 reward model 放到独立资源池（`reward.reward_model.enable_resource_pool=true`）。如果 RM 与 actor colocate，reward 在回放采样之后才算出来，V1 会直接断言失败。

### Y.5.3 什么时候用 GSPO/CISPO

- **GSPO**：把重要性比率从 token 级搬到序列级，适合长序列、token 级比率方差过大的情况。它的函数签名默认 `seq-mean-token-mean`，但 v0.9.1 的配置默认仍是 `token-mean`（源码注释明确说与论文不同）；要用论文口径就得显式设置 `actor_rollout_ref.actor.loss_agg_mode=seq-mean-token-mean`，否则长序列会被重复加权。
- **CISPO**：裁剪重要性权重并 `detach`，梯度走 score function，适合训练-推理 mismatch 较大、或需要更保守更新的场景。
- **VAPO/PRIME/SPPO**：更偏“价值/过程奖励/自博弈”的组合，先确认你的任务真的有对应的失效，再引入。

选择顺序建议：

```text
GRPO 稳定 -> 有效组太少 -> DAPO 动态采样
         -> 熵掉太快 -> clip-higher / entropy 正则
         -> 长序列比率方差大 -> GSPO
         -> mismatch/不稳定 -> CISPO / rollout correction
```

一次只加一个组件，并保留消融实验。**不要**把四个组件一起开，然后无法归因。

## Y.6 长度与熵：推理 RL 的两个“隐形杀手”

### Y.6.1 长度爆炸

表现：`response_length` 持续上升，reward 不涨或微涨，KL 上升，验证集变差。

原因：

- 奖励里有“长度/格式”捷径；
- overlong 惩罚太弱或没生效；
- 模型学会用更长输出“碰运气”；
- 组内基线让长回复的优势被高估。

处理：

1. 确认 overlong shaping 真的被读取（打印最终 config，见附录 S.7）；
2. 加长度惩罚/长度归一化；
3. 限制 `max_response_length`，并在验证时用同一上限；
4. 检查 verifier 是否对“长但错”给了部分分。

### Y.6.2 熵坍缩

表现：`actor/entropy` 在几十到几百步内快速下降，输出多样性下降，pass@k 不升。

原因：

- 策略过早自信，探索不足；
- clip 太紧/学习率太大；
- 组内优势被标准差归一化放大；
- 数据太简单，模型很快全对。

处理：

- clip-higher（上界放宽）；
- 熵正则（`actor.entropy_coeff`，注意符号：它是 bonus，不是 penalty）；
- 提高 temperature/top-p（但评估要用固定参数）；
- 动态采样/难度课程，避免全对组；
- 降低学习率或增大 batch。

熵不是越高越好。目标是“在验证集上保持多样性，同时 reward 上升”；盲目加熵会把模型推回随机。

### Y.6.3 全对/全错组

这是推理 RL 最常见的“看起来在训练、其实没梯度”的状态。verl 本身暴露的是过滤计数，而不是现成的“有效组比例”：

```text
training/filter_groups/evicted_samples          # 被过滤的组数
training/filter_groups/discarded_surplus_samples # 过滤后多余的样本
training/off_policy/evicted_samples             # 过期样本（异步时）
```

建议在日志侧近似派生 `valid_group_ratio ≈ 1 - evicted/generated`，并额外记录全对/全错计数（可以在 reward manager 或回放脚本里统计）。如果有效组长期低于 20%，优先改数据难度或开动态采样，而不是调学习率。

## Y.7 训练配置模板

下面是一份“从 GRPO 起手”的模板，字段均对应 v0.9.1 的配置树（省略数据路径与集群字段）：

```yaml
data:
  max_prompt_length: 1024
  max_response_length: 16384
  train_batch_size: 512
  gen_batch_size: 1536
  filter_overlong_prompts: true

actor_rollout_ref:
  actor:
    strategy: fsdp2
    ppo_mini_batch_size: 256
    ppo_micro_batch_size_per_gpu: 4
    use_dynamic_bsz: true
    ppo_max_token_len_per_gpu: 32768
    clip_ratio: 0.2
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
    loss_agg_mode: token-mean
    entropy_coeff: 0
    use_kl_loss: true
    kl_loss_coef: 0.001
    kl_loss_type: low_var_kl
    ppo_epochs: 1
  rollout:
    name: vllm
    mode: async
    n: 8
    temperature: 1.0
    top_p: 1.0
    top_k: -1
    gpu_memory_utilization: 0.5
    free_cache_engine: true
    enable_prefix_caching: true
    max_num_batched_tokens: 16384

algorithm:
  adv_estimator: grpo
  norm_adv_by_std_in_grpo: true
  use_kl_in_reward: false
  filter_groups:
    enable: true
    metric: acc
    max_inflight_gen_batches: 1

trainer:
  n_gpus_per_node: 8
  nnodes: 1
  total_epochs: 15
  save_freq: 20
  test_freq: 5
```

这不是“最佳配置”，而是一个**可消融的起点**。每个组件都要有对应的指标与关闭实验。

## Y.8 稳定性诊断：先看哪几个指标

| 指标 | 健康信号 | 异常含义 |
|---|---|---|
| `reward/mean` | 缓慢上升，有波动 | 长期不动：数据/奖励/难度问题 |
| `filter_groups/evicted_samples`（派生有效组比例） | 有效组 > 30% | 太低：全对/全错，梯度消失 |
| `actor/entropy` | 缓慢下降，不贴 0 | 快速掉：熵坍缩 |
| `actor/pg_loss` | 小幅波动 | 大幅震荡：学习率/clip/比率异常 |
| `actor/ppo_kl` / `actor/kl_loss` | 平稳，接近目标 | 持续上升：策略漂移 |
| `response_length` | 平稳或缓慢上升 | 爆炸：长度捷径 |
| `actor/pg_clipfrac` | 0.05～0.3 | 过高：更新太猛/比率偏差 |
| `val/pass@1` | 与 reward 同向 | 背离：过拟合/奖励 hacking |
| `timing/gen` vs `timing/update` | 看瓶颈 | gen 占 80%+：正常但可优化 |

诊断顺序：

```text
reward 不动
  -> 派生有效组比例低？ -> 难度/动态采样
  -> verifier 全 0？ -> 打印样本
  -> 长度爆了？ -> overlong/长度惩罚
  -> 熵掉了？ -> clip/entropy/学习率
  -> kl 爆了？ -> KL 系数/参考模型
```

## Y.9 评估：pass@1 不是全部

### Y.9.1 采样口径

评估必须固定：

- temperature/top-p/top-k；
- 每题采样次数 $n$；
- max response length；
- 是否允许工具/多轮；
- parser 版本。

训练时用 temperature=1.0 探索，评估时可以用 temperature=0（greedy）或固定低温；但**训练与评估的口径要分别记录**，不要混着比较。

### Y.9.2 pass@k、avg@k、maj@k

| 指标 | 含义 | 适用 |
|---|---|---|
| pass@1 | 单次采样正确率 | 线上单次体验 |
| pass@k | k 次中至少一次正确 | 有重试/搜索预算 |
| avg@k | k 次平均正确率 | 整体能力 |
| maj@k | 多数投票正确率 | 有自洽投票预算 |

pass@k 会随 k 上升，不能和 pass@1 直接比较。报告中必须写清 k、n、采样参数。

### Y.9.3 显著性

两个 checkpoint 的 pass@1 差 1～2 个点，可能只是采样噪声。做法：

- 固定评测集与采样种子；
- 用配对检验（同一题同一采样预算下比较）；
- 报告置信区间或 bootstrap；
- 至少看 3 个 seed 的训练曲线，而不是单条。

第 32.13 节给了更完整的实验设计方法。

## Y.10 算力与时间预算

推理 RL 的时间通常由 rollout 主导：

$$
T_{\text{step}}
\approx \frac{B\cdot n\cdot L_{\text{resp}}\cdot C_{\text{gen}}}{H_{\text{rollout}}}
+ T_{\text{update}},
$$

其中 $B$ 是 prompt 数，$n$ 是组大小，$L_{\text{resp}}$ 是平均响应长度，$C_{\text{gen}}$ 是每 token 生成成本，$H_{\text{rollout}}$ 是 rollout 吞吐。

三个直接结论：

1. **长度是乘数**：响应长度翻倍，生成时间近似翻倍；overlong 不只是奖励问题，也是成本问题。
2. **n 是乘数**：GRPO 的组大小直接乘在生成量上；n=16 比 n=8 贵一倍，但可能显著提高有效组比例。
3. **过滤会放大生成量**：动态采样可能生成 2～3 倍的候选才凑够 batch；预算要按重采次数（legacy 的 `max_num_gen_batches`，或 V1 的 refill 队列深度）留余量。

粗略规划时，先测“单卡每秒生成 token 数”和“单步 update 时间”，再反推需要多少卡。不要用 FLOPs 估算代替实测。

## Y.11 常见失败模式

| 现象 | 根因 | 处理 |
|---|---|---|
| reward 全 0 | parser 不匹配/答案格式 | 打印 100 条样本，对齐抽取规则 |
| reward 全 1 | 数据太简单/泄漏 | 重估难度，去重，换 val |
| reward 涨、val 跌 | 奖励 hacking/过拟合 | 对抗探针、换 verifier、早停 |
| 长度爆炸 | 长度捷径/惩罚未生效 | 打印最终 config，开 overlong |
| 熵坍缩 | clip 太紧/学习率大/数据简单 | clip-higher、熵正则、课程 |
| 有效组比例低 | 难度两极化 | 动态采样、重估 pass_rate |
| KL 持续上升 | 参考模型/系数/学习率 | 检查 ref 是否被覆盖，调 KL |
| 训练-推理 logprob 不一致 | 引擎/精度/template | rollout correction、对齐 dtype |
| 多机吞吐不升 | rollout 瓶颈/通信 | 看 `timing/gen`、PD 分离、TP |
| 结果不可复现 | 种子/数据顺序/引擎 | 固定 seed、determinism、记录指纹 |

## Y.12 从 7B 到 32B/671B

| 规模 | 训练后端 | rollout | 关键约束 |
|---|---|---|---|
| 7B | FSDP2 | vLLM 单机 | 显存足够 colocate；先调 batch/length |
| 32B | FSDP2/Megatron | vLLM/SGLang 多卡 | TP/PP、权重同步、长序列 |
| 671B MoE | Megatron + EP | vLLM/SGLang 分离 | EP/ETP、专家并行、PD 分离 |

规模上去后，算法不变，系统约束变成主要矛盾：

- **权重同步**：训练布局与推理布局不同，同步字节与频率成为瓶颈（第 14 章、附录 W）；
- **长序列**：CP/SP/Ulysses、KV cache、prefix caching（第 13、16 章）；
- **MoE**：EP/容量/路由一致性（第 12、16、30 章，附录 V）；
- **容错**：NCCL timeout、rollout 重启、checkpoint 恢复（第 31 章）。

一个常见误区：在小规模上调好的超参，直接放大到 32B。学习率、batch、KL 系数、clip 都可能需要重调；**先做规模不变的 smoke test，再做规模扫描**。

## Y.13 MiniVerl 的对应实现

MiniVerl 用 bandit/算术任务演示了推理 RL 的系统链路：

- `mini_verl/rewards.py`：数学抽取、精确匹配、格式/长度惩罚、DAPO overlong；
- `mini_verl/algo/core_algos.py`：GAE、GRPO、RLOO、REINFORCE++、ReMax、CISPO；
- `mini_verl/trainer/main_ppo.py`：同步/异步主循环、动态过滤、长度均衡；
- `examples/03_grpo_vs_ppo.py`、`examples/08_dapo_filtering.py`、`examples/20_cispo_training.py`：算法对照；
- `examples/15_math_reward.py`：数学 verifier 的抽取与归一化。

它不模拟长 CoT 的语义质量，但把“数据 → 奖励 → 组基线 → 过滤 → 更新”的契约跑通了。读完后回到真实 verl，差异主要在模型、引擎与规模，而不是算法骨架。

## Y.14 检查清单

**数据**

- [ ] 每题有 pass_rate 估计，难度分布合理。
- [ ] train/val 按 prompt 分组，无污染。
- [ ] 长度分布已统计，超长题已处理。
- [ ] 数据指纹与版本已记录。

**奖励**

- [ ] 抽取/归一化/等价规则与 prompt 模板一致。
- [ ] 训练与验证使用同一 verifier。
- [ ] 格式分权重足够小。
- [ ] overlong shaping 真正被读取。
- [ ] 失败样本计 0 且留痕。

**算法**

- [ ] 从 GRPO 基线开始，逐组件消融。
- [ ] 动态采样与 `gen_batch_size`/`train_batch_size` 匹配。
- [ ] GSPO/CISPO 的聚合方式与算法匹配。
- [ ] KL、clip、熵的配置有明确意图。

**监控**

- [ ] 派生有效组比例、`actor/entropy`、`response_length`、`actor/ppo_kl`、`actor/pg_clipfrac` 都在看。
- [ ] `timing/gen` 与 `timing/update` 分开。
- [ ] 代理奖励与真实评测同时记录。

**评估**

- [ ] 固定采样参数与 parser。
- [ ] 报告 pass@1/pass@k 的口径与 k。
- [ ] 多 seed/配对检验，不拿单点噪声当提升。
- [ ] 与基线 checkpoint 做同口径对比。

## 本章小结

- 推理 RL 的核心矛盾是稀疏奖励与全对/全错组；数据难度校准优先于算法调参。
- 用 pass@k 估计每题难度，保留可学习区间；动态采样是在线修复难度分布。
- verifier 是目标函数：抽取、归一化、等价、超时与失败留痕都要工程化。
- GRPO 是基线；DAPO 解决有效组/熵/长序列/截断四类失效；GSPO/CISPO 在比率方差或 mismatch 大时使用。
- 长度爆炸与熵坍缩是两个隐形杀手；先看派生有效组比例、`actor/entropy`、`response_length`、`actor/ppo_kl`。
- 评估要固定口径，报告 pass@1/pass@k 的 k 与采样参数，并用多 seed/配对检验判断显著性。
- 规模上去后，系统约束（权重同步、长序列、MoE、容错）取代算法成为主要矛盾。

## 延伸阅读

- DeepSeekMath / GRPO：<https://arxiv.org/abs/2402.03300>
- DAPO：<https://arxiv.org/abs/2503.14476>
- GSPO：<https://arxiv.org/abs/2507.18071>
- DeepSeek-R1：<https://arxiv.org/abs/2501.12948>
- VAPO：<https://arxiv.org/abs/2504.05118>
- PRIME：<https://arxiv.org/abs/2502.01456>
- 第 3 章：GRPO 家族与估计器
- 第 22 章：动态批、DAPO 过滤与过采样
- 第 25 章：策略损失注册表
- 第 32 章：性能与实验设计
- 附录 K：过程奖励、树搜索与自我博弈
- 附录 L：评测与基准
- 附录 X：奖励模型与 RLAIF
