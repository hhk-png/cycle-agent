"""``miniray.util.scheduling_strategies`` —— 转出调度策略类。

保持这个路径是为了让从 Ray 迁移过来的代码**不用改 import**:

.. code-block:: python

    # 在 Ray 里
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
    # 在 mini-ray 里,同一行只需要改包名
    from miniray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
"""

from __future__ import annotations

from ..scheduling_strategies import (  # noqa: F401
    DEFAULT,
    SPREAD,
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)

__all__ = [
    "NodeAffinitySchedulingStrategy",
    "PlacementGroupSchedulingStrategy",
    "DEFAULT",
    "SPREAD",
]
