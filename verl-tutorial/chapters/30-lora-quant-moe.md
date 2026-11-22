# 第 30 章 LoRA、量化、MoE 与大模型训练

## 30.1 LoRA 的数学

对一个线性层 $W\in\mathbb{R}^{d_{out}\times d_{in}}$，LoRA 不直接更新 $W$，而是学习低秩增量：

$$
W'=W+\frac{\alpha}{r}BA,
\qquad B\in\mathbb{R}^{d_{out}\times r},\;A\in\mathbb{R}^{r\times d_{in}}.
$$

- $r$：秩，常见 8/16/32/64；
- $\alpha$：缩放，常见 `alpha=2r` 或固定 16/32；
- $A$ 随机初始化，$B$ 零初始化，使初始增量为 0；
- 只训练 $A,B$，冻结 $W$。

可训练参数：

$$
d_{out}\cdot r+r\cdot d_{in}.
$$

对 7B、r=16、所有线性层，通常是几十到几百 MB，而不是 14 GB。

## 30.2 LoRA 在 RL 中的位置

verl 的 `model.lora` 配置：

```yaml
actor_rollout_ref:
  model:
    lora:
      type: lora
      rank: 16
      alpha: 32
      dropout: 0.0
      target_modules: [linear_qkv, linear_proj, linear_fc1, linear_fc2]
      exclude_modules: []
      merge: false
      adapter_path: null
```

关键点：

- `rank=0` 表示关闭；
- `target_modules` 决定注入哪些层，且**与后端强相关**：上面示例是 Megatron 的 `linear_qkv`/`linear_proj`/`linear_fc1`/`linear_fc2`；HF/FSDP 模型通常写 `q_proj`/`k_proj`/`v_proj`/`o_proj`/`gate_proj`/`up_proj`/`down_proj`，或使用 `all-linear` 一类通配。写错名字不会报错，只会“没有注入任何层”。
- `merge=true` 在推理前把 LoRA 合并进基座权重；
- `adapter_path` 可以加载已有 adapter；
- FSDP/Megatron 都支持 LoRA，但实现细节不同。

RL 中 LoRA 的两种用法：

1. **只训练 LoRA**：显存小、可以多任务共享基座；
2. **全参 + LoRA**：少见，通常不需要。

## 30.3 LoRA 的权重同步

rollout 引擎需要看到合并后的权重：

- `merge=true`：在**发给推理引擎的副本**上把 $W+\frac{\alpha}{r}BA$ 写回，再同步；训练副本必须保留 adapter；
- `merge=false`：同步基座 + adapter，推理引擎需要支持 LoRA。

v0.9.1 的 `ActorRolloutRefWorker.update_weights` 会在需要时合并 LoRA。MiniVerl 在 `mini_verl/lora.py` 中实现了同样的两段式语义：训练副本保留 adapter，发给 rollout 的副本可以选择调用 `merge_lora_inplace`；细节见 30.12。

## 30.4 QLoRA 与量化

QLoRA = 4-bit 量化的基座 + LoRA：

- 基座用 NF4/FP4 存储；
- 计算时反量化到 bf16；
- LoRA 用 bf16。

优点：显存大幅下降；缺点：速度慢、精度损失、实现复杂。

verl 的 `qat`（quantization-aware training）配置：

```yaml
fsdp_config:
  qat:
    enable: false
    mode: w4a16
    group_size: 16
    ignore_patterns: [lm_head, embed_tokens]
```

`w4a16` 表示权重 4-bit、激活 16-bit。它用于训练时模拟量化，提升部署后的精度。

量化 RL 的难点：

- rollout 引擎与训练引擎的量化格式要一致；
- 权重同步要传量化参数/scale；
- KL/log-prob 对精度敏感；
- 量化误差可能让 reward 下降。

## 30.5 FP8 与低精度训练

FP8 训练在 H100 等硬件上可以显著提升吞吐：

- E4M3：前向权重/激活；
- E5M2：反向梯度；
- scale 管理是关键。

verl 的低精度支持取决于后端（Megatron/FSDP）与硬件。使用前要确认：

- 模型层是否支持 FP8；
- rollout 引擎是否支持；
- 权重同步是否保持精度；
- 数值稳定性（KL、softmax）。

## 30.6 MoE 基础

MoE 层：

$$
y=\sum_{i\in\text{top-}k}\operatorname{softmax}(g(x))_i\cdot E_i(x).
$$

- router $g$ 选择 top-k 专家；
- 每个专家是一个 FFN；
- 总参数量大，但每 token 只激活部分专家。

MoE 的 RL 难点：

- 专家并行（EP）与 all-to-all；
- 负载均衡（capacity factor、aux loss）；
- 路由不稳定导致训练震荡；
- 权重同步要包含所有专家；
- checkpoint 合并复杂。

verl 的 Megatron 后端支持 EP、grouped GEMM、token dispatcher、router replay。LoRA 与 MoE 的组合支持取决于后端版本与配置，使用前要在目标模型上做一次前向/反向 smoke test，确认 router 与 adapter 都被正确保存和同步。

## 30.7 Router replay

MoE 训练中，同一 token 在不同并行/不同步可能被路由到不同专家，导致训练/推理不一致。Router replay 记录训练时的路由决策，在反向/重算时复用，保证**重算与并行切分下看到同一组专家**：

- 避免激活重计算时路由改变；
- 提升可复现性；
- 支持确定性训练。

它不改变 router 的学习目标，也不“稳定”路由分布；它只是让同一次前向的决策可重放。

MiniVerl 的 `moe_route` 演示了 top-k、容量、丢弃，但没有 router replay。

## 30.8 大模型训练的显存账

以 671B MoE（约 640B 专家参数 + 31B dense/共享参数）、bf16、256 卡为例。这里必须先声明 **expert tensor parallel（ETP）**：下面先假设 ETP=TP=8，再给出 ETP=1 的对照。

```text
并行: TP=8, PP=4, DP=8, EP=8   -> 8*4*8 = 256 卡
       EP 是 DP 内部的再切分，expert-DP = DP/EP = 1

[ETP = TP = 8]
专家参数: 640B / (EP*ETP*PP) = 640B / (8*8*4) = 2.5B/卡 -> 5.0 GB/卡
dense 参数: 31B / (TP*PP)    = 31B / 32       = 0.97B/卡 -> 1.9 GB/卡
            （dense 层在 DP/EP 维上复制，不能除以 DP 或 EP）
参数小计: 3.47B -> 约 6.9 GB/卡

梯度（bf16）: 约 6.9 GB/卡
AdamW m/v（fp32）: 3.47B * 8 = 27.8 GB/卡
fp32 master（如启用）: 3.47B * 4 = 13.9 GB/卡
合计（含 master）: 约 55.5 GB/卡
```

如果 Megatron 配置里 **ETP=1**（专家权重不按 TP 切分），专家参数只按 `EP*PP` 切：

```text
专家参数: 640B / (EP*PP) = 640B / 32 = 20B/卡 -> 40.0 GB/卡
dense 参数: 31B / (TP*PP) = 0.97B/卡 -> 1.9 GB/卡
参数小计: 20.97B -> 约 41.9 GB/卡
梯度（bf16）: 41.9 GB/卡
AdamW m/v（fp32）: 20.97B * 8 = 167.8 GB/卡
fp32 master（如启用）: 20.97B * 4 = 83.9 GB/卡
合计（含 master）: 约 335.5 GB/卡
```

这已经超过单卡 80 GB，必须再切 EP/PP 或上更多卡。所以“671B 用 256 卡”是否成立，取决于 ETP 和并行组合，不能只看总参数除以卡数。

两个常见错误：

1. 用 `总参数 / (EP*TP*PP*DP)` 一刀切——dense 参数并不按 EP 切分，DP 是复制而不是切分；
2. 把“优化器状态”当成 4×参数——AdamW 的 m/v 是 8 B/参数，再加 fp32 master 才是 12 B/参数。

实际还要考虑专家负载不均、all-to-all buffer、PP 气泡、checkpoint 大小。真实预算要在目标集群上用 profile 校准。

## 30.9 选择建议

| 场景 | 建议 |
|---|---|
| 单卡/小集群 7B | LoRA/QLoRA |
| 8 卡 7B～32B | FSDP2 全参或 LoRA |
| 多机 32B～70B | FSDP2/Megatron + TP/PP |
| 235B～671B MoE | Megatron + EP/PP |
| 部署受限 | QAT/FP8 |
| 多任务共享基座 | LoRA adapter |

## 30.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| LoRA 不生效 | target_modules/rank=0 | 检查配置与权重 |
| merge 后输出变差 | 缩放/精度 | 检查 alpha/r、dtype |
| QLoRA 训练慢 | 反量化开销 | 用 bf16 LoRA、调 batch |
| MoE 丢 token 多 | capacity 太小 | 增大 capacity/aux loss |
| 专家负载不均 | router 崩溃 | aux loss、router replay |
| 权重同步漏专家 | 参数过滤 | 检查 state dict |

## 30.11 MiniVerl 对照

MiniVerl 实现了 LoRA 的训练与合并，没有实现量化与真实 MoE 训练：

- `mini_verl/lora.py`：`LoRALinear`、`apply_lora`、`freeze_base_parameters`、`merge_lora_inplace`、`adapter_state_dict`；
- `mini_verl/nn.py::Module.trainable_parameters`：区分 adapter 与冻结基座；
- `mini_verl/config.py::ModelConfig`：`lora_rank/lora_alpha/lora_dropout/lora_targets`；
- `mini_verl/parallel.py::moe_route` 演示 MoE 路由；
- `mini_verl/engine.py::estimate_memory` 演示显存估算。

`examples/17_lora.py` 展示从“初始增量为 0”到训练、冻结校验、合并后前向一致的全过程；`tests/test_lora.py` 用 10 个用例固定这些不变量。

## 30.12 MiniVerl 的 LoRA 实现

MiniVerl 的 LoRA 只有两百多行，但把真实实现里最容易错的三件事显式化了：**冻结范围、初始恒等、合并时机**。

### 包装而不是改模型

`LoRALinear` 持有一个冻结的 `Linear`，并新增两个可训练参数：

```python
class LoRALinear(Module):
    def __init__(self, base, rank, alpha, seed=0):
        self.base = base
        for p in base.parameters():
            p.requires_grad = False
        self.A = Parameter(randn(in_features, rank) / sqrt(in_features))
        self.B = Parameter(zeros(rank, out_features))
        self.scaling = alpha / rank

    def forward(self, x):
        out = self.base(x)
        if self.merged:
            return out
        return out + (x @ self.A @ self.B) * self.scaling
```

`B=0` 保证新包装的模型与原模型逐位一致。这不是“差不多”，而是 RL 的硬要求：参考模型 log-prob、rollout 首轮分布、KL 都必须从同一个起点出发。测试 `test_initial_adapter_is_identity` 直接比较包装前后的前向输出。

### 冻结整个基座，而不只是被包装的层

只冻结 `LoRALinear.base` 是不够的：embedding、LayerNorm、未被选中的线性层仍然是 `Parameter`，优化器会更新它们。`apply_lora` 在包装完成后调用 `freeze_base_parameters`，把所有非 adapter 参数设为 `requires_grad=False`。于是：

```text
可训练参数 = Σ (d_in * r + r * d_out)
```

MiniVerl 的 `Module.trainable_parameters()` 与 `num_trainable_parameters()` 让这个数字可以直接打印。示例里的 tiny 模型维度小，adapter 占比看起来有 20%～30%；真实 7B、r=16 时通常低于 1%。

### 合并是“给 rollout 的副本”，不是训练副本

`merge_lora_inplace(model)` 做两件事：

1. `W ← W + (α/r) A B`；
2. 用 `base` 替换 `LoRALinear`，模型回到普通 `Linear`。

它只应作用于发给推理引擎的副本。训练副本如果被合并，adapter 参数消失，后续梯度就无处可去。示例先保存未合并模型的 logits，再对克隆模型合并并比较，验证 `merge_matches_training_model=True`。

### 与 PPO 主循环的接口

MiniVerl 的配置是**扁平化教学 schema**（`model.lora_rank` / `lora_alpha` / `lora_targets`），与真实 verl 的嵌套 `model.lora.*` 不同；字段对照见附录 A。在 `build_policy` 中，只要 `model.lora_rank > 0` 就自动注入 adapter：

```yaml
actor_rollout_ref:
  model:
    lora_rank: 16
    lora_alpha: 32
    lora_targets: [c_attn, c_proj, fc_in, fc_out]
```

actor 与 ref 使用同一套包装，`load_state_dict` 的 key 因而一致；优化器只看到 A/B 的梯度；rollout 侧拿到的是包含 adapter 的完整 state dict。真实的 vLLM/SGLang 可以在 `merge=false` 时加载 adapter，也可以先合并再同步——这正是 `docs/advance/ppo_lora.rst` 描述的两种路径。

### 边界

MiniVerl 没有实现：adapter 的按 rank 分片、FSDP 下的 adapter all-gather、量化基座、多 adapter 热切换。这些是生产实现的主要复杂度，但契约已经在这里：**冻结什么、初始是否恒等、合并发生在哪一侧**。

## 30.13 QAT、激活卸载与 FP8 的工程细节

### QAT 在 RL 里的位置

QAT（quantization-aware training）不是“训练完再量化”，而是在训练前向/反向里插入伪量化，让模型学会适应低精度误差。verl 的 `verl/utils/qat/` 提供 quantizer/linear 包装，`modelopt` 路径负责把量化模型导出给推理引擎。

RL 的特殊之处是：**rollout 与训练必须看到同一套量化误差**。如果训练用 bf16、rollout 用 fp8，`rollout_log_probs` 与 `old_log_probs` 的差异会被误判成 off-policy，IS 权重爆炸。配置上要保证：

- 训练引擎与推理引擎的量化格式一致（w4a16/w8a8/fp8）；
- scale/zero-point 随权重一起同步；
- 权重更新后重新校准 scale，或使用动态 scale；
- KL/log-prob 的精度误差在可接受范围内（用固定 batch 对比）。

### NVFP4 QAT：4-bit 浮点与 block scale

NVFP4 是 NVIDIA 的 4-bit 浮点格式：每个数用 E2M1（1 符号位、2 指数位、1 尾数位）表示，每 16 个元素共享一个 FP8 scale，再叠一层 per-tensor 的二级 scale。它比 INT4 的优点是动态范围更接近浮点，适合权重/激活分布跨度大的 LLM；代价是 scale 管理与 kernel 支持更复杂。

v0.9.1 的 `docs/low_precision/nvfp4_qat.md` 给出的路线是 **QAT + ModelOpt 导出**，而不是训练后直接量化：

1. 训练时在 Linear 前向里插入 fake-quant（NVFP4 量化→反量化），让模型适应误差；
2. 校准/统计 scale，按 block 更新；
3. 导出成推理引擎可加载的量化 checkpoint；
4. rollout 与训练使用同一套量化格式，避免 log-prob 偏差。

RL 里做 NVFP4 要额外注意：

- **rollout 与训练的量化必须一致**。训练用 fake-quant、rollout 用 FP4 kernel，两者的 log-prob 差异会被误判成 off-policy；IS 权重可能爆炸。
- **scale 要随权重同步**。权重每步更新，旧的 scale 会让量化误差变大；同步时把 scale 作为权重的一部分，或在同步后重新校准。
- **敏感层排除**。`lm_head`、embedding、LayerNorm 通常不量化；QAT 配置里的 `ignore_patterns` 不是可选项，写错会直接掉点。
- **校准数据**。用训练分布的一小批样本做校准，不要用验证集或与任务无关的通用语料。
- **收益判断**。NVFP4 主要省显存/带宽；如果模型是 compute-bound、或推理引擎没有 FP4 kernel，收益可能为负（fake-quant 开销 + 反量化）。

适用顺序：先确认目标推理硬件/引擎支持 FP4，再在小模型上做 QAT 对照（同数据、同 seed、同评测），确认 reward/KL 可接受后再放大。不要在没有 rollout 数值对照的情况下直接上 671B。

### 激活卸载

激活显存通常是长序列 RL 的主要瓶颈。常见手段：

| 方法 | 省什么 | 代价 |
|---|---|---|
| gradient checkpointing | 激活 | 重算，+30% 计算 |
| activation offload | 激活 | PCIe 带宽，+延迟 |
| sequence parallel | 激活 | all-gather/reduce-scatter |
| 动态 batch | 激活峰值 | 调度复杂 |
| 降 micro-batch | 激活 | 吞吐下降 |

`verl/utils/activation_offload.py` 把激活在 forward 后搬到 CPU，backward 前搬回。它适合“GPU 显存紧、PCIe 还有余量”的场景；如果 PCIe 已经打满，卸载只会让 step time 更长。

### FP8 的 scale 管理

FP8（E4M3/E5M2）的关键不是 dtype，而是 scale：

- per-tensor scale：实现简单，但对 outlier 不鲁棒；
- per-channel/per-block scale：精度更好，kernel 更复杂；
- delayed scaling：用历史最大值估 scale，需要 warmup；
- dynamic scaling：每步统计，通信/同步开销。

RL 中还有额外问题：**权重每步都变**，scale 需要跟着更新；如果 scale 更新滞后，rollout 与训练的数值差异会变大。生产实现通常把 scale 作为权重同步的一部分，并在同步后做一次校准。

### 优化器卸载

`optimizer_offload=true` 把 AdamW 状态放到 CPU。它省的是显存，不是算力：

- 每步需要把 m/v 搬到 GPU（或分片搬）；
- PCIe/NVLink 带宽成为新瓶颈；
- 适合“优化器状态远大于激活”的大模型；
- 与 ZeRO-Offload/FSDP offload 的语义要对齐，避免重复卸载。

### 选择顺序

显存不够时，建议按这个顺序试：

1. 降 micro-batch / 序列长度；
2. gradient checkpointing；
3. LoRA/QLoRA；
4. sequence parallel / 动态 batch；
5. activation offload；
6. optimizer offload；
7. FP8/QAT。

越往后，对吞吐和数值稳定性的影响越大，也越难调试。每次只改一项，并记录 step time、峰值显存与 reward 曲线。

## 30.14 Muon 优化器：二阶近似、分片与适用边界

AdamW 对每个参数做逐元素归一化；Muon（MomentUm Orthogonalized by Newton-Schulz）则对**二维权重矩阵的动量**做正交化，再按谱范数缩放。它在中小模型预训练上常带来更快的收敛，但在 RL 里要考虑三件事：哪些参数适用、学习率怎么迁移、以及权重同步/checkpoint 的语义。

### 更新规则

设第 $t$ 步梯度为 $G_t$，动量

$$
M_t = \mu M_{t-1} + G_t .
$$

对二维矩阵 $M_t$ 做奇异值分解 $M_t = U S V^\top$，Muon 用

$$
O_t = U V^\top
$$

作为更新方向（只保留正交部分，抹掉奇异值），再乘学习率与缩放因子：

$$
W_{t+1} = W_t - \eta\, \alpha\, O_t .
$$

直接做 SVD 太贵，工程上用 Newton-Schulz 迭代近似 $U V^\top$：从 $X_0 = M_t / \|M_t\|_F$ 出发，迭代

$$
X_{k+1} = a X_k + b\, X_k X_k^\top X_k + c\, (X_k X_k^\top)^2 X_k,
$$

用五次多项式逼近矩阵符号函数。`muon_num_ns_steps` 控制迭代步数：步数越多越接近精确正交化，但每步都是矩阵乘。

### 哪些参数用 Muon

Muon 只适用于**二维隐藏层权重**。以下参数通常仍交给 AdamW（Megatron-Core 的默认 fallback）：

- embedding 与 lm_head；
- LayerNorm/RMSNorm 的 1D 参数；
- bias；
- 路由/门控等小参数。

注意：`Muon` 配置里的 `muon_scalar_optimizer` 字段在 v0.9.1 的 `OptimizerConfig` 中声明，但**没有代码路径读取它**；默认值是 `adam`，设成 `adamw` 是静默 no-op。不要用它来控制 fallback，fallback 是 Megatron-Core 的默认行为。

把 1D 参数硬套 Muon 没有意义：正交化针对的是矩阵的谱结构。分层策略（layer-wise）下，Megatron 的 `LayerWiseDistributedOptimizer` 会把不同层/不同参数类型分到不同优化器。

### 与 TP/PP 的交互

Muon 的正交化是**跨整个矩阵**的操作，但 TP 把矩阵切到了不同 rank。两种做法：

- 在切分前做正交化（需要 gather，通信量大）；
- 在切分后对本地块做近似（`muon_tp_mode` 控制），牺牲一点精确性换通信。

`muon_split_qkv` 控制融合的 QKV 投影是否**按 head 独立**做正交化（per-head），而不是把 Q/K/V 拆成三条独立的流。不同 head 的谱结构不同，按 head 拆开通常更合理。`muon_fp32_matmul_prec` 控制 Newton-Schulz 的矩阵乘精度：低精度会让正交化不稳定。

### 学习率迁移

从 AdamW 换到 Muon 时，学习率不能直接照搬。v0.9.1 提供 `muon_match_adamw_update_rms`：按 AdamW 的更新 RMS 推出额外缩放因子

$$
\alpha = \sqrt{\frac{1-\beta_1}{1+\beta_1}} .
$$

当 $\beta_1=0.9$ 时 $\alpha \approx 0.229416$。这个因子只匹配动量/EMA 的尺度；`muon_scale_mode` 还负责按参数形状归一化，两者正交，不能只设一个。

不设这个因子、直接沿用 AdamW 学习率时，Muon 的有效步长约是 AdamW 的 $1/0.2294 \approx 4.4$ 倍，常见症状是不稳定或不收敛。正确做法是设 `muon_match_adamw_update_rms=True`，让 verl 从 `optim.betas[0]` 推导，并在 rank 0 打印解析值；不要手写一个常数。同时设 `muon_match_adamw_update_rms=True` 和显式 `muon_extra_scale_factor` 会报错。

注意：网上常说的"Muon 学习率约 0.2×AdamW"是 $\beta_1=0.9$ 时这个因子的数值，不是可以直接套用的经验常数。换模型/换 $\beta_1$ 要重新算。

### RL 里的额外问题

- **优化器状态**：Muon 的动量、Newton-Schulz 的中间状态都要进 checkpoint；只存模型权重无法续训。
- **权重同步**：Muon 每步更新是全矩阵的，delta 同步的"变化比例"可能比 LoRA 高；要测每步变化字节。
- **数值稳定**：正交化对异常值敏感；梯度裁剪、FP32 matmul、warmup 都影响稳定性。
- **与 KL/ entropy 的交互**：更激进的更新方向可能让 KL 上升更快；监控 KL 与 clipfrac，不要只看 reward。

### 配置形状

```yaml
actor_rollout_ref:
  actor:
    optim:
      optimizer: muon
      lr: 0.02
      use_layer_wise_distributed_optimizer: true
      muon_momentum: 0.95
      muon_nesterov: false
      muon_coefficient_type: quintic
      muon_num_ns_steps: 5
      muon_scale_mode: spectral
      muon_tp_mode: blockwise
      muon_split_qkv: true
      muon_match_adamw_update_rms: true
```

字段名以安装的 Megatron-Core 为准：v0.9.1 的 `verl/utils/megatron/optimizer.py` 只转发 `OptimizerConfig` 实际声明的字段；如果安装的 Megatron 没有任何 Muon 字段，它会**直接报错**，而不是静默回退到 Adam。这是正确的选择：优化器静默回退会让实验结论完全不可比。

`use_layer_wise_distributed_optimizer=True` 的作用是构建 Megatron 的 LayerWise distributed optimizer 路径，把 Muon 的逐层 buffer 也分片；在 30B 量级，它是让 Muon 的优化器峰值显存低于 AdamW 的关键。不开这个开关，逐层 buffer 不分片，显存优势会消失。

### 什么时候不值得

- 小模型/短训练：Muon 的收益不如调学习率和数据；
- 大量 1D 参数或 LoRA：Muon 的适用面很窄；
- 需要与 AdamW 的 checkpoint 互操作：状态不兼容，迁移成本高；
- 集群通信受限：正交化的 gather/矩阵乘可能吃掉收益。

先在小规模上做 A/B，固定其他变量，比较达到同一 reward 的步数与墙钟时间，而不是只比较单步 loss。

## 本章小结

- LoRA 用低秩增量替代全参更新，显著降低显存与存储。
- QLoRA/QAT/FP8/NVFP4 进一步压缩，但对精度与实现要求更高；4-bit 路线必须让 rollout 与训练看到同一套量化误差。
- MoE 的 EP/all-to-all/容量/路由是主要工程难点。
- 大模型训练是参数、优化器、激活、KV cache 的综合预算。
- Muon 对二维权重矩阵的动量做正交化；学习率迁移、TP 切分、优化器状态与权重同步都要单独处理。
- MiniVerl 实现了 LoRA 的冻结、训练与合并；MoE 路由与显存估算覆盖概念，量化和真实 MoE 训练仍是边界。

## 延伸阅读

- LoRA：<https://arxiv.org/abs/2106.09685>
- QLoRA：<https://arxiv.org/abs/2305.14314>
- Megatron MoE 文档：<https://docs.nvidia.com/megatron-core/>
- 附录 U：模型接入与权重加载。
- 附录 V：DeepSeek V4 的量化权重同步与路由回放。
- 下一章：容错、弹性、可观测性与故障排查。
