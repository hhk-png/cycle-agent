# 验证记录

## 本版验证范围

- 新增 MiniVerl 过程奖励模型（PRM）：`mini_verl/prm.py`、`mini_verl/trainer/main_prm.py`、`configs/prm_copy.yaml`（多步 copy）、`configs/prm_bandit.yaml`（单动作 bandit / PPO）、`examples/26_process_reward.py`、`tests/test_prm.py`（13 个用例）。
- PRM 覆盖：`local`/`suffix` 步骤标签、前缀构造、稳定逐步 BCE（`softplus(z)-y*z`）、六种聚合、`PRMTrainer`（早停/检查点/step/trajectory 指标）、`ProcessRewardManager`（边界校验、逐步打分、scatter、overlong shaping、`outcome_reward_fn` verifier 组合）。
- PPO 接入 PRM：`reward.prm.enable=true` + `model_path` 构造 `ProcessRewardManager`；缺路径在 `init()` 提前失败；`evaluate()` 走 `without_shaping()`。
- 章节新增：17.17（PRM 全链路）、19.12（步骤级 GAE 与过程奖励）、26.16（Agent turn 级过程奖励与工具反馈）；17.12 补步骤切分、两类标签与六种聚合。
- 附录：K.7 改写为已落地实现、新增 K.9（PRM 评估六维）；A.13（PRM 配置/API）；G.31（PRM 代码导读）；B/F/X 补术语、Lab 19 与 PRM 评估。
- 计数：测试 237 → 250，示例 25 → 26，章节 45，附录 26。

## 本轮事实核对

- PRM 不再被写成标量 ORM 的一种训练方式（17.3）：ORM 是 `(prompt, response) -> scalar`，PRM 是 `(prompt, step_1..step_k) -> (s_1..s_k)`。
- PRM 标签区分“过去正确性”与“未来可解性”（17.12、K.2）；两者用同一个 BCE，但目标不同，不能混在同一训练集里还期待校准。
- 生成式 RM 的支持范围：判别式 RM 有原生路径；生成式 RM 必须提供 `custom_reward_function`，reward loop 不自动解析（17.12、X.7）。
- 附录 S 的 `prime.py` 章节交叉引用从「17、25」修正为「25」；第 17 章没有 PRIME 内容。
- MiniVerl 代码规模从约 9700 行更新为约 1.08 万行（`mini_verl/**/*.py`，含新增 PRM）。
- 历史事实（继续有效）：`reward_manager` V1 注册名 `rate_limited`；V1 `ReplayBuffer` 忽略 `max_num_gen_batches`；V1 `filter_groups` 的 reward metric 约束；GSPO 签名/配置默认不一致；指标名 `actor/pg_clipfrac` 等；投机解码字段位于 `model.mtp`。

## 本轮独立审计修复

对新增 PRM 做了一轮对抗性代码/文档审计，发现并修复：

1. **serving 默认值与 checkpoint 脱节**：`ProcessRewardManager` 原先把 `aggregation="min"`/`step_tokens=1` 写死为默认，配置省略时会静默使用与训练不同的切分/聚合。现在 `None` 表示从 checkpoint 元数据读取，显式配置仍可覆盖；PPO 分支也传 `None` 而不是硬编码默认。
2. **serving 测试没有约束前缀构造**：原来的假模型只看最后一个 token，无法区分“只给当前步”和“给 prompt + 前 k 步”。新增 `_ContainsTokenModel` 断言：prompt 里的 token 必须出现在每个前缀中。
3. **`outcome_reward_fn` 但 `outcome_weight=0` 静默退化**：现在构造时直接报错，不会悄悄变成 PRM-only。
4. **copy 配置的步骤数写错**：gold 答案 2 个字符 + EOS，`step_tokens=1` 是 3 步，不是 2 步；配置注释已修正，与 example 26 的 `steps=3` 一致。
5. **边界口径与空 response**：文档统一为“相对 `response_mask=1` 的有效 response token 计数”；显式 `step_boundaries` 但整行没有有效 token 时直接报错，不再静默给 0。
6. **`prm_raw_scores` 命名误导**：它其实是聚合后的轨迹分数（outcome 组合前），已改名为 `prm_trajectory_scores`；`prm_scores` 保留为同义字段。

另外补充文档：`normalize=zscore/minmax` 是 batch-dependent，独立评测应用 `normalize=none`。

## 历史验证范围（之前版本）

- 新增 `mini_verl/tools.py`：`ToolSchema`、`ToolResult`、`ToolInstance`、`BaseTool`、`FunctionTool`、`@function_tool`、`ToolRegistry`；覆盖 schema 推断、每轨迹实例、异常/超时封装、重名拒绝与 legacy `call`。
- 新增 `mini_verl/parallel.py` 的路由回放：`moe_route(..., replay_top_idx, replay_mask)`、`record_moe_routes`、`replay_moe_routes`（层数不一致直接失败）、`router_replay_report`（定位首个错层与错 token）。
- 新增 `examples/23_tool_registry.py` 与 `examples/24_router_replay.py`；示例数 22 → 24。
- 新增 `tests/test_tools_registry.py`（21 个）与 `tests/test_router_replay.py`（10 个）；测试 190 → 221。
- 新增附录 T（工具系统/沙箱）、U（模型接入/权重加载）、V（DeepSeek V4/量化同步/路由回放）、W（资源分离/checkpoint engine/弹性部署）；附录 19 → 23。
- 章节新增：5.15、15.13、16.13、26.15、30.14、41.15。
- 上一版：`examples/22_lifecycle.py` 全生命周期；GAE/REINFORCE++ mask 语义对齐；第二轮审计 11 项修复由 `tests/test_audit_regressions.py` 固定。

## 上一轮独立审计修复（历史）

两路独立审计（代码 + 事实）发现的问题已修复并由测试/文档固定：

**代码**

- `FunctionTool` 的 JSON 序列化失败不再逃逸；自带 `TimeoutError` 不会再被误报为超时；混合位置/关键字调用正确合并。
- `CalculatorTool` 的格式转换纳入异常边界；`**` 增加指数/位数上限，`9**9**9` 不再挂死。
- `ToolRegistry` 重命名失败会回滚，不留下旧 key 别名；重复注册会报错；未知 instance id 转为 `error: KeyError`；schema 导出为防御性拷贝。
- schema 推断支持 `Optional[int]`、`int | None`、`list[int]`、`tuple[int, int]`；lambda 名会清洗成合法工具名。
- `moe_route` 校验 2D/实数/有限 logits、整数 `top_k`、`expert_outputs` 形状；`replay_mask` 必须配 `replay_top_idx`；`dropped_tokens` 改为唯一 token 数，另给 `dropped_slots`。
- `record_moe_routes(with_accepted=True)` 与 `router_replay_report` 能发现“top-k 相同但容量丢弃不同”；报告新增 `mismatched_assignments` 与 `shape_mismatched_layers`。

**事实**

- 附录 U：Megatron 默认走 Megatron-Bridge，`ModelRegistry` 是 legacy 路径；四个 saver 是 `NotImplementedError` 桩；legacy loader 只做 TP、不处理 tied embeddings；`model_merger` 是离线 CLI；VeOmni 是 FSDP2 + Ulysses SP + EP（无 TP）；只有 Megatron 支持 PP。
- 附录 V/第 16 章：`REPLAY_BACKWARD` 是 `pop(0)` FIFO，不是倒序。
- 附录 W/第 15 章：`hccl` 不是独立注册键（Ascend 配 `backend=nccl`）；资源池 helper 只建 `trainer_pool`；`CheckpointEngine` 只有 `send_weights/receive_weights`，`get_weights` 在无具体子类的 `CheckpointEngineWithCache` 上；`get_per_tensor_param()` 返回 `(generator, metadata)`。
- 附录 T/第 26 章：上游没有 `ToolRegistry` 类；`load_function_tools_from_path` 返回 `list[FunctionTool]`；TQ manager 在 `verl/trainer/ppo/v1/agent_loop_tq.py`；AgentLoop 对 FunctionTool/native 走不同分支。
- 第 30 章：`muon_scalar_optimizer` 是未读取的 no-op 字段；`muon_split_qkv` 是 per-head 正交化。

## 环境

- 日期：2026-11-22
- 平台：Windows 11（Git Bash）
- Python：3.14.6
- NumPy：2.5.1
- pytest：9.1.1
- PyYAML：6.0.3
- GPU：无（MiniVerl 全部在 CPU 上运行）

## 三条命令

```bash
python -m pytest tests/ -q
python -X utf8 examples/run_all.py
python -X utf8 check_docs.py
```

### 1. 测试

```text
250 passed
```

覆盖：

- `test_autograd.py`：21 个，含有限差分；
- `test_nn.py`：7 个；
- `test_protocol.py`：10 个；
- `test_config.py`：9 个，含 CLI 裸 override 解析；
- `test_dispatch.py`：8 个；
- `test_core_algos.py`：36 个，含 GAE 内部 mask、R++ 折扣、entropy mask 对齐、CISPO 公式/梯度；
- `test_data_rewards.py`：12 个；
- `test_engine_parallel.py`：16 个，含 TP 不可整除复制、delta 字节；
- `test_rollout_checkpoint.py`：13 个，含 PD（含 Gbps→bit/s 的手算传输断言）、delta 部分更新、ReplayBuffer 版本；
- `test_trainer.py`：25 个，含优化器恢复、ref 不被覆盖、DAPO 全过滤、CISPO 端到端、独立评测与 checkpoint 重载；
- `test_lora.py`：10 个，覆盖冻结、初始恒等、梯度归属、合并与 state_dict 往返；
- `test_transfer_queue.py`：4 个，覆盖部分取用、背压、快照恢复；
- `test_regressions.py`：8 个，覆盖独立审计发现的回归：DPO 跨 world size 的全局归一、reward 失败/超时、IS 均匀偏移不被抵消、delta_sharded 字节上限、LoRA 字符串 target、权重更新后 prefix cache 失效、checkpoint `max_to_keep=0`。
- `test_audit_regressions.py`：11 个，覆盖第二轮独立审计：CISPO/GSPO 的 IS 权重、sync 模式 rollout correction、最终权重同步、按 actor 更新计数的 staleness、cosine LR horizon、评测剥离 shaping、数学 prompt 截断、DPO sum log-prob/mask 压缩、蒸馏全局归一、稳定 softplus、resume 的 RNG 状态。
- `test_tools_registry.py`：21 个，覆盖 schema 推断（含 `Optional`/`int | None`/`list[int]`）、两种装饰器写法、异常与 JSON 序列化不逃逸、协作式超时与自带 `TimeoutError` 的区分、实例隔离、重名冲突与重命名回滚、未知实例 id、`*args` 拒绝、schema 防御性拷贝。
- `test_router_replay.py`：10 个，覆盖 logits 改变时复现、行级 mask、形状/范围/有限性校验、层数不匹配、首个错层定位、容量丢弃与 `accepted` 差异、记录独立性。
- `test_reward_model.py`：16 个，覆盖稳定 `sigmoid`/`softplus`/`log_sigmoid` 的数值与有限差分梯度、BT 方向/margin/label smoothing、pairwise accuracy 与 ECE、last/mean pooling 与 attention mask、偏好 batch 的 padding、RM 训练收敛、检查点往返、`ModelRewardManager` 的 scatter/缺字段/`without_shaping`，以及 PPO 接入与缺 `model_path` 早失败。
- `test_prm.py`：13 个，覆盖步骤标签/前缀/EOS、`local`/`suffix` 传播、稳定 BCE 与 mask、六种聚合、指标、PRM 训练收敛、检查点与格式校验、边界（最后一块/严格递增）、scatter、overlong shaping、PRM+verifier 组合与缺 `model_path` 早失败。

### 2. 示例

```text
all 26 examples passed
```

包括 quickstart、PPO/GAE、GRPO、DataProto、dispatch/sharding、rollout/sync、checkpoint/resume、DAPO、Agent、异步、SFT/DPO、显存/并行、自定义 reward、benchmark、数学奖励、并行模式、LoRA 冻结/训练/合并、PD 分离、TransferQueue、CISPO、独立评测、全生命周期（训练→续训→评测→导出）、工具注册表、路由回放、奖励模型（偏好对→BT→校准→检查点→PPO 模型奖励）、过程奖励模型（步骤标签→前缀 PRM→六种聚合→PRM+verifier PPO）。

### 3. 文档自检

```text
OK: 45 chapters, 26 appendices, 250 tests, 26 examples, all references resolved
```

检查项：

- 章/附录编号连续、标题格式；
- `本章小结`/`延伸阅读`；
- 相对链接；
- 章节/附录引用；
- `verl/` 路径在 v0.9.1 清单中；
- `mini_verl/`/`tests/`/`examples/`/`scripts/` 路径存在；
- 硬数字与 pytest/示例数一致；
- 无占位符、代码围栏平衡。

## 一次跑完

```bash
python -X utf8 scripts/verify_all.py
```

## 限制

- MiniVerl 不训练真实 LLM；TinyTransformer/线性策略只用于验证系统契约。
- 没有 GPU/NCCL/Ray/vLLM/SGLang；分布式是语义模拟，不是性能基准。
- 真实 verl 的事实以 v0.9.1 源码与文档为准；本仓库用 `reference/verl-v0.9.1-paths.json` 校验路径。
- MiniVerl 的 LoRA 是单机 NumPy 实现，未覆盖 FSDP/Megatron 下的 adapter 分片、量化基座与多 adapter 热切换。
- MiniVerl 的 CISPO 只实现 token 级比率版本；DPPO/DRO/SAPO/GPG 等仍只有文档索引，配置层会提前拒绝未知 `loss_mode`。
- 独立评测 `main_eval` 只覆盖 MiniVerl 的合成任务与规则奖励；它不包含常驻生成服务器，也不替代真实 benchmark 的 parser/污染控制（见附录 L）。
- 附录 K 的 PRM 部分已由 `mini_verl/prm.py` 落地；树搜索/自博弈仍是设计与工程指南，未在真实集群/大模型上执行。附录 L 的基准数字是方法示例，不是排行榜结论。
- 附录 M（引擎后端）、N（安全/沙箱）、O（案例研究）是设计与工程指南，未在真实集群上执行。
- 附录 P（二次开发）、Q（高级特性）、R（交付手册）是工程指南；其中的命令与路径按 v0.9.1 文档/清单核对，但未在真实 GPU/多机环境逐条执行。
- 附录 T（工具/沙箱）的沙箱部分（sandbox fusion、容器/微虚拟机）是设计与工程指南；MiniVerl 只实现进程内工具层，`FunctionTool` 的超时是协作式的，不能杀死后台线程。
- 附录 U（模型接入）与 V（DeepSeek V4）的权重转换、FP8/FP4/MXFP4、UE8M0 scale、vLLM 后处理生命周期均未在真实 GPU/vLLM 上执行；MiniVerl 只模拟路由回放，不模拟量化布局。
- 附录 W（资源分离/checkpoint engine）的带宽数字来自上游 README 的量级示例，未在本机复测；MiniVerl 的 TransferQueue/PD 是单进程语义版。
- 附录 X（奖励模型/RLAIF）与 Y（推理 RL 手册）是设计与工程指南；其中的配置字段按 v0.9.1 源码/文档核对，超参与阈值是经验区间而非保证。MiniVerl 的 RM 只验证接口与梯度，不训练真实语义 RM。
- 附录 Z（rollout 引擎内部）的调度/KV/投机公式是通用引擎模型，未在真实 vLLM/SGLang 集群上逐项复测；具体默认值随版本变化，容量数字是算例而非基准。
- MiniVerl 的标量 RM 只验证数据/损失/校准/检查点/PPO 接入契约，不是语义 RM；PRM 只验证步骤标签/前缀打分/聚合/服务契约，不是语义 PRM。二者都没有 HTTP reward server、router、多租户或 GPU 批处理。相关测试计入 250 个测试。
- PRM 的步骤切分是固定的（`step_tokens` 或显式边界）；没有换行/工具轮次解析、重 tokenize 校验或截断步骤的自动处理。PRM-only 奖励可被策略 reward hacking，示例默认与 verifier 组合。
- 如果用 PRM/RM 作为 reward manager 跑 `evaluate()`，pass@k 衡量的是奖励模型认为的质量，不是任务真实正确率；真实准确率需独立 verifier（24.13）。
- PD 分离只做 KV 传输账，不模拟真实 prefill/decode 调度；TransferQueue 是单进程语义版，不含跨节点传输。
- 商业分析中的数字是假设/量级，不是财务数据或报价。
