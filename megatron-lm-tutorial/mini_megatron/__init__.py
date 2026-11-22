"""mini-megatron: a compact, runnable reconstruction of Megatron-LM's core.

The package intentionally avoids NVIDIA-only dependencies so the concepts are
executable on a laptop CPU.  It includes a simplified expert-parallel
all-to-all path, an executable interleaved GPipe scheduler, TP/PP
model/optimizer-state resharding, a standalone MLA reference, a flat
distributed optimizer, ring attention and Ulysses context-parallel references,
aux-free MoE balancing, and a paged KV cache.
Production features (Transformer Engine,
FP8/FP4 kernels, grouped GEMM, ZeRO/FSDP2 resharding, CUDA graphs) are
documented in the tutorial and mapped to the simplified code where possible.
"""

__version__ = "0.4.4"
