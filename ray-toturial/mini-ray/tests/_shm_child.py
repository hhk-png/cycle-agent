"""子进程:attach 到父进程的共享内存段,验证「零拷贝」是真的。

用法::

    python _shm_child.py <shm_name> <offset> <shape_json> <dtype> [--write <value>]

打印 ``RESULT:{...}`` 供父进程断言。如果这里是拷贝而不是共享内存,
父进程那边的数组不会被这次写入影响 —— 测试就会失败。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniray.object_store import create_ndarray_view  # noqa: E402


def main() -> int:
    shm_name, offset, shape_json, dtype = sys.argv[1:5]
    write_value = None
    if "--write" in sys.argv:
        write_value = float(sys.argv[sys.argv.index("--write") + 1])

    descriptor = {
        "kind": "ndarray",
        "shm": shm_name,
        "offset": int(offset),
        "shape": json.loads(shape_json),
        "dtype": dtype,
        "readonly": True,
    }
    view = create_ndarray_view(descriptor)
    before = float(view.sum())

    # 只读视图必须拒绝写入(Ray 的行为:对象存储里的对象不可变)
    readonly_blocked = False
    try:
        view[0] = 1.0
    except ValueError:
        readonly_blocked = True

    if write_value is not None:
        view.flags.writeable = True  # 显式解锁,证明这块内存确实是共享的
        view[0] = write_value

    print("RESULT:" + json.dumps({"before": before, "readonly_blocked": readonly_blocked}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
