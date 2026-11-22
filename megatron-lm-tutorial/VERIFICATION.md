# 验证记录

本文件记录 mini-megatron 的最近一次完整验证。教程中的“可运行”承诺以本文件
为准；如果本机结果与这里不一致，先检查 Python/PyTorch 版本与运行平台。

## 0. 本轮复验（2026-11-22，第九版）

- 平台：Windows 11 Home China，CPU（无 CUDA）
- Python：3.14.6
- PyTorch：2.9.1+cpu
- NumPy：2.5.1，pytest：9.1.1
- mini-megatron：0.4.4
- 工作目录：`megatron-lm-tutorial/`

命令与结果：

```bash
python -m pytest -q
# 130 passed in 106.02s

python scripts/verify.py
# {"status": "ok", "elapsed_sec": 219.5, "cases": 25}

python scripts/check_docs.py
# chapters: 53 main + 14 appendices
# docs consistency: OK
```

第九版是**新增 Ulysses 参考实现 + 审计修正 + 广度补章**：

- 新增 `parallel/ulysses.py`、`tests/unit/test_ulysses.py`（7 项）与
  `scripts/ulysses_demo.py`：两次 all-to-all 布局变换、与 dense 的
  cp=1/2/4 前向/反向一致、因果性回归（改未来 token 不影响过去）、逆置换
  往返、不可整除报错、通信量公式。demo 输出（seq=64, heads=8, dim=16）：
  cp=1/2/4 的 `max_abs_error=0/2.09e-7/2.09e-7`，布局往返均为
  `exact=true`，per-rank 发送 0/16384/12288 个元素。pytest 从 123 增至
  **130 passed**（+7）。
- 新增 9.2.4（Ulysses）、34.3.2（MTP）、38.8.2（Energon）、33.2.2
  （Flight Recorder）、23.7.1（重试/OOM）、5.9.1（梯度压缩）、40.4.5
  （streaming/attention sink）、12.4.2（QAT/剪枝）、41.7.1（实验跟踪）、
  11.6.1（memory snapshot）、44.4.4（后训练/RL TCO）、A.21（Ulysses
  通信量）、L19b（练习）。
- 审计修正覆盖：EP/DP 梯度语义（`ep=dp` 前提、`dp>ep` 的 EDP 归约）、
  SP 通信方向、70B 显存结论、全局 batch 下限、推理成本决策、TCO 故障率
  与 43/44 基线对齐、KV 量化因子 2、SFT mask、检查点公式、1B 激活口径、
  DeepSeek V4/MTP 的官方证据、代码路径（`dist_checkpointing/`、
  `multi_latent_attention.py`）以及测试计数/页脚/符号等一致性问题。
- 详细清单见 [README 第九版修订](README.md)。

## 0.1 历史记录：第八版复验（2026-11-22）

- 平台：Windows 11 Home China，CPU（无 CUDA）
- Python：3.14.6
- PyTorch：2.9.1+cpu
- NumPy：2.5.1，pytest：9.1.1
- mini-megatron：0.4.3
- 工作目录：`megatron-lm-tutorial/`

命令与结果：

```bash
python -m pytest -q
# 123 passed in 100.80s

python scripts/verify.py
# {"status": "ok", "elapsed_sec": 211.6, "cases": 25}

python scripts/check_docs.py
# chapters: 53 main + 14 appendices
# docs consistency: OK
```

第八版是**文档扩充 + 数据采样语义修正 + 一致性修正**：

- 新增第 51 章“从 PyTorch 到 Megatron：渐进式迁移与验收”，原总结顺延为
  第 52 章；新增 8.11（非均匀/flexible PP、zero-bubble、DualPipe）、
  34.3.1（DualPipe 映射）、30.4.4–30.4.6（SSM scan/并行/混合比例）、
  32.2.2（Kubernetes PyTorchJob）、40.8.1（KV cache 量化）。
- `BatchProvider` 从“每个 DP rank 独立采样”改为“采样一个全局 batch，再按
  rank 取连续切片”。新增
  `test_data_parallel_ranks_partition_one_global_batch` 锁定“各 rank 并集
  = 单进程全局 batch”的不变量；pytest 从 117 增至 **123 passed**（数据回归 1 个 + 审计回归 5 个）。
- 对齐全部训练超参后（`tiny.json` 派生 DP=2 配置），DP=2/ZeRO-1 与单进程
  的 4 步 loss 与 grad norm 逐元素差为 0.0。注意 `configs/tiny_dp2.json`
  的 `warmup_steps=1`，与 `tiny.json` 的 2 不同，直接比较曲线会看到差异，
  这不是并行 bug。
- 修正文档中的一致性错误：README 总结章号、29.4.1 的 L/E 分级混用、
  10.3.6 的 MoE aux 截断口径、26.7 的 GDN/GDP 分类、05/31/G 的 sampler
  公式、22.4.1/11.5 的 flat 优化器“通信形状 vs 内存形状”、03/28 的启动与
  nsys 命令、01/14 的 462B 示例口径、25.6.1 的 `f_i` 分母、第 51 章的
  Embedding 通信方向与 flat clip 归约范围。
- 代码审计修复：全 mask 的 attention/MLA 行产生 NaN（现在 nan_to_num 为 0）；
  PP>1 的 loss 归约占位张量使用参数 device（CUDA 下避免 CPU/NCCL 混用）；
  checkpoint 重分片不再拒绝 EP=1 的复制专家（只拒绝 target EP>1）；
  `AuxFreeBalancer` 恢复 `update_speed/normalize`；paged cache 在空区间
  也校验 `which`。新增 5 个回归测试。

## 1. 历史记录：第七版复验（2026-11-22）

- 平台：Windows 11 Home China，CPU（无 CUDA）
- Python：3.14.6
- PyTorch：2.9.1+cpu
- NumPy：2.5.1，pytest：9.1.1
- 工作目录：`megatron-lm-tutorial/`

命令与结果：

```bash
python -m pytest -q
# 117 passed in 99.78s

python scripts/verify.py
# {"status": "ok", "elapsed_sec": 213.6, "cases": 25}

python scripts/check_docs.py
# chapters: 52 main + 14 appendices
# docs consistency: OK
```

第七版是**文档扩充 + 一个估算器语义修正**：新增附录 N（16 个故障注入
实验）、18.1.1 RoPE 外推、30.1.1 结构选择、34.4.1/34.4.2 稀疏注意力与 1M
验收、11.7.3 MoE 临时显存、25.12.1 `ep<dp` 专家副本、40.7.3 PP 推理、
38.8.1 生产数据接线、48.3.4 1B 估算和附录 M.15 Core 前向实验。

`analysis.py` 的 `gpu_hours` 现在返回**总 GPU-hours**（与集群大小无关），
新增 `cluster_hours` 返回墙钟小时；`estimate` 同时打印两者。修复前
`reference_1b.json` 会把 152.7 GPU-hours 打印成 19.1 GPU-hours。测试
`test_training_flops_and_mfu` 增加了精确断言与 `1/world_size` 关系；
`test_default_group_rank_is_the_global_rank` 锁定 `group=None` 的语义；另有
5 个回归测试覆盖代码审计发现的恢复/FP8/flat-TP/clip/checkpoint/head_dim 问题，
pytest 为 **117 passed**。`verify.py` 仍为 25 个验收用例（26 次子进程调用）。

`verify.py` 实际执行 26 次子进程调用，全部 OK。脚本报告的 `cases: 25` 不含
compile；下面把 compile 也列出，便于逐项对照：

1. `compileall` 编译检查；
2. 单元 + 等价性测试（pytest，含 EP/VPP/优化器重分片/flat optimizer）；
3. 单进程训练（4 步，写检查点）；
4. 单进程恢复（从第 4 步续到第 6 步）；
5. 从检查点生成；
6. VPP 等价性（pp=2, v=2 vs PP=1）；
7. TP=2；
8. PP=2（1F1B）；
9. PP=2（GPipe）；
10. CP=2；
11. DP=2（ZeRO-1）；
12. DP=2（flat distributed optimizer）；
13. MoE+TP=2；
14. EP=2（all-to-all）；
15. TP=2+PP=2（4 进程）；
16. TP=2+DP=2+ZeRO（4 进程）；
17. PP=2+DP=2（4 进程）；
18. CP=2+PP=2（4 进程）；
19. TP=2+CP=2（4 进程）；
20. TP=2+EP=2（4 进程）；
21. PP=2 检查点保存；
22. PP=2 恢复；
23. EP=2 检查点保存；
24. EP=2 恢复；
25. flat optimizer 检查点保存；
26. flat optimizer 恢复。

## 2. 数值等价性（与 TP=1/PP=1/CP=1 参考实现对比）

| 用例 | loss 误差 | 梯度/参数最大误差 | 说明 |
|---|---:|---:|---|
| TP=2 + SP | 0.0 | 1.86e-8 | 与 TP=1 逐元素 |
| TP=2 + SP + bias/norm | 0.0 | 1.94e-7 | 含 bias/norm 梯度 |
| PP=2（1F1B） | 0.0 | 0.0 | 与 PP=1 |
| CP=2 | 4.77e-7 | 2.79e-9 | 含 CP 全局 loss 与 K/V 逐元素梯度 |
| GPipe vs 1F1B | 最终 step loss 一致 | 最终 grad_norm 一致 | 只比较最后一步，不是逐步 |
| ZeRO-1 vs AdamW | 0.0 | 参数最大差 0.0 | DP=2 |
| **flat optimizer vs AdamW** | **0.0** | **参数最大差 0.0** | **DP=2, 3 步；grad_norm 差 2.38e-7** |
| **flat optimizer + TP=2/DP=2 + clip** | **0.0** | **参数最大差 0.0** | **全局 norm 与 `global_grad_norm` 差 <1e-5** |
| EP=2 vs 专家复制 | 0.0 | 4.66e-10 | 全局 batch 的 logits/aux/梯度 |
| interleaved VPP vs PP=1 | 0.0 | 0.0 | 全层/embedding/output 梯度 |

浮点误差会随 PyTorch/CPU 版本略有变化；“0.0”表示在该次运行中逐元素相等，
不是数学上的保证。

flat optimizer 的逐步结果（`tests/distributed_optimizer_equiv.py`）：

```json
{
  "steps": [
    {"step": 0, "loss_diff": 0.0, "param_max_diff": 0.0, "grad_norm_diff": 0.0},
    {"step": 1, "loss_diff": 0.0, "param_max_diff": 0.0, "grad_norm_diff": 2.38e-07},
    {"step": 2, "loss_diff": 0.0, "param_max_diff": 0.0, "grad_norm_diff": 0.0}
  ],
  "loss_diff": 0.0,
  "param_max_diff": 0.0,
  "grad_norm_diff": 2.38e-07
}
```

EP 的详细结果（`tests/parallel_ep.py`）：

```json
{
  "loss_diff": 0.0,
  "logits_diff": 1.19e-07,
  "aux_diff": 0.0,
  "router_diff": 2.33e-10,
  "q_diff": 2.33e-10,
  "expert_diff": 4.66e-10,
  "all_grad_diff": 3.73e-08,
  "aux_grad_diff": 1.30e-08
}
```

VPP 的详细结果（`tests/parallel_vpp.py`）：

```json
{"rank": 0, "loss_diff": 0.0, "grad_diff": 0.0, "num_chunks": 2}
```

## 3. 新增/变更模块的验证

| 模块 | 测试 | 验证内容 |
|---|---|---|
| `optim/distributed_optimizer.py` | `test_distributed_optimizer.py` (4)、`distributed_optimizer_equiv.py` (DP=2)、`flat_tp_equiv.py` (TP=2/DP=2) | flat padding/decay mask；单进程 3 步与 AdamW 逐元素相等；裁剪；state_dict 往返；布局不匹配拒绝；DP=2 参数/loss/grad_norm 一致；TP=2 的全局 norm/clip 与参考一致 |
| `parallel/ring_attention.py` | `test_ring_attention.py` (5) | cp=1/2/4/8 与 dense 因果一致；非因果；因果性；大 logits 数值稳定；反向梯度一致；负载统计 |
| `parallel/ulysses.py` | `test_ulysses.py` (7) | cp=1/2/4 与 dense 前向/反向一致；非因果；改未来 token 不影响过去；布局逆置换精确往返；`S/H` 不可整除报错；通信量 `4·B·S·H·D·(P-1)/P²` |
| `parallel/moe_balance.py` | `test_moe_balance.py` (6) | bias 方向/平衡后不动；选择与门控权重分离；state 往返；global counts no-op；node 预算；不可行预算报错 |
| `inference/paged_cache.py` | `test_paged_cache.py` (7) | 跨 block 边界 gather；K/V 分离；部分块；层锁步；释放复用；耗尽报错；碎片/内存公式 |
| `parallel/expert_parallel.py` | `test_expert_parallel_equivalence` | 变长 all-to-all dispatch/combine；全局 aux；专家梯度 `/ep`；TP×EP 冒烟 |
| `pipeline/interleaved_exec.py` | `test_interleaved_vpp_equivalence` | 环形 chunk 映射、`(mb,chunk)` 激活、反向路由；与 PP=1 全梯度一致 |
| `checkpoint_reshard.py`（优化器） | `test_optimizer_state_reshard_roundtrip` | AdamW `exp_avg/exp_avg_sq/step` 的 TP/PP 切分与合并逐元素一致 |
| GELU / tied embeddings | `test_gelu_fc1_fc2_sharding_roundtrip`、`test_tied_embedding_optimizer_state_roundtrip` | `fc1/fc2` 的 TP 切分；tied embedding 的优化器状态去重 |
| 旧 checkpoint | `test_pre_ep_checkpoint_metadata_defaults_to_ep1` | 缺少 EP 元数据的旧 checkpoint 按 EP=1 加载 |
| 复现 MoE 的 DP 同步 | `test_replicated_moe_dp_collective_symmetry` | 某专家在部分 rank 无梯度时，集合通信仍然对齐（零梯度物化） |
| SP + learned position | `parallel_equiv.py --case tp_learned` | 位置 embedding 的 TP 梯度归约 |
| `analysis.py` | `test_analysis.py` (11) | 参数与真实模型一致（SwiGLU/GELU bias、MoE active norms）；TP/PP/ZeRO/EP 显存缩放；激活（含 in-flight 峰值）/attention/KV/FLOPs/通信；GPU-hours 与墙钟小时的换算 |
| `checkpoint_reshard.py`（权重） | `test_checkpoint_reshard.py` (8) | TP=2/PP=2 分片拼接回全量 state dict 逐元素一致；重分片往返；flat/ZeRO 优化器状态被明确拒绝 |
| `data/packing.py` | `test_packing.py` (3) | 文档边界 label mask、position reset、padding mask、块对角 mask |
| `pipeline/interleaved.py` | `test_interleaved.py` (6) | chunk 归属/层映射、依赖校验、VPP 气泡小于非 interleaved；tied embedding + VPP 拒绝路径 |
| `generation_kv.py` | `test_generation_kv.py` (3) | cached logits 与全量前向逐步一致；KV cache 生成与全量重算 greedy 输出一致 |
| `comm.py` | `test_comm.py` (2) | 相同 rank 集合的 group key 不再冲突；`group=None` 的组内 rank 等于全局 rank |
| TP 初始化 | `test_init.py` (4) | 不同 TP rank 的分片权重不同；同一 rank 可复现；同 rank 多层不重复；复制参数跨 TP 一致 |
| `parallel/mla.py` | `test_mla.py` (6) | 形状/latent cache 布局；因果性；prefill+decode 与全量前向一致；两条 query 路径反向；padding mask |
| RoPE cache | `test_rope_cache.py` (2) | 设备变化 + 短序列重建；interleaved 形状/配对 |
| CE fp32 | `test_cross_entropy.py` (5) | fp32 log-sum-exp；bf16 loss/grad dtype；ignore_index；z-loss |

### 3.1 flat distributed optimizer 的要点

- `master`/`grad_flat` 是 padded 到 `world` 整数倍的连续 fp32 buffer；pad 区
  的梯度为 0、decay mask 为 0，散回参数时截断；
- 反向结束后 `reduce_scatter_tensor` 得到本地 shard 的 DP 平均梯度，再在
  shard 上做 AdamW；
- 全局梯度范数是各 shard 范数平方和的 all-reduce 再开方；
- 参数更新后 `all_gather` 所有 shard，拼回 full buffer 并散回各参数；
- 检查点保存本地 master/exp_avg/exp_avg_sq/step 与布局元数据；加载时
  all-gather 重建完整 master，校验 `world`/`padded_numel`；
- mini 限制：单 bucket、同步通信、无梯度分片（ZeRO-2）、无多 bucket 重叠、
  不支持 EP + distributed optimizer。

### 3.2 ring attention 的要点

- 单进程模拟 `cp` 个 rank，KV 块按 `(r-step)%cp` 倒序流动；
- 因果模式下 `j > r` 的整块跳过；`j == r` 用块内三角 mask；
- online softmax 维护 `m/l/acc`，第一块 `alpha=0` 不会污染累加器；
- cp=8 时 `blocks_per_rank=[1..8]`，暴露朴素连续切分的 8× 负载不均；
  生产用 load-balanced causal mask；
- 测试覆盖反向：`square().mean().backward()` 后 q/k/v 梯度与 dense 一致。

### 3.3 aux-free / node-limited 的要点

- `bias` 只影响 top-k 选择，不影响门控权重；
- `bias += speed * sign(mean(counts) - counts)`；`counts` 必须是全局（EP 下
  all-reduce）；
- 更新速度过大时会出现整块翻转/振荡；demo 用 `0.005`，200 步把最大负载从
  3240 降到 1124（理想 1024）；
- `node_limited_topk` 在超预算时屏蔽“最强专家分数最低”的节点并重选；
  `max_nodes × experts_per_node < k` 直接报错。

### 3.4 paged KV 的要点

- 每个序列维护 block table；逻辑位置 `p` 映射到
  `table[p // block_size][p % block_size]`；
- K/V 分开存储；`gather` 返回 `[heads, tokens, dim]`；
- 同一 token chunk 必须写完所有层（层锁步），长度才对读者可见；
- 释放后 block 回 free list；耗尽抛 `MemoryError`；
- 碎片公式 `1 - used_tokens / (blocks × block_size)`；测试用 33 token/16
  块验证 31% 尾部碎片。

## 4. 测试覆盖的语义

- TP=2 与 TP=1 的 loss 和梯度等价（误差 < 1e-5）；
- PP=2 与 PP=1 的 loss 和梯度等价；
- CP=2 与 CP=1 的 loss 和梯度等价（含 K/V 逐元素梯度）；
- EP=2 与专家复制 MoE 的 loss/logits/aux/梯度等价；
- interleaved VPP（pp=2, v=2）与 PP=1 的 loss/梯度等价；
- GPipe 与 1F1B 在相同权重、相同数据下最终 step 的 loss/grad_norm 一致；
- flat optimizer 与 AdamW 在 DP=2 下参数/loss/grad_norm 一致；
- 单进程、2 进程、4 进程的多种并行组合可完成前向/反向/优化器更新；
- 检查点保存与恢复（单进程 / PP=2 / EP=2 / flat DP=2）、生成与 CLI 冒烟；
  **多进程 ZeRO-1 恢复未覆盖**；
- packing/document mask、VPP 规划、KV cache、权重重分片、通信 group 隔离；
- 估算器的激活峰值按调度器的 in-flight micro-batch 数计算（1F1B=`pp-1`，
  GPipe=`m`）；
- AdamW 状态重分片往返；
- MLA 因果性/cache 等价/反向；RoPE cache 设备变化；
- ring attention dense 等价/反向/负载；aux-free 方向/预算；paged KV
  跨块/复用/碎片。

## 5. 平台与后端说明

- Windows 上多进程使用 `mini_megatron/parallel/comm.py` 的 portable Queue 后端；
  这避免了部分 PyTorch Windows 轮子的 Gloo 初始化问题。
- Linux 生产路径为 `torch.distributed`（Gloo/NCCL）；`scripts/run_distributed.py`
  与 `torchrun` 都可启动。
- CPU 上 `tokens_per_sec` 只用于比较配置趋势，不代表 GPU 吞吐。
- ring attention / paged cache / aux-free 都是单进程参考；没有真实 P2P、
  paged-attention kernel 或 EP 主链集成。

## 6. 已知不覆盖的内容

- 这些数字验证的是 mini 实现的**语义正确性**，不代表生产 Megatron 的吞吐/规模。
- EP：只支持 `ep = dp`，没有专家副本；dispatch 用 padding 而非 split sizes；
  专家 GEMM 是 Python 循环，没有 grouped GEMM；没有 all-to-all 与计算重叠。
- CP + MoE/EP：aux loss 只在本地 CP 序列切片上统计，没有跨 CP 的全局
  `f_i/P_i` 归约；CP 与 EP 的组合没有数值等价性测试。
- SP + learned position embedding 的 TP 梯度归约已补 hook 与测试；但
  embedding/output 的 tied 参数、GELU `fc1/fc2` 只在重分片测试中覆盖。
- VPP：是可执行的 interleaved GPipe，不是 interleaved 1F1B/zero-bubble；
  没有接进 `Trainer`/checkpoint，没有通信重叠。
- 检查点：同布局恢复；权重与非 ZeRO AdamW 可 TP/PP 重分片；flat optimizer
  只支持同 DP size；ZeRO/FSDP2 优化器状态与 EP 专家重排不支持。
- 不包含：Transformer Engine fused kernel、CUDA Graph、真正的 ring attention
  通信、连续批处理/prefix cache、任意并行 order。
- MLA：`parallel/mla.py` 是单进程参考，没有 TP/CP、FP8、吸收推理 kernel，
  也不接入 `Trainer`/checkpoint；`test_mla.py` 只验证单进程语义。
- ring attention：单进程算术参考，没有 P2P send/recv、没有与 attention
  计算重叠、没有 load-balanced causal mask 的执行器。
- paged cache：只有数据结构/allocator，没有 paged-attention kernel、
  prefix sharing 的引用计数、连续批处理调度器。
- aux-free/node-limited：独立模块与测试，未接入 `MoELayer`/
  `ExpertParallelMoELayer` 的训练主链；全局统计只提供 helper，未在 EP 训练
  中端到端验证。
- 数据：训练主链不传 `attention_mask`，`data/packing.py` 未接入训练循环；
  因此 packed/document mask 的“模块测试通过”不等于“训练链正确”。
- TP+CP 的验证是 2 步训练冒烟（`tiny_tp2_cp2`），不是逐元素等价性测试。
- 附录 G 描述的 TP/DP 分离 RNG tracker 属于生产 Megatron；mini 只对参数
  初始化实现 TP/PP 偏移 seeding，训练期 RNG tracker 未实现。

## 7. 第五轮审计修复

第五版在新增模块之外，修正了以下文档/代码问题：

| 问题 | 影响 | 修复/回归 |
|---|---|---|
| `analysis.py` GELU bias 按 3 个投影计数 | 参数量高估 | 按 SwiGLU/GELU 分支；`test_gelu_bias_parameter_count_matches_real_model` |
| MoE active 参数漏算 `4h` norms | 激活参数低估 | 加 `4h`；精确断言 `total-active=(E-k)·expert·L` |
| CE 在 bf16 下直接算 exp/log | 数值不稳 | forward/backward 用 fp32，梯度 cast 回；bf16 契约测试 |
| 04 章 tiny 激活示例 `h` 与数字不符 | 读者代入公式对不上 | 改为 `h=64` 并给出 2816 B |
| 14 章 “11.3 的例子” 交叉引用错 | 指向错误章节 | 改为 11.7 |
| 01 章 70B/462B 的 token/param 标签反了 | 结论误导 | 按 28.6/21.6 重写 |
| 37 章 NVLink/PCIe 单位口径 | 带宽比高估 2× | 统一为每方向/双向并说明约 7× |
| 10 章 NVLink 400 GB/s 未说明单向 | 通信时间差 2× | 标注“每方向、保守取 400” |
| 44 章 自建/云 break-even 与情景表矛盾 | 结论错误 | 改为 `u*` 公式；基准情景云便宜约 44% |
| 43 章 “数百万美元级” 未写口径 | 夸大 | 改为单任务/年化两种口径 |
| G 附录 EP RNG 前后矛盾 | 读者无法判断 | 统一为“以源码为准，不假设独立流” |
| 42 章前 4 个案例与 27.5 近重复 | 阅读冗余 | 改为“规模化增量”表并指向 27.5 |
| 31/38 章 packing/sampler 重复 | 阅读冗余 | 31 保留配方视角，实现细节指向 38 |
| 15 章依赖图方向错 | 初始化顺序误导 | 按实际 import 重画 |
| 17/26/20 章对 hook/generator/CE 的描述过强 | 与代码不符 | 按实际条件改写 |

## 8. 第七轮修订（内容扩充与估算器语义）

| 问题 | 影响 | 修复/回归 |
|---|---|---|
| `gpu_hours` 把墙钟小时标成 GPU-hours | 1B 估算 152.7 GPU-hours 被读成 19.1；采购/排期差 8× | 拆成 `gpu_hours` 与 `cluster_hours`；`estimate` 同时打印；`test_training_flops_and_mfu` 锁定语义 |
| A.5/4.4.4 的 ZeRO-1 公式把权重/梯度也除了 `d_z` | 70B 状态低估 2.75×（17.5 vs 48.1 GB/卡） | 改为 `4N/(tp·pp) + 12N/(tp·pp·d_z)` |
| 10.1 MoE 总参数倍数分子漏共享部分 | 可能算出 <1 的“倍数” | 改为 `(N_shared+E·N_expert)/(N_shared+N_expert)` |
| 24 章 DCP 异步 API 名不存在 | 照抄会报错 | 改为 `async_save`/`AsyncStager` |
| 29 章 SSM 路径与 decoder spec 返回类型 | 读源码/写代码会找错位置 | `core/ssm/`；区分 `get_gpt_decoder_layer_specs`（list）与 `get_gpt_decoder_block_spec`（block submodules） |
| 8.10 测试覆盖范围夸大 | 读者以为 4 进程组合在 pytest 里 | 改为 7 组 2 进程 + verify 的 4 进程矩阵；GPipe/1F1B 只比最终 step |
| 37.7 PP p2p 写成 2bsh | 通信账少一半 | 改为 4bsh（前向激活 + 反向梯度） |
| 17.6 LM head 形状自相矛盾 | 照抄会形状错 | 拆成 SP gather / 不 gather 两行 |
| A.16 MLA 吸收的 `W_UK` 转置与 30/K 不一致 | 实现会乘错矩阵 | 统一为 `q_nope_i · W_UK_i`，并注明两种布局 |
| 15 章声称 `kv_cache_bytes` 支持 MLA | 读者以为有 MLA 分支 | 改为 MHA/GQA；MLA 指向 30.5 |
| 40.7.2 EP all-to-all 多除 `ep` | 容量规划低估 `ep` 倍 | 改为 ch10.6 的聚合/每卡公式 |
| 16/22 两处实现细节 | 与源码不符 | group key 加 `sorted`；decay mask 加 `shape[-1]==1` |

### 代码审计修复（第七轮）

独立代码审计（不读文档、直接对照 dense 参考与实跑）发现并修复了 7 个问题；
前 6 个都有永久回归测试：

| 问题 | 影响 | 修复/回归 |
|---|---|---|
| `Trainer.fit` 恢复从 `last+1` 开始 | checkpoint `step_000004` 表示 4 次已完成更新，恢复却从 5 开始，**静默跳过一步** | 改为 `start=last`；`test_two_steps_and_checkpoint` 改为逐步对比未中断轨迹 |
| `simulate_fp8` 的 backward 也过 E4M3 | 权重梯度 ~1e-5 下溢为 0，FP8 模拟让**所有梯度归零** | 改为 straight-through estimator；`test_fp8_simulation_preserves_gradient` + 非零梯度断言 |
| flat optimizer 的 `grad_norm` 只 all-reduce DP | TP/PP 的 disjoint shard 未合并，clip 系数错；TP=2/DP=2 下与参考不一致 | 全 world 归约 + TP 复制权重 1/tp + CP 去重；新增 `tests/flat_tp_equiv.py`（4 进程） |
| `checkpoint_reshard` 把 flat 优化器状态当空 | 静默丢弃 Adam m/v 与 param_groups | `optimizer_state_by_name` 拒绝非标准 state_dict；加载路径同时拒绝 `master_shard`/`format` |
| `estimate` 的 1F1B in-flight 写成 `min(m,pp)` | 实现的实际峰值是 `pp-1`，pp=2 时高估 2× | 改为 `max(1,min(m,pp-1))`；测试改为断言 1 |
| RoPE 的奇数 head_dim 未校验 | 校验通过、首次前向才 shape error | `ModelConfig.validate` 要求 head_dim 偶数 |
| flat optimizer buffer 硬编码 CPU | NCCL 下 GPU 训练会失败（本机 CPU 无法复现） | buffer/临时 tensor 跟随模型 device |

这些修复改变了行为，所以 pytest 从 **112 增加到 117**（+5 个回归测试），
`verify.py` 的用例数不变。

新增内容不改变训练主链：附录 N 是实验手册；18.1.1/30.1.1/34.4.x 是文档；
11.7.3/25.12.1/40.7.3/38.8.1 是账本与接线；`configs/reference_1b.json` 只用于
`estimate`。

## 9. 第六轮修订（文档与估算器）

| 问题 | 影响 | 修复/回归 |
|---|---|---|
| 48 章 `--train-iters 200000` 与 24B token 预算不符 | 读者会训练 210B token | 改为 22,888 步并给出换算 |
| 44/I 的 1000×H100 功耗口径不一致 | 年电费差约 60% | 统一 9 kW/节点，I 改为约 $1.02M/年 |
| HSDP 写成“节点内复制、节点间分片” | 分层通信/显存收益全错 | 改为“节点内分片、节点间复制”，同步 50/22 |
| reduce-scatter 说成与 all-reduce 等量 | 通信账高估 2× | 改为 RS 为一半，RS+AG 才等于 all-reduce |
| LayerNorm 融合说总访存降到 1/3 | 带宽收益高估 | 改为总访存 1/2；只计读才是 1/3 |
| 08 与 21 对 `max(1, warmup)` 解释矛盾 | 调度依赖理解错误 | 统一为“循环写法的补偿，不是依赖” |
| μP/PyTorch 初始化方差写成 `1/fan_in` | 初始化/LR 迁移结论错 | 改为 PyTorch `≈1/(3 fan_in)`、μP `gain²/fan_in` |
| EP 通信把聚合字节除以单卡带宽 | 时间高估 `ep` 倍 | 明确聚合/每卡口径，改为每卡 6.3GB、约 16ms/125ms |
| 激活峰值只算一个 micro-batch | PP 峰值低估 `pp`/`m` 倍 | `analysis.py` 加 in-flight 峰值并打印 |
| ZeRO-2 表漏参数同步 | 读者以为 ZeRO-2 不需通信 | 补“更新后 all-gather/广播”，统一三张表 |
| offload 用单卡 840GB 计算 | DP/ZeRO 下高估 `dp_z` 倍 | 加 `840/dp_z` 与 DP=8 对照 |
| 代码片段与仓库不一致 | 照抄会越界/形状错 | 修 CE mask/clamp、Embedding mask、attention contiguous、communicator device、all_reduce_gradients 签名 |
| `analysis.py` 的 `a`/`k` 符号冲突、EP 总参数“E 倍” | 公式/账本误读 | 统一 `n_h`、`c_act`，改为 `N_total=N_shared+E·N_expert` |
| 附录 K 链接跳 H、final norm 命名、L 练习重号、35 的 L1–L4 冲突 | 导航/命名/分级混乱 | 全部修正；35 改用 S1–S4 |
| README 把 roadmap 写成“已发布”、Qwen3.5-VL 断言 | 证据分级错误 | 改为 L3 信号、删除未核实型号 |

新增的文档检查：

```bash
python scripts/check_docs.py
# chapters: 52 main + 13 appendices
# docs consistency: OK
```

它检查章号/附录连续性、SUMMARY 覆盖与重复、相对链接、`第 N 章`/`N.M`
引用范围、README 的章数/附录范围；不能替代内容审计，但能抓住“加了一章只
改了一半导航”的回归。

## 10. 如何复验

```bash
python -m compileall -q mini_megatron tests scripts
python -m pytest -q
python scripts/verify.py
python scripts/check_docs.py
```

如果 `pytest` 全绿但 `verify.py` 失败，优先看多进程启动日志与残留进程；如果
两者都失败，先确认 `PYTHONPATH` 指向本目录、CPU 线程数已限制
（`OMP_NUM_THREADS=1`），并清理 `.verify_tmp/` 后重试。

单项复验：

```bash
python tests/parallel_ep.py --world-size 2 --out ep.json
python tests/parallel_vpp.py --world-size 2 --out vpp.json
python tests/parallel_equiv.py --case tp --world-size 2 --out tp.json
python tests/distributed_optimizer_equiv.py --out dopt.json
python scripts/ring_attention_demo.py
python scripts/moe_balance_demo.py
```
