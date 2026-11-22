# 第 9 章 DataProto 与 TensorDict：批数据的契约

## 9.1 为什么不用一个普通 dict

在单进程脚本里，一个 batch 就是一个 dict：

```python
batch = {"input_ids": ids, "attention_mask": mask, "rewards": rewards}
```

但在分布式 RL 中，这个 dict 必须满足额外约束：

- 所有张量共享同一个 batch 维；
- 非张量字段（prompt 文本、uid、data_source）也要随 batch 切分；
- 切分后能无损 concat；
- padding 后能去 padding；
- 能在 controller 与 Ray actor 之间序列化；
- 能携带 meta_info（温度、版本、是否验证）。

verl 的 `DataProto` 就是为此设计的。

## 9.2 三部分结构

`verl/protocol.py` 中的 `DataProto`：

```python
@dataclass
class DataProto:
    batch: TensorDict
    non_tensor_batch: dict[str, np.ndarray]
    meta_info: dict[str, Any]
```

### batch

`batch` 是一个 `TensorDict`，所有张量有相同的 batch size。典型字段：

```text
input_ids          (B, T)
attention_mask     (B, T)
position_ids       (B, T)
responses          (B, L)
response_mask      (B, L)
old_log_probs      (B, L)
ref_log_prob       (B, L)
values             (B, L)
advantages         (B, L)
returns            (B, L)
token_level_rewards(B, L)
rm_scores          (B,)
rollout_log_probs  (B, L)
```

### non_tensor_batch

`non_tensor_batch` 是 `np.ndarray(dtype=object)`，第一维是 batch。典型字段：

```text
raw_prompt      # chat messages / 文本
data_source     # 奖励函数 key
uid             # GRPO 分组 key
index           # 数据集索引
tools_kwargs    # 工具参数
response_text   # 解码后的响应
```

### meta_info

`meta_info` 是任意 Python 元数据：

```text
temperature
global_steps
rollout_version
micro_batch_size
is_validate
```

它不参与 batch 切分，但会随 DataProto 复制。

## 9.3 切分与合并

### chunk

```python
chunks = data.chunk(world_size)
```

要求 `len(data) % world_size == 0`。verl 的 dispatch 在切分前会先 padding：

```python
data, pad = pad_dataproto_to_divisor(data, world_size)
chunks = data.chunk(world_size)
...
result = DataProto.concat(outputs)
result = unpad_dataproto(result, pad)
```

### concat

`DataProto.concat` 按第一维拼接所有张量与非张量字段，meta_info 取第一个并合并缺失键。它要求所有输入的字段集合一致。

### union

`union` 合并两个字段集合不同的 DataProto，但要求 batch 维一致，且同名字段值相等。它适合“一个 worker 返回 log_probs，另一个返回 values”的场景。

### repeat

`repeat(n, interleave=True)`：

- `interleave=True`：每个样本连续重复 n 次（`s0,s0,s1,s1,...`），适合 GRPO 分组；
- `interleave=False`：先铺一遍再重复（`s0,s1,...,s0,s1,...`），适合某些 batch 结构。

MiniVerl 的 `DataProto.repeat` 用 `np.repeat`/`np.tile` 实现，并保留 non_tensor 字段。

### select / reorder

`select_idxs(indices)` 按索引取子集并返回新对象；`reorder(indices)` **原地**重排（真实 verl 的 docstring 明确写 "this operation is in-place"），不返回新 batch。MiniVerl 的 `reorder` 为了链式调用返回了新对象，这是与上游的一个语义差异。长度均衡、DAPO 过滤、采样排序都依赖这两个操作。

## 9.4 padding 的语义

padding 不是“补零”这么简单：

- `input_ids` 补 pad token；
- `attention_mask` 补 0；
- `response_mask` 补 0；
- `old_log_probs` 等补 0；
- `non_tensor_batch` 复制样本或补占位。

verl 的 `pad_dataproto_to_divisor` 会重复前面的样本，并记录 `_padding_size`；dispatch 收集后按这个数量裁掉尾部。MiniVerl 的 `pad_dataproto_to_divisor` 也把 `_padding_size` 放进 `meta_info`，并且会把重复行的 `response_mask` / `chosen_mask` / `rejected_mask` 清零，同时写一个样本级 `loss_mask`。

**关键**：padding 的样本会参与前向计算，但 mask=0，所以不应该影响损失。如果 mask 错了，padding 会悄悄改变梯度。对 DPO 这种“每对样本一个 loss”的目标，光清 token mask 不够，还要用 `loss_mask` 把重复的整对样本排除。

## 9.5 序列化

verl 的 DataProto 支持 pickle / Ray 序列化。v0.9.1 还提供 `serialize_tensordict` 等函数，把张量转成字节，以降低跨进程开销。

工程上需要注意：

- `non_tensor_batch` 里的对象必须可 pickle；
- 大张量跨 Ray 传输有拷贝成本；
- `meta_info` 不要放不可序列化的对象（如打开的文件）；
- 异步场景要记录 `rollout_version` 等元信息。

## 9.6 DataProtoFuture

`DataProtoFuture` 表示“尚未完成的 RPC 结果”。它允许 controller 在等待某个 worker 的同时继续调度其他 worker。`dispatch` 层会在需要时 `ray.get`。

MiniVerl 用 `LocalFuture` 简化了这一层：`execute_all_async` 返回一个已完成的 future。它不提供真正的并行，但保留了 API 形状。

## 9.7 一个完整的例子

```python
from mini_verl.protocol import DataProto
import numpy as np

data = DataProto(
    batch={
        "input_ids": np.arange(12).reshape(6, 2),
        "response_mask": np.ones((6, 2)),
    },
    non_tensor_batch={
        "uid": np.arange(6),
        "text": np.asarray([f"s{i}" for i in range(6)], dtype=object),
    },
    meta_info={"source": "demo"},
)

chunks = data.chunk(3)
restored = DataProto.concat(chunks)
assert np.array_equal(restored.batch["input_ids"], data.batch["input_ids"])

repeated = data.repeat(2, interleave=True)
print(repeated.non_tensor_batch["text"].tolist())
# ['s0','s0','s1','s1',...]

padded, pad = data.pad_to_divisor(4)
print(len(padded), pad)
```

运行 `examples/04_dataproto_tour.py` 可以看到完整输出。

## 9.8 常见错误

1. **字段 batch 维不一致。** 构造时就应该报错；MiniVerl 的 `_validate` 会检查。
2. **chunk 前忘记 padding。** `len(data) % world_size != 0` 时直接切会丢样本。
3. **concat 顺序错。** 收集结果必须按 rank 顺序拼接，否则样本与 uid 错位。
4. **non_tensor 字段没有随 batch 切分。** 只切 tensor 会导致 prompt 与 response 对不上。
5. **meta_info 被意外覆盖。** 例如 padding 的 `_padding_size` 在 concat 后要保留。
6. **异步场景忘记版本号。** 没有 `rollout_version` 就无法做 staleness 控制。

## 9.9 MiniVerl 对照

`mini_verl/protocol.py` 实现了：

- `DataProto.__init__` / `_validate`
- `__getitem__` / `select_idxs` / `reorder` / `slice`
- `chunk` / `split` / `concat` / `union`
- `repeat`
- `pad_to_divisor` / `unpad_dataproto`
- `pop` / `rename` / `to_dict` / `from_dict`

测试 `tests/test_protocol.py` 覆盖了 10 个场景，包括 auto padding、padding mask 清零与 non_tensor 字段。

## 本章小结

- DataProto = TensorDict + 非张量批 + meta_info，是 controller 与 worker 之间的数据契约。
- chunk/concat/padding 必须保持所有字段对齐；non_tensor 字段不能漏。
- repeat 的 interleave 语义决定 GRPO 分组是否正确。
- 序列化与 meta_info 是异步/分布式调试的关键线索。

## 延伸阅读

- `verl/protocol.py`
- `verl/single_controller/base/decorator.py` 中的 `_split_args_kwargs_data_proto`
- 下一章：单控制器、WorkerGroup 与 Ray 执行模型。
