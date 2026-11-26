"""Package version.

The tutorial pins no external MLflow version at runtime.  The MiniMLflow
implementation follows the *ideas* of MLflow 2.x/3.x while keeping a small,
auditable surface.
"""

__version__ = "1.0.0"
MLFLOW_COMPAT_HINT = "3.x concepts, intentionally simplified REST/API surface"
