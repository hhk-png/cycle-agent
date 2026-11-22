# 附录 V 前沿模型接入案例：DeepSeek V4、量化权重同步与路由回放

DeepSeek V4 是一个很好的"模型-系统协同"样本：它的问题不是"能不能加载"，而是**同一份权重在训练后端与推理后端的表示不同**，以及**同一批 token 在两个后端被路由到不同专家**。前者会让 log-prob 系统性偏移，后者会让 on-policy 假设失效。两者都不会以"报错"的形式出现。

本附录以 v0.9.1 的 `docs/advance/deepseek_v4_integration.rst` 与相关源码为基线，提炼成可复用的接入方法。

## V.1 为什么单列一个案例

普通 dense 模型的权重同步是"拷贝同名张量"。MoE + 低精度的模型不是：

- 同一层可能同时有 FP8 dense 权重与 FP4 专家权重；
- 推理后端在首次加载时会把原始专家张量转换成 MXFP4 或 MegaMoE 布局，**转换后原参数可能被替换或删除**；
- 训练后端（Megatron）导出的是原始 checkpoint 布局，名字相同但语义不同；
- 专家路由依赖 router logits，而 hash router 的专家 id 来自 `input_ids` 查表，不是可微的 router；
- 训练与推理的 kernel/精度不同，路由可能分叉。

所以 DeepSeek V4 的接入工作分成两条独立主线：**权重同步**与**路由回放**。先做权重同步，再开路由回放；顺序反了，无法判断误差来自哪一层。

## V.2 DeepSeek V4 的权重表示

DeepSeek-V4-Flash 不是"全 FP8"：

| 部分 | 表示 | 说明 |
|---|---|---|
| dense 权重 | E4M3 FP8 + UE8M0 scale | scale 是 2 的幂，不能随意转成别的浮点格式 |
| routed MoE experts | packed FP4 | 打包后的 4-bit 权重 |
| 推理侧布局 | MXFP4 或 MegaMoE | 首次 `load_weights` 时由 vLLM 转换 |

关键事实：**Megatron 导出的是原始 checkpoint 布局，vLLM 持有的是后处理布局**。即使张量名相同，也不能直接拷贝。正确的同步路径是：

1. 恢复原始 dense 与 expert 参数，连同 `weight_loader` 元数据；
2. 在运行任何非幂等的模型后处理之前，先加载所有 weight bucket；
3. UE8M0 scale 保持原始 dtype，不做"反量化再量化"；
4. 让 vLLM 的 loader 负责 TP 切分与 `w1/w3 → w13` 合并；
5. 最后一个 bucket 之后，再重建一次 MXFP4/MegaMoE 表示。

第 2 步和第 5 步是"全有或全无"的：如果每加载一个 bucket 就做一次后处理，后面的 bucket 会被写进一个已经被前一次后处理改造过的模型。这种错误不会报错，只会让权重逐渐失真。

## V.3 分桶同步协议

设一次更新要发送 $B$ 个 bucket，每个 bucket 包含若干命名张量。协议是：

```text
prepare(model)                       # 只做一次，恢复原始参数与 loader 元数据
for bucket in buckets:               # 逐桶
    load_weights(bucket)             # 只加载，不做非幂等后处理
finalize(model)                      # 只做一次，重建 MXFP4/MegaMoE
```

为什么必须分桶？因为整模型权重在 host 内存里放不下，且传输要流式进行。分桶的代价是：**任何跨桶的全局后处理都必须被拆成 prepare/finalize 两段**，不能藏在单个 bucket 的回调里。

v0.9.1 的入口：

- `verl/utils/vllm/vllm_quant_utils.py`：量化出站权重流，按层把 prepare/finalize 分派给对应的 scheme；
- `verl/utils/vllm/vllm_fp8_utils.py`：block FP8 linear 与 MoE；
- `verl/utils/vllm/vllm_fp4_utils.py`：MXFP4 routed experts；
- `verl/workers/rollout/vllm_rollout/utils.py`：bucket 级编排。

这个分层的好处是：新增一种量化格式只需要实现 scheme 的 prepare/finalize/load，不需要改传输层。

## V.4 权重同步的失败模式

| 现象 | 根因 | 为什么难发现 |
|---|---|---|
| `target 1024 vs loaded 2048` | 专家形状不匹配 | 至少会报错，属于"好"失败 |
| `load_weights` 成功但生成质量下降 | expert 布局或 scale 错 | 不报错，只有 log-prob 相关性下降 |
| 第二步训练后突然崩 | 第二次同步时 vLLM 已后处理过参数 | 第一步正常，第二步才暴露 |
| scale 精度损失 | UE8M0 被反量化再量化 | 单步误差小，累积后显著 |
| 只有部分专家更新 | bucket 边界与 expert 分片不对齐 | 路由到未更新专家的 token 表现异常 |

最重要的一条：**`load_weights` 成功不等于同步正确**。必须用 V.7 的相关性检查作为验收标准。

## V.5 Router replay：让训练与推理走同一组专家

MoE 的前向是：

$$
y = \sum_{i \in \text{TopK}(x)} g_i(x)\, E_i(x).
$$

如果 rollout 时 token $x$ 路由到专家 $\{2,5\}$，而 actor 更新时路由到 $\{2,7\}$，那么 PPO 的重要性比率

$$
\rho_t = \frac{\pi_\theta(a_t \mid s_t)}{\pi_{\theta_{\text{old}}}(a_t \mid s_t)}
$$

就不再对应"同一个策略在同一条轨迹上的概率比"。策略梯度仍然能算，但 on-policy 假设被破坏，方差与偏差都变大。

Router replay 的做法是：**在 rollout/old-log-prob 阶段记录每层 top-k 专家 id，在 actor 更新时强制使用记录值**。v0.9.1 有三种动作：

| 动作 | 何时用 | 行为 |
|---|---|---|
| `RECORD` | rollout 或 old-log-prob | 正常路由，记录 top-k |
| `REPLAY_FORWARD` | actor 前向 | 用记录的 top-k，不重新路由 |
| `REPLAY_BACKWARD` | 反向重放 | 从列表头部 `pop(0)`，先进先出；不是倒序 |

对应 `verl/utils/megatron/router_replay_patch.py` 的 `RouterReplay`、`RouterReplayAction`。`set_replay_data(all_layers_topk_indices)` 把每层的记录分发给对应的 router 实例；`get_recorded_data()` 收集所有层的记录。

两个模式：

- **R2**：在 actor log-prob 计算时记录，在 actor 更新时重放；
- **R3**：在 rollout 后端记录，在 Megatron 中重放。

R2 解决"训练内部两次前向不一致"；R3 解决"推理后端与训练后端不一致"。只做 R2 不做 R3，rollout 与 actor 仍可能分叉。

## V.6 hash router：层序比形状更危险

DeepSeek-V4-Flash 的前三层 routed layer 使用 **hash router**：专家 id 由 `input_ids` 与一张 token-to-expert 表决定，不来自可学习的 router logits。这意味着：

- 现有的"拦截 learned top-k"机制看不到这三层；
- 如果只记录 learned router，vLLM 报告所有 MoE 层，而 Megatron 少了前三层；
- 后续每一层的记录都会**错位一层**，即使张量形状完全兼容。

修复需要三件事：

1. 把 `input_ids` 传进 decoder，让 hash router 与 rollout 看到同一输入；
2. 记录三层 hash-router 输出，再记录后续 learned top-k；
3. 保持 routed layer 的顺序在 vLLM 与 Megatron 之间一致。

相关实现位于 `verl/models/mcore/model_forward_fused.py` 与 `verl/utils/megatron/router_replay_patch.py`；R3 的 mask 构造与路由分布在 `verl/utils/megatron/router_replay_utils.py`。VeOmni 路径另有 `verl/utils/veomni/router_replay.py`。

这个 bug 的教训具有普遍性：**当"层数"是隐式契约的一部分时，必须显式校验层数**。MiniVerl 的 `replay_moe_routes` 在层数不一致时直接抛错，就是为了把这种静默错位变成显式失败。

## V.7 replay mask 与重复专家

R3 需要一个 causal replay mask：它必须包含**影响 response token logits 的所有行**，而不只是被标记为 response 的行。原因是在 causal attention 下，prompt 的最后一小段也参与第一个 response token 的计算；如果 mask 漏掉它们，重放的路由与真实前向不一致。

另一个细节：当重放的 top-k 结果包含重复专家 id 时，dispatcher 的 token count 不能假设等于 `num_tokens * topk`，而要从实际 routing map 统计。否则容量因子与丢弃判断会偏。

这类问题的共同点是：**路由不是一个 (tokens, top_k) 的数组，而是一个带顺序、带 mask、带容量的过程**。只对数组做 `torch.where` 不足以复现它。

## V.8 验证协议

按顺序做，前一步不过不要进下一步：

1. **关闭 router replay，跑至少两个优化器 step**。第二步会触发 vLLM 已经后处理过参数的同步路径；只跑一步会漏掉这个问题。
2. **比较 rollout 与 actor 的 token log-prob**。报告 Pearson 相关、最大差、均值差、标准差。相关性低指向权重/scale/packed layout，而不是 replay。
3. **开启目标 replay 模式（R2 或 R3），重复比较**。确认三层 hash router 与所有 learned router 都被记录，且顺序一致。
4. **保存并重载 checkpoint**（模型、优化器、scheduler、trainer state）。权重传输成功不能证明混合 FP32/量化参数的优化器状态正确。
5. **用目标负载的 prompt/response 长度跑**。短 smoke test 会漏掉 sequence packing、attention window、routed expert 的边界问题。

建议把第 2 步的指标做成训练日志的常驻项，而不是一次性检查。权重同步是每次更新都发生的事，回归也随时可能发生。

## V.9 对其它模型的启示

DeepSeek V4 不是特例，它是三类问题的放大版：

- **低精度权重的表示边界**：训练导出的是"原始表示"，推理持有的是"后处理表示"；同步必须理解两端的生命周期。
- **MoE 路由的一致性**：只要训练与推理的 kernel/精度/并行不同，路由就可能分叉；记录-重放是通用解法。
- **隐式层序契约**：hash router、MTP、VL 的视觉层都会改变"第几层是什么"；层数/顺序必须显式校验。

把这三条抽象出来，可以用于任何 MoE + 量化 + 多后端推理的模型接入。

## V.10 MiniVerl 的路由回放模拟

MiniVerl 在 `mini_verl/parallel.py` 中实现了同一套语义的简化版：

- `moe_route(..., replay_top_idx=..., replay_mask=...)`：用记录值替换 top-k，支持行级 mask；
- `record_moe_routes(result)`：复制记录，避免后续原地修改污染；
- `replay_moe_routes(layer_logits, recorded_routes, ...)`：逐层重放，**层数不一致直接抛错**；
- `router_replay_report(recorded, replayed)`：返回 `exact_match`、`mismatched_layers`、`first_mismatch`、`mismatched_tokens`。

`tests/test_router_replay.py` 覆盖：logits 改变时重放仍复现、行级 mask 混用、形状/范围校验、层数不匹配、首个错层定位、记录独立性。`examples/24_router_replay.py` 演示"记录 → 重放 → hash-router 层数陷阱 → 单点损坏定位"。

MiniVerl 不模拟 FP8/FP4 的字节级布局，也不模拟 vLLM 的后处理生命周期；它只验证路由的记录/重放契约。量化权重同步的字节账与生命周期，需要在真实 verl + vLLM 上按 V.2～V.4 验证。

## V.11 检查清单

- [ ] 明确哪些权重是 FP8、哪些是 FP4，scale 的 dtype 与语义已记录。
- [ ] 权重同步实现 prepare → buckets → finalize，后处理不在桶内重复执行。
- [ ] UE8M0 scale 没有被反量化再量化。
- [ ] 专家形状、TP 切分、`w1/w3 → w13` 合并由推理侧 loader 负责。
- [ ] 至少跑两个优化器 step 再判断同步是否稳定。
- [ ] 记录 rollout/actor log-prob 的相关性与差异统计。
- [ ] hash router 层与 learned router 层都被记录，层序显式校验。
- [ ] replay mask 覆盖影响 response logits 的所有行。
- [ ] 检查点恢复覆盖模型、优化器、scheduler、trainer state。
- [ ] 用目标长度负载做最终验证，而不是只跑短 smoke test。

## 延伸阅读

- `verl/utils/vllm/vllm_quant_utils.py`、`verl/utils/vllm/vllm_fp8_utils.py`、`verl/utils/vllm/vllm_fp4_utils.py`
- `verl/workers/rollout/vllm_rollout/utils.py`
- `verl/models/mcore/model_forward_fused.py`
- `verl/utils/megatron/router_replay_patch.py`、`verl/utils/megatron/router_replay_utils.py`
- `verl/utils/veomni/router_replay.py`
- `examples/router_replay/run_qwen3_30b_a3b_megatron.sh`
- 附录 U：模型接入与权重加载
- 第 14 章：权重同步与 3D-HybridEngine
- 第 30 章：LoRA、量化、MoE 与大模型训练
