from __future__ import annotations

import _bootstrap  # noqa: F401

from mini_rabbitmq.cluster import QuorumQueue


def main() -> None:
    queue = QuorumQueue("payments", ["node-1", "node-2", "node-3"])
    leader = queue.cluster.elect()
    assert leader is not None
    print("leader:", leader.node_id)
    queue.publish({"id": 1, "amount": 42})
    queue.publish({"id": 2, "amount": 7})
    assert queue.messages() == [
        {"id": 1, "amount": 42},
        {"id": 2, "amount": 7},
    ]

    old_leader = queue.leader
    new_leader = queue.failover()
    assert new_leader is not None
    print("old leader:", old_leader)
    print("new leader:", new_leader.node_id)
    print("committed messages:", len(queue.messages()))
    state = queue.state()
    print("cluster term:", state["term"])
    assert queue.leader != old_leader
    assert len(queue.messages()) == 2
    print("OK 15_quorum_failover")


if __name__ == "__main__":
    main()
