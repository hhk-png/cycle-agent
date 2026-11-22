# 第 35 章 Megatron Bridge 与生态互操作

## 35.1 为什么需要 Bridge

Megatron 的并行/性能很强，但生态在 Hugging Face：

- 研究人员用 HF 权重/模型；
- 微调/评测/部署工具默认 HF 格式；
- Megatron 的 TP/PP 分片格式与 HF 单文件不同；
- 手工转权重容易错（QKV 融合/GQA/MLA/PP 切分）。

Megatron Bridge 提供：

- HF ↔ Megatron 的双向转换；
- 预训练/SFT/LoRA recipes；
- 自动模型映射；
- 权重验证；
- 与 NeMo 容器/平台集成。

## 35.2 Bridge 的基本用法

公开示例大致如下（API 可能随版本变化）：

```python
from megatron.bridge import AutoBridge

bridge = AutoBridge.from_hf_pretrained("meta-llama/Llama-3.2-1B")
provider = bridge.to_megatron_provider()
provider.tensor_model_parallel_size = 2
provider.pipeline_model_parallel_size = 1
provider.finalize()
model = provider.provide_distributed_model(wrap_with_ddp=False)
bridge.save_hf_pretrained(model, "./hf_exports/llama32_1b")
```

CLI：

```bash
python examples/conversion/convert_checkpoints.py import \
  --hf-model meta-llama/Llama-3.2-1B \
  --megatron-path ./checkpoints/llama3_2_1b

python examples/conversion/convert_checkpoints.py export \
  --megatron-path ./checkpoints/llama3_2_1b \
  --hf-path ./exports/llama3_2_1b_hf
```

注意：

- Python 版本以 Bridge 的 `pyproject.toml`/安装文档为准（本书基线 3.12+）；
- 转换需要足够的 CPU 内存/磁盘；
- FP8 权重导出需要 dequantization；
- PP 导出需要 broadcast/gather；
- 转换后要做数值校验（logits/loss）。

## 35.3 映射类型

Bridge 的技术文档列出：

- `AutoMapping`：自动推断；
- `ColumnParallelMapping`：列并行切分；
- `RowParallelMapping`：行并行；
- `QKVMapping`：QKV 融合/拆分；
- `GatedMLPMapping`（旧文档/旧版本可能写作 `GatedGLPMapping`，以当前源码
  为准）：gate/up 交错；
- `ReplicatedMapping`：复制参数。

这些映射对应第 17 章的行/列切分、GLU 对齐、GQA/MLA 结构。理解 mini 的
`sharding.py`，再看 Bridge 的 mapping，会容易很多。

### 35.3.1 一个 Llama 转换的逐参数映射

以 Llama 3.x 为例（`h`=hidden，`n_h`=query heads，`n_kv`=KV heads，`d`=head
dim，`ffn`=FFN hidden）：

| HF 参数 | HF 形状 | Megatron 参数 | Megatron 形状 | 切分 |
|---|---|---|---|---|
| `model.embed_tokens.weight` | `[vocab, h]` | `embedding.word_embeddings.weight` | `[vocab/tp, h]` | 词表行切 |
| `self_attn.q_proj.weight` | `[n_h·d, h]` | `self_attention.linear_qkv.weight` | `[(n_h+2n_kv)·d/tp, h]` | 按 head 融合后切 |
| `self_attn.k_proj.weight` | `[n_kv·d, h]` | 同上 | 同上 | 同上 |
| `self_attn.v_proj.weight` | `[n_kv·d, h]` | 同上 | 同上 | 同上 |
| `self_attn.o_proj.weight` | `[h, n_h·d]` | `self_attention.linear_proj.weight` | `[h, n_h·d/tp]` | 输入列切 |
| `mlp.gate_proj.weight` | `[ffn, h]` | `mlp.linear_fc1.weight` | `[2·ffn/tp, h]` | gate/up 融合后切 |
| `mlp.up_proj.weight` | `[ffn, h]` | 同上 | 同上 | 同上 |
| `mlp.down_proj.weight` | `[h, ffn]` | `mlp.linear_fc2.weight` | `[h, ffn/tp]` | 输入列切 |
| `input_layernorm.weight` | `[h]` | `input_layernorm.weight` | `[h]` | 复制 |
| `post_attention_layernorm.weight` | `[h]` | `pre_mlp_layernorm.weight` | `[h]` | 复制 |
| `model.norm.weight` | `[h]` | `decoder.final_layernorm.weight` | `[h]` | 复制 |
| `lm_head.weight` | `[vocab, h]` | `output_layer.weight` | `[vocab/tp, h]` | 词表行切 |

三个不能想当然的地方：

1. **QKV 不是简单 concatenate**。Megatron 的 fused QKV 按 query group 排列：
   每个 group 内是 `q_0..q_{g-1}, k, v`，其中 `g = n_h/n_kv`。直接把 HF 的
   `q/k/v` 按行拼起来会得到错误的 head 顺序——模型能跑，attention 已经错了。
2. **GQA 的 KV 不复制到每个 query head**。Megatron 的 KV 头数是 `n_kv`，
   attention 内部做 repeat；转换时不要把 `k_proj` 复制 `g` 份。
3. **GLU 的 gate/up 顺序**。`linear_fc1` 的输出通常是 `[gate; up]`，但有些
   实现是交错排列；必须与 Megatron 的 `gated_linear_unit` 实现对齐。

### 35.3.2 TP/PP 下的形状变化

- **Column Parallel**：输出维按 TP 切。QKV 的切分要先按 head 分组，再按 TP
  取连续 head；不能对融合后的大矩阵直接 `chunk`。
- **Row Parallel**：输入维按 TP 切。`o_proj/down_proj` 的列切必须与上游
  head/FFN 的切分一致。
- **PP**：第 `i` 层落到哪个 stage 由 `num_layers/pp` 决定；embedding 在
  stage 0，final norm/output 在最后一个 stage。导出时要把各 stage 的参数
  broadcast/gather 回全局，再写 HF 格式。
- **Tied embeddings**：PP=1 时可以直接共享同一个 `Parameter`；PP>1 时
  embedding 在 stage 0、output 在最后一个 stage，不能共享内存。生产
  Megatron 通过 `finalize_model_grads` 对两侧梯度做 PP 归约、并在优化器
  更新后同步权重来实现“逻辑绑定”。转换/导出时要把它们当成同一个全局张量
  处理，不要导出两份逐渐漂移的副本。

### 35.3.3 转换后的数值校验

转换正确与否不能看“能加载”，要看数值。最小校验：

```python
# 1. 固定输入（不要用随机采样，避免掩盖 argmax 差异）
ids = torch.tensor([[1, 2, 3, 4, 5]])
# 2. 同一权重、同一 dtype、同一 device
hf_logits = hf_model(ids).logits
mg_logits = megatron_model(ids)
# 3. 比较 logits，而不是只比 loss
max_diff = (hf_logits - mg_logits).abs().max()
assert max_diff < 1e-3, max_diff
# 4. 再比 greedy 生成的前 N 个 token
assert hf_generate(ids)[0, :16].tolist() == mg_generate(ids)[0, :16].tolist()
```

注意：

- bf16 下 1e-3 的 logits 误差可能是正常的；fp32 下应更小；
- 如果 logits 接近但生成不同，查 RoPE（interleaved vs half）、attention mask、
  tokenizer 的特殊 token；
- 如果只有部分层差异，逐层 hook 比较 hidden state，定位第一个发散的层。

## 35.4 支持的模型家族

“支持某模型”是一个**版本化的事实**，不能按家族名一句话概括。不同 Bridge
版本对同一家族的支持程度可能是（这里用 **S1–S4（Support）**，避免与第 36.1
节的 L1–L4 发布分级、附录 J 的 E1–E4 证据分级混淆）：

| 级别 | 含义 |
|---|---|
| S1 转换 | 有 checkpoint mapping，能 HF → Megatron |
| S2 训练 | 有 recipe/并行配置，能复现预训练/SFT |
| S3 对齐 | 有数值校验脚本与已知误差范围 |
| S4 生产 | 有性能调优、长上下文/多模态/量化的验证 |

截至本书基线，常见家族包括 Llama 3.x、Qwen 2.5/3、DeepSeek V2/V3/V4
（V4 在 0.20.0 release notes 中列为 HybridModel 支持，Bridge 0.7 有对应
provider/config）、Mixtral/Mixtral 类 MoE、Gemma、Phi、GPT-OSS 等；具体到某个 3.5/4.x 变体、
某个 MoE/MLA/多模态结构，必须查当前 Bridge 的 model registry、recipe 目录和
release notes，不要根据本文列表下单。旧模型（Llama 2、Mistral 7B 等）可能
只保留转换路径，不再维护训练 recipe。

选型时至少确认：mapping 类名、支持的 TP/PP/EP 组合、tokenizer 版本、
数值校验脚本是否存在，以及最近一次 CI 是否覆盖该模型。

## 35.5 NeMo、vLLM、TensorRT-LLM

- **NeMo**：企业训练平台，使用 Megatron Core 作为后端之一，提供数据/实验/
  部署/Guardrails 等。
- **vLLM**：高吞吐推理引擎，支持 HF 权重、PagedAttention、连续批处理；
  Megatron 训练的模型通常先转 HF 再交给 vLLM。
- **TensorRT-LLM**：NVIDIA 推理优化，支持 FP8/FP4、in-flight batching、
  multi-GPU/多节点。
- **Megatron Core inference**：把推理原语纳入 Core，支持 chunked prefill/
  prefix cache/speculative/prefill-decode 分离。

选型：

| 需求 | 建议 |
|---|---|
| 研究/预训练 | Megatron-LM examples + Core |
| HF 模型微调 | Bridge recipes / NeMo |
| 高吞吐在线推理 | vLLM / TensorRT-LLM |
| 极致 NVIDIA 硬件性能 | TensorRT-LLM / Core inference |
| 企业平台/合规 | NeMo |

## 35.6 互操作的工程问题

- **权重格式**：HF 的 fused QKV vs Megatron 的分片 QKV；GQA 的 KV 复制；
- **PP 切分**：层到 stage 的映射、embedding/output 位置；
- **TP 分片**：行/列方向、词表 padding；
- **精度**：FP8 scale/amax 的转换；
- **tokenizer**：特殊 token/词表大小；
- **配置**：rope base、norm epsilon、activation、tie embedding；
- **验证**：转换后 logits 对齐、生成样例、loss 对比。

一个常见错误：转换后模型能跑，但 logits 有微小差异，生成质量下降。原因可能是
QKV 顺序、RoPE interleaved、norm dtype、GQA repeat 顺序。必须做数值校验。

### 35.6.1 失败模式速查

| 现象 | 首要怀疑 | 验证 |
|---|---|---|
| 加载时报 shape mismatch | TP/PP 切分方向错 | 打印每个参数的全局/本地形状 |
| logits 全 NaN | FP8 scale/amax 未反量化 | 对比 fp32 权重 |
| logits 接近但生成乱码 | RoPE interleaved / QKV head 顺序 | 固定 prompt 比 argmax |
| 只有部分层 hidden 发散 | GLU gate/up 顺序 / bias | 逐层 hook 比较 |
| 多卡结果与单卡不同 | TP 分片的 head 分组错 | TP=1 vs TP=2 logits |
| PP 导出后 loss 变差 | stage 层映射 / tied embedding | 对比每层参数哈希 |
| tokenizer 特殊 token 错位 | 词表 padding / added tokens | 比 token id 与 decode |

### 35.6.2 一个最小回归测试

把转换纳入 CI 的最小闭环：

```python
def test_conversion_roundtrip():
    hf = load_hf_model()
    mg = convert(hf.state_dict(), tp=2, pp=2)
    back = convert_back(mg)
    for name, tensor in hf.state_dict().items():
        assert torch.allclose(back[name], tensor, atol=1e-5), name
    assert max_logit_diff(hf, mg) < 1e-3
```

这个测试不验证“模型好不好”，只验证“转换没有丢/错/重排参数”。它比人工抽查
可靠得多，也是 Bridge 这类工具的核心价值。

## 35.7 mini 与 Bridge 的关系

mini 的 `sharding.py` 只做 TP/PP 切片，不处理 HF 命名/QKV 融合/FP8。但它展示
了 Bridge 的核心思想：

```text
全局参数语义 → 按并行布局切片 → 加载/导出 → 数值校验
```

如果你要写自己的转换器，建议：

1. 列出每个参数的全局形状/语义；
2. 写 mapping（名称/转置/融合/切分）；
3. 单卡加载后对比 logits；
4. 再多卡/PP 验证；
5. 加回归测试。

## 35.8 本章小结

- Bridge 解决 HF ↔ Megatron 的转换/recipes/验证。
- 映射类型对应 TP 行/列、QKV、GLU、复制参数。
- NeMo/vLLM/TensorRT-LLM 各有定位，通常组合使用。
- 互操作的坑在权重顺序/精度/tokenizer/配置，必须数值校验。
- mini 的 sharding 是理解 Bridge 的简化入口。

下一章看 2026–2027 的路线图与技术判断。
