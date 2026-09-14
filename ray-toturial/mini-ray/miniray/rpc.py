"""极简 RPC 框架 —— mini-ray 里所有跨进程通信的底座。

真实 Ray 的控制面用 gRPC(``raylet``/``core worker``/``GCS`` 之间),数据面用
「对象传输」的独立通道(2.55 起默认多连接)。mini-ray 用一个更小的东西:
**TCP + 4 字节长度前缀 + pickle**,原因是教学上要能一眼看完。

.. code-block:: text

   ┌────────────┬──────────────────────────────┐
   │ length(4B) │  pickle({"method":…, …})     │
   └────────────┴──────────────────────────────┘
     big-endian

设计要点(每一条都是踩过坑才有的):

* **一连接一线程 + 长轮询**。worker 拿任务不是 raylet 主动推(那需要 worker 也开
  监听端口),而是 worker 发一个「有活干吗」的请求,raylet 把它挂起,有任务时再回复。
  Ray 的 raylet 早期也是这个模型(worker 定期 ``RequestWorkerLease``)。
* **服务端每 1 秒超时回一个 noop**。这样长轮询不需要真的「永久挂起」:
  客户端收到 noop 就再问一次。副作用是顺便做了心跳 —— 连接断了立刻能发现。
* **异常不走 RPC 错误通道,而是当成值**。任务的异常要能带回原始类型与堆栈,
  所以它被序列化成对象放进对象存储,由 ``ray.get`` 重新抛出(见 errors.py)。
  RPC 层只管「框架自己出错」的情况。
"""

from __future__ import annotations

import os
import pickle
import socket
import socketserver
import struct
import threading
import traceback
from typing import Any, Callable, Dict, Optional

from .errors import MiniRayletDiedError, MiniRayError, RaySystemError

__all__ = ["RpcClient", "RpcServer", "RpcError", "MAX_MESSAGE_SIZE"]

_HEADER = struct.Struct("!I")
#: 单条消息上限(与 Ray 的 gRPC 上限不同,这里主要是防止误用导致内存爆炸)
MAX_MESSAGE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
_RECV_CHUNK = 1 << 20


class RpcError(RaySystemError):
    """RPC 层自身的错误(不是被调用方法的业务异常)。"""


def _minray_error_class(type_name: str):
    """把错误类型名映射回 mini-ray 的异常类(找不到返回 ``None``)。"""
    if not type_name:
        return None
    from . import errors

    candidate = getattr(errors, type_name, None)
    if isinstance(candidate, type) and issubclass(candidate, MiniRayError):
        return candidate
    return None


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(remaining, _RECV_CHUNK))
        if not chunk:
            raise ConnectionError("连接在对端关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_MESSAGE_SIZE:
        raise RpcError(f"单条消息过大: {len(payload)} 字节")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _recv_frame(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, _HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE_SIZE:
        raise RpcError(f"收到非法长度 {length},可能不是 mini-ray 的协议")
    return _recv_exact(sock, length)


class RpcClient:
    """线程安全的 RPC 客户端。

    同一个 :class:`RpcClient` 可以被多个线程用(内部加锁串行化),但**长轮询会
    长时间占住连接**,所以 actor 的并发执行上下文各自持有自己的 client。
    """

    def __init__(self, address: str, *, name: str = "raylet") -> None:
        self._address = address
        self._name = name
        self._host, port_str = address.rsplit(":", 1)
        self._port = int(port_str)
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=10.0)
        except OSError as exc:
            raise MiniRayletDiedError(
                f"无法连接 {self._name} ({self._address}): {exc}。"
                "通常意味着 raylet 已经退出(检查是否调用了 miniray.shutdown())"
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 平时不设超时(阻塞等回复);需要超时的调用会临时 settimeout
        sock.settimeout(None)
        return sock

    def _ensure_sock(self) -> socket.socket:
        if self._sock is None:
            self._sock = self._connect()
        return self._sock

    def _drop_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------ 调用
    def call(self, method: str, *args: Any, _call_timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """调用服务端方法。

        :param _call_timeout: **客户端**等待回复的超时时间(秒)。
            注意它叫 ``_call_timeout`` 而不是 ``timeout`` —— 因为很多 RPC 方法
            自己就有一个叫 ``timeout`` 的参数(``wait_for_objects`` /
            ``wait_for_generator``),两者同名会把「服务端等 0.5 秒」变成
            「客户端 0.5 秒后放弃」,是一个非常阴的 bug。
            默认 ``None`` 表示一直等(服务端的长轮询最多 1 秒会回一次 noop)。
        """
        payload = pickle.dumps({"method": method, "args": args, "kwargs": kwargs}, protocol=5)
        with self._lock:
            if self._closed:
                raise MiniRayletDiedError(f"{self._name} 客户端已关闭")
            for attempt in (0, 1):
                sock = self._ensure_sock()
                try:
                    _send_frame(sock, payload)
                    if _call_timeout is not None:
                        sock.settimeout(_call_timeout)
                    raw = _recv_frame(sock)
                    if _call_timeout is not None:
                        sock.settimeout(None)
                    break
                except socket.timeout as exc:
                    # 必须先于 OSError 捕获:Python 3.10 起 socket.timeout 就是
                    # TimeoutError,而它是 OSError 的子类
                    self._drop_sock()
                    raise RpcError(f"调用 {self._name}.{method} 超时({_call_timeout}s)") from exc
                except (ConnectionError, OSError, struct.error) as exc:
                    self._drop_sock()
                    if attempt == 0:
                        continue  # 只重试一次:可能只是连接被 idle 断开了
                    raise MiniRayletDiedError(
                        f"调用 {self._name}.{method} 失败: {exc!r}"
                    ) from exc

        reply = pickle.loads(raw)
        if reply.get("ok"):
            return reply["value"]
        err = reply["error"]
        message = f"{self._name}.{method} 失败: {err['type']}: {err['message']}"
        # 如果服务端抛的是 mini-ray 自己的异常(比如 ObjectStoreFullError),
        # 客户端应该拿到**同样的类型** —— 否则调用方只能靠解析字符串来判定,
        # 而「对象存储满了」这类错误是必须能被程序化处理的。
        error_cls = _minray_error_class(err.get("type", ""))
        if error_cls is not None:
            raise error_cls(message)
        raise RpcError(f"{message}\n{err.get('traceback','')}")

    @property
    def address(self) -> str:
        return self._address

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop_sock()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"RpcClient({self._address}, closed={self._closed})"


class _ThreadingRpcServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True  # 主进程退出时不要被 handler 线程卡住
    request_queue_size = 128

    def handle_error(self, request, client_address):  # pragma: no cover - 调试用
        # 默认实现会把 traceback 打到 stderr;RPC 断连是常态,不该刷屏
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, ConnectionResetError, OSError)):
            return
        traceback.print_exc()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service = self.server.service  # type: ignore[attr-defined]
        sock = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 钩子是可选的:服务对象不定义它们也能用(getattr 而不是直接取属性,
        # 否则每个没定义钩子的服务都会在第一条连接上炸掉)
        on_connect = getattr(service, "on_connect", None)
        on_disconnect = getattr(service, "on_disconnect", None)
        if on_connect is not None:
            try:
                on_connect(self.client_address)
            except Exception:  # pragma: no cover - 钩子不该影响主流程
                traceback.print_exc()
        try:
            while True:
                raw = _recv_frame(sock)
                request = pickle.loads(raw)
                reply = self._dispatch(service, request)
                _send_frame(sock, pickle.dumps(reply, protocol=5))
        except (ConnectionError, OSError, struct.error, EOFError):
            pass  # 对端关闭,正常退出
        except Exception:  # pragma: no cover - 协议层出错
            traceback.print_exc()
        finally:
            if on_disconnect is not None:
                try:
                    on_disconnect(self.client_address)
                except Exception:  # pragma: no cover
                    traceback.print_exc()

    @staticmethod
    def _dispatch(service: Any, request: Dict[str, Any]) -> Dict[str, Any]:
        method_name = request["method"]
        method: Optional[Callable] = getattr(service, method_name, None)
        if method is None or method_name.startswith("_"):
            return {
                "ok": False,
                "error": {
                    "type": "AttributeError",
                    "message": f"服务端没有方法 {method_name!r}",
                    "traceback": "",
                },
            }
        try:
            value = method(*request["args"], **request["kwargs"])
            return {"ok": True, "value": value}
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }


class RpcServer:
    """把任意对象的公开方法暴露成 RPC。

    服务对象上所有不以 ``_`` 开头的属性都是可调用方法;想收连接事件就定义
    ``on_connect`` / ``on_disconnect`` 钩子。
    """

    def __init__(self, service: Any, *, host: str = "127.0.0.1", port: int = 0, name: str = "rpc") -> None:
        self._server = _ThreadingRpcServer((host, port), _Handler)
        self._server.service = service  # type: ignore[attr-defined]
        self._service = service
        self._name = name
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    @property
    def address(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> "RpcServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"miniray-{self._name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._server.shutdown()
        except Exception:  # pragma: no cover
            pass
        try:
            self._server.server_close()
        except Exception:  # pragma: no cover
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "RpcServer":  # pragma: no cover - 便捷用法
        return self.start()

    def __exit__(self, *exc_info) -> None:  # pragma: no cover
        self.stop()


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * Ray 控制面是 gRPC + protobuf(强类型、跨语言、支持流式);mini-ray 用
#     pickle over TCP,牺牲跨语言能力换取「零依赖 + 可读」。
#   * Ray 的对象传输有专门的 ObjectManager + 多连接/分块;mini-ray 的对象传输
#     走同一条 RPC 通道(大对象用共享内存规避拷贝,见 object_store.py)。
#   * Ray 有超时、重试、背压、TLS、鉴权;mini-ray 只有「重连一次」。
# ---------------------------------------------------------------------------
