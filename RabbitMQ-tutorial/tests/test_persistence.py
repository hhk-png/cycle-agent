from __future__ import annotations

import json

import pytest

from mini_rabbitmq import Broker
from mini_rabbitmq.errors import InternalError


def test_durable_topology_and_persistent_messages_recover(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.declare_exchange("ex", "direct", durable=True)
    broker.declare_queue("q", durable=True)
    broker.bind_queue("q", "ex", "rk")
    broker.publish("ex", "rk", b"persistent", properties={"delivery_mode": 2})
    broker.publish("ex", "rk", b"transient")
    broker.close()
    recovered = Broker(data_file=path)
    queue = recovered.vhost().get_queue("q")
    assert queue.messages_ready == 1
    assert recovered.get("q").message.body == b"persistent"
    recovered.close()


def test_acked_message_does_not_recover(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.declare_queue("q", durable=True)
    broker.publish("", "q", b"x", properties={"delivery_mode": 2})
    delivery = broker.get("q")
    broker.ack(delivery.delivery_tag)
    broker.close()
    recovered = Broker(data_file=path)
    assert recovered.get("q") is None
    recovered.close()


def test_unacked_message_recovers_as_ready(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.declare_queue("q", durable=True)
    broker.publish("", "q", b"x", properties={"delivery_mode": 2})
    broker.get("q")  # unacked
    broker.save()
    recovered = Broker(data_file=path)
    queue = recovered.vhost().get_queue("q")
    assert queue.messages_ready == 1
    assert queue.messages_unacknowledged == 0
    recovered.close()
    broker.close()


def test_policy_and_users_persist(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.set_policy("cap", ".*", {"max-length": 3})
    broker.add_user("alice", "secret", tags=("monitoring",))
    broker.set_permissions("alice", "/", configure="^q$", write="^ex$", read="^q$")
    broker.close()
    recovered = Broker(data_file=path)
    assert recovered.vhost().policies.get("cap") is not None
    user = recovered.auth.authenticate("alice", "secret")
    assert user is not None and "monitoring" in user.tags
    assert recovered.auth.authorize("alice", "/", "write", "ex")
    assert not recovered.auth.authorize("alice", "/", "write", "other")
    recovered.close()


def test_transient_objects_do_not_persist(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.declare_queue("transient")
    broker.declare_queue("durable", durable=True)
    broker.publish("", "durable", b"not-persistent")
    broker.close()
    recovered = Broker(data_file=path)
    assert "transient" not in recovered.vhost().queues
    assert recovered.vhost().get_queue("durable").messages_ready == 0
    recovered.close()


def test_vhosts_persist(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.create_vhost("stage")
    broker.declare_queue("q", durable=True, vhost="stage")
    broker.publish("", "q", b"x", properties={"delivery_mode": 2}, vhost="stage")
    broker.close()
    recovered = Broker(data_file=path)
    assert "stage" in recovered.vhosts
    assert recovered.get("q", vhost="stage").message.body == b"x"
    recovered.close()


def test_corrupt_snapshot_raises(local_tmp):
    path = local_tmp / "broker.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(InternalError):
        Broker(data_file=path)


def test_snapshot_is_json_and_has_no_plaintext_password(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path)
    broker.add_user("bob", "top-secret")
    broker.close()
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data)
    assert "top-secret" not in text
    assert data["format"] == "mini-rabbitmq-snapshot"
    assert data["version"] == 1


def test_auto_checkpoint_can_be_disabled(local_tmp):
    path = local_tmp / "broker.json"
    broker = Broker(data_file=path, auto_checkpoint=False)
    broker.declare_queue("q", durable=True)
    assert not path.exists()
    broker.flush()
    assert path.exists()
    broker.close()
