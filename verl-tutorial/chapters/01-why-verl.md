# 第 1 章 为什么需要 verl：LLM 后训练的系统问题

## 1.1 从“会写”到“会想”：后训练的目标变了

预训练教模型预测下一个 token，监督微调（SFT）教模型模仿人类示范。两者都只要求“输出像训练数据”。推理模型、代码智能体和工具调用系统要求的是另一件事：

> 在一条完整的轨迹上，模型做出的**决策序列**是否让最终结果更好。

这条轨迹可能包含几千个思考 token、十几次工具调用、若干次失败重试。我们不再能对每个 token 给出标签，只能对最终结果给出一个可验证的分数：数学答案对不对、单元测试过不过、工具调用是否完成、代码能否编译、用户是否采纳。于是训练目标从“最小化交叉熵”变成“最大化期望回报”：

$$
\max_\theta \; \mathbb{E}_{\tau \sim \pi_\theta}\left[R(\tau)\right]
$$

其中 $\tau$ 是由策略 $\pi_\theta$ 采样出的完整轨迹，$R$ 是规则验证器、奖励模型或环境返回的标量。这个变化把 LLM 训练从“一个前向-反向传播问题”变成了“一个分布式系统问题”。

## 1.2 RL 后训练的五个系统难题

把 RLHF/RLVR 写成一个循环只需要几行伪代码：

```text
for prompt in dataset:
    responses = policy.generate(prompt)
    rewards = reward_model(responses)
    policy.update(responses, rewards)
```

但要在 8 卡、64 卡、上千卡上高效且正确地跑起来，会同时遇到五个难题。

### 难题一：生成与训练是两种完全不同的负载

生成（rollout）是自回归、访存密集、变长、带 KV cache 的推理负载；训练是前向-反向、计算密集、需要保存激活值的训练负载。它们的并行策略不同：

- 训练常用 FSDP/FSDP2，按参数和优化器状态分片；
- 生成常用 vLLM/SGLang 的 TP + continuous batching + PagedAttention；
- 训练要保留所有层，生成只需要按 TP 切分后的推理副本；
- 生成需要把权重从训练布局重排到推理布局，每轮更新一次。

如果两者各占一组 GPU，利用率可能不错，但成本高；如果共享同一组 GPU，就要在“训练时把生成引擎显存让出来”和“生成时把训练状态挪走”之间做精细调度。verl 的 3D-HybridEngine 与 V1 的 `checkpoint_engine` 就是在解决这个问题。

### 难题二：一条轨迹不是一条样本

PPO 需要同一批数据的多个版本：

- `input_ids` / `attention_mask`：模型实际看到的 token；
- `response_mask`：哪些 token 参与策略损失；
- `old_log_probs`：采样时策略的 log-prob；
- `ref_log_prob`：参考模型的 log-prob；
- `values`：critic 的价值估计；
- `advantages` / `returns`：优势与回报；
- `rollout_log_probs`：异步场景下行为策略的 log-prob；
- 多轮场景还要记录工具返回、轮次、mask。

这些字段来自不同 worker（actor、ref、critic、reward），却必须在同一条样本上对齐。verl 用 `DataProto` 把三部分打包：`batch`（张量，如 `input_ids`/`response_mask`）、`non_tensor_batch`（对象数组，如 prompt 文本、uid、answer）、`meta_info`（标量元信息，如全局 token 数、温度）。它能在 controller、Ray actor、rollout server 之间传递，并支持 chunk/concat/padding。第 9 章会展开它的 API；这里先记住：**RL 的数据契约不是“一个 tensor”，而是“可切分、可合并、带元信息的一批轨迹”**。

### 难题三：并行维度是组合爆炸

一个 671B MoE 模型可能同时使用：

- FSDP/FSDP2 的数据并行（DP）；
- Megatron 的张量并行（TP）、流水线并行（PP）、上下文并行（CP）、专家并行（EP）；
- Ulysses 序列并行（SP）；
- vLLM 的 TP 与 DP；
- 多机多卡下的 placement group、NCCL 拓扑、节点亲和。

同一份 batch 在不同 mesh 上的切分方式不同。controller 必须知道“这个方法该按哪个 mesh 切、切完怎么收”。verl 的 dispatch 模式（`DP_COMPUTE_PROTO`、`ONE_TO_ALL`、`make_nd_compute_dataproto_dispatch_fn`）就是把这层复杂性封装成声明式 RPC。

### 难题四：长尾与异步

推理模型的输出长度分布极不均匀：简单题几十个 token，难题几万个 token。同步 RL 中，一个 batch 要等最慢的样本结束；GPU 在长尾上空转。异步 RL 让 rollout 和训练重叠，但会引入 off-policy 偏差：

- 训练用的轨迹可能由旧版本策略产生；
- 旧 log-prob 与新策略不一致；
- 需要 importance sampling、staleness 阈值、partial rollout。

verl 的 `fully_async_policy`、`one_step_off_policy` 和 V1 的 `colocate_async` / `separate_async` 是这条路线上的不同折中。

### 难题五：正确性很难被测试发现

一个 RL 训练脚本“能跑”不代表“算对”。常见静默错误包括：

- response mask 错位，把 prompt token 算进损失；
- GAE 在 padding 位置 bootstrap，价值泄漏；
- GRPO 分组用了错误的 key，优势全为零；
- loss aggregation 在 DP 下没有按全局 token 数归一；
- 权重同步只同步了部分张量，rollout 仍在用旧权重；
- 异步样本没有做重要性修正，训练悄悄偏置。

这些错误不会抛异常，只会让曲线“看起来不太对”。所以本教程不仅给实现，还给**可复现的测试与对照**：MiniVerl 的 250 个测试覆盖 autograd、GAE/GRPO、dispatch、分片、rollout、checkpoint、标量 RM、过程奖励模型（PRM）、异步和端到端训练。

## 1.3 为什么是 verl

verl（Volcano Engine Reinforcement Learning for LLMs）最初由字节跳动 Seed 团队开源，2026 年 1 月迁移到 `verl-project` 组织。它对应的论文是 *HybridFlow: A Flexible and Efficient RLHF Framework*（arXiv 2409.19256，EuroSys 2025）。

verl 的核心主张不是“又一个 PPO 实现”，而是：

1. **用单控制器描述数据流。** 训练循环是一个普通 Python 程序，RL 算法写成 `generate → reward → advantage → update` 的顺序代码，而不是把控制逻辑塞进每个 worker。
2. **用多进程 worker 执行计算。** actor、critic、ref、reward、rollout 都是 Ray actor 或 SPMD 进程，各自使用最适合的并行后端。
3. **用统一协议搬运数据。** `DataProto` 让 controller 不需要知道 worker 内部用 FSDP 还是 Megatron。
4. **把训练/推理的布局转换做成可插拔组件。** 3D-HybridEngine、sharding manager、checkpoint engine 负责权重重排与传输。

这套设计让 verl 能同时支持：

- FSDP、FSDP2、Megatron-LM、Automodel、VeOmni、TorchTitan、MindSpeed；
- vLLM、SGLang、HF Transformers、TensorRT-LLM；
- PPO、GRPO、GSPO、DAPO、RLOO、REINFORCE++、ReMax、PRIME、SPPO、DPO、SFT、蒸馏；
- 文本、VLM、Omni、VLA、扩散模型（通过 verl-omni / verl-vla 等扩展）；
- NVIDIA、AMD、Ascend NPU；
- 同步、colocate async、separate async、fully async。

## 1.4 本教程的两条主线

**主线一：把 verl 讲透。** 以 v0.9.1 为基线，沿着 `DataProto → WorkerGroup → Engine → Rollout → Trainer` 的真实调用链，解释每个设计解决什么失效模式、代价是什么、什么时候会失效。所有对真实 verl 的描述都尽量落到仓库路径和可核对的行为上。

**主线二：给一个能跑的 MiniVerl。** `mini_verl/` 是一个只依赖 NumPy 的教学实现：

- 自己的反向模式自动微分与 TinyTransformer；
- `DataProto`、`LocalWorkerGroup`、dispatch 模式、资源池；
- FSDP 式梯度 all-reduce、TP/PP/MoE/Ulysses 数值模拟；
- PPO、GRPO、DAPO、GSPO、CISPO、RLOO、REINFORCE++、ReMax；
- 规则奖励、reward loop、标量奖励模型（Bradley-Terry/校准/检查点/接入 PPO）、多轮工具调用、异步 replay buffer、checkpoint 与 safetensors 导出；
- 独立评测入口（从 checkpoint 重建配置、按题分组算 pass@k）；
- 250 个测试、26 个示例、端到端 bandit 训练。

MiniVerl 不是 verl 的替代品，也不是“玩具”。它的目标是把 verl 中**不可见的契约**变成可运行、可断点、可验证的代码。读完后再看真实 verl，你应该能回答：

- 这个字段从哪来、到哪去？
- 这个 dispatch 模式切的是哪一维？
- 这个 loss 在 DP 下怎么归一？
- 权重同步搬了多少字节？
- 异步样本的 staleness 如何影响梯度？

## 1.5 阅读路线

- 想先跑起来：第 6 章 → 第 34～39 章（MiniVerl）→ 回读第 2～5 章。
- 想做算法：第 2、3、18～26 章，以及附录 K（PRM/树搜索/自博弈）。
- 想做系统/性能：第 4、9～17、22、31～33 章，以及附录 J（部署与集群）、附录 Z（引擎调度与容量规划）。
- 想做奖励/偏好：第 17、28 章，以及附录 X（RM/RLAIF）、第 17.16 节（MiniVerl RM）、第 17.17 节（MiniVerl PRM）。
- 想做评测：第 24 章与附录 L（基准、污染、统计显著性）。
- 想做平台/商业：第 41～45 章与附录 J。
- 想扩展后端/做安全：附录 M（引擎后端）、附录 N（安全、沙箱与多租户）。
- 想接工具/做 Agent：第 26 章与附录 T（工具生命周期、FunctionTool、ToolRegistry、沙箱）。
- 想接新模型/做量化：第 12、14、16、30 章与附录 U（模型接入/权重加载）、附录 V（DeepSeek V4、FP8/FP4、路由回放）。
- 想做分离部署/弹性：第 15、27 章与附录 W（DetachActorWorker、checkpoint engine、成本模型）。
- 想二次开发：附录 P（自定义 reward/AgentLoop/dataset/model/rollout/engine/algorithm 与测试/CI/贡献）。
- 想用高级特性：附录 Q（attention、RoPE、MTP、dynamic CP、FSDP Turbo、Megatron Lite、skip manager、determinism、可观测性）。
- 想交付项目：附录 R（30/60/90 天节奏、数据/奖励/评测、实验 manifest、复盘、上线与回滚）。
- 想看完整方案：附录 O 的三个端到端案例（数学 RLVR、代码 Agent、多模态 RL）。
- 想核对实现：先读附录 G（MiniVerl 代码导读），再按章节中的文件路径跳转。

## 1.6 符号、术语与阅读约定

全书公式与代码字段的对应关系如下。读后面章节时若忘记某个符号，可以回到这张表。

| 符号 | 含义 | 对应字段/代码 |
|---|---|---|
| $x$ | prompt | `input_ids` 的 prompt 部分 |
| $y=(y_1,\dots,y_T)$ | response | `input_ids` 的 response 部分 |
| $\tau$ | 一条完整轨迹 | 一行 `DataProto`（多轮时含 observation） |
| $\pi_\theta$ | 当前策略（actor） | `actor` 引擎 |
| $\pi_{\theta_{\text{old}}}$ | 采样时的行为策略 | `old_log_probs` |
| $\pi_{\text{ref}}$ | 参考策略 | `ref_log_prob` |
| $r_t$ | token 级奖励 | `token_level_rewards` |
| $R(\tau)$ | 轨迹级奖励 | `rm_scores` 或 reward 标量 |
| $V_\phi(s_t)$ | critic 的价值估计 | `values` |
| $A_t$ | 优势 | `advantages` |
| $\hat R_t$ | 回报 | `returns` |
| $\rho_t$ | 重要性比率 | `exp(log_prob - old_log_prob)` |
| $\epsilon$ | PPO 裁剪范围 | `clip_ratio` / `clip_ratio_low/high` |
| $\beta$ | KL 系数 | `kl_loss_coef` / `kl_ctrl.kl_coef` |
| $\gamma,\lambda$ | 折扣与 GAE 参数 | `algorithm.gamma` / `algorithm.lam` |
| $m_t$ | response mask | `response_mask` |

三条命名约定：

1. **“序列”与“prompt”不是一回事。** `train_batch_size` 通常是 prompt 数；乘以 `rollout.n` 之后才是序列数。本书在容易混淆处会显式写“prompt 数/序列数”。
2. **“全局”与“每卡”要看清。** `ppo_mini_batch_size` 常是全局序列数，`ppo_micro_batch_size_per_gpu` 才是每卡；loss 的 token-mean 必须用全局 token 数归一。
3. **“能跑”与“算对”是两件事。** 书中反复出现“静默错误”，指的是不抛异常但改变优化目标的实现偏差；每章末尾的小结会指出该章最容易被忽略的那个。

章节引用约定：`第 X 章` 指整章，`第 X.Y 节` 指小节，`§X.Y` 是同一引用的简写；`附录 <字母>` 指附录。代码路径一律用反引号标出，`verl/...` 是 v0.9.1 上游路径，`mini_verl/...` 是本仓库实现。

## 本章小结

- LLM 后训练的核心矛盾，是“轨迹级奖励”与“token 级训练”之间的系统落差。
- verl 用单控制器表达 RL 数据流，用多进程 worker 执行异构计算，用 `DataProto` 统一数据契约。
- 真正的难点是权重同步、并行维度、长尾异步与静默正确性，而不是 PPO 公式本身。
- 本教程以 verl v0.9.1 为事实基线，并用一个可运行的 MiniVerl 把关键契约显式化。

## 延伸阅读

- HybridFlow 论文：<https://arxiv.org/abs/2409.19256>
- verl 仓库：<https://github.com/verl-project/verl>
- verl 文档：<https://verl.readthedocs.io/>
- 下一章：RL 后训练的数学基础，从策略梯度推到 GAE 与 PPO。
