from __future__ import annotations

import pytest

from mini_rabbitmq.cluster import QuorumQueue, RaftCluster
from mini_rabbitmq.errors import NoQuorum


def make_cluster():
    return RaftCluster(["n1", "n2", "n3"])


def test_no_leader_means_no_append():
    cluster = make_cluster()
    with pytest.raises(NoQuorum):
        cluster.append("x")


def test_election_requires_majority():
    cluster = make_cluster()
    leader = cluster.elect()
    assert leader is not None
    assert leader.node_id in {"n1", "n2", "n3"}
    assert leader.current_term >= 1
    assert cluster.leader_id == leader.node_id


def test_append_replicates_and_commits():
    cluster = make_cluster()
    leader = cluster.elect()
    index = cluster.append("one")
    assert index == 1
    assert cluster.committed_index() == 1
    assert cluster.applied == ["one"]
    followers = [node for node in cluster.nodes.values() if node.node_id != leader.node_id]
    for follower in followers:
        assert follower.log[-1].command == "one"
        assert follower.commit_index == 1


def test_append_survives_one_failure():
    cluster = make_cluster()
    cluster.elect()
    follower = next(node for node in cluster.nodes.values() if node is not cluster.leader())
    cluster.fail(follower.node_id)
    assert cluster.append("still-committed") == 1
    assert cluster.applied == ["still-committed"]


def test_majority_loss_blocks_append():
    cluster = make_cluster()
    leader = cluster.elect()
    followers = [node for node in cluster.nodes.values() if node is not leader]
    cluster.fail(followers[0].node_id)
    cluster.fail(followers[1].node_id)
    with pytest.raises(NoQuorum):
        cluster.append("blocked")


def test_leader_failover_and_new_leader():
    cluster = make_cluster()
    old = cluster.elect()
    cluster.append("one")
    cluster.fail(old.node_id)
    new = cluster.elect()
    assert new is not None
    assert new.node_id != old.node_id
    assert new.current_term > old.current_term
    assert cluster.append("two") == 2


def test_recovered_node_catches_up():
    cluster = make_cluster()
    leader = cluster.elect()
    follower = next(node for node in cluster.nodes.values() if node is not leader)
    cluster.fail(follower.node_id)
    cluster.append("while-down")
    cluster.recover(follower.node_id)
    cluster.sync()
    assert follower.log[-1].command == "while-down"
    assert follower.commit_index == cluster.committed_index()


def test_new_leader_chooses_most_up_to_date_log():
    cluster = make_cluster()
    old = cluster.elect()
    cluster.append("committed")
    follower = next(node for node in cluster.nodes.values() if node is not old)
    # 人为让一个 follower 的日志更旧，另一个与 leader 一致。
    follower.log = follower.log[:1]
    cluster.fail(old.node_id)
    new = cluster.elect()
    assert new is not None
    assert new.last_log_index == 1


def test_terms_increase_with_elections():
    cluster = make_cluster()
    first = cluster.elect()
    term_one = first.current_term
    cluster.fail(first.node_id)
    second = cluster.elect()
    assert second is not None and second.current_term > term_one


def test_quorum_queue_publish_and_failover():
    queue = QuorumQueue("orders")
    first = queue.cluster.elect()
    assert first is not None
    assert queue.publish(b"m1") == 1
    assert queue.publish(b"m2") == 2
    assert queue.messages() == [b"m1", b"m2"]
    assert queue.leader == first.node_id
    new_leader = queue.failover()
    assert new_leader is not None
    assert queue.leader == new_leader.node_id
    assert queue.messages() == [b"m1", b"m2"]


def test_quorum_queue_state_shape():
    queue = QuorumQueue("q", ["a", "b", "c"])
    queue.cluster.elect()
    queue.publish(b"x")
    state = queue.state()
    assert state["queue"] == "q"
    assert state["messages"] == 1
    assert len(state["nodes"]) == 3


def test_divergent_uncommitted_suffix_is_truncated_on_next_append():
    cluster = make_cluster()
    old_leader = cluster.elect()
    followers = [node for node in cluster.nodes.values() if node is not old_leader]
    cluster.fail(followers[0].node_id)
    cluster.fail(followers[1].node_id)
    with pytest.raises(NoQuorum):
        cluster.append("uncommitted")
    # 旧 leader 的未提交条目只存在于自己；新任期 leader 的日志与之冲突。
    cluster.fail(old_leader.node_id)
    cluster.recover(followers[0].node_id)
    cluster.recover(followers[1].node_id)
    new_leader = cluster.elect()
    assert new_leader is not None
    cluster.append("new-committed")
    cluster.recover(old_leader.node_id)
    cluster.sync()
    assert all(
        entry.command != "uncommitted"
        for node in cluster.nodes.values()
        for entry in node.log
    )
    assert cluster.applied == ["new-committed"]


def test_single_node_cluster():
    cluster = RaftCluster(["solo"])
    leader = cluster.elect()
    assert leader is not None
    assert cluster.append("x") == 1
    assert cluster.majority() == 1
