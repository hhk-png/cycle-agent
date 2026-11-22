# 第 16 章 配置系统与进程组

## 16.1 配置对象

`mini_megatron/config.py` 定义三个 dataclass：

```python
@dataclass
class ModelConfig:
    vocab_size: int
    hidden_size: int
    num_layers: int
    num_attention_heads: int
    num_query_groups: int
    ...
    use_moe: bool = False
    activation_checkpointing: bool = False
    fp8_simulation: bool = False

@dataclass
class ParallelConfig:
    tensor_model_parallel_size: int = 1
    pipeline_model_parallel_size: int = 1
    context_parallel_size: int = 1
    expert_model_parallel_size: int = 1   # 1 或 dp_size（mini 的 ep=dp 约束）
    sequence_parallel: bool = False
    distributed_optimizer: bool = False
    distributed_optimizer_style: str = "zero1"  # zero1 | flat
    pipeline_schedule: str = "1f1b"

@dataclass
class TrainConfig:
    micro_batch_size: int
    global_batch_size: int
    seq_len: int
    max_steps: int
    lr: float
    ...
```

为什么用 dataclass 而不是 argparse 的几百个参数？

- 可序列化/可复现（JSON）；
- 校验集中；
- IDE/类型提示友好；
- 实验配置可以纳入版本控制。

生产 Megatron 的 `megatron/training/arguments.py` 参数更多（数百个），但核心
分层类似：模型、并行、训练、数据、优化器、日志。

## 16.2 校验规则

`ModelConfig.validate(parallel)` 检查：

```text
hidden % heads == 0
heads % query_groups == 0
heads % tp == 0
groups % tp == 0
hidden % tp == 0
vocab % tp == 0
ffn % tp == 0
max_seq_len % tp == 0（若 SP）
max_seq_len % cp == 0
layers % pp == 0
vocab 等能被 TP 整除
SP 与 CP 不同时开
tie embedding 需要 PP=1 且不能与 SP 同开
```

`ParallelConfig.validate(world)` 检查：

```text
world % (tp*pp*cp) == 0
SP 只在 tp>1 时有意义
ep ∈ {1, dp}                      # mini 把 EP 解释为整个 DP 维
pipeline_schedule ∈ {1f1b, gpipe}
```

`ExperimentConfig.validate` 还检查：

```text
ep > 1 ⇒ use_moe
num_experts % ep == 0
ep > 1 与 distributed_optimizer 不同时开   # 专家状态已分片，避免双重语义
```

`TrainConfig.validate` 检查：

```text
global_batch % (micro_batch * dp) == 0
seq_len <= max_seq_len
seq_len % cp == 0
seq_len % tp == 0（若 SP）
autocast 不与 PP 组合（mini 的限制）
```

### 16.2.1 哪些字段是“惰性”的

读配置时要注意，**字段存在不等于代码消费了它**：

| 字段 | 现状 |
|---|---|
| `gated_linear_unit` | 声明了但 `MLP` 按 `activation` 选择 SwiGLU/GELU，不读这个布尔值 |
| `use_grad_scaler` | 声明了但训练循环未使用（CPU 路径不需要；GPU 混合精度需自己接） |
| `ParallelConfig.data_parallel_size` | 属性会抛 `AttributeError`，DP 由运行时 `world_size/(tp*pp*cp)` 推导 |
| `TrainConfig.gradient_accumulation_steps` | 同上，由 `Trainer` 按 global/micro/DP 推导 |

这类字段在写实验配置时容易造成“我设了但没生效”的假象。新增字段时，最好在
`config.py` 的注释里标明“consumed by …”，并加一个测试断言它真的改变了行为。

**先校验再建模型**。如果 `hidden=4096, tp=3`，应该在初始化通信前就报错，而不是
在第一次 all-reduce 时才发现。

## 16.3 rank 布局

mini 使用：

```text
rank = ((pp_idx * dp_size + dp_idx) * cp_size + cp_idx) * tp_size + tp_idx
```

与 Megatron 默认布局的关键共同点：

- TP 最快变化，便于把 TP group 映射到连续 GPU/NVLink；
- PP 最慢变化，跨节点；
- DP/CP 在中间。

代码：

```python
tp_idx = rank % tp
cp_idx = (rank // tp) % cp
dp_idx = (rank // (tp * cp)) % dp
pp_idx = rank // (tp * cp * dp)
```

四组 group 的构造：

- TP：固定 `(pp, dp, cp)`，变化 `tp`；
- CP：固定 `(pp, dp, tp)`，变化 `cp`；
- DP：固定 `(pp, cp, tp)`，变化 `dp`；
- PP：固定 `(tp, cp, dp)`，变化 `pp`。

每个 rank 只保存自己所属的 group；`get_*_group()` 返回该对象。

## 16.4 通信抽象

`parallel/comm.py` 暴露统一 API：

```python
initialize_torch(backend, init_method)
initialize_portable(rank, world, inbox, outboxes)
new_group(ranks)
get_group_size(group)
get_group_rank(group)
all_reduce(tensor, op, group)
all_gather(list, tensor, group)
reduce_scatter_tensor(out, tensor, op, group)
broadcast(tensor, src, group)
send/recv/isend
barrier
destroy
```

### torch 后端

- Linux 训练默认；
- `new_group` 返回 `torch.distributed.ProcessGroup`；
- 集合通信用 NCCL/Gloo；
- p2p 用 `dist.send/recv/isend`。

### portable 后端

- 每个 rank 一个 `multiprocessing.Queue`；
- 集合通信用 `(kind, group_key, sequence)` 匹配；
- p2p 用 `(tag, sequence, src)` 匹配；
- 数据通过 pickle/CPU 拷贝；
- 仅用于 Windows/教学，性能低。

portable 的 sequence 机制很关键：同一个 group 的所有 rank 必须以相同顺序调用
collective，序列号才能对齐。这也是生产集合通信的隐含约束：**所有 rank 必须
走相同的代码路径**，不能有 rank 因条件分支跳过一次 all-reduce。

## 16.5 group 的创建顺序

`initialize_model_parallel` 用固定顺序的循环创建所有 group：

```text
for pp: for dp: for cp: create TP group
for pp: for dp: for tp: create CP group
for pp: for cp: for tp: create DP group
for tp: for cp: for dp: create PP group
if ep > 1: for pp: for cp: for tp: create EP group   # 排在 PP 之后
```

为什么顺序重要：

- torch.distributed 的 `new_group` 是集合操作，所有 rank 必须以相同顺序调用；
- 如果某个 rank 在循环里提前 `continue`，会死锁；
- 生产 Megatron 用更复杂的 order 字符串，但“所有 rank 同序”不变。

EP 组在 PP 组之后创建，rank 集合与 DP 组相同（`ep = dp` 时），但必须是
**独立的 group 对象**：portable 后端的消息 key 含组序号，共用对象会把 DP
梯度 all-reduce 与 EP all-to-all 的消息串在一起。

portable 后端**也有同样的顺序约束**：`new_group` 用全局 `_GROUP_COUNTER`
把组序号编进消息 key（`("group", counter, tuple(sorted(ranks)))`），避免相同 rank 集合的
CP/DP 组串线。如果两个 rank 创建组的顺序不同，counter 就会错位，集合通信
会匹配到错误的组。因此 mini 的 portable 后端不是“随便建组都行”，只是把
torch 的集合建组换成了本地对象 + 全局计数器。

### 16.5.1 初始化流与 rank 布局

group 建好之后、模型构造之前，`set_model_parallel_init_seed(seed)` 会重置一条
确定性初始化流。它利用 rank 布局做两件事：

```text
sharded 权重：seed + f(tp_rank, pp_rank, layer_counter)
replicated 参数：seed + f(pp_rank, layer_counter)   # 不含 tp_rank
```

- `tp_rank` 进入分片流：不同 TP rank 得到不同切片；
- `pp_rank` 进入两条流：不同 stage 的对应层不重复；
- `dp_rank` / `cp_rank` 不进入：同一 `(tp, pp)` 的 DP/CP 副本权重必须相同。

这和第 17 章的层初始化、附录 G 的 RNG 语义是同一件事的三个视角。

## 16.6 全局 rank 与局部 rank

这是 PP/DP 组合最容易错的地方：

```python
pp_rank = comm.get_group_rank(pp_group)       # 0..pp_size-1
global_rank = comm.get_global_rank(pp_group, pp_rank)
```

- p2p 的 `src/dst` 参数是**全局 rank**；
- `get_pipeline_model_parallel_rank()` 返回**局部 rank**；
- 不能用 `local_rank + 1` 直接当 dst，除非 PP group 的 rank 恰好连续。

mini 的 `PipelineCommunicator` 预先算 `global_ranks`，所有 p2p 用
`self._global(local)`。这个修复解决了一个真实的 4 卡死锁：`PP=2, DP=2` 时
PP group 是 `[0,2]`，局部 rank 0 的下一跳是 global 2，而不是 1。

## 16.7 设备与 dtype

- `Trainer` 把 stage `.to(device)`；
- 数据 batch 在 `train_step/evaluate` 中 `.to(device)`；
- `RotaryEmbedding` 的 cache 会随 query device 重建；
- p2p 的 buffer 在 `PipelineCommunicator._device()` 中选择 CUDA/CPU；
- 生产实现需要显式传递 dtype/shape，避免 autocast 下 stage 间 dtype 不一致。

mini 限制 `autocast + PP>1`，因为 p2p 的 dtype 协商被省略了。生产 Megatron 会
在 stage 间传递 metadata，或要求统一的 compute dtype。

## 16.8 本章小结

- 配置用 dataclass + JSON，先校验再建模型。
- rank 布局决定了 group；所有 rank 必须以相同顺序创建 group。
- 通信抽象让 torch/portable 后端可切换。
- p2p 用全局 rank，collective 用 group；两者不能混。
- group 的 collective 调用顺序必须一致。

下一章逐行拆解张量并行层。
