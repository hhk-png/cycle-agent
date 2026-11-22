# 第 27 章 测试与数值等价性

## 27.1 为什么“能跑”不等于“对”

并行实现有三种常见状态：

1. 跑不起来（shape/通信错误）——容易发现；
2. 跑起来但 loss 不降（梯度错误/数据错误）——较难；
3. loss 能降，但与单卡参考实现有系统性偏差（漏 reduce/缩放错）——最难。

第 3 种最危险：小模型/短训练可能看不出，放大后才暴露。因此必须做
**数值等价性测试**：相同权重、相同数据、相同 mask，对比 loss 和梯度。

## 27.2 测试金字塔

```text
单元测试
  ├─ config 校验
  ├─ 线性层/embedding world=1
  ├─ CE vs F.cross_entropy
  ├─ 数据集/优化器/调度
  └─ 模型前向/反向
集成测试
  ├─ Trainer 2 步 + 检查点
  ├─ CLI 冒烟
  └─ 生成
多进程等价性
  ├─ TP=2 vs TP=1
  ├─ PP=2 vs PP=1
  ├─ CP=2 vs CP=1
  └─ TP+bias/norm 梯度
多进程冒烟
  ├─ TP/PP/CP/DP/MoE
  ├─ 4 卡组合
  └─ GPipe vs 1F1B
```

## 27.3 等价性方法

`tests/parallel_equiv.py` 的流程：

1. 在 world=2 的每个 worker 里，先不初始化并行组，构建 TP=1/PP=1 参考模型；
2. 用固定 seed/数据算 loss、反向、保存关心的梯度；
3. 初始化 TP/PP/CP group，构建并行 stage；
4. 用 `sharding.py` 把参考 state dict 切片加载；
5. 算同样的 loss/梯度；
6. 对比。

为什么每个 worker 都建参考模型：避免跨进程传大 state dict；同时保证参考模型
的权重与并行 stage 完全一致。

`make_stage_state` 的规则：

- embedding/output：词表行；
- q/k/v/gate/up：输出行；
- out/down：输入列；
- norm/router：复制；
- PP：选择层并重命名。

## 27.4 容差

| 用例 | loss | 梯度 |
|---|---:|---:|
| TP | <1e-5 | <1e-5 |
| TP+bias/SP | <1e-5 | <1e-5 |
| PP | <1e-5 | <1e-5 |
| CP | <2e-5 | <1e-5 |

CP 的 loss/logits 可能有 1e-7 量级的浮点差异（softmax/归一化顺序），梯度在
修复 K/V all-reduce 后也能到 1e-8 量级。不要用 `==`；也不要把容差放到 1e-2，
那会掩盖真实 bug。

## 27.5 测试抓到的真实 bug

### bug 1：Column Parallel 漏 grad_input all-reduce

现象：TP=2 的 loss 与 TP=1 **完全一致**，但 out_proj 梯度范数只有参考的 0.68。
原因：`F.linear` 的 backward 只算本地 head 的输入梯度，没有跨 TP 求和。
修复：`_ColumnParallelLinearFn.backward` 加 all-reduce。梯度误差降到 1e-8。

### bug 2：SP 下 attention 的序列长度

现象：TP=2+SP 报 `shape '[2,16,2,16]' invalid for input of size 2048`。
原因：q_proj 先 all-gather 了序列，q 的序列长度是 `s`，但代码用输入的 `s/t`
reshape。
修复：用 `q.size(1)`。

### bug 3：SP reduce-scatter 切错维度

现象：残差相加 shape 不匹配（64 vs 32）。
原因：对 hidden 维 reduce-scatter，得到 `[b,s,h/t]`，应为 `[b,s/t,h]`。
修复：all-reduce 后按序列维 chunk。

### bug 4：CP K/V 梯度漏 all-reduce

现象：CP=2 的 q_proj 梯度接近，但 k/v 梯度范数只有参考的 0.85，LayerNorm 梯度
0.74。
原因：all-gather K/V 的反向只 chunk，没有把其他 query rank 的贡献求和。
修复：backward 先 all-reduce grad_output，再 chunk。误差降到 1e-8。

### bug 5：PP 用局部 rank 当 global rank

现象：PP=2,DP=2 死锁在 `recv`。
原因：PP group `[0,2]`，local rank 0 的 next 是 global 2，不是 1。
修复：`comm.get_global_rank(group, local)`。

### bug 6：TP 复制参数的梯度范数重复计数

现象：TP=2+SP 的 loss/梯度都等价，但 `global_grad_norm` 比参考大约 1.3 倍。
原因：LayerNorm/Row bias/router 在 TP 组内复制，每个 rank 都有完整梯度，
全局范数把它们计了 `tp` 次。
修复：标记 `_is_replicated`，求平方和时除以 TP；TP/PP 的验证用例现在也对比
`gradnorm_diff`。

### bug 7：autocast 下 Column backward dtype 不匹配

现象：bf16 autocast 报 `expected m1 and m2 to have the same dtype`。
修复：backward 把 grad_output 转成 weight dtype，grad_input 转回 x dtype。

### bug 8：EP combine 的截断方向写反

现象：EP=2 训练 loss 正常，但 `parallel_ep.py` 的 `logits_diff` 是 0.025
（容差 1e-5）。原因：combine 返回块按 `counts_all[src][rank]` 截断，正确值
是 `counts_all[rank][src]`（自己发给 peer 的数量）。修复后 `logits_diff`
降到 1.2e-7。

### bug 9：VPP 本地 chunk 交接的 `.grad` 为 None

现象：pp=1、v=2 时反向抛 `KeyError`。原因：本地交接直接存上游图张量，
下游拿到的不是叶子，`.grad` 为 None，梯度交接被跳过。修复：本地交接也
`detach()`，与远程路径一致。

### bug 10：`position_embedding.weight` 被当成词表 embedding 切分

现象：SP+learned position 的 TP=2 加载报 shape mismatch（16 vs 32）。
原因：`_tp_shard` 用 `endswith("embedding.weight")` 匹配，误伤了
`position_embedding.weight`。修复：词表 embedding 用精确 key 匹配。

### bug 11：MoE 未激活专家导致集合通信数量不齐

现象：复现 MoE + DP=2 在部分 step 挂起/超时。原因：某专家在一个 rank 有
梯度、另一个 rank 为 `None`，`all_reduce_gradients` 跳过 `None`，两边的
collective 数量不同。修复：MoE 模型在同步前把 `None` 梯度物化为 0，保证
所有 rank 参与相同的集合通信。

### bug 12：EP aux 梯度被低估 `1/ep`

现象：aux 的**数值**与参考一致，但 router 的 aux 梯度偏小。原因：
`_AllReduceSum` 的反向是恒等，而 forward 是 sum 后除 ep；DP 又平均一次。
修复：反向对上游梯度做 SUM all-reduce。新增 `aux_grad_diff` 断言（1.3e-8）。

### bug 13：全 mask 的 query 行产生 NaN

现象：给 `attention_mask` 传入标准左 padding（某行所有 key 都是 0）时，
loss 变成 `nan`，所有梯度也变 `nan`；去掉 mask 后同一批数据正常。
原因：`softmax` 的分母是 0（整行 `-inf`），返回 NaN，随后污染残差流。
修复：softmax 后做 `nan_to_num(nan=0)`，与 fused attention 的“全 mask 行
输出 0”语义一致；MLA（`parallel/mla.py`）同样修复。回归测试：
`test_fully_masked_query_row_is_finite`、`test_mla_fully_masked_row_is_finite`。

这个 bug 的教训是：**“mask 的形状对”不等于“mask 的语义对”**。训练主链目前
还没接 `attention_mask`，所以它不会在训练 loss 里暴露；但一旦接 packing/
padding，它就是第一个炸的点。

### bug 14：PP>1 的 loss 归约混用 CPU/CUDA 张量

现象（审计推断，CPU 环境未能直接复现）：PP>1 + CUDA 时，非最后一段没有
loss，`_finish_stats` 用 `torch.zeros(())` 造了一个 **CPU** 占位张量；
最后一段的 loss 在 GPU 上。两者进入同一个 `all_reduce`，NCCL 拒绝 CPU
张量，Gloo 又拒绝 CUDA 张量。
原因：占位张量的 device 没有跟随模型。
修复：`BaseSchedule` 记录 `next(stage.parameters()).device`，`_finish_stats`
与 `forward_only` 的零张量都用这个 device。CPU 测试无法覆盖这个分支，所以
它属于“必须靠代码审计/GPU smoke 补”的一类问题。

> 上述 0.68、0.85、0.74、1.3×、0.025 等具体数字来自当时的调试记录，用于
> 说明“症状长什么样”；当前测试断言的是**误差上界**（如 1e-5/1e-7），不是
> 复现这些比值。硬件/库版本变化会让浮点数字漂移，但“loss 对、梯度错”的
> 模式不变。

这些 bug 都不是“语法错误”，而是并行语义错误。只有梯度等价性测试能稳定抓住。
新增并行功能时，建议按同样的模板记录：现象、最小复现、根因、修复、回归
断言。即使具体数字不可复现，排查路径仍然可复用。

## 27.6 GPipe vs 1F1B

相同 seed/权重/数据下，两种 schedule 的最终 loss/grad_norm 应该一致（允许
浮点误差）。`tiny_pp2.json` 与 `tiny_pp2_gpipe.json` 的对比是回归测试：如果
schedule 的 loss 缩放/梯度传递有错，两者会分叉。

注意：当前测试比较的是**最后一个 step** 的汇总值，不是逐步 history。若要
逐步对比，需要让两个进程在每个 step 交换 loss 并断言；这会增加同步开销，
教学实现暂未做。

## 27.7 Windows/portable 测试

`scripts/run_distributed.py`：

- 创建每 rank 的 Queue；
- `spawn` 子进程；
- 先初始化 portable comm，再调用 CLI；
- 所有 collective/p2p 语义与 torch 后端一致。

这使得 `pytest` 可以在没有可用 Gloo 的 Windows 上跑多进程测试。生产 Linux
仍应测试 torch 后端（`torchrun` + NCCL/Gloo）。

## 27.8 测试命令

```bash
# 全量
python -m pytest -q

# 只跑单元测试
python -m pytest -q tests/unit

# 只跑某个等价性用例
python tests/parallel_equiv.py --case tp --world-size 2 --out tp.json

# 完整验证（含多进程矩阵/检查点/生成）
python scripts/verify.py
```

## 27.9 新增模块的测试

除并行等价性外，还有一组“结构性/数值性”测试；当前全量为 **130 passed**：

| 模块 | 测试 | 抓什么 |
|---|---|---|
| `analysis.py` | 参数量与真实模型一致（SwiGLU/GELU bias、MoE active norms）；激活峰值含 in-flight；GPU-hours 与墙钟小时 | 公式抄错/模型结构变化/峰值低估/工作量与时间混用 |
| `optim/distributed_optimizer.py` | flat optimizer vs AdamW（单进程 3 步 + DP=2） | padding/offset/decay mask、shard 范数、all-gather 错位 |
| `parallel/ring_attention.py` | cp=1/2/4/8 vs dense、反向、负载统计 | online softmax 更新、全局因果 mask、块跳过 |
| `parallel/moe_balance.py` | bias 方向、选择/权重分离、node 预算 | aux-free 符号错、node-limited 约束失效 |
| `inference/paged_cache.py` | 跨块 gather、K/V、复用、层锁步 | block table 映射、部分块、释放泄漏 |
| `checkpoint_reshard.py` | 权重与 AdamW 状态的分片→全量→分片逐元素一致 | TP/PP 切分方向/优化器索引错误 |
| `data/packing.py` | 文档边界/position/loss mask | 跨文档污染 |
| `pipeline/interleaved.py` | chunk 映射/依赖校验 | VPP 调度顺序错误 |
| `pipeline/interleaved_exec.py` (`parallel_vpp.py`) | VPP 与 PP=1 的 loss/全梯度一致 | 环形 p2p、`(mb,chunk)` buffer、反向路由 |
| `parallel_ep.py` | EP 与专家复制 MoE 的 loss/logits/aux/梯度一致 | dispatch/combine 索引方向、专家梯度缩放、aux 梯度 |
| `parallel_equiv.py --case tp_learned` | SP + learned position 的 TP 梯度 | 位置 embedding 被误当词表切分 |
| `tiny_moe_dp2` | 部分 rank 无专家梯度时仍能完成 DP 同步 | `grad=None` 导致集合通信数量不齐 |
| `test_gelu_fc1_fc2_sharding_roundtrip` | GELU MLP 的 TP 切分 | `fc1/fc2` 未进切分表 |
| `test_tied_embedding_optimizer_state_roundtrip` | tied embedding 的优化器状态去重 | 参数别名导致索引错位 |
| `generation_kv.py` | cached logits vs 全量前向 | 位置/拼接/TP head 切分错误 |
| `test_init.py` | TP 分片不同/同 rank 可复现/复制参数一致 | “同一 seed 建分片”导致权重复制 |
| `test_comm.py` | 相同 rank 集合的 group key 不冲突；`group=None` 的组内 rank = 全局 rank | CP/PP/EP group 串消息；reduce-scatter 每个 rank 都取 chunk 0 |
| `training.py` 恢复 | 恢复后的逐步 loss/grad_norm 与未中断训练一致 | checkpoint step 语义 off-by-one（恢复跳过一步） |
| `simulate_fp8` | forward 量化、backward 为 STE 且梯度非零 | FP8 模拟把梯度量化成 0 |
| `flat_tp_equiv.py` | TP=2/DP=2 的 clip norm 与参考全局范数一致 | flat optimizer 只对 DP 求范数，忽略 TP/PP |
| `checkpoint_reshard.py` | flat/ZeRO 优化器状态被明确拒绝 | 静默返回空 Adam 状态 |
| `config.py` | RoPE 要求偶数 head_dim | 奇数 head_dim 在校验后、前向时才崩 |
| `test_mla.py` | 因果性/prefill+decode cache 等价/padding mask/反向 | latent cache 语义、RoPE 重复应用、mask 错位 |
| `test_rope_cache.py` | 设备变化 + 短序列重建 | 旧 cache 长度与 reshape 不一致 |
| `test_interleaved.py`（tie+VPP） | tied embedding + VPP 拒绝路径 | 未定义变量导致 `NameError` 而非明确拒绝 |

EP 和 VPP 的等价性用例都对比了**全局 batch/全梯度**，而不是只看本地 loss；
这是它们能抓到“索引方向写反”这类静默 bug 的原因。

这些测试的共同思路：**不要只验证“能跑”，要验证“与一个更简单的参考实现
一致”**。布局/缓存/调度的 bug 往往不报错，只会让数值慢慢偏。

### 27.9.1 第七轮审计新增的回归

第七轮独立代码审计又抓到了几类“测试假绿”：

| 问题 | 旧测试为什么没抓到 | 新回归 |
|---|---|---|
| 恢复从 `last+1` 开始，跳过一步 | `evaluate(0) == evaluate(0)` 是同一表达式 | 恢复轨迹与未中断轨迹逐步对比 |
| FP8 模拟的 backward 也被量化，梯度全 0 | 只断言 `finite`，0 也 finite | `simulate_fp8` 的 STE 梯度为 1 |
| flat optimizer 的 clip norm 只看 DP | 等价性测试只有 DP=2；单进程 clip 时 local=global | `flat_tp_equiv.py`（TP=2/DP=2）对比全局 norm 与参数 |
| flat checkpoint 被重分片当空状态 | 没有“拒绝 shard 状态”的测试 | `optimizer_state_by_name` 抛错 |
| 奇数 head_dim 通过校验 | 只测了整除关系 | `validate` 要求 head_dim 偶数 |
| `group=None` 的组内 rank 返回 0 | 单 rank 下 0 恰好正确 | 多 rank portable 下断言 rank=1 |

共同教训：**“finite”和“退出码 0”是最弱的断言**。恢复要比轨迹，clip 要比
全局范数，量化要比梯度，缓存要比 dense 参考；只验证“没崩”会漏掉静默错误。
对应的“亲手弄坏一次”实验见附录 N。

## 27.10 本章小结

- 并行正确性必须对比 loss 和梯度，不能只看 loss。
- 参考实现 + 权重切片 + 固定数据是可靠的验证方法。
- 容差要小到能抓 bug，又允许浮点顺序差异。
- 测试抓到的 bug 集中在“漏 reduce/切错维度/局部 vs 全局 rank”。
- GPipe vs 1F1B、TP/PP/CP/EP/VPP 等价性是回归测试的核心。

下一章讨论性能剖析与调优实验。
