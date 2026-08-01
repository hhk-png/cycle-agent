# 18 · 附录 C：mini-vLLM 完整工程参考

> 本章是全教程的"工程手册"：把第 06 章讲透的 mini-vLLM 从"能看懂"升级为"能上手改"。内容包括完整 API 参考、关键数据约定、工程化检查清单、可复现的验证矩阵、9 个由浅入深的练习（练习 0–8，含 1 个热身），以及常见陷阱与调试方法。读完这一章，你应该有信心给 mini-vLLM 加一个新特性，或者把它移植进自己的项目。

---

## 1. 工程目标与设计原则

mini-vLLM 不是"能跑的玩具代码"，它刻意按真实工程规范组织。它的五条原则正好对应 vLLM 团队自己维护代码的方式：

### 1.1 职责单一（Single Responsibility）

```
配置层   config.py        # 所有参数收敛为 dataclass，杜绝魔法数字散落
数据层   tokenizer.py / sequence.py / data.py
核心层   kv_cache.py / scheduler.py / attention.py / sampler.py
模型层   model.py         # 与核心层解耦：模型只认 "block table + KVStore"
引擎层   engine.py / async_engine.py    # 唯一有状态的协调者
服务层   api_server.py / cli.py         # 薄封装，不含业务逻辑
```

判据：**管内存的文件不 import 调度器，管调度的文件不 import 采样器。** 打开 `scheduler.py` 看它的 import 就明白——它只依赖 `config.py`、`kv_cache.py`、`sequence.py`。

### 1.2 配置收敛（Configuration Convergence）

真实 vLLM 用 `EngineArgs` 解析上百个 CLI 参数，再拆成 `ModelConfig / CacheConfig / SchedulerConfig / ParallelConfig`。mini-vLLM 用三个 dataclass 复刻了同样的拆分：

```python
EngineConfig
 ├── model:     ModelConfig        # vocab/n_embd/n_layer/n_head/head_dim/block_size
 ├── cache:     CacheConfig        # block_size / num_gpu_blocks / num_cpu_blocks
 └── scheduler: SchedulerConfig    # max_num_seqs / max_num_batched_tokens / ...
```

好处是**可序列化**（`to_json` / `from_json`）——整个配置能写进 `config.json` 随 checkpoint 一起保存和恢复。真实 vLLM 的 `EngineArgs` 同样支持导出。

### 1.3 测试即文档（Tests-as-Docs）

`tests/` 里每一个测试都在守护一个**容易被改坏的不变量**（详见 §6 验证矩阵）。改动核心逻辑时，跑一遍测试就知道"哪个承诺被你打破了"。

### 1.4 库 + 应用双形态（Library + App）

`pyproject.toml` 里的 `[project.scripts] minivllm = "minivllm.cli:main"` 让同一套代码既能被 `import` 调用，也能被命令行驱动：

```python
# 库形态
from minivllm import LLMEngine, EngineConfig, SamplingParams
engine = LLMEngine(EngineConfig())
out = engine.generate("vLLM is", SamplingParams(max_tokens=16))

# 应用形态
python -m minivllm serve --model artifacts/tinygpt --port 8000
```

这正是真实 vLLM 的做法：`vllm` 包既是 Python 库也是 `vllm serve` 命令。

### 1.5 可复现性（Reproducibility）

`seed` 从 `EngineConfig.seed` 一路贯穿到采样器。每个 `Sequence` 的采样可以用 `SamplingParams.seed` 单独派生随机流（覆盖全局 seed），也可以走引擎的全局 RNG。这是所有测试"固定 seed 即可复现"的基础，也与真实 vLLM 的 per-request seed 语义一致。

---

## 2. 模块全景：文件 → 真实 vLLM 映射

| mini-vLLM 文件 | 对应真实 vLLM | 一句话职责 | 依赖 |
|---|---|---|---|
| `config.py` | `vllm/config/` + `vllm/engine/arg_utils.py` | 配置 dataclass 与序列化 | 无 |
| `tokenizer.py` | `vllm/transformers_utils/tokenizer.py` | 字符级 tokenizer | 无 |
| `model.py` | `vllm/model_executor/models/gpt2.py` | TinyGPT 前向（KV 在模型外） | attention, config |
| `attention.py` | `vllm/attention/ops/paged_attn.py` | 分页注意力 | kv_cache |
| `kv_cache.py` | `vllm/core/block_manager_v1.py` + `vllm/worker/model_runner.py` | KVStore / 分配器 / 前缀缓存 / 块表 / COW | config |
| `scheduler.py` | `vllm/core/scheduler.py` | 连续批处理 + 分块 prefill + 抢占 | config, kv_cache, sequence |
| `sampler.py` | `vllm/model_executor/layers/sampler.py` | 温度/top-k/top-p/采样 | config |
| `sequence.py` | `vllm/sequence.py` | Sequence / RequestOutput | config |
| `engine.py` | `vllm/engine/llm_engine.py` | 同步引擎主循环 | 除 api_server 外几乎全部 |
| `async_engine.py` | `vllm/engine/async_llm_engine.py` | 后台线程跑引擎 | engine |
| `api_server.py` | `vllm/entrypoints/openai/api_server.py` | OpenAI 兼容 FastAPI | async_engine, config, sequence |
| `cli.py` | `vllm/entrypoints/cli.py` | serve/chat/demo/spec/quant 子命令 | checkpoint, config |
| `checkpoint.py` | HF model repo（`save_pretrained`/`from_pretrained`） | 保存/加载 权重+配置+tokenizer | model, config, tokenizer |
| `quantize.py` | `vllm/model_executor/layers/quantization/` | int8 weight-only 数据面 | checkpoint, model |
| `speculative.py` | `vllm/spec_decode/` | bigram draft + target verify | kv_cache, model, sequence, tokenizer |
| `training.py` | （vLLM 不训练） | 纯 NumPy 反向传播 | model |
| `data.py` | （vLLM 不训练） | 训练语料 | 无 |

> 依赖方向永远是"上层依赖下层"，没有环。这也是它能被单个文件读懂的工程原因。

### 2.1 模块全景图（依赖关系）

把 §2 表格画成"谁 import 谁"的依赖图。**箭头永远向下，没有环**——这是整套工程能单向读懂的关键：

```
         Layer 0          config.py   ← 无依赖，一切参数的源头
                           │
         Layer 1    ┌──────┼──────┬─────────┬──────────┬───────────┐
                  tokenizer model sequence  sampler   kv_cache  checkpoint
                    │        │      │         │          │
                    │        └─► attention.py（依赖 kv_cache）
                    │              │
                    │              └──────────►（读写分页 KV cache）
                    └─── checkpoint.py（model + config + tokenizer）
                           │
         Layer 2      scheduler.py   ← config + kv_cache + sequence
                           │
         Layer 3        engine.py    ← 除 api_server 外几乎全部
                           │
         Layer 4     async_engine.py ← engine
                           │
         Layer 5    ┌──────┴───────┐
                 api_server.py    cli.py
                 (async_engine,   (checkpoint,
                  config, sequence) config)
```

**读图要点**：

- `config.py` 在最顶端、被所有人依赖，但它自己不依赖任何人——这就是 §1.2"配置收敛"的物理体现；
- `model.py` 与 `kv_cache.py` 通过 `attention.py` 交界：模型只认"block table + KVStore"，不直接摸分配器；
- `engine.py` 是唯一有状态的协调者，`api_server.py` / `cli.py` 只是薄封装；
- `checkpoint.py` 横跨 Layer 1（model + config + tokenizer），`quantize.py` / `speculative.py` 再挂在它与 `model.py` 之下——与真实 vLLM 的 `save_pretrained` / `vllm/spec_decode/` 一一对应。

---

## 3. 完整 API 参考

> 以下签名以仓库当前代码为准（`mini-vllm/minivllm/`）。每个类/函数只列**公共接口**，省略内部实现细节。

### 3.1 配置层 `config.py`

```python
@dataclass
class ModelConfig:
    vocab_size: int = 128
    n_embd: int = 64            # d_model
    n_layer: int = 2
    n_head: int = 4
    head_dim: int = 16          # n_embd // n_head
    block_size: int = 64        # 最大上下文（n_positions）
    dropout: float = 0.0
    tie_word_embeddings: bool = True
    @property
    def hidden_dim(self) -> int: ...      # 4 * n_embd
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> "ModelConfig": ...

@dataclass
class CacheConfig:
    block_size: int = 16        # KV block 大小（--block-size）
    num_gpu_blocks: int = 64    # 物理 block 数
    num_cpu_blocks: int = 8     # 预留用于 swap（mini 版未实现）
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> "CacheConfig": ...

@dataclass
class SchedulerConfig:
    max_num_seqs: int = 8                # --max-num-seqs
    max_num_batched_tokens: int = 512    # 每步 token 预算
    enable_chunked_prefill: bool = True
    enable_preemption: bool = True
    preemption_mode: str = "recompute"   # "recompute" | "swap"（仅 recompute 实现）
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> "SchedulerConfig": ...

@dataclass
class EngineConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    seed: int = 0
    dtype: str = "float32"
    quantize: Optional[str] = None       # None | "int8"
    enable_prefix_caching: bool = True
    speculative: bool = False
    max_model_len: int = 0               # 0 -> 派生自 model.block_size
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> "EngineConfig": ...

@dataclass
class SamplingParams:
    temperature: float = 1.0
    top_k: int = -1             # -1 关闭
    top_p: float = 1.0          # 1.0 关闭
    max_tokens: int = 64
    stop: Optional[List[str]] = None
    stop_token_ids: Optional[List[int]] = None
    ignore_eos: bool = False
    seed: Optional[int] = None  # None -> 全局 RNG
    echo: bool = False          # OpenAI 兼容 "echo"
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> "SamplingParams": ...
```

### 3.2 Tokenizer `tokenizer.py`

```python
class CharTokenizer:
    eos_token_id: int          # = len(chars)（预留一个 EOS id）
    pad_token_id: int          # = 0
    @property
    def vocab_size(self) -> int: ...   # chars + EOS
    def encode(self, text) -> List[int]: ...
    def decode(self, ids) -> str: ...
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data) -> "CharTokenizer": ...
    def save(self, path) -> None: ...
    @classmethod
    def load(cls, path) -> "CharTokenizer": ...
```

### 3.3 模型 `model.py`

```python
class TinyGPT:
    def __init__(self, config: ModelConfig, seed: int = 0,
                 weights: Optional[Dict[str, np.ndarray]] = None): ...
    def state_dict(self) -> Dict[str, np.ndarray]: ...
    def load_state_dict(self, sd: Dict[str, np.ndarray]) -> None: ...
    def forward(self, x, positions, seqs, block_manager) -> np.ndarray:
        """(B,T) token ids + (B,T) 绝对位置 + 序列列表 + BlockManager -> (B,T,vocab) logits。
        前向内部把新 token 的 K/V 写入分页 KV cache，再跑 paged attention。"""
```

### 3.4 注意力 `attention.py`

```python
def gather_kv(k_cache, v_cache, block_table, logical_start, logical_end,
              block_size, num_heads, head_dim):
    """按 block table 把逻辑区间 [logical_start, logical_end) 的 K/V 抓取成连续数组。"""

def paged_attention_batch(q, k, v, cached_lens, block_tables,
                          kv_store, layer, num_heads, head_dim, block_size) -> np.ndarray:
    """对一个 batch 的序列做因果分页注意力，返回 (B, num_heads, num_new, head_dim)。"""
```

### 3.5 KV cache `kv_cache.py`

```python
class NoFreeBlocksError(RuntimeError): ...

class KVStore:
    def __init__(self, cache_config, model_config):
        self.k_cache: List[np.ndarray]   # 每层 (num_blocks, block_size, num_heads, head_dim)
        self.v_cache: List[np.ndarray]
    def clear(self) -> None: ...

class BlockAllocator:
    def __init__(self, num_blocks: int): ...
    @property
    def num_free(self) -> int: ...
    def allocate(self) -> int: ...
    def free_block(self, block_id: int) -> None: ...   # 引用归零才真正释放
    def touch(self, block_id: int) -> None: ...        # 前缀共享时 +1 引用

class PrefixCache:
    def __init__(self, allocator, max_cached_blocks: int): ...
    def get(self, prefix_tokens: tuple) -> Optional[List[int]]: ...
    def put(self, prefix_tokens: tuple, block_ids: List[int]) -> None: ...

class BlockManager:
    def __init__(self, cache_config, model_config, enable_prefix_caching=True): ...
    @property
    def num_free_blocks(self) -> int: ...
    @property
    def num_total_blocks(self) -> int: ...
    def get_block_table(self, seq) -> List[int]: ...
    def match_prefix(self, prompt_ids):                 # -> (prefix_len, block_ids)
    def register_prefix(self, seq) -> None: ...
    def can_allocate(self, seq, num_tokens_needed) -> bool: ...
    def attach_shared_blocks(self, seq, block_ids) -> None: ...
    def ensure_blocks(self, seq, num_tokens_needed) -> None: ...
    def release(self, seq) -> None: ...
    def write_kv(self, layer, seq, logical_pos, k, v) -> None: ...   # 含 COW
```

### 3.6 调度器 `scheduler.py`

```python
@dataclass
class ScheduledStep:
    prefill_items: List[Tuple[Sequence, int]]   # (seq, 要 prefill 的 token 数)
    decode_items:  List[Sequence]               # 每序列 decode 1 个 token
    preempted:     List[Sequence]               # 本步被抢占的序列

class Scheduler:
    def __init__(self, config: SchedulerConfig, block_manager: BlockManager):
        self.waiting: deque[Sequence] = deque()
        self.running: List[Sequence] = []
    def add_sequence(self, seq) -> None: ...
    def has_pending(self) -> bool: ...
    def schedule(self) -> ScheduledStep: ...
```

### 3.7 采样器 `sampler.py`

```python
def apply_temperature(logits, temperature) -> np.ndarray: ...
def apply_top_k(logits, top_k) -> np.ndarray: ...
def apply_top_p(logits, top_p) -> np.ndarray: ...
def sample_token(logits, params: SamplingParams, rng) -> Tuple[np.ndarray, np.ndarray]:
    """-> (token_ids, logprobs)，形状均为 (B,)。顺序：温度 -> top-k -> top-p -> softmax -> 采样。"""
```

### 3.8 数据结构 `sequence.py`

```python
WAITING, RUNNING, FINISHED, PREEMPTED = "WAITING", "RUNNING", "FINISHED", "PREEMPTED"

@dataclass
class Sequence:
    seq_id: int
    prompt_ids: List[int]
    sampling_params: SamplingParams
    arrival_time: float = 0.0
    output_ids: List[int] = field(default_factory=list)
    cached_len: int = 0            # 已缓存 token 数（prompt + 输出）
    phase: str = "PREFILL"         # "PREFILL" | "DECODE"
    state: str = WAITING
    stop_reason: Optional[str] = None
    cumulative_logprob: float = 0.0
    is_preempted: bool = False
    priority: int = 0              # 到达序号（next(itertools.count()) 递增）；
                                   # 注意抢占按 running 队尾弹出，并不按此字段选（见 §4.6）
    @property
    def prompt_len(self) -> int: ...
    @property
    def all_ids(self) -> List[int]: ...     # prompt_ids + output_ids
    @property
    def num_tokens(self) -> int: ...
    @property
    def is_finished(self) -> bool: ...
    @property
    def in_prefill(self) -> bool: ...
    def append_token(self, token_id: int) -> None: ...

@dataclass
class CompletionOutput:
    index: int
    text: str
    token_ids: List[int]
    cumulative_logprob: float = 0.0
    finish_reason: Optional[str] = None
    def delta(self, other_text: str) -> str: ...   # 相对快照的增量文本

@dataclass
class RequestOutput:
    request_id: int
    prompt: str
    prompt_token_ids: List[int]
    outputs: List[CompletionOutput]
    finished: bool
    @classmethod
    def from_sequence(cls, seq, tokenizer) -> "RequestOutput": ...
```

### 3.9 引擎 `engine.py`

```python
class LLMEngine:
    def __init__(self, config: EngineConfig, tokenizer=None, model=None):
        self.block_manager = BlockManager(...)
        self.scheduler = Scheduler(...)
        self.rng = np.random.default_rng(config.seed)
        self.seqs: dict = {}
    def add_request(self, prompt, sampling_params=None, request_id=None) -> int:
        """返回 seq_id；prompt 超 max_model_len 会从尾部截断保留 1 token 输出位。"""
    def has_pending(self) -> bool: ...
    def get_num_unfinished(self) -> int: ...
    def step(self) -> List[RequestOutput]:
        """一个 engine step：schedule() -> _execute()。"""
    def generate(self, prompt, sampling_params=None) -> RequestOutput:
        """跑到完成（离线 LLM.generate 的对应物）。"""
    def generate_stream(self, prompt, sampling_params=None) -> Iterator[RequestOutput]:
        """逐 token 产出（AsyncLLMEngine 的同步版）。"""
```

### 3.10 异步引擎 `async_engine.py`

```python
class AsyncLLMEngine:
    def __init__(self, config, tokenizer=None, model=None): ...
    def stop(self) -> None: ...
    async def stream(self, prompt, sampling_params=None) -> AsyncIterator:
        """后台线程跑引擎 step，每步 yield 一个 RequestOutput。"""
    async def complete(self, prompt, sampling_params=None):
        """收完整条流并返回最终 RequestOutput。"""
```

### 3.11 服务器 `api_server.py`

```python
def create_app(engine: AsyncLLMEngine, tokenizer: CharTokenizer) -> FastAPI:
    """返回 FastAPI 应用，暴露：
    GET  /health
    GET  /v1/models
    POST /v1/completions        （支持 stream=True 走 SSE）
    POST /v1/chat/completions   （支持 stream=True 走 SSE）"""
```

### 3.12 CLI `cli.py`

```bash
python -m minivllm serve  --model artifacts/tinygpt [--host H] [--port P] [--quantize int8]
python -m minivllm chat   --model artifacts/tinygpt [--temperature T] [--max-tokens N]
python -m minivllm demo   --model artifacts/tinygpt [--prompts ...] [--max-tokens N]
python -m minivllm spec   --model artifacts/tinygpt [--num-speculative K] [--prompt S]
python -m minivllm quant  --model artifacts/tinygpt
```

### 3.13 检查点 `checkpoint.py`

```python
def save_checkpoint(model, config: EngineConfig, tokenizer, path) -> None:
    """目录含 model.npz / config.json / tokenizer.json。"""
def load_checkpoint(path):
    """-> (TinyGPT, EngineConfig, CharTokenizer)"""
```

### 3.14 量化 `quantize.py`

```python
def quantize_matrix(w) -> Tuple[np.ndarray, np.ndarray]:   # (q_int8, per-column scale)
def quantize_state_dict(sd): ...
def dequantize_state_dict(qd) -> Dict[str, np.ndarray]: ...
def quantize_model(model) -> Tuple[TinyGPT, Dict[str, float]]:
    """-> (反量化后的新模型, {original_bytes, quantized_bytes, compression_ratio, max_abs_error})"""
```

### 3.15 投机解码 `speculative.py`

```python
class BigramDraftModel:
    def __init__(self, tokenizer, corpus: str): ...
    def draft(self, context: List[int], num_tokens: int) -> List[int]: ...

def speculative_generate(model, tokenizer, block_manager, draft_model,
                         prompt_ids, max_new_tokens, num_speculative_tokens=5) -> dict:
    """-> {output_ids, draft_count, accepted_count, target_forwards, steps}"""
```

### 3.16 训练 `training.py` + `data.py`

```python
class TinyGPTTrainer:
    def __init__(self, model: TinyGPT, lr=0.02, momentum=0.9): ...
    def forward(self, x) -> dict: ...
    def compute_loss(self, logits, targets) -> Tuple[float, np.ndarray]: ...
    def backward(self, x, dlogits, fwd) -> Dict[str, list]: ...
    def apply_grads(self, grads) -> None: ...
    def train_step(self, x, targets) -> float: ...   # -> loss

# data.py
CORPUS: str    # 一段关于 vLLM 的结构化文本
REPEAT: int    # 语料重复次数（默认 40）
```

---

## 4. 关键数据约定（易错点）

这些约定是 mini-vLLM 正确性的"隐性知识"，也是第 06 章之外最值得记住的东西。

### 4.1 decode 约定：`cached_len == num_tokens - 1`

prefill 完成后，最后一个 prompt token 的 KV 已经入缓存，此时 `cached_len == num_tokens`。但 decode 路径的语义是"把 `all_ids[cached_len]` 作为输入，先写它的 KV，再预测下一个"，因此**prefill 完成时立刻做一次 `cached_len -= 1`**。

效果：第一个 decode step 会**幂等重写**最后一个 prompt token 的 KV（无害），但让 prefill / decode 共用同一条 `_sample_and_apply` 路径。**如果你看到 `cached_len` 比预期多 1 或少 1，先检查这里。**

### 4.2 块表与引用计数

- 每个序列有一张 `block_tables[seq_id]`：逻辑 block 下标 → 物理 block id。
- 物理 block 的 `ref_count` 记录"有多少个持有者"：活动序列 + 前缀缓存。
- 只有 `ref_count` 归零，block 才回到 `free` 链表。

### 4.3 Copy-on-Write 的触发时机

`write_kv` 在**写入一个被共享的物理 block**（`ref_count > 1`）时触发 COW：先 `allocate()` 一个新 block，把旧 block 内容复制过去，`free_block(旧)` 减掉自己的引用，然后写新 block。**共享来自前缀缓存命中**——同一物理 block 被多条序列引用时，谁要写谁就复制。

### 4.4 chunk 预算的双重约束

`_chunk_for` 同时受**每步 token 预算**（`max_num_batched_tokens`）和**空闲 block**（`num_free_blocks × block_size`）约束：

```python
max_tokens_by_blocks = (have_blocks + num_free_blocks) * block_size
extra_by_blocks = max(0, max_tokens_by_blocks - seq.cached_len)
return max(0, min(need, remaining, extra_by_blocks))
```

这让"KV cache 放不下整个 prompt"时也能推进——每次只塞能放下的那块。**如果你把 `num_gpu_blocks` 设得很小，分块 prefill 会自动接管，不会卡死。**

### 4.5 前缀缓存的对齐问题

`register_prefix` 只缓存**属于 prompt 的 block**（`ceil(prompt_len / block_size)` 个）。`match_prefix` 按 token 序列从最长到最短逐级查 LRU。注意：

- 前缀命中要求**token 序列逐块对齐**——只有块内内容完全相同才会命中（真实 vLLM 的 hash 也是按 block 粒度）。
- `attach_shared_blocks` 只是把缓存的 block 表挂到新序列上并 `touch` 增引用，**不拷贝数据**。

### 4.6 抢占的"新准入保护"

`Scheduler._newly_admitted` 记录本次 `schedule()` 里刚准入的 seq。`_preempt_one` **跳过这些 seq**，否则会出现"准入一个又立刻踢掉一个"的抖动。这是调度器稳定性的细节，真实 vLLM 同样有类似保护。

---

## 5. 工程化检查清单

用这份清单评判任何"类 serving 引擎"代码：

- [ ] **模块分层**：配置 / 数据 / 核心 / 模型 / 引擎 / 服务是否各司其职？依赖是否无环？
- [ ] **参数收敛**：是否有魔法数字散落？是否有一个配置对象统一承载并支持序列化？
- [ ] **测试焊死不变量**：核心不变量（等价性、收敛性）是否被测试守护？
- [ ] **双形态**：同一套代码是否既可 import 又可命令行驱动？
- [ ] **可复现**：seed 是否从顶到底贯穿？固定 seed 输出是否确定？
- [ ] **错误处理**：KV 耗尽是否抛明确异常（`NoFreeBlocksError`）而非静默错乱？
- [ ] **资源回收**：序列结束/被抢占时 block 是否一定释放（`release`）？
- [ ] **可观测**：是否有 demo / 日志 / 指标能看清每一步在干什么？

---

## 6. 验证矩阵与复现命令

以下全部在纯 CPU 环境实测通过（Python 3.14.6 + NumPy 2.5.1，Windows，无需 GPU）：

| 验证项 | 命令 | 结果 |
|---|---|---|
| PagedAttention 与稠密注意力数值一致 | `python tests/test_paged_attention.py` | `test_paged_attention: OK` |
| 引擎不变量（确定性 / 批处理等价 / 分块 prefill / 前缀缓存 / 抢占 / 流式） | `python tests/test_engine.py` | `test_engine: OK` |
| OpenAI 服务器 4 端点 + SSE 流式 | `python tests/test_server.py` | `test_server: OK` |
| 连续批处理 demo | `python -m minivllm demo --model artifacts/tinygpt` | `[cli] done in 30 engine steps` |
| 投机解码 demo | `python -m minivllm spec --model artifacts/tinygpt` | `draft tokens: 68, accepted: 16 (23.5%), target forwards: 28, steps: 14` |
| int8 量化 demo | `python -m minivllm quant --model artifacts/tinygpt` | `original: 1467264 B, quantized: 374784 B (0.26x), max err 0.00391` |
| OpenAI 兼容服务（手测） | `python -m minivllm serve --model artifacts/tinygpt --port 8099` | `/health`、`/v1/models`、`/v1/completions`、SSE `data: [DONE]` 全部正常 |

> 以上验证矩阵已在 **Python 3.14 + NumPy 2.5** 环境重新验证通过（Windows，无需 GPU）。纯 NumPy 实现受版本影响很小，但若本地 NumPy 偏旧（< 2.0），建议先升级再复现；升级后若个别断言失败，优先查 §8 的"数值对拍不一致"一行的建议。

**复现流程**：

```bash
cd vllm-toturial/mini-vllm
pip install -e .[test]                     # 安装运行时依赖 + 测试依赖（httpx/pytest）
python scripts/train.py --steps 400 --embd 96 --layers 3 --out artifacts/tinygpt   # 重新训练（约 6 分钟）
python tests/test_paged_attention.py
python tests/test_engine.py
python tests/test_server.py
python -m minivllm demo --model artifacts/tinygpt
```

> 注意：训练随机性使**重新训练的模型**与仓库里 `artifacts/tinygpt` 的生成文本不同，但**引擎机制测试**（不变量）与**接受率/误差量级**是稳定的。

---

## 7. 扩展练习（由浅入深）

> 每个练习给出：**背景**（对应真实 vLLM 的哪个模块）→ **实现思路** → **关键代码位置** → **验收标准**。建议按顺序做，完成练习 3 之后你就已经给 mini-vLLM 加了两个 vLLM 的真实特性了。

### 练习 0：热身——写你的第一个测试

- 背景：`tests/` 是回归测试也是文档。
- 任务：仿照 `test_streaming_yields_tokens`，写一个测试验证"`temperature=0` 时输出确定"。
- 验收：`python tests/test_my_first.py` 打印 `test_my_first: OK`。

### 练习 1：给采样器加 `min_p` 过滤

- 背景：真实 vLLM 的 `Sampler` 支持 `min_p`（相对概率阈值，见第 05 章）。
- 实现思路：`min_p` 过滤掉概率 `< min_p × max_prob` 的 token，在 top-p 之后、softmax 之前作用。改 `sampler.py`：
  ```python
  def apply_min_p(logits, min_p):
      probs = _softmax(logits)
      max_prob = probs.max(axis=-1, keepdims=True)
      return np.where(probs < min_p * max_prob, -np.inf, logits)
  ```
- 关键位置：`sampler.py::sample_token`（在 `apply_top_p` 之后插入）。
- 验收：`SamplingParams(min_p=0.05)` 能跑通；`min_p=1.0` 退化为贪心（只剩 max 一个 token）。

### 练习 2：实现 SWAP 抢占

- 背景：真实 vLLM 有 `recompute` 和 `swap` 两种抢占（第 07 章）。mini 版只实现了 recompute。`CacheConfig.num_cpu_blocks` 已经预留了 CPU block 数。
- 实现思路：
  1. 在 `BlockManager` 增加一个 CPU 侧的 `KVStore`（`num_blocks=num_cpu_blocks`）和它的 `BlockAllocator`。
  2. `Scheduler._preempt_one` 里，当 `config.preemption_mode == "swap"` 时：把被抢占序列的每个物理 block 的 KV **拷到 CPU block**，记录映射，再释放 GPU block；序列状态置为"已换出"。
  3. 重新调度该序列时：把 CPU block 拷回 GPU，`cached_len` 保持不变（**不用重算 prefill**）。
- 关键位置：`scheduler.py::_preempt_one`、`kv_cache.py::BlockManager`。
- 验收：
  - 新增 `tests/test_swap_recovers.py`：同样用超订的 KV cache（复刻 `test_preemption_recovers` 的配置）断言所有请求完成。
  - 对比 recompute 与 swap：swap 模式下 `cached_len` 不归零（不重算），因此**更快的恢复**但消耗 PCIe 拷贝带宽。

### 练习 3：实现 `n`（一个 prompt 生成多条序列）

- 背景：真实 vLLM 的 `n` 参数（第 05 章）。mini 版的 `RequestOutput.outputs` 已经是列表，但引擎只填 1 条。
- 实现思路：
  1. `SamplingParams` 增加 `n: int = 1`。
  2. `engine.add_request` 对同一个 prompt 创建 `n` 个 `Sequence`（共享 `seq_id` 前缀，各自独立 `output_ids` / `cached_len` / block table）。
  3. 调度器需要能同时容纳同 prompt 的多条序列——它们共享前缀 KV block（正好复用 `match_prefix` / `attach_shared_blocks`，但要注意 COW：一旦各自 decode，写共享 block 会触发复制）。
- 关键位置：`engine.py::add_request`、`sequence.py`、`api_server.py::_sampling_params_from_body`。
- 验收：`SamplingParams(n=3)` 返回 3 条不同的输出（固定 seed 下可复现且互不相同）。

### 练习 4：实现 embedding 模式与 `/v1/embeddings`

- 背景：真实 vLLM 支持 `--task embed`（第 11 章），forward 最后一层从 logits 换成 pooled hidden state。
- 实现思路：
  1. `EngineConfig` 增加 `task: str = "generate"`。
  2. `model.py::TinyGPT` 增加 `embed()`：跑前向但返回最后一层 hidden state（`h`），而不是 `h @ wte.T`。可用 mean pooling 或取最后一个 token。
  3. `api_server.py` 增加 `POST /v1/embeddings`：输入文本 → 输出向量。
- 关键位置：`model.py::forward`（return 前分支）、`api_server.py::create_app`。
- 验收：`"vLLM"` 与 `"vLLM "` 的向量余弦相似度接近 1，与 `"quantization"` 明显不同。

### 练习 5：暴露前缀缓存命中率指标

- 背景：真实 vLLM 的 `/metrics` 暴露 `vllm:gpu_cache_usage_perc` 等指标（第 10 章）。
- 实现思路：
  1. `BlockManager` 记录 `match_prefix` 的调用次数与"命中 > 0"的次数。
  2. 新增 `GET /metrics` 端点，返回 Prometheus 文本格式：`minivllm_prefix_cache_hit_rate`。
  3. 在 `demo` 子命令里打印命中率（两个相同前缀的请求一起跑）。
- 关键位置：`kv_cache.py::BlockManager`、`api_server.py::create_app`。
- 验收：连续提交两个相同 prompt 的请求后，命中率 > 0；`curl /metrics` 能看到该指标。

### 练习 6：给引擎加并发压测

- 背景：真实 vLLM 用 `benchmark_serving.py` 测吞吐/延迟（第 08 章）。
- 实现思路：写一个脚本 `scripts/bench.py`：
  1. 用 `AsyncLLMEngine` + 多个 `asyncio` 任务并发提交请求；
  2. 统计总耗时、`tokens/s`、平均 TTFT / TPOT；
  3. 对比不同 `max_num_seqs` 下的吞吐。
- 验收：`max_num_seqs` 从 1 提到 8，`tokens/s` 单调上升（说明连续批处理在起作用）；画出曲线。

### 练习 7：给采样器加 `repetition_penalty` 支持

- 背景：真实 vLLM 的 `SamplingParams` 有 `repetition_penalty`（第 05 章）。它属于 **logits processor**：在 temperature 之前，把"序列里已经出现过的 token"的 logit 按规则压低（`score > 0` 时除以惩罚值，`score < 0` 时乘以惩罚值），从而抑制重复生成。mini 版目前没有惩罚项，§8 里"输出重复循环"的陷阱恰好提示了这一点。
- 实现思路：
  1. `SamplingParams` 增加 `repetition_penalty: float = 1.0`（`1.0` 表示关闭）。
  2. `sampler.py` 新增，调用顺序放在 temperature 之前（对应 vLLM 的 logits processor 阶段）：
     ```python
     def apply_repetition_penalty(logits, penalty, past_token_ids):
         """penalty>1 时压低序列中已出现 token 的 logit（vLLM 语义）。"""
         if penalty == 1.0:
             return logits
         out = logits.copy()
         for t in past_token_ids:
             s = out[..., t]
             out[..., t] = np.where(s > 0, s / penalty, s * penalty)
         return out
     ```
  3. 惩罚需要"这个序列到目前为止生成过哪些 token"，所以 `sample_token` 的签名要新增 `past_token_ids`（batch 下按行传入），并在 temperature 之前调用；调用方 `engine._sample_and_apply` 传入 `[seq.all_ids]`（prompt + 已生成的输出）。
- 关键位置：`config.py::SamplingParams`（新字段）、`sampler.py::sample_token`（签名与调用顺序）、`engine.py::_sample_and_apply`（传 `past_token_ids`）。
- 验收：
  - `repetition_penalty=1.0` 时行为完全不变（回归 `test_engine`）；
  - 固定 seed 下，`repetition_penalty=1.5` 的长生成（`max_tokens=200`）中**同一 token 连续重复的频次**明显低于 `=1.0` 的基线；
  - `temperature=0`（贪心）+ 高惩罚：某个已出现 token 的 logit 被压低后，贪心不再反复选它——最直观的判定；
  - 新增 `tests/test_repetition_penalty.py`，断言"加惩罚序列的重复度 ≤ 不加惩罚序列的重复度"。

### 练习 8：实现多步调度（multi-step scheduling）

- 背景：真实 vLLM 的 **multi-step scheduling**（`--num-scheduler-steps`，V1 默认）让调度器一次决定未来 N 步，期间**不回 CPU 重新 schedule**，把"CPU 调度 + kernel 发射"的开销摊薄到 N 个 token 上（第 02 章 §4.1）。mini 版现在是"每步 `schedule()` 一次、`execute()` 一次"，天然是"一步调度"。
- 实现思路：
  1. 给 `SchedulerConfig` 加 `num_scheduler_steps: int = 1`（`1` 表示不启用）。
  2. 在 `LLMEngine` 里把 `step()` 循环改造为**两段式**：
     ```python
     # 每 N 步才真正调用一次 schedule()
     if self._steps_until_reschedule == 0:
         self._scheduled = self.scheduler.schedule()
         self._steps_until_reschedule = self.config.scheduler.num_scheduler_steps
     self._steps_until_reschedule -= 1
     return self._execute(self._scheduled)
     ```
  3. `_execute` 要处理"多步复用同一批次"带来的三个细节：
     - **decode 输入要跟随步进**：多步下 `seq.cached_len` 每步 +1，`_sample_and_apply` 的输入 token 要从 `seq.all_ids[seq.cached_len]` 取（而不是固定在某个快照）；
     - **新请求被冻结**：`schedule()` 只在第 1 步跑，所以中间 N−1 步新到的请求**进不了批次**（真实 vLLM 的行为）；可以在 `_execute` 里不处理 `waiting`，靠测试验证"冻结期内新请求被推迟到下一个调度点"；
     - **提前终止的 slot 浪费**：某序列在第 2 步就 `finish` 了，第 3–N 步的批次槽位**空转**（不产出错误 token，只是少算）——这是多步调度与"调度不影响正确性"唯一的例外形态，测试要显式接受"允许浪费、不允许错"。
  4. 可选：把 `max_num_batched_tokens` 在多步内**不递减**（每步都按整预算算），模拟 V1 的动态预算。
- 关键位置：`config.py::SchedulerConfig`（新字段）、`engine.py::step`（两段式改造）、`scheduler.py::schedule`（幂等：同一步调度结果可被安全地复用 N 次）。
- 验收：
  - `num_scheduler_steps=1` 时行为与现状完全一致（回归全部 `test_engine`）；
  - `num_scheduler_steps=4`、固定 seed 下，单请求生成结果与 `=1` **完全相同**（多步不改 token 序列）；
  - 并发多请求下，`num_scheduler_steps=4` 的完成顺序可能与 `=1` 不同（新请求被冻结更久），但**所有请求最终都完成**；
  - 新增 `tests/test_multi_step.py`：断言"N 步调度 == 1 步调度"的 token 级等价（这是多步调度的核心正确性承诺）。

> 做完练习 2、3、8，你就给 mini-vLLM 补上了"SWAP 抢占、`n` 多序列、多步调度"三个真实 vLLM 的核心调度特性。第 9 节还给了"从 mini 到真实 vLLM"的四步演进地图。

---

## 8. 常见陷阱与调试

| 现象 | 可能原因 | 排查方法 |
|---|---|---|
| 输出是空字符串 | 采样参数 `max_tokens` 为 0 / prompt 编码为空 | 检查 `tokenizer.encode` 返回值；检查 `_apply_token` 是否立刻命中 stop |
| 输出重复循环 | 温度太低 + 无惩罚项 | 提高温度或加 `frequency_penalty`（真实 vLLM 同理，见第 05 章） |
| 固定 seed 仍不唯一 | 用了全局 `rng` 且多个请求共用 | 每个 `Sequence` 用 `SamplingParams.seed` 派生独立随机流 |
| 分块 prefill 结果与全量不同 | `_chunk_for` 边界算错 / 因果 mask 越界 | 跑 `test_chunked_prefill_matches_full`；逐层打印 `cached_len` 变化 |
| 前缀缓存不命中 | 前缀长度未达 block 对齐；`register_prefix` 只在 `_finish` 且非抢占时触发 | 打印 `match_prefix` 返回值；检查 `enable_prefix_caching` |
| COW 写坏了共享 block | `write_kv` 没检查 `ref_count` | 单测：两个序列共享前缀，各自 decode 后 block 内容独立 |
| 抢占后请求丢失 | `_preempt_one` 把 `cached_len` 清 0 但 `output_ids` 没保留 | 确认 `all_ids = prompt_ids + output_ids` 在重 prefill 时被完整使用 |
| 数值对拍不一致 | dtype 混用（float32/float64）/ softmax 未减最大值 | 统一 float32；`_softmax` 先 `x - max` |
| `NoFreeBlocksError` 被吞 | 调度器没处理 `can_allocate` 失败分支 | 确认 Phase A 里 `_preempt_one` 后仍失败就 `continue`（让出这步） |
| 服务端流式丢 chunk | `_delta` 基于累计文本求差，前置文本不匹配 | 确认 `_delta` 的 `startswith` 分支；检查 SSE 是否 `[DONE]` 结尾 |
| 前缀缓存命中率很高却仍 `NoFreeBlocksError` | `attach_shared_blocks` / `touch` 加了引用，但 `release` 没对共享 block 减引用，`ref_count` 泄漏不归零 | 打印每个物理 block 的 `ref_count`；确认 `BlockManager.release` 遍历 `block_tables[seq_id]` 逐个 `free_block`；写"同一前缀连续 N 次请求后 `num_free_blocks` 回到初始值"的断言 |
| 投机解码接受率异常（远低于/高于文档的 23.5%） | ① 用了重新训练的模型（接受率随模型变化，但量级应接近）；② 若把 verify 从贪心改成"按采样概率接受"，`float32`/`float64` 混用或 `top_p` 重归一化不一致会让接受判定失真 | ① 与 `artifacts/tinygpt` 基线对比，确认是模型差异而非代码错误；② 统一 `float64`、保证 draft 与 target 用同一套 `SamplingParams`（尤其 `top_p` 归一化），打印"被接受 token 的目标 logprob vs 阈值"定位 |

---

## 9. 与真实 vLLM 的差距与演进地图

mini-vLLM 有意省略了真实 vLLM 的以下部分——读懂这张表，你就知道真实 vLLM 的复杂度从哪来：

| 维度 | mini-vLLM | 真实 vLLM |
|---|---|---|
| 计算后端 | NumPy einsum | CUDA / Triton kernel（PagedAttention、FlashAttention） |
| KV cache | 固定 `num_gpu_blocks` | `gpu_memory_utilization` 自动推导 + KV cache 量化（fp8） |
| 调度 | recompute 抢占 | recompute + swap + 优先级 + 分块 prefill 的 V0/V1 两套调度器 |
| 采样 | 温度/top-k/top-p | + min-p、惩罚项、beam search、guided decoding（FSM）、logprobs 全量 |
| 服务 | 4 个端点 | + `/v1/embeddings`、`/metrics`、多模型、结构化输出、工具调用 |
| 并行 | 无 | TP/PP/EP/DP、Ray、NCCL、PD 分离、context parallel |
| 模型支持 | 1 个 TinyGPT | 上百个架构注册表 + 多模态 + LoRA |
| 训练 | 内置训练脚本 | 不训练，加载 HF 预训练权重 |

**演进地图**（从 mini 出发往真实走）：

1. **换内核**：把 `attention.py` / `model.py` 的 NumPy 换成 PyTorch，`paged_attention_batch` 换成 FlashAttention API → 这就是一个最小可用的 PyTorch serving 引擎。
2. **加自动显存规划**：把 `CacheConfig.num_gpu_blocks` 从"配"改成"算"（按 `gpu_memory_utilization` 反推）→ 见第 17 章附录 B。
3. **加并行**：让 `LLMEngine` 支持多个 worker（对应 TP）→ 见第 14 章。
4. **加部署**：把 `create_app` 套进 Docker + 反向代理 + 指标采集 → 见第 10 章。

---

## 10. 小结

- mini-vLLM 的工程原则：**职责单一、配置收敛、测试即文档、库+应用双形态、可复现**。
- 完整 API 参考在 §3，关键数据约定（decode 约定 / COW / chunk 预算 / 前缀对齐）在 §4，易错点在 §8。
- 验证矩阵（§6）证明它**可运行、可复现**；扩展练习（§7）把"读懂"变成"会改"。
- 从 mini 到真实 vLLM 的演进路径：换内核 → 加显存规划 → 加并行 → 加部署。

这是教程参考手册的第三份附录（全套共五份，另有 16 参数 / 17 显存 / 21 压测 / 22 术语）。读到这里，你应该既理解了 vLLM 的原理与工程实现，也亲手跑通并扩展了一个最小可用的 serving 引擎。

教程的正文在 13 章结束、五份附录（16/17/18/21/22）补齐了"查参数、算显存、会改代码、做基准与调优、查术语"。若还想继续向纵深走：《19-模型架构基础.md》讲透模型的架构（GQA/MLA/RoPE/MoE），《20-端到端部署案例.md》带你照做一次完整上线，《21-附录D-性能基准测试与调优指南.md》给出压测方法论与调优循环（其中 §9 与本章练习 6 互补），《22-附录E-术语表.md》是随手可查的术语词典。回目录见《README.md》。
