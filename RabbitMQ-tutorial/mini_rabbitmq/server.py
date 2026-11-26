"""AMQP 0-9-1 TCP 服务端。

每个连接一个读线程；消息投递可以发生在发布线程、消费线程或维护线程，
因此所有出站帧都通过连接级发送锁串行化。真实 RabbitMQ 使用更复杂的
调度器/credit 机制；这里的模型足以演示协议、确认和连接生命周期。
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .broker import Broker
from .errors import (
    AccessRefused,
    ConnectionError as AMQPConnectionError,
    FrameError,
    InvalidArgument,
    NotFound,
    NotImplementedError_,
    PreconditionFailed,
    ProtocolError,
    PublishRejected,
    RabbitMQError,
    ResourceError,
    ResourceLocked,
)
from .message import Message
from .protocol import (
    FRAME_BODY,
    FRAME_END,
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
    method_name,
    read_exact,
    read_frame,
)
from .queue import ConsumerHandle


LOG = logging.getLogger(__name__)
SERVER_VERSION = "0.1.0"


class _StopConnection(Exception):
    pass


def _is_loopback(address: str) -> bool:
    return (
        address in {"127.0.0.1", "::1", "localhost"}
        or address.startswith("127.")
        or address.startswith("::ffff:127.")
    )


@dataclass
class _PendingPublish:
    exchange: str
    routing_key: str
    mandatory: bool
    sequence: int
    body_size: int | None = None
    properties: dict[str, Any] | None = None
    body: bytearray = field(default_factory=bytearray)


class ChannelState:
    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection.connection_id,
            "channel_id": self.channel_id,
            "vhost": self.vhost,
            "user": self.user.name if self.user else None,
            "consumer_count": len(self.consumers),
            "prefetch_count": self.prefetch_count,
            "confirm": self.confirm,
            "transactional": self.tx,
            "state": "closed" if self.closed else "running",
        }

    def __init__(self, connection: "ConnectionHandler", channel_id: int) -> None:
        self.connection = connection
        self.broker = connection.broker
        self.channel_id = channel_id
        self.user = connection.user
        self.vhost = connection.vhost_name
        self.consumers: dict[str, ConsumerHandle] = {}
        self.consumer_wire_tags: dict[str, str] = {}
        self.prefetch_count = 0
        self.confirm = False
        self.tx = False
        self.next_publish_seq = 0
        self.tx_publishes: list[tuple[Message, bool, int]] = []
        self.tx_acks: list[tuple[str, int, bool, bool]] = []
        self.pending_publish: _PendingPublish | None = None
        self.closed = False
        self.cleanup_done = False

    def cleanup(self) -> None:
        if self.cleanup_done:
            return
        self.cleanup_done = True
        self.closed = True
        self.tx_publishes.clear()
        self.tx_acks.clear()
        try:
            self.broker.close_owner(self)
        except Exception:
            LOG.exception("channel cleanup failed")


class ConnectionHandler:
    def __init__(
        self,
        server: "AMQPServer",
        sock: socket.socket,
        address: tuple[str, int],
    ) -> None:
        self.server = server
        self.sock = sock
        self.address = address
        self.broker: Broker = server.broker
        self.auth = server.auth
        self.connection_id = uuid.uuid4().hex
        self.channels: dict[int, ChannelState] = {}
        self.send_lock = threading.RLock()
        self._closed = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.user = None
        self.vhost_name = "/"
        self.frame_max = server.frame_max
        self.channel_max = server.channel_max
        self.heartbeat = 0
        self.state = "starting"
        self.last_received = time.time()
        self.connected_at = time.time()
        self.recv_count = 0
        self.send_count = 0

    # ---- 生命周期 -----------------------------------------------------
    def run(self) -> None:
        try:
            self.sock.settimeout(10.0)
            self._handshake()
            self.state = "running"
            self.sock.settimeout(None)
            if self.heartbeat:
                self._start_heartbeat()
            self._frame_loop()
        except (_StopConnection, AMQPConnectionError, socket.timeout, OSError):
            pass
        except Exception:
            LOG.exception("connection %s crashed", self.connection_id)
        finally:
            self._shutdown()

    def _handshake(self) -> None:
        header = read_exact(self.sock, 8)
        if header != PROTOCOL_HEADER:
            try:
                self.sock.sendall(PROTOCOL_HEADER)
            except OSError:
                pass
            raise _StopConnection()

        start_props = {
            "product": "mini-rabbitmq",
            "product_version": SERVER_VERSION,
            "version": SERVER_VERSION,
            "platform": "Python",
            "copyright": "Educational implementation",
            "information": "AMQP 0-9-1 subset for the RabbitMQ tutorial",
            "capabilities": {
                "publisher_confirms": True,
                "exchange_exchange_bindings": True,
                "basic.nack": True,
                "consumer_cancel_notify": True,
                "connection.blocked": True,
                "authentication_failure_close": True,
            },
        }
        writer = Writer()
        writer.octet(0).octet(9).table(start_props).longstr(b"PLAIN").longstr(b"en_US")
        self._send_method(0, 10, 10, writer)

        class_id, method_id, reader = self._read_method(0)
        if (class_id, method_id) != (10, 11):
            raise ProtocolError("expected connection.start-ok")
        client_props = reader.table()
        mechanism = reader.shortstr()
        response = reader.longstr()
        locale = reader.shortstr()
        del client_props, locale
        if mechanism != "PLAIN":
            self._send_connection_close(403, f"unsupported mechanism '{mechanism}'", 10, 11)
            raise _StopConnection()
        parts = response.split(b"\x00")
        if len(parts) < 3:
            self._send_connection_close(403, "malformed PLAIN response", 10, 11)
            raise _StopConnection()
        username = parts[1].decode("utf-8", errors="replace")
        password = parts[2].decode("utf-8", errors="replace")
        if username == "guest" and not _is_loopback(self.address[0]):
            self._send_connection_close(
                403, "guest user can only connect from localhost", 10, 11
            )
            raise _StopConnection()
        user = self.auth.authenticate(username, password)
        if user is None:
            self._send_connection_close(403, "ACCESS_REFUSED - login was refused", 10, 11)
            raise _StopConnection()
        self.user = user

        writer = Writer()
        writer.short(self.channel_max).long(self.frame_max).short(self.server.heartbeat)
        self._send_method(0, 10, 30, writer)

        class_id, method_id, reader = self._read_method(0)
        if (class_id, method_id) != (10, 31):
            raise ProtocolError("expected connection.tune-ok")
        client_channel_max = reader.short()
        client_frame_max = reader.long()
        client_heartbeat = reader.short()
        if client_channel_max:
            self.channel_max = min(self.channel_max, client_channel_max)
        if client_frame_max:
            self.frame_max = min(self.frame_max, client_frame_max)
        self.heartbeat = client_heartbeat

        class_id, method_id, reader = self._read_method(0)
        if (class_id, method_id) != (10, 40):
            raise ProtocolError("expected connection.open")
        vhost = reader.shortstr()
        reader.shortstr()  # reserved capabilities
        reader.bits(1)  # insist
        if not self.auth.can_access_vhost(username, vhost):
            self._send_connection_close(530, f"no access to vhost '{vhost}'", 10, 40)
            raise _StopConnection()
        self.vhost_name = vhost
        writer = Writer().shortstr("")
        self._send_method(0, 10, 41, writer)
        self.server.register_connection(self)
        self.broker.metrics.inc("connections_total")
        self.broker.metrics.set("connections", len(self.server.connections))

    def _start_heartbeat(self) -> None:
        interval = max(1.0, self.heartbeat / 2.0)

        def loop() -> None:
            while not self._heartbeat_stop.wait(interval):
                if self._closed.is_set():
                    return
                if time.time() - self.last_received > self.heartbeat * 3:
                    LOG.info("closing connection %s after heartbeat timeout", self.connection_id)
                    self._shutdown()
                    return
                try:
                    with self.send_lock:
                        self._send_raw(encode_frame(FRAME_HEARTBEAT, 0, b""))
                except OSError:
                    self._shutdown()
                    return

        self._heartbeat_thread = threading.Thread(
            target=loop, name=f"amqp-heartbeat-{self.connection_id[:8]}", daemon=True
        )
        self._heartbeat_thread.start()

    def _shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._heartbeat_stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        for channel in list(self.channels.values()):
            channel.cleanup()
        self.channels.clear()
        try:
            self.broker.close_owner(self)
        except Exception:
            LOG.exception("connection owner cleanup failed")
        self.state = "closed"
        self.server.unregister_connection(self)
        self.broker.metrics.set("connections", len(self.server.connections))

    # ---- 帧与发送 -----------------------------------------------------
    def _send_raw(self, data: bytes) -> None:
        if self._closed.is_set():
            return
        self.sock.sendall(data)
        self.send_count += 1

    def _send_method(self, channel: int, class_id: int, method_id: int, writer: Writer | None = None) -> None:
        args = writer.payload() if writer is not None else b""
        payload = encode_method(class_id, method_id, args)
        with self.send_lock:
            self._send_raw(encode_frame(FRAME_METHOD, channel, payload))

    def _send_method_unlocked(
        self, channel: int, class_id: int, method_id: int, args: bytes
    ) -> None:
        self._send_raw(encode_frame(FRAME_METHOD, channel, encode_method(class_id, method_id, args)))

    def _send_content(
        self,
        channel: int,
        class_id: int,
        method_id: int,
        args: bytes,
        message: Message,
    ) -> None:
        if self._closed.is_set():
            return
        body = message.body
        chunk_size = self.frame_max - 8 if self.frame_max else 131064
        chunk_size = max(1, chunk_size)
        with self.send_lock:
            self._send_method_unlocked(channel, class_id, method_id, args)
            self._send_raw(
                encode_frame(
                    FRAME_HEADER,
                    channel,
                    encode_content_header(message.properties, len(body)),
                )
            )
            for start in range(0, len(body), chunk_size):
                self._send_raw(
                    encode_frame(FRAME_BODY, channel, body[start : start + chunk_size])
                )

    def _send_connection_close(
        self, code: int, text: str, class_id: int = 0, method_id: int = 0
    ) -> None:
        writer = Writer().short(code).shortstr(text).short(class_id).short(method_id)
        try:
            self._send_method(0, 10, 50, writer)
        except OSError:
            pass

    def _read_method(self, channel: int) -> tuple[int, int, Reader]:
        frame_type, frame_channel, payload = self._read_frame()
        if frame_type != FRAME_METHOD or frame_channel != channel:
            raise ProtocolError("expected a method frame")
        return decode_method(payload)

    def _read_frame(self) -> tuple[int, int, bytes]:
        frame = read_frame(self.sock, self.frame_max)
        self.last_received = time.time()
        return frame

    # ---- 主循环 -------------------------------------------------------
    def _frame_loop(self) -> None:
        while not self._closed.is_set():
            frame_type, channel_id, payload = self._read_frame()
            self.recv_count += 1
            try:
                if frame_type == FRAME_HEARTBEAT:
                    if channel_id != 0 or payload:
                        raise ProtocolError("malformed heartbeat frame")
                    continue
                if frame_type == FRAME_METHOD:
                    class_id, method_id, reader = decode_method(payload)
                    if channel_id == 0:
                        self._handle_connection_method(class_id, method_id, reader)
                    else:
                        self._handle_channel_method(channel_id, class_id, method_id, reader)
                    continue
                if frame_type == FRAME_HEADER:
                    self._handle_content_header(channel_id, payload)
                    continue
                if frame_type == FRAME_BODY:
                    self._handle_content_body(channel_id, payload)
                    continue
                raise FrameError(f"unknown frame type {frame_type}")
            except RabbitMQError as exc:
                if isinstance(exc, AMQPConnectionError) or channel_id == 0:
                    self._send_connection_close(exc.code, exc.message, 0, 0)
                    raise _StopConnection() from exc
                channel = self.channels.get(channel_id)
                if channel is not None:
                    self._close_channel(channel, exc.code, exc.message, 0, 0)
                else:
                    self._send_connection_close(exc.code, exc.message, 0, 0)
                    raise _StopConnection() from exc

    def _handle_connection_method(self, class_id: int, method_id: int, reader: Reader) -> None:
        if (class_id, method_id) == (10, 50):  # close
            code = reader.short()
            text = reader.shortstr()
            reader.short()
            reader.short()
            writer = Writer()
            self._send_method(0, 10, 51, writer)
            del code, text
            raise _StopConnection()
        if (class_id, method_id) == (10, 51):
            raise _StopConnection()
        if (class_id, method_id) == (10, 70):  # update-secret, not implemented
            self._send_connection_close(540, "connection.update-secret not implemented", 10, 70)
            raise _StopConnection()
        raise ProtocolError(f"unexpected method on channel 0: {method_name(class_id, method_id)}")

    def _handle_channel_method(
        self, channel_id: int, class_id: int, method_id: int, reader: Reader
    ) -> None:
        if channel_id > self.channel_max:
            raise AMQPConnectionError(f"channel {channel_id} exceeds channel-max {self.channel_max}")
        channel = self.channels.get(channel_id)
        if channel is not None and channel.closed:
            if (class_id, method_id) == (20, 41):
                self.channels.pop(channel_id, None)
                return
            return
        if (class_id, method_id) == (20, 10):
            self._channel_open(channel_id, reader)
            return
        if channel is None:
            raise AMQPConnectionError(f"channel {channel_id} is not open")
        try:
            self._dispatch_channel_method(channel, class_id, method_id, reader)
        except RabbitMQError as exc:
            if isinstance(exc, AMQPConnectionError):
                self._send_connection_close(exc.code, exc.message, class_id, method_id)
                raise _StopConnection()
            self._close_channel(channel, exc.code, exc.message, class_id, method_id)
        except Exception as exc:
            LOG.exception("channel method %s failed", method_name(class_id, method_id))
            self._close_channel(channel, 541, f"internal error: {exc}", class_id, method_id)

    def _channel_open(self, channel_id: int, reader: Reader) -> None:
        reader.shortstr()
        if channel_id in self.channels and not self.channels[channel_id].closed:
            raise AMQPConnectionError(f"channel {channel_id} already open")
        self.channels[channel_id] = ChannelState(self, channel_id)
        writer = Writer().longstr(b"")
        self._send_method(channel_id, 20, 11, writer)

    def _close_channel(
        self,
        channel: ChannelState,
        code: int,
        text: str,
        class_id: int = 0,
        method_id: int = 0,
    ) -> None:
        if channel.closed:
            return
        channel.closed = True
        writer = Writer().short(code).shortstr(text[:255]).short(class_id).short(method_id)
        try:
            self._send_method(channel.channel_id, 20, 40, writer)
        except OSError:
            pass
        channel.cleanup()
        self.broker.metrics.inc("channel_errors_total")

    # ---- 授权 ---------------------------------------------------------
    def _authorize(self, action: str, resource: str) -> None:
        if self.user is None or not self.auth.authorize(
            self.user.name, self.vhost_name, action, resource
        ):
            raise AccessRefused(
                f"user '{self.user.name if self.user else ''}' is not allowed to "
                f"{action} '{resource}' in vhost '{self.vhost_name}'"
            )

    # ---- 方法分发 -----------------------------------------------------
    def _dispatch_channel_method(
        self,
        channel: ChannelState,
        class_id: int,
        method_id: int,
        reader: Reader,
    ) -> None:
        if class_id == 20:
            self._channel_class(channel, method_id, reader)
        elif class_id == 40:
            self._exchange_class(channel, method_id, reader)
        elif class_id == 50:
            self._queue_class(channel, method_id, reader)
        elif class_id == 60:
            self._basic_class(channel, method_id, reader)
        elif class_id == 85:
            self._confirm_class(channel, method_id, reader)
        elif class_id == 90:
            self._tx_class(channel, method_id, reader)
        else:
            raise NotImplementedError_(f"class {class_id} is not implemented")

    def _channel_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        if method_id == 20:  # flow
            active = reader.bits(1)[0]
            self._send_method(channel.channel_id, 20, 21, Writer().bits(active))
        elif method_id == 40:  # close
            reader.short()
            reader.shortstr()
            reader.short()
            reader.short()
            channel.closed = True
            self._send_method(channel.channel_id, 20, 41, Writer())
            channel.cleanup()
        elif method_id == 41:
            self.channels.pop(channel.channel_id, None)
        else:
            raise NotImplementedError_(f"channel method {method_id} not implemented")

    # ---- Exchange -----------------------------------------------------
    def _exchange_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        if method_id == 10:  # declare
            reader.short()
            name = reader.shortstr()
            exchange_type = reader.shortstr()
            passive, durable, auto_delete, internal, nowait = reader.bits(5)
            arguments = reader.table()
            if not passive:
                self._authorize("configure", name)
            self.broker.declare_exchange(
                name,
                exchange_type,
                durable=durable,
                auto_delete=auto_delete,
                internal=internal,
                arguments=arguments,
                passive=passive,
                vhost=self.vhost_name,
            )
            if not nowait:
                self._send_method(channel.channel_id, 40, 11, Writer())
        elif method_id == 20:  # delete
            reader.short()
            name = reader.shortstr()
            if_unused, nowait = reader.bits(2)
            self._authorize("configure", name)
            self.broker.delete_exchange(name, if_unused=if_unused, vhost=self.vhost_name)
            if not nowait:
                self._send_method(channel.channel_id, 40, 21, Writer())
        elif method_id in (30, 40):  # bind / unbind
            reader.short()
            destination = reader.shortstr()
            source = reader.shortstr()
            routing_key = reader.shortstr()
            nowait = reader.bits(1)[0]
            arguments = reader.table()
            self._authorize("configure", destination)
            self._authorize("configure", source)
            if method_id == 30:
                self.broker.bind_exchange(
                    source,
                    destination,
                    routing_key,
                    arguments=arguments,
                    vhost=self.vhost_name,
                )
                reply = 31
            else:
                self.broker.unbind_exchange(
                    source,
                    destination,
                    routing_key,
                    arguments=arguments,
                    vhost=self.vhost_name,
                )
                reply = 41
            if not nowait:
                self._send_method(channel.channel_id, 40, reply, Writer())
        else:
            raise NotImplementedError_(f"exchange method {method_id} not implemented")

    # ---- Queue --------------------------------------------------------
    def _queue_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        if method_id == 10:  # declare
            reader.short()
            name = reader.shortstr()
            passive, durable, exclusive, auto_delete, nowait = reader.bits(5)
            arguments = reader.table()
            if not passive:
                self._authorize("configure", name or "amq.gen-*")
            queue = self.broker.declare_queue(
                name,
                durable=durable,
                exclusive=exclusive,
                auto_delete=auto_delete,
                arguments=arguments,
                owner=self,
                declaring_user=self.user.name if self.user else None,
                passive=passive,
                vhost=self.vhost_name,
            )
            if not nowait:
                writer = (
                    Writer()
                    .shortstr(queue.name)
                    .long(queue.messages_ready)
                    .long(queue.consumer_count)
                )
                self._send_method(channel.channel_id, 50, 11, writer)
        elif method_id == 20:  # bind
            reader.short()
            queue_name = reader.shortstr()
            exchange_name = reader.shortstr()
            routing_key = reader.shortstr()
            nowait = reader.bits(1)[0]
            arguments = reader.table()
            self._authorize("read", queue_name)
            self._authorize("write", exchange_name)
            self.broker.bind_queue(
                queue_name,
                exchange_name,
                routing_key,
                arguments=arguments,
                vhost=self.vhost_name,
            )
            if not nowait:
                self._send_method(channel.channel_id, 50, 21, Writer())
        elif method_id == 30:  # purge
            reader.short()
            queue_name = reader.shortstr()
            nowait = reader.bits(1)[0]
            self._authorize("configure", queue_name)
            queue = self.broker.vhost(self.vhost_name).get_queue(queue_name)
            count = queue.purge()
            self.broker.note_state_change()
            if not nowait:
                self._send_method(channel.channel_id, 50, 31, Writer().long(count))
        elif method_id == 40:  # delete
            reader.short()
            queue_name = reader.shortstr()
            if_unused, if_empty, nowait = reader.bits(3)
            self._authorize("configure", queue_name)
            count = self.broker.delete_queue(
                queue_name,
                if_unused=if_unused,
                if_empty=if_empty,
                vhost=self.vhost_name,
            )
            if not nowait:
                self._send_method(channel.channel_id, 50, 41, Writer().long(count))
        elif method_id == 50:  # unbind
            reader.short()
            queue_name = reader.shortstr()
            exchange_name = reader.shortstr()
            routing_key = reader.shortstr()
            arguments = reader.table()
            self._authorize("read", queue_name)
            self._authorize("write", exchange_name)
            self.broker.unbind_queue(
                queue_name,
                exchange_name,
                routing_key,
                arguments=arguments,
                vhost=self.vhost_name,
            )
            self._send_method(channel.channel_id, 50, 51, Writer())
        else:
            raise NotImplementedError_(f"queue method {method_id} not implemented")

    # ---- Basic --------------------------------------------------------
    def _basic_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        if method_id == 10:
            self._basic_qos(channel, reader)
        elif method_id == 20:
            self._basic_consume(channel, reader)
        elif method_id == 30:
            self._basic_cancel(channel, reader)
        elif method_id == 40:
            self._basic_publish(channel, reader)
        elif method_id == 70:
            self._basic_get(channel, reader)
        elif method_id == 80:
            self._basic_ack(channel, reader)
        elif method_id == 90:
            self._basic_reject(channel, reader)
        elif method_id in (100, 110):
            self._basic_recover(channel, method_id, reader)
        elif method_id == 120:
            self._basic_nack(channel, reader)
        else:
            raise NotImplementedError_(f"basic method {method_id} not implemented")

    def _basic_qos(self, channel: ChannelState, reader: Reader) -> None:
        prefetch_size = reader.long()
        prefetch_count = reader.short()
        global_ = reader.bits(1)[0]
        if prefetch_size:
            raise NotImplementedError_("basic.qos prefetch-size is not supported")
        if global_:
            raise NotImplementedError_("basic.qos global=true is not supported; use per-consumer QoS")
        channel.prefetch_count = prefetch_count
        for handle in channel.consumers.values():
            handle.prefetch_count = prefetch_count
        self._send_method(channel.channel_id, 60, 11, Writer())
        for handle in channel.consumers.values():
            queue = self.broker.vhost(self.vhost_name).get_queue(handle.queue_name)
            queue.dispatch()

    def _basic_consume(self, channel: ChannelState, reader: Reader) -> None:
        reader.short()
        queue_name = reader.shortstr()
        wire_tag = reader.shortstr()
        no_local, no_ack, exclusive, nowait = reader.bits(4)
        arguments = reader.table()
        if no_local:
            raise NotImplementedError_("basic.consume no-local is not supported")
        self._authorize("read", queue_name)
        if not wire_tag:
            wire_tag = f"ctag-{self.connection_id[:8]}-{len(channel.consumers) + 1}"
        if wire_tag in channel.consumers:
            raise PreconditionFailed(f"consumer tag '{wire_tag}' already in use")
        priority = 0
        try:
            priority = int(arguments.get("x-priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        internal_tag = f"{id(channel)}:{wire_tag}"

        def deliver(message: Message, delivery: Any) -> None:
            self._deliver_to_client(channel, wire_tag, message, delivery)

        handle = self.broker.add_consumer(
            queue_name,
            deliver,
            consumer_tag=internal_tag,
            auto_ack=no_ack,
            prefetch_count=channel.prefetch_count,
            exclusive=exclusive,
            priority=priority,
            owner=channel,
            suspended=True,
            dispatch=False,
            vhost=self.vhost_name,
        )
        channel.consumers[wire_tag] = handle
        if not nowait:
            self._send_method(channel.channel_id, 60, 21, Writer().shortstr(wire_tag))
        handle.suspended = False
        self.broker.vhost(self.vhost_name).get_queue(queue_name).dispatch()

    def _deliver_to_client(
        self, channel: ChannelState, wire_tag: str, message: Message, delivery: Any
    ) -> None:
        if channel.closed or self._closed.is_set():
            return
        writer = (
            Writer()
            .shortstr(wire_tag)
            .longlong(delivery.delivery_tag)
            .bits(delivery.message.redelivered)
            .shortstr(message.exchange)
            .shortstr(message.routing_key)
        )
        self._send_content(
            channel.channel_id,
            60,
            60,
            writer.payload(),
            message,
        )

    def _basic_cancel(self, channel: ChannelState, reader: Reader) -> None:
        wire_tag = reader.shortstr()
        nowait = reader.bits(1)[0]
        handle = channel.consumers.pop(wire_tag, None)
        if handle is not None:
            try:
                queue = self.broker.vhost(self.vhost_name).get_queue(handle.queue_name)
                queue.remove_consumer(handle.consumer_tag)
                if queue.should_auto_delete():
                    self.broker.delete_queue(queue.name, vhost=self.vhost_name)
            except NotFound:
                pass
        if not nowait:
            self._send_method(channel.channel_id, 60, 31, Writer().shortstr(wire_tag))

    def _basic_publish(self, channel: ChannelState, reader: Reader) -> None:
        reader.short()
        exchange = reader.shortstr()
        routing_key = reader.shortstr()
        mandatory, immediate = reader.bits(2)
        if immediate:
            raise NotImplementedError_(
                "basic.publish immediate=true was removed from RabbitMQ; use TTL + DLX"
            )
        self._authorize("write", exchange)
        if channel.pending_publish is not None:
            raise AMQPConnectionError("basic.publish received before previous content completed")
        channel.next_publish_seq += 1
        channel.pending_publish = _PendingPublish(
            exchange=exchange,
            routing_key=routing_key,
            mandatory=mandatory,
            sequence=channel.next_publish_seq,
        )

    def _handle_content_header(self, channel_id: int, payload: bytes) -> None:
        channel = self.channels.get(channel_id)
        if channel is None or channel.closed:
            raise AMQPConnectionError(f"content frame on closed channel {channel_id}")
        pending = channel.pending_publish
        if pending is None or pending.body_size is not None:
            raise AMQPConnectionError("unexpected content header frame")
        class_id, body_size, properties = decode_content_header(payload)
        if class_id != 60:
            raise AMQPConnectionError(f"content header class {class_id} is not basic")
        user_id = properties.get("user_id")
        if user_id and self.user is not None and user_id != self.user.name:
            raise PreconditionFailed("user_id property does not match authenticated user")
        pending.body_size = body_size
        pending.properties = properties
        if body_size == 0:
            self._finish_publish_safe(channel)

    def _handle_content_body(self, channel_id: int, payload: bytes) -> None:
        channel = self.channels.get(channel_id)
        if channel is None or channel.closed:
            raise AMQPConnectionError(f"body frame on closed channel {channel_id}")
        pending = channel.pending_publish
        if pending is None or pending.body_size is None:
            raise AMQPConnectionError("unexpected content body frame")
        pending.body += payload
        if len(pending.body) > pending.body_size:
            raise AMQPConnectionError("message body exceeds declared size")
        if len(pending.body) == pending.body_size:
            self._finish_publish_safe(channel)

    def _finish_publish_safe(self, channel: ChannelState) -> None:
        try:
            self._finish_publish(channel)
        except RabbitMQError as exc:
            if isinstance(exc, AMQPConnectionError):
                self._send_connection_close(exc.code, exc.message, 60, 40)
                raise _StopConnection() from exc
            self._close_channel(channel, exc.code, exc.message, 60, 40)
        except Exception as exc:  # pragma: no cover - 防御性
            LOG.exception("publishing failed")
            self._close_channel(channel, 541, f"internal error: {exc}", 60, 40)

    def _finish_publish(self, channel: ChannelState) -> None:
        pending = channel.pending_publish
        if pending is None:
            return
        channel.pending_publish = None
        message = Message(
            body=bytes(pending.body),
            properties=pending.properties or {},
            exchange=pending.exchange,
            routing_key=pending.routing_key,
        )
        if channel.tx:
            channel.tx_publishes.append((message, pending.mandatory, pending.sequence))
            return
        self._publish_now(channel, message, pending.mandatory, pending.sequence)

    def _publish_now(
        self, channel: ChannelState, message: Message, mandatory: bool, sequence: int
    ) -> None:
        try:
            result = self.broker.publish_message(
                message, mandatory=mandatory, vhost=self.vhost_name
            )
        except PublishRejected:
            # RabbitMQ 对 classic 队列的 reject-publish 以 basic.nack 回应
            # publisher confirm，不关闭信道。
            if channel.confirm:
                self._send_confirm(channel, sequence, ack=False)
            return
        except ResourceError as exc:
            if channel.confirm:
                self._send_confirm(channel, sequence, ack=False)
            raise exc
        except Exception:
            if channel.confirm:
                self._send_confirm(channel, sequence, ack=False)
            raise
        for returned in result.returned:
            writer = (
                Writer()
                .short(312)
                .shortstr("NO_ROUTE")
                .shortstr(returned.exchange)
                .shortstr(returned.routing_key)
            )
            self._send_content(channel.channel_id, 60, 50, writer.payload(), returned)
        if channel.confirm:
            self._send_confirm(channel, sequence, ack=True)

    def _send_confirm(self, channel: ChannelState, sequence: int, *, ack: bool) -> None:
        writer = Writer().longlong(sequence).bits(False)
        self._send_method(channel.channel_id, 60, 80 if ack else 120, writer)
        self.broker.metrics.inc("publisher_confirms_total")
        if not ack:
            self.broker.metrics.inc("publisher_nacks_total")

    def _basic_get(self, channel: ChannelState, reader: Reader) -> None:
        reader.short()
        queue_name = reader.shortstr()
        no_ack = reader.bits(1)[0]
        self._authorize("read", queue_name)
        queue = self.broker.vhost(self.vhost_name).get_queue(queue_name)
        delivery = queue.get(auto_ack=no_ack, owner=channel)
        if delivery is None:
            self._send_method(channel.channel_id, 60, 72, Writer().shortstr(""))
            return
        writer = (
            Writer()
            .longlong(delivery.delivery_tag)
            .bits(delivery.message.redelivered)
            .shortstr(delivery.message.exchange)
            .shortstr(delivery.message.routing_key)
            .long(queue.messages_ready)
        )
        self._send_content(channel.channel_id, 60, 71, writer.payload(), delivery.message)
        if no_ack:
            self.broker.note_state_change()

    def _basic_ack(self, channel: ChannelState, reader: Reader) -> None:
        delivery_tag = reader.longlong()
        multiple = reader.bits(1)[0]
        if channel.tx:
            channel.tx_acks.append(("ack", delivery_tag, multiple, False))
        else:
            self.broker.ack(delivery_tag, multiple=multiple, owner=channel)

    def _basic_nack(self, channel: ChannelState, reader: Reader) -> None:
        delivery_tag = reader.longlong()
        multiple, requeue = reader.bits(2)
        if channel.tx:
            channel.tx_acks.append(("nack", delivery_tag, multiple, requeue))
        else:
            self.broker.nack(
                delivery_tag, multiple=multiple, requeue=requeue, owner=channel
            )

    def _basic_reject(self, channel: ChannelState, reader: Reader) -> None:
        delivery_tag = reader.longlong()
        requeue = reader.bits(1)[0]
        if channel.tx:
            channel.tx_acks.append(("nack", delivery_tag, False, requeue))
        else:
            self.broker.nack(delivery_tag, requeue=requeue, owner=channel)

    def _basic_recover(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        requeue = reader.bits(1)[0]
        if not requeue:
            raise NotImplementedError_("basic.recover(requeue=false) is not supported")
        self.broker.recover_owner(channel)
        if method_id == 110:
            self._send_method(channel.channel_id, 60, 111, Writer())

    # ---- Confirm / Tx -------------------------------------------------
    def _confirm_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        if method_id != 10:
            raise NotImplementedError_(f"confirm method {method_id} not implemented")
        nowait = reader.bits(1)[0]
        if channel.tx:
            raise PreconditionFailed("cannot enable publisher confirms in a transactional channel")
        channel.confirm = True
        if not nowait:
            self._send_method(channel.channel_id, 85, 11, Writer())

    def _tx_class(self, channel: ChannelState, method_id: int, reader: Reader) -> None:
        del reader
        if method_id == 10:
            if channel.confirm:
                raise PreconditionFailed("cannot select transactions on a confirm channel")
            channel.tx = True
            self._send_method(channel.channel_id, 90, 11, Writer())
        elif method_id == 20:
            if not channel.tx:
                raise PreconditionFailed("tx.commit without tx.select")
            self._commit_tx(channel)
            self._send_method(channel.channel_id, 90, 21, Writer())
        elif method_id == 30:
            channel.tx_publishes.clear()
            channel.tx_acks.clear()
            self._send_method(channel.channel_id, 90, 31, Writer())
        else:
            raise NotImplementedError_(f"tx method {method_id} not implemented")

    def _commit_tx(self, channel: ChannelState) -> None:
        for kind, tag, multiple, requeue in channel.tx_acks:
            if kind == "ack":
                self.broker.ack(tag, multiple=multiple, owner=channel)
            else:
                self.broker.nack(tag, multiple=multiple, requeue=requeue, owner=channel)
        channel.tx_acks.clear()
        for message, mandatory, sequence in channel.tx_publishes:
            self._publish_now(channel, message, mandatory, sequence)
        channel.tx_publishes.clear()

    # ---- 管理展示 -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "name": f"{self.address[0]}:{self.address[1]} -> {self.server.host}:{self.server.port}",
            "peer_host": self.address[0],
            "peer_port": self.address[1],
            "user": self.user.name if self.user else None,
            "vhost": self.vhost_name,
            "state": self.state,
            "channels": len(self.channels),
            "connected_at": self.connected_at,
            "recv_count": self.recv_count,
            "send_count": self.send_count,
            "protocol": "AMQP 0-9-1",
        }


class AMQPServer:
    def __init__(
        self,
        broker: Broker,
        host: str = "127.0.0.1",
        port: int = 5672,
        *,
        heartbeat: int = 60,
        frame_max: int = 131072,
        channel_max: int = 2047,
        ssl_context: ssl.SSLContext | None = None,
        auth=None,
    ) -> None:
        self.broker = broker
        self.host = host
        self.port = port
        self.heartbeat = heartbeat
        self.frame_max = frame_max
        self.channel_max = channel_max
        self.ssl_context = ssl_context
        self.auth = auth or broker.auth
        self.connections: set[ConnectionHandler] = set()
        self._lock = threading.RLock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._alarm_listener = self._on_alarm

    # ---- 启停 ---------------------------------------------------------
    def start(self) -> int:
        if self._listener is not None:
            return self.port
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(128)
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="amqp-accept", daemon=True
        )
        self._thread.start()
        self.broker.add_alarm_listener(self._alarm_listener)
        return self.port

    start_background = start

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client, address = self._listener.accept()
            except OSError:
                break
            client.settimeout(None)
            sock = client
            if self.ssl_context is not None:
                try:
                    sock = self.ssl_context.wrap_socket(client, server_side=True)
                except ssl.SSLError:
                    client.close()
                    continue
            handler = ConnectionHandler(self, sock, address)
            with self._lock:
                self.connections.add(handler)
            threading.Thread(
                target=handler.run,
                name=f"amqp-conn-{handler.connection_id[:8]}",
                daemon=True,
            ).start()

    def register_connection(self, connection: ConnectionHandler) -> None:
        with self._lock:
            self.connections.add(connection)

    def unregister_connection(self, connection: ConnectionHandler) -> None:
        with self._lock:
            self.connections.discard(connection)

    def stop(self) -> None:
        self._stop.set()
        self.broker.remove_alarm_listener(self._alarm_listener)
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self.connections)
        for connection in connections:
            connection._shutdown()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def __enter__(self) -> "AMQPServer":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # ---- 管理查询 -----------------------------------------------------
    def connection_list(self) -> list[dict[str, Any]]:
        with self._lock:
            connections = list(self.connections)
        return [connection.to_dict() for connection in connections]

    def channel_list(self) -> list[dict[str, Any]]:
        with self._lock:
            connections = list(self.connections)
        result: list[dict[str, Any]] = []
        for connection in connections:
            result.extend(channel.to_dict() for channel in connection.channels.values())
        return result

    def _on_alarm(self, kind: str, active: bool, reason: str) -> None:
        with self._lock:
            connections = list(self.connections)
        for connection in connections:
            if connection._closed.is_set():
                continue
            try:
                if active:
                    writer = Writer().shortstr(f"{kind}: {reason}")
                    connection._send_method(0, 10, 60, writer)
                else:
                    connection._send_method(0, 10, 61, Writer())
            except OSError:
                pass
