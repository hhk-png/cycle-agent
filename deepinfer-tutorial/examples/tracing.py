"""Record a synthetic request trace and print a span summary."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_deepinfer.tracing import Tracer  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def main() -> None:
    clock = Clock()
    tracer = Tracer(clock=clock)

    trace = tracer.start("req-demo", tenant="search", model="mini/tiny-lm")
    with tracer.span(trace, "queue.wait", tokens=2048):
        clock.advance(0.004)
    with tracer.span(trace, "prefill", tokens=2048):
        clock.advance(0.018)
    for step in range(8):
        with tracer.span(trace, "decode.step", step=step):
            clock.advance(0.0015)
            with tracer.span(trace, "decode.forward"):
                clock.advance(0.0011)
            with tracer.span(trace, "decode.sample"):
                clock.advance(0.0002)
    with tracer.span(trace, "detokenize"):
        clock.advance(0.0004)

    print("trace id:", trace.trace_id)
    print("spans   :", len(trace.spans))
    print("total ms: %.3f" % (trace.duration_s * 1000))
    print("by name :")
    for name, spans in sorted(trace.by_name().items()):
        total = sum(span.duration_s for span in spans) * 1000
        print(f"  {name:16s} count={len(spans):2d} total_ms={total:.3f}")

    print("summary :")
    summary = tracer.summary()
    print("  traces      :", summary["traces"])
    print("  trace p95 ms: %.3f" % summary["trace_p95_ms"])
    for name, values in sorted(summary["spans"].items()):
        print(f"  {name:16s} mean_ms={values['mean_ms']:.3f} p95_ms={values['p95_ms']:.3f}")

    out = ROOT / "examples" / "_trace_demo.jsonl"
    tracer.export_jsonl(out)
    print("exported:", out.relative_to(ROOT))
    out.unlink()


if __name__ == "__main__":
    main()
