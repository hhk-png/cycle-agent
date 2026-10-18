"""MiniKubeRay - a readable, runnable model of the KubeRay control plane.

The package intentionally mirrors Kubernetes concepts instead of hiding them:
objects are JSON-compatible dictionaries, the API server speaks a small subset
of the Kubernetes REST API, and controllers reconcile desired state through a
work queue.  It is a teaching implementation, not a production operator.
"""

from .version import __version__

__all__ = ["__version__"]
