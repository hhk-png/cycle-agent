# 附录 F mini-megatron API 与命令

## F.1 包结构

```python
from mini_megatron.config import ExperimentConfig, ModelConfig, ParallelConfig, TrainConfig
from mini_megatron.parallel.state import initialize_model_parallel, destroy_model_parallel
from mini_megatron.parallel.layers import set_model_parallel_init_seed
from mini_megatron.parallel.transformer import TransformerStage, GPTModel
from mini_megatron.pipeline.schedules import build_schedule
from mini_megatron.pipeline.interleaved import VirtualPipelineLayout, vpp_bubble_ratio
from mini_megatron.pipeline.interleaved_exec import InterleavedGPipeExecutor
from mini_megatron.parallel.expert_parallel import ExpertParallelMoELayer
from mini_megatron.parallel.mla import MLAttention
from mini_megatron.parallel.ulysses import ulysses_attention, ulysses_layout_roundtrip
from mini_megatron.training import Trainer
from mini_megatron.generation import generate
from mini_megatron.generation_kv import generate_with_cache
from mini_megatron.checkpoint import save_checkpoint, load_checkpoint
from mini_megatron.checkpoint_reshard import (
    full_state_from_stage_states,
    load_full_state_from_checkpoint,
    load_full_optimizer_from_checkpoint,
    merge_optimizer_state_by_name,
    optimizer_state_by_name,
    optimizer_state_dict_from_by_name,
    reshard_full_state,
    split_optimizer_state_by_name,
)
from mini_megatron.sharding import make_stage_state, load_stage_from_full
from mini_megatron.analysis import estimate, parameter_count, kv_cache_bytes
from mini_megatron.data.packing import PackedSequenceDataset, pack_documents
```

## F.2 最小训练示例

```python
import torch
from mini_megatron.config import ExperimentConfig
from mini_megatron.parallel.state import initialize_model_parallel
from mini_megatron.parallel.transformer import TransformerStage
from mini_megatron.training import Trainer

cfg = ExperimentConfig.from_json("configs/tiny.json")
cfg.train.max_steps = 3
cfg.train.checkpoint_dir = ""
initialize_model_parallel(1, 1, 1)
# 建模前重置 TP 感知的初始化流；TP>1 时各 rank 分片权重才会不同。
set_model_parallel_init_seed(cfg.train.seed)
stage = TransformerStage(cfg.model, cfg.parallel)
trainer = Trainer(cfg, stage=stage)
history = trainer.fit(save_final=False)
print(history[-1])
```

## F.3 自定义并行

```python
from mini_megatron.config import ParallelConfig

cfg.parallel = ParallelConfig(
    tensor_model_parallel_size=2,
    pipeline_model_parallel_size=1,
    context_parallel_size=1,
    sequence_parallel=True,
)
```

必须由启动器设置 world_size/env；单进程不能伪造 TP=2。

专家并行（要求 `ep == dp`）：

```python
cfg.model.use_moe = True
cfg.model.num_experts = 4
cfg.parallel = ParallelConfig(expert_model_parallel_size=2)
initialize_model_parallel(1, 1, 1, expert_model_parallel_size=2)
```

可执行 VPP：

```python
from mini_megatron.pipeline.interleaved_exec import InterleavedGPipeExecutor

executor = InterleavedGPipeExecutor(
    cfg.model, cfg.parallel, cfg.train, chunks_per_rank=2
)
loss, aux = executor.forward_backward(micro_batches)
```

VPP 执行器不接 `Trainer`，但可以像普通模型一样 `zero_grad()`/`train()`/
`parameters()`。

## F.4 命令行

```bash
python -m mini_megatron.cli train --config configs/tiny.json
python -m mini_megatron.cli info --config configs/tiny.json
python -m mini_megatron.cli benchmark --config configs/tiny.json --steps 5
python -m mini_megatron.cli generate --config configs/tiny.json \
  --checkpoint checkpoints/tiny --prompt 1,2,3,4 --max-new-tokens 8
python -m mini_megatron.cli estimate --config configs/tiny.json \
  --tokens 1e12 --gpu h100 --mfu 0.4
python -m mini_megatron.pipeline.interleaved --pp 4 --chunks-per-rank 2 \
  --layers 16 --microbatches 8
python tests/parallel_vpp.py --world-size 2 --out vpp.json
```

## F.5 多进程

```bash
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json \
  --steps 4 --checkpoint-dir checkpoints/pp --auto-resume
# 专家并行
python scripts/run_distributed.py --world-size 2 --config configs/tiny_ep2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_ep2.json
# 只让 rank 0 写训练记录（字段与单进程 --jsonl 相同）
python scripts/run_distributed.py --world-size 2 --config configs/tiny_dp2.json \
  --jsonl .verify_tmp/dp2.jsonl
```

`--jsonl` 会透传给 CLI 的 `train`（也适用于 `--mode train`）；它不改变
训练语义，只把 rank 0 的逐步 `loss/grad_norm/lr` 追加到文件，便于做第 51
章的迁移对照。

Linux torchrun：

```bash
torchrun --standalone --nproc_per_node=2 -m mini_megatron.cli train \
  --config configs/tiny_tp2.json
```

## F.6 检查点

```python
trainer.save("checkpoints/exp", step=10)
step = trainer.resume("checkpoints/exp")
```

目录：

```text
checkpoints/exp/
  latest
  step_000010/
    metadata.json
    rank_00000.pt
    ...
```

非 ZeRO AdamW 状态重分片：

```python
from mini_megatron.checkpoint_reshard import (
    load_full_optimizer_from_checkpoint,
    optimizer_state_dict_from_by_name,
    split_optimizer_state_by_name,
)

full_opt, template = load_full_optimizer_from_checkpoint(
    "checkpoints/exp/step_000010", source_cfg, dp_size=2
)
local = split_optimizer_state_by_name(
    full_opt, target_stage_states, tp_size=target_tp
)[(pp_rank, tp_rank)]
state_dict = optimizer_state_dict_from_by_name(
    local, target_local_model_state, template
)
```

ZeRO/EP 状态会抛 `NotImplementedError`，按原布局恢复后再迁移。

## F.7 生成

```python
from mini_megatron.generation import generate

out = generate(
    stage,
    torch.tensor([[1, 2, 3]]),
    max_new_tokens=8,
    temperature=0.0,
)
```

PP>1/CP>1 会抛 `NotImplementedError`；TP>1 会 gather 词表 logits。

KV cache 版本：

```python
from mini_megatron.generation_kv import generate_with_cache

out = generate_with_cache(
    stage,
    torch.tensor([[1, 2, 3]]),
    max_new_tokens=8,
    temperature=0.0,
)
```

`stage` 必须是 PP=1、CP=1、`sequence_parallel=False` 的推理 stage。cache 的
正确性由 `test_generation_kv.py` 与全量前向逐步对比。

## F.7.1 MLA 独立模块

```python
from mini_megatron.parallel.mla import MLAttention

m = MLAttention(
    hidden_size=64, num_heads=8,
    q_lora_rank=None,          # None = 直接 q_proj；整数 = 低秩 query
    kv_lora_rank=32,
    qk_nope_head_dim=16,
    qk_rope_head_dim=8,
    v_head_dim=16,
    max_seq_len=128,
)
out = m(x, position_ids=pos)                       # 训练/全量前向
out, (c_kv, k_pe) = m(x, position_ids=pos, use_cache=True)
out_step, cache = m(x_new, position_ids=pos_new,
                    past_key_value=cache, use_cache=True)   # 增量解码
```

- cache 是 `(c_kv, k_pe)`，不是完整 K/V；
- 只支持单进程；TP/CP/FP8/吸收推理未实现；
- 测试：`tests/unit/test_mla.py`（6 项）；演示：`scripts/mla_demo.py`。

## F.8 测试

```bash
python -m pytest -q
python tests/parallel_equiv.py --case tp --world-size 2 --out tp.json
python tests/parallel_ep.py --world-size 2 --out ep.json
python tests/parallel_vpp.py --world-size 2 --out vpp.json
python scripts/verify.py
python scripts/check_docs.py   # 章号/附录/SUMMARY/链接一致性
```

## F.9 扩展点

- 新层：`parallel/layers.py` + `sharding.py` + 等价性测试；
- 新模型：`parallel/transformer.py` 的 block/embedding；
- 新 schedule：`pipeline/schedules.py`；VPP 规划：`pipeline/interleaved.py`；
  VPP 执行：`pipeline/interleaved_exec.py`；
- 新并行维度：`state.py` + `comm.py` + `adamw.py` 的梯度同步语义
  （EP 是完整示例）；
- 新优化器：`optim/adamw.py`；
- 新通信后端：`parallel/comm.py`（需实现 all-reduce/all-gather/all-to-all/
  send/recv）；
- 新推理优化：`generation_kv.py`（分页/连续批处理/prefix cache）；
- 新数据管线：`data/packing.py`（接入 attention 的 document mask）；
- 新重分片：`checkpoint_reshard.py`（ZeRO/FSDP2/EP 仍待实现）。

---

上一附录：[附录 E 参考文献与链接](E-references.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 G 随机性、确定性与复现手册](G-determinism-rng.md)
