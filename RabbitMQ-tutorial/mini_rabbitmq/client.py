"""同步 AMQP 0-9-1 客户端，用于示例和端到端测试。

它不是通用客户端库：只实现 mini 服务端暴露的受控子集，但遵循真实帧布局，
因此可以作为学习“库如何工作”的参考。
"""

from __future__ import annotations

import logging
import queue
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import ProtocolError, RabbitMQError
from .message import Message
from .protocol import (
    FRAME_BODY,
    FRAME_HEADER,
    FRAME_HEARTBEAT,
    FRAME_METHOD,
    PROTOCOL_HEADER,
    Reader,
    Writer,
    decode_content_header,
    decode_method,
    encode_content_header,
    encode_frame,
    encode_method,
    read_exact,
    read_frame,
)


LOG = logging.getLogger(__name__)


@dataclass
class WireDelivery:
    delivery_tag: int
    consumer_tag: str
    redelivered: bool
    exchange: str
    routing_key: str
    channel: int


@dataclass
class ConsumerSubscription:
    channel: int
    consumer_tag: str


@dataclass
class GetResult:
    channel: int
    delivery_tag: int
    message: Message
    redelivered: bool
    message_count: int


@dataclass
class QueueDeclareResult:
    name: str
    message_count: int
    consumer_count: int


@dataclass
class ReturnedMessage:
    reply_code: int
    reply_text: str
    message: Message


@dataclass
class _RpcResult:
    method: str
    fields: dict[str, Any] = field(default_factory=dict)
    message: Message | None = None


@dataclass
class _PendingContent:
    method: str
    fields: dict[str, Any]
    properties: dict[str, Any] | None = None
    body_size: int | None = None
    body: bytearray = field(default_factory=bytearray)


class ChannelClosedError(RabbitMQError):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(text, code=code)


class AMQPClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5672,
        *,
        username: str = "guest",
        password: str = "guest",
        vhost: str = "/",
        heartbeat: int = 0,
        timeout: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
        client_properties: dict[str, Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.vhost = vhost
        self.heartbeat = heartbeat
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.client_properties = client_properties or {
            "product": "mini-rabbitmq-client",
            "platform": "Python",
            "version": "0.1.0",
            "capabilities": {
                "publisher_confirms": True,
                "basic.nack": True,
                "consumer_cancel_notify": True,
            },
        }
        self.sock: socket.socket | None = None
        self.frame_max = 131072
        self.channel_max = 2047
        self._send_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._closed = threading.Event()
        self._channels: set[int] = set()
        self._next_channel = 1
        self._default_channel: int | None = None
        self._waiters: dict[int, queue.Queue[_RpcResult | BaseException]] = {}
        self._pending_content: dict[int, _PendingContent] = {}
        self._consumers: dict[tuple[int, str], Callable[[Message, WireDelivery], None]] = {}
        self._consumer_channels: dict[int, set[str]] = {}
        self._confirm_channels: set[int] = set()
        self._publish_seq: dict[int, int] = {}
        self._pending_confirms: dict[int, set[int]] = {}
        self._nacked: dict[int, set[int]] = {}
        self._confirm_cv = threading.Condition()
        self._returned: list[ReturnedMessage] = []
        self.on_return: Callable[[ReturnedMessage], None] | None = None
        self.on_connection_blocked: Callable[[str], None] | None = None
        self.on_connection_unblocked: Callable[[], None] | None = None
        self.last_received = time.time()
        self.last_error: BaseException | None = None

    # ---- 连接与握手 ---------------------------------------------------
    def connect(self) -> "AMQPClient":
        if self.sock is not None:
            return self
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(self.timeout)
        self.sock = sock
        try:
            sock.sendall(PROTOCOL_HEADER)
            frame_type, channel, payload = read_frame(sock)
            if frame_type != FRAME_METHOD or channel != 0:
                raise ProtocolError("expected connection.start")
            class_id, method_id, reader = decode_method(payload)
            if (class_id, method_id) != (10, 10):
                raise ProtocolError("expected connection.start")
            reader.octet()  # version-major (0)
            reader.octet()  # version-minor (9)
            server_properties = reader.table()
            mechanisms = reader.longstr().decode("ascii", errors="replace")
            reader.longstr()  # locales
            if "PLAIN" not in mechanisms.split():
                raise ProtocolError("server does not offer PLAIN authentication")
            del server_properties
            response = b"\x00" + self.username.encode("utf-8") + b"\x00" + self.password.encode("utf-8")
            writer = (
                Writer()
                .table(self.client_properties)
                .shortstr("PLAIN")
                .longstr(response)
                .shortstr("en_US")
            )
            self._send_method(0, 10, 11, writer)
            frame_type, channel, payload = read_frame(sock)
            if frame_type != FRAME_METHOD or channel != 0:
                raise ProtocolError("expected connection.tune")
            class_id, method_id, reader = decode_method(payload)
            if (class_id, method_id) == (10, 50):
                code = reader.short()
                text = reader.shortstr()
                raise ChannelClosedError(code, text)
            if (class_id, method_id) != (10, 30):
                raise ProtocolError("expected connection.tune")
            server_channel_max = reader.short()
            server_frame_max = reader.long()
            reader.short()  # server heartbeat
            self.channel_max = server_channel_max or 2047
            self.frame_max = server_frame_max or 131072
            writer = Writer().short(self.channel_max).long(self.frame_max).short(self.heartbeat)
            self._send_method(0, 10, 31, writer)
            writer = Writer().shortstr(self.vhost).shortstr("").bits(False)
            self._send_method(0, 10, 40, writer)
            frame_type, channel, payload = read_frame(sock)
            if frame_type != FRAME_METHOD or channel != 0:
                raise ProtocolError("expected connection.open-ok")
            class_id, method_id, reader = decode_method(payload)
            if (class_id, method_id) == (10, 50):
                # 服务端拒绝认证或 vhost
                code = reader.short()
                text = reader.shortstr()
                raise ChannelClosedError(code, text)
            if (class_id, method_id) != (10, 41):
                raise ProtocolError("expected connection.open-ok")
            sock.settimeout(None)
            self._reader_thread = threading.Thread(
                target=self._reader_loop, name="amqp-client-reader", daemon=True
            )
            self._reader_thread.start()
            if self.heartbeat:
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, name="amqp-client-heartbeat", daemon=True
                )
                self._heartbeat_thread.start()
            return self
        except BaseException:
            sock.close()
            self.sock = None
            raise

    # ---- 底层发送 -----------------------------------------------------
    def _send_method(self, channel: int, class_id: int, method_id: int, writer: Writer | None = None) -> None:
        if self.sock is None:
            raise ConnectionError("client is not connected")
        payload = encode_method(class_id, method_id, writer.payload() if writer else b"")
        with self._send_lock:
            self.sock.sendall(encode_frame(FRAME_METHOD, channel, payload))

    def _send_content(
        self,
        channel: int,
        class_id: int,
        method_id: int,
        args: Writer,
        body: bytes,
        properties: dict[str, Any],
    ) -> None:
        if self.sock is None:
            raise ConnectionError("client is not connected")
        chunk_size = max(1, (self.frame_max or 131072) - 8)
        with self._send_lock:
            self.sock.sendall(
                encode_frame(
                    FRAME_METHOD,
                    channel,
                    encode_method(class_id, method_id, args.payload()),
                )
            )
            self.sock.sendall(
                encode_frame(
                    FRAME_HEADER,
                    channel,
                    encode_content_header(properties, len(body)),
                )
            )
            for start in range(0, len(body), chunk_size):
                self.sock.sendall(
                    encode_frame(FRAME_BODY, channel, body[start : start + chunk_size])
                )

    def _heartbeat_loop(self) -> None:
        interval = max(1.0, self.heartbeat / 2)

        def loop() -> None:
            while not self._heartbeat_stop.wait(interval):
                if self._closed.is_set() or self.sock is None:
                    return
                if time.time() - self.last_received > self.heartbeat * 3:
                    self._hard_close(ConnectionError("heartbeat timeout"))
                    return
                try:
                    with self._send_lock:
                        self.sock.sendall(encode_frame(FRAME_HEARTBEAT, 0, b""))
                except OSError as exc:
                    self._hard_close(exc)
                    return

        self._heartbeat_thread = threading.Thread(target=loop, daemon=True)
        self._heartbeat_thread.start()

    # ---- 读取与分发 ---------------------------------------------------
    def _reader_loop(self) -> None:
        assert self.sock is not None
        try:
            while not self._closed.is_set():
                frame_type, channel, payload = read_frame(self.sock, self.frame_max)
                self.last_received = time.time()
                if frame_type == FRAME_HEARTBEAT:
                    continue
                if frame_type == FRAME_METHOD:
                    self._handle_method_frame(channel, payload)
                elif frame_type == FRAME_HEADER:
                    self._handle_header_frame(channel, payload)
                elif frame_type == FRAME_BODY:
                    self._handle_body_frame(channel, payload)
                else:
                    raise ProtocolError(f"unknown frame type {frame_type}")
        except (OSError, ConnectionError) as exc:
            self._hard_close(exc)
        except BaseException as exc:  # pragma: no cover - 防御性
            self.last_error = exc
            self._hard_close(exc)

    def _handle_method_frame(self, channel: int, payload: bytes) -> None:
        class_id, method_id, reader = decode_method(payload)
        name = self._method_name(class_id, method_id)
        if (class_id, method_id) == (10, 50):
            code = reader.short()
            text = reader.shortstr()
            try:
                self._send_method(0, 10, 51, Writer())
            except OSError:
                pass
            exc = ChannelClosedError(code, text)
            self.last_error = exc
            self._hard_close(exc)
            return
        if (class_id, method_id) == (10, 60):
            reason = reader.shortstr()
            if self.on_connection_blocked:
                self.on_connection_blocked(reason)
            return
        if (class_id, method_id) == (10, 61):
            if self.on_connection_unblocked:
                self.on_connection_unblocked()
            return
        if (class_id, method_id) == (20, 40):
            code = reader.short()
            text = reader.shortstr()
            reader.short()
            reader.short()
            try:
                self._send_method(channel, 20, 41, Writer())
            except OSError:
                pass
            self._channels.discard(channel)
            self._consumer_channels.pop(channel, None)
            self._put_waiter(channel, ChannelClosedError(code, text))
            return
        if (class_id, method_id) == (60, 60):  # deliver
            wire_tag = reader.shortstr()
            delivery_tag = reader.longlong()
            redelivered = reader.bits(1)[0]
            exchange = reader.shortstr()
            routing_key = reader.shortstr()
            self._pending_content[channel] = _PendingContent(
                "deliver",
                {
                    "consumer_tag": wire_tag,
                    "delivery_tag": delivery_tag,
                    "redelivered": redelivered,
                    "exchange": exchange,
                    "routing_key": routing_key,
                },
            )
            return
        if (class_id, method_id) == (60, 71):  # get-ok
            delivery_tag = reader.longlong()
            redelivered = reader.bits(1)[0]
            exchange = reader.shortstr()
            routing_key = reader.shortstr()
            message_count = reader.long()
            self._pending_content[channel] = _PendingContent(
                "get-ok",
                {
                    "delivery_tag": delivery_tag,
                    "redelivered": redelivered,
                    "exchange": exchange,
                    "routing_key": routing_key,
                    "message_count": message_count,
                },
            )
            return
        if (class_id, method_id) == (60, 50):  # return
            reply_code = reader.short()
            reply_text = reader.shortstr()
            exchange = reader.shortstr()
            routing_key = reader.shortstr()
            self._pending_content[channel] = _PendingContent(
                "return",
                {
                    "reply_code": reply_code,
                    "reply_text": reply_text,
                    "exchange": exchange,
                    "routing_key": routing_key,
                },
            )
            return
        if (class_id, method_id) in ((60, 80), (60, 120)) and channel in self._confirm_channels:
            delivery_tag = reader.longlong()
            multiple = reader.bits(1)[0]
            ack = (class_id, method_id) == (60, 80)
            self._confirm(channel, delivery_tag, multiple, ack)
            return
        if (class_id, method_id) == (60, 30):  # server-initiated cancel
            wire_tag = reader.shortstr()
            reader.bits(1)
            self._consumers.pop((channel, wire_tag), None)
            return

        fields: dict[str, Any] = {}
        if name == "queue.declare-ok":
            fields = {
                "queue": reader.shortstr(),
                "message_count": reader.long(),
                "consumer_count": reader.long(),
            }
        elif name == "queue.purge-ok":
            fields = {"message_count": reader.long()}
        elif name == "queue.delete-ok":
            fields = {"message_count": reader.long()}
        elif name == "basic.consume-ok":
            fields = {"consumer_tag": reader.shortstr()}
        elif name == "basic.cancel-ok":
            fields = {"consumer_tag": reader.shortstr()}
        elif name == "basic.get-empty":
            fields = {"reserved": reader.shortstr()}
        elif (class_id, method_id) == (20, 11):
            fields = {"reserved": reader.longstr()}
        elif (class_id, method_id) == (10, 41):
            fields = {"reserved": reader.shortstr()}
        if (class_id, method_id) in ((10, 51), (20, 41)):
            fields = {}
        self._put_waiter(channel, _RpcResult(name, fields))

    def _handle_header_frame(self, channel: int, payload: bytes) -> None:
        pending = self._pending_content.get(channel)
        if pending is None:
            raise ProtocolError("unexpected content header")
        class_id, body_size, properties = decode_content_header(payload)
        if class_id != 60:
            raise ProtocolError("content header class is not basic")
        pending.body_size = body_size
        pending.properties = properties
        if body_size == 0:
            self._finish_content(channel, pending)

    def _handle_body_frame(self, channel: int, payload: bytes) -> None:
        pending = self._pending_content.get(channel)
        if pending is None or pending.body_size is None:
            raise ProtocolError("unexpected content body")
        pending.body += payload
        if len(pending.body) > pending.body_size:
            raise ProtocolError("content body overrun")
        if len(pending.body) == pending.body_size:
            self._finish_content(channel, pending)

    def _finish_content(self, channel: int, pending: _PendingContent) -> None:
        self._pending_content.pop(channel, None)
        fields = dict(pending.fields)
        properties = pending.properties or {}
        message = Message(
            body=bytes(pending.body),
            properties=properties,
            exchange=fields.get("exchange", ""),
            routing_key=fields.get("routing_key", ""),
            redelivered=bool(fields.get("redelivered", False)),
        )
        if pending.method == "deliver":
            callback = self._consumers.get((channel, fields["consumer_tag"]))
            if callback is not None:
                info = WireDelivery(
                    delivery_tag=fields["delivery_tag"],
                    consumer_tag=fields["consumer_tag"],
                    redelivered=fields["redelivered"],
                    exchange=fields["exchange"],
                    routing_key=fields["routing_key"],
                    channel=channel,
                )
                try:
                    callback(message, info)
                except Exception:
                    LOG.exception("consumer callback %s failed", fields["consumer_tag"])
            return
        if pending.method == "get-ok":
            self._put_waiter(
                channel,
                _RpcResult(
                    "basic.get-ok",
                    {
                        **fields,
                        "channel": channel,
                        "message": message,
                    },
                    message,
                ),
            )
            return
        if pending.method == "return":
            returned = ReturnedMessage(
                reply_code=fields["reply_code"],
                reply_text=fields["reply_text"],
                message=message,
            )
            self._returned.append(returned)
            if self.on_return:
                self.on_return(returned)

    def _put_waiter(self, channel: int, result: _RpcResult | BaseException) -> None:
        waiter = self._waiters.get(channel)
        if waiter is not None:
            waiter.put(result)

    def _confirm(self, channel: int, tag: int, multiple: bool, ack: bool) -> None:
        with self._confirm_cv:
            pending = self._pending_confirms.setdefault(channel, set())
            if multiple:
                matched = {seq for seq in pending if seq <= tag or tag == 0}
            else:
                matched = {tag} & pending
            pending.difference_update(matched)
            if not ack:
                self._nacked.setdefault(channel, set()).update(matched)
            self._confirm_cv.notify_all()

    # ---- RPC ----------------------------------------------------------
    def _waiter(self, channel: int) -> queue.Queue[_RpcResult | BaseException]:
        return self._waiters.setdefault(channel, queue.Queue())

    def _rpc(
        self,
        channel: int,
        class_id: int,
        method_id: int,
        writer: Writer,
        expected: str | tuple[str, ...],
        timeout: float | None = None,
    ) -> _RpcResult:
        expected_names = (expected,) if isinstance(expected, str) else expected
        waiter = self._waiter(channel)
        # 清掉上一个请求可能残留的回复，避免错配。
        while not waiter.empty():
            try:
                waiter.get_nowait()
            except queue.Empty:
                break
        self._send_method(channel, class_id, method_id, writer)
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {expected_names}")
            try:
                result = waiter.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"timed out waiting for {expected_names}") from exc
            if isinstance(result, BaseException):
                raise result
            if result.method in expected_names:
                return result
            # 不是当前期待的响应：保留一个诊断，避免无限循环。
            self.last_error = ProtocolError(
                f"unexpected RPC response {result.method}, expected {expected_names}"
            )

    # ---- 信道 ---------------------------------------------------------
    def channel(self) -> int:
        self._require_connection()
        channel = self._next_channel
        self._next_channel += 1
        if channel > self.channel_max:
            raise RuntimeError("channel-max exhausted")
        waiter = self._waiter(channel)
        while not waiter.empty():
            waiter.get_nowait()
        self._send_method(channel, 20, 10, Writer().shortstr(""))
        deadline = time.monotonic() + self.timeout
        while True:
            result = waiter.get(timeout=max(0.001, deadline - time.monotonic()))
            if isinstance(result, BaseException):
                raise result
            if result.method == "channel.open-ok":
                break
        self._channels.add(channel)
        return channel

    def _ensure_channel(self, channel: int | None) -> int:
        if channel is not None:
            if channel not in self._channels:
                raise RuntimeError(f"channel {channel} is not open")
            return channel
        if self._default_channel is None or self._default_channel not in self._channels:
            self._default_channel = self.channel()
        return self._default_channel

    def _require_connection(self) -> None:
        if self.sock is None or self._closed.is_set():
            raise ConnectionError("client is not connected")

    # ---- 拓扑 ---------------------------------------------------------
    def declare_exchange(
        self,
        name: str,
        exchange_type: str = "direct",
        *,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
        passive: bool = False,
        nowait: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(name)
            .shortstr(exchange_type)
            .bits(passive, durable, auto_delete, internal, nowait)
            .table(arguments or {})
        )
        if nowait:
            self._send_method(channel, 40, 10, writer)
            return
        self._rpc(channel, 40, 10, writer, "exchange.declare-ok")

    def delete_exchange(
        self,
        name: str,
        *,
        if_unused: bool = False,
        nowait: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = Writer().short(0).shortstr(name).bits(if_unused, nowait)
        if nowait:
            self._send_method(channel, 40, 20, writer)
            return
        self._rpc(channel, 40, 20, writer, "exchange.delete-ok")

    def bind_exchange(
        self,
        destination: str,
        source: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        nowait: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(destination)
            .shortstr(source)
            .shortstr(routing_key)
            .bits(nowait)
            .table(arguments or {})
        )
        if nowait:
            self._send_method(channel, 40, 30, writer)
            return
        self._rpc(channel, 40, 30, writer, "exchange.bind-ok")

    def unbind_exchange(
        self,
        destination: str,
        source: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(destination)
            .shortstr(source)
            .shortstr(routing_key)
            .bits(False)
            .table(arguments or {})
        )
        self._rpc(channel, 40, 40, writer, "exchange.unbind-ok")

    def declare_queue(
        self,
        name: str = "",
        *,
        durable: bool = False,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
        passive: bool = False,
        nowait: bool = False,
        channel: int | None = None,
    ) -> QueueDeclareResult:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(name)
            .bits(passive, durable, exclusive, auto_delete, nowait)
            .table(arguments or {})
        )
        if nowait:
            self._send_method(channel, 50, 10, writer)
            return QueueDeclareResult(name, 0, 0)
        result = self._rpc(channel, 50, 10, writer, "queue.declare-ok")
        return QueueDeclareResult(
            result.fields["queue"],
            result.fields["message_count"],
            result.fields["consumer_count"],
        )

    def bind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        nowait: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(queue_name)
            .shortstr(exchange_name)
            .shortstr(routing_key)
            .bits(nowait)
            .table(arguments or {})
        )
        if nowait:
            self._send_method(channel, 50, 20, writer)
            return
        self._rpc(channel, 50, 20, writer, "queue.bind-ok")

    def unbind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        writer = (
            Writer()
            .short(0)
            .shortstr(queue_name)
            .shortstr(exchange_name)
            .shortstr(routing_key)
            .table(arguments or {})
        )
        self._rpc(channel, 50, 50, writer, "queue.unbind-ok")

    def purge_queue(self, name: str, *, nowait: bool = False, channel: int | None = None) -> int:
        channel = self._ensure_channel(channel)
        writer = Writer().short(0).shortstr(name).bits(nowait)
        if nowait:
            self._send_method(channel, 50, 30, writer)
            return 0
        return int(self._rpc(channel, 50, 30, writer, "queue.purge-ok").fields["message_count"])

    def delete_queue(
        self,
        name: str,
        *,
        if_unused: bool = False,
        if_empty: bool = False,
        nowait: bool = False,
        channel: int | None = None,
    ) -> int:
        channel = self._ensure_channel(channel)
        writer = Writer().short(0).shortstr(name).bits(if_unused, if_empty, nowait)
        if nowait:
            self._send_method(channel, 50, 40, writer)
            return 0
        return int(self._rpc(channel, 50, 40, writer, "queue.delete-ok").fields["message_count"])

    # ---- 发布 ---------------------------------------------------------
    def publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes | str,
        *,
        properties: dict[str, Any] | None = None,
        mandatory: bool = False,
        channel: int | None = None,
    ) -> int:
        channel = self._ensure_channel(channel)
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        props = dict(properties or {})
        with self._confirm_cv:
            sequence = self._publish_seq.get(channel, 0) + 1
            self._publish_seq[channel] = sequence
            if channel in self._confirm_channels:
                self._pending_confirms.setdefault(channel, set()).add(sequence)
        writer = (
            Writer()
            .short(0)
            .shortstr(exchange)
            .shortstr(routing_key)
            .bits(mandatory, False)
        )
        self._send_content(channel, 60, 40, writer, raw, props)
        return sequence

    def wait_for_confirms(self, channel: int | None = None, timeout: float | None = None) -> bool:
        channel = self._ensure_channel(channel)
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        with self._confirm_cv:
            while self._pending_confirms.get(channel):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._confirm_cv.wait(remaining)
            return not self._nacked.get(channel)

    def nacked_confirms(self, channel: int | None = None) -> set[int]:
        channel = self._ensure_channel(channel)
        with self._confirm_cv:
            return set(self._nacked.get(channel, set()))

    def confirm_select(self, *, channel: int | None = None) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(channel, 85, 10, Writer().bits(False), "confirm.select-ok")
        self._confirm_channels.add(channel)
        self._pending_confirms.setdefault(channel, set())

    def returned_messages(self) -> list[ReturnedMessage]:
        return list(self._returned)

    # ---- 消费 ---------------------------------------------------------
    def qos(
        self,
        prefetch_count: int,
        *,
        prefetch_size: int = 0,
        global_: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(
            channel,
            60,
            10,
            Writer().long(prefetch_size).short(prefetch_count).bits(global_),
            "basic.qos-ok",
        )

    def consume(
        self,
        queue_name: str,
        callback: Callable[[Message, WireDelivery], None],
        *,
        auto_ack: bool = False,
        exclusive: bool = False,
        consumer_tag: str = "",
        arguments: dict[str, Any] | None = None,
        channel: int | None = None,
    ) -> ConsumerSubscription:
        channel = self._ensure_channel(channel)
        tag = consumer_tag or f"ctag-{uuid.uuid4().hex[:12]}"
        self._consumers[(channel, tag)] = callback
        self._consumer_channels.setdefault(channel, set()).add(tag)
        writer = (
            Writer()
            .short(0)
            .shortstr(queue_name)
            .shortstr(tag)
            .bits(False, auto_ack, exclusive, False)
            .table(arguments or {})
        )
        try:
            result = self._rpc(channel, 60, 20, writer, "basic.consume-ok")
        except BaseException:
            self._consumers.pop((channel, tag), None)
            raise
        returned_tag = result.fields.get("consumer_tag", tag)
        if returned_tag != tag:
            callback_entry = self._consumers.pop((channel, tag), None)
            if callback_entry is not None:
                self._consumers[(channel, returned_tag)] = callback_entry
        return ConsumerSubscription(channel, returned_tag)

    def cancel_consumer(
        self,
        consumer: ConsumerSubscription | str,
        *,
        channel: int | None = None,
        nowait: bool = False,
    ) -> None:
        if isinstance(consumer, ConsumerSubscription):
            channel = consumer.channel
            tag = consumer.consumer_tag
        else:
            tag = consumer
            channel = self._ensure_channel(channel)
        assert channel is not None
        if nowait:
            self._send_method(channel, 60, 30, Writer().shortstr(tag).bits(True))
        else:
            self._rpc(
                channel,
                60,
                30,
                Writer().shortstr(tag).bits(False),
                "basic.cancel-ok",
            )
        self._consumers.pop((channel, tag), None)

    def get(
        self,
        queue_name: str,
        *,
        auto_ack: bool = False,
        channel: int | None = None,
    ) -> GetResult | None:
        channel = self._ensure_channel(channel)
        result = self._rpc(
            channel,
            60,
            70,
            Writer().short(0).shortstr(queue_name).bits(auto_ack),
            ("basic.get-ok", "basic.get-empty"),
        )
        if result.method == "basic.get-empty":
            return None
        message = result.message
        if message is None:  # pragma: no cover - 协议防御
            raise ProtocolError("basic.get-ok without content")
        return GetResult(
            channel=channel,
            delivery_tag=int(result.fields["delivery_tag"]),
            message=message,
            redelivered=bool(result.fields["redelivered"]),
            message_count=int(result.fields["message_count"]),
        )

    def ack(
        self,
        delivery_tag: int,
        *,
        multiple: bool = False,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        self._send_method(channel, 60, 80, Writer().longlong(delivery_tag).bits(multiple))

    def nack(
        self,
        delivery_tag: int,
        *,
        multiple: bool = False,
        requeue: bool = True,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        self._send_method(
            channel,
            60,
            120,
            Writer().longlong(delivery_tag).bits(multiple, requeue),
        )

    def reject(
        self,
        delivery_tag: int,
        *,
        requeue: bool = True,
        channel: int | None = None,
    ) -> None:
        channel = self._ensure_channel(channel)
        self._send_method(channel, 60, 90, Writer().longlong(delivery_tag).bits(requeue))

    def recover(self, *, requeue: bool = True, channel: int | None = None) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(channel, 60, 110, Writer().bits(requeue), "basic.recover-ok")

    # ---- 事务 ---------------------------------------------------------
    def tx_select(self, *, channel: int | None = None) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(channel, 90, 10, Writer(), "tx.select-ok")

    def tx_commit(self, *, channel: int | None = None) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(channel, 90, 20, Writer(), "tx.commit-ok")

    def tx_rollback(self, *, channel: int | None = None) -> None:
        channel = self._ensure_channel(channel)
        self._rpc(channel, 90, 30, Writer(), "tx.rollback-ok")

    # ---- 关闭 ---------------------------------------------------------
    def close_channel(self, channel: int) -> None:
        if channel not in self._channels:
            return
        try:
            self._rpc(channel, 20, 40, Writer().short(200).shortstr("OK").short(0).short(0), "channel.close-ok")
        finally:
            self._channels.discard(channel)
            self._consumer_channels.pop(channel, None)
            self._waiters.pop(channel, None)

    def close(self) -> None:
        if self._closed.is_set():
            return
        try:
            if self.sock is not None:
                waiter = self._waiter(0)
                self._send_method(
                    0,
                    10,
                    50,
                    Writer().short(200).shortstr("OK").short(0).short(0),
                )
                try:
                    waiter.get(timeout=1.0)
                except queue.Empty:
                    pass
        except OSError:
            pass
        self._hard_close(None)

    def _hard_close(self, exc: BaseException | None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._heartbeat_stop.set()
        if exc is not None:
            self.last_error = exc
        for waiter in list(self._waiters.values()):
            if isinstance(exc, BaseException):
                waiter.put(exc)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        with self._confirm_cv:
            self._confirm_cv.notify_all()

    def __enter__(self) -> "AMQPClient":
        return self.connect()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @staticmethod
    def _method_name(class_id: int, method_id: int) -> str:
        from .protocol import method_name

        return method_name(class_id, method_id)
