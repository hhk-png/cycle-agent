# 第 23 章 Checkpoint、断点续训、模型合并与 HF 导出

## 23.1 RL checkpoint 里有什么

一个完整的训练 checkpoint 至少包含：

- actor 模型参数；
- actor 优化器状态（AdamW m/v）；
- LR scheduler 状态；
- critic 模型/优化器（如果启用）；
- global step、epoch；
- 配置（YAML）；
- tokenizer/processor；
- 随机数状态（可选）；
- TransferQueue/数据队列状态（V1 异步）。

verl 的 checkpoint 是**分片**的：

- FSDP：按 FSDP world size 分片；
- Megatron：按 TP/PP/EP 分片；
- 优化器状态可能进一步分片。

默认分片 checkpoint 不能直接给 HF/vLLM 用，需要 merge；但 v0.9.1 允许在保存时直接写 HF 格式：

```yaml
actor_rollout_ref:
  actor:
    checkpoint:
      save_contents: [model, optimizer, extra, hf_model]
```

`hf_model` 会额外写一份完整的 `huggingface/` 目录（FSDP 路径支持；Megatron 路径需要 mbridge/合并）。只想要推理权重时可以用 `save_contents: [hf_model]`，跳过优化器状态。LoRA 还有 `save_lora_only` 之类的选项。因此“必须 merge”只对默认的 `[model, optimizer, extra]` 成立。

## 23.2 目录结构

典型：

```text
checkpoints/${project_name}/${experiment_name}/
├── global_step_10/
│   ├── actor/
│   │   ├── model_world_size_8_rank_0.pt
│   │   ├── ...
│   │   └── optim_world_size_8_rank_0.pt
│   └── critic/
└── latest_checkpointed_iteration.txt
```

具体文件名随后端与版本变化；以 `verl/utils/checkpoint/checkpoint_manager.py` 为准。

MiniVerl 的结构更简单：

```text
checkpoints/.../global_step_10/
├── actor.npz
├── critic.npz
├── actor_optimizer.npz
├── config.yaml
├── tokenizer.json
└── trainer_state.json
```

## 23.3 保存频率与保留策略

```yaml
trainer:
  save_freq: 10
  max_actor_ckpt_to_keep: 3
  max_critic_ckpt_to_keep: 3
  resume_mode: auto
  resume_from_path: null
```

- `save_freq=-1`：不保存；
- `max_*_ckpt_to_keep`：只保留最近 N 个；
- `resume_mode=auto`：自动找最新；
- `resume_mode=resume_path`：配合 `resume_from_path` 指定 `.../global_step_N`。真实 verl 只在 `resume_mode=resume_path` 时读取 `resume_from_path`；只设路径不设模式不会生效。MiniVerl 为了调试方便，只要设置了 `resume_from_path` 就加载，并以该目录的父目录为 checkpoint root。

保存本身可能很慢（几十 GB 写盘）。生产环境要：

- 异步保存（`checkpoint.async_save`，v0.9.1 仅 Megatron 路径实现）；
- 共享存储（NFS/HDFS/S3）；
- 保存时暂停数据生产或记录版本；
- 校验文件完整性。

## 23.4 断点续训

恢复流程：

1. 读取 `latest_checkpointed_iteration.txt` 或扫描目录；
2. 加载 actor/critic 模型与优化器；
3. 恢复 global step/epoch；
4. 重建 LR scheduler；
5. 重新初始化 rollout 并同步权重；
6. 恢复数据队列（V1 TransferQueue 支持 snapshot）。

常见问题：

- world size 变了：FSDP 分片数不匹配，需要 reshard；
- 优化器状态缺失：loss 会跳变；
- tokenizer/config 不一致：生成分布变化；
- rollout 未同步：前几步用旧权重；
- 数据队列未恢复：重复/漏样本。

MiniVerl 的 `_maybe_resume` 加载 actor/critic、优化器状态并 `_sync_weights`；`test_checkpoint_resume` 验证权重一致，`test_checkpoint_restores_optimizer_state` 验证 step count 与 AdamW 矩一致。

## 23.5 模型合并

FSDP/Megatron 的分片 checkpoint 要合并成 HF 格式：

```bash
python3 -m verl.model_merger merge \
  --backend fsdp \
  --local_dir checkpoints/${project}/.../global_step_1/actor \
  --target_dir checkpoints/${project}/.../global_step_1/actor/huggingface
```

Megatron：

```bash
python3 -m verl.model_merger merge \
  --backend megatron \
  --local_dir ... \
  --target_dir ... \
  --use_cpu_initialization
```

具体参数以 `verl/model_merger/` 与 `--help` 为准。合并后应得到：

- `config.json`
- `model.safetensors`（或分片）
- tokenizer 文件
- generation config

`verl/model_merger/output_validation.py` 会检查输出是否完整。

## 23.6 为什么需要 merge

训练布局与推理布局不同：

- FSDP 把参数展平/分片；
- Megatron 把 QKV 融合、按 TP/PP 切分；
- vLLM/SGLang 需要 HF 名称与完整权重；
- HF 的 `config.json` 描述层数、hidden size、attention heads。

merge 做三件事：

1. gather 分片；
2. 重命名/反融合；
3. 写 safetensors + config。

MiniVerl 的 `checkpoint.export_hf_like` 演示了同样的形状：`model.safetensors` + `manifest.json` + 配置。它写的是真实的 safetensors 容器（8 字节 header + JSON + 原始字节），但没有生产模型的完整 config。

## 23.7 safetensors 格式

safetensors 的结构：

```text
[8 bytes little-endian header length][JSON header padded to 8 bytes][raw tensor bytes]
```

header 里每个 tensor：

```json
{"name": {"dtype": "F32", "shape": [2,3], "data_offsets": [0,24]}}
```

MiniVerl 的 `write_safetensors/read_safetensors` 不依赖外部库，测试 `test_safetensors_roundtrip` 验证往返。

## 23.8 异步保存与一致性

训练在保存时仍在更新权重，可能写出“半新半旧”的 checkpoint。解决方案：

- 保存前暂停 optimizer；
- 用 snapshot/COW；
- 异步保存时先拷贝到 CPU；
- 记录保存的 global step；
- 校验文件 hash。

verl 的 `checkpoint.async_save` 与 checkpoint callback 提供这些能力。

## 23.9 从 checkpoint 继续 vs 从 HF 模型继续

两种恢复不同：

- **resume**：恢复优化器、scheduler、step，继续原训练；
- **fine-tune**：只加载模型权重，优化器重置，step 从 0。

RL 中常见错误是把 HF 模型当 resume，导致：

- 优化器状态丢失；
- LR scheduler 重置；
- 数据队列重复；
- 训练曲线跳变。

## 23.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| merge 后模型输出乱码 | 权重映射/反融合错 | 对比 logits、用官方 merger |
| resume 后 loss 跳变 | 优化器状态缺失 | 保存/加载完整状态 |
| checkpoint 很大 | 优化器状态 + 多副本 | 只保留必要项、异步清理 |
| 保存时 OOM | gather 到 CPU/GPU | 异步保存、分片写 |
| world size 变化无法加载 | 分片数不匹配 | reshard 或固定 world size |
| rollout 仍旧权重 | 恢复后未同步 | 调 `update_weights` |

## 23.11 MiniVerl 对照

- `mini_verl/checkpoint.py::CheckpointManager`
- `mini_verl/checkpoint.py::write_safetensors/read_safetensors`
- `mini_verl/checkpoint.py::export_hf_like`
- `mini_verl/engine.py::save_checkpoint/load_checkpoint`
- `mini_verl/trainer/ppo_trainer.py::save_checkpoint/_maybe_resume`

`examples/07_checkpoint_resume.py` 演示训练 5 步、保存、恢复、比较权重。

## 23.12 优化器状态、LR scheduler 与完整恢复

只保存模型权重不叫“断点续训”，那只是“从权重热启动”。一个真正等价的 resume 至少还要恢复：

- AdamW 的一阶/二阶矩；
- optimizer 的 step count；
- LR scheduler 的步数（如果与 optimizer step 绑定）；
- AMP 的 loss scale（如果使用）；
- 数据游标/epoch/已消费样本数；
- 随机数状态（Python/NumPy/框架 RNG）；
- 异步队列与在飞请求（如果使用 TransferQueue/partial rollout）。

### 为什么优化器状态重要

AdamW 的更新是：

$$
m_t=\beta_1m_{t-1}+(1-\beta_1)g_t,\qquad
v_t=\beta_2v_{t-1}+(1-\beta_2)g_t^2,
$$

$$
\theta_t=\theta_{t-1}-\eta\frac{\hat m_t}{\sqrt{\hat v_t}+\epsilon}.
$$

如果 resume 时 $m,v$ 归零，前几步的 $\hat m/\sqrt{\hat v}$ 会剧烈跳变，loss 出现一个尖峰；如果 step count 也归零，bias correction 会从 $1-\beta^1$ 重新开始，等效学习率与断点前完全不同。这就是“权重一致但 loss 跳变”的典型原因。

### 保存格式

不同后端的优化器状态布局不同：

- FSDP：优化器状态可能按 flat parameter 分片，需与模型分片对齐；
- Megatron：按 TP/PP/EP 分片，distributed optimizer 还会把状态切到 DP；
- 单机：可以直接按参数名保存。

工程上建议：

1. 模型与优化器使用**同一套分片元数据**（world size、mesh、param name → shard）；
2. 保存时记录 `global_step`、`consumed_samples`、`epoch`；
3. 读取时先校验 world size/并行度，再决定直接加载还是 reshard；
4. 对不兼容的 checkpoint 明确报错，不要静默跳过优化器。

### MiniVerl 的实现

`CheckpointManager.save` 现在接收 `actor_optimizer` / `critic_optimizer`，写为 `actor_optimizer.npz` / `critic_optimizer.npz`：

```text
global_step_10/
├── actor.npz
├── critic.npz
├── actor_optimizer.npz      # __step_count__ + name::m / name::v
├── critic_optimizer.npz
├── config.yaml
├── tokenizer.json
└── trainer_state.json
```

`PPOTrainer.save_checkpoint` 从 worker group 取 `get_optimizer_state_dict()`；`_maybe_resume` 通过 `load_optimizer_state_dict()` 恢复。`trainer_state.json` 还记录 `train_cursor`、`epoch` 与当前 KL 系数，resume 后不会从数据集开头重放。`tests/test_trainer.py::test_checkpoint_restores_optimizer_state` 比较恢复前后的 `step_count` 与所有矩张量，`test_resume_from_path_uses_requested_run` 验证跨 run 的 `resume_from_path` 与游标恢复。

### 还没恢复的东西

MiniVerl 保存了数据游标 `train_cursor` 与 `epoch`，但还没有保存 RNG 状态与 DataLoader worker 状态：resume 后随机数会重新播种，多 worker 的预取顺序也不保证一致。对教学 bandit 没问题，但对长训练会改变采样顺序。生产实现应把：

- `np.random.Generator.bit_generator.state`；
- DataLoader 的 epoch/worker 状态；
- TransferQueue 的 snapshot；
- partial rollout 的请求状态；

一起写入 checkpoint，或者在文档中明确“resume 不保证逐样本等价”。

### 异步保存的一致性

异步保存的难点是“保存过程中参数还在变”。可选方案：

- 保存前暂停 optimizer step（简单，但增加空泡）；
- 在 step 边界拷贝一份 CPU snapshot，再异步写盘（推荐）；
- 使用 COW/版本化存储；
- 保存后校验文件大小/hash，并在 `trainer_state.json` 记录完成时间。

MiniVerl 是同步保存，写入的是 step 边界上的副本，因此不存在半新半旧；但它也没有覆盖“保存耗时”对训练节奏的影响。

## 本章小结

- RL checkpoint 包含模型、优化器、scheduler、step、配置、tokenizer。
- 分片 checkpoint 需要 model merger 合并成 HF 格式。
- resume 与 fine-tune 的语义不同，不能混用。
- 异步保存要保证一致性。
- MiniVerl 用 npz + safetensors 演示完整生命周期。

## 延伸阅读

- `verl/model_merger/`
- `verl/utils/checkpoint/checkpoint_manager.py`
- 下一章：验证、指标、日志与实验管理。
