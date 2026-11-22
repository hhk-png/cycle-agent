# 第 17 章 Reward：规则奖励、模型奖励与 reward loop

## 17.1 奖励是 RL 的目标函数

PPO/GRPO 只优化“期望奖励”。奖励函数错了，算法再正确也会学到错的东西。奖励的三种来源：

1. **规则验证器（verifiable reward）**：数学答案、单元测试、工具返回、格式检查；
2. **奖励模型（reward model）**：人类偏好训练的标量模型；
3. **环境奖励**：游戏分数、编译器反馈、用户行为。

verl 的 reward 配置：

```yaml
reward:
  reward_manager:
    source: register           # register | importlib
    name: naive                # naive / dapo / prime / batch（legacy）
  custom_reward_function:
    path: my_module.py
    name: my_reward
  reward_model:
    enable: false
    model_path: ...
  sandbox_fusion:
    ...
```

v0.9.1 有两套实现：

- `verl/workers/reward_manager/`：legacy manager（`naive`、`dapo`、`prime`、`batch`，另有 `abstract` 基类）；
- `verl/experimental/reward_loop/`：V1 reward loop（`naive`、`dapo`、`gdpo`、`rate_limited`、`remote`，含 registry、模型 server、限流、路由）。

## 17.2 规则奖励

### 数学

GSM8K 的经典流程：

1. 从模型输出中抽取最终答案（`#### 42`、`\boxed{42}`、最后一个数字）；
2. 与 `reward_model.ground_truth` 比较；
3. 正确 1，错误 0。

`verl/utils/reward_score/` 下有 `gsm8k.py`、`math_reward.py`、`math_verify.py`、`math_dapo.py`、`math_batch.py`、`geo3k.py` 等（没有 `math.py`）。v0.9.1 也支持 `math_verify` 等外部验证器。

常见坑：

- 抽取规则与 prompt 模板不一致；
- 数字格式（`1,000`、`3.50`、分数）未归一化；
- 多答案/多解；
- 模型输出“答案是 42”但没有 boxed，规则漏判；
- 训练集与验证集使用不同抽取规则。

### 代码

代码奖励通常需要沙箱：

1. 从响应中抽取代码块；
2. 在隔离环境执行；
3. 跑单元测试；
4. 返回通过率/是否通过。

风险：

- 执行任意代码的安全问题；
- 超时/死循环；
- 依赖缺失；
- 测试泄漏。

verl 提供 sandbox fusion 集成，把执行放到独立服务。

### 格式奖励

格式奖励通常作为 shaping：

```python
score = correctness + 0.1 * has_boxed + 0.1 * has_reasoning
```

它可以帮助模型尽早学会输出格式，但权重不能太高，否则会 reward hacking（只输出格式不推理）。

## 17.3 模型奖励

奖励模型是一个标量模型：

```text
(prompt, response) -> scalar
```

训练方式：

- 人类偏好对（chosen/rejected）→ Bradley-Terry；
- 规则蒸馏（用可验证答案构造偏好对）；
- 校准与对抗探针（让分数可当作阈值使用）。

注意：**过程奖励模型（PRM）不是标量 ORM 的一种训练方式**，而是另一种打分契约：ORM 是 `(prompt, response) -> scalar`，PRM 是 `(prompt, step_1, ..., step_k) -> (s_1, ..., s_k)`。PRM 的标签、损失、聚合与接入位置都不同，见 17.12 与 17.17。

verl 的 `reward.reward_model.enable=true` 会启动 reward worker/server。它可以是：

- 与 actor 同规模的模型；
- 小得多的判别模型；
- 远程 API。

偏好数据怎么构造、Bradley-Terry 怎么落到 loss、RM 怎么校准与评估、RLAIF/Constitutional AI 怎么接入，见附录 X。那里也解释了为什么 `reward_model.enable` 是推理开关而不是训练开关。

模型奖励的风险：

- reward hacking：模型学会骗奖励模型；
- 分布漂移：奖励模型没见过新策略的输出；
- 成本：每个样本一次前向；
- 延迟：同步训练中成为瓶颈。

## 17.4 RewardManager 的职责

`NaiveRewardManager`：

1. 对每个样本调用 reward function；
2. 得到标量分数；
3. 把分数放到最后一个有效 response token；
4. 返回 `token_level_rewards` 与 `rm_scores`。

为什么放在最后一个 token？

- PPO/GAE 需要 token 级奖励；
- 终止奖励放在末尾，GAE 从后往前传播；
- mask=0 的位置不参与。

DAPO reward manager 额外做：

- overlong penalty；
- 长度惩罚；
- 规则/格式组合。

MiniVerl 的 `RewardManager.run` 就是这套逻辑：

```python
valid = np.flatnonzero(response_mask[row] > 0)
token_rewards[row, valid[-1]] = score
```

## 17.5 Reward Loop

v0.9.1 的 `verl/experimental/reward_loop/` 把 reward 计算做成异步服务：

- `RewardLoopManager`：管理 worker/server；
- `reward_manager/naive.py`、`dapo.py`、`gdpo.py`、`limited.py`（注册名 `rate_limited`）、`registry.py`、`remote.py`；
- `reward_model.py`：模型奖励；
- `router/`：请求路由；
- 支持限流、超时、重试。

它解决的问题：

- 规则奖励可能是 CPU 密集（正则、沙箱、执行）；
- 模型奖励需要 GPU；
- reward 计算可能比生成还慢；
- 需要与训练解耦。

MiniVerl 的 `RewardLoopManager` 用 `ThreadPoolExecutor` 模拟并发与重试，记录 `failures`/`retries`。

## 17.6 奖励归一化与尺度

不同任务的奖励尺度可能差异巨大：

- 数学 0/1；
- 代码通过率 0～1；
- 模型奖励 -10～10；
- 长度惩罚 -1～0。

直接混合会导致某些任务主导梯度。常见处理：

- 按任务归一化（z-score/rank）；
- 多任务加权；
- 奖励裁剪；
- 分组基线（GRPO 自带）；
- 分别训练再合并。

verl 的 `reward_model` 与自定义 reward 可以在函数内部做归一化；没有全局的“自动多任务平衡”。

## 17.7 Reward Hacking

典型表现：

- 输出变长但正确率不升；
- 格式奖励很高、正确率很低；
- 模型学会重复 prompt；
- 代码通过简单测试但不解决实际问题；
- 奖励模型分数上涨、人工评估下降。

对策：

- 验证集与奖励函数解耦；
- 定期人工抽查；
- 多奖励组合；
- 长度惩罚；
- 对抗性测试；
- 过程奖励/中间检查。

## 17.8 奖励与 mask 的交互

多轮轨迹：

```text
assistant: <tool>calc(3+4)</tool>
tool:      <obs>7</obs>
assistant: 7
```

奖励应该加在最后一个 assistant token，而不是 tool observation。mask=0 的 observation 不参与策略损失，但奖励要能穿过它传播（REINFORCE++ 的 running_return 处理）。

MiniVerl 的 `AgentLoop` 返回的 `response_mask` 把 `<obs>...</obs>` 标为 0；`RewardManager` 把分数放在最后一个 mask=1 的 token。

## 17.9 奖励函数的工程化

一个可维护的 reward 函数应该：

- 纯函数、可单测；
- 对异常返回 0 并记录；
- 有超时；
- 不依赖全局状态；
- 版本化；
- 能在小样本上快速回放。

MiniVerl 的 `load_custom_reward("module:function")` 演示了动态加载；`examples/custom_reward.py` 是一个例子。

## 17.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| reward 全 0 | 抽取规则不匹配 | 打印 100 条样本 |
| reward 全 1 | 验证太松/数据泄漏 | 检查 ground truth 与测试 |
| reward 波动大 | 模型奖励不稳定 | 归一化、裁剪、集成 |
| reward 计算慢 | 沙箱/模型串行 | reward loop、并发、缓存 |
| 训练 reward 涨但验证不涨 | reward hacking | 换验证集、加惩罚 |
| 多轮 reward 错位 | 放在 observation 上 | 找最后一个 assistant token |

## 17.11 MiniVerl 对照

- `mini_verl/rewards.py`：`math_reward`、`bandit_reward`、`format_reward`、`length_penalty`；
- `RewardManager` / `DAPORewardManager` / `RewardLoopManager`；
- `mini_verl/agent.py`：多轮 mask；
- `examples/13_custom_reward.py`：自定义奖励；
- `examples/15_math_reward.py`：数学抽取。

测试 `tests/test_data_rewards.py` 覆盖了抽取、归一化、token 位置、overlong 惩罚与并发执行。

## 17.12 过程奖励模型（PRM）：从结果监督到步骤监督

结果奖励模型（ORM）只看最终答案：

```text
(prompt, full_response) -> scalar
```

过程奖励模型（PRM）给每个推理步骤打分：

```text
(prompt, step_1, ..., step_k) -> (s_1, ..., s_k)
```

### 为什么需要 PRM

在长链推理里，最终奖励是稀疏的：一条 8K token 的轨迹只在最后得到一个 0/1。策略梯度要把这个信号分配给几千个 token，方差极大。PRM 把“这一步是否走在正确路上”变成密集信号，能缓解信用分配问题。

但 PRM 也引入新的失败模式：**步骤边界怎么切、标签从哪来、分数怎么聚合**。这三件事没有标准答案。

### 训练目标

如果每个步骤有二元标签 $y_k\in\{0,1\}$，最直接的是逐步二元交叉熵：

$$
\mathcal{L}_{\text{PRM}}
=-\frac{1}{K}\sum_{k=1}^{K}
\left[y_k\log \sigma(r_k)+(1-y_k)\log(1-\sigma(r_k))\right],
$$

其中 $r_k$ 是 PRM 对第 $k$ 步的 logit。数据可以来自人工标注（如 PRM800K 风格）、蒙特卡洛 rollout 的成功率、或强模型的步骤验证。

这里有一个容易被含糊过去的区别：$y_k$ 到底表示什么？至少有两种目标，它们用同一个 BCE 损失，却训练出不同的模型：

- **过去正确性** $y_k^{\text{past}}$：到第 $k$ 步为止的推理是否仍然正确（前缀是否与参考解一致）。它适合定位“哪一步开始错”。
- **未来可解性** $y_k^{\text{future}}$：从第 $k$ 步的中间状态继续采样，最终能否成功。它适合估计“这一步之后还有没有救”，但对策略分布敏感、成本高。

人工标注通常接近前者；蒙特卡洛 rollout 得到的是后者；MiniVerl 的 `suffix` 标签是前者的一个廉价近似（一旦某步错，后续都标 0）。不要把两种标签混在同一个训练集里还期待校准。

### 步骤切分与 token 映射

PRM 的第二个误差源不是模型，而是**步骤边界**。训练时的步骤切分必须和推理时一致，否则第 $k$ 个标签会落到错误的 token 上：

- **按分隔符切**：换行、`\n\n`、`Step k:`、`<|step|>`。简单，但模型可能不遵守格式；需要为“缺少分隔符”的轨迹定义回退策略。
- **按固定 token 数切**：实现最简单，适合教学与吞吐估算；缺点是切点与语义步骤无关。
- **按工具/轮次切**：Agent 场景天然有 turn 边界（一次 assistant 输出 + 一次 observation），见第 26 章。
- **用模型切**：让强模型或规则解析器标注步骤，再人工抽检；成本最高，边界最接近语义。

切完还要做三件事：

1. **重新 tokenize 后核对边界**：训练数据里的 step span 是 token 下标，不是字符下标；`decode` 再 `encode` 可能改变长度。
2. **把步骤分数映射回 token span**：`s_k` 可以广播到该步的所有 response token，也可以只放在该步最后一个 token。前者是密集奖励，后者接近轨迹奖励；两者对 GAE 的影响不同。
3. **处理不完整步骤**：被 `max_response_length` 截断的轨迹没有“最后一步”。要么丢掉，要么把“截断”作为一个负步骤；不能在推理时假装它完整。

### 分数聚合

推理时有六种常见聚合（MiniVerl 的 `AGGREGATIONS` 与之一一对应）：

| 方式 | 公式 | 特点 |
|---|---|---|
| 最后一步 | $s_K$ | 最接近 ORM，信号稀疏，可能漏掉早期错误 |
| 第一步 | $s_1$ | 只看开头，适合“方向性”判断，容易被简单首步骗过 |
| 最小值 | $\min_k s_k$ | 保守，惩罚“前面对了最后错” |
| 最大值 | $\max_k s_k$ | 乐观，只要有一个高光步骤就给高分，通常不建议做奖励 |
| 平均值 | $\frac1K\sum_k s_k$ | 平滑，但会被简单步骤稀释，也会掩盖单点失败 |
| 乘积/对数概率 | $\prod_k s_k$ | 强调每一步都不能错；必须在 log 空间计算，否则长轨迹会下溢 |

没有哪一种总是更好。数学证明类任务常用最小值或最后一步；代码/Agent 任务常用平均或按关键步骤加权；安全/合规场景倾向最小值或乘积，因为“任何一步违规”都应否决整条轨迹。

### PRM 进入 RL 的三种方式

1. **作为密集奖励**：把 $s_k$ 加到第 $k$ 步的 token 奖励上。简单，但会改变最优策略；如果 shaping 不是 potential-based，可能引入偏差。
2. **作为步骤优势**：在步骤级做 GAE/组基线，再广播到该步骤的 token。需要步骤边界与 mask。
3. **作为过滤/重排信号**：不直接进梯度，而是用于 best-of-n、树搜索或数据筛选。风险最低，收益也最间接。

v0.9.1 的 reward loop 对判别式 RM 有原生路径（`verl/experimental/reward_loop/reward_model.py`，测试见 `tests/experimental/reward_loop/test_reward_model_disrm.py`）；生成式 RM 也有测试（`test_reward_model_genrm.py`），但 reward loop **不会自动解析生成文本**，必须提供 `custom_reward_function.path` 把模型输出转成标量，否则会报错（详见附录 X.7）。PRM 通常作为自定义 reward function 或独立的 reward server 接入，而不是一个内置开关。

### PRM 的坑

- **步骤切分与 tokenizer 不一致**：训练时按换行切，推理时按模型输出切，边界错位会让标签对不上；
- **PRM 奖励 hacking**：策略学会写“看起来正确”的步骤，而不是正确答案；
- **分布漂移**：PRM 在旧策略的输出上训练，新策略会找到它的盲区；
- **成本**：每个步骤一次前向，长轨迹的 reward 成本可能超过生成。

## 17.13 生成式奖励与 rubric

生成式奖励模型（GenRM）不输出一个标量，而是让 LLM 生成判断：

```text
prompt + response + rubric
  -> "步骤 1 正确；步骤 2 计算错误；最终答案正确"
  -> 解析为分数
```

它与传统 RM 的区别：

| 维度 | 标量 RM | GenRM |
|---|---|---|
| 输出 | 一个分数 | 解释 + 分数 |
| 可解释性 | 低 | 高 |
| 成本 | 一次前向 | 一次生成（可能很长） |
| 偏差 | 训练数据偏差 | 位置/长度/措辞偏差 |
| 适用 | 大规模筛选 | 小规模高质量标注 |

### rubric 设计

rubric 是把“好”拆成可检查的条目：

```text
- 最终答案与标准答案一致（必须）
- 关键中间步骤完整（必须）
- 没有编造定理/引用（必须）
- 表达简洁（加分，最多 1 分）
```

每条 rubric 最好能独立验证，避免“整体感觉不错”这种不可复现的判断。GenRM 的输出要能被 parser 稳定解析；解析失败应视为 0 分并记录，而不是让异常中断训练。

### 偏差与校准

GenRM 常见偏差：

- **位置偏差**：pairwise 比较时偏好第一个/最后一个；
- **长度偏差**：更长的解释得分更高；
- **自我偏好**：同族模型互相打高分；
- **格式偏差**：Markdown 更漂亮不等于更正确。

对策：交换位置重复评估、长度归一化、多模型集成、用人类标注做校准集。GenRM 的分数尺度通常不是概率，不要直接和规则奖励相加；先做 z-score 或分位数映射。

## 17.14 多目标奖励与约束优化

真实产品很少只有一个目标：正确性、长度、成本、延迟、安全、风格都要兼顾。最常见的是线性标量化：

$$
R=\sum_{j=1}^{m} w_j R_j.
$$

它简单，但有两个问题：

1. **量纲不同**：正确率 0/1，延迟几百毫秒，直接相加会被大量纲主导；
2. **权重难调**：$w_j$ 改变一点，策略可能完全偏向某个目标。

更稳妥的做法：

- **按任务/目标归一化**：z-score、rank、分位数映射到 [0,1]；
- **约束形式**：把安全/延迟作为约束，优化主目标，用拉格朗日乘子或阈值过滤；
- **Pareto 前沿**：跑多组权重，观察不同权衡，而不是追一个“最优”；
- **分阶段**：先学正确性，再加入长度/风格 shaping，避免早期信号互相干扰。

拉格朗日形式：

$$
\max_\theta \; \mathbb{E}[R_0]
\quad\text{s.t.}\quad
\mathbb{E}[C_j]\le d_j,
$$

实践上把它写成 $R_0-\sum_j\lambda_j(C_j-d_j)$，并周期性更新 $\lambda_j$。这不是 verl 的内置功能，但可以在 reward function 里实现；关键是**约束要在验证集上监控**，否则模型会学会在训练分布上满足约束。

## 17.15 Reward Loop 的协议与工程实现

前面的 RewardManager 是“在 trainer 进程里同步调用一个 Python 函数”。当奖励来自 reward model、代码沙箱或远程服务时，它变成一条独立的推理链路：`verl/experimental/reward_loop/`。

### 一次 reward 请求长什么样

一个最小请求应包含：

```text
request_id        # 幂等键，重试不重复计费
data_source       # 路由到哪个 reward model/verifier
prompt            # 原始 prompt（模型奖励可能需要）
response          # 生成文本
response_mask     # 哪些 token 是模型生成的
metadata          # 任务 id、测试用例、工具返回
timeout_ms
```

响应：

```text
score             # 标量
reward_extra_info # accuracy/format/长度等，用于指标
error             # 可重试/不可重试
latency_ms
```

`RewardLoopManager` 负责把 batch 拆成请求、并发发送、收集结果、把失败转成 0 分并记录。legacy `verl/workers/reward_manager/` 下的 `naive.py`、`dapo.py`、`prime.py`、`batch.py` 决定“标量如何变成 token-level reward”；`verl/experimental/reward_loop/reward_manager/` 下则是 `naive.py`、`dapo.py`、`gdpo.py`、`limited.py`、`remote.py`。两套目录不要混用。

### Worker 生命周期

```text
spawn workers ──► warmup (load model/tokenizer) ──► serve loop
     ▲                                              │
     └────────── restart on health check failure ◄──┘
```

生产实现要处理：

- **warmup**：reward model 首次加载可能几十秒，不能算进单请求超时；
- **health check**：进程活着但 CUDA OOM，需要重启；
- **优雅退出**：在飞请求要么完成，要么返回可重试错误；
- **批处理**：把多个请求拼成一个 model batch，提高 GPU 利用率；
- **限流**：reward model 的 QPS 远低于 rollout，必须背压；
- **重试**：只重试幂等错误（超时、5xx），不重试“答案错误”。

### Router

`reward_loop/router/` 提供两种路由：

- `naive_router.py`：轮询/随机；
- `inner_sglang_router.py`：把请求发给 SGLang 内部 router，利用其负载均衡。

路由键通常是 `data_source`：数学题走规则 verifier，代码题走沙箱，开放题走 GenRM。路由错会把代码题送进数学 parser，分数全 0，但不会报错。

### 超时、重试与失败计费

推荐策略：

| 错误 | 处理 |
|---|---|
| 连接超时/5xx | 重试 1～2 次，指数退避 |
| 4xx/参数错 | 不重试，记录并返回 0 |
| 沙箱超时 | 返回 0，并标记 `timeout` |
| reward model OOM | 重启 worker，请求重试 |
| 解析失败 | 返回 0，记录原始输出供排查 |

失败样本不能静默变成“正确答案为 0”而不留痕；否则 reward hacking 与基础设施故障无法区分。至少要在指标里记录 `reward/error_rate`、`reward/timeout_rate`、`reward/retry_count`。

### 与训练循环的交互

reward loop 是 rollout 之后、advantage 之前的同步点。异步 RL 中它也可能成为瓶颈：rollout 生产很快，reward 服务排队，trainer 等 reward。监控要分开看 `timing/gen`、`timing/reward`、`timing/update`；如果 reward 占比高，优先加 reward worker 或批处理，而不是加 GPU。

### MiniVerl 的对应实现

`mini_verl/rewards.py::RewardLoopManager` 用线程池模拟并发、超时/重试与失败计数；`RewardManager` 负责把标量 scatter 到最后一个 response token。它没有独立的 reward model server/路由，但保留了“请求 → 响应 → 失败计数”的契约。`tests/test_data_rewards.py` 覆盖了规则奖励与失败转换。

## 17.16 MiniVerl 的标量奖励模型：从偏好对到 PPO 奖励

前面 17.3 节把模型奖励讲成“一个黑盒服务”，附录 X 讲了训练与校准的方法。MiniVerl 现在把这条链路真正落地为一个可运行、可测试的最小实现：`mini_verl/reward_model.py`。

它不追求语义质量，只固定六个契约：**偏好数据的方向、Bradley-Terry 的数值稳定、mask 的对齐、检查点的一致性、校准的可度量、以及接入 PPO 后的奖励位置。**

### 数据契约

`build_preference_pairs` 把 MiniVerl 的 RL 数据集变成 `(prompt, chosen, rejected)` 三元组：

```python
from mini_verl.data import CharTokenizer, build_dataset
from mini_verl.reward_model import build_preference_pairs, make_preference_batches

tokenizer = CharTokenizer()
pairs = build_preference_pairs(
    build_dataset("bandit", 128, seed=0), tokenizer,
    max_prompt_length=16, max_response_length=8,
)
batches = make_preference_batches(pairs, batch_size=16, pad_token_id=tokenizer.pad_token_id)
```

负例不是随机字符串：bandit 任务选一个不同的动作，其他任务改动第一个字母/数字。这样负例与正例共享大部分 token，RM 必须看 prompt 与 response 的组合，而不能只学“某个 token 一定好”。这条规则对应附录 X.2.2 的“规则蒸馏”：用可验证答案构造偏好对，但不要把合成负例当成人类偏好数据质量的上限。

`PreferenceBatch` 里 chosen/rejected 各自 padding，`attention_mask` 是唯一的位置权威。padding 位置不参与 pooling，也不参与损失；测试 `test_preference_pairs_and_batches_pad_without_scoring_padding` 会把 padding 改成任意 token，断言分数不变。

### 模型与损失

`TinyRewardModel` 提供三种 backbone：

| arch | 结构 | pooling | 用途 |
|---|---|---|---|
| `linear` | embedding + LayerNorm + 标量头 | mean | 验证最简线性 RM 的边界 |
| `mlp` | embedding + LayerNorm + GELU + 标量头 | mean | CPU 上默认，能学 context×response 的非线性交互 |
| `transformer` | `TinyTransformer` trunk + 标量头 | last | 与生产 RM 的“最后一个有效 token”形状一致 |

损失直接复用附录 X.3 的 Bradley-Terry：

$$
\mathcal{L}_{\text{BT}}
=-\frac{1}{N}\sum_i \log\sigma\left(r(x_i,y_w)-r(x_i,y_l)-m\right),
$$

`margin` $m$ 要求 chosen 至少领先多少；`label_smoothing` 把一部分概率质量分给翻转偏好，降低标注噪声下的过度自信。两个项都用 `log_sigmoid` 的稳定形式：

```text
log_sigmoid(z) = -log(1 + exp(-z)) = -softplus(-z)
```

`mini_verl/autograd.py` 新增了 `sigmoid`、`softplus`、`log_sigmoid` 三个算子，前向用分支避免 `exp` 溢出，反向分别是 `σ(1-σ)`、`σ`、`σ(-z)`。测试用有限差分与极端值（$z=\pm 500$）同时验证数值与梯度；如果写成 `log(sigmoid(z))`，极端负值会得到 `-inf`，梯度也会坏掉。

### 训练与评估

```bash
python -X utf8 -m mini_verl.trainer.main_rm configs/rm_bandit.yaml
```

`RMTrainer` 的循环是：

```text
build preference pairs
  -> AdamW(BT loss)
  -> every eval_freq: pairwise accuracy / margin / ECE
  -> keep best state, early stop on target accuracy or patience
  -> save reward_model.npz + rm_config.json + tokenizer.json
```

评估不只看 loss，而是同时报：

- **pairwise accuracy**：chosen 分数严格大于 rejected 的比例；
- **margin**：平均分差，反映置信度；
- **ECE**：把 `σ(z)` 当概率时，置信度与准确率的差距。

在 bandit 任务上，每步 16 对、20 步左右就能到 100% pairwise accuracy；这不是“RM 很强”，而是任务足够可验证，用来固定数据/损失/评估的契约。

### 接入 PPO

训练好的 RM 通过 `ModelRewardManager` 替换规则奖励：

```yaml
reward:
  reward_model:
    enable: true
    model_path: outputs/reward_model/bandit
    normalize: none        # none | zscore | minmax
```

`PPOTrainer.init()` 看到 `enable=true` 就构造 `ModelRewardManager`，要求必须有 `model_path`，否则提前失败；它不会悄悄退回规则奖励。`run(data)` 用 `input_ids` + `attention_mask` 打分，再把标量 scatter 到最后一个 `response_mask=1` 的位置：

```python
valid = np.flatnonzero(response_mask[row] > 0)
token_rewards[row, valid[-1]] = score
```

这与 `NaiveRewardManager` 的位置契约完全一致，因此 GAE、in-reward KL、DAPO 过滤都不需要改。独立评测会调用 `without_shaping()`，避免把训练期的 overlong 惩罚带进 pass@k。

三个必须说清的边界：

1. **`reward_model.enable` 仍是推理开关**。训练走 `main_rm.py`，不是 PPO 的配置开关；这与真实 verl 的语义一致（附录 X.7）。
2. **没有 HTTP server / router / 多租户**。MiniVerl 验证请求-响应与失败语义，不验证吞吐；真实 reward loop 的限流、超时、重试见 17.15 节。
3. **不是语义 RM**。线性/MLP/一层 Transformer 只能证明“偏好方向能学、分数能校准、检查点能复现”，不能证明它理解写作质量。

完整端到端示例见 `examples/25_reward_model.py`：训练 RM → 报 accuracy/ECE → 保存/加载 → 只用 RM 奖励跑 40 步 PPO → 用规则验证器独立测真实准确率。测试 `tests/test_reward_model.py` 固定了稳定算子梯度、损失方向、margin/label smoothing、padding 不参与、检查点往返、scatter 位置、`without_shaping` 与缺 `model_path` 的早失败。

## 17.17 MiniVerl 的过程奖励模型：步骤标签、前缀打分与奖励组合

17.12 节讲了 PRM 的方法论，附录 K 讲了搜索与自我博弈。这一节把 PRM 做成 MiniVerl 里可运行、可测试的最小实现：`mini_verl/prm.py`。

先说明边界：**v0.9.1 的真实 verl 没有 `main_prm` 这个 CLI，也没有 `reward.prm` 这个内置配置键**。verl 的做法是把 PRM 当作自定义 reward function 或 reward server 接到 reward loop 上。MiniVerl 额外提供 `reward.prm`，是为了让“训练 PRM → 保存 → PPO 消费”这条链路能在一份配置里跑通；它是教学扩展，不是对上游 API 的复刻。

这个实现固定六个契约：

1. **步骤标签的含义**：`local` 只标出错的步骤，`suffix` 把错误传播到后续步骤；
2. **前缀打分**：第 $k$ 步的分数只能看到 `prompt + step_1..step_k`，不能偷看未来；
3. **数值稳定的 BCE**：用 `softplus(z) - y*z`，不用 `log(sigmoid(z))`；
4. **步骤聚合**：`last/first/min/max/mean/product` 六种方式，乘积在 log 空间算；
5. **检查点一致性**：步长、标签模式、聚合方式、tokenizer 一起保存；
6. **接入 PPO 的字段兼容**：仍写 `rm_scores` 与 `token_level_rewards`，下游不需要 PRM 分支。

### 17.17.1 数据契约：一条轨迹如何变成前缀样本

`StepSample` 只存三样东西：prompt token、按步骤切好的 response token、每步的二元标签。

```python
from mini_verl.data import CharTokenizer, build_dataset
from mini_verl.prm import build_step_samples

tokenizer = CharTokenizer()
samples = build_step_samples(
    build_dataset("copy", 128, seed=0),
    tokenizer,
    max_prompt_length=16,
    max_response_length=4,
    step_tokens=1,
    corruptions_per_sample=8,
    label_mode="suffix",
    seed=0,
)
```

正例是 gold answer 按 `step_tokens` 切出来的轨迹；每个反例替换其中一个步骤的 token。前缀由 `step_prefixes` 构造：

```text
prefix_k = prompt + step_1 + ... + step_k
```

这保证第 $k$ 个分数只看得到第 $k$ 步结束时的信息。训练数据里包含 EOS，因为 rollout 也会把 EOS 当作合法 response token；如果训练时去掉 EOS，推理时 PRM 会把一个没见过的“最后一步”当成异常。

两个标签模式的区别不是实现细节，而是目标函数：

| 模式 | 标签 | 学到的东西 | 适用 |
|---|---|---|---|
| `local` | 只有被破坏的步骤为 0 | 定位“哪一步错了” | 步骤诊断、细粒度反馈 |
| `suffix` | 被破坏步骤及其后所有步骤为 0 | “一旦错了就无法挽回” | 轨迹级奖励、配合 `min`/`product` |

反例 token 只从**单字符字母数字**里选，而不是从标点里选。原因是 rollout 往往被约束在字母动作空间；如果训练时只见过标点错误，策略会用字母序列去攻击 PRM 的盲区。开发时做过对照：用标点做反例时，PRM-only PPO 很快学会输出高分的错误串；改成字母数字反例后，PRM 对策略实际能产生的错误才有区分度。

### 17.17.2 损失：稳定 BCE 与 mask

逐步 BCE 的稳定写法是：

$$
\mathcal{L}_{\text{PRM}}
=\frac1N\sum_i \left[\operatorname{softplus}(z_i)-y_i z_i\right],
$$

它等价于 `BCEWithLogits`。当 $z=-500$ 且 $y=1$ 时，`softplus(-500)≈0`，损失有限、梯度是 $\sigma(z)$ 量级；而 `log(sigmoid(-500))` 会直接变成 `-inf`。

```python
from mini_verl.autograd import Tensor
from mini_verl.prm import step_bce_loss

loss = step_bce_loss(scores, labels)          # scores: Tensor (N,), labels: (N,)
```

`mask` 参数用于忽略 padding 或无效步骤；实现用 `sum / max(mask.sum(), 1)`，不会在空 batch 上除零。`tests/test_prm.py` 同时验证了方向性（正确前缀损失更小）、$z=0$ 时等于 $\log 2$、极端值有限、梯度有限，以及 mask 后的均值等于只对保留样本求均值。

### 17.17.3 训练：指标不只看 loss

```bash
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_copy.yaml
```

`PRMTrainer` 的循环与 `RMTrainer` 同构：AdamW → 定期评估 → 保留最佳状态 → 早停 → 保存 `prm_model.npz` + `prm_config.json` + `tokenizer.json`。`configs/prm_copy.yaml` 是本节的多步示例；`configs/prm_bandit.yaml` 是下面 PPO 接入用的单动作示例。

关键配置都在 `reward.prm` 下：

| 字段 | 含义 | 常见错误 |
|---|---|---|
| `step_tokens` | 每步固定 token 数 | 训练与推理不一致，标签错位 |
| `label_mode` | `local` / `suffix` | 用 `local` 训练却用 `last` 聚合 |
| `corruptions_per_sample` | 每个样本造几个反例 | 太少导致 PRM 没见过策略会输出的错误 |
| `aggregation` | 评估与 serving 的聚合方式 | 训练/评估/PPO 三处不一致 |
| `target_accuracy` | 早停阈值 | 只看 step accuracy，忽略 traj accuracy |

评估同时报四个量：

- **step accuracy**：每个前缀的 0/1 判断是否正确；
- **Brier**：概率校准的均方误差；
- **trajectory accuracy**：按配置聚合后，整条轨迹是否判对；
- **mean score**：聚合分数的均值，用于发现“全 positive”或“全 negative”坍缩。

在 copy 任务上，100 步左右 step accuracy 约 0.93、trajectory accuracy 约 0.93；这不是“PRM 很强”，而是任务足够可验证，用来固定数据/损失/聚合的契约。

### 17.17.4 服务：边界、聚合与 scatter

`ProcessRewardManager` 的输入是 `input_ids` + `response_mask`，可选 `non_tensor_batch["step_boundaries"]`。边界是**相对 `response_mask=1` 位置计数的 end index（含）**；如果没有提供，就按 `step_tokens` 均匀切，最后一块吸收余数。注意它数的是有效 response token，不是原始列下标；observation 空洞不占边界序号，但仍会出现在前缀里。

```python
from mini_verl.prm import ProcessRewardManager

manager = ProcessRewardManager(
    "outputs/example_prm/bandit_prm",
    aggregation="min",
    step_tokens=1,
)
scored = manager.run(data)
scored.batch["rm_scores"]                       # 轨迹级聚合分数，PPO/GAE/DAPO 直接用
scored.non_tensor_batch["prm_step_scores"]      # 原始逐步概率（ragged，放 non_tensor）
```

三个不变量：

1. **最后一个边界必须等于最后一个有效 response 位置**。否则说明有 token 没被任何步骤覆盖；管理器直接报错，而不是悄悄丢掉尾部。
2. **边界必须严格递增且非负**。重复边界会产生零长度步骤，训练/推理的标签对不上。
3. **步骤分数只 scatter 到最后一个有效 response token**，与 `ModelRewardManager` 的位置契约一致。这样 GAE、in-reward KL、DAPO 过滤、独立评测都不需要改。

另外，PRM checkpoint 里保存了训练时的 `max_seq_len`、`step_tokens` 与 `aggregation`。serving 时如果配置没显式覆盖，管理器会**从 checkpoint 读取这三个值**，不会静默退回 `step_tokens=1`/`min`；如果 `input_ids` 比 `max_seq_len` 长，则直接报错而不是截断前缀。这能避免“训练/推理看到的上下文或切分不一致”这类静默错误。`reward.prm` 与 `reward.reward_model` 不能同时 `enable`，否则两个 reward manager 会互相覆盖，`PPOTrainer.init()` 会提前失败。

如果确实要做**密集过程奖励**，不应改 `rm_scores` 的语义，而应额外写 `token_level_rewards` 的逐步广播，并同步调整 GAE 的 `gamma/lam` 与归一化。第 19.12 节给出步骤级 GAE 的公式与边界。

### 17.17.5 不要用 PRM 单独做奖励：与 verifier 组合

PRM 是一个**学习出来的代理**，不是验证器。开发这个示例时，PRM-only PPO 很快会找到 PRM 打高分但任务错误的序列：例如在 copy 任务上输出重复字符。这不是实现 bug，而是 reward hacking 的标准形态。

因此 `ProcessRewardManager` 支持把 PRM 作为 shaping，与可验证奖励组合：

$$
r = w_{\text{prm}}\cdot s_{\text{prm}} + w_{\text{out}}\cdot r_{\text{verifier}}.
$$

```yaml
reward:
  prm:
    enable: true
    model_path: outputs/example_prm/bandit_prm
    aggregation: min
    step_tokens: 1
    outcome_reward_fn: bandit     # exact_match | bandit | math
    outcome_weight: 1.0
    prm_weight: 0.1
```

`outcome_reward_fn` 从 `non_tensor_batch["response_text"]` 与 `["answers"]` 读取文本；缺字段会直接报错。只写 `outcome_reward_fn` 而不给 `outcome_weight > 0` 也会报错——否则 verifier 会被静默忽略，退化成 PRM-only。`examples/26_process_reward.py` 用 bandit 验证器 + 0.1 的 PRM shaping，60 步后真实准确率到 1.0；而 PRM-only 的对照会坍缩到高分错误串。

### 17.17.6 从步骤分数到 RL 信号：三种接法

| 接法 | 做法 | 偏差/成本 |
|---|---|---|
| 轨迹奖励 | 聚合成一个标量，scatter 到最后一个 token | 最稳，兼容现有 PPO；过程信息被压缩 |
| 密集奖励 | 把 $s_k$ 广播到第 $k$ 步的 token | 信号密，但改变最优策略；非 potential-based 时有偏 |
| 步骤优势 | 在步骤级做 GAE/组基线，再广播到 token | 最贴近过程监督；需要边界、mask 与步骤级 critic |

如果采用密集奖励，至少满足 potential-based shaping 的形式：

$$
F(s,a,s')=\gamma\Phi(s')-\Phi(s),
$$

其中 $\Phi$ 是由 PRM 分数定义的势函数。否则最优策略会改变，PRM 的误差会被直接优化。步骤级 GAE 的递推、终止/截断与 mask 细节见 19.12 节；多轮 Agent 的轮级信用分配见 26.12 与 26.16 节。

### 17.17.7 边界

1. **不是语义 PRM**：线性/MLP/一层 Transformer 只能证明标签、损失、聚合、检查点与接入契约，不能证明它理解推理步骤。
2. **步骤切分是固定的**：MiniVerl 用 `step_tokens` 或显式边界；真实系统还要处理换行、工具轮次、截断与重 tokenize。
3. **没有真实 serving**：进程内打分，无 HTTP、router、多租户、GPU batching；吞吐结论不可迁移。
4. **评估口径要写清**：如果用 PRM-only 作为 reward manager 跑 `evaluate()`，pass@k 衡量的是“PRM 认为对”；即使与 verifier 组合，它也包含 shaping。真实准确率要用独立 verifier（见 24.13）。另外，`normalize=zscore/minmax` 是 **batch-dependent** 的；独立评测/跨 batch 比较应设 `normalize=none`，否则同一响应换一个 batch 分数就变了。
5. **分布漂移**：PRM 在旧策略的轨迹上训练，新策略会找到它的盲区；必须用当前策略的 rollout 周期性重训或做对抗探针。

完整链路见 `examples/26_process_reward.py`：copy 任务上训练多步 PRM → 逐步概率与六种聚合 → checkpoint 往返 → bandit 任务上 PRM shaping + verifier → PPO → 独立 verifier 测真实准确率。`tests/test_prm.py` 的 13 个用例固定了步骤样本、前缀、标签传播、稳定 BCE、mask、聚合、指标、训练收敛、检查点、边界校验、scatter、overlong shaping、verifier 组合与 PPO 接入。

## 本章小结

- 奖励是 RL 的目标函数；规则奖励可验证、模型奖励更通用但更贵。
- RewardManager 把序列级奖励放到最后一个有效 response token。
- v0.9.1 的 reward loop 把奖励计算异步化，支持限流/重试/模型奖励。
- 奖励尺度、mask、reward hacking 是最常见的问题。
- PRM 把稀疏结果奖励变成步骤信号，但步骤切分、标签（过去正确性 vs 未来可解性）与聚合方式都会引入偏差。
- GenRM/rubric 提升可解释性，也带来位置、长度与自我偏好；多目标奖励要先归一化再聚合。
- MiniVerl 用规则奖励 + 线程池 loop 演示完整链路，17.16 节实现了从偏好对到 Bradley-Terry、校准、检查点与 PPO 模型奖励的标量 RM。
- 17.17 节实现了逐步 PRM：前缀打分、稳定 BCE、六种聚合、检查点、`ProcessRewardManager`，并支持与 verifier 组合；PRM 单独做奖励会被 reward hacking。

## 延伸阅读

- 奖励函数文档：<https://verl.readthedocs.io/en/latest/preparation/reward_function.html>
- `verl/workers/reward_manager/`
- `verl/experimental/reward_loop/`
- 附录 K：过程奖励、树搜索与自我博弈
- 附录 X：奖励模型、偏好数据与 RLAIF
- 下一章：Actor 更新与 PPO loss。
