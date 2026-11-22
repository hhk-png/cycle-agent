# 附录 M 引擎后端与扩展：BaseEngine、ShardSpec 与 EngineRegistry

本附录回答一个在正文里被频繁提到、但没有展开的问题：verl 如何把 FSDP、Megatron、Automodel、VeOmni、TorchTitan、MindSpeed 这些完全不同的训练后端，藏到同一套 PPO 主循环后面。

## M.1 为什么需要后端抽象

RL 训练循环只关心四件事：

1. 给定 batch，算 log-prob / values / loss；
2. 反向后得到梯度并更新参数；
3. 把参数导出成推理引擎能加载的布局；
4. 保存/恢复分片 checkpoint。

如果每个后端都直接暴露 `torch.nn.Module`、`FSDP`、`Megatron` 的 API，controller 就会被后端细节污染。verl 的做法是定义一层 `BaseEngine`：controller 只调用引擎方法，不关心底层是 DTensor 还是 Megatron 的 ShardedTensor。

## M.2 BaseEngine 的契约

`verl/workers/engine/base.py` 的核心方法可以分成四组：

### 计算

```python
def forward_backward_batch(self, data, loss_fn, forward_only=False): ...
def infer_batch(self, data, loss_fn): ...
```

输入是 `DataProto` 拆出来的 batch，输出是 loss/metrics；引擎负责 micro-batch、梯度累积、混合精度与重计算。

### 参数导出

```python
def get_per_tensor_param(self): ...
def get_per_tensor_param_shard(self, **kwargs): ...
def get_per_tensor_param_delta_shard(self, **kwargs): ...
```

- 全量导出：给 naive/nccl 同步；
- 分片导出：给 checkpoint engine 按 `ShardSpec` gather；
- delta 导出：只给变化元素，给 delta 后端。

### 生命周期

```python
def train_mode(self): ...
def eval_mode(self): ...
def to(self, device, model=True, optimizer=True, grad=True): ...
def initialize(self): ...
```

包括 sleep/wake、参数/优化器卸载、CUDA graph 捕获等。

### checkpoint

```python
def save_checkpoint(self, local_path, hdfs_path=None, global_step=0,
                    max_ckpt_to_keep=None, **kwargs): ...
def load_checkpoint(self, local_path, hdfs_path=None,
                    del_local_after_load=True, **kwargs): ...
```

`BaseEngine` **没有** `state_dict()`；状态读写走上面的 checkpoint API。MiniVerl 的 `save_checkpoint(path, prefix)` / `state_dict()` 是教学简化版，不要当成真实签名。分片格式由后端决定；engine 负责写元数据（world size、mesh、param name → shard）。计算接口的参数名是 `loss_function`（不是 `loss_fn`）。

## M.3 ShardSpec：把布局说清楚

`verl/workers/engine/spec.py` 的 `ShardSpec` 描述“一个本地 shard 在全局参数里的位置”：

- `full_shape`：完整参数的形状；
- `mesh` / `placements`：DTensor 的 DeviceMesh 与 Shard/Replicate；
- `place` / `gather_group` / `contributes`：这个 shard 属于哪个 gather 组、是否贡献数据；
- `to_hf_chunk` / `hf_slots`：如何把 fused 参数（QKV、gate/up）映射到 HF 名称。

没有这层描述，checkpoint engine 就只能“猜”每个 shard 该拼到哪里。猜错的后果不是报错，而是权重顺序错、输出乱码。

## M.4 EngineRegistry 与配置选择

`EngineRegistry` 根据 `(model_type, backend, device)` 选择实现。配置里通常写：

```yaml
actor_rollout_ref:
  actor:
    strategy: fsdp2        # 或 megatron
    fsdp_config:
      param_offload: false
      optimizer_offload: false
      reshard_after_forward: true
```

FSDP 的 offload 开关在 `fsdp_config:` 子树，不在 `engine:`；写到错误子树不会报错，但不会生效。Megatron 的字段在 `megatron:` 子树；不同版本的 MCore 字段会变，升级时以 `verl/workers/config/` 下的 dataclass 为准。

后端矩阵：

| 后端 | 路径 | 特点 | 适用 |
|---|---|---|---|
| FSDP/FSDP2 | `verl/workers/engine/fsdp/` | DTensor、逐参数分片 | 7B～70B dense |
| Megatron | `verl/workers/engine/megatron/` | TP/PP/CP/EP、MoE | 百 B～千 B |
| Automodel | `verl/workers/engine/automodel/` | 自动选择/转换 | 快速接新模型 |
| VeOmni | `verl/workers/engine/veomni/` | 多模态/统一并行 | VLM/Omni |
| TorchTitan | `verl/workers/engine/torchtitan/` | PyTorch 原生并行 | 研究/新硬件 |
| MindSpeed | `verl/workers/engine/mindspeed/` | Ascend NPU | 华为栈 |

## M.5 新增一个后端要做什么

1. **实现 BaseEngine 子类**：至少覆盖 forward/backward、optimizer、参数导出、checkpoint；
2. **实现 ShardSpec**：把本地 shard 映射到全局 HF 坐标；
3. **注册到 EngineRegistry**：给出 strategy 名称与选择条件；
4. **补配置 dataclass**：字段要能被 Hydra 覆盖与校验；
5. **补 smoke test**：单卡前向、单步反向、保存/加载 roundtrip、权重导出 roundtrip；
6. **补差分测试**：与参考实现（dense NumPy/PyTorch）比较 loss 与梯度；
7. **补文档**：哪些并行组合合法、哪些 dtype 支持、哪些模型结构不支持。

最小的 smoke test 应该回答：

```text
同一 batch，同一 seed：
  engine.forward_backward_batch -> loss
  参考 dense 实现 -> loss
  两者 allclose?
  导出权重 -> 重新加载 -> 输出一致?
```

## M.6 常见失败模式

| 现象 | 原因 | 排查 |
|---|---|---|
| loss 与参考实现差很多 | mask/归一化/dtype 不一致 | 固定 batch，逐层对比 |
| 导出权重后输出乱码 | ShardSpec 映射错 | 对比 HF logits，用 model merger 校验 |
| 多卡 loss 不降 | 梯度 all-reduce 被 no_sync 跳过 | 检查最后一个 micro-batch 是否同步 |
| checkpoint 无法跨 world size | 分片元数据缺失 | 保存 mesh/world size，加载时 reshard |
| NPU 上 NaN | FP8/BF16 支持差异 | 关 FP8，逐层查 dtype |
| 新模型加载失败 | 参数名映射缺失 | 补 `to_hf_chunk`/`hf_slots` |

## M.7 MiniVerl 的对应实现

MiniVerl 没有真正的后端切换，但保留了同样的分层：

- `mini_verl/engine.py::BaseEngine`：forward/backward、optimizer、checkpoint 契约；
- `FSDPEngine`：dense replica + 梯度 all-reduce；
- `MegatronEngine`：同样的 dense 数学，但导出时可用 TP 布局；
- `EngineRegistry.new`：按 strategy 选择；
- `mini_verl/sharding.py::tp_shard_state_dict/tp_gather_state_dict`：ShardSpec 的极简版；
- `mini_verl/parallel.py`：TP/PP/MoE/Ulysses 的数值模拟。

读真实 verl 时，可以把 `BaseEngine` 的方法名与 MiniVerl 一一对应，再看每个后端如何实现分片与导出。

## 本章小结

- `BaseEngine` 把计算、导出、生命周期、checkpoint 四组能力抽象出来，controller 不感知后端。
- `ShardSpec` 是分片与 HF 坐标之间的契约，缺了它 checkpoint engine 只能猜。
- `EngineRegistry` 按 strategy/model/device 选择实现；新增后端要补配置、测试与文档。
- 后端正确性只能靠“与 dense 参考实现对比 + 导出 roundtrip”验证，不能只看能跑。
- MiniVerl 用 dense replica 模拟同一套契约，便于逐行对照。

## 延伸阅读

- `verl/workers/engine/base.py`
- `verl/workers/engine/spec.py`
- `verl/workers/engine/fsdp/`
- `verl/workers/engine/megatron/`
- `verl/workers/config/`
