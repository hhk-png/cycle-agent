# 第 23 章 训练循环、日志与恢复

## 23.1 Trainer 的构造

`mini_megatron/training.py` 的 `Trainer.__init__`：

```python
config.validate(world_size)
self.device = torch.device(config.train.device)
self.dp_size = get_data_parallel_world_size()
self.grad_accum = global_batch // (micro_batch * dp_size)
torch.manual_seed(config.train.seed)
self.stage = TransformerStage(...)
self.optimizer = build_optimizer(...)
if distributed_optimizer_style == "flat":
    self.dist_optimizer = DistributedOptimizer(...)   # dp=1 也可用
elif distributed_optimizer and dp_size > 1:
    self.zero_optimizer = ZeRO1Optimizer(...)         # ZeRO-1 需要 DP>1
self.schedule = build_schedule(...)
self.train_provider = BatchProvider(...)
self.val_provider = BatchProvider(...)
```

顺序很重要：

1. 校验配置；
2. 确定 DP/grad accumulation；
3. **设 seed 并重置分片初始化流再建模型**：逻辑模型一致，但 TP/PP 的本地分片
   按 rank 偏移，复制参数在 TP 组内一致（见 17.5）；
4. 建优化器/调度/数据。

如果先建模型再设 seed，不同进程可能拿到不同的随机权重；如果所有 rank 共用一个
seed 而不区分分片，TP 分片会退化成复制（见附录 G）。两种错误都表现为“loss
抖动大/无法复现”。

## 23.2 train_step

```python
zero_grad
micro_batches = provider.get_micro_batches(step)
loss, aux = schedule.forward_backward(micro_batches)
all_reduce_gradients(DP)
if CP>1: all_reduce_gradients(CP, average=False)
grad_norm = clip_gradients(...)
lr = get_lr(step, ...)
set_lr(...)
optimizer.step()
```

几个细节：

- 数据 batch 要 `.to(device)`；
- loss/aux 在 DP 组内平均后记录；**CP+MoE 下 aux 只在本地 CP 序列切片上
  统计**（`expert_parallel.py` 的 `f_i/P_i` 没有跨 CP 归约），所以日志里的
  `aux_loss` 是“每个 CP rank 的局部值再对 DP 平均”，不是全局 aux。单看它
  判断负载均衡会漏掉 CP 维度的偏斜；跨 CP 的全局 aux 目前未实现（见 25.12）。
- `grad_norm` 是全局范数（见第 22 章）；
- LR 在 step 前设置，optimizer.step 使用当前 LR；
- 记录 `tokens_per_sec = global_batch * seq_len / elapsed`。

## 23.3 日志

mini 输出 JSON Lines：

```json
{"step": 0, "loss": 4.84, "aux_loss": 0.0, "lr": 0.00015, "grad_norm": 1.36,
 "tokens_per_sec": 3200}
```

生产日志通常包含：

- loss/grad norm/lr；
- 每个 stage/rank 的显存；
- 通信时间/占比；
- 数据加载时间；
- MFU；
- MoE 专家负载；
- 故障/重试事件。

日志设计原则：**每个 step 可解析、可聚合、可回溯**。不要把关键指标只打印成
自由文本。

## 23.4 评估

```python
@torch.no_grad()
def evaluate(step):
    for i in range(eval_iters):
        batches = val_provider.get_micro_batches(step, sample_offset=i*101)
        loss, _ = schedule.forward_only(batches)
    return mean(loss)
```

评估使用独立的 seed/offset，避免和训练数据重叠。PP 下 `forward_only` 仍然要
走 p2p；CP 下 loss 在 CP 组内全局归一化。

## 23.5 检查点与恢复

`fit()`：

```python
start = 0
if resume:
    completed = self.resume(resume)   # 已完成的 step 数
    start = completed                 # 下一步就是 completed
for step in range(start, max_steps):
    train_step(step)
    if eval_interval: evaluate
    if save_interval: save(step + 1)
if save_final: save(max_steps)
```

- **保存的 step 是“已完成的 step 数”**：第 0 步跑完后存 `step_000001`，
  跑完 0..3 后存 `step_000004`；恢复时下一步是 4，不是 5。写成
  `start = last + 1` 会静默跳过一步（第七轮审计的 bug 1）。
- `auto_resume` 读取 `checkpoint_dir/latest`；
- 恢复后 LR 根据 step 重新计算，不依赖保存的 optimizer LR（或显式恢复）。

## 23.6 混合精度

`use_autocast=True` 时，schedule 在 stage 前向外包 `torch.autocast`：

```python
with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
    out = self.stage(...)
```

反向在 autocast 外，PyTorch 会根据前向图处理 dtype。mini 禁止 autocast+PP，
因为 stage 间 p2p dtype 协商被省略。生产 Megatron 用 TE 的 `fp8_autocast` 和
统一的 compute dtype。

## 23.7 断点续训的语义

一个可靠的 checkpoint 至少包含：

- 模型参数（本 rank 分片）；
- 优化器状态；
- step；
- 数据加载位置/RNG；
- 并行配置；
- 代码/配置版本。

mini 保存模型/优化器/step/RNG/metadata，但不保存数据迭代器的精确位置（数据是
随机采样，按 step 重新生成）。生产训练需要保存样本索引/epoch，确保恢复后不
重复/不跳过数据。

### 23.7.1 一个 step 失败了怎么办：重试语义与 OOM

长时间训练里，“某一步失败”是常态而不是异常。关键不是“能不能重试”，而是
**重试是否幂等**：数据、学习率、梯度累积和 step 计数会不会被重复推进。

先把失败分类：

| 失败 | 典型信号 | 正确处理 | 错误处理 |
|---|---|---|---|
| CUDA OOM | `torch.OutOfMemoryError` | 降 micro-batch、增 grad accum、开重算；保持全局 batch | 无限重试同一配置 |
| NCCL 超时 | watchdog/collective timeout | 保存现场、从最近 checkpoint 重启 | 原地重试同一个 collective |
| 数据坏样本 | decode/越界异常 | 隔离样本、计数、继续或换 shard | 静默 `except: continue` |
| 检查点写失败 | 磁盘满/权限/校验失败 | 写临时目录 + 原子 rename，保留上一个 | 覆盖唯一的 checkpoint |
| 抢占/SIGTERM | 调度器信号 | 保存 emergency checkpoint，barrier 后退出 | 直接 `kill -9` |
| 数值异常 | NaN/Inf/loss spike | 回滚、跳数据、降 lr、fp32 复现 | 只调大 clip |

**OOM 重试的三个不变量。** 如果要通过“降 micro-batch”重试，必须同时：

1. `global_batch = micro_batch × dp × grad_accum` 保持不变（否则学习率、
   warmup、数据顺序的含义都变了）；
2. 重试从**同一个 step 的开头**开始，不能在一半的 micro-batch 上续；
3. 记录本次重试的配置变更，并在日志里标记 `retry_count/config_delta`，
   否则复盘时会把两条不同的曲线当成一条。

```python
def train_step_with_retry(step):
    for attempt in range(max_retries):
        try:
            return train_step(step)          # 内部先 zero_grad
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            shrink_micro_batch_and_grow_accum()   # 保持 global batch
            log("oom_retry", step=step, attempt=attempt, config=snapshot())
        except DataError as exc:
            quarantine(exc.sample_id)        # 隔离 + 计数
            log("data_quarantine", step=step, sample=exc.sample_id)
    raise RuntimeError(f"step {step} failed after {max_retries} retries")
```

`torch.cuda.empty_cache()` 只能回收缓存，不解决碎片/泄漏；如果同一配置反复
OOM，应该开激活重算、降序列长度或换并行度，而不是把重试次数调大。

**NCCL 超时不要原地重试。** collective 已经处于不确定状态，某个 rank 可能
收到了部分数据；继续跑可能得到静默错误的结果。正确做法是：抓 Flight
Recorder/栈（33.2.2）→ 让任务失败 → 从最近 checkpoint 重启 → 用相同配置
复现，确认不是硬件/网络。

**SIGTERM 与抢占。** 云上和 Slurm 都会先发 `SIGTERM`，过一段时间再
`SIGKILL`。训练循环应注册 handler：

```python
stop = False
def _on_term(signum, frame):
    global stop
    stop = True          # 只置标志，不在 handler 里做 I/O/集合通信
signal.signal(signal.SIGTERM, _on_term)
```

然后在 step 边界检查 `stop`：保存 emergency checkpoint（可异步，但要等
落盘/校验）、在所有 rank 上 `barrier`（带超时）、以非零码退出。不要在
signal handler 里直接 `save()`：handler 可能打断正在持有的锁/集合通信，
造成半写状态。

**检查点的原子性。** 至少做到：

```text
写 <ckpt>.tmp/ → fsync 文件与目录 → rename 为 <ckpt>/ → 更新 latest
```

保留最近 `K` 个完整 checkpoint，清理时先删旧再删新；恢复时校验
metadata（并行度、step、参数 shape、dtype），校验失败就回退到上一个，而不是
“加载了但少几个 key”。

**重试的可观测性。** 每次重试都要记录：step、异常类型、堆栈摘要、rank、
配置差异、耗时、是否改变了数据顺序。没有这些字段，事后只能看到“这一步
慢了 20 分钟”，无法判断是 OOM 重试、数据隔离还是网络抖动。

## 23.8 一个典型训练曲线

`tiny.json` 10 步：

```text
step 0  loss 4.84
step 1  loss 4.81
...
step 9  loss ~4.6
```

小模型/小数据不追求 loss 很低，只验证：

- loss 单调下降（允许噪声）；
- grad norm 不发散；
- 学习率 warmup/decay 正确；
- 检查点可恢复；
- 生成不越界。

## 23.9 本章小结

- 先设 seed 再建模型；DP 副本初始化必须一致。
- 一个 step 的顺序：前向/反向 → DP/CP 同步 → 裁剪 → LR → step。
- 日志要结构化，包含 loss/grad/throughput/显存。
- 恢复的 step 语义、数据位置、并行配置都要保存。
- autocast 在 mini 中有限制，生产用 TE。

下一章拆解检查点格式与重分片。
