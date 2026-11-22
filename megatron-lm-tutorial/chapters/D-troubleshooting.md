# 附录 D 故障排查清单

> 使用顺序：先 D.1/D.2 排除配置与环境，再 D.3–D.5 定位训练问题，最后 D.6–D.11
> 处理检查点、数据、通信、显存、复现与安全。每一条都尽量给出“先看什么证据”，
> 而不是只给结论。

## D.1 启动前

```text
[ ] Python/PyTorch/CUDA/NCCL/TE 版本匹配且已锁定
[ ] world_size % (tp*pp*cp) == 0
[ ] hidden/heads/query_groups/ffn/vocab 能被 TP 整除
[ ] num_layers 能被 PP 整除
[ ] seq_len 能被 CP 整除；SP 时能被 TP 整除
[ ] global_batch % (micro_batch*dp) == 0
[ ] 数据路径/词表大小/tokenizer 一致
[ ] 检查点布局与当前配置一致
[ ] 所有 rank 使用同一份代码/配置/镜像
[ ] 没有残留进程占用端口/GPU
```

## D.2 启动即报错

| 错误/现象 | 常见原因 | 处理 |
|---|---|---|
| `world_size is not divisible` | 进程数与 tp·pp·cp 不符 | 改 world 或配置 |
| `hidden_size must be divisible by TP` | 模型维度与 TP 不匹配 | 调 hidden/TP |
| `num_query_groups must be divisible` | GQA 的 KV head 不够分 | 调 query groups/TP |
| `num_layers must be divisible by PP` | 层数不能均分 stage | 调层数/PP |
| `global_batch_size must be divisible` | 梯度累积无法整除 | 调 batch/DP |
| `makeDeviceForHostname` | Windows Gloo 轮子问题 | 用 portable/换 PyTorch |
| `Address already in use` | MASTER_PORT 冲突 | 换端口/清理残留 |
| `No module named mini_megatron` | cwd/PYTHONPATH 错 | 在项目根目录运行 |
| `CUDA out of memory` | batch/seq/并行度太大 | 见 D.9 |
| `NCCL error ... unhandled system error` | 驱动/网络/IB 问题 | 见 D.10 |
| shape mismatch | SP/CP 的 seq、TP 分片方向错 | 打印每层 shape |
| dtype mismatch | autocast/PP 传输/FP8 | 统一 dtype 转换 |

## D.3 训练挂起

### D.3.1 先判断“挂在哪”

```text
[ ] 所有 rank 都停，还是部分 rank 在跑？
[ ] 最后一条日志是 forward / backward / optimizer / checkpoint / data？
[ ] GPU 利用率是否掉零？是否有一张卡 100% 而其他空闲？
[ ] 是否有 rank OOM/异常退出，其他 rank 在等它？
[ ] 最近是否改了 schedule/group/tag/并行度/数据？
```

### D.3.2 常见根因

| 根因 | 证据 | 修复 |
|---|---|---|
| collective 顺序不一致 | 某 rank 跳过/多走一次 all-reduce | 去掉依赖 rank 的条件分支 |
| p2p 用局部 rank | `PP×DP` 时 recv 错 rank | `get_global_rank(group, local)` |
| tag 冲突 | 不同 micro-batch/方向串消息 | tag 编码 mb + 方向 |
| 某 rank 先 OOM | 其他 rank 永久等 | 看退出码/日志，先修 OOM |
| NCCL 超时/网络分区 | `NCCL_DEBUG=INFO` 有 timeout | 查 IB/路由/节点 |
| 数据加载卡住 | 队列为空、CPU 100% | 查存储/worker/死锁 |
| checkpoint 同步写盘 | 所有 rank 等 IO | 异步保存/本地缓存 |

### D.3.3 取证命令

```bash
# PyTorch 分布式调试
export TORCH_DISTRIBUTED_DEBUG=DETAIL
# NCCL 拓扑/连接
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=ALL
# 看进程栈（Linux）
py-spy dump --pid <pid>
# GPU 状态
nvidia-smi
nvidia-smi topo -m
# 清理残留（确认没有有效 checkpoint 正在写）
pkill -f mini_megatron
```

Windows/portable 后端：Queue 超时会抛 `queue.Empty` 并打印栈；先看哪个 rank
没有进入下一次 `get/put`。

## D.4 loss 异常

### D.4.1 NaN/Inf

```text
1. 找第一个 NaN 的 step/rank/layer（不要只看最后一步）
2. 关 FP8/bf16/dropout/重算，用 fp32 小 batch 复现
3. 检查 attention mask 是否整行 -inf（尤其 CP/padding）
4. 检查 CE 的 ignore_index、空样本、越界 token
5. 检查梯度裁剪前的 grad_norm 是否爆炸
6. 检查 MoE 路由是否 NaN/全选一个专家
7. 检查 checkpoint 是否 missing keys/加载错
8. 用 hook 打每层 activation/grad 的 min/max
```

### D.4.2 loss 不降

| 可能原因 | 检查 |
|---|---|
| 标签错位/offset 错 | 打印 `input_ids/labels` 的前 20 个 token |
| 数据无信号/重复 | 换真实数据、小样本过拟合 |
| LR 太大/太小 | 扫 LR，warmup 是否足够 |
| 梯度没同步 | DP all-reduce 是否执行 |
| 参数未连接/被冻结 | 检查 `requires_grad`/missing keys |
| CE mask 全 ignore | 统计有效 label 数 |
| 精度问题 | bf16 vs fp32 对照 |
| packing/document mask 错 | 跨文档 attention 是否泄漏 |

### D.4.3 loss spike/抖动

```text
[ ] 记录 spike 前后 lr/grad_norm/数据 shard/step
[ ] 回滚到最近 checkpoint
[ ] 跳过可疑数据 shard
[ ] 降 lr/增 warmup/调 batch
[ ] 检查 FP8 amax/scale、MoE 路由、attention logits
[ ] 固定 seed/并行度复现
```

良性 spike 通常会在几十步内恢复；如果权重统计（grad norm、activation
max）持续异常，按不可逆退化处理。

### D.4.4 梯度异常

| 现象 | 检查 |
|---|---|
| grad_norm=0 | 参数未连接、全 mask、detach、优化器没 step |
| grad_norm 巨大 | LR/初始化/精度/数据、是否漏 clip |
| 某些参数无梯度 | 未使用、被冻结、PP stage 不属于它 |
| 梯度范数随 TP 放大 | 复制参数被重复计数（见第 27 章 bug 6） |
| 梯度在 CP/TP 组内不一致 | 漏 all-reduce/切错维度 |

## D.5 性能下降

### D.5.1 先分解 step time

```text
step_time = data_wait + forward + backward + optimizer + communication + checkpoint
```

逐项测量，不要只看总 tokens/s。常见比例：

- 计算密集：GEMM 占 60%–80%；
- 通信密集：TP all-reduce/EP all-to-all 占 20%+；
- 数据密集：data_wait 占 10%+；
- PP 气泡：大 stage 数 + 小 micro-batch 时明显。

### D.5.2 常见根因

| 现象 | 可能原因 | 处理 |
|---|---|---|
| GPU 利用率低、GEMM 短 | micro-batch 太小 | 增大 micro-batch/累积 |
| 通信占比高 | TP 跨 IB/消息太小 | TP 留 NVLink、bucket、重叠 |
| PP 气泡大 | m 太小/PP 太深 | 增大 m、VPP、减 PP |
| 数据加载空档 | CPU/存储瓶颈 | prefetch、更多 worker、本地缓存 |
| 显存碎片 OOM | 变长/临时张量 | `expandable_segments`、重算 |
| 单卡拖后腿 | 坏卡/降频/NVLink 降速 | 隔离节点、查 DCGM |
| 专家负载不均 | 路由塌缩 | aux/bias、capacity、监控 |
| checkpoint 卡顿 | 同步写盘 | 异步保存、分层存储 |

## D.6 检查点

```text
[ ] metadata 的 TP/PP/CP/DP 与当前配置匹配？
[ ] 文件大小/数量符合预期？hash/校验和通过？
[ ] missing/unexpected keys？
[ ] optimizer state（Adam m/v/step）是否加载？
[ ] LR scheduler/GradScaler 是否恢复？
[ ] 数据 sampler/epoch/offset 是否恢复？
[ ] RNG（全局/tracker）是否恢复？
[ ] 恢复后 step 是否从 step+1 开始？
[ ] 恢复后 loss 是否与中断前连续？
```

恢复后 loss 跳变的排查顺序：优化器状态 → 数据位置 → RNG → LR scheduler →
并行布局 → missing keys → 精度/FP8 scale。详见第 24 章与附录 G。

## D.7 生成/推理

```text
[ ] prompt token id 是否越界/超过 max_seq_len？
[ ] tokenizer/chat template 是否与训练一致？
[ ] TP 词表 gather/输出层是否只取本地 shard？
[ ] 采样 temperature/top-k/top-p/seed 是否一致？
[ ] eos/pad 处理是否正确？
[ ] PP/CP 是否被禁用或正确支持？
[ ] KV cache 的 position/长度是否对？
[ ] 生成是否在 eval/no_grad 模式？
```

常见错误：训练时加了 BOS、推理时没加；输出层只取了 rank 0 的 vocab shard；
KV cache 的 position 从 0 重新开始；greedy 与采样混用导致不可复现。

## D.8 数据管线

```text
[ ] 每个 DP rank 是否看到不同 batch？
[ ] 同一 DP rank 的 TP/PP/CP 副本是否看到相同 batch？
[ ] shuffle 的 seed 是否按 DP rank 区分？
[ ] drop_last 是否导致最后 batch 形状不一致？
[ ] packing 的 document mask/position reset 是否正确？
[ ] 恢复时 sampler 是否从正确位置继续？
[ ] 数据是否有重复/空样本/越界 token？
[ ] 预处理 CPU 是否成为瓶颈？
```

## D.9 显存 OOM

按“先降峰值，再查泄漏”的顺序：

```text
[ ] 降低 micro-batch / seq_len
[ ] 开 activation checkpointing
[ ] 开 ZeRO-1/分布式优化器
[ ] 增大 TP/PP/CP（把状态/激活切开）
[ ] 用 bf16/FP8（确认数值）
[ ] 清理临时张量/避免不必要的大 buffer
[ ] PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
[ ] 检查碎片：是否频繁 alloc/free 不同 shape
[ ] 检查泄漏：step 间显存是否持续增长
```

“假 OOM”常见于：attention score 物化（长序列）、all-gather KV、checkpoint
同时保存多份、评测/生成没有 no_grad。

## D.10 NCCL/网络

```text
[ ] nvidia-smi topo -m 看 GPU/NVLink 拓扑
[ ] ibstat/ibv_devinfo 看 IB 端口状态
[ ] NCCL_DEBUG=INFO 看 ring/tree、连接、超时
[ ] 跑 NCCL all-reduce microbenchmark 对比带宽
[ ] 检查 MTU/路由/防火墙/容器网络
[ ] 检查是否跨 rail/跨交换机
[ ] 单节点 vs 多节点分别测
[ ] 坏节点隔离后重试
```

常见现象：单节点正常、多节点慢/挂 → 网络；所有节点都慢 → 配置/驱动；只有
某些 rank 慢 → 坏卡/坏链路/负载不均。

## D.11 复现性

```text
[ ] 代码/镜像/驱动/CUDA/NCCL/TE 版本一致？
[ ] 配置/数据/tokenizer hash 一致？
[ ] seed 与 RNG 流（TP/DP）语义正确？
[ ] 数据 sampler 位置/epoch 恢复？
[ ] 并行度/拓扑一致？
[ ] 关闭非确定 kernel/开启 deterministic？
[ ] 是否接受统计复现而非逐位复现？
```

详见附录 G。

## D.12 安全与合规

```text
[ ] 数据/模型/许可证可追溯？
[ ] PII/密钥/敏感信息已脱敏？
[ ] 多租户隔离/访问控制？
[ ] 检查点/日志是否包含敏感数据？
[ ] 依赖/镜像是否经过扫描？
[ ] 出口管制/地区法规是否满足？
[ ] 事故响应/删除流程是否可执行？
```

安全问题的特点是“平时不出现，出现就是高成本”；不要等出事才补。

## D.13 错误字符串索引（按报错找原因）

| 报错/日志片段 | 常见原因 | 先查 |
|---|---|---|
| `must be divisible by` | TP/PP/CP/EP 整除关系 | 配置校验、world size |
| `shape ... is invalid for input of size` | SP/CP 下 reshape 用了错误序列长 | 用 `tensor.size()`，打印每层 shape |
| `Expected all tensors to be on the same device` | 模型/输入/缓存设备不一致 | `.to(device)`、RoPE cache 设备 |
| `CUDA out of memory` | 激活/参数/碎片/临时 buffer | micro-batch、重算、序列、碎片 |
| `NCCL error ... unhandled system error` | 链路/驱动/GPU 故障 | dmesg、DCGM、ibstat、重试 |
| `NCCL timeout` / `Watchdog caught collective operation timeout` | 死锁/某 rank 掉队 | rank 映射、p2p tag、栈 |
| `Connection reset by peer` | 节点/网络中断、rendezvous 失败 | 调度器、防火墙、IB |
| `Address already in use` | MASTER_PORT 冲突 | 换端口/清理残留进程 |
| `KeyError: 'model'` / `Missing key(s)` | checkpoint 格式/布局不匹配 | metadata、参数名、并行度 |
| `size mismatch for ...` | TP/PP/EP 切分或转换映射错 | 逐参数形状、转换脚本 |
| `loss is nan` / `inf` | LR/精度/坏数据/FP8 scale | 回退 bf16、查 batch、grad norm |
| `grad_norm` 恒为 0 | 梯度未同步/参数未参与/优化器错 | 打印 `requires_grad`、grad norm |
| `RuntimeError: Expected to have finished reduction` | DDP/FSDP 未使用参数 | `find_unused_parameters`、MoE 零梯度 |
| `bus error` / `Segmentation fault` | 驱动/内存/自定义 kernel | 回退版本、禁用自定义 kernel |
| `unpickling ... weights_only` | PyTorch 版本/安全加载 | 统一版本、显式 `weights_only` |
| `tokenizer`/`vocab size` mismatch | tokenizer 与模型/checkpoint 不一致 | vocab size、special tokens |
| 生成输出乱码/重复 | 权重转换、RoPE、tokenizer、采样 | logits 对齐、greedy 对比 |

原则：**先定位“第一个报错的 rank/进程”**，不要被后续的连锁报错带偏；
再确认“是配置、数据、数值还是硬件”。把每次定位过程写进 runbook，比记住
单个报错更有用。

---

上一附录：[附录 C 术语表](C-glossary.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 E 参考文献与链接](E-references.md)
