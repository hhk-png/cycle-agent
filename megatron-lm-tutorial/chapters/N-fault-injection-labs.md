# 附录 N 故障注入与数值对照实验手册

> 第 27 章记录的是“我们踩过并修好的 bug”，本附录是“你自己把不变量弄坏，
> 看测试怎样抓住它”。前者是病历，后者是实验课。

## N.0 为什么“故意弄坏”

并行实现的 bug 有一个共同特征：**前向 loss 往往仍然正常**。漏一次
all-reduce、切错一个维度、把 local rank 当 global rank，训练曲线可能只是
慢一点或抖一点，甚至完全看不出异常；等到评测/长上下文/换并行度时才暴露。

读代码只能建立“应该有这个 all-reduce”的印象；把它删掉、跑一遍、看哪个
断言变红，才能建立“没有它会发生什么”的直觉。本附录的每个实验都遵循
同一模板：

```text
不变量 → 注入 → 运行 → 预期症状 → 根因/修复 → 生产对应指标
```

## N.1 实验环境与安全约定

- 所有命令都在 `megatron-lm-tutorial/` 根目录执行；
- 注入只在**副本**上做：先 `cp` 到系统临时目录，或 `git stash` 后恢复；
  不要把坏代码留在工作区；
- 多进程测试用 portable 后端，Windows/Linux 都可跑；
- 每个实验先记录**基线**（未注入时测试通过），再注入；
- 出现挂起时，先杀进程，再看 `.verify_tmp/` 与子进程日志；portable 后端
  默认 180 秒超时，pytest 外层另有超时。

一个安全的操作模板：

```bash
# 备份（Windows 用 %TEMP%，Linux/macOS 用 /tmp）
cp mini_megatron/parallel/layers.py "$TEMP/layers.py.bak"
# 注入、运行、观察
python tests/parallel_equiv.py --case tp_bias --world-size 2 --out "$TEMP/tp.json"
# 恢复
cp "$TEMP/layers.py.bak" mini_megatron/parallel/layers.py
```

> 不要用“跑全量 `verify.py`”做注入实验：一个失败会中断后续用例，且 200 秒
> 的矩阵会掩盖最小复现。每个实验只跑一个最小命令。

## N.2 实验索引

| # | 破坏的不变量 | 注入位置 | 最小命令 | 典型症状 |
|---|---|---|---|---|
| 1 | Column 反向的 grad_input 求和 | `parallel/layers.py` | `parallel_equiv.py --case tp_bias` | loss 对，norm/bias 梯度错 |
| 2 | SP 下用输入序列长度 reshape | `parallel/attention.py` | `--case tp` | shape error |
| 3 | CP K/V 反向跨 rank 求和 | `parallel/attention.py` | `--case cp` | k/v 梯度偏小 |
| 4 | PP 用 local rank 寻址 | `pipeline/schedules.py` | `--case pp --world-size 4` | 死锁/超时 |
| 5 | EP combine 的截断方向 | `parallel/expert_parallel.py` | `parallel_ep.py` | logits 漂移 |
| 6 | EP 专家梯度除以 ep | `optim/adamw.py` | `parallel_ep.py` | 专家梯度 ep 倍 |
| 7 | flat optimizer 的 scatter offset | `optim/distributed_optimizer.py` | `distributed_optimizer_equiv.py` | 参数错位 |
| 8 | 全局梯度范数用 shard 拼接 | `optim/distributed_optimizer.py` | 同上 | norm 差 √dp |
| 9 | VPP 本地交接 detach | `pipeline/interleaved_exec.py` | `parallel_vpp.py` | `.grad` 为 None |
| 10 | MoE 零梯度物化 | `optim/adamw.py` | `test_distributed.py::test_replicated_moe_dp_collective_symmetry` | 挂起/超时 |
| 11 | checkpoint 布局校验 | `checkpoint.py` | 单进程存 + 多进程载 | 静默/缺文件 |
| 12 | paged cache 层锁步 | `inference/paged_cache.py` | `test_paged_cache.py` | 读到半写 KV |
| 13 | RoPE cache 设备/长度重建 | `parallel/attention.py` | `test_rope_cache.py` | shape/device 错 |

N.16 另有 3 个第七轮代码审计补充实验：恢复 off-by-one、FP8 模拟梯度归零、
flat optimizer 的 TP/PP clip norm。

## N.3 实验 1：删掉 Column Parallel 的 grad_input all-reduce

**不变量**：Column Parallel 每个 rank 只输出一部分特征；输入梯度是各 rank
贡献之和，反向必须 all-reduce。

**注入**（`mini_megatron/parallel/layers.py`，`_ColumnParallelLinearFn.backward`）：

```python
if ctx.needs_input_grad[0]:
    grad_input = grad_output_w.matmul(weight).to(x.dtype)
    if ctx.world > 1:
        # 注入：注释掉这一行
        # grad_input = all_reduce(grad_input, group=ctx.group)
        pass
```

**运行**：

```bash
python tests/parallel_equiv.py --case tp_bias --world-size 2 --out "$TEMP/tp_bias.json"
```

**预期**：`loss_diff` 仍接近 0（前向不变）；`tp_bias` 的 `grad_diff` 明显
变大，尤其是 `input_layernorm.weight.grad`。只跑 `--case tp` 时，`grad_diff`
只比较 q_proj/out_proj 的权重梯度，可能仍然通过；`gradnorm_diff` 可能变红，
但用它定位不如 `tp_bias` 直接。这是本实验最重要的一课：**测试要覆盖被破坏
的那条路径**。

**根因/修复**：补回 all-reduce。修复后 `tp_bias` 的梯度误差约 `1e-7`。

**生产对应**：TP 的“loss 正常但收敛慢/梯度范数异常”先查 Column 反向的
输入梯度归约；监控 `grad_norm` 与单卡参考的偏差，比只看 loss 更敏感。

## N.4 实验 2：SP 下用输入序列长度 reshape

**不变量**：SP 下输入 `x` 是 `[b, s/tp, h]`，但 Column Parallel 的 q_proj
会先 all-gather 成 `[b, s, h]`；reshape q/k/v 必须用 **q 的序列长度**。

**注入**（`parallel/attention.py`）：

```python
# 注入：把 q_seq = q.size(1) 改成输入长度
q_seq = x.shape[1]
q = q.view(b, q_seq, self.num_attention_heads_per_partition, self.head_dim)
```

**运行**：

```bash
python tests/parallel_equiv.py --case tp --world-size 2 --out "$TEMP/tp.json"
```

**预期**：`shape '[2, 16, 2, 16]' is invalid for input of size 2048` 一类的
reshape 错误。普通 TP（无 SP）不会触发，因为输入序列没有被切。

**根因/修复**：用 `q.size(1)`，不要用输入 `x` 的序列长度；SP 的布局变换
发生在 q/k/v 投影之前。

**生产对应**：SP + GQA/MLA 的形状错误通常在第一层就炸；如果没炸，检查
`q_seq` 是否恰好等于 `s/tp` 的倍数而“碰巧能 reshape”。

## N.5 实验 3：CP 的 K/V 反向只 chunk 不 all-reduce

**不变量**：每个 CP rank 用同一份完整 K/V 计算不同 Q 块；全局 loss 是各
Q 块之和，所以本地 K/V 的梯度是所有 rank 贡献之和。

**注入**（`parallel/attention.py`，`_GatherContextParallel.backward`）：

```python
# 注入：删掉跨 CP 的 all-reduce
# grad_output = comm.all_reduce(
#     grad_output, op=comm.SUM, group=get_context_parallel_group()
# )
rank = _group_rank(get_context_parallel_group())
return torch.chunk(grad_output, ctx.world, dim=2)[rank]
```

**运行**：

```bash
python tests/parallel_equiv.py --case cp --world-size 2 --out "$TEMP/cp.json"
```

**预期**：`q` 梯度可能接近，`k_grad_diff`/`v_grad_diff` 明显变大；
`norm_diff` 也可能偏。历史上观察到 K/V 梯度范数约 0.85×、LayerNorm 约
0.74× 参考值。

**根因/修复**：backward 先 `all_reduce` 再 chunk。修复后 K/V 逐元素误差
约 `1e-8`。

**生产对应**：CP 下“loss 正常、长序列效果差”优先查 K/V 梯度；只比较梯度
范数可能漏掉方向错误，必须做逐元素对比。

## N.6 实验 4：PP 用 local rank 当 global rank

**不变量**：PP group 的 rank 不一定是连续的全局 rank。例如 `pp=2, dp=2` 时
rank 0 的 PP 邻居是 global 2，不是 1。

**注入**（`pipeline/schedules.py`，`PipelineCommunicator`）：

```python
# 注入：send/recv 直接用 local rank
work = comm.isend(tensor, dst=self.rank + 1, tag=1000 + mb, group=self.group)
# recv:
comm.recv(tensor, src=self.rank - 1, tag=1000 + mb, group=self.group)
```

**运行**：

```bash
python tests/parallel_equiv.py --case pp --world-size 4 --out "$TEMP/pp.json"
```

**预期**：死锁/超时，或 p2p 消息被错误 rank 收到。`pp=2,dp=1`（world=2）
不会暴露，因为 local=global；必须用 `world=4` 让 DP 维插在 PP 邻居之间。

**根因/修复**：用 `comm.get_global_rank(group, local)` 把组内 rank 转成
全局 rank。修复后 `--case pp` 的 loss/grad 与 PP=1 一致。

**生产对应**：多机启动时“第一个 step 就卡住”常是 rank 映射问题；打印每个
rank 的 `(dp, pp, cp, tp)` 与 p2p 邻居，比看 NCCL 日志更快。

## N.7 实验 5：EP combine 的截断方向写反

**不变量**：dispatch 时 rank r 收到“别人发给 r”的 token；combine 时 r 把
“r 收到的那些 token”发回原发送者。返回块应按 **自己发给 peer 的数量**
截断。

**注入**（`parallel/expert_parallel.py`）：

```python
# 注入：把 counts_all[self.ep_rank][src] 改成 counts_all[src][self.ep_rank]
returned_x = torch.cat(
    [
        returned[src][: int(counts_all[src][self.ep_rank].item())]
        for src in range(self.ep_size)
    ],
    dim=0,
)
```

**运行**：

```bash
python tests/parallel_ep.py --world-size 2 --out "$TEMP/ep.json"
```

**预期**：loss 可能仍能下降，但 `logits_diff` 约 `0.025`（容差 `1e-5`），
`expert_diff`/`all_grad_diff` 也会变大。

**根因/修复**：截断长度用 `counts_all[self.ep_rank][src]`。修复后
`logits_diff` 约 `1.2e-7`。

**生产对应**：EP 的“能训但评测差”优先查 combine 索引；只比较本地 loss
抓不到，必须对比全局 logits/梯度。

## N.8 实验 6：EP 专家梯度不除以 ep

**不变量**：每个专家收到所有 EP rank 的 token；它的梯度是各 rank 局部
mean-loss 梯度之和。稠密参数经过 DP 平均后是全局 mean，专家参数也要除以
`ep` 才是同一尺度。

**注入**（`optim/adamw.py`，`all_reduce_gradients`）：

```python
if skip_experts:
    for p in model.parameters():
        if p.grad is not None and getattr(p, "_is_expert_parallel", False):
            # 注入：注释掉这一行
            # p.grad.div_(ep)
            pass
```

**运行**：

```bash
python tests/parallel_ep.py --world-size 2 --out "$TEMP/ep.json"
```

**预期**：`logits_diff`/`aux_diff` 可能正常，但 `expert_diff` 约 `ep` 倍；
训练能跑，专家学习率实际被放大。

**根因/修复**：补回 `div_(ep)`；同时确认 DP 调用跳过专家参数、CP 调用
不跳过。

**生产对应**：MoE 训练“专家权重更新过快/路由塌缩”要查专家梯度缩放；
把 dense 与 expert 的 grad norm 分开记录。

## N.9 实验 7：flat optimizer 的 scatter offset 错位

**不变量**：flat buffer 按 `model.parameters()` 顺序打包；散回参数时
offset 必须逐参数递增 `numel`。

**注入**（`optim/distributed_optimizer.py`，`_scatter_params`）：

```python
for p, n in zip(self.params, self.numels):
    p.data.copy_(flat[offset : offset + n].view_as(p).to(p.dtype))
    # 注入：多跳一个元素
    offset += n + 1
```

**运行**：

```bash
python tests/distributed_optimizer_equiv.py --out "$TEMP/dopt.json"
python -m pytest -q tests/unit/test_distributed_optimizer.py
```

**预期**：单进程测试的 `param_max_diff` 变大；DP=2 等价性失败。若 padding
区恰好被跳过，可能只表现为部分参数错位，更需要逐元素对比。

**根因/修复**：offset 必须与打包顺序完全一致；用参数名/形状校验，而不是
只信总元素数。

**生产对应**：分布式优化器“loss 跳/某些参数不更新”先打印每个参数的
`(name, offset, numel, shard)`，与 checkpoint metadata 对照。

## N.10 实验 8：全局梯度范数用 shard 拼接

**不变量**：flat optimizer 的每个 rank 只有梯度 shard；全局范数是各 shard
范数平方和的 all-reduce 再开方。

**注入**（`optim/distributed_optimizer.py`，`grad_norm`）：

```python
# 注入：用完整 grad_flat（本地未 reduce-scatter 的副本）算范数
local = self.grad_flat.detach().pow(2).sum()
```

**运行**：

```bash
python tests/distributed_optimizer_equiv.py --out "$TEMP/dopt.json"
```

**预期**：`grad_norm_diff` 约 `√dp` 倍（DP=2 时约 1.4×）；参数更新本身
可能仍正确，但裁剪系数错。

**根因/修复**：只用 `grad_shard` 的局部范数，再 all-reduce SUM；不要用
“每卡完整梯度”的公式。

**生产对应**：grad clip 后 loss 异常、不同 DP size 下等效学习率漂移，先查
范数口径。

## N.11 实验 9：VPP 本地交接不 detach

**不变量**：VPP 的本地 chunk 交接与远程路径一样，下游拿到的是新叶子
张量；否则 `.grad` 为 `None`，反向路由被跳过。

**注入**（`pipeline/interleaved_exec.py`，`_pass_hidden`）：

```python
# 注入：去掉 detach
self._hidden[(mb, chunk)] = tensor
```

**运行**：

```bash
python tests/parallel_vpp.py --world-size 2 --out "$TEMP/vpp.json"
```

**预期**：反向抛 `KeyError`/`RuntimeError`，或某些参数的 `.grad` 为
`None`；`loss_diff` 可能仍为 0（前向正确）。

**根因/修复**：本地交接也 `tensor.detach()`，与远程 `isend` 路径一致；
在反向测试里断言所有预期参数的梯度非 `None`。

**生产对应**：VPP/interleaved 1F1B 的“前向对、反向缺梯度”优先查本地
chunk 的图边界与 `.grad`。

## N.12 实验 10：MoE 未激活专家导致集合通信数量不齐

**不变量**：MoE 路由是数据相关的；某专家在一个 rank 有梯度、另一个 rank
为 `None` 时，所有 rank 仍必须以相同顺序参与相同数量的 collective。

**注入**（`optim/adamw.py`，`all_reduce_gradients`）：

```python
has_moe = any(getattr(module, "is_moe", False) for module in model.modules())
if has_moe:
    for p in model.parameters():
        if p.grad is None and p.requires_grad:
            # 注入：不物化零梯度
            # p.grad = torch.zeros_like(p)
            pass
```

**运行**：

```bash
python -m pytest -q tests/unit/test_distributed.py::test_replicated_moe_dp_collective_symmetry
```

**预期**：挂起/超时，或 NCCL 把不同参数的梯度错误配对。

**根因/修复**：MoE 模型在同步前把所有 `None` 梯度物化为 0；必要时用
`torch.zeros_like` 并标记为“已同步”。代价是未使用参数也走一次通信。

**生产对应**：MoE + DP 的“偶发挂起”先看各 rank 的 collective 计数与
专家激活分布；不要只看最后一个成功 step。

## N.13 实验 11：checkpoint 布局校验被绕过

**不变量**：TP/PP/CP/DP 任一变化都不能按原目录 `load_state_dict`；必须走
重分片工具。

**注入**（`checkpoint.py`，`load_checkpoint`）：

```python
for key, value in expected.items():
    actual = metadata.get(key, ...)
    if actual != value:
        # 注入：跳过校验
        # raise ValueError(...)
        pass
```

**运行**：

```bash
# 1) 单进程存 TP=1 checkpoint
python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 1 --checkpoint-dir .verify_tmp/layout
# 2) 用 TP=2 多进程尝试恢复
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json \
  --steps 2 --checkpoint-dir .verify_tmp/layout --auto-resume
```

**预期**：rank 0 可能“成功”加载自己的文件，rank 1 缺文件/形状不匹配；
更危险的是某些布局下 `strict=False` 只加载部分参数，loss 表面能跑。

**根因/修复**：保留 metadata 校验；不同布局走
`checkpoint_reshard.py`。恢复后跑 10–100 步对比 loss。

**生产对应**：换并行度后的第一次恢复必须做“loss 连续性 + 逐元素权重”
双重验收；metadata 只做文件列表是不够的。

## N.14 实验 12：paged cache 去掉层锁步

**不变量**：同一 token chunk 必须写完所有层，公开的 `length` 才能前进；
否则读者会看到“长度已更新、某些层还没写”的半成品。

**注入**（`inference/paged_cache.py`，`append`）：

```python
if layer == self.num_layers - 1:
    if any(
        self.layer_lengths[(seq_id, other)] != end
        for other in range(self.num_layers)
    ):
        # 注入：跳过锁步检查
        # raise ValueError(...)
        pass
    self.lengths[seq_id] = end
```

**运行**：

```bash
python -m pytest -q tests/unit/test_paged_cache.py
```

**预期**：测试在“部分写/跨层不一致”用例失败；若测试只读最后写入的层，
可能仍然通过——这正是需要“层锁步”断言的原因。

**根因/修复**：保留 layer_lengths 检查；或把 `length` 改成按层独立、由
调度器保证一致，但要把不变量写进接口。

**生产对应**：PagedAttention/prefix cache 的“偶发读到旧 KV/错 token”先查
写入可见性、块表与层同步。

## N.15 实验 13：RoPE cache 设备变化不重建

**不变量**：`cos/sin` cache 的 device 与长度必须匹配当前请求；设备变化
（CPU prefill → CUDA decode）或 cache 比当前请求长时，都必须正确重建。

**注入**（`parallel/attention.py`，`_build_cache`）：

```python
if (
    seq_len <= self._cache_len
    and self._cos is not None
    and self._cos.device == device
):
    return
# 注入：用请求长度而不是实际缓存长度 reshape
length = seq_len
```

**运行**：

```bash
python -m pytest -q tests/unit/test_rope_cache.py
```

**预期**：interleaved 分支出现 shape mismatch；或 CPU/GPU cache 混用导致
device error。

**根因/修复**：用 `max(seq_len, self._cache_len)` 重建，并把 device 纳入
cache key；测试覆盖“先长后短”和“设备变化”两条路径。

**生产对应**：prefill/decode 分离、CPU offload、动态 batching 下 RoPE
cache 的 device/长度是最常见的“换环境才炸”问题。

## N.16 补充：第七轮审计的三个新实验

第七轮代码审计又抓到三个“测试假绿”的 bug；它们很适合作为额外的注入实验。

### N.16.1 恢复跳过一步

**不变量**：checkpoint 的 `step` 是“已完成的更新数”；恢复的下一步就是它，
不是 `step+1`。

**注入**（`mini_megatron/training.py`，`fit`）：

```python
if resume:
    last = self.resume(resume)
    start = last + 1          # 注入：跳过一步
```

**运行**：

```bash
python -m pytest -q tests/unit/test_training.py::test_two_steps_and_checkpoint
```

**预期**：恢复轨迹的第 2 步与未中断轨迹对不上（off-by-one）。旧测试用
`evaluate(0) == evaluate(0)` 是同一表达式，永远不会失败；新测试逐步对比
loss/grad_norm。

**根因/修复**：`save(step + 1)` 保存的是完成数，恢复用 `start = last`。

### N.16.2 FP8 模拟把梯度量化成 0

**不变量**：FP8 模拟的 forward 量化，backward 不能被 E4M3 再量化。

**注入**（`parallel/layers.py`，`_round_to_fp8`）：

```python
# 注入：去掉 STE，直接把量化结果返回
return quantised
```

**运行**：

```bash
python -m pytest -q tests/unit/test_layers.py::test_fp8_simulation_preserves_gradient
python -m pytest -q tests/unit/test_model.py::test_fp8_simulation_forward_backward
```

**预期**：`x.grad` 变成 0；模型所有参数梯度为 0。旧测试只断言 `finite`，
0 也 finite。

**根因/修复**：`return x + (quantised - x).detach()`。

### N.16.3 flat optimizer 的 clip norm 只看 DP

**不变量**：flat shard 同时是 TP/PP 的切片；clip 的范数必须在整个 world
上归约，TP 复制参数按 `1/tp` 去重。

**注入**（`optim/distributed_optimizer.py`，`grad_norm`）：

```python
# 注入：只归约 DP 组，且忽略 TP 复制权重
local = self.grad_shard.detach().pow(2).sum()
total = comm.all_reduce(local, op=comm.SUM, group=self.group)
return total.sqrt()
```

**运行**：

```bash
python tests/flat_tp_equiv.py --out "$TEMP/flat_tp.json"
```

**预期**：TP=2/DP=2 时 flat 报 `~1.36`，参考全局范数 `~1.97`；`clip=0.01` 下
一步后 `param_diff ≈ 2.76e-5`。修复后 norm 一致、`param_diff=0`。

**根因/修复**：全 world 归约 + `norm_weight`（TP 复制 1/tp）+ CP 去重。

## N.17 从故障到监控：把实验变成告警

每个实验都能映射到一个生产指标。不要等 loss 崩才查：

| 实验 | 监控指标 | 告警思路 |
|---|---|---|
| 1/3/6 | 各参数组的 grad norm、与单卡参考的偏差 | dense/expert/KV 分组看，不只看总 norm |
| 2 | 首层 shape/断言 | 启动冒烟：固定 batch 跑 2 步 |
| 4 | step time、p2p 等待时间 | 首 step 超时/邻居不匹配直接失败 |
| 5/6 | 全局 logits 与参考的 cosine/最大差 | 每次改 EP 配置跑固定 batch 对照 |
| 7/8 | 参数 checksum、clip 前后 norm | 保存前校验参数名/offset |
| 9/10 | collective 计数、未同步参数数 | watchdog + 每 rank 计数对齐 |
| 11 | 恢复后 loss 连续性 | 自动恢复演练，失败则回退 |
| 12/13 | KV 长度/层写指针、cache device | 断言 + 端到端 greedy 对齐 |

## N.18 一页纸实验记录模板

```text
实验编号：
不变量（一句话）：
基线命令与结果：
注入（文件/函数/一行改动）：
运行命令：
观察到的症状（loss/grad/shape/挂起）：
根因：
修复：
回归测试（命令 + 断言）：
生产监控指标：
是否留下永久测试：是/否（为什么）
```

把这张表贴在 PR 描述里，比“修了一个 bug”有用得多：它让评审者能看到
**哪个不变量被破坏、哪个测试能防止回归**。

## N.19 小结

- 并行 bug 的默认形态是“loss 正常、梯度/长上下文错”；必须主动破坏来学习。
- 每个实验只跑最小命令，先记录基线，再注入，再恢复。
- 同一个 bug 可能通过一个测试、失败另一个；测试要覆盖被破坏的路径。
- 把实验映射到监控指标，才能从“事后排查”变成“事前告警”。
- 第 27 章是病历，本附录是实验课；两者配合，才能把“能跑”变成“能信”。

---

上一附录：[附录 M 源码阅读实验手册](M-source-reading-labs.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
返回全书总结：[第 52 章](52-conclusion.md)
