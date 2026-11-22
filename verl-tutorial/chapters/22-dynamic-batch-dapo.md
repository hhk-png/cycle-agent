# 第 22 章 动态批、长度均衡、DAPO 过滤与过采样

## 22.1 一个 batch 的四种“大小”

RL 配置里有很多 batch 字段，含义不同：

| 字段 | 含义 | 单位 |
|---|---|---|
| `data.train_batch_size` | 每次取多少 prompt | prompt |
| `rollout.n` | 每个 prompt 采样几条 | 倍数 |
| `actor.ppo_mini_batch_size` | 每次更新多少序列 | 序列 |
| `ppo_micro_batch_size_per_gpu` | 每卡每次前向多少序列 | 序列 |

关系：

```text
总序列数 = train_batch_size × rollout.n
mini-batch 数 = 总序列数 / ppo_mini_batch_size
micro-batch 数 = ppo_mini_batch_size / (micro_batch_size × DP)
```

如果 `train_batch_size=1024`、`n=8`、`mini=256`，则总序列 8192，mini-batch 32 个。

## 22.2 动态 batch

固定 batch 的问题是长度波动。动态 batch 用 token 预算：

```yaml
actor_rollout_ref:
  actor:
    use_dynamic_bsz: true
    ppo_max_token_len_per_gpu: 16384
```

算法大致是：

1. 按长度排序；
2. 依次放入样本，直到累计 token 接近预算；
3. 每个 bucket 作为一个 micro-batch。

优点：

- 长序列不会 OOM；
- 短序列不会浪费；
- 每个 micro-batch 的有效 token 数接近。

代价：

- micro-batch 的样本数不固定；
- 需要按全局 token 数归一化 loss；
- 与 PP/CP 组合时更复杂。

## 22.3 长度均衡

即使动态 batch，DP rank 之间仍可能不均。真实 verl 的 `balance_batch=true` 走 `karmarkar_karp(...)`（LDM 近似最优划分），而不是简单 LPT；MiniVerl 用堆实现 LPT 作为教学替代（分区结果不保证一致）。下面这段是 MiniVerl 的写法：

```python
order = argsort(-lengths)
heap = [(0.0, rank) for rank in range(world_size)]
for idx in order:
    load, rank = heappop(heap)
    groups[rank].append(idx)
    heappush(heap, (load + lengths[idx], rank))
```

目标是让每个 rank 的 `sum(length)` 接近。指标：

```text
data/seqlen_imbalance = (max_rank_sum - min_rank_sum) / mean_rank_sum
```

MiniVerl 的 `_balance_batch` 就是这个算法，测试 `test_balance_batch` 验证长度不变、指标非负。

## 22.4 DAPO 动态采样

DAPO 的核心观察：如果一组样本的奖励全相同，GRPO 优势为 0，这条数据白采样。动态采样：

1. 计算每个 prompt 组的奖励；
2. 过滤掉组内奖励相同的组；
3. 用新 prompt 补足 batch；
4. 重复直到达到目标 batch 或达到最大生成轮数。

verl 的配置：

```yaml
algorithm:
  filter_groups:
    enable: true
    metric: reward
    max_num_gen_batches: 0
    max_inflight_gen_batches: 1
```

- `metric`：用 `reward` 还是 `reward_extra_info` 里的字段（如 `acc`）；
- `max_num_gen_batches`：旧版/非 V1 路径的“最多重新生成几轮”。**V1 内置 `ReplayBuffer` 会读取它但打印 warning 并忽略**；V1 的 DAPO 过滤是“逐 prompt 精确补采”，不再按整批轮数控制。
- `max_inflight_gen_batches`：V1 sync DAPO 同时在飞的生成 batch 数，用来限制补采的并发度。

V1 在 DAPO 开启时会把 `data.gen_batch_size` 强制设为 1（逐 prompt 取数，才能精确补足被过滤的组），所以也不要再用 `gen_batch_size > train_batch_size` 做过采样开关。`filter_groups` 需要奖励在采样时可见，所以规则奖励或 `reward_model.enable_resource_pool=true` 才支持。

MiniVerl 的 `filter_groups_for_dapo` 实现“保留非恒定奖励组”，`PPOTrainer.train_step` 在 reward 后调用。它没有“补采样”循环，只过滤当前 batch；这是教学简化。

## 22.5 过采样（over-sampling）

“过采样”在 verl 里有三个容易混淆的入口：

| 入口 | 所在层 | 语义 | V1 是否使用 |
|---|---|---|---|
| `data.gen_batch_size > data.train_batch_size` | 数据加载 | 一次取更多 prompt，过滤后凑目标 | **否**；DAPO 开启时被强制为 1 |
| `algorithm.filter_groups.max_num_gen_batches` | 过滤/重采样 | 旧版“最多生成几轮” | **否**；V1 读取后 warning 并忽略 |
| `algorithm.filter_groups.max_inflight_gen_batches` | 过滤/重采样 | 同时有多少个生成 batch 在飞 | **是**；V1 用来限制补采并发 |
| `rollout.over_sample_rate` | rollout | 完成比例达到 `1-rate` 时提前终止剩余请求 | 是；但它是“提前停”，不是“多生成” |

`rollout.over_sample_rate` 的语义值得单独说清：它在 `(1 - over_sample_rate) × 总请求数` 完成后提前终止剩余请求，用来削掉长尾，**不是**“多生成再过滤”。设成 0.1 表示“完成 90% 就停”，代价是剩下 10% 的样本可能缺失；它与 DAPO 的补采是两套机制，不要互相替代。

V1 的 DAPO 补采逻辑是：过滤后按缺失的组数逐 prompt 补采（`gen_batch_size=1`），并用 `max_inflight_gen_batches` 控制同时在飞的生成量。它不需要、也不支持“先生成 320 再过滤到 256”的整批过采样配方。

一个可用的 V1 配置形态：

```yaml
data:
  train_batch_size: 256
  # 不要设 gen_batch_size>1；DAPO 开启时会被覆盖
algorithm:
  filter_groups:
    enable: true
    metric: reward
    max_inflight_gen_batches: 1
rollout:
  over_sample_rate: 0.0   # 想提前停时再调大，注意它与过滤补采是两件事
```

真正的“多生成”如果确实需要，应该走自定义 `ReplayBuffer`/sampler（`trainer.v1.sampler.custom_sampler.path/name`）或非 V1 的 recipe，而不是改 `gen_batch_size`。代价是更多 rollout 与更复杂的 staleness 控制。

MiniVerl 只实现“过滤当前 batch”，没有补采样循环，因此不需要这些参数；这是教学简化，不是生产配方。

## 22.6 课程与难度

动态采样只解决“组内无差异”，不解决“整批太难/太简单”。进一步策略：

- 按难度分桶，逐步提升；
- 用 pass rate 估计难度；
- 对太简单的组降采样；
- 对太难的组增加提示/分解。

verl 没有内置课程学习器；通常通过自定义 `ReplayBuffer` 或数据采样器实现。

## 22.7 过滤的副作用

- **分布偏移**：过滤后只剩“中等难度”样本，训练分布与验证分布不同；
- **批大小不稳定**：过滤太多会导致 batch 变小；
- **指标偏差**：训练 reward 可能看起来更高，因为简单/困难组被过滤；
- **实现错误**：按错误 key 分组会过滤掉所有组。

监控：

```text
dapo/filtered_groups
dapo/kept_groups
dapo/filtered_ratio
```

如果 `filtered_ratio` 接近 1，说明奖励几乎全恒定，训练没有信号。

## 22.8 长度惩罚

v0.9.1 的 DAPO overlong 惩罚不是“截断就按比例扣分”，而是**在最大长度前留一个 buffer，进入 buffer 后线性扣分，最多扣到 0**：

```python
# verl/workers/reward_manager/dapo.py
expected_len = max_resp_len - overlong_buffer_len
exceed_len = valid_response_length - expected_len
reward += min(-exceed_len / overlong_buffer_len * penalty_factor, 0)
```

它只在 `overlong_buffer_cfg.enable=True` 时生效，且没有 `truncated` 判断：

- 响应长度没进入最后 `overlong_buffer_len` 个 token 时，`exceed_len <= 0`，惩罚为 0；
- 进入 buffer 后按超出比例线性增加；
- `min(..., 0)` 保证只罚不奖。

配置在 reward kwargs 下：

```yaml
reward:
  reward_manager:
    name: dapo
  reward_kwargs:
    max_resp_len: 8192
    overlong_buffer_cfg:
      enable: true
      len: 512
      penalty_factor: 1.0
      log: true
```

这鼓励模型在预算内完成推理，而不是无限生成。注意：

- 惩罚太强会让模型过早停止；
- 惩罚太弱会让长度爆炸；
- `max_resp_len` 必须与 `data.max_response_length` 对齐，否则 buffer 的位置算错；
- 验证时截断样本要单独统计。

MiniVerl 的 `DAPORewardManager` 是简化版：它在 `truncated=True` 时扣 1.0（或按 `overlong_penalty_factor` 线性扣分），没有 buffer 配置。它验证的是“截断样本被惩罚”这一契约，不是 verl 的完整 buffer 公式；生产配置不要照搬 MiniVerl 的字段。

## 22.9 批大小与吞吐

经验：

- `ppo_mini_batch_size` 太小：更新噪声大、吞吐低；
- 太大：显存高、样本利用率低；
- `micro_batch_size` 受显存限制；
- 动态 batch 让 token 预算成为真正的约束。

调参顺序：

1. 先确定能放下的 `micro_batch_size`；
2. 再定 `mini_batch_size`（通常是 micro × DP × accumulation）；
3. 最后调 `train_batch_size` 与 `n`（影响数据新鲜度与成本）。

## 22.10 MiniVerl 对照

- `_balance_batch`：长度均衡；
- `filter_groups_for_dapo`：动态采样；
- `DAPORewardManager`：overlong penalty；
- `rollout.n`：组采样；
- `ppo_mini_batch_size`：更新批。

MiniVerl 的 `examples/08_dapo_filtering.py` 演示了过滤统计与一次 DAPO 更新。

## 本章小结

- `train_batch_size`、`rollout.n`、`ppo_mini_batch_size`、micro batch 是四个不同层级。
- 动态 batch 用 token 预算替代固定样本数，适合长尾。
- 长度均衡让 DP rank 的 token 数接近，减少 straggler。
- DAPO 过滤去掉零优势组；V1 逐 prompt 精确补采，`max_num_gen_batches` 被忽略，`gen_batch_size` 被强制为 1。
- `max_inflight_gen_batches` 控制补采并发；`rollout.over_sample_rate` 是提前停，不是多生成。
- 过滤会改变数据分布，必须监控 filtered_ratio 与验证指标。

## 延伸阅读

- DAPO 论文：<https://arxiv.org/abs/2503.14476>
- `verl/trainer/ppo/v1/replay_buffer.py`
- `verl/trainer/config/algorithm.py`
- 下一章：Checkpoint、断点续训、模型合并与 HF 导出。
