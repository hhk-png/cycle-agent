# mini-vLLM

一个用**纯 NumPy** 实现的、可运行的 vLLM 简化版。它完整复刻了 vLLM 的
**数据布局**（分页 KV cache）、**调度逻辑**（连续批处理）与**服务形态**
（OpenAI 兼容 API），但没有 CUDA kernel、没有 GPU 依赖，可以在任何装有
NumPy 的机器上跑通并验证。

它是《vLLM 教程》第 6 章的配套代码。

## 目录结构

```
mini-vllm/
├── minivllm/
│   ├── __init__.py        # 包入口（导出 LLMEngine / EngineConfig / SamplingParams 等）
│   ├── __main__.py        # python -m minivllm 入口
│   ├── config.py          # EngineConfig / ModelConfig / CacheConfig / SchedulerConfig / SamplingParams
│   ├── tokenizer.py       # 字符级 tokenizer
│   ├── model.py           # TinyGPT（纯 NumPy transformer，权值与 lm_head 绑定）
│   ├── attention.py       # PagedAttention（分块注意力）
│   ├── kv_cache.py        # KVStore / BlockAllocator / PrefixCache / BlockManager（含 copy-on-write）
│   ├── scheduler.py       # 连续批处理 + 分块 prefill + 抢占（recompute）
│   ├── sampler.py         # 温度 / top-k / top-p / 采样
│   ├── sequence.py        # Sequence / RequestOutput
│   ├── engine.py          # LLMEngine
│   ├── async_engine.py    # AsyncLLMEngine（后台线程跑引擎）
│   ├── api_server.py      # OpenAI 兼容 FastAPI 服务器（含 SSE 流式）
│   ├── cli.py             # serve / chat / demo / spec / quant
│   ├── training.py        # 纯 NumPy 反向传播训练（教育用途）
│   ├── quantize.py        # int8 权重量化演示
│   ├── speculative.py     # bigram 草稿模型的投机解码演示
│   ├── checkpoint.py      # 保存/加载 模型+配置+tokenizer
│   └── data.py            # 训练语料
├── scripts/train.py       # 训练入口
├── tests/                 # 测试（PagedAttention / 引擎 / 服务器）
└── artifacts/tinygpt/     # 训练出的检查点
```

## 快速开始

```bash
# 0. 安装依赖（运行时 + 测试）
pip install -e .[test]        # 等价于 numpy fastapi uvicorn + httpx pytest

# 1. 训练玩具模型（约 6 分钟，产出 artifacts/tinygpt）
python scripts/train.py --steps 400 --embd 96 --layers 3 --out artifacts/tinygpt

# 2. 跑测试（验证正确性）
python tests/test_paged_attention.py   # PagedAttention 与稠密注意力数值一致
python tests/test_engine.py            # 确定性 / 批处理等价 / 分块 prefill / 前缀缓存 / 抢占 / 流式
python tests/test_server.py            # OpenAI 端点 + SSE 流式（需 httpx）

# 3. 连续批处理 demo
python -m minivllm demo --model artifacts/tinygpt

# 4. 启动 OpenAI 兼容服务器
python -m minivllm serve --model artifacts/tinygpt --host 0.0.0.0 --port 8000
curl http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"prompt":"vLLM is","max_tokens":32}'

# 5. 其他子命令
python -m minivllm chat  --model artifacts/tinygpt      # 交互式对话
python -m minivllm spec  --model artifacts/tinygpt      # 投机解码演示
python -m minivllm quant --model artifacts/tinygpt      # int8 量化演示
```

## 特性与对应关系

| 特性 | 真实 vLLM | mini-vLLM |
|------|-----------|-----------|
| 分页 KV cache | CUDA kernel + block manager | `kv_cache.py`（NumPy） |
| 连续批处理 | `vllm/core/scheduler.py` | `scheduler.py` |
| 分块 prefill | `--enable-chunked-prefill` | `_chunk_for()`（token+block 双预算） |
| 抢占 | SWAP / RECOMPUTE | `_preempt_one()`（recompute） |
| 前缀缓存 | V2 block manager + hash | `PrefixCache`（LRU + 引用计数） |
| OpenAI 服务器 | `vllm/entrypoints/openai/` | `api_server.py` + `async_engine.py` |
| 量化 | GPTQ / AWQ / FP8 / GGUF | `quantize.py`（int8 数据层） |
| 投机解码 | `vllm/spec_decode/` | `speculative.py`（bigram 草稿） |

## 设计说明

- **模型**：TinyGPT，2–3 层、64–96 维，权值与 lm_head 绑定，`state_dict()` /
  `load_state_dict()` 模仿 HF 权重接口。
- **KV cache 布局**：`(num_blocks, block_size, num_heads, head_dim)`，与 vLLM
  完全一致；每个序列持有一张"逻辑 block → 物理 block"的 block table。
- **Copy-on-Write**：共享 block（前缀缓存产生）要写入时先复制再写，保证不
  污染其它使用者。
- **decode 约定**：decode 时 `cached_len == num_tokens - 1`，最新 token 的 KV
  在当步 forward 中写入；prefill 完成后做一次 `cached_len -= 1` 统一两条路径。

## 正确性验证

`tests/test_engine.py` 证明三个关键不变量：

1. **分块 prefill 等价**：超小 token 预算下的输出 == 一次全量 prefill 的输出。
2. **批处理等价**：多个请求一起跑 == 各自单独跑（固定 seed）。
3. **抢占收敛**：严重超订 KV cache 时所有请求最终都能完成。

> 说明：模型是玩具（在几百字符语料上训练几分钟），生成的文本不是真实英文，
> 但**引擎机制是真实的**——PagedAttention、连续批处理、前缀缓存、抢占、
> 分块 prefill、流式服务全部可运行、可验证。

**测试覆盖边界（诚实说明）**：`tests/` 目前只有 3 个文件，焊死的是"分页注意力数值正确 + 引擎不变量 + 服务器端点"这三类核心承诺。COW、前缀缓存引用计数、采样器、checkpoint 往返、量化误差、投机无损等模块**暂时没有测试守护**——它们由第 6 章 / 附录 C 的扩展练习（如练习 5 前缀命中率指标、练习 7 采样惩罚项）引导你去补，补完再把这些模块"焊死"。
