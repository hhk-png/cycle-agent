# 第 15 章 mini-megatron 工程总览

> **第 III 部分导读（15–28）**
> 本部分把原理落成可运行代码。前置是 05–10（至少 06/08/09）；读完应能独立
> 实现 Column/Row Parallel、1F1B、词表并行 CE，并为新并行层写等价性测试。
> 建议边读边跑 `pytest`，顺序 15→16→17→18→19→20→21→22→23→24→25→26→27→28；
> 22/25/28 可在需要时回读。

## 15.1 设计目标

mini-megatron 不是“把 Megatron 抄一遍”，而是满足四个约束：

1. **可运行**：CPU、Windows、单机多进程都能跑；
2. **可验证**：与单卡参考实现做 loss/梯度等价性对比；
3. **可映射**：类名/文件结构与 Megatron Core 对应；
4. **可裁剪**：去掉不影响正确性的性能优化，保留关键路径。

因此它不追求吞吐，而追求“你能在一天内读完并改对”。

## 15.2 模块依赖图

核心训练路径（先读这张）：

```text
config.py                      # 独立：只做 dataclass + 校验
   │
   ▼
parallel/comm.py ──► parallel/state.py
                         │
                         ├─► parallel/layers.py
                         │      └─► parallel/attention.py
                         ├─► parallel/cross_entropy.py
                         ├─► parallel/moe.py
                         ├─► parallel/expert_parallel.py   # EP/all-to-all
                         └─► parallel/transformer.py
                                   │
                                   ▼
                            pipeline/schedules.py
                                   │
   optim/adamw.py ─────────────────┤
   optim/distributed_optimizer.py ─┤   # 可选 flat 路径（style="flat"）
   data/dataset.py ────────────────┤
   checkpoint.py ──────────────────┤
                                   ▼
                              training.py
                                   │
                                   ▼
                                cli.py
                                   │
                                   └─► generation.py      # 仅 CLI 推理路径
```

注意方向：`config.py` 不 import 任何运行时模块；`state.py` import `comm.py`；
`training.py` 才把模型、数据、优化器、调度和检查点串起来。`generation.py`
不在训练主链上，只由 `cli.py` 的 `generate` 子命令调用。`generation_kv.py`
是测试/脚本用的独立 KV-cache 参考，**没有接进 CLI**（26.9 会用到它）。
把 `config.py` 画成“所有模块的父节点”会误导读者对初始化顺序的理解。

教学扩展模块（不在这条主链上，但会被章节引用）：

```text
analysis.py               参数/显存/FLOPs/通信估算
sharding.py               全量 state_dict → TP/PP 切片（测试用）
checkpoint_reshard.py     模型权重 + 非 ZeRO AdamW 状态的 TP/PP 重分片
data/packing.py           document mask / position reset
pipeline/interleaved.py   VPP chunk 映射、依赖校验、气泡估算
parallel/ring_attention.py ring attention 参考（online softmax/因果 mask）
parallel/ulysses.py       Ulysses CP 参考（两次 all-to-all 布局变换）
parallel/moe_balance.py   aux-free bias 均衡 / node-limited routing
inference/paged_cache.py  分页 KV cache（block table/allocator）
pipeline/interleaved_exec.py 可执行的 interleaved GPipe（环形 p2p）
generation_kv.py          KV cache 解码
parallel/mla.py           MLA 独立参考（latent KV / 解耦 RoPE）
```

### 15.2.1 analysis.py：把账本变成代码

`analysis.py` 不在训练主链上，但它是第 04 章所有公式的可执行版本。核心函数：

| 函数 | 作用 |
|---|---|
| `parameter_count(config)` | 总参数/激活参数（MoE 两者不同） |
| `dense_layer_parameters(config)` / `moe_layer_parameters(config)` | 单层参数（含 GELU/SwiGLU 不同的 bias 数） |
| `optimizer_state_bytes(...)` | 混合精度 AdamW 的 16 B/param 分解 |
| `state_memory_per_rank(model, parallel, ...)` | 按 TP/PP/ZeRO/EP 计算每卡状态显存 |
| `activation_memory_bytes(...)` | 用 `s·b·h·(34+5·n_h·s/h)` 量级估激活 |
| `attention_score_bytes(...)` | 显式 `s×s` score 矩阵 |
| `kv_cache_bytes(...)` | 推理 KV（MHA/GQA；MLA 的 `(d_c+d_R)` 公式见 30.5，尚未实现） |
| `training_flops(...)` | `6ND` + 注意力项；MoE 用激活参数 |
| `gpu_hours(...)` / `cluster_hours(...)` / `mfu(...)` | 总 GPU-hours、墙钟小时、MFU 的换算 |
| `communication_bytes(...)` | DP/TP/PP/CP/EP 的一阶通信量 |
| `format_estimate(...)` | CLI `estimate` 的文本/JSON 输出 |

它刻意把假设写成参数和注释，方便你改一个数就看到结果变化。注意它的局限：
通信模型是一阶和，不含重叠；激活是经验公式，不含 allocator 碎片；MFU 是
输入而不是预测。它适合做方案筛选，不适合替代 profiling。具体的公式口径见
4.4–4.5，CLI 用法见 4.8。

读代码的建议顺序：

1. `config.py`：知道有哪些开关；
2. `comm.py` + `state.py`：知道 rank 和 group（TP/PP/CP/DP/EP）；
3. `layers.py`：知道一个层怎样切；
4. `attention.py` / `transformer.py`：知道模型怎样拼；
5. `cross_entropy.py` / `moe.py` / `expert_parallel.py`：三个特殊模块；
6. `schedules.py` / `interleaved.py`：多 micro-batch 与 VPP；
7. `training.py` / `checkpoint.py`：一次训练怎样闭环。

## 15.3 配置矩阵

`configs/` 里的配置是“实验矩阵”，不是随便的示例：

| 配置 | world | TP | PP | CP | DP | 验证点 |
|---|---:|---:|---:|---:|---:|---|
| `tiny.json` | 1 | 1 | 1 | 1 | 1 | 基线、生成、检查点 |
| `tiny_tp2.json` | 2 | 2 | 1 | 1 | 1 | TP+SP、CE |
| `tiny_pp2.json` | 2 | 1 | 2 | 1 | 1 | 1F1B、p2p |
| `tiny_pp2_gpipe.json` | 2 | 1 | 2 | 1 | 1 | GPipe vs 1F1B |
| `tiny_cp2.json` | 2 | 1 | 1 | 2 | 1 | CP mask、KV 梯度 |
| `tiny_dp2.json` | 2 | 1 | 1 | 1 | 2 | DP + ZeRO-1 |
| `tiny_dp2_flat.json` | 2 | 1 | 1 | 1 | 2 | DP + flat reduce-scatter/all-gather |
| `tiny_moe.json` | 1 | 1 | 1 | 1 | 1 | 路由/aux |
| `tiny_moe_tp2.json` | 2 | 2 | 1 | 1 | 1 | MoE+TP+SP |
| `tiny_moe_dp2.json` | 2 | 1 | 1 | 1 | 2 | MoE+DP 的 `grad=None` 集合通信对称性 |
| `tiny_ep2.json` | 2 | 1 | 1 | 1 | 2 | EP=2 all-to-all |
| `tiny_tp2_ep2.json` | 4 | 2 | 1 | 1 | 2 | TP×EP 组正交 |
| `tiny_tp2_pp2.json` | 4 | 2 | 2 | 1 | 1 | TP+PP+SP |
| `tiny_pp2_dp2.json` | 4 | 1 | 2 | 1 | 2 | PP+DP |
| `tiny_cp2_pp2.json` | 4 | 1 | 2 | 2 | 1 | CP+PP |
| `tiny_tp2_dp2.json` | 4 | 2 | 1 | 1 | 2 | TP+DP+ZeRO |
| `tiny_tp2_cp2.json` | 4 | 2 | 1 | 2 | 1 | TP+CP |

`tiny_ep2` / `tiny_tp2_ep2` 的 DP 列是 2，同时把 DP 维度重新解释为 EP=2：
专家按 rank 切分，token 通过 all-to-all 往返。mini 限制 `ep = dp`，因此没有
“专家副本”层；生产 Megatron 允许 `ep < dp`。

`tiny_moe_dp2.json` 是回归配置：`seq_len=2` 让不同 DP rank 的路由结果不同，
用来验证“某专家在部分 rank 没有梯度”时集合通信仍然对齐。

这些配置不是“性能最优”，而是覆盖组合路径。`verify.py` 会跑其中大部分
（TP/PP/CP/DP/ZeRO-1/flat optimizer/EP/MoE/TP+PP/TP+DP/PP+DP/CP+PP/TP+CP/TP+EP），完整矩阵可以
手动跑。注意 `tiny_tp2_cp2` 是 2 步训练冒烟，不是逐元素等价性测试；
`tiny_tp2_dp2` 覆盖 TP+DP+ZeRO 的训练路径，但 ZeRO 的多进程 checkpoint
恢复仍未覆盖。

## 15.4 一个 step 的调用链

以 PP=2 为例：

```text
Trainer.train_step
  ├─ BatchProvider.get_micro_batches
  ├─ schedule.forward_backward
  │    ├─ stage0.forward(ids) -> hidden
  │    ├─ comm.isend(hidden) -> stage1
  │    ├─ stage1.forward(hidden, labels) -> loss
  │    ├─ loss.backward()
  │    ├─ comm.send(grad) -> stage0
  │    └─ stage0.backward()
  ├─ all_reduce_gradients(DP)
  ├─ all_reduce_gradients(CP)
  ├─ clip_gradients
  ├─ set_lr
  └─ optimizer.step
```

如果你在调试，建议在每个箭头处打印 shape、dtype、rank、step，并确认：

- stage0 的输出和 stage1 的输入 shape 一致；
- loss 只在最后一个 stage 非 None；
- 梯度 send 的 tensor 和 recv buffer shape 一致；
- DP all-reduce 的 group 不包含 PP 邻居；
- CP all-reduce 只在 CP group 内。

## 15.5 工程原则

### 1. 先校验，再分配

`ExperimentConfig.validate(world_size)` 在任何模型/通信初始化之前执行。它能
拦住大多数“跑起来才发现维度不对”的错误。

### 2. 通信抽象，而不是到处 `dist.*`

所有集合通信经过 `comm.py`。好处：

- Windows 可以用 portable；
- 测试可以替换后端；
- 代码更容易读/审计；
- 未来接入 NCCL/自定义通信只需改一处。

### 3. 数值等价性优先于吞吐

TP/PP/CP 在 `tests/parallel_equiv.py` 有 loss/梯度等价性用例；DP/ZeRO 在
`tests/zero_equiv.py`；EP 在 `tests/parallel_ep.py`（对比全局 batch 的
logits/aux/梯度）。新增功能时，先让 world=1 的参考实现通过，再让多进程
loss/梯度对齐，最后才优化性能。

### 4. 显式状态，不依赖隐式全局

mini 仍然使用模块级 group 变量（与 Megatron 类似），但所有 group 在
`initialize_model_parallel` 中一次性创建；测试 fixture 会 destroy。生产代码应
避免在库内部隐式初始化全局状态。

### 5. 先正确，再重叠

mini 的 all-reduce/p2p 是同步或极简异步；生产 Megatron 的大量复杂度来自通信
重叠。不要在 mini 上做性能结论，但可以用它验证“顺序/依赖”是否正确。

## 15.6 如何扩展

### 加一个新层

1. 在 `parallel/layers.py` 实现 TP 版本；
2. 写 world=1 的数值测试；
3. 用 `sharding.py` 加 TP/PP 切片规则；
4. 在 `tests/parallel_equiv.py` 加 loss/梯度对比；
5. 更新文档中的形状表。

### 加一个新并行维度

1. 在 `state.py` 加 group；
2. 在 `comm.py` 确认 collective 语义（EP 需要变长 all-to-all）；
3. 在 attention/MLP/CE 加对应通信；
4. 加 CP/DP/EP 梯度同步（哪些参数该相加、哪些该平均、哪些该跳过）；
5. 加参照实现和等价性测试。

EP 就是按这个流程加的：`state.py` 建 EP 组 → `comm.py` 加 all-to-all →
`expert_parallel.py` 实现 dispatch/combine → `adamw.py` 跳过专家梯度并除以
ep → `tests/parallel_ep.py` 对比全局 batch。

完整的 API、CLI 与扩展点见 [附录 F](F-mini-api.md)。

### 加一个新 schedule

1. 实现 `BaseSchedule.forward_backward`；
2. 确保 warmup/cooldown 依赖正确；
3. 用相同权重对比 GPipe/1F1B 的 loss；
4. 测峰值激活/显存（CPU 可看 tensor 生命周期）。

## 15.7 局限

- `Trainer` 主循环不接 VPP；`pipeline/interleaved_exec.py` 提供可运行的
  interleaved GPipe（含等价性测试），但没有 1F1B/zero-bubble/重叠；
- EP 只支持 `ep = dp`，没有专家副本；dispatch 用 padding 而非 split sizes，
  专家 GEMM 是 Python 循环而非 grouped GEMM；
- CP 用 all-gather KV，不省显存；
- 检查点只支持同布局恢复；`checkpoint_reshard.py` 支持模型权重与**非 ZeRO**
  AdamW 状态（`exp_avg/exp_avg_sq/step`）的 TP/PP 重分片，但不支持 ZeRO/EP
  状态重排；
- 没有 CUDA Graph/FlashAttention/TE；
- MLA 只是一个**独立教学模块**（`parallel/mla.py`），支持 prefill/decode
  与压缩 KV cache，但没有接入 `TransformerStage`/Trainer，也没有 TP/CP
  切分；其矩阵形状与吸收公式见 30.5；
- 生成支持单请求 KV cache，但没有连续批处理/prefix cache；
- 数据管线非常简化（提供 packing/document mask 的教学实现）。

这些局限在对应章节都会展开；不要把 mini 的吞吐/显存当成生产指标。

## 15.8 本章小结

- mini 的目标是正确、可读、可验证，而不是性能。
- 模块依赖清晰：config → comm/state → layers/model → schedule → trainer。
- 配置矩阵覆盖主要并行组合。
- 扩展新功能时，先写参考实现和等价性测试，再优化。
- 生产差距主要在 kernel、通信重叠、检查点重分片和规模工程。

下一章从配置与进程组开始逐模块拆解。
