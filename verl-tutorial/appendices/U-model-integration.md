# 附录 U 模型接入与权重加载

"verl 支持某个模型"不是一句开关。一个模型要真正跑进 PPO/GRPO，必须同时穿过五道边界：配置转换、模型构建、权重加载、权重导出、推理后端。任何一道没对齐，症状都可能只是"loss 不降"或"生成质量差"，而不是报错。

本附录以 v0.9.1 的 `verl/models/` 与 `verl/workers/engine/` 为基线，讲清楚这五道边界和接入新模型的检查清单。

## U.1 两条模型路径

verl 有两条并行的模型路径：

| 路径 | 入口 | 适用 | 代价 |
|---|---|---|---|
| HF Transformers | `verl/models/transformers/` | FSDP/FSDP2、单机到中等规模 | 显存效率一般，依赖 HF 实现 |
| Megatron Core | `verl/models/mcore/` | TP/PP/CP/EP、大模型 | 需要配置转换与权重映射 |

FSDP 路径直接使用 HF 的 `AutoModelForCausalLM`（或 verl 的 monkey patch 版本），权重就是 HF state_dict，加载/保存相对直接。Megatron 路径要把 HF 的 `PretrainedConfig` 转成 `TransformerConfig`，把 HF 的权重张量切到并行布局，再把训练后的分片权重合并回 HF 格式；v0.9.1 默认由 Megatron-Bridge 完成这些转换，legacy registry 只在显式关闭 bridge 时使用。

v0.9.1 还增加了第三条路：**外部引擎适配**（Automodel、TorchTitan、VeOmni）。它们把模型构建、并行、优化器分片、LR schedule、checkpoint 委托给外部框架，verl 只保留训练循环、数据管线与 loss。这个设计把"模型接入"从"改 verl"变成"实现一个 engine 适配层"。

## U.2 两条 Megatron 接入路径：Bridge 与 legacy registry

v0.9.1 的 Megatron 引擎默认 `use_mbridge=True`（`verl/trainer/config/engine/megatron.yaml`），模型/配置/权重转换由 **Megatron-Bridge** 负责。`verl/models/mcore/readme.md` 明确把"按模型逐个写转换函数"的旧路径标为 deprecated。

`verl/models/registry.py` 的 `ModelRegistry` 属于 **legacy 路径**：

```python
_MODELS = {
    "LlamaForCausalLM": ("llama", (...)),
    "Qwen2ForCausalLM": ("qwen2", (...)),
    "MistralForCausalLM": ("mistral", (...)),
    "ApertusForCausalLM": ("apertus", (...)),
}
```

`load_model_cls(model_arch, value=False)` 动态 import 对应的 Megatron 模型类，再按 actor/ref 或 critic/rm 选择。这张表只覆盖四个架构，而且它的调用入口在 v0.9.1 里没有实际 call site。**不要用它来判断"某个模型是否支持"**；默认路径要看 Megatron-Bridge 的 provider 是否覆盖该架构，只有显式关闭 bridge 时才走 legacy registry。

legacy registry 仍然值得读，因为它把"新模型需要哪些件"列得最清楚：

1. HF config → Megatron `TransformerConfig` 的转换；
2. 并行模型类（`Parallel*ForCausalLMRmPadPP` 等）；
3. HF → Megatron 的权重加载器；
4. Megatron → HF 的权重保存/合并器；
5. 前向函数（dense/MoE/VLM 的 packing 方式不同）。

`verl/models/mcore/registry.py` 进一步区分 dense、MoE、VLM：`supported_vlm` 列出 Qwen2.5-VL、Qwen3-VL、Qwen3-MoE-VL、Qwen3.5-VL 等，`get_mcore_forward_fn` 按架构返回对应的前向生成器。VLM 与纯文本模型的差别不只在视觉塔，还在 packed sequence 的布局与 attention mask。

## U.3 legacy 权重加载：HF state_dict → Megatron 分片

`verl/models/mcore/loader.py` 的 `load_state_dict_to_megatron_gptmodel(state_dict, wrapped_models, config, params_dtype, is_value_model=False)` 做四件事：

1. 按 PP rank 计算当前 rank 拥有的层（`_megatron_calc_layer_map`）；
2. 把 HF 的 `model.layers.N.*` 映射到本地层号；
3. 按 **TP** 切分 QKV、MLP、embedding、lm_head；
4. 处理 value model 的额外 head。

这个 loader **只做 TP 切分**，没有 EP/ETP 的专家布局逻辑；也**不处理 tied embeddings**（tied 的处理在 saver 侧）。这是 legacy 路径的边界，不要把它当成默认 bridge 路径的能力清单。

常见的映射陷阱（legacy 路径）：

- **QKV 融合**：HF 可能存 `q_proj/k_proj/v_proj`，Megatron 存融合的 `qkv`；切分顺序与 head 交错方式必须一致。
- **gate/up 融合**：MoE/MLP 的 `w1/w3` 与 `gate_up` 的排列不同。
- **词表 padding**：TP 要求 vocab 能被 TP size 整除，加载时要 pad，保存时要裁掉。
- **tied embeddings**：`tie_word_embeddings=True` 时 lm_head 不独立存在；legacy loader 不处理，必须在 saver/合并侧避免重复或丢失。
- **MoE 专家布局**：EP/ETP 下专家按 rank 分布；legacy loader 不覆盖，默认 bridge 路径另有实现。
- **MLA**：DeepSeek 系列的 KV 压缩投影有独立的 latent 维度，不能按普通 MHA 处理。

`verl/models/weight_loader_registry.py` 的 `get_weight_loader(arch)` 只对 Llama/Qwen2 返回通用 loader，其余架构直接抛错。这是一种"早失败"设计：宁可启动时报"不支持"，也不要在训练中途产生错误的权重。

## U.4 legacy 权重保存与离线合并

`get_weight_saver(arch)` 的注册表看起来覆盖很多架构，但 v0.9.1 里只有 dense 的 `merge_megatron_ckpt_gptmodel` 是完整实现；`merge_megatron_ckpt_gptmodel_mixtral`、`..._qwen_moe`、`..._qwen2_5_vl`、`..._dpskv3` 在 `verl/models/mcore/saver.py` 里都是直接 `raise NotImplementedError` 的桩。`get_weight_saver` 本身在仓库里也没有 call site。

因此"注册表里有名字"不等于"能保存"。读这张表时，先看函数体，而不是只看键。

真正可用的 **`verl/model_merger/`** 是一个**离线 CLI**：

```bash
python -m verl.model_merger merge --backend megatron --local_dir <ckpt> --target_dir <hf>
```

它把分片 checkpoint 合并成 HF 格式，适合训练结束后执行。它**不是**"每次保存 checkpoint 时自动合并"；每步/每 N 步的 HF 导出是 checkpoint manager 的能力（`verl/utils/checkpoint/checkpoint_manager.py` 等），不要混用。

合并本身是 loader 的逆操作：把各 TP/PP/EP rank 的分片收集起来，按 HF 的命名与形状拼回去，再写 safetensors。它必须处理：

- 各 rank 的层号偏移；
- TP 切分轴的逆拼接；
- QKV/gate-up 的反融合；
- 词表裁剪；
- tied embeddings；
- dtype 转换（训练 bf16/fp32 → 保存 dtype）。

## U.5 引擎后端：FSDP2、Megatron、Automodel、TorchTitan、VeOmni 与 MindSpeed

v0.9.1 的 `verl/workers/engine/` 下有多个后端：

| 后端 | 并行 | 特点 | 限制 |
|---|---|---|---|
| FSDP/FSDP2 | DP（+ HSDP） | 与 HF 模型兼容最好，接入成本低 | 无 PP；超大模型/长序列显存压力大 |
| Megatron | TP/PP/CP/EP | 极致规模，MoE 原生 | Bridge/provider 或 legacy 转换；配置复杂 |
| Automodel | FSDP2/TP/EP | 直接吃 HF checkpoint，无需转换；MoE + DeepEP | 不支持 PP |
| TorchTitan | FSDP2/TP/CP/EP | N-D 并行、`torch.compile`、多种 attention backend | nightly 依赖；不支持 PP |
| VeOmni | FSDP2/SP(Ulysses)/EP | MoE 专家并行、多模态 | 无 TP（只有 FSDP2 + Ulysses SP + EP） |
| MindSpeed | TP/PP/EP | Ascend NPU 原生 | 绑定华为栈 |

选择逻辑不是"越强越好"，而是"你的模型和集群能不能承受它的约束"：

- 7B～32B dense、单机多卡：FSDP2 足够，接入最快；
- 32B+ 或 MoE、需要 TP/EP：Megatron 或 TorchTitan；
- 想直接用 HF checkpoint、不想写转换：Automodel；
- 需要多模态 + MoE + 专家并行：VeOmni 是候选；
- 需要 PP：目前只有 Megatron 实现（FSDP 没有 PP 字段/流水线调度，Automodel/TorchTitan 明确不支持）。

引擎后端的统一契约在 `verl/workers/engine_workers.py` 与 `verl/workers/engine/base.py`：`initialize`、`train_mode/eval_mode`、`forward_backward_batch`、`optimizer_step`、`get_per_tensor_param`、`save/load_checkpoint`。附录 M 给出更完整的 BaseEngine/ShardSpec/EngineRegistry 说明。

## U.6 接入一个新模型：检查清单

### 配置与 tokenizer

- [ ] `architectures` 只有一个；默认 bridge 路径确认 provider 支持，legacy 路径确认 registry 键存在。
- [ ] `model.path` 指向本地或 HF hub；离线集群要预下载。
- [ ] tokenizer 的 `pad_token`、`eos_token`、`chat_template` 与训练数据一致。
- [ ] 词表大小能被 TP size 整除，或确认 loader/saver 会 pad/crop。
- [ ] `trust_remote_code` 只在确认来源可信时开启。

### 模型结构

- [ ] attention 类型（MHA/GQA/MQA/MLA）与后端 kernel 匹配。
- [ ] RoPE 的 base、scaling、partial rotary 与 HF 配置一致。
- [ ] MoE 的专家数、top-k、shared expert、capacity factor、router 类型（softmax/sigmoid/hash）明确。
- [ ] tied embeddings 的语义在加载/保存/合并三处一致。
- [ ] 多模态的视觉塔、projector、image token 展开方式与 rollout 后端一致。

### 权重

- [ ] 有 HF → 训练后端的 loader。
- [ ] 有训练后端 → HF 的 saver/merger。
- [ ] QKV/gate-up 的融合顺序经过逐张量比对。
- [ ] 词表 padding、tied weights、value head 处理正确。
- [ ] 合并后的 safetensors 能被原始 HF 模型直接加载。

### 推理后端

- [ ] rollout 引擎（vLLM/SGLang）支持该架构，或提供自定义 loader。
- [ ] 量化格式（FP8/FP4/MXFP4）在训练→推理同步时保持一致的 scale 语义。
- [ ] 权重更新后，rollout 与 actor 的 log-prob 相关性在阈值内（见 U.7）。

### 训练

- [ ] 参考模型与 actor 使用同一套权重加载逻辑。
- [ ] critic/value head 的初始化与保存路径明确。
- [ ] checkpoint 能恢复模型、优化器、scheduler、RNG 与 trainer 状态。
- [ ] 多轮/多模态的 mask 与 position_ids 经过小样本对拍。

## U.7 验证：不要只看 loss

模型接入的正确性要用**分层验证**，而不是"跑起来就算成功"：

1. **权重逐张量比对**：加载后，用同一输入分别跑 HF 与训练后端，比较中间层输出；至少比较 embedding、第一层 attention、最后一层 hidden state、lm_head logits。
2. **logits 对齐**：同一 prompt、同一 dtype、无采样，比较 top-1 token 与前 10 个 logits 的误差。bf16 下允许小量级差异，但 top-1 不应频繁不同。
3. **log-prob 对齐**：rollout 记录的 `rollout_log_probs` 与 actor 重算的 `old_log_probs` 的 Pearson 相关、最大/均值/标准差差异。相关性低通常指向权重、scale 或 packed layout 问题。
4. **生成一致性**：greedy 解码下，训练后端与推理后端应产生相同或近乎相同的序列；temperature>0 时比较分布而不是逐 token。
5. **保存-重载一致性**：训练 → 保存 → 合并 → 用 HF 加载 → 再推理，输出应与保存前一致。
6. **checkpoint 完整性**：恢复后继续训练若干步，loss 曲线应与未中断时衔接，而不是跳变。

只跑 loss 的风险是：错误的权重布局可能仍然让 loss 缓慢下降（模型在拟合一个错误的表示），直到评测时才暴露。第 24 章与附录 V 给出更细的验证协议。

## U.8 常见失败模式

| 现象 | 可能原因 | 定位 |
|---|---|---|
| 启动报 unsupported architecture | 默认 bridge provider 不支持，或 legacy registry 没有该架构 | 先看 `use_mbridge`；legacy 路径再看 `ModelRegistry.get_supported_archs()` |
| 加载后 loss 为 NaN | scale/dtype 不匹配、RoPE 配置错 | 逐层比对 hidden state |
| 生成乱码但 loss 下降 | 权重合并顺序错、词表错位 | 比较 logits 与 top-1 |
| rollout 与 actor log-prob 相关性低 | 权重同步不完整、量化 scale 错 | 见附录 V |
| MoE 路由不稳定 | 专家布局/容量因子错 | 记录每层 expert counts |
| 保存后 HF 加载失败 | tied weights/词表 padding/key 名错 | 用 `safetensors` 检查 key 集合 |
| 多卡结果与单卡不一致 | TP 切分轴或 all-reduce 语义错 | 固定 seed，逐 rank 比对 |

## U.9 MiniVerl 的边界

MiniVerl 不实现真实的 HF/Megatron 权重转换；它的 `mini_verl/nn.py` 只有 TinyTransformer/TinyLinearLM，`mini_verl/engine.py` 的 `EngineRegistry` 用 dense replica 模拟 FSDP/Megatron 的训练语义。它能验证的是：

- 引擎契约（forward/backward/optimizer/export）；
- TP/PP/MoE 的布局与字节账（`mini_verl/parallel.py`）；
- 权重同步与 checkpoint 生命周期；
- 路由回放的记录/重放/层序检查（`replay_moe_routes`）。

它不能验证真实模型的数值对齐、量化 scale、MoE 专家分片。这些必须按 U.7 在真实 verl + GPU 上做。

## 延伸阅读

- `verl/models/registry.py`、`verl/models/weight_loader_registry.py`
- `verl/models/mcore/loader.py`、`verl/models/mcore/saver.py`、`verl/models/mcore/config_converter.py`
- `verl/models/mcore/registry.py`
- `verl/workers/engine/automodel/`、`verl/workers/engine/torchtitan/`、`verl/workers/engine/veomni/`
- 第 12 章：Megatron 引擎
- 第 23 章：Checkpoint、模型合并与 HF 导出
- 附录 M：引擎后端与扩展
- 附录 V：DeepSeek V4 的量化权重同步与路由回放
