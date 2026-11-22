# 附录 A 配置与 API 速查

## A.1 入口

```bash
# 真实 verl
python3 -m verl.trainer.main_ppo <overrides>

# MiniVerl
python -X utf8 -m mini_verl.trainer.main_ppo <overrides>
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run
```

## A.2 最常用的覆盖

```bash
data.train_files=/data/train.parquet
data.val_files=/data/test.parquet
data.train_batch_size=256
data.max_prompt_length=512
data.max_response_length=512

actor_rollout_ref.model.path=Qwen/Qwen2.5-0.5B-Instruct
actor_rollout_ref.actor.optim.lr=1e-6
actor_rollout_ref.actor.ppo_mini_batch_size=64
actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=4
actor_rollout_ref.actor.use_kl_loss=true
actor_rollout_ref.actor.kl_loss_coef=0.001
actor_rollout_ref.actor.loss_agg_mode=token-mean

actor_rollout_ref.rollout.name=vllm
actor_rollout_ref.rollout.mode=async
actor_rollout_ref.rollout.n=8
actor_rollout_ref.rollout.temperature=1.0
actor_rollout_ref.rollout.tensor_model_parallel_size=2
actor_rollout_ref.rollout.gpu_memory_utilization=0.5
actor_rollout_ref.rollout.free_cache_engine=true

critic.model.path=$MODEL
critic.optim.lr=1e-5
critic.cliprange_value=0.5

actor_rollout_ref.actor.policy_loss.loss_mode=vanilla
actor_rollout_ref.rollout.checkpoint_engine.backend=naive

algorithm.adv_estimator=grpo
algorithm.use_kl_in_reward=false
algorithm.filter_groups.enable=true

trainer.n_gpus_per_node=8
trainer.nnodes=1
trainer.logger='["console","wandb"]'
trainer.save_freq=10
trainer.test_freq=10
trainer.val_before_train=false
```

## A.3 关键字段对照（真实 verl ↔ MiniVerl）

| 真实 verl | MiniVerl | 备注 |
|---|---|---|
| `data.train_files` | `data.dataset` | MiniVerl 用合成数据 |
| `data.train_batch_size` | 同名 | prompt 数 |
| `data.max_prompt_length` | 同名 | |
| `data.max_response_length` | 同名 | |
| `actor_rollout_ref.rollout.n` | 同名 | 组采样 |
| `actor_rollout_ref.rollout.name` | 同名（naive） | |
| `actor_rollout_ref.rollout.tensor_model_parallel_size` | 同名 | 模拟 |
| `actor_rollout_ref.actor.ppo_mini_batch_size` | 同名 | 全局序列数 |
| `actor_rollout_ref.actor.ppo_micro_batch_size` | 同名 | MiniVerl 只有这一个；真实 verl 的全局 `ppo_micro_batch_size` 已标记 deprecated，优先用 `..._per_gpu` |
| `actor_rollout_ref.actor.use_kl_loss` | 同名 | |
| `actor_rollout_ref.actor.loss_agg_mode` | 同名 | |
| `critic.ppo_mini_batch_size` | 同名 | |
| `critic.cliprange_value` | 同名 | |
| `algorithm.adv_estimator` | 同名 | |
| `algorithm.filter_groups` | 同名 | |
| `algorithm.rollout_correction` | 同名 | |
| `trainer.v1.trainer_mode` | `trainer.trainer_mode` | sync/colocate_async/separate_async；MiniVerl 键名少一层 |
| `trainer.use_v1` | `trainer.use_v1` | MiniVerl 有该字段但未生效（默认 V1 语义） |
| `reward.reward_fn` | 无 | 旧版/部分 recipe 字段；v0.9.1 常用 `reward.custom_reward_function.path/name` |
| `reward.reward_manager` | 同名 | |
| `actor_rollout_ref.model.lora.rank` | `model.lora_rank` | 0 表示关闭 |
| `actor_rollout_ref.model.lora.alpha` | `model.lora_alpha` | 缩放 `alpha/rank` |
| `actor_rollout_ref.model.lora.target_modules` | `model.lora_targets` | 按层名注入 |

## A.4 MiniVerl CLI 示例

```bash
# GRPO bandit
python -X utf8 -m mini_verl.trainer.main_ppo \
  algorithm.adv_estimator=grpo \
  actor_rollout_ref.rollout.n=8 \
  trainer.total_training_steps=40

# PPO + GAE
python -X utf8 -m mini_verl.trainer.main_ppo \
  algorithm.adv_estimator=gae \
  actor_rollout_ref.rollout.n=2 \
  actor_rollout_ref.actor.ppo_mini_batch_size=16 \
  data.train_batch_size=8 \
  trainer.total_training_steps=20

# DAPO 过滤（MiniVerl 也接受 adv_estimator=dapo 作为兼容别名；
# 真实 verl 没有 adv_estimator=dapo，推荐用 policy_loss.loss_mode）
python -X utf8 -m mini_verl.trainer.main_ppo \
  algorithm.adv_estimator=grpo \
  actor_rollout_ref.actor.policy_loss.loss_mode=dapo \
  algorithm.filter_groups.enable=true \
  reward.reward_manager=dapo \
  trainer.total_training_steps=20

# SFT / DPO
python -X utf8 -m mini_verl.trainer.main_sft trainer.total_training_steps=10
python -X utf8 -m mini_verl.trainer.main_dpo trainer.total_training_steps=10

# CISPO（裁剪重要性权重，梯度走 score function）
python -X utf8 -m mini_verl.trainer.main_ppo \
  actor_rollout_ref.actor.policy_loss.loss_mode=cispo \
  trainer.total_training_steps=40

# 独立评测：从 checkpoint 读配置，按题分组算 pass@k
python -X utf8 -m mini_verl.trainer.main_eval \
  --checkpoint outputs/checkpoints/demo/grpo/global_step_60 \
  --n 8 --k 1,4,8 --json

# LoRA（冻结基座，只训练 adapter）
python -X utf8 examples/17_lora.py
```

## A.5 调试命令

```bash
# 看最终配置
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run

# 单测
python -m pytest tests/ -q
python -m pytest tests/test_trainer.py -q

# 示例
python -X utf8 examples/01_quickstart.py
python -X utf8 examples/run_all.py

# 文档自检
python -X utf8 check_docs.py
```

## A.6 代码入口速查

| 功能 | MiniVerl | 真实 verl |
|---|---|---|
| DataProto | `mini_verl/protocol.py` | `verl/protocol.py` |
| Dispatch | `mini_verl/single_controller/decorator.py` | `verl/single_controller/base/decorator.py` |
| WorkerGroup | `mini_verl/single_controller/worker_group.py` | `verl/single_controller/base/worker_group.py` |
| Ray 后端 | — | `verl/single_controller/ray/base.py` |
| Engine | `mini_verl/engine.py` | `verl/workers/engine/base.py` |
| Actor/Critic | `mini_verl/workers.py` | `verl/workers/engine_workers.py` |
| Rollout | `mini_verl/rollout.py` | `verl/workers/rollout/` |
| 算法 | `mini_verl/algo/core_algos.py` | `verl/trainer/ppo/core_algos.py` |
| PPO 主循环 | `mini_verl/trainer/ppo_trainer.py` | `verl/trainer/ppo/v1/trainer_base.py` |
| 独立评测 | `mini_verl/trainer/main_eval.py` | `verl/trainer/main_eval.py` |
| 批量生成 / 在线服务 | —（边界） | `verl/trainer/main_generation_server.py`（批量生成，不是常驻 API） |
| Checkpoint | `mini_verl/checkpoint.py` | `verl/utils/checkpoint/` |
| Model Merger | `mini_verl/checkpoint.py::export_hf_like` | `verl/model_merger/` |
| Agent | `mini_verl/agent.py` | `verl/experimental/agent_loop/` |
| 异步 | `mini_verl/async_policy.py` | `verl/experimental/fully_async_policy/` |
| LoRA | `mini_verl/lora.py` | `docs/advance/ppo_lora.rst` |

## A.7 Python API 速查

配置之外，二次开发最常用的入口如下。签名以 v0.9.1 源码为准；这里只记“从哪导入、负责什么”。

| 对象 | 导入/文件 | 用途 |
|---|---|---|
| `DataProto` | `verl/protocol.py` | `batch`/`non_tensor_batch`/`meta_info`、chunk/concat/padding |
| `Dispatch` / `register` | `verl/single_controller/base/decorator.py` | 声明 worker 方法的 dispatch 模式 |
| `RayWorkerGroup` / `RayResourcePool` | `verl/single_controller/ray/base.py` | Ray 资源池与 worker group |
| `BaseEngine` | `verl/workers/engine/base.py` | forward/backward、optimizer、checkpoint、权重导出 |
| `ShardSpec` | `verl/workers/engine/spec.py` | 参数分片描述（mesh/placements/HF 映射） |
| `CheckpointEngineManager` | `verl/checkpoint_engine/base.py` | 权重同步后端选择与调用 |
| `AgentLoopBase` / `AgentLoopManager` | `verl/experimental/agent_loop/agent_loop.py` | 多轮/工具轨迹 |
| `AbstractRewardManager`（具体实现如 `NaiveRewardManager`） | `verl/workers/reward_manager/abstract.py` | 标量 → token-level reward |
| `compute_gae_advantage_return` 等 | `verl/trainer/ppo/core_algos.py` | 优势/损失/KL/aggregation |

最小代码骨架：

```python
from verl.protocol import DataProto
from verl.single_controller.base.decorator import Dispatch, register
from verl.workers.engine.base import BaseEngine
from verl.checkpoint_engine.base import CheckpointEngineManager

batch = DataProto.from_single_dict({"input_ids": input_ids})
# worker 方法上声明 dispatch 模式
@register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
def compute(self, data: DataProto) -> DataProto: ...
```

`DataProto.from_single_dict`、`chunk`、`concat` 等具体签名请对照 `verl/protocol.py`；不同版本可能有参数差异。

## A.8 常用环境变量

```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0   # Ray + uv
VERL_LOGGING_LEVEL=INFO
NCCL_DEBUG=INFO                   # NCCL 调试
NCCL_DEBUG_SUBSYS=INIT,COLL       # 只看初始化/集合通信
NCCL_SOCKET_IFNAME=eth0           # 多机网卡选择
NCCL_IB_HCA=mlx5                  # IB 网卡选择
CUDA_LAUNCH_BLOCKING=1            # 单卡调试
PYTHONUNBUFFERED=1                # 日志实时
```

MiniVerl 不需要这些；真实 verl 的多机/uv 场景需要。

## A.9 MiniVerl 接受但未生效的字段

为了避免“改了配置但行为没变”的假象，这里显式列出当前未接线/仅占位的字段：

| 字段 | 现状 |
|---|---|
| `ref.log_prob_micro_batch_size` | 接受但不用于切分 |
| `rollout.val_kwargs` | 接受但验证仍用固定 greedy 参数 |
| `reward.reward_model` | 接受；`enable=true` 时启动进程内 `ModelRewardManager`（无 HTTP server/router） |
| `reward.custom_reward_function` | 接受；请直接用 `reward.reward_fn=module:function` |
| `trainer.total_epochs` | 接受；MiniVerl 主要用 `total_training_steps` |
| `actor/critic.fsdp_config` | 接受；dense replica 模拟不读取 offload/reshard |
| `actor.engine.strategy` | 接受；实际用 `actor.strategy` 选引擎 |
| `trainer.use_v1` | 接受；MiniVerl 只有一套 V1 语义 |

新增字段时，要么接线，要么在这里登记；否则就是“配置能解析、行为没变化”的静默 no-op。

## A.10 MiniVerl 工具 API 速查

```python
from mini_verl.tools import (
    BaseTool, ToolResult, ToolSchema, FunctionTool,
    ToolRegistry, CalculatorTool, ToolCollisionError, function_tool,
)

# 1. 继承 BaseTool：定义每轨迹实例
class Echo(BaseTool):
    name = "echo"
    def execute(self, instance_id, parameters, **kwargs):
        return ToolResult(text=str(parameters["text"]))

# 2. 函数工具：schema 从签名推断
@function_tool
def word_count(text: str) -> str:
    """Count words."""
    return str(len(text.split()))

# 3. 注册与导出 schema
registry = ToolRegistry.default()
registry.register_tool(Echo())
registry.register_tool(word_count.tool)
print(registry.names())        # ['calculator', 'echo', 'word_count']
print(registry.schemas())      # OpenAI function schemas

# 4. 执行：自动 create/release
result = registry.execute("echo", {"text": "hi"})
print(result.text, result.reward, result.metrics)

# 5. legacy 字符串接口
print(registry.call("calculator", "1+2"))   # '3'
```

要点：

- `create` 的 `instance_id` 唯一；重复 id 抛 `ValueError`。
- `FunctionTool(timeout=...)` 是协作式超时，不能杀死后台线程。
- 重名注册抛 `ToolCollisionError`，不会静默覆盖。

## A.11 MiniVerl 路由回放 API 速查

```python
from mini_verl.parallel import (
    moe_route, record_moe_routes, replay_moe_routes, router_replay_report,
)

route = moe_route(router_logits, top_k=2, capacity_factor=1.25)
recorded = record_moe_routes(route)                 # (tokens, top_k)
replayed = moe_route(other_logits, top_k=2,
                     replay_top_idx=recorded)       # 复现记录
mixed = moe_route(other_logits, top_k=2,
                  replay_top_idx=recorded,
                  replay_mask=mask)                 # 行级混合

# 多层：层数不一致直接 ValueError
routes = replay_moe_routes(layer_logits, recorded_layers, top_k=2)
print(router_replay_report(recorded_layers, [r["top_idx"] for r in routes]))
```

要点：

- `replay_top_idx` 形状必须是 `(tokens, top_k)`，专家 id 必须在范围内。
- `replay_mask` 是行级 mask：`True` 重放，`False` 走原生路由。
- 层数不一致意味着 hash router/层序错位，必须失败而不是截断。

## A.12 奖励模型配置速查（真实 verl）

```yaml
reward:
  num_workers: 8

  reward_manager:
    source: register          # register | importlib
    name: naive               # naive / dapo / gdpo / rate_limited / remote

  reward_model:
    enable: true
    enable_resource_pool: false
    n_gpus_per_node: 8
    nnodes: 0
    model_path: /path/to/reward_model
    rollout:
      name: vllm
      dtype: bfloat16
      gpu_memory_utilization: 0.5
      enforce_eager: true
      free_cache_engine: true
      tensor_model_parallel_size: 2
      max_model_len: 4096
      response_length: 4096

  sandbox_fusion:
    url: null
    max_concurrent: 64
    memory_limit_mb: 1024
```

要点：

- `reward_model.enable=true` 是**推理服务开关**，不会训练 RM；RM 要在别处训练后以 HF 权重接入。
- 配置树来自 `verl/trainer/config/reward/reward.yaml`，数据类在 `verl/workers/config/reward.py`。
- `data.return_raw_input_ids=true` 用于 RM 与策略 chat template 不同的场景。
- DAPO 的 overlong shaping 在 V1 里通过 `reward.reward_kwargs.overlong_buffer_cfg` 传入；旧文档的 `reward_model.overlong_buffer` 不是 v0.9.1 的配置键。
- 完整链路（偏好数据、BT loss、校准、RLAIF）见附录 X。

MiniVerl 的训练与接入是分开的两条命令：

```yaml
# configs/rm_bandit.yaml（训练 RM）
reward:
  reward_model:
    enable: true
    arch: mlp                 # linear | mlp | transformer
    pooling: mean             # mean | last
    lr: 0.02
    total_steps: 160
    eval_freq: 20
    margin: 0.0
    label_smoothing: 0.0
    length_normalize: 0.0
    output_dir: outputs/reward_model/bandit
```

```bash
# 训练
python -X utf8 -m mini_verl.trainer.main_rm configs/rm_bandit.yaml
# 把训练好的 RM 接入 PPO（只换奖励来源，不改主循环）
python -X utf8 -m mini_verl.trainer.main_ppo \
  reward.reward_model.enable=true \
  reward.reward_model.model_path=outputs/reward_model/bandit \
  data.max_prompt_length=16 data.max_response_length=8
```

MiniVerl 的 `enable` 同样只表示“服务这个 RM”，不表示“训练它”；实现见 `mini_verl/reward_model.py`，契约见第 17.16 节与 X.11。

## A.13 过程奖励模型（PRM）配置速查（MiniVerl 扩展）

真实 verl 没有 `reward.prm` 这个内置键；PRM 要通过自定义 reward function 或 reward server 接入 reward loop（附录 K）。MiniVerl 为了把“训练 → 服务 → PPO”跑通，提供了 `reward.prm`。

```yaml
reward:
  prm:
    enable: true
    model_path: outputs/example_prm/bandit_prm   # serving 时必填
    aggregation: min          # last | first | min | max | mean | product
    step_tokens: 1            # 没有显式 step_boundaries 时的均匀切分
    normalize: none           # none | zscore | minmax
    outcome_reward_fn: bandit # exact_match | bandit | math；与 verifier 组合
    outcome_weight: 1.0
    prm_weight: 0.1
```

训练配置（`configs/prm_copy.yaml`；多步 copy 任务）：

```yaml
reward:
  prm:
    enable: true
    arch: mlp                 # linear | mlp | transformer
    pooling: mean             # mean | last
    step_tokens: 1
    label_mode: suffix        # local | suffix
    corruptions_per_sample: 8
    aggregation: min
    lr: 0.02
    train_batch_size: 32
    total_steps: 100
    eval_freq: 20
    target_accuracy: 0.96
    patience: 4
    output_dir: outputs/prm/copy
```

命令：

```bash
# 多步 copy：用于观察逐步分数与聚合
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_copy.yaml
# 单动作 bandit：用于 PPO 接入
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_bandit.yaml
# 用 PRM 做 PPO 奖励（建议同时给 verifier，避免 PRM-only reward hacking）
python -X utf8 -m mini_verl.trainer.main_ppo \
  reward.prm.enable=true \
  reward.prm.model_path=outputs/prm/bandit \
  reward.prm.aggregation=min \
  reward.prm.outcome_reward_fn=bandit \
  reward.prm.outcome_weight=1.0 \
  reward.prm.prm_weight=0.1
```

Python API：

```python
from mini_verl.prm import (
    PRMTrainer, build_step_samples, collate_step_samples,
    aggregate_step_probs, ProcessRewardManager, load_prm,
)

manager = ProcessRewardManager(
    "outputs/example_prm/copy_prm",
    aggregation="min",
    step_tokens=1,
)
scored = manager.run(data)      # 写 rm_scores / prm_scores / prm_trajectory_scores / prm_step_scores
```

要点：

- `reward.prm.enable` 是**服务开关**，训练走 `main_prm.py`；不能与 `reward.reward_model.enable` 同时打开。
- 显式 `step_boundaries` 放在 `non_tensor_batch`，是相对 `response_mask=1` 位置计数的 end index（含）；最后一个必须等于最后一个有效 response 位置。observation 空洞不占边界序号，但会出现在前缀里。
- `outcome_reward_fn` 需要 `non_tensor_batch["response_text"]` 与 `["answers"]`；缺字段会报错。只写 `outcome_reward_fn` 而不给 `outcome_weight > 0` 也会报错，不会静默退化成 PRM-only。
- `aggregation`/`step_tokens` 省略时从 checkpoint 元数据读取；显式配置仍可覆盖。`max_seq_len` 也是 checkpoint 契约，超长直接报错。
- `normalize=zscore/minmax` 是 batch-dependent；独立评测/跨 batch 比较用 `normalize=none`。
- 契约与边界见 17.17，方法论文见附录 K。
