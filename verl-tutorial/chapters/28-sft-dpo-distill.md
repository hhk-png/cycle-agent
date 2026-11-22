# 第 28 章 蒸馏、DPO、SFT 与偏好优化

## 28.1 四种训练目标

| 目标 | 数据 | 损失 | 是否需要 rollout |
|---|---|---|---|
| SFT | (prompt, response) | 交叉熵 | 否 |
| DPO | (prompt, chosen, rejected) | Bradley-Terry 对数似然 | 否 |
| 蒸馏 | (prompt, teacher logits) | KL | 可能需要生成 |
| PPO/GRPO | (prompt, reward) | 策略梯度 | 是 |

它们共享同一套 Engine/Worker 抽象，但数据流与 loss 不同。

## 28.2 SFT

SFT 最小化：

$$
\mathcal{L}_{\text{SFT}}=-\frac{1}{\sum m_t}\sum_t m_t\log\pi_\theta(y_t\mid x,y_{<t}),
$$

其中 $m_t$ 是 response mask。

verl 的 SFT trainer 在 `verl/trainer/sft_trainer.py`（或 recipe 中），支持：

- FSDP/Megatron；
- 多轮 SFT；
- 多模态；
- LoRA；
- 动态 batch。

配置要点：

```yaml
# SFT 使用 verl/trainer/config/sft_trainer_engine.yaml 的顶层配置树，
# 与 PPO 的 actor_rollout_ref 不同：model/engine/optim/data 都在顶层。
model:
  path: Qwen/Qwen3-8B
engine:
  strategy: fsdp
optim:
  lr: 1e-5
data:
  train_files: ...
  train_batch_size: 256
  micro_batch_size_per_gpu: 4
  max_length: 4096
  use_dynamic_bsz: true
```

SFT 的常见问题：

- response mask 错位；
- 学习率太大导致灾难性遗忘；
- 数据质量差；
- 长序列 OOM。

## 28.3 DPO

DPO 直接用偏好对：

$$
\mathcal{L}_{\text{DPO}}
=-\log\sigma\left(\beta\left[
\log\frac{\pi_\theta(y_w\mid x)}{\pi_{\text{ref}}(y_w\mid x)}
-\log\frac{\pi_\theta(y_l\mid x)}{\pi_{\text{ref}}(y_l\mid x)}
\right]\right).
$$

其中 $y_w$ 是 chosen，$y_l$ 是 rejected。

优点：

- 不需要 reward model；
- 不需要 rollout；
- 训练稳定。

缺点：

- 需要偏好数据；
- 对数据质量敏感；
- 可能过度优化 chosen/rejected 的差异。

verl 的 DPO 通常在 recipe 中实现；MiniVerl 的 `DPOTrainer` 用同一套 actor/ref worker，`dpo_loss_fn` 计算序列 log-prob 差。

## 28.4 蒸馏

在线蒸馏：

$$
\mathcal{L}_{\text{distill}}
=\mathrm{KL}\left(\pi_{\text{teacher}}(\cdot\mid s)\Vert\pi_{\text{student}}(\cdot\mid s)\right).
$$

离线蒸馏：

- 用 teacher 生成数据，再 SFT；
- 用 teacher 的 logits 做 token-level 监督。

verl 的 `distillation` 配置：

```yaml
distillation:
  enabled: true
  teacher_key: data_source        # 用样本的哪个字段选择 teacher
  teacher_models:                # dict，不是 list；键名是任意 teacher 名
    teacher_a:
      key: math                  # 对应 teacher_key 的取值
      model_path: Qwen/Qwen3-32B
      num_replicas: 1
    teacher_b:
      key: code
      model_path: Qwen/Qwen3-Coder-30B
  distillation_loss:
    loss_mode: k3                # k1 / k3 / forward_kl_topk ...
    topk: 32
    distillation_loss_coef: 1.0
  n_gpus_per_node: 8
  nnodes: 0
```

它支持多 teacher（按 `teacher_key` 路由）、teacher server、多种 loss（`loss_mode`）与 top-k logprob 蒸馏。蒸馏常与 PPO 组合：

$$
\mathcal{L}=\mathcal{L}_{\text{PPO}}+\lambda\mathcal{L}_{\text{distill}}.
$$

## 28.5 偏好优化的变体

- **IPO**：用平方损失替代 log-sigmoid，缓解过拟合；
- **KTO**：只需要“好/坏”标签，不需要成对；
- **ORPO**：把 SFT 与偏好优化合并；
- **SimPO**：去掉 ref 模型，用长度归一化；
- **SPPO**：自博弈迭代。

verl 主库提供 SFT 与蒸馏；DPO 及 IPO/KTO/ORPO/SimPO 等偏好变体主要通过 recipe 或扩展实现。

偏好数据的来源、成对/并列/K 选一的格式、Bradley-Terry 的数值稳定实现，以及“用 AI 反馈代替人工偏好”的 RLAIF/Constitutional AI 流程，见附录 X。如果偏好数据质量不过关，换哪个 DPO 变体都不会有本质差别。

## 28.6 冷启动与课程

典型推理模型训练流程：

```text
预训练 -> SFT 冷启动 -> RLVR（GRPO/DAPO）-> 拒绝采样/蒸馏 -> 再 SFT
```

每一步的作用：

- SFT：教会格式与基本推理；
- RL：用可验证奖励提升正确率；
- 拒绝采样：把成功轨迹变成新 SFT 数据；
- 蒸馏：把大模型能力迁移到小模型。

verl 支持这条流水线的每一段，但需要自己串联脚本。

## 28.7 数据与 mask

SFT/DPO 的 mask 同样重要：

- SFT 只对 response 计算损失；
- DPO 分别对 chosen/rejected 计算 log-prob；
- 多轮 SFT 要对每轮 assistant token 计算损失；
- 工具 observation 不参与。

MiniVerl 的 `sft_loss_fn` 用 `response_mask`，并按全局 token 数归一（DP 下由 worker 传入 `global_num_tokens`，梯度 all-reduce 后等价于全局 token-mean）；`dpo_loss_fn` 用 chosen/rejected mask，并用 `loss_mask` 排除 padding 复制出来的整对样本。

## 28.8 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| SFT loss 不降 | lr/mask/数据 | 检查 mask、降 lr |
| DPO margin 不涨 | β 太小/数据质量 | 调 β、清洗数据 |
| 蒸馏 loss 为 0 | teacher logits 未对齐 | 检查 vocab/temperature |
| 灾难性遗忘 | SFT lr 太大/数据单一 | 混入通用数据、降 lr |
| 显存不够 | 序列太长 | 动态 batch、重计算、LoRA |

## 28.9 MiniVerl 对照

- `mini_verl/trainer/sft_trainer.py`
- `mini_verl/trainer/dpo_trainer.py`
- `mini_verl/losses.py::sft_loss_fn`
- `mini_verl/losses.py::dpo_loss_fn`
- `mini_verl/losses.py::distillation_loss_fn`

`examples/11_sft_and_dpo.py` 演示了 SFT 与 DPO 的完整训练循环。

## 28.10 Teacher Loop 与 on-policy 蒸馏

离线蒸馏把 teacher 的输出写成数据集；on-policy 蒸馏则让学生先采样，再让 teacher 对**学生实际走到的状态**打分。后者更贵，但能修正学生自己的错误分布，常用于推理模型的小模型化。

### 系统结构

verl 的 `verl/experimental/teacher_loop/` 提供：

- `TeacherManager`：管理一个或多个 teacher 模型，按 `data_source`/key 路由；
- `TeacherModel`：封装 teacher server 或本地模型，负责生成 logits/log-prob；
- 与 `distillation` 配置和 `trainer/distillation/losses.py` 配合。

典型调用链：

```text
student rollout ──► batch (input_ids, response_mask)
                       │
                       ├─► TeacherManager.route(data_source)
                       │       └─► teacher server: logits for response tokens
                       │
                       └─► distillation loss = KL(teacher || student)
```

### 三种蒸馏信号

| 信号 | 传输量 | 信息量 | 适用 |
|---|---|---|---|
| 标量 reward | 每样本 1 个数 | 最低 | 结果监督 |
| top-k logits | k × vocab 的一部分 | 中 | 通信受限 |
| 完整 logits | vocab × seq | 最高 | 小词表/同 tokenizer |

完整 logits 的传输量是：

$$
\text{bytes}=B\times T_{\text{response}}\times V\times \text{bytes\_per\_elem}.
$$

例：B=8、T=512、V=150k、fp16 → 约 1.2 GB/step。这也是为什么生产实现常用 top-k、log-prob 或“teacher 侧直接算 loss”的方式。

### Loss 与温度

标准 KD 使用温度 $T$：

$$
p_i^{\text{teacher}}=\operatorname{softmax}(z_i^{\text{teacher}}/T),\qquad
\mathcal{L}=T^2\cdot\mathrm{KL}(p^{\text{teacher}}\Vert p^{\text{student}}).
$$

$T^2$ 的作用是让梯度尺度不随温度漂移。MiniVerl 的 `distillation_loss_fn` 做了温度缩放，但没有乘 $T^2$，也没有做 logits shift 对齐——它假设调用方已经对齐了 token 位置。这是一个明确的教学简化。

### 对齐清单

蒸馏最常见的静默错误是“loss 很小但模型没学会”：

- teacher/student 的 tokenizer 与 chat template 必须一致；
- teacher logits 的第 $t$ 个位置对应 student 的第 $t$ 个 token，不能错位；
- response mask 要同时作用于 teacher 与 student；
- 温度、top-k、dtype 要记录在配置里；
- teacher 的 stop token 不能混进 loss；
- 多 teacher 时，路由 key 必须与样本 `data_source` 对齐。

### 与 PPO 组合

on-policy 蒸馏常写成：

$$
\mathcal{L}=\mathcal{L}_{\text{PPO}}+\lambda\mathcal{L}_{\text{distill}}.
$$

注意两项的归一化口径要一致：PPO 按 token/sequence 归一，蒸馏也应按同一 mask 与全局 token 数归一；否则 $\lambda$ 的最优值会随 batch 形状漂移。

### MiniVerl 的边界

MiniVerl 只实现了 `distillation_loss_fn`，没有 teacher server、没有多 teacher 路由，也没有把蒸馏接进 PPO 主循环。它适合验证“forward KL + 温度 + mask”的数值，不适合验证分布式 teacher 的吞吐。真实的 `TeacherManager` 还要处理 teacher 副本、请求批处理、超时与重试。

## 28.11 蒸馏的变体与选择

“蒸馏”不是一种损失，而是一组设计选择：

| 维度 | 选项 | 特点 | 适用 |
|---|---|---|---|
| 方向 | forward KL $\mathrm{KL}(p_t\Vert p_s)$ | 学生覆盖 teacher 的所有模式，均值寻求 | 通用、稳定 |
| 方向 | reverse KL $\mathrm{KL}(p_s\Vert p_t)$ | 学生集中在 teacher 的高概率模式，模式寻求 | 生成质量/低延迟 |
| 方向 | JS/混合 | 折中，需调权重 | 研究/蒸馏+R L |
| 粒度 | token-level | 每个 token 一个监督，信号密 | 同 tokenizer、白盒 |
| 粒度 | sequence-level | 整条序列的 log-prob/排序 | 黑盒、无 logits |
| 访问 | 白盒 logits | 信息最多，传输最贵 | 同机构/同 tokenizer |
| 访问 | top-k logits | 折中 | 带宽受限 |
| 访问 | 黑盒文本 | 只能做 SFT/拒绝采样 | API teacher |
| teacher | 单 teacher | 简单 | 单任务 |
| teacher | 多 teacher | 按 `data_source` 路由/加权 | 多任务/能力互补 |

三个容易忽略的工程问题：

1. **词表不一致**：teacher 与 student 的 tokenizer 不同，logits 不能逐位置对齐。要么映射词表（交集/字节级），要么只用文本做序列级蒸馏。
2. **序列级蒸馏的 mask**：只对 response token 计分；teacher 的 stop token、chat template 的特殊 token 不能混进 loss。
3. **多 teacher 权重**：按任务路由（hard routing）简单；按样本加权（soft routing）要防止某一 teacher 主导。权重应可记录、可消融。

一个最小选择流程：

```text
同 tokenizer + 有 logits -> token-level forward KL（默认）
带宽受限 -> top-k logits
黑盒 API -> 拒绝采样/SFT
要生成质量/低延迟 -> 试 reverse KL 或 GKD 类方法
多任务 -> 多 teacher + hard routing，先别做 soft routing
```

MiniVerl 的 `distillation_loss_fn` 是 token-level forward KL + 温度 + mask；它不覆盖 reverse KL、序列级、黑盒或多 teacher，这些需要扩展 worker/loss。

## 本章小结

- SFT/DPO/蒸馏/RL 是四种不同目标，共享 Engine/Worker 基础设施。
- DPO 不需要 reward model/rollout，但对偏好数据质量敏感。
- 蒸馏用 teacher logits，常与 PPO 组合。
- 推理模型的典型流水线是 SFT → RL → 拒绝采样/蒸馏。
- MiniVerl 用同一套 worker 演示 SFT/DPO/蒸馏损失。

## 延伸阅读

- DPO 论文：<https://arxiv.org/abs/2305.18290>
- `verl/trainer/sft_trainer.py`
- `verl-recipe` 中的 DPO/SFT 示例
- 附录 X：奖励模型、偏好数据与 RLAIF
- 下一章：多模态、VLM、Omni 与扩散模型。
