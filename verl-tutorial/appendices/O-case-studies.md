# 附录 O 端到端案例研究：数学推理、代码 Agent 与多模态 RL

本附录把正文的系统知识串成三个可落地的设计方案：关键配置 + 命令入口 + 失败模式。每个案例都按“目标 → 数据 → 算法 → 系统 → 指标 → 失败模式”展开；数字是量级假设，不是 benchmark 结论。完整启动命令见第 6 章与 `examples/grpo_trainer/README.md`，recipe 见 `verl-recipe`。

## O.1 案例一：7B 数学推理 RLVR

### 目标

让一个 7B instruct 模型在可验证数学题上提升 pass@1，同时不显著牺牲通用能力。奖励是规则 verifier，不需要 reward model。

### 数据

- 训练集：GSM8K/MATH 风格，prompt + 标准答案；答案统一成 `\boxed{}` 或可解析数字；
- 验证集：与训练集去重，按难度分层；
- 难度：先用 base 模型采样 8 条，保留正确率在 10%～90% 的题；
- 污染：训练集与评测集做 n-gram/embedding 去重，记录数据版本。

### 算法

```yaml
algorithm:
  adv_estimator: grpo
  use_kl_in_reward: false
  filter_groups:
    enable: true
    metric: reward
    max_inflight_gen_batches: 1   # V1 用这个限制补采并发；max_num_gen_batches 会被忽略
actor_rollout_ref:
  actor:
    use_kl_loss: true
    kl_loss_coef: 0.001
    clip_ratio_low: 0.2
    clip_ratio_high: 0.28
    loss_agg_mode: token-mean
  rollout:
    n: 8
    temperature: 1.0
    top_p: 1.0
data:
  max_response_length: 8192
reward:
  reward_manager:
    name: dapo
  reward_kwargs:
    max_resp_len: 8192
    overlong_buffer_cfg:
      enable: true
      len: 512
      penalty_factor: 1.0
```

启动入口（示意，数据/模型路径按实际替换）：

```bash
python3 -m verl.trainer.main_ppo \
  data.train_files=$DATA/train.parquet \
  data.val_files=$DATA/test.parquet \
  actor_rollout_ref.model.path=$MODEL \
  actor_rollout_ref.rollout.name=vllm \
  algorithm.adv_estimator=grpo \
  algorithm.filter_groups.enable=true
```

DAPO 的四个改动按需开启：

- 动态过滤：去掉全对/全错的组；
- clip-higher：放宽上界，鼓励探索；
- token-level loss：长序列不被短序列淹没；
- overlong penalty：抑制无限长思考。

### 系统

- 8×H100，FSDP2 训练 + vLLM rollout；
- `rollout.n=8`，`train_batch_size=128`，每步 1024 条轨迹；
- 长尾明显：先 sync 跑通，再开 colocate_async；
- 权重同步用 nccl；如果只训 LoRA，用 `delta_sharded`（v0.9.1 注册的后端名；`delta` 只存在于 MiniVerl 的教学实现）；
- checkpoint 每 20 步，保存优化器状态。

### 指标

```text
val/pass@1, val/pass@8
data/reward_mean, data/truncated_ratio
data/response_length_mean
actor/ppo_kl, actor/entropy
timing/gen vs timing/update_actor
```

### 失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| reward 涨、pass@1 不涨 | 长度奖励/格式 hack | 检查样例，收紧 verifier |
| 全部组被过滤 | 题目太简单/太难 | 调难度，降过滤阈值 |
| 响应越来越长 | 缺 overlong penalty | 加长度惩罚/降 max length |
| KL 爆炸 | lr 太大/ref 不对齐 | 降 lr，检查 ref |
| 吞吐低 | rollout 长尾 | 异步/长度均衡/prefix cache |

## O.2 案例二：代码 Agent RL

### 目标

让模型在多轮工具调用中完成仓库级修改：读文件、跑测试、修 bug。奖励来自测试通过率与代码质量检查。

### 数据

- 每个样本：仓库快照 + issue + 测试命令；
- 工具：读文件、搜索、编辑、运行测试；
- 轨迹：多轮 assistant/tool 交替，tool observation 不参与策略损失；
- 难度：按测试通过率与轨迹长度分层。

### 算法

- GRPO + 多轮 AgentLoop；
- 奖励：测试通过（0/1）+ 格式/工具使用惩罚 + 代码质量（lint/复杂度）；
- turn-level 信用分配：结果奖励回填到每个 assistant turn，或训练 PRM；
- 过滤：全失败/全成功的组去掉；
- 安全：所有代码执行在沙箱，见附录 N。

### 系统

```text
AgentLoop workers ──► TransferQueue ──► trainer
        │                                   │
        └── sandbox pool (no network) ◄─────┘
```

- rollout 与训练分离（separate_async），因为工具执行延迟高；
- 队列有界，防止慢沙箱撑爆内存；
- 每个 request 带幂等 id，重试不重复跑测试；
- 权重同步频率低于纯文本 RL，因为轨迹更贵；
- 监控 `tool/latency_p99`、`sandbox/timeout_rate`、`reward/error_rate`。

### 指标

```text
val/test_pass_rate
agent/turns_mean, agent/tool_calls_mean
agent/mask_ratio（observation 占比）
reward/format_error_rate
sandbox/timeout_rate
```

### 失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 模型学会“不调用工具” | 工具调用无收益/惩罚过重 | 调奖励，给工具使用正反馈 |
| 轨迹越来越长 | 无 turn 惩罚 | 加轮数/长度惩罚 |
| 测试通过但代码很差 | 奖励只看测试 | 加 lint/复杂度/人工抽检 |
| 沙箱排队 | 并发不足 | 加沙箱副本/批处理 |
| 训练不稳定 | 轨迹方差大 | 增大 group、过滤极端样本 |

## O.3 案例三：多模态 VLM RL

### 目标

提升模型在图表/几何题上的推理能力。输入是图像 + 文本，输出是带推理步骤的答案。

### 数据

- 图像、问题、标准答案；
- `image_grid_thw` 与视觉 token 预算；
- 图像分辨率分层：小图/大图/长视频帧；
- 奖励：答案 exact match + 格式 + 可选 PRM。

### 算法

- GRPO/DAPO，与文本 RL 相同；
- 视觉塔通常冻结，只训练语言侧/LoRA；
- 多模态 token 不参与 loss（只作为条件）；
- 长视觉序列需要动态 batch/序列并行；
- 如果视觉 token 很多，rollout 的 KV cache 是主要成本。

### 系统

- FSDP2 + vLLM（支持视觉输入）；
- 图像预处理在 dataloader，避免阻塞训练；
- 视觉 token 长度差异大，必须长度均衡；
- 权重同步包含视觉塔（如果训练）与 adapter；
- 评测要固定图像分辨率与预处理，否则指标不可比。

### 指标

```text
val/accuracy（按图像类型分组）
data/visual_token_mean
data/response_length_mean
perf/tokens_per_s（区分视觉/文本）
```

### 失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 模型忽略图像 | 视觉 token 被截断/ mask 错 | 检查 processor 与 mask |
| 显存爆炸 | 视觉 token 太多 | 降分辨率/帧采样/序列并行 |
| 训练/推理不一致 | 预处理不同 | 固定 processor 版本与参数 |
| 指标虚高 | 评测图与训练图重叠 | 去重/时间切分 |

## O.4 三个案例的横向对比

| 维度 | 数学 RLVR | 代码 Agent | 多模态 RL |
|---|---|---|---|
| 奖励 | 规则 verifier | 测试 + 质量 | 规则 + 可选 PRM |
| 轨迹 | 单轮长思维 | 多轮工具 | 单轮多模态 |
| 主要成本 | 生成长尾 | 沙箱/工具延迟 | 视觉 token/KV |
| 关键系统能力 | 异步/长度均衡 | 队列/沙箱/隔离 | 动态 batch/序列并行 |
| 主要风险 | reward hacking | 安全/越权 | 预处理不一致 |
| 建议起步 | sync + GRPO | separate_async + 队列 | FSDP2 + 固定预处理 |

## O.5 设计检查清单

无论哪个案例，上线前都应回答：

- [ ] 奖励是否可验证、是否容易被 hack？
- [ ] 数据与评测是否去重、版本是否固定？
- [ ] 训练/推理的 tokenizer、processor、mask 是否一致？
- [ ] 长尾是否会让 GPU 空转？是否需要异步？
- [ ] checkpoint 是否包含优化器与队列状态？
- [ ] 代码/工具执行是否沙箱化？
- [ ] 指标是否能区分“能力提升”与“格式/长度 hack”？
- [ ] 失败样本是否会静默变成 0 分？

## 本章小结

- 数学 RLVR 的核心是数据难度、GRPO/DAPO 配置与长尾吞吐。
- 代码 Agent 的核心是沙箱、队列、多轮 mask 与 turn-level 信用分配。
- 多模态 RL 的核心是视觉 token 预算、预处理一致性与动态 batch。
- 三个案例共享同一套系统骨架：数据 → rollout → reward → advantage → update → sync。
- 先跑通最小闭环，再逐项加异步、Agent、多模态；不要一次把所有复杂度叠上去。

## 延伸阅读

- 第 3 章：GRPO 家族
- 第 15 章：Rollout 与 PD 分离
- 第 26 章：AgentLoop
- 第 27 章：异步 RL 与 TransferQueue
- 附录 K：过程奖励、树搜索与自我博弈
- 附录 N：安全、沙箱与多租户
