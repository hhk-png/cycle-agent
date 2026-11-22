# 附录 X 奖励模型、偏好数据与 RLAIF 工程

第 17 章把奖励分成规则、模型与环境三类，但只用一节讲了奖励模型（RM）。本附录补上那条链路：**偏好数据怎么来、Bradley-Terry 怎么落到 loss、RM 怎么训练与校准、RLAIF 怎么在 verl 里变成奖励服务**。它回答的是一个很具体的问题：

> 当任务没有可程序验证的答案（写作、对话、代码风格、安全、主观质量）时，RL 的奖励从哪里来，怎么保证它不是一个会被人或模型轻易骗过的分数？

先给结论：RM 不是“训练一个打分器”这么简单，它是一个**数据契约 + 概率模型 + 校准与监控**的组合。三者任何一环缺失，最终都会表现为“reward 涨、人工评估跌”。

## X.1 什么时候需要奖励模型

| 奖励来源 | 适用条件 | 失败模式 |
|---|---|---|
| 规则验证器（RLVR） | 有唯一/可判定的答案：数学、单测、结构化抽取 | 抽取规则漏洞、测试泄漏、只覆盖简单题 |
| 奖励模型（RM） | 主观质量、开放式生成、安全/风格、无标准答案 | reward hacking、分布漂移、长度/位置偏差 |
| 环境奖励 | 游戏、仿真、真实系统反馈 | 奖励稀疏、环境不可复现、延迟高 |
| 人类在环 | 小规模高质量、探索阶段 | 成本高、吞吐低、标注不一致 |

选择的顺序应该是：**能验证就不要用模型**。RLVR 的信号虽然稀疏，但不会“学歪”；RM 的信号密集，但它本身是一个会被优化的近似目标。只有当“好”无法写成规则时，RM 才值得引入。

一个常见的混合结构：

```text
总奖励 = 正确性（规则 0/1）
       + 格式（规则，小权重）
       + 质量/风格（RM，小权重）
       - 长度/成本（规则惩罚）
       - 安全（分类器或规则，硬约束）
```

关键不是把所有权重调平，而是把**硬约束与软偏好分开**：正确性应该是门槛，质量分只在前者满足时起作用。否则模型会学会“写得漂亮但答错”。

## X.2 偏好数据的契约

### X.2.1 一条偏好样本包含什么

最小字段：

```text
prompt             # 输入，必须与策略训练时的 chat template 一致
chosen             # 被偏好的回复
rejected           # 较差的回复
data_source        # 任务/领域，用于路由与分桶
annotator_id       # 标注者或生成该对的模型
label_confidence   # 可选：置信度/多数票比例
metadata           # 难度、长度、语言、安全标签
```

如果要做**成对（pairwise）**训练，一条样本就是一个三元组 $(x, y_w, y_l)$。如果要支持**并列（tie）**或 **K 选一**，数据格式要能表达：

- tie：`chosen == rejected` 或单独的 `tie` 标志；
- K-way：`responses: [y_1..y_K]` + `ranking` 或 `scores`；
- 多标注者：同一对多条记录，训练时聚合或按标注者加权。

### X.2.2 数据从哪来

1. **人工标注**：质量最高，成本最高；适合定义“什么算好”的种子集。
2. **强模型生成 + 人工校验**：用更强模型产生候选，人只做选择/修正。
3. **AI 反馈（RLAIF）**：用 judge 模型或规则生成偏好；成本低，但有自我偏好与位置偏差。
4. **策略在环（on-policy）**：用当前策略采样多条回复，再由人或模型排序。它最贴近训练分布，但需要持续重标。
5. **规则蒸馏**：用可验证的分数构造 chosen/rejected（如正确 vs 错误），把稀疏信号转成偏好对。

### X.2.3 数据质量的五个检查

| 检查 | 方法 | 不做的后果 |
|---|---|---|
| 标注一致性 | 同一批样本重复标注，算 Cohen's κ / 一致率 | 噪声上限低于模型能力，RM 学不动 |
| 长度偏差 | 统计 chosen/rejected 的长度差 | RM 学会“越长越好” |
| 位置偏差 | 交换 chosen/rejected 顺序重训/重评 | 评估虚高，线上退化 |
| 去重与污染 | prompt/response 近似去重、与评测集比对 | 训练集泄漏，指标不可信 |
| 分布覆盖 | 按任务/难度/语言分桶统计 | 长尾任务被多数任务淹没 |

一个经验阈值：如果人工标注的成对一致率低于约 70%，先别训练 RM，先修标注指南或降低任务主观性。RM 的上限由标签一致性决定，模型容量不是瓶颈。

### X.2.4 on-policy 还是离线

离线偏好数据（如公开的 HelpSteer、UltraFeedback 类数据）便宜，但与当前策略的分布有差距。RL 训练会把策略推离离线分布，RM 在 new policy 的输出上可能完全没校准。

on-policy 数据的优势是“RM 见到的就是策略会生成的”；代价是每轮都要重新采样与标注。实践中的折中：

```text
第 1 轮：离线数据预训练 RM
第 2 轮：用当前策略采样，规则/AI 初筛，人工只标争议样本
第 3 轮：把 RM 的边界样本（分数接近、模型分歧大）优先送标
```

这也是一种主动学习：**把标注预算花在 RM 最不确定的样本上**，而不是随机采样。

## X.3 Bradley-Terry 与损失函数

### X.3.1 成对偏好模型

Bradley-Terry（BT）假设每条回复有一个隐含的“效用” $r(x,y)$，偏好概率为：

$$
P(y_w \succ y_l \mid x)
=\sigma\big(r(x,y_w)-r(x,y_l)\big),
\qquad
\sigma(z)=\frac{1}{1+e^{-z}}.
$$

给定数据集 $\mathcal{D}=\{(x_i,y_{w,i},y_{l,i})\}$，负对数似然：

$$
\mathcal{L}_{\text{BT}}
=-\frac{1}{N}\sum_{i=1}^{N}
\log\sigma\big(r(x_i,y_{w,i})-r(x_i,y_{l,i})\big).
$$

它只约束**差值**，不约束绝对尺度：给所有 $r$ 加一个常数，loss 不变。这点很重要——RM 的绝对分数没有意义，**同一 prompt 内的相对排序才有意义**。把 RM 分数直接当奖励做跨样本归一化时，必须按 prompt/组做，而不是全 batch 做。

### X.3.2 数值稳定写法

直接算 $\log\sigma(z)$ 在 $z$ 很负时会下溢。稳定写法：

$$
\log\sigma(z)=-\log(1+e^{-z})=\operatorname{softplus}(-z).
$$

在 MiniVerl 的 NumPy autograd 里没有现成的 `log_sigmoid`，可以用：

```python
# z = r_chosen - r_rejected
# loss = mean(softplus(-z))，softplus 用 log(1+exp(-z)) 的稳定分支
loss = np.where(z > 0, np.log1p(np.exp(-z)), -z + np.log1p(np.exp(z))).mean()
```

在 PyTorch 里直接用 `F.logsigmoid(z)` 或 `F.softplus(-z)`，不要手写 `log(sigmoid(z))`。

### X.3.3 变体

| 目标 | 公式/做法 | 适用 |
|---|---|---|
| BT pairwise | $-\log\sigma(r_w-r_l)$ | 标准成对偏好 |
| Margin BT | $-\log\sigma(r_w-r_l-m)$ | 希望“明显更好”才得分 |
| Tie-aware | 对 tie 用 $-\log\sigma(0)$ 的对称项，或三项 softmax | 允许“差不多” |
| Plackett-Luce | $-\log\frac{e^{r_1}}{\sum_j e^{r_j}}$ | K 选一/完整排序 |
| Regression | $(r(x,y)-s)^2$ | 有人类/规则给出的绝对分数 |
| Listwise | 对一组回复做 softmax 交叉熵 | 一次比较多个候选 |

**不要**把 BT 的分数解释为概率或“正确率”。它只是一个排序势函数。需要概率时，用校准（X.6）或对同一 prompt 的候选做 softmax。

### X.3.4 一个手算例子

假设 RM 给 chosen 打 1.2，给 rejected 打 0.4：

$$
z=1.2-0.4=0.8,\qquad
\sigma(0.8)\approx 0.690,\qquad
-\log 0.690\approx 0.371.
$$

如果交换两条回复，$z=-0.8$，loss 变为 $-\log\sigma(-0.8)\approx 1.171$。所以“把顺序弄反”不是符号问题，而是把一条高置信样本变成高损失样本。训练前必须确认 chosen/rejected 的方向。

## X.4 奖励模型的结构

### X.4.1 标量头

最常见的结构：

```text
tokens -> backbone (Transformer) -> hidden states
       -> 取最后一个有效 token 的 hidden
       -> Linear(dim, 1)
       -> scalar reward
```

三个实现细节：

1. **取哪个位置**：最后一个非 padding token 是标准做法；对长回复也可以做 mean pooling，但会稀释关键句。对话/多轮任务更适合取最后一个 assistant token。
2. **padding 方向**：左 padding 时“最后一个 token”是回复末尾；右 padding 时要按 `attention_mask` 找真正的末尾。搞反会把 padding 的表征当奖励。
3. **chat template**：RM 的 template 必须与策略训练时一致，否则 RM 看到的输入分布与训练分布不同。verl 的 `data.return_raw_input_ids` 就是为“RM 与策略 template 不同”准备的开关（见 X.7）。

### X.4.2 判别式 RM 与生成式 RM

| 维度 | 判别式 RM | 生成式 RM（GenRM / LLM-as-judge） |
|---|---|---|
| 输出 | 一个标量/logit | 解释 + 分数/选择 |
| 训练 | BT/回归，端到端 | SFT/偏好对齐，或直接 prompting |
| 成本 | 一次前向 | 一次生成，可能很长 |
| 可解释性 | 低 | 高 |
| 偏差 | 数据偏差、长度偏差 | 位置、长度、措辞、自我偏好 |
| 适合 | 大规模 RL 奖励 | 小规模高质量评估、rubric、冷启动 |

生成式 RM 的关键不是“让模型说好话”，而是**把判断变成可解析的输出**：固定 rubric、固定输出格式、固定解析器。解析失败要记为 0 并留痕，不能让异常中断训练（第 17.15 节）。

### X.4.3 过程奖励模型（PRM）

PRM 给每一步打分，结构上是“同一个 backbone + 每步一个标量头”或“每步一次前向”。它的训练数据比 ORM 更难获得：

- 人工步骤标注（如 PRM800K 风格）；
- 蒙特卡洛 rollout 的成功率作为步骤标签；
- 强模型/规则做步骤验证。

PRM 的工程难点在**步骤边界**：训练时按什么切、推理时按什么切、tokenizer 是否一致。边界错位会让标签整体偏移。详见第 17.12 节与附录 K；可运行实现见第 17.17 节（`mini_verl/prm.py`）。

## X.5 训练工程

### X.5.1 数据管线

一个可复现的 RM 训练管线至少要有：

```text
原始偏好数据
  -> 规范化（统一 template、截断、去重）
  -> 划分 train/val/test（按 prompt 分组，避免同题泄漏）
  -> 构造 (chosen, rejected) 张量
  -> 打包/分桶（按长度）
  -> 训练
  -> 评估（pairwise accuracy / 校准 / 偏差）
  -> 版本化（数据指纹 + 代码指纹 + checkpoint）
```

划分要**按 prompt 分组**：同一道题的不同回复不能同时出现在 train 和 val，否则评估会虚高。

### X.5.2 训练超参的经验区间

| 超参 | 常见起点 | 说明 |
|---|---|---|
| 学习率 | 1e-5 ~ 5e-5（全参） | 比 SFT 小；RM 容易过拟合 |
| epoch | 1 ~ 3 | 看 val pairwise accuracy 早停 |
| batch | 32 ~ 256 对 | 受显存限制；梯度累积可用 |
| max length | 覆盖 95% 样本 | 截断会改变偏好方向 |
| warmup | 3% ~ 5% | 稳定早期 |
| weight decay | 0.01 ~ 0.1 | 抑制过拟合 |

RM 的过拟合信号不是 train loss，而是**val pairwise accuracy 与 val margin 同时下降**。如果 train loss 降、val accuracy 平，说明模型在记数据而不是学偏好。

### X.5.3 顺序对称与位置偏差

训练时可以把每对样本做两次前向：`(chosen, rejected)` 与 `(rejected, chosen)`，共享梯度或取平均。这能减少“第一个回复更容易被偏好”的位置偏差。评估时同样要交换顺序各评一次，报告平均值。

### X.5.4 长度与风格混杂

如果 chosen 系统性更长，RM 会学到“长 = 好”。三种处理：

1. **数据层**：按长度匹配（length-matched）构造偏好对；
2. **模型层**：在 loss 中加入长度惩罚或对长度做对抗去偏；
3. **评估层**：报告按长度分桶的 pairwise accuracy，确认不是只在长样本上赢。

三者中数据层最有效，模型层最容易引入新偏差。

### X.5.5 校准

BT 只保证排序，不保证分数可解释。若要把 RM 分数用于阈值/过滤/多目标加权，需要校准：

- **温度缩放**：$p=\sigma(z/T)$，在验证集上拟合 $T$；
- **分位数映射**：把分数映射到验证集分位，减少尺度漂移；
- **ECE（Expected Calibration Error）**：把预测概率分桶，比较置信度与准确率。

校准集必须与训练集按 prompt 分离，且要覆盖不同任务/长度。

## X.6 评估：不要只看 pairwise accuracy

### X.6.1 四个层次的评估

| 层次 | 指标 | 回答的问题 |
|---|---|---|
| 排序 | pairwise accuracy、margin | 能不能分对 chosen/rejected |
| 校准 | ECE、Brier、可靠性曲线 | 分数能不能当概率/阈值 |
| 偏差 | 长度/位置/领域分桶准确率 | 是不是靠捷径赢 |
| 下游 | best-of-n 的相关性、RL 后的真实指标 | 作为奖励是否有用 |

**pairwise accuracy 高不等于奖励好用**。一个 RM 可以在静态数据上 75% 准确，但在策略的新输出上把“更长、更自信、格式更漂亮”的回复排前面。最终要看：用它做 best-of-n 或 RL，真实指标是否提升。

### X.6.2 奖励过优化（reward overoptimization）

Gao 等人的经验结论是：随着对 RM 的优化强度增加，代理奖励（RM 分数）持续上升，但真实奖励先升后降。原因不是 RM“坏”，而是**优化会把策略推到 RM 训练分布的盲区**。

监控方法：

- 固定一组 held-out prompt，定期用**独立 judge/人工**评估；
- 同时记录 `RM score` 与 `true score`，画两条曲线；
- 当二者背离时，停止或降低 RM 权重，而不是继续调大学习率。

这也是为什么第 17 章反复强调“验证集与奖励函数解耦”。

### X.6.3 对抗性探针

准备一小批“专门骗 RM”的样本：

- 正确但格式朴素 vs 错误但格式华丽；
- 短而准确 vs 长而重复；
- 拒绝回答 vs 安全但无用；
- 复制 prompt vs 真正回答。

如果 RM 在这些探针上系统性选错，说明它学到了捷径。探针不需要多，20～50 条就能暴露大部分问题。

### X.6.4 PRM 的评估

PRM 不是 ORM 的“更细版本”，评估也不能只沿用 pairwise accuracy。至少分开看：step accuracy/Brier（单步判断与校准）、trajectory accuracy（按 serving 聚合后的判断）、首错定位、聚合敏感性（`last/min/mean/product` 换一种是否改变排序）、对抗探针（高分错误串），以及下游 best-of-n/RL 的真实提升。标签来源（人工/蒙特卡洛/规则）要分层报告，因为 $y_k^{	ext{past}}$ 与 $y_k^{	ext{future}}$ 的偏差不同。完整清单见附录 K.9，MiniVerl 实现见 17.17。

## X.7 在 verl 中接入奖励模型

### X.7.1 配置面

v0.9.1 的奖励配置在 `verl/trainer/config/reward/reward.yaml`，数据类在 `verl/workers/config/reward.py`：

```yaml
reward:
  num_workers: 8

  reward_manager:
    source: register          # register | importlib
    name: naive               # naive / dapo / gdpo / rate_limited / remote

  reward_model:
    enable: true
    enable_resource_pool: false   # true: 独立资源池
    n_gpus_per_node: 8
    nnodes: 0
    model_path: /path/to/rm
    rollout:
      name: vllm              # 复用 rollout 引擎加载 RM
      tensor_model_parallel_size: 2
      dtype: bfloat16
      gpu_memory_utilization: 0.5
      enforce_eager: true
      free_cache_engine: true
      max_model_len: 4096
      response_length: 4096

  sandbox_fusion:
    url: null
    max_concurrent: 64
    memory_limit_mb: 1024
```

几个容易误解的点：

- `reward_model.enable=true` **不会训练 RM**，它只是把 RM 作为推理服务启动，供 reward loop 调用。verl 不是 RM 训练框架；RM 通常在 TRL/OpenRLHF/自研管线里训练，再以 HF 权重形式接入。
- `reward_model.rollout` 是一套完整的 `RolloutConfig`，所以 TP/EP、chunked prefill、prefix caching 等字段都能配置；但能否生效取决于引擎的 RM 路径（disrm 的 `/classify` 不一定吃这些生成优化）。RM 的吞吐直接决定 reward 是否成为训练瓶颈。
- `enable_resource_pool=true` 时，RM 使用独立的 `n_gpus_per_node`/`nnodes`，不与 actor 抢显存；代价是多一组资源与一次跨池通信。
- `data.return_raw_input_ids=true` 用于“RM 的 chat template 与策略不同”的场景；否则 `input_ids` 会被策略 template 改写，RM 看到的输入与训练时不一致。

### X.7.2 代码路径

```text
controller
  -> reward loop manager (verl/experimental/reward_loop/)
     -> reward_model.py        # 启动 RM rollout 副本；取分在 reward_loop.py
     -> router/                # naive / inner_sglang
     -> reward_manager/*.py    # 把标量 scatter 到 token
  -> DataProto.batch["rm_scores"] / ["token_level_rewards"]
```

`verl/experimental/reward_loop/reward_model.py` 负责把 RM 起成 rollout 副本；真正决定“怎么取分”的是 `verl/experimental/reward_loop/reward_loop.py`：

- **判别式 RM（disrm）**：vLLM 走 `/classify`，取最后一个 `probs` 的最后一维；SGLang 走 `/v1/embeddings`，取最后一个 `embedding` 的最后一维；TensorRT-LLM 当前直接报错（`raise ValueError`，注释表明待 TorchSampler）。
- **生成式 RM（genrm）**：reward loop **不会**自动解析生成结果；必须提供 `custom_reward_function.path`，由用户函数发请求/解析。没有自定义函数时会直接报错（源码注释：`genrm must set custom_reward_function`）。
- **确定性**：`full_determinism=true` 且走 `/classify` 时，reward loop 会把 `max_num_seqs` 强制为 1；生成式路径由用户函数自行保证（per-request seed 等）。

对应测试在 `tests/experimental/reward_loop/test_reward_model_disrm.py` 与 `test_reward_model_genrm.py`（上游仓库）。这两个测试的存在不代表“配一个 `model_path` 就能跑 GenRM”——GenRM 的解析逻辑在你的 reward function 里。

### X.7.3 自定义 RM 的三种接法

| 接法 | 做法 | 适用 |
|---|---|---|
| 内置 reward_model | 配 `model_path`，走 reward loop | 标准 HF 标量 RM |
| 自定义 reward function | `custom_reward_function.path/name`，函数内部调用 HTTP/本地模型 | 非标准模型、需要预处理 |
| 独立服务 | 自己起 FastAPI/Triton，函数只做请求 | 多租户、跨语言、独立扩缩容 |

如果 RM 是远程服务，请求契约应沿用第 17.15 节：`request_id` 幂等、`data_source` 路由、超时/重试、失败计 0 并记录。**不要让 RM 服务的异常静默变成“奖励 0”而不留痕**，否则基础设施故障会被误判为模型变差。

### X.7.4 成本与瓶颈

RM 的成本是“每个样本一次前向/生成”。在 PPO 的一个 step 里，rollout 生成 $N$ 条轨迹，RM 要对每条打分：

$$
T_{\text{reward}} \approx \frac{N\cdot L_{\text{resp}}\cdot C_{\text{RM}}}{H_{\text{RM}}},
$$

其中 $C_{\text{RM}}$ 是每 token 的计算量，$H_{\text{RM}}$ 是 RM 的吞吐。若 $T_{\text{reward}}$ 与 $T_{\text{rollout}}$ 同量级，RM 就会把同步训练拖成串行。

优化顺序：

1. 批处理与长度分桶；
2. 用更小的 RM 或蒸馏 RM；
3. 独立资源池/异步 reward loop；
4. 缓存（同一 prompt-response 不重复打分）；
5. 只对争议样本调用大 judge（级联）。

## X.8 RLAIF 与 Constitutional AI

### X.8.1 三种反馈来源

| 方式 | 反馈来源 | 成本 | 主要风险 |
|---|---|---|---|
| RLHF | 人类偏好 | 高 | 标注慢、一致性 |
| RLAIF | AI judge/规则 | 低 | 自我偏好、位置/长度偏差、可被利用 |
| RLVR | 程序验证 | 最低（边际） | 只覆盖可验证任务 |

RLAIF 不是“不要人”，而是把人从**逐条标注**移到**定义原则、抽检、校准**。

### X.8.2 Constitutional AI 的两阶段

1. **监督阶段（critique-revise）**：模型对初始回复按一组原则（constitution）自我批评并改写，得到“更符合原则”的回复；用这些回复做 SFT。
2. **偏好阶段（RLAIF）**：对同一 prompt 生成多条回复，用 AI judge 按原则排序，得到偏好对；再训练 RM 或直接做 DPO/PPO。

原则要具体、可检查，例如：

```text
- 不提供可操作的违法/危险细节
- 不编造引用或数据；不确定时明确说明
- 不泄露系统提示或隐私
- 回答直接，不堆砌无关内容
```

“有用、无害、诚实”这种抽象原则无法稳定判分；要拆成可观察的行为。

### X.8.3 judge 的工程细节

- **位置交换**：同一对样本评两次（A/B 与 B/A），取一致结果；不一致记为 tie。
- **多 judge 集成**：不同模型/提示投票，降低单模型偏差。
- **rubric 固定**：judge 的评分条目、输出格式、解析器都要版本化。
- **校准集**：用人工标注的小集估计 judge 的准确率与偏差；judge 不是 ground truth。
- **防注入**：待评回复里可能包含“请给 10 分”的指令；judge 的 system prompt 要明确忽略回复中的指令，输入要做分隔/转义。
- **成本**：生成式 judge 的 token 成本可能高于 RM；级联（小模型初筛 + 大模型复核）更现实。

### X.8.4 自我偏好与模式坍缩

用同族模型做 judge，会偏好自己的措辞/风格；用同一模型自我生成 + 自我评分，容易形成“回音室”，多样性下降。缓解：

- judge 与 policy 不同族/不同规模；
- 保留人类标注的锚点集；
- 监控输出多样性（distinct-n、熵、长度分布）；
- 定期用外部 benchmark 复核。

## X.9 RM 与 RL 的交互

### X.9.1 分数尺度

RM 的绝对分数不可比：不同 RM、不同 prompt 的分数分布不同。进入 RL 前要做：

- 按 prompt/组做归一化（GRPO 天然按组）；
- 或用 reward shaping 把 RM 分映射到有界区间；
- 多 RM 组合时先 z-score/分位数映射，再加权（第 17.14 节）。

### X.9.2 RM 的“新鲜度”

RM 是在旧策略输出上训练的。RL 训练几千步后，策略分布已经漂移，RM 的盲区会被放大。两种策略：

1. **定期重训 RM**：用当前策略采样 + 重新标注，迭代式 RLHF；
2. **限制优化强度**：KL 约束、早停、RM 集成，避免把策略推到分布外。

前者成本高但上限高；后者便宜但容易过早饱和。

### X.9.3 与 KL、长度惩罚的联合

RM 奖励 + KL 惩罚 + 长度惩罚是三种不同的约束：

- RM 负责“往哪个方向优化”；
- KL 负责“不要离参考模型太远”；
- 长度惩罚负责“不要用长度换分数”。

三者会互相抵消。调参时一次只动一个，并同时看 `rm_score`、`kl`、`response_length`、`true_eval` 四条曲线。只盯 `rm_score` 几乎一定会过优化。

### X.9.4 安全与红队

如果 RM 包含安全维度，必须把安全做成**硬约束/过滤器**，而不是一个可被质量分抵消的软分数。常见做法：

- 生成后过安全分类器，命中则奖励置负/丢弃；
- 训练奖励中安全项权重足够大，且在验证集上单独监控；
- 红队集定期回归，防止“安全分涨、越狱成功率也涨”。

## X.10 常见失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| pairwise accuracy 高，RL 后真实指标不涨 | 静态评估不代表新策略分布 | 用 on-policy 数据重评，加对抗探针 |
| RM 分数随长度单调上升 | 长度混杂 | 长度匹配数据、长度分桶评估 |
| 交换顺序后判断翻转 | 位置偏差 | 对称训练/评估、多 judge |
| train loss 降、val accuracy 平 | 过拟合/数据泄漏 | 按 prompt 分组划分、早停、正则 |
| RM 分数分布随训练漂移 | 策略分布漂移 | 定期重训/校准、KL 约束 |
| 生成式 judge 解析失败率高 | 输出格式不稳定 | 固定 rubric/格式、失败记 0 并告警 |
| 多 RM 加权后某一项主导 | 量纲不同 | 先归一化再加权 |
| 安全分与有用性互相抵消 | 软约束 | 安全做成硬门槛/过滤器 |

## X.11 MiniVerl 的最小 RM：已经落地，边界也写清楚

MiniVerl 现在有三层奖励：

- **规则奖励 + reward loop**（`mini_verl/rewards.py`）：并发、超时、失败计数；
- **标量 RM**（`mini_verl/reward_model.py`）：偏好数据 → Bradley-Terry → 校准 → 检查点 → PPO 奖励；
- **PRM**（`mini_verl/prm.py`）：步骤标签 → 前缀打分 → 聚合 → PPO 奖励，可与 verifier 组合。

第二层是本附录方法的可运行版本；第三层把逐步监督也落成了可测试模块（见 17.17、附录 K）。它不假装自己是生产 RM，但把最容易写错的契约都固定成了测试：

| 契约 | MiniVerl 实现 | 对应测试 |
|---|---|---|
| 偏好方向 | chosen 来自可验证答案，rejected 是受控负例 | `test_preference_pairs_and_batches_pad_without_scoring_padding` |
| 数值稳定 | `log_sigmoid = -softplus(-z)`，分支避免溢出 | `test_stable_ops_match_reference`、`test_bradley_terry_loss_direction_and_stability` |
| 梯度 | 手写反向 + 有限差分 | `test_stable_ops_gradients`、`test_softplus_gradient_is_sigmoid_everywhere` |
| mask | 只有 `attention_mask=1` 的位置参与 pooling | `test_pooling_respects_attention_mask` |
| margin/label smoothing | 进入 BT loss，不改变数据方向 | `test_bradley_terry_margin_and_label_smoothing` |
| 校准 | pairwise accuracy / margin / ECE | `test_pairwise_metrics_and_calibration` |
| 检查点 | 权重 + 架构元数据 + tokenizer + 指标 | `test_rm_checkpoint_roundtrip_is_identical` |
| 接入 PPO | 标量 scatter 到最后一个 response token | `test_model_reward_manager_scatters_to_last_response_token` |
| 评测分离 | `without_shaping()` 去掉 overlong 惩罚 | `test_model_reward_manager_without_shaping` |
| 早失败 | `enable=true` 但缺 `model_path` | `test_ppo_missing_reward_model_path_fails_early` |

### X.11.1 三种 backbone 各自的用途

- **linear + mean pooling**：验证“线性模型能学到什么、学不到什么”。它对 token 贡献是可加的，无法表达“同一个回答在 A 上下文好、在 B 上下文坏”的交互；
- **mlp + mean pooling**：MiniVerl 的默认。pooled 表示过一层 GELU 后可以表达非线性区域，CPU 上足够快；
- **transformer + last pooling**：与生产 RM 的形状一致（causal trunk 的最后一个有效 token），用来对照“位置选择”与 mask 的影响。

这不是“三个模型选一个最强”，而是把**结构假设**显式化：RM 的分数是 backbone、pooling、mask 三者共同定义的，换任何一个都要重新评估。

### X.11.2 负例构造的诚实边界

`build_preference_pairs` 的负例是规则构造的：bandit 换一个动作，其他任务改第一个字母/数字。它满足两个条件：

1. 与正例共享大部分 token（hard negative，不能靠长度或格式区分）；
2. 方向由可验证答案保证（不会把正确答案标成 rejected）。

但它**不满足**真实偏好数据的条件：没有标注者、没有 tie、没有多标注者一致性、没有位置偏差。因此：

- pairwise accuracy 只能说明“方向学对了”，不能说明“人类也这么选”；
- ECE 只能说明“分数在当前合成分布上校准”，不能外推到开放域；
- 不能拿 MiniVerl 的 RM 分数直接做生产阈值。

这正好对应 X.2.3 的数据检查：合成数据可以验证管线，不能替代标注质量。

### X.11.3 从 MiniVerl 到生产 RM 还差什么

| 维度 | MiniVerl | 生产 |
|---|---|---|
| backbone | 线性/MLP/一层 Transformer | 预训练 7B～70B + 标量头 |
| 数据 | 规则构造的合成对 | 人工/AI/on-policy 偏好，按 prompt 分组划分 |
| 并行 | 单进程 NumPy | FSDP/Megatron + reward worker |
| 服务 | 进程内 `ModelRewardManager` | reward loop + router + 限流/超时 |
| 校准 | ECE + 固定验证集 | 温度缩放、分位数映射、分桶偏差 |
| 监控 | accuracy/margin/ECE | 代理奖励 vs 真实指标、对抗探针、长度分桶 |
| 安全 | 无 | 硬约束、红队、注入防护 |

接入真实 verl 时，MiniVerl 的经验仍然成立：**`reward_model.enable` 是推理开关，不是训练开关；RM 的绝对分数不可跨样本比较；权重/数据/评测必须版本化。**

## X.12 检查清单

**数据**

- [ ] chosen/rejected 方向正确，且有抽检。
- [ ] 按 prompt 分组划分 train/val/test。
- [ ] 统计长度差、位置偏差、任务覆盖。
- [ ] 与评测集去重，记录数据指纹。

**训练**

- [ ] 使用稳定的 `log_sigmoid/softplus`。
- [ ] 验证集 early stop，不只看 train loss。
- [ ] 顺序对称训练/评估。
- [ ] 保存数据/代码/超参指纹。

**评估**

- [ ] pairwise accuracy + margin。
- [ ] 校准（ECE/温度缩放）。
- [ ] 长度/领域分桶。
- [ ] 对抗探针与 best-of-n 相关性。
- [ ] 独立 judge/人工复核。

**接入 verl**

- [ ] `reward_model.enable` 与 `model_path` 正确。
- [ ] chat template 与策略一致，或显式 `return_raw_input_ids`。
- [ ] RM 的吞吐/显存不成为同步瓶颈。
- [ ] 超时/重试/失败计 0 有指标。
- [ ] 同时监控 `rm_score`、`kl`、长度、真实评测。

## 本章小结

- RM 适用于“好”无法写成规则的任务；能验证时优先 RLVR。
- 偏好数据的质量上限由标注一致性决定；按 prompt 分组、去重、长度匹配是底线。
- Bradley-Terry 只约束分数差，绝对分数不可跨样本比较；loss 要用稳定的 log-sigmoid/softplus。
- RM 训练的关键是评估：pairwise accuracy、校准、偏差分桶、下游 best-of-n/RL 相关性。
- verl 不训练 RM，只通过 reward loop 服务 RM；`reward_model.enable` 是推理开关，不是训练开关。
- MiniVerl 在 X.11 与第 17.16 节实现了一个最小标量 RM：它固定数据/损失/mask/校准/检查点/PPO 接入契约，但不是语义 RM。
- RLAIF/Constitutional AI 把人从逐条标注移到原则、抽检与校准；judge 的位置/长度/自我偏好必须工程化处理。
- RM 与 RL 的交互要同时监控代理奖励与真实指标，防止奖励过优化。

## 延伸阅读

- InstructGPT（RLHF 流程）：<https://arxiv.org/abs/2203.02155>
- Constitutional AI：<https://arxiv.org/abs/2212.08073>
- RLAIF：<https://arxiv.org/abs/2309.00267>
- Scaling Laws for Reward Model Overoptimization：<https://arxiv.org/abs/2210.10760>
- Let's Verify Step by Step（PRM）：<https://arxiv.org/abs/2305.20050>
- 第 17 章：奖励、reward loop 与 PRM
- 第 28 章：DPO、SFT 与偏好优化
- 附录 K：过程奖励、树搜索与自我博弈
- 附录 L：评测与基准
- 附录 E：论文与文档清单
