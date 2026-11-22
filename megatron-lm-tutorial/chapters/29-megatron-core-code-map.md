# 第 29 章 Megatron Core 生产代码地图

> **第 IV 部分导读（29–33）**
> 本部分从 mini 跳到生产栈：Core 代码地图、模型家族、数据、集群与可观测性。
> 前置是 15–28 或同等分布式经验；读完应能从启动开关定位到消费代码、为一个
> 模型选择 recipe/并行配置，并分清哪些能力在 `core/`、哪些在
> `training/`/`examples/`。

## 29.1 先分清“库”和“训练脚本”

Megatron-LM 仓库里有两类代码：

- `megatron/core/`：可复用库（并行、模型、优化器、检查点、推理）；
- `megatron/training/`、`examples/`：预训练入口、参数解析、数据/日志/recipe；
- `megatron/post_training/`：SFT/LoRA/偏好优化/RL 等后训练路径；
- `megatron/legacy/`：旧模型实现（逐步迁移）。

生产集成通常只依赖 `megatron-core`，自己写训练循环；研究复现则直接用
`examples/` 的脚本。读源码时先确定你在哪一层。

## 29.2 关键模块

### 并行状态

`megatron/core/parallel_state.py`：

- `initialize_model_parallel(...)`；
- `get_tensor_model_parallel_group()`；
- `get_pipeline_model_parallel_group()`；
- `get_data_parallel_group()`；
- `get_context_parallel_group()`；
- rank 查询与 group 创建。

这是所有并行代码的入口。先读它，再看别的模块。

### 张量并行

`megatron/core/tensor_parallel/`：

- `layers.py`：`ColumnParallelLinear`、`RowParallelLinear`、
  `VocabParallelEmbedding`、`parallel_lm_logits`；
- `cross_entropy.py`：词表并行 CE；
- `random.py`：TP 下的 RNG 同步（`CudaRNGStatesTracker` 等）；
- `mappings.py`：copy/gather/reduce/scatter 的 autograd 包装，序列并行的
  `scatter/gather` 原语也在这里；
- `data.py`：数据并行的 `broadcast_data` 等辅助，不是序列并行的布局实现。
  序列并行的数据布局由 `mappings.py` 与各层的 SP 分支决定。

生产实现会调用 Transformer Engine 的 fused linear/attention，并支持异步
all-reduce、CUDA Graph。

### 优化器、数据与模型

这三块在正文里分别对应第 13/31/30 章，读代码时先找到“入口”：

- `core/optimizer/`：`get_megatron_optimizer`、Adam/混合精度状态、
  LR scheduler、Muon 相关实现（版本相关）。分布式优化器的 buffer/通信在
  `core/distributed/` 与 `core/optimizer/` 之间协作，不要只读一边。
- `core/datasets/`：`IndexedDataset`/`MMapIndexedDataset`、`GPTDataset`、
  `BlendedMegatronDatasetBuilder`、`sampler.py`、`utils.py`；
  这是 38.8.1 的内置数据路径，不是 Energon（38.8.2）。
- `core/tokenizers/`：tokenizer 构建/包装（类名与文件随版本变化）。
- `core/models/`：`gpt`、`bert`、`t5`、`mamba`、`hybrid`、`multimodal`
  等模型包装；每个模型负责“把 transformer block 组装成可训练的模块”，
  并行逻辑仍下沉到 `core/transformer/` 与 `core/tensor_parallel/`。

### Transformer

`megatron/core/transformer/`：

- `transformer_block.py`：把 N 层 layer 组装成 block，处理 PP 分层/checkpoint；
- `transformer_layer.py`：pre/post norm、残差、并行 MLP/attention；
- `attention.py`：self-attention、RoPE、GQA；MLA 在
  `multi_latent_attention.py`（`MLASelfAttention` 等）；
- `mlp.py`：MLP/SwiGLU；
- `transformer_config.py`：模型/并行配置；
- `moe/`：router、dispatcher、experts、aux loss、all-to-all；dispatcher 常见
  `MoEAlltoAllTokenDispatcher`/`MoEAllGatherTokenDispatcher`，新版本还有
  flexible/DeepEP 路径（以源码为准）；
- `experimental_attention_variant/`：混合/稀疏注意力；
- `multi_token_prediction.py`：MTP block 与 loss（34.3.2）。

### SSM / Hybrid

SSM/Hybrid 不在 `core/transformer/` 下，而在独立的 `megatron/core/ssm/`：

- `mamba_mixer.py`、`mamba_block.py`、`mamba_layer.py`：Mamba/SSM 层；
- `gated_delta_product.py` 一类：混合线性注意力变体；
- 模型包装在 `core/models/mamba`、`core/models/hybrid`；
- 并行语义与 Transformer 不同：CP 传递的是递归状态，不是 KV 块（见 30.4）。

### 流水线

`megatron/core/pipeline_parallel/`：

- `schedules.py`：`forward_backward_pipelining_without_interleaving`、
  `...with_interleaving`、`get_forward_backward_func`；
- `p2p_communication.py`：send/recv、shape/dtype 元数据、异步；
- `pipeline_parallel.py`：stage 切分。

### 分布式优化器/检查点

`megatron/core/distributed/`：

- `distributed_data_parallel.py`；
- `finalize_model_grads.py`；
- `distributed_optimizer.py`；
- `fsdp/`：Megatron FSDP/HSDP；
- `core/dist_checkpointing/`（`serialization.py`、`mapping.py`、`strategies/`）
  与 `training/checkpointing.py`：前者是可复用的分布式检查点 API，后者是
  训练侧的保存/恢复、重分片、异步保存编排。注意路径是 `dist_checkpointing/`，
  不是 `distributed/checkpointing.py`。

### 推理

`megatron/core/inference/`：

- KV cache；
- chunked prefill；
- prefix caching；
- speculative decoding；
- prefill/decode 分离；
- 独立的 `InferenceEngine`（不是 vLLM 的封装）。与 vLLM/TensorRT-LLM 的
  协作主要在权重导出、tokenizer/协议与部署层，不要把 Core inference 当成
  这两个引擎的“接口层”。

### 精度/算子

- Transformer Engine（外部依赖）：FP8/FP4、fused attention/linear；
- `fp8_utils.py`、`quantization/`；
- CUDA Graph、communication overlap。

## 29.3 一次 GPT 训练的调用链（生产）

```text
pretrain.py
  ├─ parse_args
  ├─ initialize_megatron
  │    ├─ initialize_model_parallel
  │    ├─ build tokenizer/dataset
  │    └─ build model (GPTModel)
  ├─ train loop
  │    ├─ get_batch
  │    ├─ forward_backward_func
  │    │    ├─ pipeline schedule
  │    │    ├─ forward_step (model)
  │    │    └─ backward_step
  │    ├─ optimizer.step
  │    └─ save_checkpoint
  └─ evaluate_and_log
```

读源码时可以从 `pretrain.py` 的 `train()` 函数往下追，但不要一开始就读所有
分支。建议先读一个最小模型（如 `examples/gpt3/train_gpt3_*.sh`）的配置，再
对照 `GPTModel`/`TransformerLayer`。

### 29.3.1 函数级地图

| 任务 | 入口（0.20.x 基线） | 文件 |
|---|---|---|
| 初始化并行组 | `initialize_model_parallel` | `core/parallel_state.py` |
| 构建模型 | `GPTModel` / `TransformerLayer` | `core/transformer/` |
| 选择 schedule | `get_forward_backward_func` | `core/pipeline_parallel/schedules.py` |
| 单步前向 | `forward_step`（由 `pretrain_*.py` 定义并传入） | `pretrain_*.py` |
| 训练步 | `train_step` | `training/training.py` |
| 梯度收尾 | `finalize_model_grads` | `core/distributed/finalize_model_grads.py` |
| 构建优化器 | `get_megatron_optimizer` | `core/optimizer/__init__.py` |
| 保存/加载 | `save_checkpoint` / `load_checkpoint` | `training/checkpointing.py`；核心 API 在 `core/dist_checkpointing/` |
| 数据迭代器 | `build_train_valid_test_data_iterators` | `training/training.py` |
| MoE dispatch | `MoEAlltoAllTokenDispatcher` / `MoEAllGatherTokenDispatcher` | `core/transformer/moe/` |
| EP 组 | `get_expert_model_parallel_group` | `core/parallel_state.py` |
| 推理引擎 | `InferenceEngine` / dynamic engine | `core/inference/` |
| HF 转换 | `AutoBridge` | Megatron Bridge |
| FP8/FP4 | TE `fp8_autocast` / `DelayedScaling`；FP4 format/recipe | Transformer Engine + `core/fp8_utils.py` / `core/fp4_utils.py` |

这张表的作用是“先找到函数，再读实现”。函数名/路径会随版本漂移，但**职责**
稳定：并行组、模型、调度、梯度收尾、优化器、检查点、数据、推理。

### 29.3.2 开关 → 文件 → 函数

| 开关 | 首先影响 | 然后看 |
|---|---|---|
| `--tensor-model-parallel-size` | `parallel_state.initialize_model_parallel` | `tensor_parallel/layers.py` |
| `--sequence-parallel` | `tensor_parallel/mappings.py` | Column/Row 的 SP 分支 |
| `--pipeline-model-parallel-size` | stage 切分 | `pipeline_parallel/pipeline_parallel.py` |
| `--virtual-pipeline-model-parallel-size` | interleaved schedule | `pipeline_parallel/schedules.py` |
| `--context-parallel-size` | CP group | attention 的 CP 分支 |
| `--expert-model-parallel-size` | EP group | `transformer/moe/token_dispatcher.py` |
| `--use-distributed-optimizer` | `distributed/distributed_optimizer.py` | 参数/状态分片 |
| `--fp8-format` / `--fp8-recipe` | TE autocast | `fp8_utils.py` / TE |
| `--ckpt-format` | checkpointing 后端 | `checkpointing/` |
| `--decoder-first-pipeline-num-layers` | 不均匀 PP | stage 层数分配 |

用法：先 grep 开关名，找到 `arguments.py` 的定义和默认值，再 grep 变量名找到
消费它的函数。不要从旧博客复制参数名。

## 29.4 版本与 API 漂移

Core 仍在快速演进，但**不要把二手博客的版本号当事实**。一个可操作的判断
方法：

1. 先在仓库的 release notes / tag 里找到功能名；
2. 再在 `transformer_config.py`/`arguments.py` 里找到对应开关；
3. 最后在代码里找到消费该开关的函数；
4. 三步都对上，才写成“某版本已支持”。

按这个标准，HybridModel、Muon、packed-sequence MoE dispatch 等能力是在
0.1x 系列逐步引入并持续重构的，不应简单归到某一个 0.19/0.20 版本；推理侧的
chunked prefill、prefix cache、speculative decoding 与 PD 分离也在多个版本
中迭代。本书正文提到这些能力时，默认以“当前基线源码 + 开关”描述，不承诺
某个补丁号一定包含。

同样，roadmap 里的 FSDP v2、2D MXFP8、NVFP4 fusion、1M 上下文等是**方向**，
不是已发布合同。引用时标注“roadmap/实验性”，并给出验证命令或 issue 链接。
Megatron Bridge 的 recipes 也会随模型更新，转换脚本要匹配版本。

### 29.4.1 一个可执行的“发布说明 → 代码”核对流程

看到一条 release note（例如“支持 X”）时，按下面四步核对，避免把 roadmap
当事实：

```bash
# 1. 找到版本/提交
git log --oneline --decorate -n 20
git tag --contains <commit>

# 2. 找到开关
grep -R "X" megatron/core/transformer/transformer_config.py \
          megatron/training/arguments.py

# 3. 找到消费该开关的代码（不是只定义）
# 注意：-n 会给每行加 file:line: 前缀，所以不能用 `grep -v ":"` 过滤，
# 那会把所有结果都删掉。只排除 def 行即可。
grep -R -n --include='*.py' "config\.X\|args\.X" megatron/ | grep -v "def " | head

# 4. 找到测试/recipe
grep -R "X" tests/ examples/ recipes/ | head
```

判定标准（**证据强度用 E1–E4，发布状态用 L1–L4，两套标签不能混用**）：

- 只有 config 字段、没有消费路径 → **E1**（代码存在）；是否已发布另看
  release tag，可能只是 L3/L4；
- 有消费路径、没有测试/recipe → **E1–E2**；
- 有测试 + 小规模可跑 → **E2**；与参考实现 logits/loss 对齐才是 **E3**；
- 有大规模/性能/收敛证据 → **E4**。

发布状态再看第 36.1 节：release tag + 文档 + 示例是 L1，只在 `dev`/main
是 L2，roadmap/issue 是 L3，二手转述是 L4。一个功能完全可以是
“L2 已合并 + E3 数值对齐”，也可以是“L1 已发布 + E2 只跑过 demo”。

这套流程同样适用于判断“某模型是否真的被支持”：先找 mapping/recipe，再找
数值校验脚本，最后看 CI/报告。附录 J 给了声明登记模板与 E1–E4 的定义。

## 29.5 如何定位一个功能

以“sequence parallel 在哪里实现”为例：

1. `parallel_state.py` 找 group；
2. `tensor_parallel/mappings.py` 找 `scatter_to_sequence_parallel_region`；
3. `tensor_parallel/layers.py` 看 Column/Row 的 SP 分支；
4. `transformer/transformer_layer.py` 看 LayerNorm/dropout 的布局；
5. `arguments.py` 找 `--sequence-parallel`。

以“MoE all-to-all”为例：

1. `transformer/moe/` 的 `token_dispatcher.py`；
2. `moe_layer.py` 的 router/dispatcher；
3. `experts.py` 的 grouped GEMM；
4. `parallel_state.py` 的 EP group；
5. `arguments.py` 的 `--expert-model-parallel-size`。

这种“从开关到实现”的追踪比从头读目录高效。

## 29.6 模型组装：ModuleSpec、Provider 与 Submodules

读完并行组和模型目录，下一个问题是：**Megatron 怎样把“配置 + 各种可选实现”
拼成一个模型？** 答案不是继承一个大类，而是 `ModuleSpec` 这层间接。

### 29.6.1 ModuleSpec 的三层结构

`megatron/core/transformer/spec_utils.py` 里的 `ModuleSpec` 大致是：

```python
@dataclass
class ModuleSpec:
    module: type | Callable          # 真正的 nn.Module / 函数
    params: dict | None = None       # 构造参数
    submodules: Any | None = None    # 子模块的 spec（可嵌套）
```

`build_module(spec, **kwargs)` 做三件事：

1. 合并 `spec.params` 与运行时 `kwargs`（同名参数以运行时为准）；
2. 递归 `build_module` 每个 `submodules`；
3. `spec.module(**params, **built_submodules)` 构造对象。

未指定的子模块通常回退到 `IdentityOp` / `IdentityFuncOp`，所以一个“最小
spec”只写你关心的部分，其余是恒等。这也意味着**漏写一个子模块不会报错，
而是静默变成 Identity**——自定义模型时要显式检查。

### 29.6.2 GPT 的 spec 从哪来

`megatron/core/models/gpt/gpt_layer_specs.py` 提供两类入口：

| 入口 | 返回 | 用途 |
|---|---|---|
| `get_gpt_layer_local_spec(...)` | `ModuleSpec(TransformerLayer, submodules=TransformerLayerSubmodules(...))` | 纯 Core 实现，不依赖 TE |
| `get_gpt_layer_with_transformer_engine_spec(...)` | 同上，但 linear/attention/norm 换成 TE 模块 | 生产性能路径 |
| `get_gpt_decoder_layer_specs(...)` | `list[ModuleSpec]`（每层一个） | 逐层选择 dense/MoE/TE/local spec |
| `get_gpt_decoder_block_spec(...)` | `TransformerBlockSubmodules`（含 layer_specs） | 直接交给 `GPTModel(transformer_layer_spec=...)` |

`TransformerLayerSubmodules` 的常见字段：

```text
input_layernorm        pre_mlp_layernorm
self_attention         mlp
self_attn_bda          mlp_bda        # bias-dropout-add
q_layernorm/k_layernorm（可选）
```

`TransformerBlockSubmodules` 则是：

```python
@dataclass
class TransformerBlockSubmodules:
    layer_specs: list[ModuleSpec]   # 每层一个 spec
    layer_norm: ModuleSpec | None = None
```

这解释了 Megatron 如何支持**逐层异构**：`moe_layer_freq` 可以是整数或模式，
`get_gpt_decoder_layer_specs` 按层号选择 dense spec 或 MoE spec，返回
`list[ModuleSpec]`；`get_gpt_decoder_block_spec` 再把它包装成
`TransformerBlockSubmodules`，最终交给 `GPTModel`。`TransformerBlock` 遍历
`layer_specs` 构建每一层，而不是假设所有层结构相同。两个入口的返回类型不同，
不要直接把 `get_gpt_decoder_layer_specs(...)` 的 list 当成
`transformer_layer_spec` 传进去。

### 29.6.3 改一个模块的最小步骤

以“把 LayerNorm 换成 RMSNorm”为例：

```python
from copy import deepcopy
from megatron.core.transformer.spec_utils import ModuleSpec
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_layer_local_spec

spec = get_gpt_layer_local_spec()
spec = deepcopy(spec)                      # 不要原地改共享的 spec
spec.submodules.input_layernorm = ModuleSpec(module=MyRMSNorm)
spec.submodules.pre_mlp_layernorm = ModuleSpec(module=MyRMSNorm)
# 再把 spec 传给 GPTModel / TransformerBlock
```

要点：

- **先 deepcopy**：`get_gpt_layer_local_spec()` 每次调用应返回新对象，但缓存/
  全局 spec 在版本间可能被复用；原地修改会污染别的模型；
- **接口一致**：自定义模块要接受 `config`/`hidden_size`/`eps` 等约定参数，
  并返回与原子模块相同的形状/dtype；
- **检查 Identity**：如果新模块没被 spec 引用，前向可能直接跳过它；
- **数值验收**：同一权重下对比替换前后的 logits/梯度，不要只看能否跑通。

### 29.6.4 Provider/Builder：谁把 spec 交给模型

训练入口通常有一个 `model_provider` 函数（或在 `gpt_builders.py` 一类文件里）：

```python
def model_provider(pre_process=True, post_process=True):
    config = get_model_config()
    # block spec 才是 TransformerBlockSubmodules；若只用 decoder_layer_specs，
    # 需要自己包成 TransformerBlockSubmodules(layer_specs=...)。
    spec = get_gpt_decoder_block_spec(config, use_transformer_engine=True)
    return GPTModel(
        config=config,
        transformer_layer_spec=spec,
        vocab_size=...,
        max_sequence_length=...,
        pre_process=pre_process,
        post_process=post_process,
    )
```

- `pre_process/post_process` 决定当前 PP stage 是否包含 embedding/output；
- VPP 下还要传 `vp_stage`/stage 编号，确保每层落到正确的 chunk；
- Provider 是“配置 → 模型”的唯一边界，换模型结构时优先改 spec/provider，
  而不是在 `TransformerLayer` 里加 `if`；
- 训练脚本、Bridge、NeMo 都通过各自的 provider 复用同一个 Core 模型。

### 29.6.5 与 mini 的对照

mini 没有 spec 系统：`TransformerStage` 直接 `TransformerBlock(...)`，层类型
由 `config.use_moe` 决定。这是刻意的简化，代价是：

- 不能在同一模型里逐层混用 dense/MoE；
- 换 norm/attention 要改类本身，不能只换 spec；
- 没有 TE/local 两条实现的无缝切换。

如果要在 mini 里加 spec，最小做法是定义一个 `LayerSpec` dataclass，让
`TransformerBlock` 接受 `list[LayerSpec]`，每层用 `build_module` 构造。这不
会影响现有等价性测试，但能让“逐层异构”的练习（附录 L）更接近生产。

> 版本提示：`get_gpt_decoder_layer_specs`、`TransformerBlockSubmodules` 的
> 参数和字段会随版本调整；用 `grep` 找当前定义，不要照抄旧博客。原则不变：
> **module/params/submodules 三层 + 未指定即 Identity + 逐层 spec 列表**。

## 29.7 本章小结

- Core 是库，training/examples 是入口；先确定层次。
- parallel_state → tensor_parallel → transformer → pipeline 是推荐阅读顺序。
- 生产实现依赖 TE/fused kernel/异步通信，mini 只保留语义。
- 版本漂移快，API 以当前源码为准。
- 用“功能开关 → 模块 → 代码”的路径定位实现。
- 模型由 `ModuleSpec`（module/params/submodules）+ 逐层 spec 列表组装；
  换 norm/attention 优先改 spec，而不是改大类。

下一章看 Megatron 支持的模型家族。
