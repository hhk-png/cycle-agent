# 第 27 章 异步 RL：one-step off-policy 与 fully async

## 27.1 同步 RL 为什么浪费 GPU

同步 PPO 的一步：

```text
生成（等最慢的样本） -> 训练
```

生成长度服从重尾分布：大多数样本几百 token，少数几万 token。一个 batch 的完成时间由最慢样本决定。在长尾上，其他 GPU 已经空闲，但不能开始下一轮生成，因为要等 actor 更新完、权重同步完。

设生成时间 $T_g$、训练时间 $T_u$，同步总时间约为 $T_g+T_u$。如果能重叠，理想时间约为 $\max(T_g,T_u)$。

## 27.2 三种异步程度

verl v0.9.1 的 V1 trainer 有三种模式：

```yaml
trainer:
  v1:
    trainer_mode: sync | colocate_async | separate_async
```

### sync

- 训练与 rollout colocate；
- 生成完一批，sleep rollout，训练，update weights，wake；
- 无 partial rollout；
- 最稳定，吞吐受长尾影响。

### colocate_async

- 训练与 rollout 共享 GPU；
- 用 replay buffer 预取若干 batch；
- 训练当前 batch 时，rollout 生成下一批；
- 需要 staleness 控制与权重版本。

### separate_async

- rollout 使用独立资源池；
- 训练与生成完全解耦；
- `parameter_sync_step` 控制每几次本地更新同步一次权重；
- 适合长尾严重、追求吞吐的场景。

## 27.3 fully_async_policy 的架构

`verl/experimental/fully_async_policy/` 的 README 描述四件套：

1. **Rollouter**：逐样本生成，把样本放进 MessageQueue；
2. **MessageQueue**：缓存样本，控制生产速度；
3. **Trainer**：从队列取样本，凑够 batch 后训练；
4. **ParameterSynchronizer**：用 NCCL 在 Rollouter 与 Trainer 之间同步权重。

关键参数：

| 参数 | 含义 |
|---|---|
| `async_training.require_batches` | 一次取几个 mini-batch |
| `async_training.trigger_parameter_sync_step` | 训练多少次后同步 |
| `async_training.staleness_threshold` | 允许的 stale 比例 |
| `async_training.partial_rollout` | 是否保存进行中的生成 |
| `rollout.total_rollout_steps` | 总生成步数 |

README 报告：在 128 GPU、Qwen2.5-7B 上，fully async 相对基线有 2.35x～2.67x 的吞吐提升，且效果没有显著下降。这个数字依赖硬件、模型、数据与超参，不应直接套用到其他场景。

## 27.4 staleness 与新鲜度

定义样本的 staleness：

$$
\text{staleness}=\text{current\_version}-\text{rollout\_version}.
$$

`staleness_threshold=0` 表示同步；`>0` 表示允许旧样本。这里的 threshold 是**相对同步窗口的比例**（通常 0～1），不是“允许 T 条旧样本”的条数。README 给出的生成上限：

$$
\text{rollout\_num}=(1+\text{threshold})\times\text{trigger\_step}\times\text{require\_batches}\times\text{mini\_batch}
-\text{num\_staleness\_sample}.
$$

直觉：

- 阈值越大，rollout 越自由，吞吐越高；
- 旧样本越多，off-policy 偏差越大；
- 通常建议 <1；
- 需要 importance sampling 或 decoupled PPO 修正。

## 27.5 partial rollout

当需要同步权重时，正在生成的请求怎么办？

- 丢弃：浪费已经生成的 token；
- 等待：长尾阻塞；
- partial rollout：保存 KV/状态，下一轮继续。

README 的 `sleep()/resume()` 逻辑就是保存进行中的 rollout，在参数同步后继续。它需要推理引擎支持请求级暂停/恢复。

## 27.6 rollout correction

异步样本的行为策略 $\mu$ 与当前策略 $\pi$ 不同。重要性比率：

$$
w_t=\frac{\pi_\theta(y_t\mid s_t)}{\mu(y_t\mid s_t)}.
$$

直接使用会方差爆炸，因此要截断：

$$
\tilde w_t=\operatorname{clip}(w_t,1/c,c).
$$

v0.9.1 的 `algorithm.rollout_correction` 没有 `enable`/`truncate_ratio`；它用“级别 + 阈值”表达是否启用与如何截断：

```yaml
algorithm:
  rollout_correction:
    rollout_is: token          # null | token | sequence
    rollout_is_threshold: 2.0  # float: TIS 上界；"0.5_5.0": IcePop 上下界
    rollout_rs: null           # null | token_k1 | seq_sum_k1 | seq_mean_k3 ...
    rollout_rs_threshold: null
    bypass_mode: false         # false: Decoupled（3 策略）；true: Bypass（2 策略）
    loss_type: ppo_clip        # bypass_mode=true 时：ppo_clip | reinforce
    rollout_is_batch_normalize: false
```

字段语义：

- `rollout_is=null`：不做 IS；`token`/`sequence` 分别按 token 或序列聚合比率。
- `rollout_is_threshold` 是浮点数时是 TIS 上界 $\tilde w=\min(w,c)$；写成 `"lower_upper"` 时是 IcePop 的双边界截断。
- `rollout_rs` 是拒绝采样（rejection sampling）的级别；它按阈值丢弃整条/整 token 的样本，而不是加权。
- `bypass_mode=true` 时 trainer 直接把 `rollout_log_prob` 当作 `old_log_prob`；PPO ratio 已经包含 IS 修正，所以 `loss_type=ppo_clip` 不再额外乘 IS 权重（否则双重修正）；`loss_type=reinforce` 才显式乘 $w$。
- `rollout_is_batch_normalize=true` 把 IS 权重归一化到均值 1；它会抵消“行为策略整体偏移”这类均匀比率。

bypass 的正确性依赖 rollout 记录的是**行为策略**（含 temperature/top-k/top-p）的 log-prob，而不是原始 logits。

MiniVerl 的字段名不同：`algorithm.rollout_correction.enable`、`bypass_mode`、`truncate_ratio`、`normalize`。`compute_rollout_correction` 实现截断 IS，返回 `rollout_is_weights` 与统计指标；默认**不做** batch 归一化。读 MiniVerl 配置时不要把 `enable`/`truncate_ratio` 当成真实 verl 字段。

## 27.7 one-step off-policy

`verl/experimental/one_step_off_policy/` 是 fully async 的简化版：

- rollout 与训练分离；
- 训练用上一轮样本（一步滞后）；
- 参数同步频率低；
- 比 fully async 简单，适合先验证收益。

它解决“长尾导致 GPU 空闲”，但不追求完全流式。

## 27.8 异步的代价

- **off-policy 偏差**：旧样本可能已经不适合当前策略；
- **实现复杂度**：队列、版本、同步、partial rollout；
- **调试困难**：同样的配置在不同负载下表现不同；
- **数值不稳定**：IS 权重、旧 logprob、权重版本错位；
- **验证延迟**：验证也要抢 rollout 资源。

## 27.9 何时用异步

| 场景 | 建议 |
|---|---|
| 小集群、7B、短序列 | sync |
| 长尾明显、追求吞吐 | colocate_async |
| 大规模、独立 rollout 集群 | separate_async |
| 极限吞吐、能接受偏差 | fully_async |
| 调试算法 | sync |

先用 sync 确认算法正确，再逐步开异步。不要一上来就 fully async，否则很难区分“算法不收敛”与“异步实现错”。

## 27.10 监控指标

v0.9.1 的 fully async 与 rollout correction 使用不同前缀：

```text
# fully_async_policy
fully_async/count/stale_samples_processed
fully_async/count/dropped_stale_samples
fully_async/count/current_param_version
fully_async/trainer/idle_ratio
fully_async/partial/*

# rollout correction
rollout_is_mean
rollout_is_max
rollout_is_min
rollout_is_eff_sample_size
rollout_is_seq_*
rollout_is_batch_norm_factor
```

`stale_samples_processed` 持续升高说明 rollout 太慢或训练太快；`rollout_is_max` 很大、`eff_sample_size` 很小，说明 IS 权重极端，训练可能不稳定。

MiniVerl 的教学指标名不同：`async/staleness_max`、`async/dropped_samples`、`rollout_is/truncated_frac`，计时是 `timing/gen_s`、`timing/update_actor_s`、`timing/total_s`。读日志时先确认是哪个实现的口径。

## 27.11 MiniVerl 的异步模拟

MiniVerl 提供：

- `ReplayBuffer`：版本化队列、drop/keep 策略；
- `AsyncRolloutServer`：produce/pop；
- `compute_rollout_correction`：截断 IS；
- `PPOTrainer` 的 `colocate_async` / `separate_async` 模式；
- `parameter_sync_step`、`max_off_policy_threshold`。

它不是真正的并发：训练与生成仍在同一进程顺序执行，但保留了版本、staleness、IS 的语义。`examples/10_async_rollout.py` 演示了 buffer 与 staleness 指标。

## 27.12 TransferQueue：V1 的数据面

同步模式里，controller 调用一次 rollout，拿到一个 `DataProto`，直接送给训练；数据不需要“排队”。异步模式把生产与消费拆成两个独立进程后，中间必须有一个数据面：生产端不能无限往内存里堆轨迹，消费端也不能每次只拿一条。TransferQueue 就是这层抽象。

### 角色与生命周期

```text
AgentLoopManagerTQ / Rollouter          Trainer
        │                                  │
        │  put(trajectories, version)      │
        ├──────────────► TransferQueue ────┤
        │                 (bounded)        │  get(batch_size)
        │                                  │
        │◄──── backpressure / drop ◄───────┤
```

- **生产者**：每个 AgentLoop/rollout 请求完成后，把 `DataProto` 加上版本号、时间戳、来源元信息写入队列；
- **队列**：有界缓冲区，控制生产速度，防止 OOM；支持 drop/block 两种背压策略；
- **消费者**：训练器按 `require_batches` 拉取足够样本，凑成一个 update batch；
- **版本**：每个样本带产生它的策略版本，训练器据此计算 staleness 与 IS 权重；
- **快照**：队列可以 snapshot/restore，用于 trainer 重启或弹性迁移。

### 为什么不是简单的 Python queue

生产环境的 TransferQueue 要解决：

- **跨进程/跨节点**：对象存储、共享内存、RDMA；
- **大对象**：一个 batch 的 token + mask + logprob 可能几十 MB，不能走普通 pickle；
- **零拷贝**：尽量让训练侧直接读到共享 buffer；
- **背压**：生产速度 > 消费速度时，不能无限增长；
- **一致性**：训练器崩溃后，已消费/未消费的样本不能重复或丢失；
- **版本可见性**：所有 rank 看到同一个 version 才能做正确的 off-policy 修正。

MiniVerl 的 `mini_verl/transfer_queue.py` 是一个单进程版本，保留 `put/get/try_get/snapshot/restore/stats` 契约。它不解决跨节点传输，但把“有界队列 + 版本 + 快照”的语义显式化了：

```python
queue.put(batch, version=policy_version, produced_at=global_step)
batch = queue.get(require_batches * mini_batch_size)
```

`get` 可以只取部分 batch，剩余样本留在队列里；取出的 `DataProto` 会带 `queue_version` 与 `queue_produced_at`，训练器据此算 staleness。`examples/19_transfer_queue.py` 演示了部分取用、容量淘汰与 snapshot/restore。

### 与 ReplayBuffer 的区别

| 维度 | ReplayBuffer | TransferQueue |
|---|---|---|
| 位置 | trainer 内部 | 生产/消费之间 |
| 单位 | 已完成的 batch | 样本/轨迹 |
| 背压 | 由采样策略控制 | 有界队列 + drop/block |
| 版本 | 训练版本 | 生产版本 + 消费版本 |
| 快照 | 通常不需要 | 支持恢复 |

两者可以叠加：Rollouter 写入 TransferQueue，trainer 取出后放进 ReplayBuffer 做 staleness 过滤。

## 27.13 完全异步的调度、staleness 上界与动态资源

### staleness 上界的推导

设：

- 每次训练 update 消费 `B` 个样本；
- 每 `S` 次 update 同步一次权重（`trigger_parameter_sync_step=S`）；
- 队列里允许额外 `T` 个同步窗口的旧样本（`staleness_threshold=T`，比例口径，不是条数）；
- 生产端最多领先消费端 `R` 个 batch。

一个粗略上界是：训练器在同步前最多消费 `S` 个 batch，而生产端在这段时间最多生产 `S + R` 个 batch，因此最旧样本的版本差不超过 `S + R - 1`。README 给出的 `rollout_num` 公式就是在这个思路上再加上“已存在的 stale 样本数”：

$$
\text{rollout\_num}=(1+T)\times S\times \text{require\_batches}\times \text{mini\_batch}
-\text{num\_staleness\_sample}.
$$

这个公式不是定理：它假设生产/消费速率稳定、没有 partial rollout、没有重试。注意它和 27.4 是同一个公式，只是把 `T` 解释成“额外同步窗口的比例”；不要把它同时当成“T 条样本”的容量上限。真实系统里要用监控指标验证上界，而不是只信配置。

### 动态调度

`verl/experimental/fully_async_policy/dynamic_schedule/` 提供：

- `base.py`：调度策略接口；
- `fixed_ratio_policy.py`：固定 rollout/train 资源比例；
- `static_fully_async_policy.py`：静态完全异步；
- `dynamic_resource_controller.py`：根据队列长度/利用率动态调整资源。

核心权衡是：

- 队列太长 → staleness 大、内存高；
- 队列太短 → trainer 等数据、GPU 空闲；
- rollout 太多 → 训练侧吃不饱；
- 训练太多 → 生成侧排队。

动态调度的目标不是“让队列恒定”，而是“让 trainer 的空闲时间与 staleness 同时低于阈值”。这通常需要一个滞回控制器，避免在两个极端之间震荡。

### 一个最小排队模型

把 rollout 看作生产者、trainer 看作消费者。设样本到达率 $\lambda$（样本/s）、消费率 $\mu$（样本/s），利用率：

$$
\rho=\frac{\lambda}{\mu}.
$$

稳态要求 $\rho<1$；$\rho\to1$ 时队列长度和等待时间非线性上升。粗略的 M/M/1 近似：

$$
L_q\approx\frac{\rho^2}{1-\rho},\qquad W_q\approx\frac{\rho}{\mu(1-\rho)}.
$$

Little's Law 给出系统内样本数 $L=\lambda W$。这些公式假设泊松到达/指数服务，真实 RL 的长尾会让 $W_q$ 更差；但它们足以说明：**不要把利用率压到 95% 再谈稳定**，留 20%～30% 余量比事后调参便宜。

调度策略的取舍：

| 策略 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| FIFO | 简单、公平 | 长尾阻塞 | 同质任务 |
| 按 staleness 优先 | 降低旧样本比例 | 可能饿死新样本 | 异步 RL |
| 按长度 SJF | 降低平均等待 | 长样本饿死 | 长度差异大 |
| 抢占/partial | 避免长尾占资源 | 状态管理复杂 | 长尾严重 |

配置 `require_batches`/`trigger_parameter_sync_step` 时，先测 $\lambda$ 与 $\mu$，再按目标 $\rho$ 反推队列容量；不要凭感觉设一个“看起来很大”的数。

### 失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| 队列无限增长 | 生产 > 消费，无背压 | 设有界队列 + drop/block |
| staleness 超阈值 | 同步太慢/生产太快 | 降 `parameter_sync_step`、加同步频率 |
| trainer 空转 | 队列太短/rollout 太慢 | 加 rollout 副本、预热 batch |
| 恢复后重复样本 | snapshot 未记录消费位置 | 消费确认与快照原子化 |
| 版本错位 | 生产者/消费者看到的 version 不一致 | 版本号随 batch 传递，训练前校验 |
| partial rollout 状态丢失 | 只存了 token，没存 KV/请求状态 | 请求级暂停/恢复接口 |

### 何时不要用 fully async

- 算法还在调试：同步模式更容易定位“算法不收敛”还是“异步实现错”；
- 集群小、rollout 不是瓶颈：异步的复杂度换不来吞吐；
- 对 off-policy 偏差敏感：先做 one-step off-policy，确认 IS 修正有效，再放大 staleness。

## 本章小结

- 同步 RL 在长尾上浪费 GPU；异步用重叠换吞吐，但引入 off-policy 偏差。
- V1 有 sync/colocate_async/separate_async 三种模式；fully_async 是更激进的流式实现。
- staleness、partial rollout、rollout correction 是异步的三块基石。
- 异步的收益依赖硬件与负载，不能盲目套用。
- MiniVerl 用版本化 buffer + 截断 IS 演示核心语义。

## 延伸阅读

- `verl/experimental/fully_async_policy/README.md`
- `verl/experimental/one_step_off_policy/`
- `verl/trainer/ppo/v1/trainer_separate_async.py`
- 下一章：蒸馏、DPO、SFT 与偏好优化。
