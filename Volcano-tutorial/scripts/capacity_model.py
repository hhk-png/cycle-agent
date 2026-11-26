#!/usr/bin/env python3
"""Capacity, queueing and ROI calculator for the Volcano tutorial.

Standard library only. All compute quantities are GPU-equivalents unless a
field says otherwise. The model is deliberately simple and transparent: it is
an engineering estimate, not a benchmark, a price quote or a promise.

Examples:
    python scripts/capacity_model.py --selftest

    python scripts/capacity_model.py \
      --gpus 512 --price 2 --hours 8760 \
      --baseline 0.55 --improved 0.65 --annual-cost 500000 \
      --queue train:3:120:300 --queue infer:1:60:120 \
      --train-params 70 --train-tokens 1e12 --world-size 512 \
      --peak-tflops 989 --mfu 0.4 --tp 8 --pp 2 --cp 1 \
      --kv-layers 80 --kv-heads 8 --head-dim 128 --seq-len 4096 \
      --concurrency 32 --qps 20 --out-tokens 500 \
      --tokens-per-sec-replica 2000 --target-util 0.7 --redundancy 1

Units:
    * GPU time is in hours, money is in the same currency as --price.
    * Queue guarantees/capabilities are in GPU-equivalents.
    * KV cache reports decimal GB (1e9 bytes) and GiB (2^30 bytes).
"""

from __future__ import annotations

import argparse
import math
import sys


# ---------------------------------------------------------------------------
# Core formulas (kept as small pure functions so --selftest can pin them).
# ---------------------------------------------------------------------------

def annual_gpu_cost(gpus: float, price_per_gpu_hour: float, hours: float) -> float:
    return gpus * price_per_gpu_hour * hours


def effective_gpu_hours(gpus: float, hours: float, utilization: float) -> float:
    return gpus * hours * utilization


def cost_per_effective_gpu_hour(total_cost: float, effective_hours: float) -> float:
    if effective_hours <= 0:
        raise ValueError("effective GPU-hours must be positive")
    return total_cost / effective_hours


def savings_fixed_demand(total_cost: float, base_util: float, improved_util: float) -> float:
    """Money saved if the same work is done with fewer GPUs."""
    if improved_util <= 0 or base_util <= 0:
        raise ValueError("utilization must be positive")
    return total_cost * (1.0 - base_util / improved_util)


def extra_work_fixed_cluster(base_util: float, improved_util: float) -> float:
    """Fractional increase in useful work on the same cluster."""
    if base_util <= 0:
        raise ValueError("baseline utilization must be positive")
    return improved_util / base_util - 1.0


def breakeven_improved_util(total_cost: float, annual_program_cost: float, base_util: float) -> float:
    """Utilization needed so that savings_fixed_demand == annual_program_cost."""
    if annual_program_cost <= 0:
        return base_util
    if annual_program_cost >= total_cost:
        return math.inf
    return base_util / (1.0 - annual_program_cost / total_cost)


def queue_plan(gpus: float, reserve: float, queues: list[tuple[str, float, float, float]]) -> dict:
    """Return capacity, deserved shares and guarantee/capability checks.

    Each queue tuple is (name, weight, guarantee, capability), all in
    GPU-equivalents. deserved = capacity * weight / sum(weight).
    """
    capacity = gpus * (1.0 - reserve)
    total_weight = sum(q[1] for q in queues)
    rows = []
    if total_weight <= 0:
        raise ValueError("sum of queue weights must be positive")
    for name, weight, guarantee, capability in queues:
        deserved = capacity * weight / total_weight
        rows.append(
            {
                "name": name,
                "weight": weight,
                "guarantee": guarantee,
                "capability": capability,
                "deserved": deserved,
                "guarantee_ok": guarantee <= deserved + 1e-9,
                "deserved_ok": deserved <= capability + 1e-9,
            }
        )
    guarantee_sum = sum(q[2] for q in queues)
    capability_sum = sum(q[3] for q in queues)
    return {
        "capacity": capacity,
        "total_weight": total_weight,
        "rows": rows,
        "guarantee_sum": guarantee_sum,
        "guarantee_fits": guarantee_sum <= capacity + 1e-9,
        "capability_sum": capability_sum,
        "headroom": capacity - guarantee_sum,
    }


def training_flops(params: float, tokens: float) -> float:
    """6 * params * tokens is the usual dense-training estimate."""
    return 6.0 * params * tokens


def training_seconds(
    params: float,
    tokens: float,
    world_size: float,
    peak_tflops: float,
    mfu: float,
) -> float:
    if world_size <= 0 or peak_tflops <= 0 or mfu <= 0:
        raise ValueError("world size, peak TFLOPS and MFU must be positive")
    flops = training_flops(params, tokens)
    effective_flops_per_second = world_size * peak_tflops * 1e12 * mfu
    return flops / effective_flops_per_second


def kv_cache_bytes(
    layers: float,
    kv_heads: float,
    head_dim: float,
    seq_len: float,
    concurrency: float,
    bytes_per_element: float,
) -> float:
    """Key and value, all layers, all concurrent sequences."""
    per_sequence = 2.0 * layers * kv_heads * head_dim * seq_len * bytes_per_element
    return per_sequence * concurrency


def inference_replicas(
    qps: float,
    out_tokens: float,
    tokens_per_sec_per_replica: float,
    target_util: float,
    redundancy: int,
) -> tuple[int, int]:
    if tokens_per_sec_per_replica <= 0 or target_util <= 0 or target_util > 1:
        raise ValueError("tokens/s per replica must be positive and 0 < target-util <= 1")
    base = qps * out_tokens / (tokens_per_sec_per_replica * target_util)
    base_replicas = max(0, math.ceil(base - 1e-9))
    total = base_replicas + max(0, redundancy)
    return base_replicas, total


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def money(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:,.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:,.3f}M"
    if value >= 1e3:
        return f"${value / 1e3:,.1f}k"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


def num(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(args: argparse.Namespace) -> str:
    if not (0.0 < args.baseline <= 1.0):
        raise ValueError("--baseline must be in (0, 1]")
    if not (0.0 < args.improved <= 1.0):
        raise ValueError("--improved must be in (0, 1]")
    if args.improved < args.baseline:
        raise ValueError("--improved must be >= --baseline; otherwise the model reports negative benefit")

    total_cost = annual_gpu_cost(args.gpus, args.price, args.hours)
    base_hours = effective_gpu_hours(args.gpus, args.hours, args.baseline)
    improved_hours = effective_gpu_hours(args.gpus, args.hours, args.improved)
    base_unit = cost_per_effective_gpu_hour(total_cost, base_hours)
    improved_unit = cost_per_effective_gpu_hour(total_cost, improved_hours)
    savings = savings_fixed_demand(total_cost, args.baseline, args.improved)
    extra = extra_work_fixed_cluster(args.baseline, args.improved)
    breakeven = breakeven_improved_util(total_cost, args.annual_cost, args.baseline)

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("Volcano capacity / queueing / ROI model")
    lines.append("=" * 72)
    lines.append("")
    lines.append("[1] GPU economics")
    lines.append(f"  GPUs                       : {num(args.gpus, 0)}")
    lines.append(f"  Price per GPU-hour         : ${args.price:,.2f}")
    lines.append(f"  Hours per year             : {num(args.hours, 0)}")
    lines.append(f"  Annual GPU cost            : {money(total_cost)}")
    lines.append(f"  Baseline utilization       : {pct(args.baseline)}")
    lines.append(f"  Improved utilization       : {pct(args.improved)}")
    lines.append(f"  Effective GPU-hours (base) : {num(base_hours, 0)}")
    lines.append(f"  Effective GPU-hours (new)  : {num(improved_hours, 0)}")
    lines.append(f"  Cost / effective GPU-hour  : {money(base_unit)} -> {money(improved_unit)}")
    lines.append(f"  Unit-cost reduction        : {pct(1.0 - improved_unit / base_unit)}")
    lines.append("")
    lines.append("  Two ways to read the improvement (do not add them):")
    lines.append(f"    - Fixed demand, shrink fleet: save {money(savings)}/year")
    lines.append(f"    - Fixed fleet, grow demand  : +{pct(extra)} useful work")
    if args.annual_cost > 0:
        if math.isinf(breakeven):
            lines.append("  Program cost >= annual GPU cost: no utilization can break even.")
        else:
            pp = (breakeven - args.baseline) * 100.0
            lines.append(
                f"  Break-even for {money(args.annual_cost)}/year program cost: "
                f"{pct(breakeven)} (+{pp:.2f} pp)"
            )
    lines.append("")

    if args.queue:
        plan = queue_plan(args.gpus, args.reserve, args.queue)
        lines.append("[2] Queue plan (GPU-equivalents)")
        lines.append(f"  Usable capacity (reserve {pct(args.reserve)}): {num(plan['capacity'])}")
        lines.append(
            f"  {'queue':<16}{'weight':>8}{'guarantee':>12}{'capability':>12}{'deserved':>12}  checks"
        )
        for row in plan["rows"]:
            checks = []
            if not row["guarantee_ok"]:
                checks.append("guarantee>deserved")
            if not row["deserved_ok"]:
                checks.append("deserved>capability")
            lines.append(
                f"  {row['name']:<16}{num(row['weight'], 0):>8}"
                f"{num(row['guarantee']):>12}{num(row['capability']):>12}"
                f"{num(row['deserved']):>12}  {','.join(checks) or 'ok'}"
            )
        lines.append(
            f"  Sum guarantee = {num(plan['guarantee_sum'])}; "
            f"headroom = {num(plan['headroom'])}; "
            f"{'fits' if plan['guarantee_fits'] else 'OVER CAPACITY'}"
        )
        lines.append(
            f"  Sum capability = {num(plan['capability_sum'])} "
            f"(can exceed capacity; capability is an upper bound, not a reservation)"
        )
        if not plan["guarantee_fits"]:
            lines.append("  WARNING: guarantees cannot all be honored at the same time.")
        lines.append("")

    if args.train_params > 0 or args.train_tokens > 0:
        seconds = training_seconds(
            args.train_params,
            args.train_tokens,
            args.world_size,
            args.peak_tflops,
            args.mfu,
        )
        lines.append("[3] Training estimate")
        lines.append(f"  FLOPs                      : {training_flops(args.train_params, args.train_tokens):.3e}")
        lines.append(f"  World size (GPUs)          : {num(args.world_size, 0)}")
        lines.append(f"  Peak per GPU (TFLOPS)      : {num(args.peak_tflops)}")
        lines.append(f"  MFU                        : {pct(args.mfu)}")
        lines.append(f"  Estimated wall-clock       : {seconds / 3600.0:,.1f} h "
                     f"({seconds / 86400.0:,.2f} days)")
        parallel = args.tp * args.pp * args.cp
        if parallel > 0 and args.world_size > 0:
            ok = abs(args.world_size / parallel - round(args.world_size / parallel)) < 1e-9
            lines.append(
                f"  TP x PP x CP               : {args.tp:g} x {args.pp:g} x {args.cp:g} = {parallel:g}; "
                f"world size divisible: {'yes' if ok else 'NO'}"
            )
            if not ok:
                lines.append("  WARNING: rank layout will fail even if the scheduler starts the Pods.")
        lines.append("")

    if args.kv_layers > 0:
        per_seq = kv_cache_bytes(
            args.kv_layers, args.kv_heads, args.head_dim, args.seq_len, 1, args.bytes_per_element
        )
        total_kv = kv_cache_bytes(
            args.kv_layers,
            args.kv_heads,
            args.head_dim,
            args.seq_len,
            args.concurrency,
            args.bytes_per_element,
        )
        lines.append("[4] KV cache estimate")
        lines.append(f"  Per sequence ({num(args.seq_len, 0)} tokens): "
                     f"{per_seq / 1e9:.3f} GB ({per_seq / 2**30:.3f} GiB)")
        lines.append(f"  {num(args.concurrency, 0)} concurrent sequences: "
                     f"{total_kv / 1e9:.2f} GB ({total_kv / 2**30:.2f} GiB)")
        lines.append("  KV cache is per serving replica; compare it with that replica's")
        lines.append("  GPU memory after weights and activations, not with the whole cluster.")
        lines.append("")

    if args.qps > 0:
        base_replicas, total_replicas = inference_replicas(
            args.qps,
            args.out_tokens,
            args.tokens_per_sec_replica,
            args.target_util,
            args.redundancy,
        )
        gpus_per_replica = args.gpus_per_replica
        lines.append("[5] Inference replica estimate")
        lines.append(f"  Offered load               : {num(args.qps)} req/s x {num(args.out_tokens, 0)} output tokens")
        lines.append(f"  Per-replica throughput     : {num(args.tokens_per_sec_replica, 0)} tokens/s")
        lines.append(f"  Target utilization         : {pct(args.target_util)}")
        lines.append(f"  Base replicas              : {base_replicas}")
        lines.append(f"  With {args.redundancy} spare replica(s)      : {total_replicas}")
        lines.append(f"  GPUs ({num(gpus_per_replica, 0)} per replica)       : "
                     f"{num(total_replicas * gpus_per_replica, 0)}")
        if args.price > 0:
            lines.append(f"  Annual cost of those GPUs  : "
                         f"{money(annual_gpu_cost(total_replicas * gpus_per_replica, args.price, args.hours))}")
        lines.append("")

    lines.append("Notes")
    lines.append("  - Fixed-demand savings and fixed-fleet extra work are different")
    lines.append("    accounting views; never add them together.")
    lines.append("  - MFU, tokens/s and KV sizes are workload-specific. Measure them;")
    lines.append("    do not treat this output as a benchmark.")
    lines.append("  - Queue deserved uses a one-shot weighted split. Hierarchical")
    lines.append("    capacity, borrowing and reclaim can change the steady state.")
    lines.append("  - A guarantee sum above capacity means some guarantees cannot be")
    lines.append("    honored simultaneously; the scheduler does not create GPUs.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _close(got: float, want: float, rel: float = 1e-6) -> bool:
    return math.isclose(got, want, rel_tol=rel, abs_tol=1e-6)


def selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, condition))

    total = annual_gpu_cost(512, 2.0, 8760)
    check("annual cost 512 x $2 x 8760", _close(total, 8_970_240.0))

    base_hours = effective_gpu_hours(512, 8760, 0.55)
    check("effective hours at 55%", _close(base_hours, 2_466_816.0))

    base_unit = cost_per_effective_gpu_hour(total, base_hours)
    check("cost per effective hour", _close(base_unit, 3.636_363_636, rel=1e-6))

    new_hours = effective_gpu_hours(512, 8760, 0.65)
    check("effective hours at 65%", _close(new_hours, 2_915_328.0))

    new_unit = cost_per_effective_gpu_hour(total, new_hours)
    check("new cost per effective hour", _close(new_unit, 3.076_923_077, rel=1e-5))

    saved = savings_fixed_demand(total, 0.55, 0.65)
    check("fixed-demand savings", _close(saved, 1_380_036.923, rel=1e-5))

    extra = extra_work_fixed_cluster(0.55, 0.65)
    check("fixed-fleet extra work", _close(extra, 0.181_818_181, rel=1e-6))

    breakeven = breakeven_improved_util(total, 500_000, 0.55)
    check("break-even utilization", _close(breakeven, 0.582_466, rel=1e-4))

    plan = queue_plan(512, 0.1, [("train", 3, 120, 300), ("infer", 1, 60, 120)])
    check("usable capacity", _close(plan["capacity"], 460.8))
    check("train deserved", _close(plan["rows"][0]["deserved"], 345.6))
    check("guarantee sum fits", plan["guarantee_fits"] is True)

    flops = training_flops(70e9, 1e12)
    check("training FLOPs", _close(flops, 4.2e23, rel=1e-6))

    seconds = training_seconds(70e9, 1e12, 512, 989, 0.4)
    check("training days", _close(seconds / 86400.0, 24.005, rel=1e-3))

    per_seq = kv_cache_bytes(80, 8, 128, 4096, 1, 2)
    check("KV per sequence GB", _close(per_seq / 1e9, 1.342_177_28, rel=1e-6))

    kv32 = kv_cache_bytes(80, 8, 128, 4096, 32, 2)
    check("KV 32 sequences GB", _close(kv32 / 1e9, 42.949_672_96, rel=1e-6))

    base_replicas, total_replicas = inference_replicas(20, 500, 2000, 0.7, 1)
    check("inference base replicas", base_replicas == 8)
    check("inference total replicas", total_replicas == 9)

    failed = [name for name, ok in checks if not ok]
    if failed:
        print("capacity model selftest: FAILED", file=sys.stderr)
        for name in failed:
            print(f"  - {name}", file=sys.stderr)
        return 1
    print(f"capacity model selftest: {len(checks)} checks passed")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_queue(spec: str) -> tuple[str, float, float, float]:
    parts = spec.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"queue must be name:weight:guarantee:capability, got {spec!r}"
        )
    name = parts[0].strip()
    if not name:
        raise argparse.ArgumentTypeError("queue name must not be empty")
    try:
        weight, guarantee, capability = (float(x) for x in parts[1:])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"queue numbers are invalid: {spec!r}") from exc
    if weight < 0 or guarantee < 0 or capability < 0:
        raise argparse.ArgumentTypeError("queue values must be non-negative")
    return name, weight, guarantee, capability


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Volcano capacity, queueing and ROI calculator (standard library only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--selftest", action="store_true", help="run the built-in numerical checks")
    p.add_argument("--gpus", type=float, default=512, help="total GPUs in the cluster")
    p.add_argument("--price", type=float, default=2.0, help="price per GPU-hour")
    p.add_argument("--hours", type=float, default=8760, help="hours per year")
    p.add_argument("--baseline", type=float, default=0.55, help="baseline utilization (0-1]")
    p.add_argument("--improved", type=float, default=0.65, help="improved utilization (0-1]")
    p.add_argument("--annual-cost", type=float, default=0.0,
                   help="annual program cost to break even (0 disables)")
    p.add_argument("--reserve", type=float, default=0.1,
                   help="capacity reserved for system/failure/fragmentation (0-1)")
    p.add_argument("--queue", type=parse_queue, action="append", default=[],
                   metavar="NAME:WEIGHT:GUARANTEE:CAPABILITY",
                   help="queue in GPU-equivalents; repeatable")
    p.add_argument("--train-params", type=float, default=0.0, help="model parameters")
    p.add_argument("--train-tokens", type=float, default=0.0, help="training tokens")
    p.add_argument("--world-size", type=float, default=0.0, help="training world size in GPUs")
    p.add_argument("--peak-tflops", type=float, default=989.0, help="per-GPU peak TFLOPS")
    p.add_argument("--mfu", type=float, default=0.4, help="model FLOPs utilization (0-1]")
    p.add_argument("--tp", type=float, default=8.0, help="tensor parallel size")
    p.add_argument("--pp", type=float, default=2.0, help="pipeline parallel size")
    p.add_argument("--cp", type=float, default=1.0, help="context parallel size")
    p.add_argument("--kv-layers", type=float, default=0.0, help="model layers (0 disables)")
    p.add_argument("--kv-heads", type=float, default=8.0, help="KV heads per layer")
    p.add_argument("--head-dim", type=float, default=128.0, help="attention head dimension")
    p.add_argument("--seq-len", type=float, default=4096.0, help="sequence length")
    p.add_argument("--concurrency", type=float, default=32.0, help="concurrent sequences")
    p.add_argument("--bytes-per-element", type=float, default=2.0,
                   help="KV element size in bytes (bf16=2)")
    p.add_argument("--qps", type=float, default=0.0, help="inference requests per second (0 disables)")
    p.add_argument("--out-tokens", type=float, default=500.0, help="average output tokens per request")
    p.add_argument("--tokens-per-sec-replica", type=float, default=2000.0,
                   help="measured output tokens/s per replica")
    p.add_argument("--target-util", type=float, default=0.7,
                   help="target utilization of inference replicas (0-1]")
    p.add_argument("--redundancy", type=int, default=1, help="spare replicas (N+1 by default)")
    p.add_argument("--gpus-per-replica", type=float, default=8.0,
                   help="GPUs per inference replica")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        print(build_report(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
