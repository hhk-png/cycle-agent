"""A real Ray job, suitable for `ray job submit` / RayJob.entrypoint.

This file is intentionally independent of MiniKubeRay.  It demonstrates the
kind of entrypoint a RayJob carries in production:

    ray job submit --address http://127.0.0.1:8265 -- python examples/ray_job.py
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import ray
    except ImportError:
        print("ray is not installed; install it with: pip install 'ray[default]'")
        return 1

    ray.init(address="auto")
    try:

        @ray.remote
        def square(value: int) -> int:
            return value * value

        values = [ray.get(square.remote(i)) for i in range(8)]
        print("workers:", len(ray.nodes()))
        print("squares:", values)
        print("runtime:", sys.version.split()[0])
        return 0
    finally:
        ray.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
