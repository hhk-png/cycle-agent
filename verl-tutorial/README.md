# verl 深度教程与 MiniVerl 工程实现

> 从 HybridFlow 的单控制器、FSDP/Megatron、vLLM/SGLang rollout、PPO/GRPO/DAPO/CISPO 算法，到标量奖励模型与过程奖励模型（PRM）/RLAIF、推理模型 RL 配方、工具/沙箱、前沿模型接入（DeepSeek V4、量化权重同步、路由回放）、rollout 引擎内部与容量规划、异步 Agent RL、商业分析与一个可运行的迷你实现。

本教程以 **verl v0.9.1**（2026 年 11 月）为事实基线。verl 是字节跳动 Seed 团队发起、现由 `verl-project` 社区维护的 LLM RL 训练库，对应 EuroSys 2025 论文 *HybridFlow: A Flexible and Efficient RLHF Framework*。

教程有两条并行主线：

1. **把 verl 讲透**：不是“怎么调 API”，而是“为什么这样设计、代价是什么、什么时候会失效”。所有对真实 verl 的描述尽量落到仓库路径与可核对的行为上。
2. **给一个能跑的 MiniVerl**：`mini_verl/` 是一个只依赖 NumPy + PyYAML 的教学实现，包含自己的自动微分、TinyTransformer、DataProto、WorkerGroup、FSDP/TP/PP/MoE 模拟、LoRA、PPO/GRPO/DAPO/CISPO、rollout、checkpoint、独立 pass@k 评测、AgentLoop、异步 replay buffer，一个可训练、可校准、可接入 PPO 的标量奖励模型，以及一个逐步打分的 PRM（可与 verifier 组合）。

## 验证状态

| 项目 | 结果 |
|---|---|
| 单元 / 集成测试 | **250 passed** |
| 端到端示例 | **26 / 26 passed** |
| 章节 | **45 章** |
| 附录 | **26 篇** |
| 文档自检 | `check_docs.py` 全部通过 |

本机验证命令（Windows Git Bash / Linux / macOS 均可）：

```bash
cd verl-tutorial
python -m pytest tests/ -q
python -X utf8 examples/run_all.py
python -X utf8 check_docs.py
```

一次跑完：

```bash
python -X utf8 scripts/verify_all.py
```

完整环境、逐项输出与限制见 [VERIFICATION.md](VERIFICATION.md)。

在 Windows 上 `-X utf8` 不是可选项：控制台默认编码可能不是 UTF-8，而输出里有中文。

## 本版更新

### 2026-11 七次修订

- **MiniVerl 新增过程奖励模型（PRM）**：`mini_verl/prm.py` 实现步骤数据契约（`local`/`suffix` 标签、前缀构造、反例分布）、稳定逐步 BCE（`softplus(z)-y*z`）、六种聚合（`last/first/min/max/mean/product`）、检查点往返，以及 `ProcessRewardManager` 的边界校验、逐步打分、scatter、overlong shaping 与 `outcome_reward_fn` verifier 组合。训练入口 `mini_verl/trainer/main_prm.py`，配置 `configs/prm_bandit.yaml`（bandit）与 `configs/prm_copy.yaml`（copy），示例 `examples/26_process_reward.py`，测试 `tests/test_prm.py`（13 个）。
- **PPO 接入 PRM**：`reward.prm.enable=true` + `model_path` 构造 `ProcessRewardManager`；缺路径早失败。支持 `prm_weight`/`outcome_weight`，避免 PRM-only 被策略 reward hacking；`evaluate()` 仍走 `without_shaping()`。
- **章节补强**：第 17 章新增 17.17（PRM 数据/损失/聚合/服务/RL 接法/边界）；第 19 章新增 19.12（步骤级 GAE 与过程奖励）；第 26 章新增 26.16（Agent 的 turn 级过程奖励与工具反馈）；17.12 补上步骤切分、两类标签（过去正确性 vs 未来可解性）与六种聚合。
- **附录同步**：K 重写 K.7（PRM 已落地）、新增 K.9（PRM 评估六维）；A 新增 A.13（PRM 配置/API）；G 新增 G.31；B/F/X 补术语、Lab 19 与 PRM 评估；S 修正 PRIME 的章节交叉引用。
- **事实校正**：PRM 不再被写成标量 ORM 的一种训练方式（17.3）；GenRM 的支持范围补上“必须提供 `custom_reward_function`”（17.12）；MiniVerl 代码规模从约 9700 行更新到约 1.08 万行。
- **计数更新**：测试 237 → 250，示例 25 → 26。

### 2026-11 六次修订

- **MiniVerl 新增标量奖励模型（RM）**：`mini_verl/reward_model.py` 实现偏好数据契约、Bradley-Terry 稳定损失（`log_sigmoid`/`softplus`）、margin/label smoothing、pairwise accuracy 与 ECE、MLP/线性/Transformer 三种 backbone、检查点往返，以及可直接替换规则奖励的 `ModelRewardManager`。训练入口 `mini_verl/trainer/main_rm.py`，配置 `configs/rm_bandit.yaml`，示例 `examples/25_reward_model.py`，测试 `tests/test_reward_model.py`（16 个）。
- **PPO 接入模型奖励**：`reward.reward_model.enable=true` + `model_path` 会构造 `ModelRewardManager`；`evaluate()` 使用 `without_shaping()`，避免把训练期 overlong 惩罚带进独立评测。缺 `model_path` 时在 `init()` 提前失败。
- **新增附录 Z（rollout 引擎内部与容量规划）**：continuous batching 的三预算、PagedAttention 块账、hash block vs radix tree、chunked prefill 的 TTFT/ITL、CUDA graph、投机解码的无损性与 RL log-prob 陷阱、KV→并发→吞吐→成本的五步估算，以及 v0.9.1 配置映射。
- **文档同步**：第 17.16 节讲 MiniVerl RM 的完整链路；附录 X.11 从“扩展练习”改写为已实现能力的边界；第 34 章覆盖矩阵、第 39 章测试清单、第 40 章迁移差距表、第 41 章扩展路线同步更新。
- **计数更新**：测试 221 → 237，示例 24 → 25，附录 25 → 26。

### 2026-11 五次修订

- **新增附录 X（奖励模型、偏好数据与 RLAIF）**：补齐第 17 章只讲了一段的 RM 链路——偏好数据契约、Bradley-Terry 与稳定 loss、判别式/生成式 RM 与 PRM、训练与校准、pairwise accuracy/ECE/对抗探针、reward overoptimization、Constitutional AI/RLAIF，以及 verl 的 `reward.reward_model` 是推理开关而非训练开关这一常见误解。
- **新增附录 Y（推理模型 RL 实战手册）**：把第 3、22、25 章散落的工程决策收束为可执行手册——pass@k 难度校准、有效组概率、verifier 设计、GRPO→DAPO→GSPO/CISPO 的逐组件消融、长度爆炸与熵坍缩、稳定性指标、评估口径与算力预算。
- **第 33 章新增 33.12**：框架基准的四个受控变量、六类指标、可复现基准协议，以及 TRL/OpenRLHF → verl 的迁移检查清单；强调 `train_batch_size`/`gen_batch_size`/`ppo_mini_batch_size` 不是同一件事。
- **交叉链接**：第 3、17、28、41 章补上 X/Y 的入口；附录 A 新增 A.12 奖励模型配置速查，B 补 RM/RLAIF 术语，E 补 InstructGPT、Constitutional AI、RLAIF、reward overoptimization、DeepSeek-R1。
- **事实校正**：DAPO 的 overlong shaping 在 v0.9.1 的 V1 reward loop 里由 `reward.reward_kwargs.overlong_buffer_cfg` 读取；旧文档的 `reward_model.overlong_buffer` 不是主线配置键。
- **计数更新**：附录 23 → 25；测试与示例数不变（221 / 24）。

### 2026-11 四次修订

- **新增四篇附录**：T 工具系统、函数调用与沙箱（BaseTool 生命周期、FunctionTool、注册入口、sandbox fusion、安全）；U 模型接入与权重加载（Megatron-Bridge 默认路径与 legacy registry、HF→Megatron 权重映射、Automodel/TorchTitan/VeOmni 后端、新模型检查清单）；V DeepSeek V4、量化权重同步与路由回放（FP8/FP4、MXFP4、prepare→buckets→finalize、R2/R3、hash router、验证协议）；W 资源分离、检查点引擎与弹性部署（DetachActorWorker、checkpoint engine 后端、delta 同步、成本模型）。
- **MiniVerl 新增工具层**：`mini_verl/tools.py` 实现 `ToolSchema`/`ToolResult`/`BaseTool`/`FunctionTool`/`@function_tool`/`ToolRegistry`，支持每轨迹实例、schema 推断、异常与超时封装、重名拒绝；`agent.py` 复用它并保持旧 API。新增 `examples/23_tool_registry.py` 与 `tests/test_tools_registry.py`。
- **MiniVerl 新增路由回放**：`moe_route` 支持 `replay_top_idx`/`replay_mask`；新增 `record_moe_routes`、`replay_moe_routes`（层数不一致直接失败）与 `router_replay_report`（定位首个错层与错 token）。新增 `examples/24_router_replay.py` 与 `tests/test_router_replay.py`。
- **章节补强**：第 5 章新增 5.15（models/tools/checkpoint_engine/separation 四条边界）；第 15 章新增 15.13（分离式 rollout）；第 16 章新增 16.13（router replay 与层序陷阱）；第 26 章新增 26.15（ToolRegistry 生命周期）；第 30 章新增 30.14（Muon 优化器）；第 41 章新增 41.15（DeepSeek V4 的模型-系统协同）。
- **计数更新**：测试 190 → 221，示例 22 → 24，附录 19 → 23。

### 2026-11 三次修订

- **新增附录 S（源码索引）**：把 v0.9.1 的入口、协议、引擎、rollout、算法注册表、配置字段、文档与 grep 定位方法整理成一张“从名字到调用链”的索引。
- **新增示例 22（全生命周期）**：训练 → checkpoint → 续训 → 独立评测 → safetensors 导出，验证 MiniVerl 的运维契约；示例数 21 → 22。
- **算法篇补全**：第 3 章新增 3.13（14 个优势估计器的全景、GDPO/OPO/GPG/OTB/Pass@k 的公式与依赖）；第 25 章新增 25.14（12 个策略损失模式的真实语义、配置字段与误用），并修正 DRO 的命名与 `reinforce` 的注册状态。
- **主循环与 MiniVerl 深化**：第 21 章新增 21.11（`parameter_sync_step` 的拆步语义、LR horizon 与指标聚合）；第 34 章新增覆盖矩阵与阅读顺序；第 35～37 章新增 `Tensor` 生命周期、数值稳定、DataProto 不变量、五种 dispatch、AdamW/显存/TP/PP/delta 的代码级推导。
- **MiniVerl 语义对齐**：GAE 与 REINFORCE++ 在 mask=0 的 observation 位置改为**冻结** `lastgaelam`/`running_return`（与 verl 源码一致，不再乘 γ/γλ）；对应测试更名并重算期望值。
- **第二轮代码审计修复**（测试 179 → 190）：GSPO/CISPO 接上 IS 权重；sync 模式也执行 rollout correction；`fit()` 结束与评测前强制同步权重；staleness 改按 actor 更新计数并在阈值 0 时补采；cosine/warmup 的 LR horizon 接线；评测剥离 DAPO shaping；数学 prompt 保留题头；DPO 改为 sum log-prob 并压缩 mask 空洞；蒸馏按全局 token 归一；resume 恢复 rollout RNG；softplus 去掉会截断梯度的 clip。回归测试见 `tests/test_audit_regressions.py`。
- **事实校正**：DAPO 在 V1 下的补采方式（`max_num_gen_batches` 被忽略、`gen_batch_size` 强制为 1）与 overlong buffer 公式；`main_eval.py`/`main_generation_server.py` 的真实职责；真实 verl 的吞吐/异步指标名；`rollout_correction` 的级别+阈值字段；671B ETP=1 显存账；checkpoint 的 `hf_model` 与 `resume_mode=resume_path`；SFT/蒸馏配置树；选型评分卡算术；附录中的 `AbstractRewardManager`、CPU extra、`delta_sharded`、Slurm 脚本与 CUDA 版本。
- **商业篇收束**：第 45 章新增 45.13（一页纸商业备忘录：模板、算例、里程碑、停止条件与五个常见谬误）。

### 2026-11 二次修订

- **MiniVerl 新增 CISPO**：`loss_mode=cispo` 走通配置 → worker → loss → 核心公式；裁剪后的重要性权重被 `detach`，梯度保留在 `log_prob` 上；新增 2 个公式/梯度测试、1 个端到端训练测试与 `examples/20_cispo_training.py`。测试 174 → 179，示例 19 → 21。
- **MiniVerl 新增独立评测入口**：`mini_verl/trainer/main_eval.py` 从 checkpoint 读取配置、强制 naive reward、按题分组计算 pass@k；`PPOTrainer.evaluate` 与训练内 `validate` 分离；新增 checkpoint 重载测试与 `examples/21_standalone_eval.py`。
- **新增/加深章节**：第 25 章新增 25.13（GPG/SAPO/DRO/DPPO/OPD/OTB/SPIN 索引与 CISPO 从公式到测试）；第 24 章新增 24.13（独立评测与生成服务器契约）；第 16 章新增 16.12（rollout KV cache offload 的成本模型与失效语义）；第 30 章补 NVFP4 QAT；第 38 章新增 38.13（CISPO 与独立评测的代码路径）；附录 I 新增 I.12（平台抽象、AMD ROCm 与 Ascend NPU 移植清单）。
- **修正**：第 3 章 3.2 标题笔误（`ver` → `verl`）。

### 2026-11 修订

- **新增三篇附录**：P 二次开发（自定义 reward、AgentLoop、dataset、model、rollout、engine、algorithm，以及测试/CI/贡献流程）、Q 高级特性（attention backend、RoPE、MTP、dynamic CP、FSDP Turbo、Megatron Lite、skip manager、determinism、rollout trace/Prometheus）、R 项目交付与实验复盘（30/60/90 天节奏、数据/奖励/评测、manifest、失败复盘、上线与回滚）。
- **补强章节**：第 1 章新增符号/术语/引用约定并补全阅读路线；第 6 章新增环境可复现与 smoke test；第 8 章新增数据引擎（合成、去重、难度分层、污染控制、版本化）；第 18 章新增 PPO 稳定性联合诊断；第 24 章新增实验 manifest 模板；第 25 章新增 recipe 工程；第 31 章新增故障树；第 32 章新增 profiling 实操；第 40 章新增迁移验收与灰度；第 41 章新增跟踪 verl 演进的方法；第 42～45 章补 TAM/SAM/SOM、敏感性分析、选型评分卡与 90 天落地计划。
- **正确性修复**：`PDDisaggregatedRollout` 的 KV 传输时间现在按 Gbps=bit/s 换算（`bytes*8/bandwidth`），并加了手算断言；第 11 章 FSDP 显存表改为“DDP 每卡 / FSDP 每卡”口径；第 30 章 671B 显存账显式区分 ETP=TP 与 ETP=1；第 23 章修正 resume 游标表述；第 6/8/10 章统一 tokenization 与 `response_mask` 的归属；第 2/19 章修正 mask 与 GAE 递推的措辞；第 6 章补上 `use_kl_in_reward=true`；第 3 章用无偏 std 重算 GRPO 数值例；第 15 章区分 PD 吞吐与单请求延迟；第 27 章统一 staleness 阈值口径；第 32 章区分训练-only 与端到端 MFU。
- **MiniVerl 审计回归修复**（新增 `tests/test_regressions.py`，测试 166 → 174）：DPO 跨 world size 的全局 pair 归一；reward 异常不再从 positional fallback 逃逸、RewardLoop 增加 timeout；rollout correction 默认不做均值归一（均匀策略偏移不再被抵消）；GSPO 强制 seq-mean-token-mean；`delta_sharded` 字节不超过全量；LoRA 字符串 target 不再被拆成字符；权重更新后 prefix cache 失效；`max_to_keep=0` 不再删掉刚写的 checkpoint；新增 `actor.policy_loss.loss_mode` 并接入 GSPO/DAPO；async refill 移到权重同步前，staleness 不再恒为 0；balance_batch 容量按 padded chunk 计算，并明确它只服务 full-batch RPC。
- **修正数字与表述**：第 3 章测试用例数（28 → 34，其中约 28 个函数 + 6 个参数化用例）、第 30 章 LoRA 测试数（9 → 10）、第 3 章方差随组大小变化的措辞、第 8 章 parquet 组织方式的表述。
- **扩展附录**：G 补全 `parallel.py`、`rewards.py`、`transfer_queue.py`、`PDDisaggregatedRollout` 的代码导读与测试映射；E 修正 SPPO 链接并补文档；I 修正 671B 并行约束与 32B 配方口径；A 修正 `trainer.v1.trainer_mode` 等字段映射；D 修正 log-prob 对比代码；N/O 修正路径与配置；B 补 RLVR/CISPO/VAPO 等术语；F 替换已实现的 RLOO 实验。

### 上一版更新

- **MiniVerl 新增 PD 分离与 TransferQueue**：`PDDisaggregatedRollout` 统计 prefill token、KV 传输字节与传输时间；`transfer_queue.py` 实现有界、版本化、可快照的生产/消费队列；新增 `examples/18_pd_disaggregation.py`、`examples/19_transfer_queue.py` 与 `tests/test_transfer_queue.py`。
- **断点续训补全优化器状态**：checkpoint 现在保存/恢复 AdamW 的 m/v 与 step count；resume 不再把 actor 覆盖到 ref（KL 不会塌成 0）。
- **正确性修复**：entropy bonus 的符号与 mask 对齐；DP padding 重复样本的 mask/loss_mask；LoRA 冻结基座在 weight decay 下不被移动；GAE/REINFORCE++ 在内部 mask 上的折扣、截断 bootstrap；rollout 记录行为策略（temperature/top-k/top-p）log-prob；ReMax 使用独立 greedy rollout 基线；DAPO overlong 只惩罚真正 truncated 的样本；TP gather 对等值/不可整除分片的处理；delta 同步真正只应用变化 tensor；resume_from_path 与训练游标/epoch/KL 系数恢复；SFT 全局 token 归一；DPO prompt padding 对齐与 loss_mask；DAPO 全过滤时跳过更新；rollout correction 的截断比例分母。
- **新增三篇附录**：M 引擎后端与扩展（BaseEngine/ShardSpec/EngineRegistry、六种后端、新增后端清单），N 安全、沙箱与多租户，O 端到端案例研究（数学 RLVR、代码 Agent、多模态 RL）。
- **加深核心章节**：第 14 章 delta 同步工程；第 15 章 PD 分离成本模型；第 16 章 prefix grouper；第 17 章 reward loop 协议；第 20 章 resume 时 ref 的语义；第 23 章优化器/scheduler/异步保存；第 27 章 TransferQueue 与动态调度；第 28 章 teacher loop/on-policy 蒸馏；第 30 章 QAT/激活卸载/FP8；第 33 章补齐 AReaL/SkyRL/ROLL 对比；第 41 章 PD/数据面/弹性。
- **事实校正**：修正无效的 YAML 根键、`adv_estimator=dapo/gspo` 的误述、Megatron 并行约束、FSDP 显存口径、视觉 token 计算、Slurm 数组、`balance_batch` 的 LPT 语义、`no_sync` 的作用范围等。
- **CLI 修复**：`python -m mini_verl.trainer.main_ppo trainer.total_training_steps=30` 这类“第一个位置参数就是 override”的写法现在可以正常工作；`load_config` 会把裸 `key=value` 识别为 override，并补了回归测试。

## 5 分钟启动 MiniVerl

要求 Python 3.10+、NumPy、PyYAML、pytest：

```bash
python -m pip install -r requirements.txt

# 看配置
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run

# 30 步 GRPO（bandit 任务，CPU 可跑）
python -X utf8 -m mini_verl.trainer.main_ppo \
  trainer.total_training_steps=30 \
  trainer.val_before_train=true

# 跑一个完整示例
python -X utf8 examples/01_quickstart.py

# 训练一个标量奖励模型（偏好对 -> Bradley-Terry -> 校准/检查点）
python -X utf8 -m mini_verl.trainer.main_rm configs/rm_bandit.yaml

# 用刚训练的 RM 作为 PPO 的唯一奖励
python -X utf8 -m mini_verl.trainer.main_ppo \
  reward.reward_model.enable=true \
  reward.reward_model.model_path=outputs/reward_model/bandit \
  data.max_prompt_length=16 \
  data.max_response_length=8 \
  trainer.total_training_steps=40

# 训练一个过程奖励模型（bandit：动作 + EOS 两步）
python -X utf8 -m mini_verl.trainer.main_prm configs/prm_bandit.yaml

# 用 PRM 做密集 shaping，并与 verifier 组合
python -X utf8 -m mini_verl.trainer.main_ppo \
  reward.prm.enable=true \
  reward.prm.model_path=outputs/prm/bandit \
  reward.prm.outcome_reward_fn=bandit \
  reward.prm.outcome_weight=1.0 \
  reward.prm.prm_weight=0.1 \
  trainer.total_training_steps=60
```

CLI 的预期输出是逐 step 日志 + 最终 history 字典：

```text
[step 0001] data/reward_mean=0.1875, ... actor/loss=-0.1876
...
steps=30
{'actor/loss': ..., 'data/accuracy': ..., 'perf/tokens_per_s': ..., ...}
```

想要人类可读的总结，跑 `examples/01_quickstart.py`：

```text
steps=30
final training reward=1.000
validation accuracy=1.000
expected: >= 0.75 once the policy has separated the four contexts
```

MiniVerl 的默认任务是四动作 contextual bandit：策略在 30～60 步内可以从 25% 学到 100%。它不是语言模型能力的展示，而是把 RL 训练的系统链路跑通。

## MiniVerl 不是什么

- 不是生产框架：没有 GPU、没有 Ray、没有 NCCL、没有 vLLM/SGLang。
- 不是生产沙箱：工具层在进程内运行，`FunctionTool` 的超时是协作式的，不能杀死后台线程。
- 不是真实 MoE：路由回放只模拟记录/重放与层序校验，不覆盖 FP8/FP4 kernel 或 hash router 的真实查表。
- 不是语义奖励模型：MiniVerl 的 RM 只验证偏好数据、Bradley-Terry 损失、校准、检查点与 PPO 接入契约；PRM 只验证步骤标签、前缀打分、聚合与服务契约。它们都不声称能判断写作质量或推理正确性，也不替代真实预训练 RM/PRM。
- 不是模型能力展示：TinyTransformer/线性策略都很小。
- 不是 verl 的替代品：它是“可运行的架构说明书”。

它**是**：

- 一个能单步调试的 HybridFlow；
- 一套与真实 verl 对齐的数据/控制/训练契约；
- 两条奖励链路：偏好对 → Bradley-Terry → PPO 的 RM，以及步骤标签 → 前缀打分 → 聚合 → PPO 的 PRM；
- 一个 250 个测试的验证基线；
- 一个可以在 CPU 上十分钟内读完的实现。

## 架构图

```text
                    ┌──────────────────────────────────────┐
                    │           PPOTrainer (controller)     │
                    │  sample -> reward(rule/RM/PRM) -> balance │
                    │  old/ref logp -> values -> advantage  │
                    │  -> critic -> actor -> sync           │
                    └───────┬───────────────┬──────────────┘
                            │ DataProto     │ RPC / dispatch
              ┌─────────────▼──────┐  ┌─────▼──────────────┐
              │ ActorRolloutRef    │  │ CriticWorker       │
              │ WorkerGroup        │  │ WorkerGroup        │
              │  actor engine      │  │  value engine      │
              │  ref engine        │  │                    │
              │  rollout           │  │                    │
              └───────┬────────────┘  └────────────────────┘
                      │ weights
              ┌───────▼────────────┐
              │ CheckpointEngine   │
              │ naive/nccl/delta   │
              └───────┬────────────┘
                      │
              ┌───────▼────────────┐
              │ Rollout engines    │
              │ vLLM/SGLang/Naive  │
              └────────────────────┘
```

## 推荐阅读路线

| 目标 | 主线 | 配套 |
|---|---|---|
| 先跑起来 | 第 6 章 → 第 34～39 章 | 附录 G（代码导读） |
| 搞懂 RL 算法 | 第 2、3、18～25 章 | 附录 C（推导）、附录 K（PRM/搜索）、附录 Y（推理 RL 配方） |
| 做奖励/偏好数据 | 第 17、28 章 | 附录 X（RM/RLAIF）、第 17.16 节（MiniVerl RM）、第 17.17 节（MiniVerl PRM）、附录 K（过程奖励）、附录 L（评测） |
| 做系统/性能 | 第 4、9～15、21～23、31～32 章 | 附录 I（硬件）、J（部署）、Q（高级特性）、Z（引擎内部与容量规划） |
| 做 Agent/多模态 | 第 26～30 章 | 附录 N（沙箱）、O（案例） |
| 做平台/二次开发 | 第 25、31、33 章 | 附录 M（后端）、P（二次开发）、S（源码索引）、T（工具）、U（模型接入）、W（分离部署） |
| 做前沿模型/量化 | 第 14、16、30 章 | 附录 U（模型接入）、V（DeepSeek V4/路由回放） |
| 做交付/商业 | 第 41～45 章 | 附录 R（交付手册）、L（评测） |

每条路线都不要求按顺序读完；但“先跑起来”和“搞懂算法”建议至少各走一遍，否则很难判断问题是实现错还是算法不收敛。

## 教程地图

### 第一部分：基础与全景

1. [为什么需要 verl：LLM 后训练的系统问题](chapters/01-why-verl.md)
2. [RL 后训练的数学基础：从策略梯度到 GAE](chapters/02-rl-math.md)
3. [GRPO 家族与推理模型训练范式](chapters/03-grpo-family.md)
4. [HybridFlow：单控制器 + 多进程 worker 的编程模型](chapters/04-hybridflow.md)
5. [verl v0.9.1 仓库地图](chapters/05-repo-map.md)
6. [安装、硬件与第一次 PPO/GRPO](chapters/06-install-quickstart.md)
7. [Hydra 配置系统与字段地图](chapters/07-config.md)
8. [数据准备：parquet、RLHFDataset 与样本契约](chapters/08-data.md)

### 第二部分：协议与分布式

9. [DataProto 与 TensorDict：批数据的契约](chapters/09-dataproto.md)
10. [单控制器、WorkerGroup 与 Ray 执行模型](chapters/10-controller-ray.md)
11. [FSDP/FSDP2 引擎：参数、梯度与优化器分片](chapters/11-fsdp.md)
12. [Megatron 引擎：TP/PP/CP/EP 与大模型训练](chapters/12-megatron.md)
13. [序列并行、remove padding 与变长批](chapters/13-padding-sequence-parallel.md)
14. [权重同步与 3D-HybridEngine](chapters/14-weight-sync.md)

### 第三部分：Rollout 与训练循环

15. [Rollout 引擎：vLLM、SGLang、HF 与 server 模式](chapters/15-rollout-engines.md)
16. [采样策略、n 采样与前缀缓存](chapters/16-sampling.md)
17. [Reward：规则奖励、模型奖励与 reward loop](chapters/17-reward.md)
18. [Actor 更新：PPO loss、KL、entropy 与 loss aggregation](chapters/18-actor-update.md)
19. [Critic、GAE 与 value 训练](chapters/19-critic-gae.md)
20. [参考模型与 KL 控制](chapters/20-ref-kl.md)
21. [主循环逐行：从 prompt 到一次参数更新](chapters/21-main-loop.md)
22. [动态批、长度均衡、DAPO 过滤与过采样](chapters/22-dynamic-batch-dapo.md)
23. [Checkpoint、断点续训、模型合并与 HF 导出](chapters/23-checkpoint-merge.md)
24. [验证、指标、日志与实验管理](chapters/24-validation-metrics.md)

### 第四部分：算法与进阶

25. [GRPO/DAPO/GSPO/Dr.GRPO/CISPO 的实现差异](chapters/25-algorithm-zoo.md)
26. [多轮对话、工具调用与 AgentLoop](chapters/26-agent-loop.md)
27. [异步 RL：one-step off-policy 与 fully async](chapters/27-async-rl.md)
28. [蒸馏、DPO、SFT 与偏好优化](chapters/28-sft-dpo-distill.md)
29. [多模态、VLM、Omni 与扩散模型](chapters/29-multimodal.md)
30. [LoRA、量化、MoE 与大模型训练](chapters/30-lora-quant-moe.md)
31. [容错、弹性、可观测性与故障排查](chapters/31-fault-tolerance.md)
32. [性能工程：吞吐、MFU、显存与调参方法论](chapters/32-performance-tuning.md)
33. [与其他框架对比：TRL、OpenRLHF、NeMo-RL、Unsloth、AReaL 与托管服务](chapters/33-framework-comparison.md)

### 第五部分：MiniVerl

34. [MiniVerl 设计：范围、确定性与映射](chapters/34-miniverl-design.md)
35. [自动微分与 TinyTransformer](chapters/35-miniverl-autograd.md)
36. [MiniVerl 的协议、配置与单控制器](chapters/36-miniverl-protocol-controller.md)
37. [MiniVerl 的引擎、分片与并行模拟](chapters/37-miniverl-engine-parallel.md)
38. [MiniVerl 的 Rollout、权重同步与 PPO/GRPO 主循环](chapters/38-miniverl-rollout-trainer.md)
39. [测试工程：单测、差分、不变量与端到端](chapters/39-miniverl-testing.md)
40. [MiniVerl 到生产 verl 的差距与迁移](chapters/40-mini-to-production.md)

### 第六部分：生态、未来与商业

41. [verl 生态、版本演进与未来方向](chapters/41-ecosystem-future.md)
42. [商业分析 I：市场、玩家与价值链](chapters/42-business-market.md)
43. [商业分析 II：成本、单位经济与 ROI](chapters/43-business-economics.md)
44. [商业分析 III：产品形态、平台与选型](chapters/44-business-product.md)
45. [商业分析 IV：投资、创业与职业](chapters/45-business-investment-career.md)

## 附录

- [附录 A 配置与 API 速查](appendices/A-config-api.md)
- [附录 B 术语表](appendices/B-glossary.md)
- [附录 C 数学推导](appendices/C-math-derivations.md)
- [附录 D 故障排查手册](appendices/D-troubleshooting.md)
- [附录 E 阅读清单](appendices/E-reading-list.md)
- [附录 F 实验手册与自测](appendices/F-labs.md)
- [附录 G MiniVerl 代码导读](appendices/G-miniverl-code-guide.md)
- [附录 H 版本迁移：0.3 → 0.9](appendices/H-version-migration.md)
- [附录 I 硬件与集群配方](appendices/I-hardware-recipes.md)
- [附录 J 部署与集群运维](appendices/J-deployment-ops.md)
- [附录 K 过程奖励、树搜索与自我博弈](appendices/K-process-reward-search-selfplay.md)
- [附录 L 评测与基准](appendices/L-evaluation-benchmarks.md)
- [附录 M 引擎后端与扩展](appendices/M-engine-backends.md)
- [附录 N 安全、沙箱与多租户](appendices/N-security-sandbox-multitenancy.md)
- [附录 O 端到端案例研究](appendices/O-case-studies.md)
- [附录 P 二次开发：从自定义 reward 到新后端](appendices/P-secondary-development.md)
- [附录 Q 高级特性：attention、RoPE、MTP、dynamic CP 与可观测性](appendices/Q-advanced-features.md)
- [附录 R 项目交付与实验复盘手册](appendices/R-delivery-playbook.md)
- [附录 S 源码索引：从名字到调用链](appendices/S-source-index.md)
- [附录 T 工具系统、函数调用与沙箱执行](appendices/T-tools-sandbox.md)
- [附录 U 模型接入与权重加载](appendices/U-model-integration.md)
- [附录 V 前沿模型接入案例：DeepSeek V4、量化权重同步与路由回放](appendices/V-deepseek-v4-router-replay.md)
- [附录 W 资源分离、检查点引擎与弹性部署](appendices/W-separation-elastic-deployment.md)
- [附录 X 奖励模型、偏好数据与 RLAIF 工程](appendices/X-reward-model-rlaif.md)
- [附录 Y 推理模型 RL 实战手册：数据、奖励、长度与稳定性](appendices/Y-reasoning-rl-playbook.md)
- [附录 Z rollout 引擎内部：调度、分页 KV 与容量规划](appendices/Z-inference-engine-internals.md)

## MiniVerl 目录

```text
mini_verl/
├── autograd.py          # 反向模式自动微分
├── nn.py                # TinyTransformer / 线性策略
├── protocol.py          # DataProto
├── config.py            # PPOConfig
├── data.py              # tokenizer / 合成数据 / collate
├── rewards.py           # 规则奖励 / reward loop
├── reward_model.py      # 偏好数据 / Bradley-Terry / 校准 / RM 服务
├── prm.py               # 步骤标签 / 前缀 BCE / 聚合 / PRM 服务
├── losses.py            # actor/critic/sft/dpo/kl/distill
├── engine.py            # 训练引擎 / AdamW / 显存估算
├── parallel.py          # TP/PP/MoE/Ulysses
├── sharding.py          # FSDP/TP shard / checkpoint engine
├── lora.py              # LoRALinear / freeze / merge
├── rollout.py           # NaiveRollout / TPRollout / PD / async server
├── transfer_queue.py    # 有界、版本化、可快照队列
├── workers.py           # Actor/Critic workers
├── async_policy.py      # replay buffer / rollout correction
├── tools.py             # ToolSchema / BaseTool / FunctionTool / ToolRegistry
├── agent.py             # AgentLoop / calculator tool
├── checkpoint.py        # checkpoint + optimizer state / safetensors
├── metrics.py           # 指标 / pass@k / logger
├── algo/core_algos.py   # GAE/GRPO/RLOO/RF++/ReMax/KL/loss
├── single_controller/   # Worker / WorkerGroup / Dispatch
└── trainer/             # PPO / SFT / DPO / RM / PRM / 独立评测 / CLI
```

## 配置示例

- [GRPO bandit](configs/grpo_bandit.yaml)
- [PPO + GAE bandit](configs/ppo_bandit.yaml)
- [DAPO bandit](configs/dapo_bandit.yaml)
- [PRM bandit](configs/prm_bandit.yaml)
- [PRM copy](configs/prm_copy.yaml)

```bash
python -X utf8 -m mini_verl.trainer.main_ppo configs/grpo_bandit.yaml
```

## 26 个示例

| 文件 | 内容 |
|---|---|
| `examples/01_quickstart.py` | GRPO bandit 端到端 |
| `examples/02_ppo_gae.py` | PPO + critic + GAE |
| `examples/03_grpo_vs_ppo.py` | PPO/GRPO 对比 |
| `examples/04_dataproto_tour.py` | DataProto 操作 |
| `examples/05_dispatch_and_sharding.py` | dispatch / FSDP / TP |
| `examples/06_rollout_weight_sync.py` | rollout / sleep / sync |
| `examples/07_checkpoint_resume.py` | checkpoint / resume |
| `examples/08_dapo_filtering.py` | DAPO 过滤 / overlong |
| `examples/09_agent_tools.py` | 多轮工具 / mask |
| `examples/10_async_rollout.py` | 异步 buffer / staleness |
| `examples/11_sft_and_dpo.py` | SFT / DPO |
| `examples/12_memory_and_parallel.py` | 显存 / TP/PP/MoE |
| `examples/13_custom_reward.py` | 自定义 reward |
| `examples/14_benchmark.py` | 吞吐基准 |
| `examples/15_math_reward.py` | 数学奖励 / 数据 |
| `examples/16_parallel_patterns.py` | 四种并行模式 |
| `examples/17_lora.py` | LoRA 冻结、训练与合并 |
| `examples/18_pd_disaggregation.py` | PD 分离 / KV 传输账 |
| `examples/19_transfer_queue.py` | TransferQueue 部分取用与快照 |
| `examples/20_cispo_training.py` | CISPO vs vanilla：裁剪权重、保留 score-function 梯度 |
| `examples/21_standalone_eval.py` | 独立评测：checkpoint → pass@k |
| `examples/22_lifecycle.py` | 全生命周期：训练 → checkpoint → 续训 → 评测 → safetensors 导出 |
| `examples/23_tool_registry.py` | 工具 schema、生命周期、重名冲突与 AgentLoop 消费 |
| `examples/24_router_replay.py` | MoE 路由记录/重放、行级 mask、层序陷阱与损坏定位 |
| `examples/25_reward_model.py` | 偏好数据 → Bradley-Terry → 校准/检查点 → PPO 模型奖励 |
| `examples/26_process_reward.py` | 步骤标签 → 前缀 PRM → 六种聚合 → PRM+verifier PPO |

## 真实 verl 快速开始

真实 verl 需要 GPU 环境。v0.9.1 推荐 uv：

```bash
git clone https://github.com/verl-project/verl.git
cd verl

uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 examples/data_preprocess/gsm8k.py --local_save_dir ~/data/gsm8k

uv run --frozen --all-packages --extra vllm --extra fsdp \
  python3 -m verl.trainer.main_ppo \
  data.train_files=$HOME/data/gsm8k/train.parquet \
  data.val_files=$HOME/data/gsm8k/test.parquet \
  actor_rollout_ref.model.path=Qwen/Qwen2.5-0.5B-Instruct \
  actor_rollout_ref.rollout.name=vllm \
  trainer.n_gpus_per_node=1
```

完整命令见第 6 章。

## 版本与事实核对

- 真实 verl 事实以 v0.9.1 源码与官方文档为准。
- 本教程中的 `verl/...` 路径均可在 v0.9.1 仓库中找到；`check_docs.py` 会校验引用。
- MiniVerl 的行为以本仓库测试为准；文档中的测试数/示例数由 `check_docs.py` 与 pytest 收集结果对齐。
- 商业分析中的数字是**假设/量级**，不是任何公司的财务数据或报价。

## License

本教程代码用于教学；真实 verl 的许可证以其仓库为准（Apache-2.0）。
