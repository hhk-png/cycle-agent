"""``miniray.util.placement_group`` —— Ray 兼容路径(见 ``ray.util.placement_group``)。"""

from __future__ import annotations

from ..placement_group import (  # noqa: F401
    PlacementGroup,
    get_current_placement_group,
    get_placement_group,
    placement_group,
    placement_group_table,
    remove_placement_group,
)

__all__ = [
    "PlacementGroup",
    "placement_group",
    "remove_placement_group",
    "get_placement_group",
    "get_current_placement_group",
    "placement_group_table",
]
