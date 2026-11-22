# 附录 Q 高级特性：attention、RoPE、MTP、dynamic CP 与可观测性

> 第 11～15、31～32 章讲的是主干（FSDP/Megatron/rollout/性能）。本附录补上 v0.9.1 中容易被教程略过、但在生产里很常用的一组“高级旋钮”：attention 实现、RoPE、MTP、dynamic CP、FSDP Turbo、Megatron Lite、skip manager、determinism 与可观测性。
>
> 这些特性的共同点是：**默认不开，开了会改变数值或调度语义**。不要在没有 baseline 的情况下一次性全开。

## Q.1 一张特性地图

| 特性 | 解决的问题 | 典型入口 | 主要风险 |
|---|---|---|---|
| Attention backend | 长序列/变长批的算力与显存 | `verl/utils/attention_utils.py` | 数值差异、varlen 不兼容 |
| RoPE scaling | 超长上下文外推 | `verl/models/transformers/` | position_ids 错位、训练/推理不一致 |
| MTP | 多 token 预测/投机解码 | `examples/mtp_trainer/` | 额外 head、接受率、权重同步 |
| Dynamic CP | CP 下序列负载不均 | `docs/advance/dynamic_context_parallel.rst` | 切分开销、mask/position 边界 |
| FSDP Turbo | FSDP 通信/显存进一步优化 | `docs/advance/fsdp_turbo_backend.rst` | 版本/内核兼容 |
| Megatron Lite | 轻量 Megatron 路径 | `docs/advance/megatron_lite_backend.rst` | 功能子集、模型覆盖 |
| Skip manager | 跳过无效/低价值 step | `verl/utils/skip/skip_manager.py` | 指标口径、优化器状态 |
| Determinism | 可复现实验 | `docs/advance/determinism.md` | 性能下降、内核限制 |
| Rollout trace | 单请求全链路追踪 | `verl/utils/rollout_trace.py` | 采样开销、隐私 |
| Prometheus/Grafana | 在线监控 | `docs/advance/grafana_prometheus.md` | 指标基数、聚合口径 |
| RL-Insight | RL 状态可视化 | `docs/advance/rl_insight.md` | 额外服务依赖 |

## Q.2 Attention backend：先对齐数值，再谈性能

verl 的 attention 实现不是“写死 flash-attn”。常见选择包括 FlashAttention 2/3、PyTorch SDPA、eager（调试用），以及模型自定义的 attention（MLA、线性 attention、滑动窗口等）。入口在 `verl/utils/attention_utils.py`，模型侧通过 `attn_implementation` 一类的配置选择。

选择顺序：

1. **正确性**：先用 eager/SDPA 跑通，确认 logits 与 HF 一致；
2. **变长批**：确认后端支持 varlen/packed（remove padding 依赖它）；
3. **长序列**：再开 FlashAttention；注意 head_dim、dtype、GPU 架构的支持矩阵；
4. **自定义 attention**：MLA/线性 attention 需要模型侧实现，不能只换 kernel。

三个高频坑：

- **varlen 与 position_ids**：packed 序列必须按 `cu_seqlens` 重置 position；否则长序列的位置会“串台”。
- **attention mask 的语义**：因果 mask、padding mask、文档 mask 是三种不同的东西；多轮 Agent 还多一层 observation mask。
- **训练/推理不一致**：rollout 引擎（vLLM/SGLang）的 attention 实现与训练侧不同，log-prob 会有细微差异；PPO 的 ratio 会受影响，必须用同一套 mask 与温度。

验收：固定 prompt，比较 HF、训练引擎、rollout 引擎三方的 logits/log-prob；差异应在可解释的浮点范围内。若差异随序列长度增长，优先查 position_ids 与 mask。

## Q.3 RoPE 与长上下文

RoPE 不是“一个位置编码函数”那么简单。生产里常见三类变体：

- **基频缩放**（linear/NTK/YaRN）：把训练时的 4K/8K 外推到 32K/128K；
- **动态 NTK**：按输入长度调整基频；
- **部分维度旋转**：只对一部分 head_dim 应用 RoPE。

verl 在 `verl/models/transformers/` 下为不同模型提供 patch，`docs/advance/rope.rst` 记录了配置方式。关键约束：

1. **训练与推理必须用同一套 RoPE 参数**；rollout 引擎不认识你的自定义 RoPE 时，长上下文输出会漂移。
2. **position_ids 要按序列重置**；varlen/packed 布局下尤其重要。
3. **长度外推不是免费的**：超出训练分布后，attention 的数值范围会变大，可能需要 QK-norm、logit soft-cap 或更低的学习率。
4. **多模态 position_ids** 可能是三维的（时间/高/宽），不能直接套文本的 1D RoPE；`maybe_fix_3d_position_ids` 一类工具就是处理这个边界。

长上下文 RL 的额外成本在 rollout：KV cache 随长度线性增长，PD 分离的 KV 传输量也线性增长。第 15.12 节的公式可以直接用来估算。

## Q.4 MTP：多 token 预测

MTP（Multi-Token Prediction）在主干模型之外加若干个预测头，让模型一次预测未来 $k$ 个 token。它在 RL 里有两种用法：

1. **训练信号**：MTP loss 作为辅助目标，提升样本效率；
2. **推理加速**：MTP head 产生候选 token，配合投机解码（speculative decoding）提升 decode 吞吐。

v0.9.1 提供 MTP 的示例与实现：

- `examples/mtp_trainer/README.md`：MTP 训练入口与配置；
- `examples/mtp_trainer/run_mimo_7b_mtp_megatron.sh`：Megatron 路径；
- `verl/models/mcore/mtp_patch.py`：MCore 侧的 MTP patch；
- `docs/advance/mtp.md`：设计与使用说明；
- `tests/utils/test_megatron_mtp_dcp.py`、`tests/workers/rollout/rollout_vllm/test_mtp_hybrid_sleep_acceptance_on_cpu.py`：MTP 与 DCP/rollout 的边界测试。

工程要点：

- **额外参数要参与权重同步**：MTP head 不在原始 HF checkpoint 里，导出/同步时容易漏；漏了不会报错，只会让推理的接受率异常。
- **接受率（acceptance rate）是关键指标**：MTP 的收益取决于候选 token 被接受的比例；接受率低时，投机解码可能比普通 decode 更慢。
- **训练/推理的 MTP 配置要一致**：头数、词表、位置偏移、采样温度都要对齐。
- **MTP 不是免费的精度**：辅助 loss 的权重太大可能损害主目标；先做消融。

验收：固定 prompt，比较开/关 MTP 的主模型 logits；确认 MTP head 的权重被保存、被同步、能被推理引擎加载；报告接受率与端到端 tokens/s，而不是只看单步 loss。

## Q.5 Dynamic context parallel

CP（context parallel）把序列维切到多卡。固定切分在长尾序列上会出现负载不均：一张卡分到 8K token，另一张只分到 512 token。Dynamic CP 的目标是**按实际 token 负载动态划分序列**，让各 rank 的计算量接近。

它影响三件事：

1. **attention 的通信模式**：动态切分后，all-gather/all-to-all 的边界随请求变化；
2. **position_ids 与 mask**：每个 rank 只持有序列的一段，必须知道全局位置；
3. **loss 聚合**：token 数按全局统计，不能按本地 shard 平均。

配置与实现见 `docs/advance/dynamic_context_parallel.rst`。使用建议：

- 先在固定 CP 下确认正确性，再开 dynamic；
- 监控每个 rank 的 token 数与等待时间；
- 与 remove padding 组合时，确认 `cu_seqlens` 在切分后仍然正确；
- 不要在有状态的自定义 attention 上直接开，除非你验证过切分语义。

## Q.6 FSDP Turbo 与 Megatron Lite

**FSDP Turbo** 是 FSDP 路径上的进一步优化（通信/显存/内核），入口见 `docs/advance/fsdp_turbo_backend.rst` 与 `verl/workers/engine/fsdp/fsdp_turbo_impl.py`。它适合：

- 已经用 FSDP2、但通信或显存仍是瓶颈；
- 模型结构是标准 transformer，没有奇怪的并行需求；
- 能接受更激进的版本/内核依赖。

**Megatron Lite** 是较轻量的 Megatron 路径，入口见 `docs/advance/megatron_lite_backend.rst`，示例包括 `examples/grpo_trainer/run_deepseek_v4_megatron_lite.sh`、`run_kimi_k2_6_glm5_1_megatron_lite.sh`。它适合：

- 想要 Megatron 的 TP/PP/EP，但不想引入完整 MCore 的复杂度；
- 模型在 Lite 的支持列表内；
- 能接受功能子集（不是所有 MCore 特性都有）。

两者都属于“换后端”，验收标准与附录 P.8 相同：dense 等价、checkpoint 往返、权重导出可加载。不要因为它们“更轻/更快”就跳过数值对齐。

## Q.7 Skip manager：跳过 step 的语义

Skip manager（`verl/utils/skip/skip_manager.py`，文档 `docs/advance/skip_manager.rst`）用于在特定条件下跳过一次 optimizer update，例如：

- 本 batch 的 advantage 全为 0（DAPO 过滤后没有有效样本）；
- 数据不足/队列为空；
- 检测到异常梯度或 NaN；
- 异步模式下样本过期比例过高。

跳过的语义必须明确：

- **跳的是 optimizer step，不是数据消费**；否则数据游标会错位；
- **指标要标记 skipped**，否则曲线会出现“平坦段”被误读为收敛；
- **学习率 scheduler 是否前进**要明确；不同实现可能不同；
- **异步模式下跳过会改变 staleness**，要重新计算版本窗口。

一个常见 bug 是“跳过更新但梯度没清零”，下一次 step 把两次梯度混在一起。MiniVerl 的 `DAPO 全过滤时跳过更新` 测试就是防这个。

## Q.8 Determinism：可复现不是默认值

`docs/advance/determinism.md` 给出了 verl 的确定性指南。RL 的随机源比 SFT 多：

- 数据 shuffle；
- rollout 采样（temperature/top-k/top-p）；
- dropout；
- 多卡 all-reduce 的浮点顺序；
- vLLM/SGLang 的 continuous batching 调度；
- 异步队列的消费顺序。

要复现一个实验，至少固定：

```text
Python/NumPy/PyTorch seed
CUDA/cuDNN deterministic 开关
torch.use_deterministic_algorithms
NCCL 算法与拓扑（如可固定）
vLLM/SGLang 的 seed、调度与版本
数据顺序与 batch 组成
异步队列的消费策略
```

代价是性能：deterministic 内核可能更慢，continuous batching 的确定性可能要求关闭某些优化。实践建议：

- **调试/对比实验**：开 determinism，牺牲吞吐；
- **大规模生产**：接受一定非确定性，但固定数据、配置与评测协议；
- **报告结果**：写清是否 deterministic，以及随机种子数量。

同 seed 同配置下，MiniVerl 可以逐位复现；真实 verl 只能做到“统计等价”，不要承诺逐位一致。

## Q.9 可观测性：trace、Prometheus 与 RL-Insight

三类工具解决三个问题：

| 工具 | 回答的问题 | 入口 |
|---|---|---|
| Rollout trace | 这一条请求为什么慢/错 | `verl/utils/rollout_trace.py`、`docs/advance/rollout_trace.rst` |
| Prometheus/Grafana | 集群现在健康吗 | `docs/advance/grafana_prometheus.md` |
| RL-Insight | RL 训练状态是否正常 | `docs/advance/rl_insight.md` |

Rollout trace 的关键是**请求级关联**：一个 prompt 从进入 router、prefill、decode、工具调用到 reward，应该有一个 trace id 串起来。否则你只能看到“平均延迟”，无法定位长尾。

Prometheus 指标要控制基数：不要把 prompt id、样本 id 当 label；用任务类型、模型、阶段、错误码。Grafana 面板至少要有：

- GPU 利用率/显存/温度；
- rollout 队列长度、p50/p95/p99 延迟、超时率；
- reward 失败率、分数分布；
- 训练 loss/KL/clipfrac/grad_norm；
- 权重同步字节数/耗时/版本；
- checkpoint 成功率。

RL-Insight 把训练指标、rollout 状态与服务 dashboard 关联起来，适合平台团队。它的价值不在“多一个面板”，而在**跨层定位**：训练崩了，是因为 reward 服务超时，还是 rollout 权重版本错位？

## Q.10 采用顺序：一次只开一个

高级特性最容易犯的错误是“一次性全开”，然后无法归因。推荐顺序：

1. **Baseline**：sync、FSDP2、eager/SDPA、固定长度、无 MTP；
2. **Attention**：换 FlashAttention，验证 logits 与吞吐；
3. **长序列**：remove padding + RoPE scaling，验证 position/mask；
4. **并行**：CP/TP/PP，验证 loss 与显存；
5. **MTP**：加辅助头，验证接受率与权重同步；
6. **调度**：skip manager、dynamic CP、异步；
7. **可观测性**：trace/Prometheus 常开，作为回归基线。

每一步都保留上一版的配置与指标；出问题时能二分回退。

## Q.11 检查清单

- [ ] Attention 后端与训练/rollout 的 mask、position_ids 对齐。
- [ ] RoPE 参数在训练与推理侧一致；长上下文有外推验证。
- [ ] MTP head 被保存、同步、加载；接受率与端到端吞吐有报告。
- [ ] Dynamic CP 下 token 负载、position_ids、loss 归集正确。
- [ ] FSDP Turbo/Megatron Lite 通过 dense 等价与 checkpoint 往返。
- [ ] Skip 语义明确：数据游标、scheduler、指标、staleness。
- [ ] Determinism 开关与性能代价写进实验记录。
- [ ] Trace/Prometheus 指标基数可控，长尾可定位。
- [ ] 每次只开一个特性，保留可回退的 baseline。

## 延伸阅读

- 官方文档：`docs/advance/attention_implementation.rst`、`docs/advance/rope.rst`、`docs/advance/mtp.md`、`docs/advance/dynamic_context_parallel.rst`、`docs/advance/fsdp_turbo_backend.rst`、`docs/advance/megatron_lite_backend.rst`、`docs/advance/skip_manager.rst`、`docs/advance/determinism.md`、`docs/advance/rollout_trace.rst`、`docs/advance/grafana_prometheus.md`、`docs/advance/rl_insight.md`。
- MTP 示例：`examples/mtp_trainer/README.md`。
- 相关章节：第 11～15 章（引擎/并行/rollout）、第 29 章（多模态 position）、第 31～32 章（容错/性能）。
- 下一章：附录 R 项目交付与实验复盘手册。
