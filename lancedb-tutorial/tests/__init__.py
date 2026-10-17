"""测试包。

做成**包**（而不是靠 pytest 的 rootdir 推断）是为了让
``from .conftest import ...`` 这种显式导入能工作 —— 共享的
``brute_force_order`` / ``make_rows`` 是测试的**参照实现**，
隐式依赖 ``sys.path`` 顺序会让它们在不同调用方式下（``pytest tests/``、
``pytest tests/test_x.py``、IDE 里点单个用例）表现不一致。
"""
