"""MiniK8s — a small, runnable Kubernetes-compatible control plane.

The package intentionally depends only on the Python standard library and
PyYAML.  Every component mirrors a real Kubernetes component closely enough
that the architectural lessons transfer, while the container runtime, network
and storage backends are simulated for determinism and safety.
"""

__version__ = "0.9.0"
