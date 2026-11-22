# 附录 M 源码阅读实验手册

> 目标：把“读 Megatron-LM 源码”从漫无目的的浏览，变成 13 个有输入、有观察、
> 有验收问题的实验。每个实验都能在普通开发机（不需要 GPU）上完成大部分步骤；
> 需要多卡的实验会明确标注。

## M.0 怎么用这份手册

- 先准备一个 Megatron-LM checkout（`git clone` 或解压 release tarball），
  记录 `git rev-parse --short HEAD` 与 `git describe --tags --always`。
- 每个实验按“目标 → 命令 → 观察 → 验收问题”做。**命令只是入口**，不要背
  路径；版本变化时用 `grep -R` 重新定位。
- 建议建一个 `notes/source-reading.md`，每个实验写 5–10 行：文件/函数、输入
  输出形状、集合通信顺序、删掉某行会坏什么。
- 需要运行的实验，先在 CPU/单卡确认语义，再上多卡。多卡实验失败时先查
  进程组初始化顺序与全局/局部 rank（第 16 章）。

## M.1 实验 1：版本锚点与目录地图

**目标**：知道自己读的是哪个版本，以及库/训练脚本/示例的边界。

```bash
git rev-parse --short HEAD
git describe --tags --always
find megatron/core -maxdepth 2 -type d | sort
ls megatron/training examples | head -40
```

**观察**：

- `megatron/core/` 是库；`megatron/training/`、`examples/` 是入口；
- `megatron/core/transformer/`、`pipeline_parallel/`、`distributed/`、
  `inference/` 是最值得先读的四个目录；
- 记录 `parallel_state.py`、`transformer_config.py`、`arguments.py` 的路径。

**验收问题**：

1. 你要改“梯度 all-reduce 的时机”，应该先看哪个目录？
2. 你要加一个新模型结构，应该改 `core/` 还是 `examples/`？
3. 如果 release note 说“支持 X”，你先在哪两个文件里找开关？

对应章节：29.1–29.2。

## M.2 实验 2：并行组与 rank 布局

**目标**：把 TP/PP/DP/CP/EP 的组织从名词变成可打印的 rank 集合。

```bash
grep -R "def initialize_model_parallel" -n megatron/core
grep -R "get_.*_model_parallel_group\|get_data_parallel_group" -n \
  megatron/core/parallel_state.py | head -30
```

写一个最小脚本（在 8 卡上用 `torchrun` 启动）：

```python
import torch.distributed as dist
from megatron.core import parallel_state

dist.init_process_group("nccl")
parallel_state.initialize_model_parallel(
    tensor_model_parallel_size=2,
    pipeline_model_parallel_size=2,
    context_parallel_size=1,
    expert_model_parallel_size=1,
)
for name, getter in [
    ("tp", parallel_state.get_tensor_model_parallel_group),
    ("pp", parallel_state.get_pipeline_model_parallel_group),
    ("dp", parallel_state.get_data_parallel_group),
    ("cp", parallel_state.get_context_parallel_group),
]:
    group = getter()
    ranks = dist.get_process_group_ranks(group) if hasattr(dist, "get_process_group_ranks") else "n/a"
    print(dist.get_rank(), name, ranks)
```

**观察**：

- TP 是否连续、PP 是否最慢变化、DP/CP 是否在中间；
- 同一个 rank 属于哪些组，哪些组是重叠的；
- EP 组与 DP 组的 rank 集合是否相同、是否是不同对象。

**验收问题**：

1. `PP=2, DP=2` 时 PP group 是 `[0,2]` 还是 `[0,1]`？p2p 的 `src/dst` 用
   局部还是全局 rank？
2. 为什么所有 rank 必须以相同顺序调用 `new_group`？
3. 如果 EP 与 DP 的 rank 集合相同，为什么不能复用同一个 group 对象？

对应章节：16.3–16.6、9.6。

## M.3 实验 3：TP 线性层的前向与反向

**目标**：看清 Column/Row Parallel 的通信点，以及 `grad_input` 为什么需要
all-reduce。

```bash
grep -R "class ColumnParallelLinear\|class RowParallelLinear" -n megatron/core/tensor_parallel
grep -R "linear_with_grad_accumulation_and_async_allreduce" -n megatron/core | head
```

**观察**：

- Column：权重按输出维切，前向通常不 gather；反向 `grad_input` 要跨 TP 求和；
- Row：前向 all-reduce（或 SP 的 reduce-scatter）；反向 `grad_input` 本地；
- 异步 all-reduce/通信重叠的入口在哪个函数；
- bias/dropout 的加法顺序（`bias_dropout_add`）如何影响数值。

**验收问题**：

1. 只删掉 Column 反向的 `grad_input` all-reduce，loss 会变吗？梯度呢？
2. 为什么 Row 的前向 all-reduce 可以和 bias 融合？
3. 序列并行打开后，这两层的通信原语分别变成什么？

对应章节：06、17、7.3。

## M.4 实验 4：序列并行的布局变换

**目标**：理解 SP 不是“再切一刀”，而是改变激活布局。

```bash
grep -R "scatter_to_sequence_parallel_region\|gather_from_sequence_parallel_region\|reduce_scatter_to_sequence_parallel_region" \
  -n megatron/core/tensor_parallel
```

**观察**：

- 哪些层在 `[b, s, h]` 与 `[b, s/t, h]` 之间转换；
- LayerNorm/dropout 在哪种布局上计算；
- Column 前向的 all-gather 与反向的 split/reduce-scatter 如何配对；
- 与 CP 同时打开时，序列网格是几维。

**验收问题**：

1. SP 省的是哪部分激活？为什么不减少 GEMM 的 FLOPs？
2. 如果 `seq_len` 不能被 TP 整除，会在哪一行报错？
3. SP 与 CP 同时开时，`s` 先被谁切？

对应章节：07、9.6。

## M.5 实验 5：Attention、RoPE 与 TE

**目标**：把“论文里的 attention”映射到代码里的模块与 kernel。

```bash
grep -R "class .*DotProductAttention\|class .*Attention" -n megatron/core/transformer | head
grep -R "rotary\|RoPE\|rope" -n megatron/core/transformer/attention.py | head -20
grep -R "get_gpt_layer_with_transformer_engine_spec\|get_gpt_layer_local_spec" -n megatron/core/models/gpt
```

**观察**：

- QKV 的 fused 布局与 head 顺序；
- RoPE 的 interleaved/half 两种实现，位置 id 从哪来；
- causal mask、padding mask、packed document mask 分别在哪一层加；
- TE 路径与 local 路径的模块替换点（`ModuleSpec`）。

**验收问题**：

1. GQA 下 K/V 的 head 数在哪里决定？repeat 发生在 kernel 内还是显式张量？
2. CP 打开时，attention 的 `q_start/kv_len` 如何计算？
3. 如果 RoPE 的 base 改了，checkpoint 还能直接加载吗？

对应章节：18、30.5、34.4。

## M.6 实验 6：流水线调度

**目标**：从调度函数看清 warmup/steady/cooldown、p2p tag 与激活形状。

```bash
grep -R "def forward_backward_pipelining_without_interleaving\|def forward_backward_pipelining_with_interleaving\|def get_forward_backward_func" \
  -n megatron/core/pipeline_parallel/schedules.py
grep -R "def send_forward\|def recv_forward\|def send_backward\|def recv_backward" \
  -n megatron/core/pipeline_parallel/p2p_communication.py
```

**观察**：

- 1F1B 的 warmup 数、steady 循环、cooldown；
- 每个 micro-batch 的 `(stage, mb)` 与 tensor shape；
- p2p 的 tag 是否编码 mb/chunk；
- VPP 下 `(mb, chunk)` 的映射与反向顺序。

**验收问题**：

1. 最后一个 stage 的经典 warmup 为什么可以是 0？`max(1, ...)` 在补什么？
2. 如果 p2p 用局部 rank 而不是全局 rank，`PP=2, DP=2` 会怎样？
3. VPP 的反向为什么不能简单按 chunk 倒序？

对应章节：08、21。

## M.7 实验 7：MoE dispatcher 与专家并行

**目标**：把 token permutation、all-to-all、combine 的索引走一遍。

```bash
grep -R "class .*TokenDispatcher" -n megatron/core/transformer/moe
grep -R "topk_softmax_with_capacity\|permute\|unpermute" -n megatron/core/transformer/moe | head -30
grep -R "expert_model_parallel" -n megatron/core/parallel_state.py | head
```

**观察**：

- 路由 top-k 后如何生成 `(token, expert)` pair；
- capacity/drop 在 permutation 前还是后；
- dispatch 的 split sizes 如何计算，combine 如何逆映射；
- 专家梯度为什么跳过 DP all-reduce、最后除以 EP；
- aux loss 的 `f_i/P_i` 在哪些组上归约。

**验收问题**：

1. 如果 combine 的索引方向写反，loss 会立刻异常还是只错梯度？
2. 某专家在某个 rank 上没有 token 时，集合通信为什么仍要对齐？
3. 把 `ep` 从 8 改到 4，专家 id 的重排规则是什么？

对应章节：10.3、25。

## M.8 实验 8：Distributed optimizer 的 bucket 与通信

**目标**：看清 flat buffer、reduce-scatter、all-gather 的时序。

```bash
grep -R "class DistributedOptimizer" -n megatron/core/distributed
grep -R "grad_reduce\|param_gather\|overlap_grad_reduce\|overlap_param_gather" \
  -n megatron/core/distributed/distributed_optimizer.py | head -30
```

**观察**：

- 参数如何 flatten、padding、按 bucket 切；
- 反向时梯度何时 reduce-scatter、何时开始 all-gather；
- 优化器状态如何在 owner 上更新；
- 与 TP/PP/EP 的参数分组如何对应。

**验收问题**：

1. reduce-scatter 的每卡字节数是 all-reduce 的多少？加上 all-gather 呢？
2. 为什么参数 all-gather 可以和下一层的反向重叠？
3. EP 专家参数为什么不能直接按 DP 再分片？

对应章节：22、50。

## M.9 实验 9：检查点格式与重分片

**目标**：从保存/加载函数追到 metadata、shard 与重分片。

```bash
grep -R "def save_checkpoint\|def load_checkpoint" -n megatron/training megatron/core/dist_checkpointing
grep -R "torch_dist\|ckpt_format\|ShardedTensor\|sharded_state_dict" -n megatron/core/dist_checkpointing/ | head -30
```

**观察**：

- 逻辑 state_dict 与物理 shard 的映射；
- metadata 记录哪些并行度/参数语义；
- 加载时如何拒绝不兼容布局；
- 异步保存的 staging/原子可见点。

**验收问题**：

1. 换 TP/PP 后，哪些参数可以按名字重分片，哪些必须按语义切？
2. 优化器状态为什么不能按 flat offset 直接搬？
3. 半写 checkpoint 如何在加载时被发现？

对应章节：24、50.5。

## M.10 实验 10：推理引擎与 KV cache

**目标**：分清训练前向与推理前向的差异。

```bash
find megatron/core/inference -maxdepth 2 -type f | sort
grep -R "class .*KVCache\|chunked\|prefix\|speculative\|prefill" -n megatron/core/inference | head -30
```

**观察**：

- KV cache 的布局（按层/按 head/按 token）；
- chunked prefill 如何切分长 prompt；
- prefix cache 的命中/复用条件；
- PD 分离时 KV 如何传输；
- TP/PP 推理与训练并行的差异。

**验收问题**：

1. decode 阶段为什么是带宽受限？batch 增大如何改变 roofline？
2. prefix cache 命中后，哪些状态可以复用、哪些必须重算？
3. 训练用的 checkpoint 直接拿去做推理，缺哪些步骤？

对应章节：26、40。

## M.11 实验 11：模型组装（ModuleSpec/Provider）

**目标**：理解 Megatron 如何把“配置 + 子模块”组装成模型，而不是硬编码
`TransformerLayer`。

```bash
grep -R "class ModuleSpec\|def build_module" -n megatron/core/transformer/spec_utils.py
grep -R "def get_gpt_layer_local_spec\|def get_gpt_decoder_layer_specs\|get_gpt_layer_with_transformer_engine_spec" \
  -n megatron/core/models/gpt/gpt_layer_specs.py
grep -R "class TransformerBlockSubmodules\|class TransformerLayerSubmodules" \
  -n megatron/core/transformer
```

**观察**：

- `ModuleSpec(module=..., params=..., submodules=...)` 的三层结构；
- 未指定的子模块如何回退到 `IdentityOp`/`IdentityFuncOp`；
- `TransformerBlockSubmodules.layer_specs` 如何支持逐层不同的结构
  （dense/MoE 交替）；
- TE spec 与 local spec 的替换点；
- `GPTModel` 如何接收 spec 并构建 `TransformerBlock`。

**验收问题**：

1. 把 LayerNorm 换成 RMSNorm，应该改 spec 的哪一层？要不要改 `TransformerLayer`？
2. 为什么 `moe_layer_freq` 能让同一模型里 dense/MoE 层交替？
3. 如果 spec 里的 `params` 和 `submodules` 冲突，谁生效？

对应章节：29.6、30。

## M.12 实验 12：测试与 CI

**目标**：找到“某个功能是否真的被支持”的证据，而不是只看 release note。

```bash
find tests -maxdepth 2 -type d | sort | head -30
grep -R "parallel_state\|tensor_parallel\|moe" -n tests/unit_tests 2>/dev/null | head -20
# 路径随版本可能是 tests/ 或 tests/unit_tests/
```

**观察**：

- 单测/集成测试/多卡测试的分层；
- 哪些测试需要 GPU/多卡，CI 如何标记；
- 测试里的 tolerance 与随机种子；
- 一个功能从 config 到 recipe 到 test 的完整证据链。

**验收问题**：

1. 只有 config 字段、没有消费路径，算“支持”吗？属于 36.1 的哪一级？
2. 一个测试通过了，但只测了 TP=1，能证明 TP=8 正确吗？
3. 你要给一个新并行层加回归测试，最小断言是什么？

对应章节：27、29.4、36.1、附录 J。

> 编号说明：实验 1–12 对应 M.1–M.12；**实验 13**（需要额外安装
> `megatron-core`）放在 M.15，M.13–M.14 是笔记模板与常见坑，不是实验。

## M.13 一页源码笔记模板

每个实验结束后，用这个模板写一页，比“读完了”有用：

```text
repo/commit/tag:
入口文件:函数:
输入/输出形状:
关键集合通信（顺序、group、字节）:
状态/checkpoint 影响:
删掉某行会坏什么（前向/梯度/恢复）:
对应教程章节:
未解决的问题:
```

## M.14 常见坑

- **只读 `core/` 不看 `training/`**：开关的默认值、参数校验、checkpoint 编排
  常在 training 侧；
- **把类名当稳定 API**：函数/路径会漂移，职责（并行组、调度、梯度收尾、
  优化器、检查点）更稳定；
- **不看测试**：测试是“这个功能到底怎么用”的最短文档；
- **忽略版本**：同一个问题在 0.1x 和 0.2x 的答案可能不同，笔记必须带 commit；
- **只看单卡**：并行代码的正确性在 rank 组合里，单卡跑通不代表多卡对。

## M.15 实验 13：用 `megatron.core` 跑一个 1-GPU 前向

前面的实验都在读源码；这个实验让你**直接调用 Core**，确认“书里的抽象”与
“库里的对象”是同一套。它需要额外安装 `megatron-core`（不在本仓库的
`requirements.txt` 里），因此**不进入本教程的 CI**；把它当作阅读实验，记录
版本/报错比“跑通”更有价值。

```python
# core_smoke.py（1 GPU；CPU 需要 use_cpu_initialization，且不是所有 kernel 支持）
import torch
from megatron.core import parallel_state
from megatron.core.models.gpt import GPTModel
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_layer_local_spec
from megatron.core.tensor_parallel.random import model_parallel_cuda_manual_seed
from megatron.core.transformer.transformer_config import TransformerConfig

parallel_state.initialize_model_parallel(1, 1)
try:
    model_parallel_cuda_manual_seed(1234)
    config = TransformerConfig(
        num_layers=2,
        hidden_size=128,
        num_attention_heads=4,
        use_cpu_initialization=not torch.cuda.is_available(),
    )
    model = GPTModel(
        config=config,
        transformer_layer_spec=get_gpt_layer_local_spec(),
        vocab_size=256,
        max_sequence_length=64,
        pre_process=True,
        post_process=True,
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    tokens = torch.randint(0, 256, (2, 16), device=device)
    position_ids = torch.arange(16, device=device).unsqueeze(0).expand(2, -1)
    logits = model(tokens, position_ids, attention_mask=None)
    print("logits:", tuple(logits.shape))  # 期望 [2, 16, 256]
finally:
    parallel_state.destroy_model_parallel()
```

预期与观察点：

```text
[ ] logits.shape == (2, 16, 256)
[ ] 打印 model 的模块树，找到 embedding / decoder.layers / final_layernorm / output_layer
[ ] 对比 mini 的 TransformerStage：哪些对象一一对应，哪些是 Core 多出来的
[ ] 把 get_gpt_layer_local_spec() 换成 get_gpt_layer_with_transformer_engine_spec()，
    观察哪些模块类型变了（需要 TE）
```

一个最小训练步的扩展（伪代码，参数名以版本为准）：

```python
loss = torch.nn.functional.cross_entropy(
    logits.reshape(-1, logits.size(-1)), labels.reshape(-1)
)
loss.backward()
opt = torch.optim.AdamW(model.parameters(), lr=3e-4)
opt.step()
```

分布式检查点扩展：用 `torch.distributed.checkpoint.save` 保存
`get_model_state_dict(model)`，再用新布局 `load`；这一步会暴露 TP/PP 的
sharded state_dict 语义（见 24.5.1）。**如果脚本在当前版本跑不通，先记录
commit、Python/PyTorch/TE 版本和完整 traceback**——这本身就是一次“版本
漂移”实验，比背 API 更有用。

对应章节：15、24.5.1、29.3、29.6、50.5。

---

上一附录：[附录 L 练习、项目与面试题](L-exercises.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 N 故障注入与数值对照实验手册](N-fault-injection-labs.md)  
返回全书总结：[第 52 章](52-conclusion.md)
