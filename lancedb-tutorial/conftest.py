"""pytest 的路径引导。

测试用 ``import mini_lancedb...`` 这种绝对导入，所以仓库根目录必须在
``sys.path`` 上。``python -m pytest`` 会自动把 cwd 加进去，但直接跑
``pytest`` 不会 —— 这里显式兜住，免得测试"换一种跑法就红"。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
