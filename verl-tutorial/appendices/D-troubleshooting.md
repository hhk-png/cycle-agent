# 附录 D 故障排查手册

## D.1 使用顺序

遇到问题时：

1. 确认是**正确性**还是**性能**问题；
2. 缩小到最小配置（单卡、小模型、小数据、sync）；
3. 打印数据/mask/reward/advantage；
4. 对比 MiniVerl 的行为；
5. 再逐步加回并行/异步/规模。

## D.2 启动阶段

| 现象 | 证据 | 根因 | 处理 |
|---|---|---|---|
| `rollout.name` 缺失 | 配置报错 | v0.9.1 要求显式 | 设 `vllm`/`sglang` |
| import 失败 | ModuleNotFound | 依赖/extras 不匹配 | 用 uv lock/官方镜像 |
| CUDA 版本不匹配 | vLLM/torch 报错 | ABI 不一致 | 统一 extras |
| Ray 启动失败 | `ray.init` 报错 | 端口/资源/uv runtime | 检查 Ray 日志 |
| TP 超过 GPU 数 | 配置校验 | 并行度不合法 | 调整 TP/DP |
| 数据路径不存在 | FileNotFound | 挂载/路径 | 检查 `train_files` |

## D.3 OOM

| 阶段 | 证据 | 处理 |
|---|---|---|
| rollout | vLLM OOM | 降 `gpu_memory_utilization`、并发、长度 |
| 训练前向 | 激活 OOM | 降 micro batch、重计算 |
| 训练反向 | 激活/梯度 OOM | 重计算、offload、FSDP |
| 权重同步 | gather OOM | `delta_sharded`（v0.9.1 的真实后端名）、offload |
| 验证 | batch 太大 | 降 `val_batch_size` |

先判断是“参数/优化器”还是“激活/KV”：

- 参数/优化器：分片/offload/LoRA；
- 激活：micro batch/重计算/remove padding；
- KV：并发/长度/GQA/TP。

## D.4 loss/指标异常

| 现象 | 先看 | 可能原因 |
|---|---|---|
| reward 全 0 | 样本/answer/reward 输出 | 抽取规则、数据、模板 |
| reward 全 1 | 验证器/泄漏 | 太松、重复数据 |
| advantage 全 0 | 组内奖励 | n 太小、奖励恒定、分组 key 错 |
| pg_loss=0 | advantage/ratio | 优势为 0、mask 全 0 |
| clipfrac≈1 | lr/old logprob | lr 太大、约束/mask 不一致 |
| ppo_kl 爆炸 | 同上 | 同上 |
| grad_norm 爆炸 | 奖励尺度/精度 | 裁剪、归一化、bf16 |
| vf_loss 不降 | lr/目标 | critic lr、returns 尺度 |
| 训练涨验证不涨 | 数据/奖励 | 过拟合、reward hacking |

## D.5 权重同步

| 现象 | 证据 | 处理 |
|---|---|---|
| 输出不变 | rollout/actor logits 相同 | 检查 `update_weights` 是否调用 |
| 输出乱码 | logits 差异大 | 布局转换/名称/dtype |
| 同步很慢 | bytes/time | delta/nccl、减少副本 |
| sleep 后崩溃 | wake 失败 | 检查备份/状态 |
| 版本不对 | `rollout_version` | 同步频率/版本号 |

验证方法（真实 verl v0.9.1 的 engine worker 走 TensorDict/TransferQueue）：

```python
# 真实 verl：compute_log_prob 收发 TensorDict，结果从 TQ 取
out = actor.compute_log_prob(batch.to_tensordict())
actor_lp = tu.get(out, "log_probs")
# rollout_log_probs 由 rollout 侧写入同一批样本的字段
```

MiniVerl 的对照写法是：

```python
out = actor.compute_log_prob(batch)          # MiniVerl: 返回 DataProto
actor_lp = out.batch["old_log_probs"]        # (B, R)
rollout_lp = batch.batch["rollout_log_probs"]
print(np.abs(actor_lp - rollout_lp).max())
```

不要把 MiniVerl 的 `DataProto`/`old_log_probs` 字段名直接套到 v0.9.1 的 V1 worker 上。

## D.6 NCCL/多机

| 现象 | 证据 | 处理 |
|---|---|---|
| NCCL timeout | 最后 rank 日志 | 网络/IB/端口/掉卡 |
| hang 在 init | 进程组 | MASTER_ADDR/端口/防火墙 |
| 多机慢 | 带宽 | IB/RoCE、拓扑、bucket |
| rank 错位 | checkpoint 路径 | 节点亲和/固定 IP |

排查：

```bash
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,COLL
```

先单机复现，再查网络。

## D.7 异步

| 现象 | 证据 | 处理 |
|---|---|---|
| staleness 大 | `fully_async/count/stale_samples_processed`、`fully_async/trainer/idle_ratio` | 提高同步频率/加资源 |
| drop 多 | `fully_async/count/dropped_stale_samples` | 降低生产速度/阈值 |
| IS 权重极端 | `rollout_is_max`、`rollout_is_eff_sample_size` | 降 staleness、调截断 |
| 训练不稳 | kl/clipfrac | 先用 sync 验证 |
| 队列空 | buffer_batches=0 | 预热/提高生成速度 |

MiniVerl 的指标名是 `async/staleness_max`、`async/dropped_samples`、`rollout_is/truncated_frac`；它们只用于本仓库的示例，不是 v0.9.1 的键名。

## D.8 多轮/工具

| 现象 | 证据 | 处理 |
|---|---|---|
| 不调用工具 | 生成文本 | prompt/格式奖励 |
| 解析失败 | 原始输出 | 换 parser/格式 |
| observation 被训练 | mask | 检查 mask |
| 轨迹过长 | num_turns/length | 限制轮次/长度 |
| 工具超时 | 延迟 | 异步/超时/并发 |
| 同名工具行为漂移 | import 顺序 | 注册表重名检查；不要静默覆盖 |
| 工具异常导致轨迹中断 | traceback | execute 内捕获并转成 `error: <Type>` |
| timeout 后后台仍在跑 | 线程/进程 | 协作式超时不够；把超时放到沙箱层 |
| 工具奖励刷次数 | 调用次数分布 | 分开记录 step/outcome reward，调尺度 |

## D.9 MoE 路由/权重同步

| 现象 | 证据 | 处理 |
|---|---|---|
| 生成质量降但 loss 正常 | log-prob 相关性 | 查权重同步的 prepare/finalize 与 scale |
| replay 后每层都错 | 层数不一致 | 记录 hash router；显式校验层数 |
| replay mask 后仍不一致 | 漏掉 prompt 尾行 | mask 覆盖所有影响 response logits 的行 |
| 重复专家导致容量异常 | routing map | token count 从实际 routing map 统计 |
| 第二步训练才崩 | 第二次同步 | 跑至少两个优化器 step 再判断 |

## D.10 文档/测试

| 现象 | 证据 | 处理 |
|---|---|---|
| check_docs 失败 | 引用/数字 | 修文档或更新事实 |
| 测试收集数变化 | pytest | 更新 README/文档 |
| 示例失败 | traceback | 修代码/示例 |
| 中文乱码 | UnicodeEncodeError | `-X utf8` |

## D.11 最小复现模板

```bash
python -X utf8 -m mini_verl.trainer.main_ppo \
  trainer.n_gpus_per_node=1 \
  trainer.nnodes=1 \
  trainer.total_training_steps=2 \
  trainer.val_before_train=false \
  data.train_size=8 \
  data.train_batch_size=8 \
  actor_rollout_ref.rollout.n=1 \
  algorithm.adv_estimator=grpo
```

如果这一步都失败，问题在核心链路；如果成功，再逐步加回并行/异步/规模。
