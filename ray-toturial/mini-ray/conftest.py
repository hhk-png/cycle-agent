"""把项目根目录放进 sys.path,这样 ``import miniray`` 在任何工作目录下都能工作。

(pytest 会把 conftest.py 所在目录插入 sys.path —— 这个空文件就是为此存在的。
子进程是 spawn 出来的,会继承父进程的 sys.path,所以 worker 里也能 import。)
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
