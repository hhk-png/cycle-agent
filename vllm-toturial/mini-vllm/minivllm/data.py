"""Shared training corpus used by ``scripts/train.py`` and the speculative
decoding demo.  A real vLLM never trains; it loads pre-trained weights from
the HuggingFace hub.  We train a *tiny* model on a *tiny* corpus only so the
tutorial is fully self-contained and can be run without downloading anything.
"""

# A short, structured text about vLLM itself.  Training on it lets the demo
# model emit recognisably vLLM-flavoured text.
CORPUS = """
vLLM is a fast and easy to use library for LLM inference and serving.
vLLM uses PagedAttention to manage the memory of the KV cache.
The KV cache stores the keys and values of every token.
PagedAttention stores the KV cache in non contiguous blocks.
The block manager keeps a logical to physical block table.
The scheduler batches many requests together for high throughput.
This technique is called continuous batching.
vLLM provides an OpenAI compatible API server.
You can start the server with the vllm serve command.
The engine runs prefill and decode phases for every request.
The prefill phase processes the prompt and stores the KV cache.
The decode phase generates one token at a time.
Chunked prefill splits a long prompt into several chunks.
When the memory is full the scheduler preempts a sequence.
The preempted sequence will be recomputed later.
Prefix caching reuses the KV cache of common prompt prefixes.
The sampler supports temperature and top p sampling.
The block size is a tunable parameter of the KV cache.
vLLM supports quantization such as GPTQ and AWQ.
The future of vLLM includes disaggregated prefill and speculation.
"""

# How many copies of the corpus to concatenate for training.  A tiny model
# learns fast on repeated data.
REPEAT = 40
