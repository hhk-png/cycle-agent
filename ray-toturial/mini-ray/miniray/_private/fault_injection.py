"""故障注入 —— 用来**演示与测试**容错机制。

分布式系统里「容错」最麻烦的地方是:你很难在生产环境里复现故障。
所以工程上通行的做法是**把故障变成可编程的**:Chaos Monkey、failpoint、
``SIGKILL`` 注入……mini-ray 提供一个最小版本。

.. code-block:: python

    from miniray._private import fault_injection

    @miniray.remote
    def big_computation():
        return sum(i * i for i in range(10_000))

    ref = big_computation.remote()
    miniray.get(ref)                                   # 正常拿到结果

    fault_injection.lose_objects()                     # 模拟「对象丢了」
    print(miniray.get(ref))                            # 触发 lineage 重建后仍然正确

真实 Ray 里对应的是「节点挂了」或者 plasma 里的对象被清理掉了。Ray 的做法是
**lineage reconstruction**:对象没了没关系,重新执行产出它的任务就是了
(前提是任务本身是确定性的 —— 这也是 Ray 文档反复强调的一点)。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import runtime

__all__ = ["lose_objects"]


def lose_objects(node_id: Optional[str] = None) -> Dict[str, Any]:
    """丢掉指定节点上的对象,模拟节点故障 / 对象丢失。

    :param node_id: 只丢这个节点的;``None`` 表示丢所有节点的。
    :return: ``{"lost": [对象 ID…], "reconstructed": 个数}``

    丢完之后,任何 **由 task 产出** 的对象都会被 lineage 重建 ——
    也就是说 ``miniray.get(ref)`` 依然能拿到正确的值(重新算了一遍)。
    """
    worker = runtime.get_core_worker()
    return worker._raylet.lose_objects(node_id=node_id)
