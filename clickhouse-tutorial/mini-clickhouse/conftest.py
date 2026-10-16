"""让 ``import minich`` 在任何工作目录下都能工作。

pytest 会把 conftest.py 所在的目录插入 ``sys.path`` —— 这个文件的唯一
作用就是保证这件事。为什么要显式写出来而不是依赖"在项目根目录运行"?
因为教程里的验证命令可能从上一级目录执行::

    cd clickhouse-tutorial && python mini-clickhouse/tools/check_docs.py
    cd clickhouse-tutorial/mini-clickhouse && python -m pytest tests/ -q

两种调用方式都必须能 import 到 ``minich``。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
