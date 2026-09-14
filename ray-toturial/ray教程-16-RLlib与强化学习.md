仓库地址：https://github.com/hhk-png/cycle-agent

# 第 16 章：RLlib 与强化学习

> 本章目标：讲清楚三件事 ——
> **RLlib 现在处于什么状态**（没被弃用，但在 API 迁移中）、
> **2026 年的 RL 生态到底长什么样**（Ray 是底座，算法在别人手里）、
> **以及如果你要做 LLM 后训练，该选谁**。
> 本章对"社区观点"与"可验证事实"做严格区分，前者一律标注。

---

## 16.1 RLlib 的定位与当前状态

RLlib 是 Ray 官方的**强化学习库**，和 Data / Train / Tune / Serve 并列。
它提供一批**算法实现**（PPO、SAC、DQN、APPO、IMPALA…）、一套**分布式执行
模型**（env runner 采样、learner 训练、replay buffer）、一套**策略抽象**
（RLModule，PyTorch 的 nn.Module 子类），以及与 Ray Tune 的超参搜索集成。

一句话：**RLlib 是「跑 RL 实验」的框架，不是「做 LLM 后训练」的框架。**
这个区分在本章后半段会变得非常关键。

⚠️ 网上有两种极端说法，**都不对**：说 "RLlib 已经被砍了/弃用了" —— **错**，
RLlib 仍在主线积极开发，新 API stack 在 **Ray Summit 2025 上宣布 GA**
（Anyscale 官方口径），官方给出的规模是**可扩展到 10,000+ env runners /
100+ learners**；说 "RLlib 什么都没变，老代码直接跑" —— **也错**，新 API stack
已经是**所有算法的默认**，回退旧 stack 必须显式写：

```python
config.api_stack(
    enable_rl_module_and_learner=False,
    enable_env_runner_and_connector_v2=False,
)
```

时间线（来源：`doc/source/rllib/new-api-stack-migration-guide` 与发布记录）：

| 版本 | 状态 |
|---|---|
| 2.10 | 新 API stack 以 alpha 形式可选 |
| 2.38 | **SAC / DQN** 先改为默认开启（#47217） |
| **2.39** | **PPO** 也改为默认开启（#48284）—— ⚠️ 本书早先跳过了这一行 |
| 2.40 | 剩下的 **APPO / IMPALA / BC / MARWIL / CQL** 一并开启（#48516、#48599）；**全局默认值在这一版翻转**，此后即"所有算法默认走新 stack" |
| Ray Summit 2025 | v2 stack 宣布 **GA** |

> 📌 **为什么要拆成三行**：如果你把 Ray 钉在 **2.39**，PPO 走新 stack、
> 而 APPO/IMPALA 那批还在旧 stack —— "2.40 起所有算法都默认新 stack"这句话
> 在 2.39 上**不成立**。排查"为什么我的 PPO 行为和教程不一样"时，
> 这个版本边界就是答案。

**结论：在 2026 年看 RLlib 教程，先确认它是新 stack 还是旧 stack 的。**
旧 stack 写法（如 `config.training(model=...)`）在新 stack 下要么报错、
要么静默无效 —— 这是初学者最常撞的墙。

> mini-ray 只实现 Ray Core，**没有 RLlib**，本章代码跑不了。唯一的交集是
> `local_mode`（见 16.4），但那是完全独立的实现。

## 16.2 新 API stack：类替换关系

这是本章最该背下来的一张表。

| 旧 stack | 新 stack | 变化本质 |
|---|---|---|
| `Policy` / `ModelV2` / `TFModelV2` | **`RLModule`** | 从"框架相关的策略包装"变成"纯 PyTorch nn.Module" |
| `RolloutWorker` | **`EnvRunner`** | 采样与推断合并，支持 async 与多环境 |
| `Policy` + `RolloutWorker` 的部分职责 | **`Learner`** | 训练变成独立 worker，可 `num_learners > 1` |
| `Connector` | **`ConnectorV2`** | 轨迹处理变成显式管线 |
| `Episode` / `EpisodeV2` / `SampleCollector` / `ViewRequirement` | **`SingleAgentEpisode`** / **`MultiAgentEpisode`** | 数据表示统一 |
| `DefaultCallbacks` | **`RLlibCallback`** | 回调基类**改了名**（老名字在迁移期仍能用）；传法不变，还是 `config.callbacks(YourClass)`。**常用钩子**（从 `DefaultCallbacks` 迁移时最需要对照的部分）：`on_algorithm_init` / `on_train_result` / `on_episode_start` / `on_episode_step` / `on_episode_end` / `on_sample_end` / `on_evaluate_start` / `on_evaluate_end` / `on_env_runners_recreated` / `on_checkpoint_loaded`。（⚠️ **`on_postprocess_trajectory` 故意不在上面这份清单里** —— 它和 `on_create_policy()` 在 2.58 的 `callbacks.py` 里都还留着，但带着 `@OldAPIStack` 装饰器，**新 API stack 下不会触发**，轨迹处理逻辑要写进 `ConnectorV2`，见下面第 4 点。）⚠️ 完整清单请以你所用版本的 `rllib/callbacks/callbacks.py` 为准 —— **本书未逐版本核对这份清单** |
| `AlgorithmConfig` / `Algorithm` | **不变** | 入口 API 保持兼容 |

几个直接后果：

1. **训练批大小变成 per-learner 的量**。旧 stack 的 `train_batch_size` 是全局的；
   新 stack 用 `config.training(train_batch_size_per_learner=...)`，该值
   **不需要随 learner 数量变化**。
2. **策略网络配置从 `config.training(model=...)` 移到 `config.rl_module()`**。
3. **探索行为从 `exploration_config` 移到 `config.env_runners(explore=True/False)`**，
   它决定 EnvRunner 调用 `RLModule._forward_exploration()` 还是
   `_forward_inference()`；自定义探索就重写 `_forward_exploration()`。
4. **回调 `on_create_policy()` / `on_postprocess_trajectory()` 不再可用**，
   轨迹处理全交给 `ConnectorV2` 管线；**`sample_async` / `AsyncSampler` 也不再支持**。

容易混淆的一点：`AlgorithmConfig` 与 `Algorithm` 这两个**名字**没变，
变的是它们下面挂的方法 —— `PPOConfig()` 还是对的，但
`PPOConfig().training(train_batch_size=...)` 已经不对了。

## 16.3 只支持 PyTorch：被弃用的配置清单

**Ray 官方明确移除了 TensorFlow 支持**，RLlib 收敛到单一框架 PyTorch。
迁移指南的原话是它 "supports a single deep learning framework, PyTorch"，
并要求用户删掉所有 TF 相关设置。

这件事不是一步到位的：Ray 2.38.0 的提交记录了 "[RLlib] Remove Tf support on
new API stack for PPO/IMPALA/APPO (only DreamerV3 on new API stack remains with
tf now)" —— **DreamerV3 一度是最后保留 TF 路径的新 stack 算法**，后来也收敛掉了。

被弃用 / 移除的配置项（部分）：

| 配置 | 状态 | 替代 |
|---|---|---|
| `exploration_config` / `config.exploration()` | 弃用；设置它会抛 `ValueError` | `config.env_runners(explore=...)` + 重写 `_forward_exploration()` |
| `num_gpus` / `_fake_gpus` | 弃用 | `config.learners(num_gpus_per_learner=...)` |
| `num_cpus_for_local_worker` | 改名 | `num_cpus_for_main_process` |
| `train_batch_size` | 不再使用 | `config.training(train_batch_size_per_learner=...)` |
| `num_workers` | 不再使用 | `config.env_runners(num_env_runners=...)` |
| `create_env_on_local_worker` | 弃用 | 见 `env_runners()` 相关设置 |
| `enable_connectors` | 弃用 | 新 stack 始终使用 ConnectorV2 |
| `eager_tracing` / `eager_max_retraces` / `tf_session_args` / `local_tf_session_args` | 弃用（TF 专用） | 删除 |
| `model=`（在 `training()` 里） | 不再支持 | `config.rl_module(model_config=...)` |

⚠️ 一个会让人困惑的报错：如果写 `config.framework("tf")` 且新 stack 开着，
`AlgorithmConfig` 的校验会直接抛错，提示你要么改 `tf2`、要么关掉新 stack。
**正确做法是 `config.framework("torch")` 并把 TF 设置全删。**

`num_gpus` 的替代写法要理解语义：

```python
config.learners(num_learners=2, num_gpus_per_learner=1)  # 旧写法 resources(num_gpus=2)
config.learners(num_learners=2, num_cpus_per_learner=4,
                num_gpus_per_learner=0)                  # 纯 CPU 多 learner
```

**扩容 learner 的经验法则**（官方迁移指南）：保持
`train_batch_size_per_learner` 不变，把学习率按
`lr = base_lr * (num_learners ** 0.5)` 放大。这是经验规则不是理论结论，
换任务要重新验证。

## 16.4 关于 `local_mode` 的移除：先纠正一个常见误解

社区里流传"Ray Core 的 `local_mode` 和 RLlib 的 `local_mode` 是两回事"。
**实际上不是。** 澄清如下：

* RLlib **没有**私有的 local mode 实现。历史上在 RLlib 里做本地调试用的是
  `ray.init(local_mode=True)` —— 这是 **Ray Core** 的特性，作用是把所有
  task/actor 放进 driver 进程的单线程里执行。
* **PR #60647（"[core] Remove support for `local_mode` (un-revert)"，合并提交
  `db822f50`）从 Ray Core 里移除了这个特性**，影响面横跨 C++ / Java / Python，
  并**同时清理了 RLlib 与 Tune 的相关代码路径**（涉及 `rollout_worker.py`、
  `policy.py`、`tf_policy.py`、`torch_policy.py`、`tune_controller.py`、
  `trial.py` 等）。
* 这是一次 **un-revert**：先前的移除（#60543）因为"RLlib 代码没同步更新"被
  回滚过，这次把 RLlib 一并修好后才重新合入。官方明确表示**没有替代品**：
  维护者（edoakes）的解释是这个实现 "just not maintainable and slowing us
  down a lot"，并说未来可能重新引入。

**行为变化**：`ray.init(local_mode=True)` 现在直接抛 `RuntimeError`；但 RLlib
示例脚本的参数解析器里 `--local-mode` **仍然被定义**，而所有使用点被删除 ——
也就是**静默忽略**。这是评审里明确指出的问题：用户以为在跑本地模式，实际跑的
是分布式模式，没有任何警告。另有报告称 `add_rllib_example_script_args()` 里该
参数被**重复定义**，会导致 `ArgumentError: conflicting option string: --local-mode`。

> 与 mini-ray 的关系：mini-ray 的 `local_mode=True` 是第 06 章里**自己实现**
> 的教学特性（`test_local_mode.py` 覆盖），不受 Ray 上游决定影响，依然可用。
> 但**不要**把 mini-ray 上的经验直接迁移到真实 Ray 2.58+。

## 16.5 现代最小示例

```python
from ray.rllib.algorithms.ppo import PPOConfig

config = (
    PPOConfig()
    .environment("CartPole-v1")
    .env_runners(num_env_runners=2,          # 取代 num_workers
                 num_envs_per_env_runner=4,  # 每个 runner 内部向量化 4 个环境
                 explore=True)               # 取代 exploration_config
    .learners(num_learners=1,
              num_gpus_per_learner=0)        # 取代 resources(num_gpus=...)
    .training(train_batch_size_per_learner=2000,   # 取代 train_batch_size
              lr=3e-4)
    .rl_module(model_config={"fcnet_hiddens": [128, 128]})  # 取代 training(model=...)
    .framework("torch")                      # 唯一的框架选项
)

algo = config.build_algo()          # 注意是 build_algo()，不是 build()
for i in range(10):
    result = algo.train()
    print(i, result["env_runners"]["episode_return_mean"])
    if i % 5 == 0:
        print("eval:", algo.evaluate())
algo.stop()
```

> 新 stack 的推荐入口是 `build_algo()`；`config.build()` 是
> `@Deprecated(new="AlgorithmConfig.build_algo", error=False)` 的**转发别名**
> （`rllib/algorithms/algorithm_config.py`，函数体就是 `return self.build_algo(*args, **kwargs)`）
> —— 行为**等价**，只是会多打一条弃用告警。

| 参数 | 含义 | 调参直觉 |
|---|---|---|
| `num_env_runners` | 采样进程数 | 采样瓶颈时加它，注意与 GPU 配比 |
| `num_envs_per_env_runner` | 每个 runner 内部并行环境数 | 环境轻量时优先加这个（省进程开销） |
| `train_batch_size_per_learner` | **每个** learner 的批大小 | 加 learner 时保持不变 |
| `num_learners` | 训练 worker 数 | 大模型 / 大 batch 时加 |
| `lr` | 学习率 | 加 learner 时按 `sqrt(num_learners)` 放大 |

## 16.6 算法清单与适用场景

| 算法 | 类型 | 适用 | 备注 |
|---|---|---|---|
| **PPO** | on-policy 策略梯度 | 通用首选，离散/连续动作都行 | 最稳，调参最省心 |
| **SAC** | off-policy 最大熵 | 连续控制、样本效率敏感 | 需要 replay buffer |
| **DQN / Rainbow** | off-policy 值方法 | 离散动作、Atari 类 | 不支持 RNN |
| **APPO** | 异步 PPO | 大规模分布式采样 | 支持 RNN |
| **IMPALA** | 异步 actor-learner | 高吞吐采样 | 支持 RNN |
| **DreamerV3** | 基于世界模型 | 样本效率极致敏感 | 结构最复杂，模型在 RLModule 内 |
| **BC** | 行为克隆（离线） | 有专家轨迹、无奖励 | 离线算法 |
| **CQL** | 保守 Q 学习（离线） | 纯离线 RL | 离线算法 |
| **MARWIL** | 优势加权回归（离线） | 有奖励标注的离线数据 | 离线算法 |

⚠️ **`rllib_contrib` 的边界**：Ray 2.8 就把 24 个算法移到了
`rllib_contrib`，包括 A3C、DDPG、Dreamer V1 等。这些算法
**不在主包里**，安装名是**每个算法一个包**，**不是**一个大包。而且：

* 它们**不保证与最新 API stack 同步** —— 迁移后仍可能停留在旧 stack 的写法，
  混用时会得到"配置项不认识"这类报错；
* 导入路径是 `rllib_contrib.<algo>`，**不在 `ray.rllib.algorithms` 下**；
* 判断一个算法在不在主包：`from ray.rllib.algorithms.<name> import <Name>Config`
  能不能 import 成功。

> 🔴 **本书第四轮在这段里写错了两个细节，第五轮对着 2.8.1 / 2.24.0 / 2.40.0
> 的源码改正**：
> ① **它不是"社区维护的独立仓库"** —— 它是 **`ray-project/ray` 仓库里的一个目录**
>    （`codeload.github.com/ray-project/rllib_contrib` 是 404）；
> ② **安装名没有 `-contrib-`**：`rllib_contrib/a3c/pyproject.toml` 里声明的是
>    `name = "rllib-a3c"`，所以是 **`pip install rllib-a3c`**，
>    而 `pypi.org/pypi/rllib-contrib-a3c/json` 返回 **Not Found**。
>    照着 `rllib-contrib-a3c` 装会直接找不到包。
>
> 📌 **还有一个更新得补上**：`rllib_contrib` 目录**已在 Ray 2.40 从主仓库删除**
>    （2.39 是最后一个还带它的版本）。所以对 2.58 而言，这些算法**既不在主包、
>    也不在仓库里** —— 它们属于历史。要用请把 Ray 钉在 2.39 之前，
>    或直接换用主包里的算法。

社区经验下的选型顺序：离散动作想快速出结果用 **PPO**；连续控制且样本贵用
**SAC**；离线有奖励用 **CQL / MARWIL**；离线无奖励（纯模仿）用 **BC**；
超大规模采样用 **APPO / IMPALA**。

**关于"跑很久不收敛"**：RLlib 社区最常见的抱怨不是 API 而是这个。原因按频率
排序是 —— ① 环境没写对（奖励尺度、termination 条件、随机种子）；② 归一化缺失
（观测/奖励没标准化，连续控制尤其致命）；③ 批大小与学习率不匹配（改了
`train_batch_size_per_learner` 忘了改 `lr`）；④ 并行环境数太多导致每次更新步数
不足。**调 RL 的时间应该大部分花在第 1 条上。**

## 16.7 扩展性与配比：采样吞吐常常是瓶颈

Ray Summit 2025 给的规模数字是 **10,000+ env runners / 100+ learners**。
但"能扩展到"和"跑得快"是两件事：

```
[EnvRunner × N] 采样 → [ConnectorV2] 处理 → [Learner × M] 训练
      ▲                                          │
      └────────── 权重同步（broadcast）◄─────────┘
T_iter = T_sample / N + T_connector + T_train / M + T_sync
```

判断谁是瓶颈：看 `algo.train()` 返回的计时字段（`env_runners` 的采样时间、
`learners` 的训练时间、`timer` 里的 `sync_weights`）。

| 现象 | 结论 | 动作 |
|---|---|---|
| 采样时间 >> 训练时间 | 采样瓶颈 | 加 `num_env_runners` / `num_envs_per_env_runner`，或让环境更快 |
| 训练时间 >> 采样时间 | 训练瓶颈 | 加 `num_learners`、上 GPU、放大 `train_batch_size_per_learner` |
| 两者都不大但迭代慢 | **同步开销瓶颈** | 检查权重同步；大规模下考虑 APPO 的异步架构 |
| GPU 利用率低但训练时间长 | 数据搬运/CPU 预处理 | 检查 ConnectorV2 里的重活、观测是 numpy 还是 torch |

**GPU 与 env runner 的配比**没有万能公式，但有一条底线：**不要让 env runner
把机器占满**。环境模拟通常纯 CPU，如果 `num_gpus_per_learner` 是 0 而你又给了
大量 env runner，learner 会抢不到 CPU。

一个常见的自欺欺人：`num_envs_per_env_runner` 开得很大看起来吞吐很高，但如果
环境是 Python 写的、受 GIL 限制，向量化收益会在某个点饱和（Gymnasium 的
`SyncVectorEnv` 就是纯串行）。改用 `AsyncVectorEnv` 或把环境改写成 C/Rust，
比继续加进程有效得多。

## 16.8 RL 生态全景（2026 视角）

这一节决定你实际上会用什么写代码。

| 框架 | 出身 | 训练侧 | Rollout 侧 | 建立在 Ray 上 |
|---|---|---|---|---|
| **verl** | 字节 Seed（现 `verl-project/verl`） | FSDP / FSDP2 / Megatron-LM / Automodel / TorchTitan / VeOmni | vLLM / SGLang / HF Transformers / TensorRT-LLM | 是 |
| **OpenRLHF** | 社区 | Megatron-LM / DeepSpeed | vLLM | 是 |
| **SkyRL** | NovaSky-AI | 模块化全栈 | 多种 | 是 |
| **NeMo-RL** | NVIDIA | 自有 + CUDA IPC 零拷贝权重传输 | vLLM | 是 |
| **AReaL** | 蚂蚁 | 异步 actor/learner 资源分离 | — | 生态内，**Ray 作为编排层未在本次检索中确证** |
| **slime** | THUDM | Megatron | **SGLang 原生** | 是 |
| **miles** | radixark | 与 slime 同源共建（企业向，LLM/VLM） | SGLang | 是 |
| **Molt** | NVIDIA NeMo（较新，PyTorch 原生 agentic RL） | FSDP2 / AutoModel | vLLM | 是（用 Ray 做放置与异步队列） |
| **Vime** | vLLM 社区（源自 slime） | 代码按 `vime/ray/placement_group.py` 等组织 | vLLM | 是 |
| **RLlib** | Ray 官方 | 自有 Learner | 自有 EnvRunner | 是（就是它自己） |

关键事实与规模感：

* Anyscale 官方说法是 **"Ray is the engine for veRL, skyRL and more"**，并称
  "veRL, SkyRL, OpenRLHF, and other leading RL libraries are built on Ray,
  no rewiring required"。
* Google Cloud 与 Anyscale 的联合内容（2026-08-26）："Ray has become a common
  runtime for orchestrating post-training workloads. Frameworks including veRL,
  NeMo-RL, SLIME, MILES, and SkyRL already use Ray to coordinate distributed
  trainers, inference engines, rollout workers, and other components."
* Hugging Face 在 2026-03-10 做的 16 个开源 RL 库调查结论是
  **"Ray is the de facto orchestration layer: 8 of 16 libraries use it."**
  注意是 **8/16** —— Ray 是主导者但**不是唯一选择**。

**反例（诚实的全景图必须包含）**：Tunix（JAX/XLA，面向 TPU）、Meta TorchForge
（Monarch actor 模型）、PipelineRL（Redis pub/sub），以及若干用原生 Python
并发的实现。**一个共同瓶颈**：HF 那份调查指出，这个生态在 2026 年的共同瓶颈是
**"长 rollout 期间的 GPU 空闲时间"**，这也是各框架纷纷转向**解耦式异步训练**
（推理 GPU 池与训练 GPU 池分离 + rollout buffer）的原因。这反过来解释了为什么
Ray 在这个位置很稳：**它擅长编排异构资源池之间的调度与数据搬运。**

## 16.9 Ray 提供的是编排，不是算法

这一节要讲透一个容易被"Ray 是引擎"这句话掩盖的边界。

```
┌──────────────────────────────────────────────────────────┐
│ 算法层：PPO / GRPO / DAPO 的损失、优势估计、KL 项          │ ← verl/SkyRL/…自己的
│ Kernel 层：FlashAttention、NCCL/RDMA、CUDA IPC、KV 传输    │ ← vLLM/SGLang/NVIDIA 的
│ 编排层：谁放哪张卡、何时拉起 rollout、权重何时同步、        │ ← **Ray 在这里**
│         placement group 怎么切、挂了怎么恢复               │
│ 资源层：CPU / GPU / 显存 / 网络                            │
└──────────────────────────────────────────────────────────┘
```

以 verl 为例说明"编排"具体是什么（verl 官方文档）：单进程 Ray driver
（`RayPPOTrainer`）串起 rollout → reward → advantage → train → sync 的循环；
`RayWorkerGroup` 把分布式 worker 变成 RPC 端点，负责资源池与放置组，分派模式
有 `ONE_TO_ALL`、`DP_COMPUTE_PROTO`、`ALL_TO_ALL` 等；`ActorRolloutRefWorker`
把 actor + rollout（+ 可选 reference policy）**共置**，`TrainingWorker` 包一个
`BaseEngine`，后端在运行时从 `actor.strategy` / `critic.strategy` 经
`EngineRegistry` 选；`update_weights` 把 trainer 权重推给 rollout 引擎 ——
v0.8 起把 vLLM worker 与 trainer 进程分离，改用 **CUDA IPC** 做权重更新。

**看这条链路里 Ray 出现的位置**：几乎不在"算法"或"kernel"里，全是
"放在哪、什么时候、怎么同步"。这就是"Ray 是编排层"的确切含义。

两个推论：① **学 Ray 的收益是"看得懂这些框架在干什么"**，而不是用 Ray 写
RL 算法 —— 你读 verl 的 `RayWorkerGroup` 时如果懂 placement group 与 actor
模型，会快很多；② **Ray 在这个位置上的可替代性讨论是真实的**，第 01 章提到的
vLLM RFC #35848 把 Ray 降级为"进程启动器 + 放置管理器"就是同一个逻辑。

## 16.10 GRPO 为什么成了默认算法

**机制**：GRPO（Group Relative Policy Optimization，出自 DeepSeekMath，
arXiv:2402.03300）对一个 prompt 采样**一组** G 个 rollout，用 verifier 给每个
打分，然后用**组内相对优势**
`A_i = (r_i - mean(r_1..r_G)) / (std(r_1..r_G) + eps)` 替代 critic；这个标准化
后的优势直接塞进 PPO 式的 token 级 clip 目标函数。

**为什么它成了默认**：

1. **无 critic**。传统 PPO 要额外训一个 value network，显存大约翻倍；GRPO 用
   样本组均值当 baseline，省下的显存可以换成更大的 batch、更长的 rollout。
   （**未确认**：常被引用的"省下约一半训练显存带宽"这个具体数字，本次检索没有
   找到可核对的出处 —— 它取决于模型与 batch 配置，请当成量级直觉而不是结论。）
2. **不需要 reward model**（在 RLVR 场景下）。RLVR 用确定性 oracle 当奖励：
   数学题答案、代码测试用例、SQL 执行结果、定理证明器。**GRPO 对奖励来源完全
   不关心**，它只消费一组标量 —— 整条链路从"四个模型（policy / ref / reward /
   critic）"塌缩成"两个（policy / ref）"。
3. **和可验证奖励天然配对，且生态动量极大**。组内归一化会自动按题目难度缩放，
   难题与简单题的梯度尺度被拉平；DeepSeek-R1（arXiv:2501.12948）的推理能力提升
   **没有用任何偏好模型**，纯靠 RLVR，此后 GRPO 成为开放模型（DeepSeek-R1、
   Qwen3、GLM）的事实标准，verl 等框架原生支持。

**已知缺陷（这段比优点更重要）**：

* **优势塌缩**：一组 rollout 拿到**完全相同**的奖励时（二元 verifier 的冷启动
  阶段极常见），组内方差为 0 → 标准化优势为 0 → **学习信号直接消失**；
* **高方差与不稳定**：长序列上的 token 级重要性比率容易频繁触发 clip；
* **信用分配粗糙**：轨迹级的单一优势被广播到所有 token；
* **熵塌缩 / 模式退化**，以及窄验证集上的 **reward hacking** 风险。

2026 年一批工作正是冲这些缺陷去的：**DAPO**（解耦 clip + 动态采样）、
**BV-Blend**（把历史聚类条件矩与 prompt 局部统计混合，稳定 critic-free RLVR）、
**VIMPO**（用 policy-implied value 做 token 级信用，仍不用 critic），以及
RC-GRPO / GSPO / TEPO 等变体。（**未确认**：这些变体的具体论文出处与相互差异
本次未逐篇核对，名字仅作为"往哪个方向找答案"的索引。）

**实践建议**（社区经验）：**从 GRPO + RLVR 起步，撞到问题再换 PPO / DAPO /
StepPO。** 不要一开始就选最复杂的方法。

### 一条要标注为"社区观点"的趋势判断

社区有一种说法：GRPO 把优化"商品化"之后，**后训练的护城河转移到了"可信的
reward"与"逐步（step-level）eval"层**。

**这是观点，不是事实。** 支持它的是 reward hacking 的普遍性；反对它的是算法侧
仍在快速迭代（DAPO / VIMPO 都是 2026 年的新东西）—— 说"优化已经商品化"为时
尚早。本书把它列为**待观察的判断**，不作为结论。

## 16.11 一个完整可读的 RL 示例

用一个自定义环境（比 CartPole 更贴近真实需求），关键参数都标出来。

```python
# rl_demo.py
import gymnasium as gym
import numpy as np
from gymnasium import spaces
from ray.rllib.algorithms.ppo import PPOConfig


class LineFollower(gym.Env):
    """一维"跟线"任务：agent 要把位置保持在 0 附近。状态 2 维，动作 3 个。"""

    def __init__(self, config=None):
        self.observation_space = spaces.Box(-5.0, 5.0, shape=(2,), dtype=np.float32)
        self.action_space = spaces.Discrete(3)
        self.pos, self.vel, self.steps = 0.0, 0.0, 0

    def _obs(self):
        return np.array([self.pos, self.vel], dtype=np.float32)

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        # 随机初始偏移：防止策略只学会"什么都不做"
        self.pos = float(self.np_random.uniform(-1.0, 1.0))
        self.vel, self.steps = 0.0, 0
        return self._obs(), {}

    def step(self, action):
        self.vel = 0.9 * self.vel + float(action - 1) * 0.5   # 动作 → -0.5/0/+0.5
        self.pos += self.vel
        self.steps += 1
        reward = -abs(self.pos)                 # 越接近 0 越好
        terminated = abs(self.pos) > 5.0        # 跑飞了就结束
        truncated = self.steps >= 200
        return self._obs(), reward, terminated, truncated, {}


def build_config() -> PPOConfig:
    return (
        PPOConfig()
        .environment(LineFollower)
        .env_runners(num_env_runners=2,           # 先给 2，跑通了再加
                     num_envs_per_env_runner=8,   # 每进程 8 个向量化环境 → 并行 16
                     explore=True)
        .learners(num_learners=1, num_gpus_per_learner=0)   # 小任务 CPU 足够
        .training(train_batch_size_per_learner=4000, minibatch_size=256,
                  num_epochs=10,                 # 每批数据复用几轮
                  lr=3e-4, gamma=0.99, lambda_=0.95,   # GAE 的 lambda
                  clip_param=0.2, entropy_coeff=0.01, vf_loss_coeff=1.0)
        .rl_module(model_config={"fcnet_hiddens": [128, 128]})
        .evaluation(evaluation_interval=5, evaluation_num_env_runners=1,
                    evaluation_duration=10,                 # 每次评估 10 个 episode
                    evaluation_config={"explore": False})   # 评估时不要探索
        .framework("torch")
        .debugging(seed=42)
    )


if __name__ == "__main__":
    algo = build_config().build_algo()
    best = -1e9
    try:
        for it in range(60):
            res = algo.train()
            if it % 5 == 0:
                ev = algo.evaluate()
                best = max(best, ev["env_runners"]["episode_return_mean"])
                print(f"[{it:3d}] train={res['env_runners']['episode_return_mean']:8.2f}"
                      f"  eval={best:8.2f}")
    finally:
        algo.stop()
```

**跑不通时按这个顺序查**：① `num_env_runners=0` 能跑吗？能跑说明问题在分布式
接线（通常是环境不可序列化）；② 环境能被 pickle 吗（自定义 `Env` 必须可序列化，
**用类而不是闭包**）；③ `evaluation_config={"explore": False}` 有没有 ——
**忘了这个，评估分数会莫名其妙地低**；④ 奖励尺度对不对 —— 把
`episode_return_mean` 打出来看量级，±1 和 ±10000 之间调学习率是完全不同的事。

## 16.12 选型：我要做 LLM 后训练，该选 verl 还是 RLlib

是 LLM（或 VLM）的 RL 后训练，且规模 > 1 张卡、要跑 PPO/GRPO 之一 →
**verl**（或生态内的 SkyRL / NeMo-RL / slime，看引擎偏好）；只是单卡小规模
验证想法也先用 verl，RLlib 在这个场景没有优势。
不是 LLM 后训练 —— 经典 RL（控制 / 游戏 / 仿真 / 推荐 / 资源调度）或离线 RL /
模仿学习（BC、CQL、MARWIL）→ **RLlib**（后两者在 LLM 后训练生态里没有对应物）。

**为什么 LLM 后训练不选 RLlib**：

| 维度 | RLlib | verl 等 |
|---|---|---|
| 训练并行 | 自有 Learner（数据并行 + `num_learners`） | FSDP / FSDP2 / Megatron-LM，含 **TP/PP/EP** |
| Rollout 引擎 | 自有 EnvRunner（环境是 gym 接口） | **vLLM / SGLang**，即"文本生成即环境" |
| 权重同步 | broadcast 到 EnvRunner | `update_weights` + CUDA IPC / 零拷贝 |
| 多轮与工具调用 | 需要自己包 gym 环境 | `AgentLoop` 抽象原生支持 |

核心矛盾只有一句话：**LLM 后训练的"环境"是一个推理引擎，不是 gym.Env。**
把 vLLM 塞进 EnvRunner 不是不可能，但你要自己解决"KV cache 怎么管、权重怎么
高频同步、TP 组怎么和 learner 组对齐" —— 而 verl 已经把这些做了两年。

**为什么经典 RL 不选 verl**：它的整套抽象（`ActorRolloutRefWorker`、
`HybridFlow`、token 级 advantage）是为"序列生成 + 序列级奖励"设计的。
拿一套为文本设计的并行方案去套一个小 MLP，复杂度收益比是负的。

**中间地带：Agentic RL**。多轮工具调用、代码执行、浏览器操作这类 agentic RL
两边都在抢：verl 有 `AgentLoop`，RLlib 有 gym 包装路线，另外还有像 Molt
（NVIDIA NeMo，PyTorch 原生 agentic RL）这样的新框架。**这个领域截至 2026 年
还没有定论**，选型时优先看"哪个框架的 agent 环境和你的任务形状最接近"。

> 📌 **这条中间地带到 2026 年已经独立成档**：它的优化目标不是"一次回答好不好"，
> 而是"一串动作好不好"，rollout 从"一次前向"变成"跑完整个 Agent 环境再收集
> 轨迹" —— 采样成本高一个量级，环境要能重放与并发跑上千实例，
> **于是它又回到了 Ray 最擅长的那件事上**。
> **完整的框架清单与四条选型判据见第 38 章 §38.10**；
> 那一档的玩家除了上面三个，还有 SkyRL / OpenRLHF / **ART**（OpenPipe）/
> **Agent Lightning**（微软）/ **RAGEN**（第 19 章 §19.8 有同样的清单，
> 三处口径一致）。

## 16.13 多智能体：`multi_agent()` 与 `MultiRLModuleSpec`

前面所有例子都是**单智能体**（一个环境、一个策略），但多智能体是 RLlib 最核心
的能力之一 —— §16.2 的表里 `MultiAgentEpisode` 就是为它准备的，正文却一次都没
展开。这一节补上。

**最小可用形态**（两个策略，按 agent id 分派）：

```python
from ray.rllib.algorithms.ppo import PPOConfig
from ray.rllib.core.rl_module.multi_rl_module import MultiRLModuleSpec
from ray.rllib.core.rl_module.rl_module import RLModuleSpec

def policy_mapping_fn(agent_id, episode, **kwargs):
    # 新 stack 的签名只有 (agent_id, episode, **kwargs)；
    # 旧 stack 的 (agent_id, episode, worker, **kwargs) 会抛 TypeError
    return "main" if str(agent_id).startswith("main") else "opponent"

config = (
    PPOConfig()
    .environment("your_multi_agent_env")      # gymnasium 的 MultiAgentEnv
    .multi_agent(
        policies={"main", "opponent"},        # 也可以 dict：{"id": PolicySpec(...)}
        policy_mapping_fn=policy_mapping_fn,
        policies_to_train=["main"],           # 只训练 main，对手冻结
    )
    .rl_module(
        rl_module_spec=MultiRLModuleSpec(
            rl_module_specs={
                "main":     RLModuleSpec(model_config={"fcnet_hiddens": [256, 256]}),
                "opponent": RLModuleSpec(model_config={"fcnet_hiddens": [64, 64]}),
            }
        )
    )
    .env_runners(num_env_runners=2)
    .training(train_batch_size_per_learner=4000)
    .framework("torch")
)
```

| 参数 | 作用 | 坑 |
|---|---|---|
| `policies` | 声明有哪些策略 | 可以是 `set` / `list`（只有 id），也可以是 dict（id → `PolicySpec` / 元组），**每个策略观测/动作空间不同时必须用 dict 形态** |
| `policy_mapping_fn` | agent id → 策略 id | **新 stack 的签名去掉了 `worker`**，照抄老教程会 `TypeError` |
| `policies_to_train` | 哪些策略参与训练 | 不写默认全部训练；冻结对手 / 陪练靠它 |
| `MultiRLModuleSpec.rl_module_specs` | 每个策略一个 `RLModuleSpec` | key 必须与 `policies` 里的 id 对得上，对不上在构建时报错 |

**几个必须知道的行为变化**：

* **`Algorithm.add_policy()` 在新 stack 下不可用**。旧 stack 靠它动态加策略
  （自对弈、联赛训练），新 stack 要改用 **`Algorithm.add_module()`** 并传
  `module_spec` —— 官方理由是"只有 policy spec 不足以构建模块"。
* **观测空间按 agent 变形**是常态（每个 agent 看到的东西不一样），别指望 RLlib
  自动推断，要在 `policies` 的 dict 形态或各自的 `RLModuleSpec` 里显式声明。
* **参数共享 / 共享 critic** 这类需求靠 `RLModuleSpec.module_class` 指定同一个
  模块类来做；官方有专门示例，**先抄示例再改**比自己搭快得多。

> **和 §16.12 的选型连起来看**：LLM 后训练里的"多智能体"（多轮工具调用、多角色
> 对话）**不在这套抽象里** —— verl 的 `AgentLoop` 才是为它设计的。RLlib 的
> `multi_agent()` 面向**经典多智能体环境**（博弈、协作、仿真、自对弈）。

## 16.14 `LearnerGroup`：把"学习"从"采样"里拆出来

§16.2 的表说 `Learner` 取代了 `Policy` 的一部分职责，但没说它长什么样、怎么单独
用。`LearnerGroup` 是**一组（可能跨机器分布的）Learner 的协调器**：每个 Learner
持有一份相同的 RLModule，拿到 batch 的不同分片算梯度，再 all-reduce。

**为什么它存在**：这是新 stack 最重要的一次架构切分 —— **采样（EnvRunner）与
训练（Learner）变成两组可以独立伸缩、独立放置的 worker**。旧 stack 里策略和采样
worker 是绑在一起的；现在你可以用 100 个 EnvRunner 喂 4 个 GPU Learner
（§16.7 那套瓶颈分析正是建立在这个切分上）。

```python
from ray.rllib.algorithms.ppo import PPOConfig

config = (
    PPOConfig()
    .environment("CartPole-v1")
    .learners(num_learners=2,               # 0 = 只在主进程里算
              num_gpus_per_learner=1)
    .training(train_batch_size_per_learner=2000)
)

learner_group = config.build_learner_group(env=env)   # 不经过 Algorithm 单独建

batch = ...                                             # 一批训练数据
results = learner_group.update(batch=batch, timesteps=2000)
future = learner_group.update(batch=batch, timesteps=2000, async_update=True)
                                                        # ↑ 非阻塞：APPO/IMPALA 需要

weights = learner_group.get_weights()                   # 只含 RLModule（网络）权重
learner_group.set_weights(weights)
```

| 方法 | 作用 | 备注 |
|---|---|---|
| `update(batch=..., timesteps=..., async_update=...)` | 跑一次训练更新 | 返回 **n 个 Learner 各自的结果 dict**（一个 list）；异步更新是 APPO / IMPALA 这类算法的形态 |
| `get_weights()` / `set_weights()` | 取 / 推**网络权重** | 只有 RLModule 的权重，**不含 optimizer 状态** |
| `get_state()` / `set_state()` | 取 / 推**完整状态** | 含 optimizer 状态 —— **续训必须用这一对** |
| `save_to_path()` / `restore_from_path()` | LearnerGroup 的 checkpoint | `Algorithm` 保存时调的就是它（§16.18） |

**和 `Algorithm` 的关系**：`Algorithm.get_weights()` / `set_weights()` 就是转发到
`learner_group`，而且 **`set_weights()` 会自动把权重同步到所有 EnvRunner** ——
"改完权重忘了广播"是旧写法里很常见的一类 bug，新 stack 直接把它消掉了。

> **一组容易记混的口径**：`get_weights` / `set_weights` **只管 RLModule**；
> `get_state` / `set_state` **管全部**（含 optimizer）。"评估时热更新权重"用
> 前者，"续训"用后者，用错了会静默丢掉优化器状态。

## 16.15 `EnvRunner`：从配置项到可调用的采样器

§16.2 只说了 `RolloutWorker` → `EnvRunner` 这次改名，§16.5 的示例里它只是
`.env_runners(...)` 里的几个配置项。它的实际身份是：**持有若干向量化环境、负责
采样、并在本地持有一份 RLModule 用于推理的 worker**。

```python
# ⚠️ 没有 config.build_env_runner() 这个方法 —— 第五轮已对着 2.58 源码确认
#    （algorithm_config.py / algorithm.py 里零命中）。要拿现成的 EnvRunner
#    集合请走 algo.env_runner_group（见本节的说明）。

# 采样：新 stack 返回的是 Episode 列表，不是 batch
episodes = env_runner.sample(num_timesteps=2000)          # 或 num_episodes=10
episodes = env_runner.sample(num_episodes=10, force_reset=True)
metrics = env_runner.get_metrics()

env_steps = sum(e.env_steps() for e in episodes)          # 新 stack 的计数方式
agent_steps = sum(e.agent_steps() for e in episodes)
```

| 变化 | 旧 stack（`RolloutWorker`） | 新 stack（`EnvRunner`） |
|---|---|---|
| `sample()` 返回 | `SampleBatch`（列式批） | **`list[SingleAgentEpisode]` / `list[MultiAgentEpisode]`** |
| 步数统计 | `batch.env_steps()` / `agent_steps()` | `sum(e.env_steps() for e in episodes)` |
| 探索 vs 推理 | `exploration_config` 决定 | `config.env_runners(explore=True/False)` 决定调 `_forward_exploration()` 还是 `_forward_inference()`（§16.2） |
| 采样与推断 | 分离 | 合并进同一个 worker |

> **改名不只是名字**：`RolloutWorker` 是"执行策略的工人"，`EnvRunner` 是"跑环境
> 的运行器"。读第三方代码或旧教程时，看到"worker 返回 batch"就是旧 stack；
> 看到"runner 返回 episodes"就是新 stack。`algo.env_runner_group` 是拿到现成
> EnvRunner 集合的入口（权重同步 `sync_env_runner_states` 就走它）。

> ✅ **`build_env_runner()` 不存在**（第五轮对 2.58 源码确认：
> `algorithm_config.py` 与 `algorithm.py` 里都**零命中**）。
> `Algorithm` 上有一个 `env_runner` **属性**、一个 `env_runner_group`，
> 但没有 `build_env_runner()`。
> 对比之下 **`build_learner_group(*, env=None, spaces=None, rl_module_spec=None,
> placement_group=None)` 是真的存在**（注意 `env` 是**仅关键字**参数）。
> 所以"单独建一个 LearnerGroup 来训练"可行，"单独建一个 EnvRunner"请改用
> `algo.env_runner_group`。

## 16.16 `ConnectorV2`：三段管线，以及它站在哪

§16.2 说 `Connector` → `ConnectorV2`"把轨迹处理变成显式管线"。**管线在"哪"比
管线本身更重要** —— 一共有三段，分居采样侧与训练侧：

```
        ┌───────────────── EnvRunner ─────────────────┐
        │ env-to-module 管线 → RLModule(推理) →       │
env.step │   ↑ [你的 ConnectorV2]    module-to-env 管线 │ → env.step()
        └─────────────────────────────────────────────┘
                        │ episodes
                        ▼
        ┌───────────────── Learner ───────────────────┐
        │ learner 管线 → RLModule(_forward_train)      │
        │   ↑ [你的 ConnectorV2]                       │
        └─────────────────────────────────────────────┘
```

| 管线 | 在哪 | 输入 → 输出 | 干什么 |
|---|---|---|---|
| **env-to-module** | EnvRunner | Episode 列表 → 一批观测 | 观测预处理（归一化、帧堆叠、flatten）、把观测凑成 batch |
| **module-to-env** | EnvRunner | RLModule 输出 → 可执行动作 | 动作后处理（反归一化、离散化、写回 Episode） |
| **learner 管线** | Learner | Episode 列表 → 训练批 | 训练侧重活（GAE、序列切分、帧堆叠的**对称实现**） |

写一个最小片段：

```python
import numpy as np
from ray.rllib.connectors.connector_v2 import ConnectorV2

class ClipObs(ConnectorV2):
    """把观测裁剪到 [-1, 1] —— 三段管线里最典型的"预处理"形态。"""

    def __call__(self, *, rl_module, batch, episodes,
                 explore=None, shared_data=None, metrics=None, **kwargs):
        assert "obs" in batch
        batch["obs"] = np.clip(batch["obs"], -1.0, 1.0)
        return batch                    # 必须把 batch 返回去

    def recompute_output_observation_space(self, input_observation_space,
                                           input_action_space):
        return input_observation_space  # 形状没变就不必改声明

config = config.env_runners(
    env_to_module_connector=lambda env, spaces, device: ClipObs(),
)
```

**必须知道的四点**：

1. **`ConnectorV2` 是 `PublicAPI(alpha)`** —— API 可能变。
2. **采样侧与训练侧要对齐**。帧堆叠这类改动如果在 EnvRunner 侧做了、Learner 侧
   没做，Learner 看到的观测分布就和采样时不一致 —— 这类 bug **不报错**，只是
   学不好（官方 frame-stacking 示例专门写两个 connector 类来保证对称）。
3. **三段管线各有一组默认片段**。例如 RLlib 会往 env-to-module 里塞一个
   `AddObservationsFromEpisodesToBatch`，保证 batch 里至少有最近一帧观测 ——
   你的片段是**插进去**的，不是替换全部。
4. **有状态的片段（归一化统计量）要实现 `get_state` / `set_state` /
   `reset_state`**，`Algorithm` 负责在 EnvRunner 与 Learner 之间同步它们。
   `MeanStdFilter` 是现成的参考实现，直接用比自己写省事。

> §16.2 提到旧 stack 的 `on_postprocess_trajectory` 回调不再可用 —— 那类逻辑现在
> 的正确归宿就是这里（通常是 learner 管线）。

## 16.17 `RLModule` 内部：`setup()` 与三个 forward

§16.2 说 `RLModule` 是"纯 PyTorch `nn.Module`"。作为 `nn.Module` 它当然有
`__init__`，但 RLlib 的约定是**把构建逻辑放在 `setup()` 里**：

```python
from ray.rllib.core.rl_module.torch import TorchRLModule

class MyModule(TorchRLModule):
    def setup(self):
        """RLlib 在构建后调用 —— 建网络写这里，不要写在 __init__ 里。"""
        obs_dim = self.observation_space.shape[0]
        self.encoder = torch.nn.Sequential(
            torch.nn.Linear(obs_dim, 128), torch.nn.ReLU())
        self.pi = torch.nn.Linear(128, self.action_space.n)
        self.vf = torch.nn.Linear(128, 1)

    def _forward_inference(self, batch, **kwargs):
        """评估 / 部署的前向：不探索、不算 loss。"""
        h = self.encoder(batch["obs"])
        return {"action_dist_inputs": self.pi(h), "vf_preds": self.vf(h).squeeze(-1)}

    def _forward_exploration(self, batch, **kwargs):
        """采样时的前向：带探索（默认直接复用 inference）。"""
        return self._forward_inference(batch, **kwargs)

    def _forward_train(self, batch, **kwargs):
        """训练前向：返回算 loss 需要的一切（value、entropy…）。"""
        ...
```

| 方法 | 谁来调 | 用途 |
|---|---|---|
| `setup()` | RLlib 构建时 | 建网络、建 Catalog（**约定：不要用 `__init__`**） |
| `_forward_inference()` | 评估、部署、`forward_inference()` 内部 | 确定性 / 贪心行为 |
| `_forward_exploration()` | EnvRunner 且 `explore=True` | 采样行为（默认 = inference） |
| `_forward_train()` | Learner | 训练前向，产出 loss 的输入 |

⚠️ **重写"带下划线"的，调用"不带下划线"的**：

```python
from ray.rllib.algorithms.algorithm import Algorithm

module = Algorithm.from_checkpoint(ckpt).get_module()   # 只取 RLModule，不要训练组件
out = module.forward_inference({"obs": obs_tensor})     # 公开入口：会跑前后处理编排
dist_cls = module.get_inference_action_dist_cls()
dist = dist_cls.from_logits(out["action_dist_inputs"])
action = dist.sample()[0].numpy()
```

自己调 `_forward_inference()` 会**绕过前后处理**（观测的 connector 管线、batch
结构校验），结果和真实服务时的行为不一定一致。

**Catalog** 是模块周边的"空间 ↔ 网络"适配层：它知道观测空间该配什么编码器、
动作空间该配什么输出头，也是 `model_config` 真正生效的地方。
`RLModuleSpec(model_config=...)` 里的东西最终就是喂给它的；某些场景下显式写
`catalog_class=None` 可以关掉 Catalog 走全手写路径（**是否推荐取决于版本，
未确认**）。

**读 → 用的完整路径**：`Algorithm.from_checkpoint()` → `get_module()` →
`forward_inference()` → 动作分布 → 采样。这条链路可以**完全脱离训练组件**，
只把一个 `RLModule` 拿去部署 —— 这是新 stack 相比旧 stack 的实质好处之一
（旧写法 `get_policy()` 在新 stack 下会直接报错）。

## 16.18 检查点：`save_to_path()` / `restore_from_path()` / `from_checkpoint()`

这是操作层面最常用、而本章前面完全没提的一组 API。

```python
from ray.rllib.algorithms.algorithm import Algorithm

ckpt = algo.save_to_path()          # 训练中断前保存，返回 checkpoint 目录
algo.stop()

algo2 = build_config().build_algo()
algo2.restore_from_path(ckpt)       # 原地恢复：会同时把权重同步给 EnvRunner

algo3 = Algorithm.from_checkpoint(ckpt)   # 用 checkpoint 里的类与构造参数新建对象
algo3.train()

module = Algorithm.from_checkpoint(ckpt).get_module()   # 只要模型，不要训练组件
```

| API | 语义 | 什么时候用 |
|---|---|---|
| `save_to_path(path=None)` | 存到目录并返回路径 | 定期保存；**Ray Tune 也会自动调它** |
| `restore_from_path(path)` | **原地**恢复（对象已存在） | 续训 |
| `Algorithm.from_checkpoint(path)` | 按 checkpoint 里的类与构造参数**新建**对象 | 换脚本、换进程恢复 |
| `algo.get_module()` | 只取 RLModule | 部署、评估、导出 |

**checkpoint 目录结构**（新 stack 的 Checkpointable API）：

```
ckpt/
├── metadata.json            # Ray 版本、RLlib checkpoint 版本、各状态文件名
├── class_and_ctor_args.pkl  # 存的是哪个类、构造参数 —— from_checkpoint 靠它重建
├── state.pkl / state.msgpack
├── env_runner/              # 子组件各自也是一套同样的结构
└── learner_group/
    └── learner/rl_module/…
```

> ⚠️ **推荐用 Checkpointable API**（`save_to_path` / `restore_from_path`），
> 这是 RLlib 官方文档指引的方向。
>
> 🔴 **但"`Algorithm.save()` / `restore()` 已弃用"这个说法是错的**
> （本书第四轮写的，第五轮对着 2.58 源码改正）：
> `Algorithm` **根本没有定义** `save()` / `restore()` —— 它们是**继承自
> Ray Tune 的 `Trainable`**，而在 2.58 的 `tune/trainable/trainable.py` 里
> 这两个方法标的是 **`@DeveloperAPI`，并没有标弃用**。
> 所以准确的说法是：**"RLlib 推荐的是 `save_to_path()`，
> `save()`/`restore()` 是 Tune 那边的遗产方法"** —— 是**推荐差异**，
> 不是**弃用状态**。照旧写法把"已弃用"当事实引用会误导读者去大改代码。
>
> 另一个常见误解：以前可以 `algo.get_policy()` 拿策略再推理 ——
> 新 stack 下这条路已经断了，改用 **`get_module()`**。

> **兼容性**：Ray 2.40 起，RLlib 的 checkpoint 在 **Ray 2.x 内向后兼容**
> （2.40 及之后的版本之间）。跨大版本不保证 —— 上生产请把 ray 版本锁进镜像，
> 并保留一份能重建 checkpoint 的训练脚本。

## 16.19 `replay_buffer_config`：off-policy 算法的缓冲区

§16.6 的算法表把 SAC / DQN 标为 off-policy，但 off-policy 的**核心组件**
replay buffer 本章一次都没出现。不配它算法也能跑（有默认值）—— 这正是它容易
被忽略的原因，也是"跑起来了但学不动"的常见原因。

```python
from ray.rllib.algorithms.sac import SACConfig
from ray.rllib.utils.replay_buffers import StorageUnit

config = (
    SACConfig()
    .environment("Pendulum-v1")
    .training(
        replay_buffer_config={
            "type": "ReplayBuffer",           # 类对象 / 短名 / 全路径三种写法等价
            "capacity": 100000,               # 单位由 storage_unit 决定，默认 10000
            "storage_unit": StorageUnit.TIMESTEPS,
            # ⚠️ learning_starts 不在这里!见下
        },
        num_steps_sampled_before_learning_starts=1000,   # ← 攒够这么多步才开始训练
    )
)
```

> ⚠️ **`learning_starts` 是旧 API stack 的字段**，新 stack 下它被置为
> `DEPRECATED_VALUE`（正是本章 §16.1 引用的那个 PR **#47217** 干的事）。
> 现行入口是 **`.training(num_steps_sampled_before_learning_starts=…)`**。
> 本书早先把它写在 `replay_buffer_config` 里，那是**旧 stack 的写法** ——
> 与本章"新 stack 是默认"的前提打架，照抄会拿到一个被忽略/报错的字段。

| 字段 | 含义 | 备注 |
|---|---|---|
| `type` | 缓冲区的类 | 三种写法等价：`ReplayBuffer` 类对象 / `"ReplayBuffer"` / 全路径字符串 |
| `capacity` | 容量，**单位由 `storage_unit` 决定** | 默认 10000；满了按 FIFO 淘汰（`ReservoirReplayBuffer` 例外） |
| `storage_unit` | 一条槽位代表什么 | `TIMESTEPS`（缓冲区默认）/ `SEQUENCES` / `EPISODES` / `FRAGMENTS`；枚举和字符串（`"episodes"`）都能传，写错会抛 `ValueError` |
| ~~`learning_starts`~~ | **旧 stack 字段，已弃用** | 新写法：`.training(num_steps_sampled_before_learning_starts=…)`。太小 → 早期全是高度相关的数据；太大 → 浪费时间 |

`storage_unit` 的语义比名字更细：**它同时决定"怎么存"和"采样时 `num_items` 的
单位"**。其中 `EPISODES` 只接受**完整** Episode（从 T=0 开始、以
terminated / truncated 结束），半截的会被丢掉 —— 想"按整条轨迹采样"就用它。

**三条实践提醒**：

1. **RNN 类算法必须用 `SEQUENCES`**。`RNNSACConfig` 自己就把 `storage_unit` 设成
   `sequences` 并配上 `replay_burn_in`；`replay_sequence_length` 由 RLlib 从
   `max_seq_len + replay_burn_in` 算出，**不要手设**。
2. **多智能体用 `MultiAgentReplayBuffer`（或它的 prioritized 变体）**，因为
   `add()` / `sample()` 要带 `policy_id`。
3. ⚠️ **新 stack 下要小心照抄旧例子**：官方文档里不少 replay buffer 的具体示例
   （PER、R2D2 等）仍然显式写着 `enable_env_runner_and_connector_v2=False`，也就是
   **跑在旧 stack 上**。抄之前先看那个例子有没有这行。新 stack 下
   `replay_buffer_config` 支持哪些字段**未确认**，以 `build_algo()` 的校验报错为准
   （配错通常会在构建时直接抛）。

> `capacity` 不是越大越好：它和 batch 大小一起被用来估算内存，**容量超过可用
> 系统内存会直接抛 `ValueError`**。

## 16.20 离线 RL：`offline_data()` 与 OPE

§16.6 的表里 BC / CQL / MARWIL 都标着"离线算法"，但整套离线数据接口在本文里是
空白的。补上 —— 注意**方法名不是 `config.offline()`**，而是 `offline_data()`。

```python
from ray.rllib.algorithms.cql import CQLConfig

config = (
    CQLConfig()
    .environment("Pendulum-v1")            # 离线训练也要环境，用于评估与空间推断
    .offline_data(
        input_="s3://bucket/expert_data/",  # 目录 / 文件 / 文件列表都能给
        input_read_episodes=True,           # 读 Episode 格式（不是旧 SampleBatch）
        input_read_batch_size=512,
        dataset_num_iters_per_learner=1,    # 每个 learner 每次更新消费几批
    )
    .training(bc_iters=1000)                # CQL：先 1000 步 BC 预热，再上 Q 学习
    .evaluation(off_policy_estimation_methods={...})   # OPE 走这里
)
algo = config.build_algo()
```

| 关注点 | 接口 | 说明 |
|---|---|---|
| 数据从哪来 | `input_` + `input_config` | 底层是 **Ray Data**，`read_parquet` / `read_json` 都能用，也支持 `s3://` 这类对象存储 |
| 数据格式 | `input_read_episodes=True` | 读 RLlib 的 Episode 格式；旧 `SampleBatch` 数据用 `input_read_sample_batches=True` |
| 外部表格数据 | `input_read_schema` | 把列名映射到 RLlib 的 `Columns`（自有数据集的常见需求） |
| 每个 learner 吃多少 | `dataset_num_iters_per_learner` | 多 learner 时决定吞吐 |
| 录数据 / 写数据 | `output=` / `output_write_episodes=True` | 把线上轨迹录成离线数据集 |
| 离线评估（OPE） | `evaluation(off_policy_estimation_methods=...)` | ⚠️ `offline_data(input_evaluation=...)` **已弃用**（会报错），旧写法要迁过来 |

**四条必须知道的边界**：

1. **新 stack 下 BC / MARWIL / CQL 默认已开启**（与 §16.1 的时间线一致）；CQL 的
   新 stack 实现（`CQLLearner` / `CQLTorchLearner`、twin-Q、connector 管线）
   已经落地。
2. **多智能体的离线 RL 不支持** —— 官方明确的边界，别尝试绕。
3. **CQL 与 BC 的主要差别就是 `bc_iters`**（先做多少步 BC 预热）；官方示例正是
   拿 BC 当 baseline 对照 CQL。
4. **有状态模型的支持不齐**：BC / MARWIL 支持在离线数据上训练有状态模型，
   **CQL / IQL 到本次检索时仍未支持**。要用 RNN + CQL，先确认版本。

> ⚠️ 官方文档明确写了 **"训练期间跑 OPE 不推荐"**（Running OPE during training
> is not recommended）—— OPE 该当离线评估工具用，而不是训练循环里的实时指标。

## 16.21 本章小结

* **RLlib 没被弃用，但在迁移中**：新 API stack 已是所有算法的默认（2.40+），
  回退旧 stack 要显式写 `config.api_stack(...)`；v2 stack 在 Ray Summit 2025
  宣布 GA。类替换关系：`RLModule` ← Policy/ModelV2、`Learner` ← 部分 Policy、
  `EnvRunner` ← RolloutWorker、`ConnectorV2` ← Connector、`SingleAgentEpisode`/
  `MultiAgentEpisode` ← Episode 系列，而 `AlgorithmConfig` / `Algorithm` 保留。
* **只支持 PyTorch**；`exploration_config`、`num_gpus`、`train_batch_size`、
  `create_env_on_local_worker`、`enable_connectors` 等已弃用，替代品分别是
  `env_runners(explore=)`、`learners(num_gpus_per_learner=)`、
  `training(train_batch_size_per_learner=)`。现代最小写法：
  `PPOConfig().environment(...).env_runners(...).learners(...).training(...)`
  → `build_algo()` → `train()` / `evaluate()`。
* **`local_mode` 在 Ray Core 层被移除**（PR #60647，un-revert），影响面同时
  覆盖 RLlib 与 Tune 的代码路径；官方表明没有替代品。它与 mini-ray 里自己
  实现的 `local_mode` 是**两件独立的事**。
* **2026 年的 RL 生态里，Ray 是编排层而不是算法层**：verl / OpenRLHF /
  SkyRL / NeMo-RL / slime / miles 都构建在 Ray 上（HF 的 16 库调查里 8 个用
  Ray），但算法与 kernel 是这些库自己的；也存在 Tunix / TorchForge /
  PipelineRL 这样的非 Ray 路线。选型一句话：**LLM/VLM 后训练 → verl 生态；
  经典 RL / 离线 RL → RLlib。**
* **agentic RL 已经从"中间地带"长成独立一档**（优化目标从"一次回答好不好"
  变成"一串动作好不好"，rollout 要跑完整个 Agent 环境），玩家包括
  verl / SkyRL / OpenRLHF / **ART** / **Agent Lightning** / **RAGEN**。
  框架清单与四条选型判据见 **第 38 章 §38.10**（也与第 19 章 §19.8 一致）。
* **GRPO 成为默认**的原因是 critic-free + 组内相对优势 + 与 RLVR 天然配对；
  主要缺陷是**奖励全同时优势塌缩**。关于"护城河转移到 reward 与 eval 层"
  的说法是**社区观点**，本章标注为待观察；"省一半显存带宽"这类具体数字
  同样标注为**未确认**。
* **多智能体**用 `.multi_agent(policies=, policy_mapping_fn=, policies_to_train=)`
  加 `.rl_module(rl_module_spec=MultiRLModuleSpec(...))`；新 stack 的
  `policy_mapping_fn(agent_id, episode, **kwargs)` **没有 `worker` 参数**，且
  `add_policy()` 不可用、要改成 `add_module()`。它面向**经典多智能体环境**，
  不覆盖 LLM 后训练里的多轮 / 多角色场景。
* **`LearnerGroup` 是新 stack 的架构核心**：采样（EnvRunner）与训练（Learner）
  解耦，可独立伸缩。`update(batch=, timesteps=, async_update=)` 返回多个 learner
  的结果；**`get_weights` / `set_weights` 只管网络，`get_state` / `set_state`
  才含 optimizer 状态**（续训必须用后者），且 `Algorithm.set_weights()` 会自动
  同步到所有 EnvRunner。
* **`EnvRunner.sample()` 返回 Episode 列表而不是 batch**（旧 `RolloutWorker` 返回
  `SampleBatch`），`ConnectorV2` 分成 env-to-module / module-to-env / learner
  **三段管线**，且采样侧与训练侧必须对齐（对不齐不报错、只是学不好）。
  `RLModule` 重写 `_forward_inference` / `_forward_exploration` /
  `_forward_train`，调用不带下划线的公开版本。
* **检查点走 Checkpointable API**：`save_to_path()` / `restore_from_path()` /
  `Algorithm.from_checkpoint()`，配 `get_module()` 可脱离训练组件拿去部署
  （旧写法 `get_policy()` 在新 stack 下会报错）。off-policy 算法要显式关注
  **`replay_buffer_config`**（`capacity` / `storage_unit`），
  而开始训练的阈值在新 stack 下已经搬到了
  `.training(num_steps_sampled_before_learning_starts=…)`
  （旧名 `learning_starts` 已弃用），
  离线算法走 **`offline_data()`**（**不是** `config.offline()`），OPE 走
  `evaluation(off_policy_estimation_methods=...)`。

下一章换到工程侧：怎么把上面这些东西真的跑在生产集群上，
以及 Ray 那个"默认不鉴权"的短板到底该怎么补。
