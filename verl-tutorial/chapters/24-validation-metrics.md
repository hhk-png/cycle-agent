# 第 24 章 验证、指标、日志与实验管理

## 24.1 训练指标与验证指标

训练指标回答“优化目标是否在动”；验证指标回答“模型是否真的变好”。

训练指标：

```text
data/reward_mean
data/accuracy
data/response_length_mean
data/advantage_mean/std
actor/pg_loss, actor/pg_clipfrac, actor/ppo_kl, actor/entropy
critic/vf_loss, critic/vf_clipfrac
timing/*
perf/tokens_per_s, perf/mfu
```

验证指标：

```text
val/accuracy
val/pass@1
val/pass@k
val/reward_mean
val/response_length_mean
```

训练 reward 上涨但验证不涨，是最常见的“假进步”信号。

## 24.2 pass@k

对每个 prompt 生成 $n$ 条，$c$ 条正确，pass@k 的无偏估计：

$$
\text{pass@}k=1-\frac{\binom{n-c}{k}}{\binom{n}{k}}.
$$

当 $n-c<k$ 时，pass@k=1。

注意：

- 验证时 `rollout.n` 要 >= k；
- 温度/采样策略影响 pass@k；
- pass@k 不是训练目标，不能直接优化；
- 代码任务常用 pass@1/pass@10。

v0.9.1 的真实入口是 `process_validation_metrics`（`verl/trainer/ppo/metric_utils.py`），它按 `data_source` 分组统计；源码里没有名为 `compute_validation_metrics` 的函数。MiniVerl 的 `compute_validation_metrics` / `compute_pass_at_k` 是教学实现，实现了同一公式。

**口径警告**：MiniVerl 的 `val/accuracy` 是 `rm_scores > 0.5`，而 `rm_scores` 是**训练奖励**（可能含 overlong/格式 shaping）。例如 DAPO 的 overlong penalty 会把“正确但被截断”的样本压到 0.5，从而被算作错误。做严肃评测时，要单独记录原始 correctness（或 `acc` 字段），不要把 shaped reward 直接当准确率；附录 L 的独立 harness 就是为此。

## 24.3 验证的生成策略

- 贪心（temperature=0）：稳定、可复现，适合 pass@1；
- 采样（temperature>0）：适合 pass@k；
- `val_kwargs` 单独配置，避免影响训练采样；
- 验证 batch 可以更大（无梯度）；
- 验证时关闭 dropout（eval 模式）。

MiniVerl 的 `validate` 用 `SamplingParams(temperature=0, do_sample=False)`。

## 24.4 指标聚合

一次 step 有多个 mini-batch/micro-batch，指标要按样本数或 token 数加权：

```python
total[key] += value * n
weight[key] += n
mean = total / weight
```

不能简单平均每个 micro-batch 的 loss，因为长度不同。verl 的 `metric_utils.py` 提供 `compute_data_metrics`、`compute_timing_metrics`、`compute_throughout_metrics`（源码拼写就是 `throughout`）等；MiniVerl 对应的是 `compute_throughput_metrics`。

MiniVerl 的 `MetricsAggregator` 用 `RunningMean` 做加权平均；`_update_worker` 按 mini-batch 大小加权。

## 24.5 日志后端

verl 支持：

- `console`
- `wandb`
- `tensorboard`
- `swanlab`
- `mlflow`
- `rl_insight`（v0.9.1 新增，在线 RL 可观测性）

配置：

```yaml
trainer:
  logger: [console, wandb]
  project_name: my_project
  experiment_name: grpo_qwen3_8b
```

最佳实践：

- 每个实验一个 `experiment_name`；
- 记录完整配置（Hydra 会自动记录）；
- 记录 git commit；
- 记录数据/模型版本；
- 记录 reward 函数版本。

## 24.6 生成样例日志

`trainer.log_val_generations` 可以记录验证生成的样例：

```yaml
trainer:
  log_val_generations: 16
```

它会打印 prompt、response、reward，便于人工检查：

- 模型是否在胡说；
- 格式是否正确；
- 是否 reward hacking；
- 多轮工具调用是否正常。

对多模态任务，样例日志还包括图像/视频引用。

## 24.7 rollout 数据落盘

`trainer.rollout_data_dir` 可以把每步的 rollout 数据写到磁盘：

```yaml
trainer:
  rollout_data_dir: /data/rollouts
  validation_data_dir: /data/validation
```

用途：

- 离线分析；
- 复现 reward；
- 训练 reward model；
- 调试 mask/长度。

注意数据量很大，生产环境要采样/压缩/定期清理。

## 24.8 性能指标

真实 verl v0.9.1 的吞吐指标键是：

```text
perf/throughput            # tokens/s/GPU
perf/total_num_tokens
perf/time_per_step
perf/mfu/actor             # 训练侧 actor MFU
perf/mfu/critic            # 训练侧 critic MFU
```

MiniVerl 为了教学可读，用了另一组键：

```text
perf/tokens_per_s
perf/samples_per_s
perf/tflops
perf/mfu
```

两者不是同名兼容关系；做日志看板或跨系统对比时要先对齐口径。

MFU（Model FLOPs Utilization）：

$$
\text{MFU}=\frac{\text{actual FLOPs/s}}{\text{peak FLOPs/s}}.
$$

粗略估算：

- 训练每 token 约 $6N$ FLOPs；
- 推理每 token 约 $2N$ FLOPs；
- $N$ 是参数量。

MFU 受 attention、通信、padding、batch 影响。7B 模型在 H100 上，训练 MFU 30%～50% 是常见区间；RL 因为 rollout/同步，端到端会更低。

## 24.9 实验管理

一个可复现实验至少记录：

- 代码版本（git commit）；
- 配置（YAML）；
- 数据版本/哈希；
- 模型版本；
- reward 函数版本；
- 随机种子；
- 硬件/驱动/CUDA/vLLM 版本；
- 关键指标曲线；
- 验证样例。

MiniVerl 的 `MetricsLogger` 写 `metrics.jsonl`，并在 console 打印关键指标；它不依赖 wandb，但字段名与 verl 对齐。

## 24.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 指标不更新 | 聚合 key 错/未 flush | 检查 logger 与 step |
| 训练/验证 reward 差距大 | 过拟合/reward hacking | 早停、换验证集 |
| pass@k 异常 | n<k/采样温度 | 调 val_kwargs |
| MFU 很低 | padding/通信/小 batch | 动态 batch、TP、profiler |
| 日志缺失 | logger 配置/权限 | 检查目录与后端 |

## 24.11 MiniVerl 对照

- `mini_verl/metrics.py::compute_data_metrics`
- `compute_timing_metrics`
- `compute_throughput_metrics`
- `compute_validation_metrics` / `compute_pass_at_k`
- `MetricsLogger`
- `mini_verl/trainer/ppo_trainer.py::validate`

测试 `test_trainer.py::test_validation_metrics` 与 `test_metrics_logger` 覆盖了 pass@k 与 JSONL 输出。

## 24.12 可复现实验：从 wandb run 到可检索的 artifact

日志后端解决“看曲线”，不解决“三个月后还能不能复现”。一个可复现实验需要三样东西：**输入指纹、代码指纹、评测指纹**。

### 输入指纹

- 训练/验证数据的路径 + sha256；
- tokenizer/processor 的版本与哈希；
- 模型基座的 HF repo + revision（不要只写模型名）；
- reward 函数的版本/哈希；
- 关键配置的规范化 YAML + sha256。

### 代码指纹

- git commit；
- 是否有未提交改动（dirty）；
- 关键依赖版本（torch/vLLM/transformers）；
- 自定义 patch 的 diff 或 commit。

### 评测指纹

- 采样参数（temperature/top-p/n/max_tokens）；
- parser 版本；
- 验证集版本；
- 指标计算脚本的 commit。

一个最小 manifest 可以写成 JSON，和 checkpoint 放在一起：

```json
{
  "experiment_id": "grpo-qwen3-8b-dapo-v3",
  "code": {"repo": "verl", "commit": "<sha>", "dirty": false},
  "data": {"train": "<path>", "train_sha256": "<sha>", "val": "<path>", "val_sha256": "<sha>"},
  "model": {"base": "Qwen/Qwen3-8B", "revision": "<sha>"},
  "reward": {"version": "reward-v4", "sha256": "<sha>"},
  "runtime": {"gpu": "8xH100", "torch": "2.11.0", "vllm": "0.24.0"},
  "seed": 1234,
  "metrics": {"val_accuracy": 0.71, "val_pass@8": 0.89}
}
```

### 回归基线

每次改动后，除了看新实验，还要跑一组固定基线的“冒烟回归”：

- 固定 200 条验证样本；
- 固定采样参数；
- 记录 accuracy/pass@k/长度/延迟；
- 与新实验对比，差异超过阈值就告警。

回归基线不需要很大，但要**稳定**。它能抓到“改了一个看似无关的 mask，验证集掉了 2 个点”这类问题。

更完整的项目级 manifest 与 30/60/90 天流程见附录 R。

## 24.13 独立评测与生成服务器：把“训练时验证”变成可交付的接口

训练循环里的 `validate()` 很方便，但它不是评测系统：

- 它复用训练进程、训练 reward manager 和训练数据管线；
- 它默认贪心、`n=1`，回答不了 pass@k；
- 它的 reward 可能带 overlong/格式 shaping；
- 它随训练进程一起退出，无法被 CI、标注平台或线上服务调用。

verl 因此把“生成”和“打分”拆成两个独立的离线入口：

- `verl/trainer/main_generation_server.py`：启动 standalone rollout servers，从 prompt parquet 批量生成 `n` 条响应，写回一个新的 parquet。它是**批量生成脚本**，不是常驻请求服务。
- `verl/trainer/main_eval.py`：读取已经生成的 parquet（`data.path`、`response_key`、`data_source_key`、`reward_model_key`），用 reward function 按 `data_source` 打分并打印 `test_score/<source>`。它**不加载 checkpoint、不做生成**。

所以真实 verl 的“评测流水线”是两步：先用 `main_generation_server.py` 生成，再用 `main_eval.py` 打分。两者不共享训练循环；常驻在线服务需要在这之上自己包一层 HTTP/gRPC 与鉴权。

### 独立评测的五个契约

1. **配置与被测模型绑定。** 生成时用的模型/采样配置要写进产物（或 checkpoint 目录）；否则三个月后你用新代码评测旧权重，指标不可比。MiniVerl 的 `main_eval` 直接读取 `global_step_<N>/config.yaml`；真实 verl 的 `main_eval` 读的是生成 parquet 与 reward 配置。
2. **评测 reward 与训练 reward 分离。** 训练可以加 overlong penalty、格式奖励、KL 惩罚；评测只应看任务本身的 correctness。MiniVerl 在评测入口强制 `reward.reward_manager=naive`。
3. **采样口径固定并记录。** `n`、temperature、top-p、max_tokens、stop strings、chat template、parser 版本，任何一个变了，pass@k 都不可比。
4. **按 prompt 分组。** `n` 条样本必须连续排列（`interleave=True`），否则 pass@k 的“同题组”会被拆散。MiniVerl 的 `PPOTrainer.evaluate` 显式做这一步；真实 verl 的 `main_eval` 按 `data_source` 分组。
5. **输出机器可读。** 生产封装至少输出 JSON：`accuracy`、`pass@k`、`n`、样本数、模型/数据指纹。v0.9.1 的 `main_eval.py` 只打印 `test_score/<source>` 字典，CI 集成要自己做一层 JSON 包装。

### MiniVerl 的独立评测

MiniVerl 把真实 verl 的两步（生成 → 打分）合成一个入口 `mini_verl/trainer/main_eval.py`：从 checkpoint 读配置、重建 trainer、生成 `n` 条样本、再用 naive reward 算 pass@k。它更接近“可交付的评测接口”，但不是真实 `main_eval.py` 的逐行对应。

```bash
# 训练时保存的 checkpoint 目录
python -X utf8 -m mini_verl.trainer.main_eval \
  --checkpoint outputs/checkpoints/demo/grpo/global_step_60 \
  --n 8 --k 1,4,8
```

```text
checkpoint=... split=val n=8
val/accuracy=0.938
val/pass@1=0.938
val/pass@4=1.000
val/pass@8=1.000
```

实现上只有三步：

```python
config = load_config(checkpoint / "config.yaml", overrides)
config.trainer.resume_mode = "auto"
config.trainer.resume_from_path = str(checkpoint)
config.reward.reward_manager = "naive"       # 不用训练 shaping
trainer = PPOTrainer(config).init()
metrics = trainer.evaluate(n=n, ks=ks, split=split)
```

`evaluate` 与 `validate` 的区别：

| | `validate` | `evaluate` |
|---|---|---|
| 采样 | 贪心、n=1 | n 条/题，可调温度 |
| 数据 | `val_batches` | `val` 或 `train` |
| reward | 当前 reward manager | 评测入口强制 naive |
| 指标 | accuracy/pass@1 | accuracy/pass@k |
| 用途 | 训练中监控 | 交付、回归、CI |

示例 `examples/21_standalone_eval.py` 走完整条链路：训练 → 保存 → 新进程加载 → pass@k。

**口径提醒**：pass@k 的分母是“题数”，不是“样本数”。MiniVerl 的 `compute_pass_at_k` 用 `len(scores)//n` 分组，余数样本被丢弃；生产评测要保证总样本数是 `n` 的整数倍，或显式记录丢弃数。真实 verl 的 `process_validation_metrics` 会按 `data_source` 分组，跨数据源的 pass@k 不能直接平均——应该先分源算，再按题数加权。

### 批量生成脚本与常驻服务的契约

`main_generation_server.py` 的实际行为是：

1. 读 prompt parquet（`data.train_files`/`data.prompt_key`）；
2. 按 `rollout.tensor_model_parallel_size` 计算副本数，启动 standalone rollout servers；
3. 把 prompt 切成 `num_replicas` 份，并发调用每个 server 的 `/v1/chat/completions`；
4. 每个 prompt 生成 `rollout.n` 条，reshape 成 `(num_prompts, n)`；
5. 写回 `data.output_path` 指向的新 parquet（列名 `responses`）。

所以它**不是**一个等待外部请求的常驻 API。它是“离线批量生成器”，只是内部用 HTTP 与 rollout server 通信。如果你要在它之上做常驻服务，需要自己补一层：

- **协议**：请求里要有 prompt（或 messages）、采样参数、`request_id`；响应里要有 token、文本、finish_reason、模型版本。流式接口还要处理分块与取消。
- **tokenization 所有权**：服务端做 chat template 和 tokenize，客户端不要重复拼接；否则模板版本一变，训练/评测分布就漂移。
- **批处理与排队**：continuous batching 由引擎负责；服务层要设置最大并发、队列上限和超时，避免一个长请求拖垮全部副本。
- **版本戳**：响应里带权重版本/step，否则异步 RL 无法判断样本由哪个策略产生。
- **健康检查**：`/health` 要能区分“进程活着”和“模型可生成”；后者需要一次极短的 warmup 生成。
- **安全**：鉴权、请求大小限制、prompt/输出日志脱敏；对外服务还要防 prompt 注入和滥用（附录 N）。

一个最小的请求/响应可以长这样（这是“常驻服务”的契约，不是 `main_generation_server.py` 的批量 parquet 格式）：

```json
{"request_id": "r-001", "messages": [{"role": "user", "content": "1+1=?"}],
 "temperature": 0.0, "max_tokens": 32}
```

```json
{"request_id": "r-001", "text": "2", "finish_reason": "stop",
 "model_step": 60, "prompt_tokens": 12, "completion_tokens": 1}
```

### 评测与服务的常见错误

| 现象 | 根因 | 处理 |
|---|---|---|
| 离线指标很好，服务上很差 | chat template/parser 不一致 | 服务端统一 tokenize，版本化模板 |
| pass@k 随 n 变化 | 没按题分组/丢弃余数 | 保证 `len(scores) % n == 0` 并记录 |
| 评测复现不了 | 配置来自工作区而非 checkpoint | 从 checkpoint 读 config |
| 指标虚高 | 用了训练 shaped reward | 评测用 naive reward/correctness |
| 指标在测奖励模型而不是任务 | reward manager 是 RM/PRM | 独立 verifier 测真实准确率；RM/PRM 分数只作代理（17.16、17.17） |
| 服务长尾 | 无队列上限/超时 | 并发限制 + 超时 + 取消 |
| 异步样本对不上策略 | 响应没有权重版本 | 响应带 `model_step`/version |

### 上线检查清单

- checkpoint 目录自带 config、tokenizer、trainer_state；
- 评测脚本能从零环境跑通，不依赖训练进程；
- 固定一套评测 manifest（见 24.12）；
- 服务端/客户端对 tokenizer 与 parser 的版本一致；
- 至少有 accuracy、pass@k、长度、延迟四类指标；
- 失败请求有可检索的 `request_id` 与错误码。

## 本章小结

- 训练指标与验证指标必须分开看；reward 涨不等于模型变好。
- pass@k 用无偏估计，验证采样数要 >= k。
- 指标聚合要按样本/token 加权，不能平均 micro-batch。
- 日志要记录配置、数据、模型、reward 版本与生成样例。
- 真实 verl 的评测是两步：`main_generation_server.py` 批量生成 parquet，`main_eval.py` 离线打分；后者不加载 checkpoint。
- MiniVerl 的 `main_eval` 把生成与打分合成一个入口，从 checkpoint 读配置、强制 naive reward、按题分组算 pass@k；不要复用训练 shaping。
- 批量生成脚本不是常驻服务；要做在线服务，需自己补协议、tokenization 所有权、限流、版本戳与健康检查。
- MiniVerl 用 jsonl + console 复现核心指标口径，并提供 `main_eval` 独立评测入口。

## 延伸阅读

- `verl/trainer/ppo/metric_utils.py`
- `verl/utils/tracking.py`
- 附录 L：评测与基准（pass@k、污染控制、统计显著性、报告模板）
- 附录 Z：TTFT/ITL、KV 使用率与 preemption 等 rollout 服务指标
- 下一章：GRPO/DAPO/GSPO/Dr.GRPO/CISPO 的实现差异。
