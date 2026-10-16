"""``python -m minich <subcommand>`` 的入口。

这个文件小得几乎可以忽略,但它承担的职责**并不小**,值得说清楚:

.. code-block:: text

   python -m minich local -q "SELECT 1"
          │
          │  ① Python 发现 minich/ 是个包,于是执行 minich/__main__.py
          ▼
   __main__.py: sys.exit(main(sys.argv[1:]))
          │
          ▼
   minich/cli.py: main(["local", "-q", "SELECT 1"])
          │
          ▼
   解析 → 建 Client → 执行 → 打印 → 返回退出码 → 进程退出

**为什么用 ``sys.exit(main())`` 而不是把逻辑写在这里?**
因为 :func:`minich.cli.main` 必须**既能在命令行用、也能被当作库调用**
(测试里直接 ``main(["local", ...])``)。把"读 argv / 设退出码"这种
进程相关的仪式感留在 ``__main__`` 里,库那边就保持成纯粹的
"参数进去、退出码出来"。这也是 Unix 程序的一条老规矩:
``main`` 返回码,由最外层决定怎么把它变成进程状态。

.. note::

   真实的 ``clickhouse`` 命令是一个多路复用器:``clickhouse client`` /
   ``clickhouse server`` / ``clickhouse local`` … 都指向同一个二进制,
   靠 ``argv[1]`` 分派。本文件就是那个"分派"的最小版本。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    # 把退出码交给 shell —— main() 自己不会调 sys.exit,这里补上那一步。
    sys.exit(main(sys.argv[1:]))
