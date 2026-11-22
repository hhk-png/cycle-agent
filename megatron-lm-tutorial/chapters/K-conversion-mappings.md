# 附录 K 模型转换映射速查

> 本附录是 Hugging Face ↔ Megatron 权重映射的“查表 + 陷阱”手册。具体类名/
> 参数名随 Megatron Bridge 版本变化，映射逻辑和数值校验方法相对稳定。
>
> 版本锚点：Megatron 列名以 Core 0.20.x 的常见命名为例；Bridge 的
> `AutoMapping`/recipe 类/CLI 在独立仓库中演进，落地时以当前
> `megatron.bridge` 版本的 mapping 类和 `--help` 为准，不要照抄本书的
> 字符串。映射表的价值在“顺序/切分/校验”，不在某个版本的 API 拼写。

## K.1 转换的三层

一次正确的转换包含三层，缺一层就可能“能加载但算错”：

```text
1. 名称映射：HF key → Megatron key
2. 形状/布局映射：QKV 融合、head 重排、TP/PP 切分
3. 数值校验：固定输入下的 logits/loss/生成对齐
```

只做第 1 层，模型可能加载成功但 attention 已经坏了；只做第 1+2 层，仍可能
因为 RoPE/mask/tokenizer 差异而错。第 3 层是必须的。

## K.2 通用 decoder-only（Llama/GPT 类）

下表省略了 GPTModel 的 `decoder.` / `language_model.` 前缀（随模型容器和版本
变化）；mini 的命名更短（如 `final_norm.weight`），不要直接把 mini 名当
Core 名。

| HF 名称 | Megatron 名称 | 形状/布局 | 注意 |
|---|---|---|---|
| `model.embed_tokens.weight` | `embedding.word_embeddings.weight` | `[V, h]` | 词表 padding；tied 时与 output 共享 |
| `model.layers.i.input_layernorm.weight` | `layers.i.input_layernorm.weight` | `[h]` | 复制参数，不切 |
| `self_attn.q_proj.weight` | `self_attention.linear_qkv.weight` 的一部分 | `[n_h·d, h]` | 与 k/v 融合，顺序必须匹配 |
| `self_attn.k_proj.weight` | 同上 | `[n_kv·d, h]` | GQA 时 `n_kv < n_h` |
| `self_attn.v_proj.weight` | 同上 | `[n_kv·d, h]` | 不要复制到每个 query head |
| `self_attn.o_proj.weight` | `self_attention.linear_proj.weight` | `[h, n_h·d]` | Row Parallel，按输入切 |
| `mlp.gate_proj.weight` | `mlp.linear_fc1.weight` 的 gate 部分 | `[ffn, h]` | 与 up 融合/交错 |
| `mlp.up_proj.weight` | 同上 up 部分 | `[ffn, h]` | gate/up 顺序必须一致 |
| `mlp.down_proj.weight` | `mlp.linear_fc2.weight` | `[h, ffn]` | Row Parallel |
| `model.norm.weight` | `decoder.final_layernorm.weight` | `[h]` | PP 最后 stage；mini 里叫 `final_norm` |
| `lm_head.weight` | `output_layer.weight` | `[V, h]` | 词表并行；tied 时共享 |

### K.2.1 QKV 融合顺序

Megatron 的 `linear_qkv` 通常把 Q/K/V 按 head 拼成一个矩阵，但**拼接顺序**
（`[Q;K;V]` vs 交错）和 GQA 的 KV 重复方式因版本/模型而异。转换时不要对
融合矩阵直接 `chunk(3)`：

```text
q = w_q.view(n_h, d, h)
k = w_k.view(n_kv, d, h)
v = w_v.view(n_kv, d, h)
# 按目标实现的 head 顺序拼接；GQA 不做 repeat_interleave
w_qkv = cat([q, k, v], dim=0)
```

如果目标实现把 KV head 复制到每个 query head（MHA 风格），需要显式
`repeat_interleave`；如果实现内部做 repeat（Megatron 常见），则不要复制。
这两种做法在数值上等价，但权重形状不同，混用会直接 shape mismatch 或
静默错位。

### K.2.2 GLU 的 gate/up

`linear_fc1` 的输出可能是 `[gate; up]`（连续）或交错。转换后必须验证：

```text
fc1(x) 的前 ffn 维 == gate_proj(x)
fc1(x) 的后 ffn 维 == up_proj(x)
```

如果顺序反了，模型仍能跑，但 MLP 表达完全变了。最可靠的验证是固定输入，
比较 `silu(gate)*up` 的逐元素输出。

## K.3 MoE 模型

| HF 名称 | Megatron 名称 | 注意 |
|---|---|---|
| `mlp.gate.weight` | `router.weight` | 路由 logits；softmax/sigmoid 类型要对 |
| `experts.i.gate_proj.weight` | `experts.local_experts.j.linear_fc1` | 专家编号重排 |
| `experts.i.down_proj.weight` | `experts.local_experts.j.linear_fc2` | EP/TP 切分 |
| `shared_expert.*` | `shared_experts.*` | 是否共享、如何并行 |
| `mlp.gate.e_score_correction_bias` | `router.e_score_correction_bias` | aux-free 路由的 bias |

MoE 转换的额外陷阱：

- **专家编号**：HF 的 `experts.i` 与 Megatron 的 local expert 编号在 EP 下
  不是一一对应；要按全局专家 id 重排；
- **路由类型**：softmax top-k、sigmoid top-k、group-limited、aux-free bias
  的公式不同，不能只搬权重；
- **shared expert**：有的实现是独立 MLP，有的合并进专家矩阵；
- **TP/EP 布局**：专家权重可能按 TP 切、按 EP 分；转换时要先还原全局专家，
  再按目标布局切；
- **容量/ dropless**：推理/训练的 capacity 逻辑不影响权重，但影响数值。

## K.4 MLA（DeepSeek 类）

| HF 名称 | Megatron/本书名称 | 形状 | 注意 |
|---|---|---|---|
| `q_a_proj.weight` | `W_DQ` | `[q_rank, h]` | 低秩 query 下投影 |
| `q_b_proj.weight` | `W_UQ` | `[n_h·(d_nope+d_R), q_rank]` | 含 nope+rope 两部分 |
| `kv_a_proj_with_mqa.weight` | `W_DKV` + `W_KR` | `[d_c+d_R, h]` | 前 `d_c` 是 latent，后 `d_R` 是共享 RoPE key |
| `kv_b_proj.weight` | `W_UK` + `W_UV` | `[n_h·(d_nope+d_v), d_c]` | 前 nope，后 value |
| `o_proj.weight` | `W_O` | `[h, n_h·d_v]` | Row Parallel |

关键检查：

1. `kv_a_proj_with_mqa` 是否把 latent 和 RoPE key 拼在一起；切分点必须是
   `d_c`，不是 `d_c + d_R` 的中间；
2. `q_b_proj` 的输出是否包含 RoPE 分量；如果 RoPE 是独立 `q_rope` 投影，
   映射不同；
3. `k_R` 是**共享单 head**，不要按 `n_h` 复制；
4. 吸收（absorption）只在推理时做，训练权重不要预先吸收；
5. RoPE 只加在 rope 分量上，且 interleaved/half 风格要与原模型一致。

本书的 `parallel/mla.py` 可以作为形状参考：`kv_b_proj` 输出
`n_h*(d_nope+d_v)`，前 `d_nope` 给 K，后 `d_v` 给 V；`k_pe` 只有 1 个 head。

## K.5 多模态

| 组件 | HF 名称示例 | Megatron 名称 | 注意 |
|---|---|---|---|
| 视觉 encoder | `vision_model.*` | `vision_model.*` | 可能独立并行组 |
| projector | `multi_modal_projector.*` | `projector.*` | 2 层 MLP/Q-Former |
| LLM | `language_model.*` | `language_model.*` | 与纯文本相同 |
| image token | `image_token_id` | 特殊 token | 必须在词表中 |
| 位置 | `image_grid_thw` | M-RoPE 参数 | 与 packing 布局一致 |

多模态转换的常见错误：

- 图像 token 的 loss mask 没设，模型去预测 patch；
- 视觉 encoder 和 LLM 用同一个 TP group，导致不该跨节点的 all-reduce；
- projector 的 dtype/归一化与训练不一致；
- 图像分辨率/patch 数与训练配置不同，序列长度爆炸。

## K.6 TP/PP 切分规则

| 参数类型 | TP | PP | 备注 |
|---|---|---|---|
| embedding/output | 按词表行切 | stage 0 / last | tied 时逻辑共享 |
| QKV/FC1 | 按输出维（head/FFN）切 | 按层 | Column Parallel |
| O/FC2 | 按输入维切 | 按层 | Row Parallel |
| norm/bias/router | 复制 | 按层 | 需要梯度归约 |
| MoE experts | 按专家/TP 切 | 按层 | EP 另有分组 |
| MLA latent | 通常复制/按维切 | 按层 | 共享 `k_R` 不切 |

PP 切分时，层号要还原成全局层号再映射；不要把“stage 内第 0 层”当成
“全局第 0 层”。这是重分片/转换中最常见的错位来源。

## K.7 数值校验脚本模板

```python
import torch

@torch.no_grad()
def check(hf_model, mg_model, tokenizer, prompts):
    for text in prompts:
        ids = tokenizer(text, return_tensors="pt").input_ids
        hf_logits = hf_model(ids).logits.float()
        mg_logits = mg_model(ids).float()
        diff = (hf_logits - mg_logits).abs().max().item()
        assert diff < 1e-3, (text, diff)
        # 生成也要比：greedy 前 N 个 token
        assert hf_generate(hf_model, ids)[0, :16].tolist() == \
               mg_generate(mg_model, ids)[0, :16].tolist()
```

分层定位：

1. 先比 embedding 输出；
2. 再比第 0 层 attention/MLP 的 hidden；
3. 找到第一个发散的层/算子；
4. 对比该层的 Q/K/V、RoPE、mask、softmax；
5. 最后比 logits/生成。

不要一上来就比最终 loss；层内定位能省大量时间。

## K.8 转换验收清单

- [ ] tokenizer/vocab size/padding 一致；
- [ ] tied/untied embedding 明确；
- [ ] QKV 融合顺序与 GQA 复制策略一致；
- [ ] GLU gate/up 顺序一致；
- [ ] RoPE 风格（interleaved/half）、base、scaling 一致；
- [ ] attention mask/padding 语义一致；
- [ ] MoE 专家编号/路由类型/shared expert 一致；
- [ ] MLA latent/RoPE 分量切分点一致；
- [ ] TP/PP/EP 切分与 checkpoint 布局一致；
- [ ] 固定输入 logits 误差在容差内；
- [ ] greedy 生成前 N token 一致；
- [ ] 记录转换脚本/版本/日期，纳入回归。

---

上一附录：[附录 J 版本、路线图与声明登记表](J-claim-register.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 L 练习、项目与面试题](L-exercises.md)
