# 第 19 章 Transformer Block 与 Pipeline Stage

## 19.1 Block 的结构

mini 的 `TransformerBlock` 是 pre-norm decoder block：

```text
x = x + Dropout(Attention(LayerNorm(x)))
x = x + Dropout(MLP(LayerNorm(x)))
```

对应代码：

```python
residual = x
h = self.input_layernorm(x)
attn_out = self.self_attention(h, position_ids=...)
h = residual + self.hidden_dropout(attn_out)

residual = h
h = self.post_attention_layernorm(h)
mlp_out, aux = self.mlp(h)  # MoE 返回 aux
h = residual + self.hidden_dropout(mlp_out)
```

为什么 pre-norm：深层网络更稳定，梯度不需要穿过 LayerNorm 的缩放；GPT-2 之后
成为主流。post-norm 需要更精细的初始化/warmup。

## 19.2 激活重计算

```python
if self.activation_checkpointing and self.training:
    return checkpoint(
        self._forward_impl,
        x,
        position_ids,
        attention_mask,
        past_key_value,
        use_cache,
        use_reentrant=False,
    )
```

- 只保存 block 输入，反向重算；
- `use_reentrant=False` 对包含非张量输出/自定义 autograd 更友好；
- 真实函数签名是 `_forward_impl(x, position_ids, attention_mask,
  past_key_value, use_cache)`，少传参数会在反向重算时直接 TypeError；
- `_forward_impl` 返回 `(hidden, aux_loss, present)`，MoE aux 是张量，和
  hidden 一起返回；非 MoE 时 aux 是 `x.new_zeros(())`；
- 重算会重新执行 attention 的 CP gather/TP all-gather，通信次数增加，需权衡。
  mini 只做“整 block”重算，生产 Megatron 支持 selective activation
  recomputation（只重算 attention/MLP 的一部分）和更细的粒度。

## 19.3 Stage 的切分

`TransformerStage` 把一个完整 GPT 切成 PP 段：

```python
layers_per_stage = num_layers // pp_size
start = pp_rank * layers_per_stage
self.layer_indices = range(start, start + layers_per_stage)
```

- 第一个 stage：`embedding`（+ 可选 learned position）；
- 中间 stage：只有 `layers`；
- 最后一个 stage：`final_norm` + `output_layer`；
- PP=1 时三部分在同一个 stage。

为什么要求 `num_layers % pp_size == 0`：mini 用均匀切分，简单可验证。生产
Megatron 支持 uneven pipeline（每个 stage 层数不同）以及 interleaved/VPP，
需要更复杂的 `PipelineStage` 和调度。

## 19.4 前向数据流

```python
def forward(input_ids=None, hidden_states=None, labels=None, return_logits=False):
    if first:
        if cp > 1: input_ids = scatter_cp(input_ids); labels = scatter_cp(labels)
        x = embedding(input_ids)
        positions = _position_ids(...)
        if learned: x += position_embedding(positions)  # SP 时按序列 chunk
    else:
        x = hidden_states
        positions = _position_ids(...)
        if labels and cp > 1: labels = scatter_cp(labels)

    for layer in layers:
        x, layer_aux, present = layer(x, positions, attention_mask,
                                      past_key_value=past, use_cache=use_cache)
        aux = aux + layer_aux
    if last:
        x = final_norm(x)
        logits = output_layer(x) if labels/return_logits
        loss = CE(logits, labels) if labels
        return StageOutput(x, loss, aux, logits)
    return StageOutput(x, aux_loss=aux)
```

几个容易错的点：

### 1. 位置编码的序列长度

- 普通：`positions = 0..s-1`；
- CP：`positions = cp_rank*s_local .. +s_local-1`；
- SP：非第一个 stage 的 `x` 是 `[b, s/t, h]`，但 attention 会 all-gather 成
  `s`，所以 position ids 必须是完整 `s`。mini 在 `_position_ids` 中把
  `seq_len` 乘以 `tp_size`（非 first stage）。

### 2. CP 的输入切片

`input_ids/labels` 在第一个/最后一个 stage 按 CP 切。第一个 stage 的
embedding 只处理本地 token；最后一个 stage 的 CE 只处理本地 token，并在 CP
组内做全局归一化。

### 3. SP 的 learned position

`position_embedding(positions)` 得到完整序列的 position embedding，然后按 TP
rank chunk 成 `[b, s/t, h]`，与 embedding 输出对齐。

### 4. 输出层

`output_layer` 是 Column Parallel，`gather_output=False`，输出 `[b, s, V/t]`。
SP 下它会先 all-gather 序列，输出完整序列的 logits；CP 下只输出本地序列。
两者都交给 `vocab_parallel_cross_entropy`。

## 19.5 权重绑定

`tie_word_embeddings=True` 时：

```python
self.output_layer.weight = self.embedding.weight
```

要求 PP=1（embedding 与 output 同 stage）。共享 Parameter 的梯度会累加；如果
同时开 SP，输出层会 all-gather 序列、embedding 输入完整，两者的梯度语义不同，
mini 直接禁止这个组合。

## 19.6 MoE 的 aux loss

Block 返回 `(hidden, aux_loss)`；Stage 累加所有层的 aux，并乘以
`moe_aux_loss_coeff`。Pipeline schedule 在反向时把 aux 与主 loss 一起
`torch.autograd.backward([hidden, aux], [grad, 1])`，确保共享的路由计算图只
反向一次。

如果单独 `aux.backward()` 再 `loss.backward()`，共享子图会被释放，报
“Trying to backward through the graph a second time”。这是一个常见的 MoE
实现错误。

## 19.7 Stage 的参数统计

`num_parameters()` 只统计当前 stage 的参数。CLI 的输出
`parameters_this_stage` 不是全模型参数量；全模型要按 TP/PP 汇总：

```text
N_total ≈ sum_over_stages(tp * local_params)   # 近似，复制参数会重复
```

更准确的做法是按参数名去重/标记复制参数。生产 Megatron 的
`num_parameters` 工具会处理这些细节。

## 19.8 测试

- `test_forward_backward_and_gqa`；
- `test_learned_position_embedding`；
- `test_moe_aux_loss_and_backward`；
- `test_tied_embeddings`；
- `test_activation_checkpointing`；
- `parallel_equiv.py --case pp`：PP=2 的 loss/梯度。

## 19.9 本章小结

- Pre-norm block 是主流；残差路径保持 dtype/布局一致。
- Stage 的切分决定 embedding/output 的位置；PP=1 才能 tie embedding。
- 位置编码在 CP/SP 下的序列长度最容易错。
- MoE aux 必须和主 loss 一起反向，避免共享子图被释放。
- 参数统计要区分“本 stage 本地参数”和“全模型参数”。

下一章拆解词表并行交叉熵：一个看似简单、实则最容易出数值错误的模块。
