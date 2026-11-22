# 附录 H 版本迁移：0.3 → 0.9

## H.1 为什么需要迁移指南

verl 在 2025～2026 年经历了架构级重构：

- V1 trainer 成为默认；
- Engine/EngineWorker 取代旧 worker；
- checkpoint engine 取代 sharding manager；
- TransferQueue 进入核心路径；
- recipe 独立成仓库；
- rollout/config 字段变化。

旧教程/旧脚本里的路径可能已经不存在。

## H.2 文件/类迁移

| 旧版（0.3～0.5） | 新版（0.9） |
|---|---|
| `verl/workers/fsdp_workers.py` | `verl/workers/engine_workers.py` + `verl/workers/engine/fsdp/` |
| `verl/workers/megatron_workers.py` | `verl/workers/engine_workers.py` + `verl/workers/engine/megatron/` |
| `verl/workers/actor/dp_actor.py` | `FSDPEngineWithLMHead` + `TrainingWorker` |
| `verl/workers/actor/megatron_actor.py` | `MegatronEngineWithLMHead` + `TrainingWorker` |
| `verl/workers/critic/dp_critic.py` | `FSDPEngineWithValueHead` + `TrainingWorker` |
| `verl/workers/sharding_manager/` | `verl/checkpoint_engine/` + `BaseEngine.get_per_tensor_param*` |
| `verl/workers/rollout/vllm_rollout_spmd.py` | `verl/workers/rollout/vllm_rollout/vllm_rollout.py` |
| `verl/workers/reward_manager/` | 保留 legacy + `verl/experimental/reward_loop/` |

## H.3 trainer 迁移

旧：

```python
from verl.trainer.ppo.ray_trainer import RayPPOTrainer
```

新：

```python
from verl.trainer.ppo.v1 import PPOTrainer
# 同步/异步子类按 trainer_mode 选择；符号以 v1/ 下的定义文件为准
```

配置：

```yaml
trainer:
  use_v1: true
  v1:
    trainer_mode: sync
```

## H.4 rollout 配置

旧：

```yaml
actor_rollout_ref:
  rollout:
    name: vllm
```

新：

```yaml
actor_rollout_ref:
  rollout:
    name: vllm        # 必填
    mode: async       # 默认 async
```

注意：

- `name` 不再是默认值；
- server 模式需要 health/超时/路由；
- `free_cache_engine` 与 async 的交互变化；
- `logprobs_mode` 新增。

## H.5 算法/损失

- `adv_estimator` 新增：`grpo_vectorized`（部分版本还有 `rloo_vectorized`）等；
- `policy_loss.loss_mode` 注册表（GSPO 等序列级损失在这里，不在 `adv_estimator`）；
- `loss_agg_mode`/`loss_scale_factor`；
- `filter_groups`；
- `rollout_correction`；
- `distillation`。

旧脚本里的 `algorithm.adv_estimator=grpo` 仍然可用，但相关字段可能不同。

## H.6 checkpoint

旧：

- sharding manager 直接管理；
- merge 脚本在 `scripts/`。

新：

- `checkpoint_engine` backend；
- `model_merger` 模块；
- `--backend fsdp|megatron`；
- output validation。

## H.7 recipe

旧：

- `recipe/` 在主仓库。

新：

- `recipe/` 是 submodule → `verl-recipe`；
- 每个 recipe 有 `REQUIRED_VERL.txt`；
- 先 `git submodule update --init --recursive recipe`。

## H.8 数据

旧：

- `RLHFDataset.__getitem__` 返回 tokenized 的 prompt 字段。

新：

- **所有路径**的 `__getitem__` 都只返回 `raw_prompt`；chat template 与 tokenization 移到了 AgentLoop（单轮 `SingleTurnAgentLoop`、多轮/工具 `ToolAgentLoop`、V1 `AgentLoopManagerTQ`）；
- collate 只 padding/stack，不再 tokenize；
- 多模态 processor 也在 AgentLoop 路径处理；
- `DataProto.batch` 可能只有 `dummy_tensor`，真正的 token 在 AgentLoop 输出里；
- dataset 阶段唯一会 tokenize 的是 `maybe_filter_out_long_prompts`（过滤超长 prompt），它不是训练输入。

详见第 8.2 节。

## H.9 迁移检查清单

- [ ] 更新 import 路径；
- [ ] 设 `trainer.use_v1=true`；
- [ ] 设 `rollout.name`/`mode`；
- [ ] 检查 `adv_estimator`/`policy_loss`；
- [ ] 检查 `checkpoint_engine.backend`；
- [ ] 更新 model merger 命令；
- [ ] 更新 recipe 引用；
- [ ] 检查数据字段/多模态；
- [ ] 跑小规模 e2e；
- [ ] 对比旧/新指标。

## H.10 回滚

如果新版本不兼容：

- 固定 verl tag；
- 固定 recipe commit；
- 固定 vLLM/torch 版本；
- 用 docker 镜像；
- 记录升级前的配置/指标。

## H.11 MiniVerl 的版本

MiniVerl 的 `__version__` 是 `0.9.1-mini.1`，表示它对齐 v0.9.1 的架构概念，但不是 verl 的替代实现。它的配置字段是子集，迁移到真实 verl 时需要补全后端/数据/多模态字段。
