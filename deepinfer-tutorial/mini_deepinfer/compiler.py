"""A tiny tensor-graph compiler.

Production inference stacks do not execute Python ops one by one.  They lower
a model into a graph, fold constants, remove dead code, fuse elementwise
chains, plan memory, choose kernels, and only then hand the result to a
runtime such as CUDA Graph.  This module implements the graph-level part at a
scale that can be read in one sitting.

The numerical semantics are intentionally small: add, multiply, matmul,
RMSNorm, SiLU, softmax, reshape and transpose.  The optimization passes are
the real subject: constant folding, dead-code elimination, two fusion
patterns, and a liveness-based memory plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

Array = np.ndarray


@dataclass(frozen=True)
class Op:
    """One graph node.

    ``attrs`` is a tuple of pairs rather than a dict so that an ``Op`` stays
    hashable and easy to print.  Values are small configuration objects, never
    large tensors except for ``const`` nodes.
    """

    name: str
    inputs: tuple[str, ...]
    output: str
    attrs: tuple[tuple[str, Any], ...] = ()

    def attr(self, key: str, default: Any = None) -> Any:
        for name, value in self.attrs:
            if name == key:
                return value
        return default

    def __str__(self) -> str:
        return f"{self.output} = {self.name}({', '.join(self.inputs)})"


def _silu(x: Array) -> Array:
    return x * (1.0 / (1.0 + np.exp(-x)))


def _rms_norm(x: Array, weight: Array, eps: float) -> Array:
    scale = np.sqrt(np.mean(x * x, axis=-1, keepdims=True) + eps)
    return (x / scale) * weight


def _softmax(x: Array, axis: int) -> Array:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


def _eval(op: Op, env: dict[str, Array]) -> Array:
    name = op.name
    if name == "const":
        return np.asarray(op.attr("value"))
    if name == "add":
        return env[op.inputs[0]] + env[op.inputs[1]]
    if name == "mul":
        return env[op.inputs[0]] * env[op.inputs[1]]
    if name == "matmul":
        return env[op.inputs[0]] @ env[op.inputs[1]]
    if name == "silu":
        return _silu(env[op.inputs[0]])
    if name == "rmsnorm":
        return _rms_norm(env[op.inputs[0]], env[op.inputs[1]], float(op.attr("eps", 1e-5)))
    if name == "softmax":
        return _softmax(env[op.inputs[0]], int(op.attr("axis", -1)))
    if name == "reshape":
        return np.reshape(env[op.inputs[0]], tuple(op.attr("shape")))
    if name == "transpose":
        return np.transpose(env[op.inputs[0]], tuple(op.attr("axes")))
    if name == "fused_rmsnorm_matmul":
        x = _rms_norm(
            env[op.inputs[0]], env[op.inputs[1]], float(op.attr("eps", 1e-5))
        )
        return x @ env[op.inputs[2]]
    if name == "swiglu":
        return _silu(env[op.inputs[0]]) * env[op.inputs[1]]
    raise ValueError(f"unknown op {name!r}")


class Graph:
    """A topologically ordered list of ops with named inputs and outputs."""

    def __init__(
        self,
        ops: Iterable[Op],
        inputs: Sequence[str],
        outputs: Sequence[str],
    ) -> None:
        self.ops = list(ops)
        self.inputs = tuple(inputs)
        self.outputs = tuple(outputs)
        self._validate()

    def _validate(self) -> None:
        available = set(self.inputs)
        if len(available) != len(self.inputs):
            raise ValueError("duplicate graph input")
        for op in self.ops:
            for name in op.inputs:
                if name not in available:
                    raise ValueError(
                        f"op {op.output!r} reads {name!r} before it is defined"
                    )
            if op.output in available:
                raise ValueError(f"tensor {op.output!r} is defined more than once")
            available.add(op.output)
        for name in self.outputs:
            if name not in available:
                raise ValueError(f"graph output {name!r} is never produced")

    def clone(self) -> "Graph":
        return Graph(self.ops, self.inputs, self.outputs)

    def with_ops(self, ops: Iterable[Op]) -> "Graph":
        return Graph(ops, self.inputs, self.outputs)

    def _execute_env(self, feed: dict[str, Array]) -> dict[str, Array]:
        missing = [name for name in self.inputs if name not in feed]
        if missing:
            raise ValueError(f"missing feeds: {missing}")
        env: dict[str, Array] = {name: np.asarray(feed[name]) for name in self.inputs}
        for op in self.ops:
            env[op.output] = np.asarray(_eval(op, env))
        return env

    def run(self, feed: dict[str, Array]) -> dict[str, Array]:
        env = self._execute_env(feed)
        return {name: env[name] for name in self.outputs}

    def __len__(self) -> int:
        return len(self.ops)

    def __str__(self) -> str:
        body = "\n".join(f"  {op}" for op in self.ops)
        return f"Graph(inputs={self.inputs}, outputs={self.outputs})\n{body}"


# ---------------------------------------------------------------------------
# Optimization passes
# ---------------------------------------------------------------------------


def constant_fold(graph: Graph) -> Graph:
    """Evaluate ops whose inputs are all compile-time constants."""
    known: dict[str, Array] = {}
    ops: list[Op] = []
    for op in graph.ops:
        if all(name in known for name in op.inputs):
            value = np.asarray(_eval(op, known))
            known[op.output] = value
            ops.append(Op("const", (), op.output, (("value", value),)))
        else:
            ops.append(op)
    return graph.with_ops(ops)


def dead_code_elimination(graph: Graph) -> Graph:
    """Keep only ops that contribute to a graph output."""
    needed = set(graph.outputs)
    kept: list[Op] = []
    for op in reversed(graph.ops):
        if op.output in needed:
            kept.append(op)
            needed.update(op.inputs)
    kept.reverse()
    return graph.with_ops(kept)


def _consumer_counts(ops: Iterable[Op]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for op in ops:
        for name in op.inputs:
            counts[name] = counts.get(name, 0) + 1
    return counts


def fuse_rmsnorm_matmul(graph: Graph) -> Graph:
    """Fuse ``rmsnorm`` into a following ``matmul``.

    This is the graph-level analogue of a fused norm+GEMM kernel: the norm
    output never has to be written to HBM.  Fusion only applies when the norm
    result feeds exactly one matmul; otherwise the intermediate is shared and
    must remain materialized.
    """
    producers = {op.output: op for op in graph.ops}
    counts = _consumer_counts(graph.ops)
    replacements: dict[str, Op] = {}
    skipped: set[str] = set()
    for op in graph.ops:
        if op.name != "matmul":
            continue
        left, right = op.inputs
        producer = producers.get(left)
        if (
            producer is not None
            and producer.name == "rmsnorm"
            and counts.get(left, 0) == 1
            and producer.output not in graph.outputs
        ):
            replacements[op.output] = Op(
                "fused_rmsnorm_matmul",
                (producer.inputs[0], producer.inputs[1], right),
                op.output,
                (("eps", producer.attr("eps", 1e-5)),),
            )
            skipped.add(producer.output)
    return graph.with_ops(
        replacements.get(op.output, op)
        for op in graph.ops
        if op.output not in skipped
    )


def fuse_swiglu(graph: Graph) -> Graph:
    """Fuse ``silu(x) * gate`` into one ``swiglu`` op."""
    producers = {op.output: op for op in graph.ops}
    counts = _consumer_counts(graph.ops)
    replacements: dict[str, Op] = {}
    skipped: set[str] = set()
    for op in graph.ops:
        if op.name != "mul":
            continue
        for activated_index in (0, 1):
            activated = op.inputs[activated_index]
            gate = op.inputs[1 - activated_index]
            producer = producers.get(activated)
            if (
                producer is not None
                and producer.name == "silu"
                and counts.get(activated, 0) == 1
                and producer.output not in graph.outputs
            ):
                replacements[op.output] = Op(
                    "swiglu", (producer.inputs[0], gate), op.output
                )
                skipped.add(producer.output)
                break
    return graph.with_ops(
        replacements.get(op.output, op)
        for op in graph.ops
        if op.output not in skipped
    )


# ---------------------------------------------------------------------------
# Memory planning and cost estimate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryPlan:
    slots: dict[str, int]
    slot_bytes: dict[int, int]
    peak_bytes: int
    naive_bytes: int

    @property
    def saving(self) -> float:
        if self.naive_bytes == 0:
            return 0.0
        return 1.0 - self.peak_bytes / self.naive_bytes


def plan_memory(graph: Graph, feed: dict[str, Array]) -> MemoryPlan:
    """Assign buffers by live range so non-overlapping tensors share slots.

    The plan is a greedy best-fit: tensors are visited in definition order,
    expired slots are released, and the smallest free slot that fits is
    reused.  It is not optimal, but it has the two properties that matter:
    it never makes two live tensors share a slot, and it never allocates more
    than the sum of all tensor sizes.
    """
    env = graph._execute_env(feed)
    sizes = {name: int(np.asarray(value).nbytes) for name, value in env.items()}

    definition: dict[str, int] = {name: 0 for name in graph.inputs}
    for index, op in enumerate(graph.ops):
        definition[op.output] = index + 1
    last_use: dict[str, int] = {name: definition[name] for name in definition}
    for index, op in enumerate(graph.ops):
        for name in op.inputs:
            last_use[name] = max(last_use.get(name, 0), index + 1)
    for name in graph.outputs:
        last_use[name] = len(graph.ops) + 1

    order = sorted(sizes, key=lambda name: (definition.get(name, 0), name))
    slots: dict[str, int] = {}
    allocated: dict[int, int] = {}
    active: dict[int, int] = {}
    for name in order:
        start = definition.get(name, 0)
        end = last_use.get(name, start)
        for slot, slot_end in list(active.items()):
            if slot_end < start:
                del active[slot]
        need = max(1, sizes[name])
        candidates = [
            slot
            for slot, size in allocated.items()
            if slot not in active and size >= need
        ]
        if candidates:
            slot = min(candidates, key=lambda item: allocated[item])
        else:
            slot = len(allocated)
            allocated[slot] = need
        slots[name] = slot
        active[slot] = end

    return MemoryPlan(
        slots=slots,
        slot_bytes=dict(allocated),
        peak_bytes=sum(allocated.values()),
        naive_bytes=sum(sizes.values()),
    )


@dataclass(frozen=True)
class GraphStats:
    ops: int
    launches: int
    flops: int
    bytes: int
    peak_bytes: int
    naive_bytes: int


def _op_flops(op: Op, out: Array, inputs: Sequence[Array]) -> int:
    if op.name == "matmul" or op.name == "fused_rmsnorm_matmul":
        if op.name == "matmul":
            m, k = inputs[0].shape
            _, n = inputs[1].shape
        else:
            m, k = inputs[0].shape
            _, n = inputs[2].shape
        return int(2 * m * k * n)
    if op.name == "rmsnorm":
        return int(4 * out.size)
    if op.name == "softmax":
        return int(5 * out.size)
    if op.name in ("add", "mul", "silu", "swiglu"):
        return int(out.size)
    return 0


def estimate(graph: Graph, feed: dict[str, Array]) -> GraphStats:
    env = graph._execute_env(feed)
    flops = 0
    moved = 0
    for op in graph.ops:
        out = env[op.output]
        inputs = [env[name] for name in op.inputs]
        flops += _op_flops(op, out, inputs)
        moved += sum(int(array.nbytes) for array in inputs) + int(out.nbytes)
    plan = plan_memory(graph, feed)
    return GraphStats(
        ops=len(graph.ops),
        launches=len(graph.ops),
        flops=flops,
        bytes=moved,
        peak_bytes=plan.peak_bytes,
        naive_bytes=plan.naive_bytes,
    )


@dataclass(frozen=True)
class OptimizedGraph:
    graph: Graph
    before: GraphStats
    after: GraphStats

    @property
    def op_reduction(self) -> float:
        if self.before.ops == 0:
            return 0.0
        return 1.0 - self.after.ops / self.before.ops


def optimize(graph: Graph, feed: dict[str, Array]) -> OptimizedGraph:
    """Run the standard pass pipeline and report what changed."""
    before = estimate(graph, feed)
    current = constant_fold(graph)
    current = dead_code_elimination(current)
    current = fuse_rmsnorm_matmul(current)
    current = fuse_swiglu(current)
    current = dead_code_elimination(current)
    after = estimate(current, feed)
    return OptimizedGraph(graph=current, before=before, after=after)


def build_mlp_graph(
    batch: int,
    dim: int,
    hidden: int,
    *,
    seed: int = 0,
) -> tuple[Graph, dict[str, Array]]:
    """Build a small transformer-like block for demos and tests.

    The graph contains both fusion opportunities: ``rmsnorm -> matmul`` and
    ``silu(x) * gate``.  Weights are graph inputs, not constants, so constant
    folding has nothing to do unless the caller folds them explicitly.
    """
    rng = np.random.default_rng(seed)
    w_norm_q = rng.normal(0.0, 0.1, size=(dim,)).astype(np.float32)
    w_norm_ffn = rng.normal(0.0, 0.1, size=(dim,)).astype(np.float32)
    w_q = rng.normal(0.0, 0.05, size=(dim, dim)).astype(np.float32)
    w_gate = rng.normal(0.0, 0.05, size=(dim, hidden)).astype(np.float32)
    w_up = rng.normal(0.0, 0.05, size=(dim, hidden)).astype(np.float32)
    w_down = rng.normal(0.0, 0.05, size=(hidden, dim)).astype(np.float32)
    x = rng.normal(size=(batch, dim)).astype(np.float32)

    ops = [
        # The query path has a private norm, so rmsnorm can fuse into matmul.
        Op("rmsnorm", ("x", "w_norm_q"), "h_q", (("eps", 1e-5),)),
        Op("matmul", ("h_q", "w_q"), "q"),
        # The FFN path shares one norm between gate and up, so that norm
        # cannot be fused; the fused elementwise pass still applies to SwiGLU.
        Op("rmsnorm", ("x", "w_norm_ffn"), "h_ffn", (("eps", 1e-5),)),
        Op("matmul", ("h_ffn", "w_gate"), "gate"),
        Op("matmul", ("h_ffn", "w_up"), "up"),
        Op("silu", ("gate",), "gate_act"),
        Op("mul", ("gate_act", "up"), "ffn_hidden"),
        Op("matmul", ("ffn_hidden", "w_down"), "ffn_out"),
        Op("add", ("q", "ffn_out"), "y"),
        Op("softmax", ("y",), "probs"),
        Op("transpose", ("probs",), "probs_t", (("axes", (1, 0)),)),
    ]
    feed = {
        "x": x,
        "w_norm_q": w_norm_q,
        "w_norm_ffn": w_norm_ffn,
        "w_q": w_q,
        "w_gate": w_gate,
        "w_up": w_up,
        "w_down": w_down,
    }
    return Graph(ops, inputs=tuple(feed), outputs=("probs", "probs_t")), feed
