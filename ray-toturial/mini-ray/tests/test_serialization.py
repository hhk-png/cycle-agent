"""序列化层的单元测试。

重点验证三件事:

1. 普通对象的 round-trip;
2. **按值序列化** —— ``__main__`` 里定义的函数/类能跨进程重建
   (这是 Ray 用 cloudpickle 解决的核心问题,用子进程真实验证);
3. ObjectRef 的内联机制(任务参数里的 ref 会被替换成占位符)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from miniray import serialization

_HERE = os.path.dirname(os.path.abspath(__file__))


def test_basic_roundtrip():
    for value in [
        None,
        True,
        123,
        3.5,
        "text",
        b"bytes",
        [1, 2, {"k": (3, 4)}],
        {"nested": [None, "x", 2.0]},
        set(),  # 集合空
    ]:
        assert serialization.loads(serialization.dumps(value)) == value


def test_lambda_and_closure_roundtrip_in_process():
    """闭包按值序列化后,在**当前**进程里也能正确重建。"""
    factor = 7
    fn = lambda x: x * factor  # noqa: E731
    blob = serialization.dumps(fn)
    restored = serialization.loads(blob)
    assert restored(6) == 42


def test_recursive_function_roundtrip():
    def fact(n):
        return 1 if n <= 1 else n * fact(n - 1)

    restored = serialization.loads(serialization.dumps(fact))
    assert restored(5) == 120


def test_main_defined_objects_cross_process():
    """端到端:__main__ 里定义的函数/类真的能跑到另一个解释器里执行。

    这个测试会 spawn 一个新进程,里面既没有 __main__ 里的那些定义,
    也没有它们的模块 —— 只能靠按值序列化重建。
    """
    script = os.path.join(_HERE, "_ser_roundtrip.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # Windows 控制台默认 GBK,中文会炸
    proc = subprocess.run(
        [sys.executable, "-u", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=os.path.dirname(_HERE),
        env=env,
    )
    assert proc.returncode == 0, f"子进程失败:\n{proc.stdout}\n{proc.stderr}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, f"没有拿到结果行:\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(line[-1][len("RESULT:") :])

    assert result["plain"] == 42
    assert result["global"] == 15, "全局变量没有跟着函数一起传过去"
    assert result["fib"] == 55, "递归函数重建后无法解析自己"
    assert result["adder"] == 101, "闭包变量丢失"
    assert result["nested0"] == 3
    assert result["nested1"] == "ABC"
    assert result["cls"] == [11, 16], "类重建失败"
    assert result["derived"] == 30, "继承关系或 super() 失败"
    assert result["ndarray"] == [[2, 3], "<f8", 15.0]
    assert result["repicklable"] == 8, "重建出来的类无法再次序列化"


def test_object_ref_inlining():
    """任务参数里的 ObjectRef 应该变成占位符,反序列化时用真值填回。"""

    class _FakeObjectID:
        def __init__(self, raw: bytes):
            self._raw = raw

        def binary(self) -> bytes:
            return self._raw

    class _FakeObjectRef:
        def __init__(self, raw: bytes):
            self.id = _FakeObjectID(raw)

    oid = b"\x11" * 16
    other = b"\x22" * 16
    # 保存并恢复原值:这个注册表是**进程全局**的,如果清空它,
    # 后面所有测试里的 ObjectRef 都不会再被内联(表现为「任务参数拿到了 ref 本身」),
    # 而且只在整套测试一起跑时才复现 —— 单独跑这个文件是绿的。
    previous = serialization._OBJECT_REF_TYPE
    serialization.register_object_ref_type(_FakeObjectRef)
    try:
        # 内联模式:ref 变成占位符
        blob = serialization.dumps([_FakeObjectRef(oid), {"k": _FakeObjectRef(other)}], inline_object_refs=True)
        restored = serialization.loads(blob, {oid: "VALUE-A", other: "VALUE-B"})
        assert restored == ["VALUE-A", {"k": "VALUE-B"}]

        # 依赖没取回来就必须报错,而不是静默变成 None
        with pytest.raises(Exception, match="没有在反序列化前取回"):
            serialization.loads(blob, {oid: "VALUE-A"})

        # 非内联模式:ref 作为普通值传递(用于返回值里带 ref 的场景)
        # 注意:类是按值重建的,所以这里只能比类型名(不是同一个类对象)
        plain = serialization.loads(serialization.dumps([_FakeObjectRef(oid)], inline_object_refs=False))
        assert type(plain[0].id).__name__ == "_FakeObjectID"
    finally:
        serialization.register_object_ref_type(previous)


def test_custom_serializer():
    class Handle:
        def __init__(self, token: str):
            self.token = token
            self._lock = __import__("threading").Lock()  # 故意不可 pickle

    serialization.register_serializer(
        Handle,
        lambda h: h.token.encode(),
        lambda b: Handle(b.decode()),
    )
    try:
        restored = serialization.loads(serialization.dumps(Handle("secret")))
        assert restored.token == "secret"
    finally:
        serialization._CUSTOM_SERIALIZERS.clear()


def test_unpicklable_error_message():
    import threading

    with pytest.raises(serialization.PicklingError, match="无法序列化"):
        serialization.dumps(threading.Lock())
