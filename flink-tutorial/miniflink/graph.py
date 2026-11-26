"""作业图：顶点、边、分区方式与拓扑构建。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .core import new_id


class Partitioning(str, Enum):
    FORWARD = "forward"
    HASH = "hash"
    REBALANCE = "rebalance"
    BROADCAST = "broadcast"


@dataclass(frozen=True)
class Edge:
    source: "Node"
    partitioning: Partitioning = Partitioning.FORWARD
    output_tag: str | None = None
    input_index: int = 0


@dataclass
class Node:
    id: str
    name: str
    operator_factory: Callable[[int, int], Any]
    parallelism: int = 1
    inputs: list[Edge] = field(default_factory=list)
    uid: str | None = None
    is_source: bool = False
    is_broadcast: bool = False

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Node) and other.id == self.id

    def describe(self) -> str:
        ins = ", ".join(f"{e.source.name}:{e.partitioning.value}" for e in self.inputs)
        return f"{self.name}[{self.parallelism}] <- ({ins or 'source'})"


@dataclass
class JobGraph:
    nodes: list[Node]
    sinks: list[Node]

    def sources(self) -> list[Node]:
        return [node for node in self.nodes if not node.inputs]

    def topological_order(self) -> list[Node]:
        visited: set[str] = set()
        visiting: set[str] = set()
        order: list[Node] = []

        def visit(node: Node) -> None:
            if node.id in visited:
                return
            if node.id in visiting:
                raise ValueError("作业图存在环")
            visiting.add(node.id)
            for edge in node.inputs:
                visit(edge.source)
            visiting.remove(node.id)
            visited.add(node.id)
            order.append(node)

        for sink in self.sinks:
            visit(sink)
        return order

    def validate(self) -> list[str]:
        errors: list[str] = []
        names: dict[str, str] = {}
        for node in self.nodes:
            if node.uid:
                if node.uid in names:
                    errors.append(f"重复 uid: {node.uid}")
                names[node.uid] = node.id
            if node.parallelism <= 0:
                errors.append(f"{node.name} 的并行度必须大于 0")
            for edge in node.inputs:
                if edge.partitioning is Partitioning.HASH and edge.output_tag is None:
                    if edge.source.parallelism <= 0:
                        errors.append(f"{edge.source.name} 并行度非法")
        return errors

    def describe(self) -> str:
        lines = ["JobGraph:"]
        for node in self.topological_order():
            lines.append("  " + node.describe())
        return "\n".join(lines)

    def to_dot(self) -> str:
        lines = ["digraph flink_job {", "  rankdir=LR;"]
        for node in self.nodes:
            label = f"{node.name}\\nparallelism={node.parallelism}"
            lines.append(f'  "{node.id}" [label="{label}", shape=box];')
        for node in self.nodes:
            for edge in node.inputs:
                style = "dashed" if edge.output_tag else "solid"
                label = edge.partitioning.value
                if edge.output_tag:
                    label += f"\\nside:{edge.output_tag}"
                lines.append(
                    f'  "{edge.source.id}" -> "{node.id}" [label="{label}", style="{style}"];'
                )
        lines.append("}")
        return "\n".join(lines)


def build_graph(terminals: list[Any]) -> JobGraph:
    """从终止节点反向收集算子，并按拓扑顺序去重。"""

    seen: dict[str, Node] = {}

    def collect(node: Node) -> None:
        if node.id in seen:
            return
        seen[node.id] = node
        for edge in node.inputs:
            collect(edge.source)

    for terminal in terminals:
        collect(terminal)
    graph = JobGraph(list(seen.values()), list(terminals))
    errors = graph.validate()
    if errors:
        raise ValueError("; ".join(errors))
    return graph


def make_node(
    name: str,
    factory: Callable[[int, int], Any],
    *,
    parallelism: int,
    inputs: list[Edge] | None = None,
    uid: str | None = None,
    is_source: bool = False,
) -> Node:
    return Node(
        id=new_id("node"),
        name=name,
        operator_factory=factory,
        parallelism=parallelism,
        inputs=inputs or [],
        uid=uid,
        is_source=is_source,
    )
