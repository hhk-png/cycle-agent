"""Quorum Queue 的 Raft 教学模型。

真实 RabbitMQ quorum queue 的复制由 Raft 实现，包含持久化日志、快照、
成员变更和网络 RPC。本模块在单进程内模拟投票、日志一致性、多数派提交和
领导者故障转移，用于解释语义，不替代生产 Raft 实现。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .errors import NoQuorum


@dataclass
class LogEntry:
    index: int
    term: int
    command: Any


@dataclass
class RaftNode:
    node_id: str
    alive: bool = True
    current_term: int = 0
    voted_for: str | None = None
    log: list[LogEntry] = field(default_factory=lambda: [LogEntry(0, 0, None)])
    commit_index: int = 0
    last_applied: int = 0
    state: str = "follower"
    leader_id: str | None = None

    @property
    def last_log_index(self) -> int:
        return len(self.log) - 1

    @property
    def last_log_term(self) -> int:
        return self.log[-1].term


@dataclass
class VoteResult:
    term: int
    granted: bool


@dataclass
class AppendResult:
    term: int
    success: bool
    match_index: int = 0
    conflict_index: int = 0


class RaftCluster:
    def __init__(self, node_ids: list[str]) -> None:
        if not node_ids:
            raise ValueError("at least one node is required")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node ids must be unique")
        self.nodes = {node_id: RaftNode(node_id) for node_id in node_ids}
        self._lock = threading.RLock()
        self._next_index: dict[str, dict[str, int]] = {}
        self._match_index: dict[str, dict[str, int]] = {}
        self.applied: list[Any] = []
        self.committed_at: dict[int, Any] = {}

    # ---- 查询 ---------------------------------------------------------
    def leader(self) -> RaftNode | None:
        with self._lock:
            for node in self.nodes.values():
                if node.alive and node.state == "leader":
                    return node
            return None

    @property
    def leader_id(self) -> str | None:
        leader = self.leader()
        return leader.node_id if leader else None

    def majority(self) -> int:
        return len(self.nodes) // 2 + 1

    def alive_ids(self) -> list[str]:
        return [node_id for node_id, node in self.nodes.items() if node.alive]

    # ---- 选举 ---------------------------------------------------------
    def elect(self, preferred: str | None = None) -> RaftNode | None:
        with self._lock:
            alive = [node for node in self.nodes.values() if node.alive]
            if not alive:
                return None
            if preferred and preferred in self.nodes and self.nodes[preferred].alive:
                candidate = self.nodes[preferred]
            else:
                candidate = sorted(
                    alive,
                    key=lambda node: (node.last_log_term, node.last_log_index, node.node_id),
                )[-1]
            for node in alive:
                node.state = "follower" if node is not candidate else node.state
                node.leader_id = None
            candidate.state = "candidate"
            candidate.current_term += 1
            candidate.voted_for = candidate.node_id
            term = candidate.current_term
            votes = {candidate.node_id}
            last_index = candidate.last_log_index
            last_term = candidate.last_log_term
            for peer in alive:
                if peer is candidate:
                    continue
                result = self._request_vote(peer, candidate.node_id, term, last_index, last_term)
                if result.granted and result.term == term:
                    votes.add(peer.node_id)
            if len(votes) >= self.majority():
                candidate.state = "leader"
                candidate.leader_id = candidate.node_id
                candidate.commit_index = candidate.commit_index
                self._next_index[candidate.node_id] = {
                    peer_id: len(candidate.log) for peer_id in self.nodes
                }
                self._match_index[candidate.node_id] = {
                    peer_id: 0 for peer_id in self.nodes
                }
                self._match_index[candidate.node_id][candidate.node_id] = candidate.last_log_index
                for peer in alive:
                    if peer is candidate:
                        continue
                    self._replicate_to(candidate, peer)
                return candidate
            candidate.state = "follower"
            candidate.voted_for = None
            return None

    def _request_vote(
        self,
        voter: RaftNode,
        candidate_id: str,
        term: int,
        last_index: int,
        last_term: int,
    ) -> VoteResult:
        if not voter.alive:
            return VoteResult(voter.current_term, False)
        if term < voter.current_term:
            return VoteResult(voter.current_term, False)
        if term > voter.current_term:
            voter.current_term = term
            voter.voted_for = None
            voter.state = "follower"
            voter.leader_id = None
        up_to_date = (last_term > voter.last_log_term) or (
            last_term == voter.last_log_term and last_index >= voter.last_log_index
        )
        if (voter.voted_for is None or voter.voted_for == candidate_id) and up_to_date:
            voter.voted_for = candidate_id
            return VoteResult(term, True)
        return VoteResult(voter.current_term, False)

    # ---- 日志复制 -----------------------------------------------------
    def append(self, command: Any) -> int:
        with self._lock:
            leader = self.leader()
            if leader is None:
                raise NoQuorum("no leader available")
            entry = LogEntry(
                index=leader.last_log_index + 1,
                term=leader.current_term,
                command=command,
            )
            leader.log.append(entry)
            next_index = self._next_index.setdefault(
                leader.node_id, {peer_id: len(leader.log) - 1 for peer_id in self.nodes}
            )
            match_index = self._match_index.setdefault(
                leader.node_id, {peer_id: 0 for peer_id in self.nodes}
            )
            next_index[leader.node_id] = len(leader.log)
            match_index[leader.node_id] = entry.index
            acks = 1
            for peer in self.nodes.values():
                if peer is leader or not peer.alive:
                    continue
                if self._replicate_to(leader, peer):
                    acks += 1
            if acks < self.majority():
                raise NoQuorum(
                    f"entry {entry.index} was not acknowledged by a majority "
                    f"({acks}/{len(self.nodes)}); leader remains but does not commit"
                )
            leader.commit_index = entry.index
            self._apply(leader)
            for peer in self.nodes.values():
                if peer is leader or not peer.alive:
                    continue
                self._send_append(leader, peer)
            return entry.index

    def _replicate_to(self, leader: RaftNode, follower: RaftNode) -> bool:
        if not follower.alive:
            return False
        next_index = self._next_index.setdefault(
            leader.node_id, {peer_id: len(leader.log) for peer_id in self.nodes}
        )
        match_index = self._match_index.setdefault(
            leader.node_id, {peer_id: 0 for peer_id in self.nodes}
        )
        attempts = len(leader.log) + 2
        while attempts > 0:
            attempts -= 1
            next_pos = max(1, next_index.get(follower.node_id, len(leader.log)))
            prev_index = next_pos - 1
            prev_term = leader.log[prev_index].term if prev_index < len(leader.log) else 0
            entries = leader.log[next_pos:]
            result = self._append_entries(
                follower,
                leader.node_id,
                leader.current_term,
                prev_index,
                prev_term,
                entries,
                leader.commit_index,
            )
            if result.success:
                match_index[follower.node_id] = result.match_index
                next_index[follower.node_id] = result.match_index + 1
                return True
            next_index[follower.node_id] = max(1, min(next_pos - 1, result.conflict_index or 1))
        return False

    def _send_append(self, leader: RaftNode, follower: RaftNode) -> bool:
        return self._replicate_to(leader, follower)

    def sync(self) -> None:
        """把 leader 的提交位置和缺失日志同步到所有在线 follower。"""

        with self._lock:
            leader = self.leader()
            if leader is None:
                return
            for peer in self.nodes.values():
                if peer is not leader and peer.alive:
                    self._replicate_to(leader, peer)

    def _append_entries(
        self,
        follower: RaftNode,
        leader_id: str,
        term: int,
        prev_index: int,
        prev_term: int,
        entries: list[LogEntry],
        leader_commit: int,
    ) -> AppendResult:
        if not follower.alive:
            return AppendResult(follower.current_term, False)
        if term < follower.current_term:
            return AppendResult(follower.current_term, False)
        if term > follower.current_term:
            follower.current_term = term
            follower.voted_for = None
        follower.state = "follower"
        follower.leader_id = leader_id
        if prev_index > follower.last_log_index:
            return AppendResult(term, False, conflict_index=follower.last_log_index + 1)
        if prev_index > 0 and follower.log[prev_index].term != prev_term:
            conflict = prev_index
            while conflict > 1 and follower.log[conflict - 1].term == follower.log[conflict].term:
                conflict -= 1
            return AppendResult(term, False, conflict_index=conflict)
        # 截断冲突后缀，再追加 leader 提供的条目。
        insert_at = prev_index + 1
        for index, entry in enumerate(entries, start=insert_at):
            if index <= follower.last_log_index:
                if follower.log[index].term != entry.term:
                    del follower.log[index:]
                    follower.log.append(LogEntry(entry.index, entry.term, entry.command))
            else:
                follower.log.append(LogEntry(entry.index, entry.term, entry.command))
        follower.commit_index = min(leader_commit, follower.last_log_index)
        return AppendResult(term, True, match_index=follower.last_log_index)

    # ---- 应用状态机与故障 --------------------------------------------
    def _apply(self, node: RaftNode) -> None:
        while node.last_applied < node.commit_index:
            node.last_applied += 1
            entry = node.log[node.last_applied]
            if node.node_id == self.leader_id:
                self.applied.append(entry.command)
                self.committed_at[entry.index] = entry.command

    def _apply_followers(self) -> None:
        leader = self.leader()
        if leader is None:
            return
        for node in self.nodes.values():
            if node is leader or not node.alive:
                continue
            node.commit_index = min(node.commit_index, leader.commit_index)
            node.last_applied = max(node.last_applied, 0)

    def fail(self, node_id: str) -> None:
        with self._lock:
            node = self.nodes[node_id]
            node.alive = False
            node.state = "follower"
            node.leader_id = None

    def recover(self, node_id: str) -> None:
        with self._lock:
            node = self.nodes[node_id]
            node.alive = True
            node.state = "follower"
            node.leader_id = None
            leader = self.leader()
            if leader is not None:
                self._replicate_to(leader, node)

    def committed_index(self) -> int:
        with self._lock:
            leader = self.leader()
            return leader.commit_index if leader else max(
                (node.commit_index for node in self.nodes.values() if node.alive), default=0
            )

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "leader": self.leader_id,
                "term": max(node.current_term for node in self.nodes.values()),
                "committed_index": self.committed_index(),
                "applied": len(self.applied),
                "nodes": [
                    {
                        "id": node.node_id,
                        "alive": node.alive,
                        "state": node.state,
                        "term": node.current_term,
                        "last_index": node.last_log_index,
                        "last_term": node.last_log_term,
                        "commit_index": node.commit_index,
                    }
                    for node in self.nodes.values()
                ],
            }

    def to_dict(self) -> dict[str, Any]:
        return self.state()


class QuorumQueue:
    def __init__(self, name: str, node_ids: list[str] | None = None) -> None:
        self.name = name
        self.cluster = RaftCluster(node_ids or ["node1", "node2", "node3"])
        self.by_offset: dict[int, Any] = {}

    def publish(self, message: Any) -> int:
        offset = self.cluster.append(message)
        self.by_offset[offset] = message
        return offset

    @property
    def leader(self) -> str | None:
        return self.cluster.leader_id

    def messages(self) -> list[Any]:
        return list(self.cluster.applied)

    def failover(self) -> RaftNode | None:
        leader = self.cluster.leader()
        if leader is not None:
            self.cluster.fail(leader.node_id)
        return self.cluster.elect()

    def state(self) -> dict[str, Any]:
        result = self.cluster.state()
        result["queue"] = self.name
        result["messages"] = len(self.cluster.applied)
        return result
