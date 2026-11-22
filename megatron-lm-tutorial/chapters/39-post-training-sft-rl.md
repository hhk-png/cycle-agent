# 第 39 章 后训练：SFT、LoRA、偏好优化与 RL

## 39.1 后训练不是“预训练的小号版本”

预训练学习“预测下一个 token”，后训练学习“按人类偏好/任务要求行动”。两者在系统上的差异：

| 维度 | 预训练 | 后训练 |
|---|---|---|
| 数据 | 海量无标注 token | prompt/response、偏好对、reward |
| loss | 全序列 next-token CE | response-only CE、pairwise、policy gradient |
| batch | 很大 | 较小、长序列、动态 |
| 并行 | TP/PP/DP/CP/EP | 常以 TP/DP 为主，推理与训练交替 |
| 瓶颈 | GPU 算力/通信 | rollout、数据、评估、权重同步 |
| 迭代 | 数周–数月 | 数小时–数天，频繁实验 |

Megatron-LM 仓库的 `post_training/`、Megatron Bridge 的 recipes、NeMo 平台都覆盖后训练，但企业落地通常还要接自己的数据/评测/部署。

## 39.2 SFT：监督微调

### 39.2.1 目标

给定 prompt `x` 和 response `y`，最大化：

```text
L_SFT = - Σ_{t ∈ response} log p(y_t | x, y_<t)
```

只对 response token 计算 loss；prompt token 的 label 设为 `ignore_index`。

### 39.2.2 Chat template

训练和推理必须使用同一个 template：

```text
<|system|>你是助手<|end|>
<|user|>问题<|end|>
<|assistant|>回答<|end|>
```

常见错误：

- 训练时加了特殊 token，推理时没加；
- role 标记的 id 不一致；
- response 的起始位置算错，loss 包含了 prompt；
- 多轮对话的边界处理错误。

### 39.2.3 Loss mask 的实现

```python
from mini_megatron.parallel.cross_entropy import vocab_parallel_cross_entropy
# logits: [tokens, vocab]（或 [batch, seq, vocab] 展平）；labels 同 token 数
labels = input_ids.clone()
# labels: [batch, seq] 时按列 mask；若已展平成 [tokens]，则用 response_mask
if labels.dim() == 2:
    labels[:, :prompt_len] = -100       # 不计算 prompt（每个样本的前 prompt_len）
else:
    labels[response_mask == 0] = -100   # packing/展平：只保留 response token
labels[input_ids == pad_id] = -100      # 不计算 padding
loss = vocab_parallel_cross_entropy(logits, labels)
```

在多轮/多样本 packing 时，还要：

- 每条样本的 response 区间都要 mask；
- 跨样本的 token 不能互相 attention；
- position id 按样本 reset 或使用全局位置。

### 39.2.4 SFT 的超参

- LR 比预训练低 1–2 个数量级；
- epoch 1–3，过多会过拟合/遗忘；
- batch 按 token 数而不是样本数；
- warmup 短；
- 序列长度覆盖真实任务分布；
- 评估看任务指标，不只看 loss。

## 39.3 LoRA 与 QLoRA

### 39.3.1 低秩适配

对冻结的权重 `W ∈ R^{d×k}`，学习低秩增量：

```text
W' = W + (α/r) · B A
A ∈ R^{r×k}, B ∈ R^{d×r}, r << min(d,k)
```

前向：

```text
y = x W^T + (α/r) · x A^T B^T
```

- `r`：秩（8/16/32/64）；
- `α`：缩放；
- 初始化：`A` 随机，`B=0`，保证初始增量为 0；
- 只训练 `A/B`，优化器状态大幅减少。

### 39.3.2 目标模块

常见选择：

- attention 的 q/k/v/o；
- MLP 的 gate/up/down；
- 全部线性层（效果更好，参数更多）。

不同模型的模块命名不同，转换/加载时要按名字映射。

### 39.3.3 QLoRA

把冻结的主干量化到 4-bit（NF4），LoRA 用 bf16：

- 显存大幅下降；
- 需要 dequant + matmul 的 kernel；
- 量化误差可能影响效果；
- 合并回主干时要处理量化/反量化。

### 39.3.4 合并与多适配器

推理时可以把 LoRA 合并进主干：

```text
W' = W + (α/r) B A
```

优点：无额外延迟；缺点：失去多适配器切换能力。

多租户场景通常保留 adapter，按请求路由；需要管理：

- adapter 版本；
- 基座模型版本；
- 合并/回滚；
- 权限/隔离。

## 39.4 偏好优化

### 39.4.1 RLHF 的三段式

经典 RLHF：

```text
1. SFT：得到初始策略 π_SFT
2. Reward model：用偏好对训练 r(x,y)
3. RL：最大化 E[r(x,y)] - β KL(π || π_SFT)
```

PPO 的目标：

```text
L_PPO = - E[ min(ρ A, clip(ρ, 1-ε, 1+ε) A) ] + β KL
ρ = π_θ(y|x) / π_old(y|x)
```

工程上需要四个模型/角色：

- policy（训练）；
- reference（冻结，算 KL）；
- reward（冻结）；
- critic（可选，算 advantage）。

显存/调度复杂，rollout 与训练交替，是“训练系统 + 推理系统”的组合。

### 39.4.2 DPO

DPO 把偏好学习转成分类损失，不需要显式 reward model/RL：

```text
L_DPO = - log σ( β [ log π(y_w|x)/π_ref(y_w|x) - log π(y_l|x)/π_ref(y_l|x) ] )
```

其中 `y_w` 是 chosen，`y_l` 是 rejected。直觉：提高 chosen 相对 reference 的概率，降低 rejected。

优点：

- 简单、稳定、只需偏好对；
- 不需要 rollout/reward model；
- 训练像 SFT 一样。

缺点：

- 依赖偏好数据质量；
- 对分布外样本可能过拟合；
- 不能像 RL 那样探索；
- `β` 需要调。

### 39.4.3 IPO / KTO / ORPO

- **IPO**：把 DPO 的 sigmoid 换成平方损失，缓解过拟合；
- **KTO**：只需要“好/坏”标签，不需要成对；
- **ORPO**：把 SFT 和偏好优化合并到一个损失。

选择取决于数据形态和稳定性要求，不要把某篇论文的配方直接套到另一个模型。

### 39.4.4 GRPO

GRPO（Group Relative Policy Optimization）用一组采样结果的相对奖励代替 critic：

```text
对每个 prompt 采样 G 个回答
r_i = reward(y_i)
A_i = (r_i - mean(r)) / std(r)
L = - E[ min(ρ A_i, clip(ρ) A_i) ] + β KL
```

优点：

- 不需要 critic；
- 适合可验证奖励（数学/代码）；
- 与推理引擎结合紧密。

难点：

- rollout 成本高；
- 组内奖励方差小/全相同时没有梯度；
- 奖励黑客/长度偏置；
- 训练/推理权重同步。

## 39.5 RL 的系统架构

一个 RL 训练步通常包含：

```text
1. 从 prompt 数据集采样 batch
2. rollout：policy 生成回答（推理引擎）
3. reward：规则/模型打分
4. 计算 advantage/return
5. 训练：policy 更新（Megatron）
6. 同步权重到推理引擎
```

### 39.5.1 Colocated vs Disaggregated

| 架构 | 特点 | 优点 | 缺点 |
|---|---|---|---|
| Colocated | 训练/推理共享 GPU | 权重同步快、成本低 | 显存/调度冲突 |
| Disaggregated | 训练/推理分开 | 各自优化、弹性 | 权重传输/延迟 |

权重同步方式：

- NCCL broadcast；
- 共享内存/文件；
- 推理引擎的 reload API；
- KV cache 不参与同步。

### 39.5.2 Rollout 引擎

常见选择：

- vLLM/SGLang：高吞吐、PagedAttention、连续批处理；
- TensorRT-LLM：NVIDIA 硬件优化；
- Megatron Core inference：与训练栈同源，便于权重/并行对齐。

关键指标：

- rollout tokens/s；
- 训练/推理 GPU 占比；
- 权重同步时间；
- 长尾样本延迟；
- 奖励计算开销。

### 39.5.3 并行策略

后训练通常：

- TP 用于大模型单机推理/训练；
- PP 较少用（rollout 气泡）；
- DP 用于吞吐；
- EP 用于 MoE 推理/训练；
- 长序列用 CP/SP。

SFT/LoRA 的显存需求低于预训练，但 rollout 的 KV cache 可能成为新瓶颈。

### 39.5.4 异步、off-policy 与多轮工具 RL

同步 RL 的每一步都要“生成完 → 更新 → 再生成”，GPU 在 rollout 和训练之间
来回切换，利用率低。生产系统常用三条路：

| 路线 | 做法 | 风险 |
|---|---|---|
| 异步 rollout | 推理引擎持续生成，训练侧消费稍旧的样本 | 样本 off-policy；需要重要性修正/裁剪 |
| 部分 rollout | 长回答分块生成，训练先消费已完成部分 | 序列/奖励的边界语义复杂 |
| 多轮/工具 | 模型调用工具、环境、沙箱，按轨迹给奖励 | 环境延迟/失败、奖励稀疏、状态管理 |

多轮/工具 RL 的系统难点不在 loss，而在：

- **环境隔离**：每个 rollout 需要沙箱/容器，启动/回收成本高；
- **轨迹存储**：prompt + 多轮 observation + action + reward，数据量远大于
  单轮；
- **部分可观测**：同一轨迹的中间步骤如何分配 advantage；
- **奖励设计**：规则/执行结果/PRM（process reward model）各有偏差；
- **超时与取消**：长尾轨迹不能拖住整个 batch。

**权重同步的带宽账**：一次同步要把全部参数从训练 rank 送到推理 rank。
70B bf16 约 140 GB；若跨节点有效带宽为 400 GB/s（示例假设；实际 IB/RoCE
常见 50–400 GB/s），单次约 0.35 秒，看似不多，但
RL 每几十步就同步一次，且要和 rollout/训练争带宽。MoE 模型还要处理专家
布局差异（训练 EP 布局 → 推理 EP/TP 布局），通常需要重排/转换，不能直接
`broadcast`。因此“训练和推理用同一套并行布局”是 colocated 架构的最大
优势，也是它显存紧张的原因。

### 39.5.5 框架版图

RL 训练框架通常不是从零写，而是在“训练后端 + 推理后端 + 编排”之间组合：

| 框架 | 定位 | 与 Megatron 的关系 |
|---|---|---|
| verl | 开源 RL 框架，强调 rollout/training 解耦与多种推理后端 | 可用 Megatron 作为训练后端（版本/接口需对齐） |
| NeMo-RL | NVIDIA 的 RL 后训练栈 | 与 NeMo/Megatron Core 生态结合 |
| SkyRL | 面向长程/多轮 agent 的 RL 框架 | 训练后端可接 Megatron/FSDP |
| OpenRLHF | 轻量、易读的 RLHF 实现 | 常用 DeepSpeed/FSDP，也可接 Megatron |

选型时不要只看“支持 GRPO/DPO”，要问：

1. 训练后端是否支持你的模型结构（MoE/MLA/长序列）与并行组合；
2. rollout 引擎能否加载同格式权重，同步是否是增量/分片；
3. 是否支持多轮/工具/异步，超时与失败如何处理；
4. 数据/轨迹/奖励的 schema 是否可扩展；
5. 评测与可复现是否和预训练同一套。

后训练的“能跑”很容易，“稳定地产出可评测的提升”很难。先固定一个小模型、
小数据、单轮任务，把 rollout→reward→update→eval 的闭环和数值对齐跑通，
再扩展到多轮/大模型。

## 39.6 蒸馏

蒸馏把教师模型的能力迁移到学生：

```text
L_KD = KL( softmax(z_T / T) || softmax(z_S / T) )
```

变体：

- response distillation：只学教师生成的文本；
- logit distillation：学完整分布；
- on-policy distillation：学生自己生成，教师打分；
- feature distillation：中间层对齐。

工程问题：

- 教师 logits 的存储/传输；
- 词表不一致；
- 温度/权重；
- 教师/学生的并行布局不同。

## 39.7 Megatron / Bridge / NeMo 中的后训练

公开资料中，后训练能力分布在：

- **Megatron-LM `post_training/`**：SFT、LoRA、偏好优化、RL 相关脚本；
- **Megatron Bridge**：HF ↔ Megatron 转换、SFT/LoRA recipes；
- **NeMo**：企业级训练/评测/部署/Guardrails；
- **Megatron Core inference**：rollout 所需的推理原语。

使用时的检查清单：

1. 基座模型与 tokenizer 版本；
2. chat template；
3. loss mask/response 边界；
4. 并行配置与 checkpoint 兼容；
5. 推理引擎的权重格式；
6. 评测口径；
7. 许可证/数据合规。

不要假设“预训练能跑的配置，后训练也能跑”：后训练的序列更长、batch 更小、推理/训练交替，瓶颈完全不同。

## 39.8 后训练的评估

- SFT：任务准确率、格式遵循、拒答率、长度分布；
- 偏好优化：win rate、reward 分数、KL、长度偏置；
- RL：pass@k、reward、样本效率、训练稳定性；
- 安全：有害内容、越狱、PII 泄露；
- 回归：通用能力是否退化（灾难性遗忘）。

评估要固定：

- prompt 集；
- 解码参数（temperature/top-p/max tokens）；
- 评测脚本/版本；
- 随机种子（如适用）。

## 39.9 mini 与后训练

mini 没有实现 SFT/LoRA/RL，但可以复用：

- `vocab_parallel_cross_entropy` 的 `ignore_index` 做 response-only loss；
- `TransformerStage` 的 TP/PP/CP 做 SFT 训练；
- `generate` 做简单的 rollout；
- `checkpoint` 做 adapter/模型保存。

如果要扩展 mini：

1. 加 response loss mask（最简单）；
2. 加 LoRA 层（冻结主干，注入低秩分支）；
3. 加 DPO loss（需要 reference 模型/冻结副本）；
4. 加 rollout/reward 循环（需要推理引擎）。

每一步都要先写数值测试，再谈性能。

## 39.10 本章小结

- 后训练的目标/数据/系统与预训练不同，不能直接套用预训练配方。
- SFT 的关键是 chat template 与 response-only loss mask。
- LoRA 用低秩增量降显存；QLoRA 进一步量化主干。
- DPO 简单稳定，RLHF/GRPO 更灵活但系统复杂。
- RL 的瓶颈在 rollout、reward、权重同步，不只是训练 kernel。
- 后训练评估要固定 prompt/解码/脚本，关注遗忘与安全。

下一章进入推理系统：KV cache、连续批处理与 PD 分离。
