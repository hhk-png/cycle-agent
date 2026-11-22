# 附录 F 实验手册与自测

## Lab 1：跑通 MiniVerl

目标：理解一次完整训练。

```bash
python -X utf8 -m mini_verl.trainer.main_ppo --dry-run
python -X utf8 -m mini_verl.trainer.main_ppo trainer.total_training_steps=20
python -m pytest tests/ -q
```

问题：

1. 一次 step 包含哪些阶段？
2. 为什么 bandit 能这么快学会？
3. 训练 reward 与验证 accuracy 的关系？

## Lab 2：PPO vs GRPO

```bash
python -X utf8 examples/03_grpo_vs_ppo.py
```

问题：

1. 哪个需要 critic？
2. 哪个需要 `rollout.n>1`？
3. 显存/算力差异在哪？

## Lab 3：DataProto

```bash
python -X utf8 examples/04_dataproto_tour.py
```

修改 `examples/04_dataproto_tour.py`：

- 把 `repeat` 改成 `interleave=False`，观察顺序；
- 增加一个 non_tensor 字段，确认 chunk/concat 后仍在；
- 用 `pad_to_divisor` 后手动 unpad，验证长度。

## Lab 4：Dispatch

```bash
python -X utf8 examples/05_dispatch_and_sharding.py
```

问题：

1. `DP_COMPUTE_PROTO` 如何 padding？
2. `ONE_TO_ALL` 与 `RANK_ZERO` 的区别？
3. FSDP shard 与 TP shard 的切分维度？

## Lab 5：Rollout/权重同步

```bash
python -X utf8 examples/06_rollout_weight_sync.py
```

修改：

- 把 backend 改成 `delta`，观察 `bytes_sent`；
- 连续两次 `update_weights`，第二次只改一个 tensor；
- 观察 `changed_ratio`。

## Lab 6：Checkpoint/Resume

```bash
python -X utf8 examples/07_checkpoint_resume.py
```

问题：

1. 保存了哪些文件？
2. 恢复时为什么要同步 rollout？
3. 如果删除 optimizer 状态会怎样？

## Lab 7：DAPO 过滤

```bash
python -X utf8 examples/08_dapo_filtering.py
```

修改 `scores`，构造：

- 全部相同 -> 全部过滤；
- 每组都不同 -> 全部保留；
- 部分相同 -> 观察 `filtered_ratio`。

## Lab 8：Agent 工具

```bash
python -X utf8 examples/09_agent_tools.py
```

问题：

1. 哪些 token 的 mask=1？
2. observation 为什么要 mask=0？
3. 如何加第二个工具？

## Lab 9：异步

```bash
python -X utf8 examples/10_async_rollout.py
```

修改：

- `max_off_policy_threshold` 改 0/2/8；
- `strategy` 改 drop/keep；
- 观察 staleness 指标。

## Lab 10：SFT/DPO

```bash
python -X utf8 examples/11_sft_and_dpo.py
```

问题：

1. SFT 与 DPO 的 loss 区别？
2. DPO 为什么需要 ref？
3. `dpo/margin` 的含义？

## Lab 11：显存/并行

```bash
python -X utf8 examples/12_memory_and_parallel.py
python -X utf8 examples/16_parallel_patterns.py
```

修改：

- `zero_stage` 0/1/2/3；
- `world_size` 1/8/64；
- 观察 `params/grads/optimizer` 的变化。

## Lab 12：自定义 reward

```bash
python -X utf8 examples/13_custom_reward.py
```

修改 `examples/custom_reward.py`：

- 加长度惩罚；
- 加格式奖励；
- 观察 `rm_scores`。

## Lab 13：扩展 MiniVerl

任务（任选）：

1. 加 `DPPO`（TV 或 KL 信任域）或 `DRO` loss（CISPO 已在 `mini_verl/algo/core_algos.py` 实现，见 Lab 16）；
2. 加 turn-level advantage（按轮次分组，而不是整个轨迹一个优势）；
3. 加 `varlen` collate；
4. 给 LoRA 加 rank 分片或 adapter 热切换；
5. 加 `ThreadPool` 异步 rollout。

要求：

- 加测试；
- 更新文档；
- 跑 `pytest`、`examples/run_all.py`、`check_docs.py`。

## Lab 14：真实 verl 小实验

如果你有 GPU：

1. 安装 verl（uv/docker）；
2. 跑 0.5B GSM8K PPO；
3. 跑 0.5B GRPO；
4. 对比 `actor/pg_loss`、`critic/vf_loss`、`val/test_score`；
5. 记录显存/吞吐。

## Lab 15：LoRA 的冻结与合并

1. 运行 `python -X utf8 examples/17_lora.py`，记录 `trainable_parameters` 与 `adapter_bytes`；
2. 把 `rank` 从 4 改成 8、16，验证可训练参数按公式增长；
3. 把 `alpha` 改成 `2*rank`，观察初始恒等是否仍然成立（B=0 时与 alpha 无关）；
4. 故意不调用 `freeze_base_parameters`（注释掉一行），确认哪些参数会被更新、测试如何抓住它；
5. 在 `tests/test_lora.py` 加一个“合并后 state_dict 不再有 adapter key”的断言；
6. 思考：如果 rollout 引擎支持 adapter，是否还需要 merge？两种路径的字节数差多少？

## Lab 16：CISPO 与独立评测

1. 运行 `python -X utf8 examples/20_cispo_training.py`，记录固定 batch 上 PPO 与 CISPO 的 loss，解释为什么一个是 -1.2、一个是 -12；
2. 把 `clip_ratio_high` 从默认 0.2 改成 0.28/1.0，观察 CISPO 的 `cispo_clipfrac` 与训练曲线如何变化；
3. 把 `tests/test_core_algos.py::test_cispo_clips_weight_but_keeps_score_gradient` 的 `detach()` 去掉，确认梯度测试会失败；恢复后重跑；
4. 运行 `python -X utf8 examples/21_standalone_eval.py`，记录 pass@1/4/8；思考为什么 bandit 上三者可能都到 1.0；
5. 用 `--n 2 --k 1,2` 跑 `main_eval`，再改成 `--n 1 --k 1`，比较 accuracy 与 pass@k 的口径；
6. 把 `PPOTrainer.evaluate` 里的 `eval_manager` 换回 `self.reward_manager`（或临时让 `overlong_penalty_factor>0`），构造超长样本，观察 shaped reward 如何污染指标；
7. 思考：如果 checkpoint 目录没有 `config.yaml`，独立评测还能复现吗？应该补什么元数据？

## Lab 17：工具系统

1. 运行 `python -X utf8 examples/23_tool_registry.py`，记录注册的工具、schema 与重名冲突的输出；
2. 用 `@function_tool` 写一个 `word_count(text: str)`，确认 schema 的 `required` 只有 `text`；
3. 写一个会抛异常的函数工具，确认 AgentLoop 收到的是 `error: <Type>` 而不是崩溃；
4. 给 `FunctionTool` 设一个极小的 `timeout`，观察 `timeout` 结果；思考为什么后台线程仍可能继续运行；
5. 在 `ToolRegistry` 里注册两个同名工具，确认抛 `ToolCollisionError`；
6. 给 `CalculatorTool` 加一条 `__import__('os')` 的输入，确认真值被 AST 白名单拒绝；
7. 思考：如果工具返回图像，`ToolResult` 应该怎么扩展？observation 的 mask 还是 0 吗？

## Lab 18：路由回放

1. 运行 `python -X utf8 examples/24_router_replay.py`，记录 `exact replay` 与 `corruption report`；
2. 修改 `moe_route` 的 `replay_mask`，让奇数行重放、偶数行原生路由，验证混合结果；
3. 构造 3 层 logits + 4 层记录，确认 `replay_moe_routes` 抛层数错误；思考如果没有检查会发生什么；
4. 把某一层的记录整体右移一层，观察 `first_mismatch` 的定位；
5. 思考：hash router 的专家 id 来自 `input_ids`，MiniVerl 该如何模拟？需要额外的表吗？

## Lab 19：过程奖励模型

1. 运行 `python -X utf8 examples/26_process_reward.py`，记录 copy PRM 的 step/trajectory accuracy、六种聚合分数，以及 PRM+verifier 的 PPO 真实准确率；
2. 运行 `python -X utf8 -m mini_verl.trainer.main_prm configs/prm_copy.yaml`，比较 `label_mode=local` 与 `suffix` 的 trajectory accuracy；再跑 `configs/prm_bandit.yaml` 观察单动作任务；
3. 把 `corruptions_per_sample` 从 8 改成 1，观察 PRM 在策略输出上的盲区；这解释了为什么反例分布要贴近 rollout；
4. 构造一条 3 步轨迹，显式传 `step_boundaries`；再把最后一个边界改小，确认 `ProcessRewardManager` 报错而不是丢 token；
5. 用 `aggregation=last` 与 `min` 分别跑同一批 PRM 分数，找一个“last 判对、min 判错”的样本；
6. 把 `reward.prm.prm_weight` 设为 0、`outcome_weight` 设为 1，确认退化成 verifier；再反过来设成 PRM-only，观察 reward hacking；
7. 思考：如果步骤边界来自换行符，但模型输出不遵守格式，训练/推理应该怎么回退？

## 自测题

1. GAE 在 padding 位置为什么要特殊处理？
2. GRPO 单样本组的优势是多少？
3. DP 下 token-mean 如何全局归一？
4. FSDP 的 `reshard_after_forward` 影响什么？
5. TP 列/行并行切哪个维度？
6. 权重同步字节数怎么算？
7. delta 同步何时有效？
8. 异步 staleness 如何控制？
9. 多轮 observation 为什么 mask=0？
10. 如何判断 reward hacking？
11. 如何把一次 7B 训练成本减半？
12. 如何定位“训练 reward 涨但验证不涨”？
13. LoRA 的初始增量为什么必须为 0？merge 应该发生在训练副本还是 rollout 副本？
14. PRM 的步骤奖励直接加到 token 奖励上，什么情况下会改变最优策略？
15. CISPO 为什么要对裁剪后的比率做 detach？如果 detach 整个 loss 会怎样？
16. 独立评测为什么不能复用训练时的 DAPO reward manager？
17. KV offload 在什么条件下不如直接丢弃 + 重算？
18. 把 CUDA 上的 RL 配方迁到 ROCm/NPU，最先要验证哪三件事？
19. 工具为什么需要 per-trajectory 实例？举两个有状态工具的例子。
20. FunctionTool 的超时为什么是协作式的？生产沙箱应该在哪一层设超时？
21. Router replay 的 R2 与 R3 分别解决什么问题？只做 R2 会漏掉什么？
22. 为什么 hash router 漏记录会让后续每一层都错位？如何用代码提前发现？

## 结业项目

选择一个任务：

- 数学/代码/结构化抽取；
- 多轮工具调用；
- 小模型 LoRA RL；
- 异步吞吐优化。

交付：

- 可运行代码；
- 数据/reward 说明；
- 指标曲线；
- 成本估算；
- 失败分析。
