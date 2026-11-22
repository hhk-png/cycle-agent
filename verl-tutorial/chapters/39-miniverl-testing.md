# 第 39 章 测试工程：单测、差分、不变量与端到端

## 39.1 为什么 RL 需要更重的测试

普通软件测试输入-输出；RL 训练测试“系统在长时间运行中是否保持正确”。很多 bug 不会抛异常：

- mask 错位；
- 优势全 0；
- 梯度未 all-reduce；
- 权重同步遗漏；
- 异步 staleness 失控。

MiniVerl 的测试策略是“多层验证”：

1. 算子级：有限差分；
2. 公式级：与手算/朴素实现对照；
3. 组件级：DataProto/dispatch/shard/checkpoint；
4. 端到端：bandit 能学会；
5. 文档级：引用/硬数字自检。

## 39.2 测试文件

```text
tests/
├── test_autograd.py          # 21
├── test_nn.py                # 7
├── test_protocol.py          # 10
├── test_config.py            # 9
├── test_dispatch.py          # 8
├── test_core_algos.py        # 36
├── test_data_rewards.py      # 12
├── test_engine_parallel.py   # 16
├── test_rollout_checkpoint.py# 13
├── test_lora.py              # 10
├── test_trainer.py           # 25
├── test_transfer_queue.py    # 4
├── test_regressions.py       # 8（审计回归：DPO 跨 rank 归一、reward 超时、IS 归一、delta 上限、LoRA 字符串、缓存失效、checkpoint prune）
├── test_audit_regressions.py # 11（第二轮审计：IS 权重、sync correction、最终同步、staleness、LR、评测 shaping、数学截断、DPO、蒸馏、softplus、RNG）
├── test_tools_registry.py    # 21（schema、装饰器、异常、超时、实例隔离、重名）
├── test_router_replay.py     # 10（记录/重放、行级 mask、形状/层数校验、错层定位）
├── test_reward_model.py      # 16（稳定算子、BT loss、mask、校准、检查点、PPO 接入）
└── test_prm.py               # 13（步骤标签、前缀、稳定 BCE、聚合、边界、scatter、verifier 组合、PPO 接入）
```

合计 250 个（以 `pytest --collect-only -q` 为准；`check_docs.py` 会核对总数）。注意 `test_core_algos.py` 的 36 是收集数：30 个函数 + 参数化展开的 6 个用例；写文档时说“36 个用例”比“36 个函数”准确。

（上面的分文件数字是写作时的快照；如果你增删测试，以实际收集数为准并更新文档。）

## 39.3 有限差分

`test_autograd.py::test_finite_difference_layer_norm`：

```python
numeric = finite_difference(fn, array)
assert np.allclose(x.grad, numeric, atol=2e-2, rtol=2e-2)
```

它验证手写 layernorm 反向。类似方法可用于任何新算子。

## 39.4 公式对照

- GAE：与逐步手算对照；
- GRPO：与手算均值/标准差对照；
- RLOO：leave-one-out；
- REINFORCE++：折扣回报；
- ReMax：greedy baseline；
- KL：k1/k2/k3；
- agg_loss：五种模式；
- policy loss：ratio=1、clip、dual-clip、clip-higher、CISPO 的 detach 与 score-function 梯度；
- value loss：未裁剪的基线（裁剪分支由 policy loss 的 clip 测试覆盖，value 的 clip 分支目前没有独立用例）；
- entropy：均匀分布 = log(V)。

这些测试的价值是“固定语义”。例如 GRPO 单样本组的分支，如果不看源码很容易写成 0；测试固定为“mean=0,std=1”。

## 39.5 组件不变量

- DataProto：chunk/concat 往返、padding/unpadding、repeat 顺序；
- dispatch：DP_COMPUTE_PROTO 自动 padding、ONE_TO_ALL、RANK_ZERO；
- shard：FSDP/TP roundtrip；
- checkpoint：save/load 一致、safetensors 往返；
- rollout：同 seed 一致、sleep 后报错、wake 恢复；
- async：staleness drop/keep；
- agent：observation mask=0；
- LoRA：初始恒等、只有 A/B 可训练、冻结基座不变、merge 前后前向一致、state_dict 往返；
- 独立评测：checkpoint 配置重载、pass@k 分组、naive reward 与训练 shaping 分离；
- 奖励模型：稳定 `log_sigmoid`/`softplus` 的有限差分、BT 方向与 margin/label smoothing、padding 不参与 pooling、检查点往返、PPO scatter 位置与 `without_shaping`；
- 过程奖励模型：步骤标签的 `local`/`suffix` 传播、前缀只看到当前步、稳定逐步 BCE、六种聚合、显式边界必须覆盖最后一个 response token、scatter 位置、overlong shaping、PRM+verifier 组合与缺 `model_path` 的早失败。

LoRA 的测试值得单独说：它同时约束**参数身份**（哪些参数可训练）和**数值语义**（初始增量是否为 0、merge 是否等价）。只测“loss 能降”会漏掉“基座也被更新了”这种错误；`tests/test_lora.py` 用参数身份与前后向数值两条线把它钉死。

## 39.6 端到端

`test_trainer.py::test_grpo_training_improves`：

- 40 步 bandit GRPO；
- 验证准确率 >= 0.75；
- 这保证“数据 → rollout → reward → advantage → update”整条链路没有断。

`test_ppo_gae_with_critic` 验证 PPO + critic。

`test_cispo_training_improves` 验证 CISPO 走的是真实代码路径而不是改名 vanilla：同样的 bandit、同样的 40 步，验证准确率 >= 0.75；固定 batch 的公式/梯度测试在 `test_core_algos.py`。

`test_evaluate_pass_at_k` 与 `test_evaluate_checkpoint_reloads_config` 验证独立评测：前者检查 n 采样下 pass@k 的单调性，后者从 checkpoint 的 `config.yaml` 重建 trainer 并加载权重。

`test_checkpoint_resume` 验证恢复后权重一致。

`test_async_modes_run` 验证 `colocate_async` 与 `separate_async` 两种异步模式能跑，并断言 staleness 非零（否则 async 等于同步）。

`test_prm.py::test_prm_trainer_learns_and_checkpoint_roundtrips` 训练一个多步 PRM 并检查 checkpoint 往返；`test_process_manager_*` 用可解释的假模型（按前缀最后一个 token 打分）验证边界、聚合、scatter、overlong shaping 与 verifier 组合，避免测试依赖训练出的随机权重。`test_ppo_rejects_prm_without_model_path_and_selects_manager` 验证配置错误早失败与 PPO 分支选择。

## 39.7 差分测试

“与朴素/手算实现对照”：

- GRPO 手算均值/标准差 vs 实现；
- TP linear vs dense；
- sequence parallel vs dense；
- safetensors writer/reader；
- FSDP shard/gather 往返；
- DPO world size 1 vs 3 的梯度等价（`test_regressions.py`）。

差分测试能发现“看起来对但有细微偏差”的实现。

## 39.8 假绿

测试全绿不代表正确。常见假绿：

- 断言恰好编码了 bug 行为；
- 只测了 happy path；
- 没有测 mask/padding 边界；
- 没有测多 worker；
- 没有测异常；
- 文档数字过期但 check_docs 没覆盖。

对策：

- 用有限差分/手算做独立验证；
- 随机化输入；
- 故意注入错误看测试是否失败；
- 让审计者独立复现，而不是只读代码。

MiniVerl 在开发过程中就遇到过“bandit 学不会”的问题，最后定位到分布/动作校准/mini-batch 顺序，而不是 PPO 公式。

第二轮审计又抓出一批“测试自己编码了错误行为”的例子：GAE 跨 observation 的 γλ 衰减、DPO 的 mean log-prob、`softplus` 的 clip 梯度、GSPO/CISPO 丢掉 IS 权重。它们当时都是绿的，因为断言与错误实现一致。修复后新增的 `tests/test_audit_regressions.py` 用“权重为 0 时 loss 必须为 0”“sum log-prob 必须等于手算”“大输入梯度不能为 0”这类**外部可验证**的断言，而不是重复实现的计算过程。

## 39.9 文档自检

`check_docs.py` 检查：

- 章/附录编号连续；
- 一级标题格式；
- `本章小结`/`延伸阅读`；
- 链接存在；
- 代码围栏平衡；
- 章节引用（`第 X.Y 节`、`§X.Y`）存在；
- 硬数字（测试数、示例数、章节数、附录数）与仓库事实一致；
- `mini_verl/`、`tests/`、`examples/` 路径存在；
- `verl/` 路径在 v0.9.1 清单中；
- 无占位符。

它抓的是“人眼审不出来”的漂移：重写章节后小节号变了、测试数变了、交叉引用指错。

## 39.10 运行方式

```bash
cd verl-tutorial
python -m pytest tests/ -q
python -X utf8 examples/run_all.py
python -X utf8 check_docs.py
```

Windows 上必须加 `-X utf8`，否则中文输出可能触发 `UnicodeEncodeError`。

## 39.11 测试与生产

MiniVerl 的测试不能替代生产验证：

- 没有真实 GPU/NCCL；
- 没有真实 vLLM/SGLang；
- 没有真实大模型；
- 没有多机故障；
- 工具层是进程内的，超时是协作式的，不覆盖真实沙箱的隔离/杀进程；
- 路由回放只验证记录-重放契约，不覆盖 FP8/FP4 kernel 或 hash router 的真实表。

但它能抓“语义级”错误，这是生产测试最难覆盖的部分。

## 本章小结

- RL 测试要覆盖算子、公式、组件、端到端、文档五层。
- 有限差分与差分测试是发现手写实现错误的最有效方法。
- 假绿很常见：断言可能编码 bug，必须独立验证。
- check_docs 抓引用/硬数字漂移。
- MiniVerl 的 250 个测试是教程可信度的基础。

## 延伸阅读

- `tests/`
- `check_docs.py`
- 下一章：MiniVerl 到生产 verl 的差距与迁移。
