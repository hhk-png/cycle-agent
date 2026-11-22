# 第 6 章 安装、硬件与第一次 PPO/GRPO

## 6.1 两条路线：真实 verl 与 MiniVerl

本教程有两套可运行代码：

| 路线 | 依赖 | 用途 |
|---|---|---|
| 真实 verl | Linux + NVIDIA/AMD/Ascend + CUDA 13.0 驱动 + PyTorch/vLLM/SGLang | 真正训练 7B～671B 模型 |
| MiniVerl | Python 3.10+ + NumPy + PyYAML + pytest | 在 CPU 上理解并验证每个系统组件 |

如果你想先感受完整流程，先跑 MiniVerl：

```bash
cd verl-tutorial
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run
python -X utf8 -m mini_verl.trainer.main_ppo trainer.total_training_steps=30
python -m pytest tests/ -q
python -X utf8 examples/run_all.py
```

MiniVerl 的默认任务是四动作 contextual bandit：策略在 30～60 步内可以从 25% 学到 100% 验证准确率。它不是语言模型能力的展示，而是把 PPO/GRPO 的数据流、dispatch、权重同步、checkpoint、异步与奖励链路跑通。

真实 verl 需要 GPU 环境。下面以 v0.9.1 的官方安装方式为准。

## 6.2 硬件与软件要求

官方 v0.9.1 安装文档的基线：

- Python >= 3.10, <3.13（uv 工作流推荐 3.12）；
- CUDA 13.0 驱动（v0.9.1 的 torch 2.11 / vLLM 0.24 都是 cu130 轮子；CUDA 12.8 只适用于旧版或自定义 cu128 构建）；
- PyTorch 2.11.0（uv lock 固定）；
- vLLM 0.24.0（cu130 轮子）；
- Megatron-LM core_v0.13.1（文档）或 core_v0.18.0（uv lock 中的 megatron-core）；
- transformers 5.x（项目级 pin 5.9.0）；
- Ray >= 2.41.0；
- tensordict 0.8～0.10（排除 0.9.0）；
- TransferQueue（v0.9.1 核心路径默认安装）。

显存粗算：

| 模型 | 训练最低（FSDP + 全参） | 推荐 |
|---|---|---|
| 0.5B～1.5B | 24 GB | 1×A10/L4/4090 |
| 7B～8B | 8×40 GB | 8×A100/H100 |
| 32B | 16×80 GB | 16～32×H100 |
| 70B | 32×80 GB + TP/PP | 多机 H100 |
| 235B MoE | 64～128×H100 | Megatron EP + 多机 |
| 671B MoE | 128～512×H100 | Megatron + EP/PP + 高性能网络 |

这些是量级，不是精确值；实际取决于序列长度、rollout.n、micro batch、是否 offload、是否 LoRA、是否用 FSDP2/Megatron。第 32 章给出估算公式与调参方法。

## 6.3 用 uv 安装（v0.9.1 推荐）

verl 的 Python 环境由 `uv.lock` 驱动。没有单独的 `pip install` 步骤，直接 `uv run`：

```bash
git clone https://github.com/verl-project/verl.git
cd verl

# FSDP + vLLM
uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 -m verl.trainer.main_ppo --help

# Megatron + SGLang
uv run --frozen --all-packages --extra sglang --extra megatron \
  python3 -m verl.trainer.main_ppo --help
```

要点：

- `--frozen` 使用提交的 lock，不重新解析；
- `--all-packages` 安装 workspace 中的所有包；
- `--extra vllm|sglang` 选择推理后端；
- `--extra fsdp|megatron` 选择训练后端；
- 同一个 job 内所有命令的 extras 必须一致，否则 `uv run` 会重装 torch。

Ray 会通过 `ray_kwargs.ray_init.runtime_env.py_executable` 让 worker 使用同一个环境。`examples/` 下的脚本已经写好这层 wiring，直接跑即可：

```bash
bash examples/grpo_trainer/run_qwen3_8b_fsdp.sh
INFER_BACKEND=sglang bash examples/grpo_trainer/run_qwen3_8b_fsdp.sh
```

## 6.4 Docker

官方提供多套镜像（CUDA、ROCm、NPU）。典型流程：

```bash
docker run --gpus all --shm-size=64g -it \
  -v $HOME/data:/data \
  -v $HOME/models:/models \
  verlai/verl:<tag> bash
```

在容器内：

```bash
cd /workspace/verl
python3 -m verl.trainer.main_ppo --help
```

注意：

- `--shm-size` 要足够大（Ray + 多进程数据加载）；
- 模型和数据挂载到容器内；
- 多机训练需要正确的 NCCL/IB 配置；
- Ascend NPU 使用专用镜像与 `device=npu`。

## 6.5 传统 pip/setup.py 方式

如果不用 uv，仍可用 `setup.py` 的 extras：

```bash
git clone https://github.com/verl-project/verl.git
cd verl
pip install -e .[vllm,fsdp]       # 具体 extra 名称以 setup.py 为准
```

v0.9.1 的 `setup.py` 保留 `vllm`、`sglang`、`trl`、`mcore`、`trtllm`、`math`、`test` 等 extras；但官方推荐 uv，因为 lock 已经把 torch/CUDA/vLLM 的 ABI 对齐好了。自己 pip 安装时最常见的坑是：

- torch 与 vLLM 的 CUDA 版本不匹配；
- flash-attn 编译失败；
- megatron-core 与 megatron-bridge 版本错位；
- tensordict 0.9.0 的 API 变化；
- Python 3.13 不在支持范围内。

## 6.6 数据准备

官方 GSM8K 快速开始：

```bash
uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 examples/data_preprocess/gsm8k.py --local_save_dir ~/data/gsm8k
```

生成的 parquet 每行至少包含：

```text
prompt            # 字符串或 chat messages
data_source       # 奖励函数选择 key
reward_model      # {"ground_truth": "...", "style": "rule"}
extra_info        # 其他元数据
```

v0.9.1 的 `verl/utils/dataset/rl_dataset.py` 会读取这些字段，但 `__getitem__` **只返回 `raw_prompt`**：chat template 与 tokenization 已移到 AgentLoop，collate 只负责 padding/stack，rollout 之后才构造 `response_mask`。数据格式的细节与调用链变化在第 8 章。

## 6.7 第一次 PPO：官方 0.5B 示例

```bash
export DATA=$HOME/data/gsm8k
export MODEL=Qwen/Qwen2.5-0.5B-Instruct

uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 -m verl.trainer.main_ppo \
  data.train_files=$DATA/train.parquet \
  data.val_files=$DATA/test.parquet \
  data.train_batch_size=256 \
  data.max_prompt_length=512 \
  data.max_response_length=512 \
  actor_rollout_ref.model.path=$MODEL \
  actor_rollout_ref.actor.optim.lr=1e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size=64 \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=4 \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=8 \
  actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.4 \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=4 \
  critic.optim.lr=1e-5 \
  critic.model.path=$MODEL \
  critic.ppo_micro_batch_size_per_gpu=4 \
  algorithm.use_kl_in_reward=true \
  algorithm.kl_ctrl.kl_coef=0.001 \
  trainer.logger=console \
  trainer.val_before_train=False \
  trainer.n_gpus_per_node=1 \
  trainer.nnodes=1 \
  trainer.save_freq=10 \
  trainer.test_freq=10 \
  trainer.total_epochs=15
```

这是 PPO + critic + GAE + in-reward KL 的经典组合。日志里应看到：

```text
timing/gen, timing/ref, timing/values, timing/adv,
timing/update_critic, critic/vf_loss, critic/vf_clipfrac,
timing/update_actor, actor/pg_loss, actor/pg_clipfrac,
actor/ppo_kl, response_length/mean, ...
```

如果显存不足：

```bash
actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1
critic.ppo_micro_batch_size_per_gpu=1
actor_rollout_ref.rollout.gpu_memory_utilization=0.3
```

## 6.8 第一次 GRPO：去掉 critic

GRPO 不需要 critic，但需要 `rollout.n > 1`。最小改动：

```bash
uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 -m verl.trainer.main_ppo \
  data.train_files=$DATA/train.parquet \
  data.val_files=$DATA/test.parquet \
  actor_rollout_ref.model.path=$MODEL \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.n=8 \
  actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
  actor_rollout_ref.actor.ppo_mini_batch_size=64 \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=4 \
  actor_rollout_ref.actor.use_kl_loss=true \
  actor_rollout_ref.actor.kl_loss_coef=0.001 \
  algorithm.adv_estimator=grpo \
  algorithm.use_kl_in_reward=false \
  trainer.logger=console \
  trainer.n_gpus_per_node=8 \
  trainer.nnodes=1
```

注意：

- `algorithm.adv_estimator=grpo`；
- `actor.use_kl_loss=true` 时才会构建 ref 并计算 KL loss；
- 不再需要 `critic.*`；
- `rollout.n` 增大时生成 token 数线性增长，先确认显存与吞吐。

更完整的 GRPO 脚本在 `examples/grpo_trainer/`，v0.9.1 的 recipe 已迁移到 `verl-recipe`，`recipe/` 作为 submodule 挂载。运行 recipe 前先：

```bash
git submodule update --init --recursive recipe
```

## 6.9 常见失败模式

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `rollout.name` 缺失 | v0.9.1 要求显式指定 | 设 `vllm` 或 `sglang` |
| CUDA OOM at generation | `gpu_memory_utilization` 太高 | 降到 0.4/0.3，开 `free_cache_engine` |
| CUDA OOM at update | micro batch 太大 | 降 `ppo_micro_batch_size_per_gpu` |
| NCCL timeout | 多机网络/端口/防火墙 | 检查 `nccl_timeout`、IB、`MASTER_ADDR` |
| vLLM 版本不兼容 | torch/vLLM CUDA ABI 不匹配 | 用 uv lock 或官方镜像 |
| reward 全 0 | 模板/答案抽取不匹配 | 打印样本与 reward 函数输出 |
| 训练不收敛 | KL 太强/太弱、lr、clip、数据 | 先看 `actor/ppo_kl`、`pg_clipfrac`、`response_length` |
| 权重同步后输出不变 | 只同步了部分 tensor | 对比 rollout 与 actor 的 logits |

## 6.10 MiniVerl 的第一次运行

MiniVerl 不需要 GPU：

```bash
cd verl-tutorial

# 看配置
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run

# 30 步 GRPO
python -X utf8 -m mini_verl.trainer.main_ppo \
  trainer.total_training_steps=30 \
  trainer.val_before_train=true

# 换成 PPO+GAE
python -X utf8 -m mini_verl.trainer.main_ppo \
  algorithm.adv_estimator=gae \
  actor_rollout_ref.rollout.n=2 \
  trainer.total_training_steps=10
```

MiniVerl 的配置字段和 verl 对齐，但不是全部字段都生效；第 7 章与附录 A 会标注差异。

## 6.11 环境可复现：把“我这能跑”变成“谁都能跑”

RL 的环境比 SFT 更容易漂移：训练侧要 torch/FSDP/Megatron，推理侧要 vLLM/SGLang，二者还共享 CUDA 驱动与 NCCL。只记录“我装了 vLLM”不够。

一个可复现的环境记录至少包含：

```text
git commit + dirty 状态
uv.lock 的 sha256（或镜像 digest）
Python / PyTorch / CUDA / 驱动版本
vLLM / SGLang / transformers / tensordict 版本
GPU 型号、数量、互联（NVLink/IB/RoCE）
NCCL 版本与关键环境变量
数据文件路径 + sha256
```

启动脚本里可以直接落一份：

```bash
{
  git rev-parse HEAD
  git status --porcelain
  sha256sum uv.lock
  python -c "import sys, torch; print(sys.version); print(torch.__version__, torch.version.cuda)"
  python -c "import vllm; print('vllm', vllm.__version__)" 2>/dev/null || true
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
} > env-manifest.txt
```

升级依赖时，**一次只升一个**，并重跑下面的 smoke test。torch 与 vLLM 的 CUDA ABI 不匹配，往往不是“import 失败”，而是运行到一半 CUDA illegal memory access。

## 6.12 Smoke test 阶梯：五分钟、半小时、两小时

不要第一次就跑完整训练。按阶梯验证，失败时才能定位到层：

| 层级 | 命令/动作 | 通过的标志 | 典型失败 |
|---|---|---|---|
| L0 导入 | `python -c "import torch, verl"` | 无 ImportError | ABI/依赖冲突 |
| L1 单卡前向 | 加载 0.5B，forward 一个 batch | loss 有限 | dtype/device/attention |
| L2 推理引擎 | 起 vLLM，生成 8 条 | 输出非空、无 OOM | 显存/端口/CUDA graph |
| L3 单步训练 | 8 prompts、n=2、1 step | 指标齐全、无 NaN | mask/advantage/同步 |
| L4 小规模 | 50～100 step、0.5B | reward 有变化、验证可跑 | 数据/奖励/学习率 |
| L5 目标规模 | 目标模型/卡数 | 吞吐与显存符合预期 | 并行/网络/长尾 |

L3 是最有价值的一步：它同时经过 rollout、reward、old/ref log-prob、advantage、actor update、weight sync。任何一层契约错了，都会在这里暴露为 NaN、全 0 优势或权重不更新。

一个最小的 L3 配置思路：

```bash
# 仅示意：用最小数据、最小模型、单卡、同步模式
data.train_batch_size=8
actor_rollout_ref.rollout.n=2
actor_rollout_ref.actor.ppo_mini_batch_size=16
trainer.total_training_steps=1
trainer.val_before_train=false
```

如果 L3 通过但 L4 不收敛，问题通常在数据/奖励/超参，而不是安装；如果 L3 失败，先查 mask、log-prob 对齐与权重同步，不要急着调学习率。

## 本章小结

- 真实 verl 推荐 uv + extras；v0.9.1 的 lock 固定了 torch/vLLM/CUDA 的兼容组合。
- 最小 PPO 示例需要数据、模型、rollout 后端、actor/critic/ref 配置与 Ray 资源。
- GRPO 去掉 critic，但需要 `rollout.n>1` 和可分的组内奖励。
- 显存问题优先降 micro batch 与 `gpu_memory_utilization`；版本问题优先用官方镜像/uv lock。
- MiniVerl 用同一套字段名在 CPU 上复现数据流，适合先建立直觉。

## 延伸阅读

- 安装文档：<https://verl.readthedocs.io/en/latest/start/install.html>
- 快速开始：<https://verl.readthedocs.io/en/latest/start/quickstart.html>
- 配置说明：<https://verl.readthedocs.io/en/latest/examples/config.html>
- 下一章：Hydra 配置系统与字段地图。
