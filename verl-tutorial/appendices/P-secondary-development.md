# 附录 P 二次开发：从自定义 reward 到新后端

> 本章回答一个教程很少正面回答的问题：**如果 verl 内置的东西不够用，应该改哪里、按什么契约改、怎么证明改对了。**
>
> 这不是 API 大全，而是一张“扩展点地图”。真实签名以 v0.9.1 源码为准；本章给出的是每个扩展点必须满足的语义与验收方法。

## P.1 先选扩展点，而不是先改源码

verl 的扩展点大致分五层。越靠上层，改动越小、越容易测试；越靠下层，性能与自由度越高，但破坏面也越大。

| 需求 | 首选扩展点 | 关键文件 | 最小验证 |
|---|---|---|---|
| 换一个奖励规则 | 自定义 reward function | `verl/trainer/ppo/reward.py` | 固定样本的 golden score |
| 换奖励的批处理/放置方式 | 自定义 RewardManager | `verl/workers/reward_manager/abstract.py` | token 位置、mask、失败语义 |
| 奖励要走独立服务 | Reward Loop worker/router | `verl/experimental/reward_loop/` | 超时、重试、幂等、限流 |
| 多轮工具/环境 | 自定义 AgentLoop | `verl/experimental/agent_loop/agent_loop.py` | 轨迹 token 与 observation mask |
| 新数据集格式 | 自定义 Dataset + collate | `verl/utils/dataset/rl_dataset.py` | 字段、截断、mask 起点 |
| 新模型结构 | HF 实现 + monkey patch / registry | `verl/models/registry.py` | 与 HF 前向 logits 对齐 |
| 新推理引擎 | 自定义 BaseRollout | `verl/workers/rollout/base.py` | 同 seed 采样一致、权重更新可见 |
| 新训练后端 | 自定义 BaseEngine + ShardSpec | `verl/workers/engine/base.py` | dense 等价、梯度一致、checkpoint 往返 |
| 新算法 | advantage/policy-loss 注册表 | `verl/trainer/ppo/core_algos.py` | 手算数值 + 端到端不变量 |
| 新数据面 | TransferQueue | `verl/utils/transferqueue_utils.py` | 有界、版本、快照、重复/丢失 |
| 新采样调度 | ReplayBuffer / Sampler | `verl/trainer/ppo/v1/replay_buffer.py` | staleness、过滤、补采 |

一个常见错误是“为了改 reward 去 fork trainer”。reward 是数据流中的一个纯函数或服务，不应该和 PPO 主循环耦合。先问：这个需求能不能在表格最上面一行解决？如果不能，再往下走。

## P.2 开发环境：先让 CPU 测试跑起来

真实 verl 的开发不要求一上来就有八卡。v0.9.1 的 CI 把测试分成 CPU、GPU、e2e 与文档几类：

- `.github/workflows/cpu_unit_tests.yml`：不需要 GPU 的单元测试；
- `.github/workflows/gpu_unit_tests.yml`：需要 CUDA 的测试；
- `.github/workflows/e2e_ppo_trainer.yml`：端到端 PPO/GRPO；
- `.github/workflows/doc.yml`：文档构建。

本地最小流程：

```bash
git clone https://github.com/verl-project/verl.git
cd verl

# CPU 测试需要 cpu extra（它包含 torch 与 pytest）；test extra 只有测试工具
uv sync --frozen --all-packages --extra cpu

# 先跑与算法/协议相关的 CPU 测试
uv run --frozen --all-packages --extra cpu pytest tests/trainer/ppo/test_core_algos_on_cpu.py -q
uv run --frozen --all-packages --extra cpu pytest tests/utils/dataset/test_rl_dataset_on_cpu.py -q
```

`tests/README.md` 记录了测试目录与标记约定；改动前先读它，比在群里问“为什么我的测试没被 CI 跑到”更快。

开发环境要固定三件事：

1. **Python 与 CUDA**：用 `uv.lock`，不要手装一个“差不多”的 torch；
2. **extras**：训练后端与推理后端要一致（`fsdp`/`megatron` × `vllm`/`sglang`），否则 `uv run` 会重装；
3. **测试范围**：CPU 测试必须全绿；GPU 测试至少覆盖你改动的后端；e2e 只在最后跑。

## P.3 自定义 reward：三个粒度

### P.3.1 一个纯函数

最小扩展是一个函数：

```python
def compute_score(
    data_source: str,
    solution_str: str,
    ground_truth,
    extra_info: dict | None = None,
) -> float | dict:
    """返回标量，或 {'score': float, 'acc': bool, ...}。"""
    if data_source == "math":
        pred = extract_final_answer(solution_str)
        return {"score": float(equal(pred, ground_truth)), "acc": equal(pred, ground_truth)}
    ...
```

在配置里指向它：

```yaml
reward:
  custom_reward_function:
    path: my_rewards.py
    name: compute_score
```

工程约束：

- **纯函数**：不要读写全局状态、不要依赖当前 step；同一个输入必须得到同一个输出。
- **异常返回 0 并记录**：不要让一个坏样本中断整个 batch；但也不能静默吞掉，要有 `reward/error_rate`。
- **不依赖 tokenizer 的隐式行为**：如果要从文本抽答案，先把文本规范化（全角/半角、逗号、单位）。
- **版本化**：reward 函数的行为变了，训练曲线就不可比；把 `reward_version` 写进实验元数据。

最小验证：准备 20～50 条覆盖边界的样本，断言每条分数；再用 1000 条真实 rollout 看分布（全 0、全 1、双峰都是信号）。

### P.3.2 一个 RewardManager

当“怎么把标量放到 token 上”也需要改时，写 Manager。它负责：

1. 把 `DataProto` 拆成单样本请求；
2. 调用 reward function/model；
3. 把分数 scatter 到**最后一个有效 response token**；
4. 生成 `token_level_rewards` 与 `rm_scores`；
5. 记录 `reward_extra_info`（accuracy、format、length、timeout）。

参考实现：`verl/workers/reward_manager/naive.py`、`dapo.py`、`registry.py`、`prime.py`。

三个必须测试的边界：

- 全是 padding 的样本：不能越界，不能把分数放到 pad 上；
- 多轮轨迹：最后一个 `response_mask=1` 的 token 之后可能还有 observation；
- 失败样本：失败应该是 0 分 + 标记，而不是异常穿透到 trainer。

### P.3.3 一个 Reward Loop worker

当 reward 是独立服务（reward model、代码沙箱、远程 API）时，扩展到 `verl/experimental/reward_loop/`：

- `reward_manager/base.py`：请求/响应契约；
- `reward_manager/naive.py`、`dapo.py`、`limited.py`、`remote.py`：批处理、限流、重试；
- `router/naive_router.py`、`inner_sglang_router.py`：按 `data_source` 路由；
- `reward_model.py`：判别式/生成式 reward model 的封装。

一个可用的请求至少包含：`request_id`（幂等键）、`data_source`、`prompt`、`response`、`response_mask`、`metadata`、`timeout_ms`。响应至少包含：`score`、`reward_extra_info`、`error`、`latency_ms`。

验收重点不是“分数对不对”，而是**故障语义**：

- 超时/5xx 重试 1～2 次，4xx 不重试；
- 重试不能重复计费（幂等键）；
- worker OOM 后能重启并重新 warmup；
- 失败率、超时率、重试次数进入指标。

## P.4 自定义 AgentLoop：轨迹的所有权

AgentLoop 是“多轮对话 + 工具 + tokenization”的所有者。自定义它，通常是因为：

- 工具有私有协议（不是 OpenAI function calling）；
- 环境需要 `reset/step`，而不是一次函数调用；
- 需要在轮次之间做特殊 masking 或奖励分配。

核心契约（以 `verl/experimental/agent_loop/agent_loop.py` 为准）：

```python
class MyAgentLoop(AgentLoopBase):
    async def run(self, sampling_params, **kwargs) -> AgentLoopOutput:
        messages = kwargs["raw_prompt"]
        tools = kwargs.get("tools_kwargs", {})
        # 1. 调 chat template
        # 2. 调 LLM server 生成
        # 3. 解析工具调用并执行
        # 4. 把 observation 拼回 messages
        # 5. 记录 token_ids / response_mask / loss_mask
        return AgentLoopOutput(...)
```

必须保证：

- **observation token 的 `response_mask=0`**：工具返回不是模型动作，不能进策略损失；但奖励要能穿过它传播。
- **tokenization 只发生一次**：不要先 tokenize 再拼字符串再 tokenize，chat template 的空格/BOS 会让位置错位。
- **轮次上限与超时**：没有上限的多轮会烧穿预算。
- **可复现**：工具调用的随机性要可控；记录工具版本与环境配置。

测试方式：用一个假 LLM server 返回固定文本，配一个假工具，断言轨迹的 token、mask、轮次和奖励位置。参考 `tests/experimental/agent_loop/test_basic_agent_loop.py` 与 `test_call_tool_on_cpu.py`。

## P.5 自定义数据集：字段比代码重要

自定义数据格式时，优先改“预处理脚本 → parquet”，而不是改 `RLHFDataset`。只有字段语义真的不同，才写 Dataset。

必须明确的契约：

| 字段 | 语义 | 常见错误 |
|---|---|---|
| `prompt` | 字符串或 chat messages | 训练/验证模板不一致 |
| `data_source` | 奖励路由 key | 新任务忘记加分支，分数全 0 |
| `reward_model.ground_truth` | 验证信息 | 类型不稳定（str/list/dict） |
| `extra_info` | 工具、测试用例、难度 | 多轮样本缺 `tools_kwargs` |
| `ability` | 能力标签 | 只用于统计，不能当奖励 |

v0.9.1 的所有路径都由 AgentLoop 做 tokenization；dataset 的 `__getitem__` 只返回 `raw_prompt`。因此 `DataProto.batch` 里可能只有 `dummy_tensor`，真正的 token 在 AgentLoop 输出里；自定义 collate 时不要假设 `input_ids` 一定存在。

验收：`tests/utils/dataset/test_rl_dataset_on_cpu.py` 风格的单测 + 一个 100 条样本的端到端 rollout，检查 `response_mask.sum()`、截断比例、reward 分布。

## P.6 接入新模型：先 HF，后 Megatron

### P.6.1 HF 路径

大多数新模型只需要：

1. transformers 能加载（`AutoModelForCausalLM` / `AutoModelForVision2Seq`）；
2. tokenizer/processor 与 chat template 正确；
3. 模型 forward 返回 logits，且支持 `position_ids`；
4. 如果需要自定义 attention/rope，在 `verl/models/transformers/` 下加实现，并在 `verl/models/registry.py` 注册。

对照测试是必须的：同一个 batch，HF 原生 forward 与 verl 包装后的 logits 在 `atol=1e-4` 内一致。否则 PPO 的 `old_log_prob` 与 rollout 的 log-prob 会对不上。

### P.6.2 Megatron 路径

Megatron 需要权重映射：

- `verl/models/mcore/bridge.py`：HF ↔ MCore 的参数映射；
- `verl/models/mcore/weight_converter.py`：逐 tensor 转换；
- `verl/models/mcore/registry.py`：模型注册；
- `verl/model_merger/megatron_model_merger.py`：分布式 checkpoint 合并回 HF。

最小验收：

1. HF 权重 → Megatron → HF 的 roundtrip 误差；
2. 单卡 dense 与 TP=2/4 的 logits 对齐；
3. PP 切分后，首/尾 stage 的输入输出形状正确；
4. MoE 模型的 router 权重与 expert 权重都被映射。

模型接入是“慢工出细活”：一个 norm 或 bias 漏映射，输出只会“稍微不对”，训练曲线不会立刻崩。务必用固定 prompt 对比 logits，而不是只看 loss。

## P.7 自定义 Rollout 后端

`verl/workers/rollout/base.py` 的 `BaseRollout` 是推理后端的契约。一个可用的实现至少要有：

```python
class MyRollout(BaseRollout):
    def generate_sequences(self, prompts: DataProto) -> DataProto: ...
    def update_weights(self, weights: Iterator[tuple[str, torch.Tensor]]) -> None: ...
    def sleep(self) -> None: ...
    def wake_up(self) -> None: ...
```

四个语义：

1. **采样**：支持 `n`、temperature/top-k/top-p、stop token、max length；返回 `rollout_log_probs`（行为策略的 log-prob，不是原始 logits）。
2. **权重更新**：接收 HF 坐标的逐 tensor 权重；支持全量/增量；更新后旧 KV cache 必须失效。
3. **sleep/wake**：sleep 释放权重/KV/CUDA graph；wake 重新加载；sleep 期间不能生成。
4. **中断/恢复**：异步/partial rollout 需要保存请求状态；至少要有超时与取消。

如果是 server 模式，还要实现健康检查、请求路由、超时与重试。参考 `verl/workers/rollout/llm_server.py`、`replica.py`、`router.py`。

验收：

- 同 seed、同参数下，与 HF/Naive 后端逐 token 一致（允许浮点误差）；
- `update_weights` 后，固定 prompt 的输出发生变化；
- sleep 后调用生成会报错，wake 后恢复；
- 权重版本号与训练侧一致。

## P.8 自定义训练引擎

`verl/workers/engine/base.py` 的 `BaseEngine` 负责“模型怎么算”，`spec.py` 的 `ShardSpec` 负责“参数怎么分”。新后端要实现：

- `forward_backward_batch`：前向、loss、反向；
- `train_batch` / `infer_batch`：训练与推理入口；
- `get_per_tensor_param*`：按 HF 坐标导出权重（全量/分片/delta）；
- `save_checkpoint` / `load_checkpoint`：模型、优化器、scheduler（真实签名带 `local_path`/`hdfs_path`/`global_step` 等参数）；
- `optimizer_zero_grad` / `optimizer_step` / `lr_scheduler_step`：由 worker 在训练 RPC 中驱动。

MiniVerl 里的 `get_gradients` / `set_gradients` / `finalize_update` 是为了把 all-reduce 显式化而加的教学方法，真实 `BaseEngine` 没有这三个接口。

`ShardSpec` 要描述：`full_shape`、`mesh`、`placements`（Shard/Replicate）、`gather_group`、`to_hf_chunk`/`hf_slots`。checkpoint engine 只认 spec，不认后端。

验收顺序：

1. dense 等价：同一模型、同一 batch，单卡 dense 与新后端的 loss/grad 一致；
2. 分片往返：shard → gather → shard 的数值一致；
3. checkpoint 往返：保存/加载后继续训练，loss 不跳变；
4. 权重导出：导出的 HF state_dict 能被 transformers 加载。

扩展指南见附录 M 与 `docs/advance/fsdp_extension.rst`、`docs/advance/megatron_extension.rst`。

## P.9 自定义算法：注册表，而不是 if/else

verl 把算法拆成三层：

1. **优势估计器**：`algorithm.adv_estimator`，注册在 `verl/trainer/ppo/core_algos.py`；
2. **策略损失**：`actor.policy_loss.loss_mode`，同样在 `core_algos.py`；
3. **数据/奖励流**：`filter_groups`、`reward_manager`、`rollout.n`。

加一个新算法的最小步骤：

1. 在 `core_algos.py` 写纯函数（输入 tensor + mask，输出 advantage/loss + metrics）；
2. 注册名字；
3. 在配置校验里加枚举；
4. 加手算单测（小 batch、固定数值）；
5. 加一个端到端 smoke test（能跑 10 步、loss 有限、指标存在）；
6. 更新文档与 recipe。

不要把 DAPO 之类“组合算法”做成一个新的 `adv_estimator`。DAPO = GRPO 优势 + clip-higher + token-level loss + 动态过滤 + overlong penalty；把它拆成可组合的配置，才能复用与消融。

## P.10 测试与 CI：四层，不要只写 happy path

| 层 | 目标 | 例子 | 运行环境 |
|---|---|---|---|
| 单元 | 纯函数/算子 | GAE、KL、mask、shard | CPU |
| 组件 | 协议/worker/engine | DataProto、dispatch、checkpoint | CPU/单卡 |
| 差分 | 与参考实现对齐 | TP vs dense、HF vs Megatron logits | GPU |
| 端到端 | 整条链路 | 小模型 PPO/GRPO 10～50 步 | GPU |

CI 对应：

- `.github/workflows/cpu_unit_tests.yml`
- `.github/workflows/gpu_unit_tests.yml`
- `.github/workflows/e2e_ppo_trainer.yml`
- `.github/workflows/doc.yml`

写测试时优先断言**不变量**，而不是快照：

- mask 外梯度为 0；
- padding 不改变 loss；
- shard/gather 往返一致；
- 同 seed 同输出；
- 权重更新后 rollout 版本递增；
- 失败样本被计为 0 且记录错误。

“测试全绿”不等于“实现正确”。如果断言恰好编码了 bug（例如把单样本组的优势写成 0），测试会保护 bug。新算法至少要有一个人工推导的数值算例。

## P.11 贡献流程：从 issue 到合并

一个可维护的贡献流程：

1. **Issue**：说明动机、复现、期望行为；性能改动附基准方法。
2. **设计**：影响公共 API/配置/checkpoint 的改动，先在 issue 里对齐；避免“先写 2000 行再被拒”。
3. **PR**：小步提交；标题符合 `.github/workflows/check-pr-title.yml`；描述里写测试命令与结果。
4. **测试**：CPU 测试必须过；GPU/e2e 至少覆盖改动后端；新行为要有回归测试。
5. **文档**：配置字段、公开 API、迁移说明；文档构建由 `doc.yml` 检查。
6. **Review**：`.github/CODEOWNERS` 指定的 owner；涉及 checkpoint/权重同步的改动要有数值验证。
7. **合并**：保持 commit 历史可读；破坏性改动写 migration note。

PR 描述模板见 `.github/PULL_REQUEST_TEMPLATE.md`。一个高质量 PR 的描述通常包含：问题、方案、权衡、测试命令、性能数据、兼容性影响。

## P.12 版本兼容：配置、checkpoint 与 recipe

verl 的版本演进很快，二次开发要处理三类兼容：

- **配置兼容**：字段改名/搬家时，保留旧字段的解析或给出明确报错；不要静默忽略未知字段。
- **checkpoint 兼容**：分片布局、优化器状态、scheduler、dtype 变化要有转换脚本；至少保证“旧 checkpoint 能加载并继续训练”。
- **recipe 兼容**：`verl-recipe` 用 `REQUIRED_VERL.txt` 固定兼容的 verl 版本/commit；升级核心库前先跑 recipe 的 smoke test。

从 0.3 到 0.9 的路径变化（`fsdp_workers.py` → `engine_workers.py`、`sharding_manager` → engine/checkpoint engine）说明：**不要在业务代码里直接依赖内部路径**。尽量通过配置、注册表和公开 worker RPC 扩展；必须依赖内部 API 时，把版本检查写进启动脚本。

## P.13 二次开发检查清单

- [ ] 先确认能否用配置/自定义函数解决，而不是 fork 主循环。
- [ ] 扩展点选在正确的层（reward / agent / dataset / model / rollout / engine / algorithm）。
- [ ] 契约写清楚：输入、输出、mask、失败语义、版本。
- [ ] 有手算/差分测试，不只测“能跑”。
- [ ] CPU 测试全绿；改动后端有 GPU/e2e 验证。
- [ ] 配置字段有文档与校验；未知字段不静默忽略。
- [ ] checkpoint 能往返；权重导出能被 HF 加载。
- [ ] 失败可观测：错误率、超时率、重试次数、版本号。
- [ ] 兼容旧 checkpoint/recipe，或提供迁移脚本。
- [ ] PR 描述包含测试命令、性能数据与兼容性影响。

## 延伸阅读

- 扩展点总览：附录 M（引擎后端）、第 17 章（reward loop）、第 26 章（AgentLoop）、第 30 章（LoRA/量化）。
- 官方文档：`docs/advance/agent_loop.rst`、`docs/advance/reward_loop.rst`、`docs/advance/dpo_extension.rst`、`docs/advance/fsdp_extension.rst`、`docs/advance/megatron_extension.rst`、`docs/advance/ppo_lora.rst`、`docs/workers/engine_workers.rst`、`docs/workers/model_engine.rst`。
- 测试与 CI：`tests/README.md`、`.github/workflows/cpu_unit_tests.yml`、`.github/workflows/gpu_unit_tests.yml`、`.github/workflows/e2e_ppo_trainer.yml`。
- 下一章：附录 Q 高级特性：attention、RoPE、MTP、dynamic CP 与可观测性。
