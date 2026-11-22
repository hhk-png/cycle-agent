# 第 36 章 MiniVerl 的协议、配置与单控制器

## 36.1 DataProto

`mini_verl/protocol.py` 的 `DataProto` 与真实 verl 同构：

```python
DataProto(
    batch={"input_ids": ..., "response_mask": ...},
    non_tensor_batch={"prompts": ..., "uid": ...},
    meta_info={"global_num_tokens": ...},
)
```

支持：

- `__getitem__` / `select_idxs` / `reorder`
- `chunk` / `split` / `concat` / `union`
- `repeat(interleave=True)`
- `pad_to_divisor` / `unpad_dataproto`
- `pop` / `rename` / `to_dict` / `from_dict`

测试 `tests/test_protocol.py` 覆盖 10 个场景（含 padding mask 清零）。

### 与真实 verl 的差异

- 真实 verl 用 `TensorDict`，MiniVerl 用 dict[str, np.ndarray]；
- 真实 verl 有 `DataProtoFuture`、序列化、自动 padding 配置，MiniVerl 只有 `LocalFuture`；
- 真实 verl 的 `non_tensor_batch` 用 `np.fromiter(dtype=object)`，MiniVerl 用 `np.asarray(dtype=object)`；
- MiniVerl 的 `meta_info` 更简单。

## 36.2 配置

`mini_verl/config.py`：

- dataclass 嵌套；
- `PPOConfig.set_path("a.b.c", value)`；
- `apply_overrides(["a.b.c=value"])`；
- `validate()`；
- `to_yaml`、`save`、`copy`、`set_path`；模块级 `from_dict` 与 `load_config`（没有 `from_yaml` 方法）。

字段与 verl 对齐：

```python
config.actor_rollout_ref.actor.ppo_mini_batch_size
config.actor_rollout_ref.rollout.n
config.algorithm.adv_estimator
config.trainer.trainer_mode
```

差异：

- 没有 Hydra `${...}` 插值；
- 没有 grouped defaults；
- `data.dataset` 取代 parquet 路径；
- 部分后端/多模态字段未实现。

`test_config.py` 覆盖默认值、覆盖、YAML 往返、非法值、额外字段。

## 36.3 Dispatch

`single_controller/decorator.py`：

```python
class Dispatch(Enum):
    RANK_ZERO
    ONE_TO_ALL
    ALL_TO_ALL
    DP_COMPUTE
    DP_COMPUTE_PROTO

def register(dispatch_mode=..., sync_gradients=False):
    ...
```

`sync_gradients=True` 是 MiniVerl 的扩展：标记训练 RPC，group 在所有 worker 返回后平均梯度并 step。

## 36.4 LocalWorkerGroup

`single_controller/worker_group.py`：

- `ResourcePool`：每节点进程数；
- `LocalWorkerGroup`：创建 worker、设置 group、`__getattr__` 返回 dispatch wrapper；
- `_dispatch`：按模式切分/调用/收集；
- `_sync_gradients_and_step`：平均梯度 + finalize；
- `all_reduce_values` / `all_gather_values`：测试用 collective。

### DP_COMPUTE_PROTO 的实现

```python
padded_args, pad = _pad_args(args, world_size)
chunks = [_chunk_dataproto(arg, world_size) for arg in padded_args]
outputs = [worker.method(chunk[rank]) for rank, worker in enumerate(workers)]
if sync_gradients:
    _sync_gradients_and_step()
    outputs = _merge_post_step_metrics(outputs)
collected = DataProto.concat(outputs)
return unpad(collected, pad)
```

它与真实 verl 的 `dispatch_dp_compute_data_proto` + `collect_dp_compute_data_proto` 对应。

## 36.5 Worker

`Worker` 提供：

- `rank` / `world_size` / `local_rank` / `group`；
- `all_reduce` / `all_gather` / `broadcast`（同进程 no-op）；
- `fail_once()` 故障注入。

真实 worker 是 Ray actor；MiniVerl worker 是同进程对象。

## 36.6 控制器

`PPOTrainer` 是控制器：

```python
trainer = PPOTrainer(config).init()
trainer.fit()
```

它持有：

- actor_group；
- critic_group；
- rollout；
- checkpoint_engine；
- reward_manager；
- logger；
- checkpoint_manager。

`init()` 的步骤与真实 verl 类似：tokenizer → dataset → worker groups → rollout → sync → reward → logger → checkpoint。

## 36.7 一个最小 dispatch 例子

```python
class Echo(Worker):
    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def echo(self, data):
        return data

group = LocalWorkerGroup(ResourcePool.single_node(2), Echo)
out = group.echo(DataProto(batch={"x": np.arange(5)}))
# len(out) == 5，自动 padding/unpadding
```

## 36.8 为什么不用多进程

- 确定性；
- 调试方便；
- 无需 Ray；
- 测试快。

代价：

- 不能测真实 NCCL/序列化；
- 不能测并发故障；
- 性能不真实。

MiniVerl 用 `sync_gradients` 显式模拟 collective，用 `fail_once` 模拟故障，已经覆盖了大部分语义。

## 36.9 DataProto 的不变量与 padding 语义

MiniVerl 的 `DataProto` 只保证四条不变量，但每条都直接对应一个训练 bug：

1. **所有字段共享第一维。** `_validate()` 收集 `batch` 与 `non_tensor_batch` 的 `shape[0]`，不一致立即抛错。真实 verl 的 TensorDict 也会在拼接时暴露这类问题，但往往晚一步；提前失败能省一次分布式调试。
2. **`non_tensor_batch` 是 object 数组。** prompt 文本、`uid`、工具返回值都不是定长张量，必须走 object 数组；如果把 `uid` 放进 `batch`，`np.asarray` 会尝试转成字符串/数字矩阵，分组 key 就变了。
3. **`meta_info` 是浅拷贝。** `copy()` 复制 `dict`，但里面的可变对象仍共享；需要深层独立时手动 `deepcopy`。全局 token 数、温度、版本号这类标量没问题，嵌套字典要小心。
4. **`chunk` 要求整除。** 这与真实 verl 的 `torch.chunk` 不同：MiniVerl 先 `pad_to_divisor` 再 `chunk`，保证每个 rank 拿到完全相同的样本数。非整除时直接报错，逼调用方显式处理 padding，而不是让最后一个 rank 少拿。

padding 的实现值得单独看：

```python
indices = np.concatenate([np.arange(len(data)), np.arange(pad)])
padded = data.select_idxs(indices)          # 重复前 pad 条
for key in ("response_mask", "chosen_mask", "rejected_mask"):
    padded.batch[key][-pad:] = 0.0          # 副本不参与 loss
padded.batch["loss_mask"] = ...             # DPO 等 pair loss 用
padded.meta_info["_padding_size"] = pad
```

三个设计选择：

- **重复样本而不是零填充。** 这样所有字段形状合法，模型 forward 不会遇到全零的非法 token；副本靠 mask 清零。
- **清除 `response_mask`/`chosen_mask`/`rejected_mask`。** 如果只 pad 不改 mask，分母按原始 token 数算、分子却把副本算进去，梯度会被放大。`test_padding_clears_loss_masks` 固定了这一点。
- **额外写 `loss_mask`。** DPO 的 pair loss 不看 `response_mask`；没有 sample 级 mask，padding 副本会贡献 pair 梯度。

一个仍然存在的边界：padding 只清除了上面几个 mask，**没有**清除 `token_level_rewards`、`rm_scores` 等字段。如果某个 worker 在 padded batch 上直接对这些字段求均值，副本会污染指标。MiniVerl 的约定是“指标在 unpad 后的原始 batch 上算”；自定义 worker 时要遵守这个约定，或者自己把相关字段清零。

`concat` / `union` 的区别也常被混淆：

| 操作 | 要求 | 用途 |
|---|---|---|
| `concat` | 各字段第一维可不同；key 集合必须一致 | 把各 rank 的输出拼回一个 batch |
| `union` | 长度必须相同；同名字段值必须相等 | 把同一批样本的不同字段合并（如 worker 返回的 log-prob） |
| `repeat(interleave=True)` | `times >= 1` | `[s0,s0,s1,s1]`，GRPO 分组顺序 |
| `repeat(interleave=False)` | 同上 | `[s0,s1,s0,s1]`，按副本分块 |

`repeat` 的顺序会影响 `uid` 的排列，但不会改变分组；真正的问题是**忘记 repeat `uid`**。如果只 repeat 了 `input_ids` 而 `uid` 还是原长度，`_validate` 会报错；更隐蔽的是自定义字段长度对但内容没 repeat，GRPO 会把不同 prompt 分到同一组。

## 36.10 配置的优先级、校验与 no-op 字段

MiniVerl 的配置优先级从低到高：

```text
dataclass 默认值
  < YAML 文件
  < 命令行 key=value
  < 代码里直接 setattr
```

`load_config(path, overrides)` 先 `from_dict(yaml)`，再 `apply_overrides`，最后 `validate()`。三个实现细节：

1. **裸 override 也能用。** `main_ppo trainer.total_training_steps=30` 里第一个位置参数会被 argparse 当成 `config`；`load_config` 检测到“路径不存在且含 `=`”时把它挪到 overrides。`test_bare_override_in_config_position` 防回归。
2. **未知 key 立即报错。** `set_path` 逐层 `hasattr`，拼错 `actor.lr`（真实字段是 `actor.optim.lr`）不会被静默忽略。Hydra 的 struct 模式也类似，但 MiniVerl 在纯 Python 层就失败。
3. **类型由当前值推断。** `_coerce` 用现有字段的类型转换字符串；`true/false`、整数、浮点都能识别。代价是“把 int 字段写成 `1.5`”会被截断或报错，取决于 `_coerce` 的实现。

`validate()` 检查的是**跨字段约束**，不是单字段类型：

- `train_batch_size * rollout.n` 必须能被 `ppo_mini_batch_size` 整除；
- `ppo_mini_batch_size` 不能超过总序列数；
- `rollout.tensor_model_parallel_size` 不能超过 GPU 数；
- `loss_agg_mode` 必须在五种模式内；
- `adv_estimator` 必须在 MiniVerl 支持的集合内。

这些约束如果漏掉，训练会在第一个 batch 才崩，或者更糟——静默跑出一个错误的归一化。附录 A.9 列出了“接受但未生效”的字段；新增字段时要么接线，要么登记，不能让它变成静默 no-op。

## 36.11 五种 dispatch 模式的语义与选择

MiniVerl 的 `Dispatch` 有五个值，和真实 verl 的 `Dispatch` 枚举对应：

| 模式 | 参数如何分发 | 返回值 | 典型用途 |
|---|---|---|---|
| `RANK_ZERO` | 只调用 rank 0 | 单 worker 返回值 | 初始化、全局状态查询 |
| `ONE_TO_ALL` | 每个 worker 收到完整参数 | 列表（每 worker 一个） | `init_model`、`save_checkpoint` |
| `ALL_TO_ALL` | 若参数是长度为 world_size 的 list/tuple，按 rank 取；否则广播 | 列表 | 自定义 per-rank 参数 |
| `DP_COMPUTE` | 同 `ALL_TO_ALL`，但不要求 DataProto | 列表 | 标量/张量级 DP 计算 |
| `DP_COMPUTE_PROTO` | 所有 DataProto 参数先 padding，再 `chunk(world_size)` | 拼接并 unpad 的 DataProto | log-prob、values、actor update |

`DP_COMPUTE_PROTO` 的关键是“先 pad 再切”：

```python
padded_args, pad = _pad_args(args, world_size)
chunks = [_chunk_dataproto(arg, world_size) for arg in padded_args]
outputs = [worker.method(chunk[rank]) for rank, worker in enumerate(workers)]
collected = DataProto.concat(outputs)
return unpad_dataproto(collected, pad)
```

三个容易踩的坑：

- **长度为 world_size 的普通 list 会被 `ALL_TO_ALL`/`DP_COMPUTE` 拆开。** 如果你真的想广播一个长度为 world_size 的 list，需要包一层（例如放进 tuple 的另一个元素），否则每个 rank 只拿到一个元素。
- **多个 DataProto 参数必须同长度。** `_pad_args` 分别 padding，`pad` 取最大值；如果两个 DataProto 长度不同，unpad 后会错位。训练 RPC 里它们通常来自同一个 batch，但要避免手动拼不同来源的 DataProto。
- **`sync_gradients=True` 才触发 all-reduce + optimizer step。** 只标 `DP_COMPUTE_PROTO` 不会自动同步梯度；actor/critic 的训练方法必须显式带这个标志，否则每个 worker 各走各的 optimizer，参数会分叉。`test_dp_compute_per_rank_args` 与 `test_collectives` 覆盖了分发与同步两条路径。

## 36.12 同进程设计的确定性与故障注入

MiniVerl 不用多进程，换来三个可测试性：

1. **顺序确定。** worker 按 rank 顺序调用，没有调度器、没有网络重排；同一 seed 下，浮点累加顺序固定。
2. **故障可注入。** `Worker.fail_once()` 让下一次 RPC 抛错，测试可以验证重试/降级逻辑，而不需要真的杀进程。`test_failure_injection` 用它检查异常是否被正确传播。
3. **状态可检查。** 测试可以直接读 `worker.engine.model` 的参数、优化器 `m/v`、`checkpoint_engine.version`，不需要 RPC 反射。

代价也要说清楚：

- 没有真实 NCCL，测不出通信死锁、超时、拓扑错误；
- 没有 Ray actor 重启，测不出 actor 崩溃后的恢复；
- 没有序列化，测不出 `DataProto` 跨进程传输的 object 数组问题；
- 没有并发，测不出异步队列的竞态。

所以 MiniVerl 的测试策略是“语义正确性用单进程覆盖，分布式故障用文档/真实集群验证”。第 39 章会把这条边界展开成测试分类；第 31 章讲真实集群的故障树。

## 本章小结

- MiniVerl 的 DataProto/dispatch/worker group 与 verl 同构但更简单。
- `sync_gradients=True` 把 DP 梯度平均显式化。
- 配置用 dataclass + key=value，保留 verl 字段名；未知 key 与跨字段约束在启动前失败。
- DataProto 的四条不变量与 padding 的 mask 清零，是 DP 正确性的基础。
- 五种 dispatch 模式的分发/收集语义不同；训练 RPC 必须显式 `sync_gradients=True`。
- 同进程设计换取确定性、故障注入与状态可检查；真实 NCCL/Ray/序列化故障留给第 31 章与集群验证。
- 第 37～38 章会把这些组件接到 engine/rollout/trainer。

## 延伸阅读

- `mini_verl/protocol.py`
- `mini_verl/config.py`
- `mini_verl/single_controller/`
- 下一章：引擎、分片与并行模拟。
