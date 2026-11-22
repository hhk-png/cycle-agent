# 第 12 章 Megatron 引擎：TP/PP/CP/EP 与大模型训练

## 12.1 什么时候需要 Megatron

FSDP 适合“**完整训练状态**（参数 + 梯度 + 优化器状态 + 激活）或目标 batch 放不进单卡”的场景；即使模型权重本身能塞进单卡，训练状态也往往放不下。当单个 transformer 层/attention 在单卡上都难以放下，或需要极致的计算/通信重叠时，才需要 Megatron-LM 的多维并行：

- **TP（Tensor Parallel）**：把单个矩阵乘切到多卡；
- **PP（Pipeline Parallel）**：把不同层放到不同卡；
- **CP（Context Parallel）**：把序列维切到多卡；
- **EP（Expert Parallel）**：把 MoE 专家分到多卡；
- **DP（Data Parallel）**：复制模型、切分数据；
- **SP（Sequence Parallel）**：LayerNorm/Dropout 等按序列切分，常与 TP 一起用。

verl 的 Megatron 后端是 235B/671B MoE 训练的主力。官方文档建议：

- 研究/原型：FSDP/FSDP2；
- 追求规模化：Megatron-LM；
- v0.9.1 文档基线 Megatron-LM v0.13.1；uv lock 中 megatron-core 为 core_v0.18.0，megatron-bridge 0.5.2。

## 12.2 列并行与行并行

一个线性层 $Y=XW$，$W\in\mathbb{R}^{d_{in}\times d_{out}}$。

### Column Parallel

把 $W$ 按输出维切：

$$
W=[W_1,\dots,W_t],\qquad Y_i=XW_i,\qquad Y=[Y_1,\dots,Y_t].
$$

每个 rank 得到输出的一个切片，通常不需要立即 all-gather；如果下一层是 Row Parallel，可以直接把切片传下去。

### Row Parallel

把 $W$ 按输入维切：

$$
W=\begin{bmatrix}W_1\\ \vdots\\ W_t\end{bmatrix},\qquad
Y=\sum_i X_iW_i.
$$

每个 rank 得到部分和，需要 all-reduce 求和。

### MLP 的标准组合

```text
X -> ColumnParallel(fc1) -> GELU -> RowParallel(fc2) -> Y
```

fc1 的输出切片正好是 fc2 的输入切片，所以中间不需要 all-gather；fc2 之后做一次 all-reduce。这是 Megatron TP 的基本模式。

MiniVerl 的 `parallel.py::ColumnParallelLinear/RowParallelLinear` 用 NumPy 显式实现了这两种切分，测试 `test_column_row_parallel_equivalence` 验证与 dense 结果一致。

## 12.3 序列并行

TP 只切 hidden 维，LayerNorm、Dropout、残差等仍是每卡完整序列。序列并行（SP）把这些部分按序列切分：

- 在 TP 的 all-reduce 前后做 `all-to-all`/`all-gather`；
- 每个 rank 只处理一段 token；
- 降低激活显存。

Ulysses 是常用的 SP 实现：把序列维切成 `sp_size` 段，attention 前做 all-to-all 重排，attention 后再 all-to-all 回来。verl 通过 `ulysses_sequence_parallel_size` 配置。

MiniVerl 的 `ulysses_split/ulysses_merge` 演示了切分与还原；`sequence_parallel_mlp` 演示了按序列切分后计算结果与 dense 一致。

## 12.4 流水线并行

PP 把层分成若干 stage，每个 stage 放在不同 GPU 上。前向按 micro-batch 流水，反向按 1F1B 调度：

```text
GPU0: F0 F1 F2 ... B2 B1 B0
GPU1:    F0 F1 F2 ... B2 B1 B0
GPU2:       F0 F1 F2 ... B2 B1 B0
```

气泡比例近似：

$$
\text{bubble}\approx\frac{p-1}{m+p-1},
$$

其中 $p$ 是 stage 数，$m$ 是 micro-batch 数。增大 $m$ 可以降低气泡，但增加激活显存。

MiniVerl 的 `PipelineSchedule.bubble_ratio()` 使用这个公式；`pipeline_forward` 验证流水输出与顺序执行一致。

## 12.5 专家并行与 MoE

MoE 层有 $E$ 个专家，每个 token 通过 router 选择 top-k 个专家。EP 把专家分到不同 GPU：

1. router 计算每个 token 的专家分配；
2. all-to-all 把 token 发到对应专家所在 GPU；
3. 专家计算；
4. all-to-all 把结果发回。

关键参数：

- `num_experts`；
- `top_k`；
- `capacity_factor`：每个专家最多接收多少 token，超出丢弃；
- `expert_model_parallel_size`；
- `moe_grouped_gemm` / `moe_token_dispatcher_type`。

容量因子太小会丢 token，太大浪费显存。MiniVerl 的 `moe_route` 实现了 top-k、容量限制、丢弃统计与加权合并。

## 12.6 Megatron 的模型定义

Megatron 不使用 HuggingFace 的 `modeling_*.py`，而是用 MCore 的 `GPTModel` 等定义。权重需要从 HF 格式转换：

- `mbridge`：HF ↔ Megatron 的权重映射；
- `megatron-bridge`：更完整的模型桥接；
- `verl/model_merger/megatron_model_merger.py`：把分布式 checkpoint 合并回 HF。

这带来两个工程约束：

1. **新模型支持慢**：需要一个模型一个模型地写映射；
2. **权重同步复杂**：训练侧是 TP/PP/EP 布局，rollout 侧是 TP 布局，需要 reshard。

verl 的 `MegatronEngineWithLMHead`/`WithValueHead` 封装了模型构建、优化器、checkpoint 与权重导出；`verl/workers/engine/megatron/transformer_impl.py` 是入口。

## 12.7 配置示例

```yaml
model_engine: megatron

actor_rollout_ref:
  actor:
    strategy: megatron
    megatron:
      tensor_model_parallel_size: 4
      pipeline_model_parallel_size: 2
      context_parallel_size: 1
      expert_model_parallel_size: 8
      sequence_parallel: true
      use_distributed_optimizer: true
      override_transformer_config:
        recompute_granularity: full
        recompute_method: uniform
        recompute_num_layers: 1
  rollout:
    name: vllm
    tensor_model_parallel_size: 4
    pipeline_model_parallel_size: 1
    expert_parallel_size: 1
```

字段名以 `verl/trainer/config/actor/megatron_actor.yaml` 与 `verl/workers/config/` 下的 dataclass 为准。不同版本的 MCore 字段会变化，升级时先看 migration note。

## 12.8 并行组合的约束

不是所有组合都合法：

- 基本约束是 **TP × PP × CP × DP = 总 GPU 数**；EP 不是独立的乘数，而是 DP 内部对专家维的再切分：`EP × expert-DP = DP`；
- TP 通常要求 hidden size 能被 TP 整除；
- PP 要求层数能被 PP 整除（或使用不均匀切分）；
- EP 要求专家数能被 EP 整除；
- CP 要求序列长度能被 CP 整除（或支持变长切分）；
- rollout 的 TP 与训练的 TP 可以不同，但权重同步要 reshard。

一个 64 卡、671B MoE 的例子：

```text
TP=8, PP=4, CP=1, DP=2  -> 8*4*1*2 = 64
EP=2                    -> expert-DP = DP / EP = 1
```

这里 DP=2 表示有两组完整的数据并行副本；EP=2 把每组中的专家再分到 2 个 rank。若写成 `TP=8, PP=4, EP=8, DP=2`，乘积会变成 512，和 64 卡对不上——这正是把 EP 误当成独立乘数的典型错误。

实际配置要用 verl 的脚本与显存估算反复试。第 32 章给出方法论。

## 12.9 Megatron 与 FSDP 的取舍

| 维度 | FSDP2 | Megatron |
|---|---|---|
| 上手难度 | 低 | 高 |
| 新模型支持 | HF 直接支持 | 需要 mbridge 映射 |
| 大模型扩展 | 一般 | 最好 |
| MoE | 较弱 | 强（EP/All-to-All） |
| 通信 | all-gather/reduce-scatter | TP/PP/CP/EP 多种 |
| 调试 | 较易 | 较难 |
| 适用 | 研究、7B～32B | 32B～671B、生产 |

## 12.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即报 shape mismatch | TP/PP/EP 与模型不整除 | 调整并行度 |
| loss 不收敛/数值异常 | TP 的 LayerNorm/softmax 精度 | 用 fp32、检查 sequence parallel |
| 权重同步后输出乱码 | HF ↔ Megatron 映射错 | 用 mbridge 校验、对比 logits |
| PP 气泡大 | micro-batch 太少 | 增加 micro-batch 或减 stage |
| MoE 丢 token 多 | capacity_factor 太小 | 增大容量或调 router |
| checkpoint 合并失败 | 分布式 checkpoint 不完整 | 检查 world size、优化器状态、merger 参数 |

## 12.11 MiniVerl 的 Megatron 模拟

MiniVerl 不实现真正的 Megatron，而是提供：

- `ColumnParallelLinear` / `RowParallelLinear`：数值等价的 TP；
- `PipelineSchedule` / `pipeline_forward`：1F1B 气泡与顺序等价；
- `moe_route`：top-k、容量、丢弃；
- `ulysses_split/merge`：序列并行；
- `sharding.tp_shard_state_dict/tp_gather_state_dict`：TP 权重布局。

`MegatronEngine` 与 `FSDPEngine` 共享 dense 训练数学，区别只在权重导出布局。这样 PPO 循环不需要感知后端，和 verl 的 `BaseEngine` 设计一致。

## 本章小结

- Megatron 用 TP/PP/CP/EP/SP 组合扩展到大模型。
- 列并行切输出、行并行切输入，MLP 的 fc1+fc2 是标准组合。
- PP 的气泡约为 `(p-1)/(m+p-1)`；增大 micro-batch 可缓解。
- MoE 的容量因子决定丢 token 比例，EP 用 all-to-all 分发。
- HF ↔ Megatron 权重映射与 reshard 是主要工程成本。
- MiniVerl 用数值模拟覆盖 TP/PP/MoE/SP 的核心机制。

## 延伸阅读

- Megatron-LM：<https://github.com/NVIDIA/Megatron-LM>
- Megatron Core 文档：<https://docs.nvidia.com/megatron-core/>
- `verl/workers/engine/megatron/transformer_impl.py`
- `verl/model_merger/megatron_model_merger.py`
- 附录 U：ModelRegistry、权重加载/保存与新模型接入清单
- 下一章：序列并行、remove padding 与变长批。
