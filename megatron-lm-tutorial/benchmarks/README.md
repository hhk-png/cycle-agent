# Benchmarks

The tutorial's `benchmark` command is intentionally small and synchronous:

```bash
python -m mini_megatron.cli benchmark --config configs/tiny.json --steps 5
```

It reports tokens/s and grad norm for the CPU implementation. Do not compare
these numbers with production Megatron: mini has no fused kernels, no CUDA
Graphs, and no communication overlap.

For production measurements, record:

- GPU model/count, NVLink/IB topology;
- Megatron Core/PyTorch/CUDA/TE versions;
- TP/PP/DP/CP/EP, micro/global batch, seq length;
- precision and whether FP8/FP4 is used;
- MFU definition and whether communication/checkpointing is included;
- p50/p99 step time, not only mean.

Use Nsight Systems for the timeline and Nsight Compute for kernel-level
roofline. A performance claim without these fields is not reproducible.
