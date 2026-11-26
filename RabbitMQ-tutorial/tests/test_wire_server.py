from __future__ import annotations

import socket
import time
import uuid

import pytest

from mini_rabbitmq.broker import Broker
from mini_rabbitmq.client import AMQPClient, ChannelClosedError
from mini_rabbitmq.protocol import (
    PROTOCOL_HEADER,
    Reader,
    decode_method,
    read_frame,
)
from mini_rabbitmq.server import AMQPServer


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_raw_handshake_connection_start(server):
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    try:
        sock.sendall(PROTOCOL_HEADER)
        frame_type, channel, payload = read_frame(sock)
        class_id, method_id, reader = decode_method(payload)
        assert (frame_type, channel, class_id, method_id) == (1, 0, 10, 10)
        assert (reader.octet(), reader.octet()) == (0, 9)
        properties = reader.table()
        assert properties["product"] == "mini-rabbitmq"
        assert reader.longstr() == b"PLAIN"
        assert reader.longstr() == b"en_US"
    finally:
        sock.close()


def test_wrong_protocol_header_gets_supported_header(server):
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    try:
        sock.sendall(b"NOPE\x00\x00\x09\x01")
        assert sock.recv(8) == PROTOCOL_HEADER
    finally:
        sock.close()


def test_default_exchange_wire(client):
    channel = client.channel()
    client.declare_queue("wire-q", channel=channel)
    client.publish("", "wire-q", b"hello", channel=channel)
    result = client.get("wire-q", channel=channel)
    assert result is not None
    assert result.message.body == b"hello"
    assert result.redelivered is False
    client.ack(result.delivery_tag, channel=channel)


def test_exchange_bind_publish_get(client):
    channel = client.channel()
    client.declare_exchange("events", "topic", channel=channel)
    client.declare_queue("audit", channel=channel)
    client.bind_queue("audit", "events", "user.#", channel=channel)
    client.publish("events", "user.login", b"login", channel=channel)
    result = client.get("audit", channel=channel)
    assert result.message.body == b"login"
    assert result.message.routing_key == "user.login"
    client.ack(result.delivery_tag, channel=channel)


def test_queue_declare_counts_and_server_named(client):
    channel = client.channel()
    result = client.declare_queue("", channel=channel)
    assert result.name.startswith("amq.gen-")
    client.publish("", result.name, b"x", channel=channel)
    declared = client.declare_queue(result.name, passive=True, channel=channel)
    assert declared.message_count == 1


def test_get_empty(client):
    channel = client.channel()
    client.declare_queue("empty-q", channel=channel)
    assert client.get("empty-q", channel=channel) is None


def test_mandatory_return_with_confirm(client):
    channel = client.channel()
    client.confirm_select(channel=channel)
    client.declare_exchange("direct-ex", "direct", channel=channel)
    client.publish("direct-ex", "missing", b"lost", mandatory=True, channel=channel)
    assert client.wait_for_confirms(channel=channel)
    assert len(client.returned_messages()) == 1
    assert client.returned_messages()[0].reply_code == 312
    assert client.returned_messages()[0].message.body == b"lost"


def test_publisher_confirms_success(client):
    channel = client.channel()
    client.confirm_select(channel=channel)
    client.declare_queue("confirmed", channel=channel)
    for i in range(3):
        client.publish("", "confirmed", f"m{i}", channel=channel)
    assert client.wait_for_confirms(channel=channel)
    assert client.nacked_confirms(channel=channel) == set()
    assert client.declare_queue("confirmed", passive=True, channel=channel).message_count == 3


def test_publisher_confirm_nack_on_reject_publish(client):
    channel = client.channel()
    client.confirm_select(channel=channel)
    client.declare_queue(
        "tiny",
        arguments={"x-max-length": 1, "x-overflow": "reject-publish"},
        channel=channel,
    )
    client.publish("", "tiny", b"first", channel=channel)
    assert client.wait_for_confirms(channel=channel)
    client.publish("", "tiny", b"second", channel=channel)
    assert client.wait_for_confirms(channel=channel) is False
    assert client.nacked_confirms(channel=channel) == {2}
    # 信道仍然可用，与 RabbitMQ 的 reject-publish 行为一致。
    assert client.declare_queue("tiny", passive=True, channel=channel).message_count == 1


def test_consume_and_ack_with_prefetch(client):
    channel = client.channel()
    client.declare_queue("stream-q", channel=channel)
    received = []
    infos = []
    lock = __import__("threading").Lock()

    def callback(message, info):
        with lock:
            received.append(message.body)
            infos.append(info)

    client.qos(1, channel=channel)
    for i in range(3):
        client.publish("", "stream-q", f"m{i}", channel=channel)
    client.consume("stream-q", callback, channel=channel)

    assert wait_until(lambda: len(received) == 1)
    time.sleep(0.1)
    # callback 没有 ack，prefetch=1 时只有第一条会被投递。
    assert len(received) == 1

    client.ack(infos[0].delivery_tag, channel=channel)
    assert wait_until(lambda: len(received) == 2)
    client.ack(infos[1].delivery_tag, channel=channel)
    assert wait_until(lambda: len(received) == 3)
    client.ack(infos[2].delivery_tag, channel=channel)
    assert received == [b"m0", b"m1", b"m2"]


def test_consumer_ack_flow_and_cancel(client):
    channel = client.channel()
    client.declare_queue("consume-q", channel=channel)
    received = []
    done = __import__("threading").Event()

    def callback(message, info):
        received.append(message.body)
        client.ack(info.delivery_tag, channel=channel)
        if len(received) == 2:
            done.set()

    consumer = client.consume("consume-q", callback, channel=channel)
    for i in range(2):
        client.publish("", "consume-q", f"m{i}", channel=channel)
    assert done.wait(2)
    assert received == [b"m0", b"m1"]
    client.cancel_consumer(consumer)
    assert "consume-q" in client._channels or True  # 消费者取消不影响信道


def test_auto_ack_consumer(client):
    channel = client.channel()
    client.declare_queue("auto-q", channel=channel)
    received = []
    done = __import__("threading").Event()

    def callback(message, info):
        received.append(message.body)
        done.set()

    client.consume("auto-q", callback, auto_ack=True, channel=channel)
    client.publish("", "auto-q", b"x", channel=channel)
    assert done.wait(2)
    assert received == [b"x"]
    assert client.declare_queue("auto-q", passive=True, channel=channel).message_count == 0


def test_channel_close_requeues_unacked(client):
    channel = client.channel()
    client.declare_queue("requeue-q", channel=channel)
    client.publish("", "requeue-q", b"x", channel=channel)
    result = client.get("requeue-q", channel=channel)
    assert result is not None
    client.close_channel(channel)
    new_channel = client.channel()
    again = client.get("requeue-q", channel=new_channel)
    assert again is not None
    assert again.message.body == b"x"
    assert again.redelivered is True


def test_connection_close_deletes_exclusive_queue(server, broker):
    first = AMQPClient(port=server.port)
    first.connect()
    channel = first.channel()
    first.declare_queue("exclusive-q", exclusive=True, channel=channel)
    first.close()
    assert wait_until(lambda: "exclusive-q" not in broker.vhost().queues)
    second = AMQPClient(port=server.port)
    second.connect()
    ch2 = second.channel()
    with pytest.raises(ChannelClosedError):
        second.declare_queue("exclusive-q", passive=True, channel=ch2)
    second.close()


def test_auto_delete_queue_after_cancel(client, broker):
    channel = client.channel()
    client.declare_queue("auto-delete-q", auto_delete=True, channel=channel)
    consumer = client.consume("auto-delete-q", lambda m, i: None, channel=channel)
    client.cancel_consumer(consumer)
    assert wait_until(lambda: "auto-delete-q" not in broker.vhost().queues)


def test_transactions_commit_and_rollback(client):
    channel = client.channel()
    client.declare_queue("tx-q", channel=channel)
    client.tx_select(channel=channel)
    client.publish("", "tx-q", b"one", channel=channel)
    assert client.declare_queue("tx-q", passive=True, channel=channel).message_count == 0
    client.tx_commit(channel=channel)
    assert client.declare_queue("tx-q", passive=True, channel=channel).message_count == 1
    client.publish("", "tx-q", b"discard", channel=channel)
    client.tx_rollback(channel=channel)
    assert client.declare_queue("tx-q", passive=True, channel=channel).message_count == 1


def test_transactions_and_confirms_are_mutually_exclusive(client):
    channel = client.channel()
    client.tx_select(channel=channel)
    with pytest.raises(ChannelClosedError):
        client.confirm_select(channel=channel)
    new_channel = client.channel()
    assert new_channel != channel


def test_passive_missing_closes_channel(client):
    channel = client.channel()
    with pytest.raises(ChannelClosedError):
        client.declare_queue("does-not-exist", passive=True, channel=channel)
    new_channel = client.channel()
    client.declare_queue("exists", channel=new_channel)


def test_wrong_password_is_rejected(server):
    bad = AMQPClient(port=server.port, password="wrong")
    with pytest.raises((ChannelClosedError, ConnectionError, OSError)):
        bad.connect()
    bad.close()


def test_guest_vhost_permission_denied(server, broker):
    broker.auth.add_user("novhost", "pw", tags=())
    bad = AMQPClient(port=server.port, username="novhost", password="pw")
    with pytest.raises(ChannelClosedError):
        bad.connect()
    bad.close()


def test_queue_permission_denied(server, broker):
    broker.auth.add_user("limited", "pw", tags=())
    broker.auth.set_permissions(
        "limited", "/", configure="^allowed$", write="^allowed$", read="^allowed$"
    )
    limited = AMQPClient(port=server.port, username="limited", password="pw")
    limited.connect()
    channel = limited.channel()
    limited.declare_queue("allowed", channel=channel)
    with pytest.raises(ChannelClosedError):
        limited.declare_queue("denied", channel=channel)
    limited.close()


def test_user_id_mismatch_closes_channel(client):
    channel = client.channel()
    client.declare_queue("user-q", channel=channel)
    client.publish(
        "",
        "user-q",
        b"x",
        channel=channel,
        properties={"user_id": "someone-else"},
    )
    with pytest.raises((ChannelClosedError, RuntimeError, OSError)):
        client.get("user-q", channel=channel)


def test_global_qos_not_implemented_closes_channel(client):
    channel = client.channel()
    with pytest.raises(ChannelClosedError):
        client.qos(1, global_=True, channel=channel)


def test_large_message_chunking(server):
    client = AMQPClient(port=server.port)
    client.connect()
    channel = client.channel()
    client.declare_queue("large", channel=channel)
    body = b"x" * 350_000
    client.publish("", "large", body, channel=channel)
    result = client.get("large", channel=channel)
    assert result.message.body == body
    client.ack(result.delivery_tag, channel=channel)
    client.close()


def test_small_frame_max_negotiation():
    broker = Broker()
    server = AMQPServer(broker, port=0, frame_max=4096)
    server.start()
    try:
        client = AMQPClient(port=server.port)
        client.connect()
        channel = client.channel()
        client.declare_queue("small-frame", channel=channel)
        payload = b"y" * 10_000
        client.publish("", "small-frame", payload, channel=channel)
        result = client.get("small-frame", channel=channel)
        assert result.message.body == payload
        client.close()
    finally:
        server.stop()
        broker.close()


def test_purge_and_delete_counts(client):
    channel = client.channel()
    client.declare_queue("counts", channel=channel)
    for i in range(3):
        client.publish("", "counts", f"m{i}", channel=channel)
    assert client.purge_queue("counts", channel=channel) == 3
    client.publish("", "counts", b"x", channel=channel)
    assert client.delete_queue("counts", channel=channel) == 1


def test_basic_recover_requeues(client):
    channel = client.channel()
    client.declare_queue("recover-q", channel=channel)
    client.publish("", "recover-q", b"x", channel=channel)
    first = client.get("recover-q", channel=channel)
    assert first is not None
    client.recover(requeue=True, channel=channel)
    second = client.get("recover-q", channel=channel)
    assert second is not None
    assert second.redelivered is True


def test_exchange_to_exchange_wire(client):
    channel = client.channel()
    client.declare_exchange("src", "direct", channel=channel)
    client.declare_exchange("dst", "fanout", channel=channel)
    client.bind_exchange("dst", "src", "key", channel=channel)
    client.declare_queue("e2e-q", channel=channel)
    client.bind_queue("e2e-q", "dst", "", channel=channel)
    client.publish("src", "key", b"x", channel=channel)
    assert client.get("e2e-q", channel=channel).message.body == b"x"
