"""交换机与路由算法。

支持 direct、fanout、topic、headers 四种内置类型，并允许注册插件型
交换机。Exchange 只负责“匹配哪些绑定”，真正的入队由 VHost 完成，这样
交换机到交换机（E2E）绑定与环路检测可以集中处理。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Binding:
    """一条绑定。

    ``destination_type`` 为 ``queue`` 或 ``exchange``。绑定参数用于 headers
    交换机、优先级等扩展。
    """

    source: str
    destination: str
    destination_type: str = "queue"
    routing_key: str = ""
    arguments: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


def topic_matches(pattern: str, routing_key: str) -> bool:
    """AMQP topic 匹配：``*`` 匹配一个词，``#`` 匹配零个或多个词。"""

    p = pattern.split(".") if pattern else []
    k = routing_key.split(".") if routing_key else []

    memo: dict[tuple[int, int], bool] = {}

    def match(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(p):
            result = j == len(k)
        elif p[i] == "#":
            result = match(i + 1, j) or (j < len(k) and match(i, j + 1))
        elif j == len(k):
            result = False
        elif p[i] == "*" or p[i] == k[j]:
            result = match(i + 1, j + 1)
        else:
            result = False
        memo[key] = result
        return result

    return match(0, 0)


class Exchange:
    """交换机基类。"""

    type_name = "direct"
    plugin = False

    def __init__(
        self,
        name: str,
        *,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.durable = bool(durable)
        self.auto_delete = bool(auto_delete)
        self.internal = bool(internal)
        self.arguments = dict(arguments or {})
        self.bindings: list[Binding] = []

    # ---- 绑定维护 -----------------------------------------------------
    def add_binding(
        self,
        destination: str,
        destination_type: str,
        routing_key: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        arguments = dict(arguments or {})
        for binding in self.bindings:
            if (
                binding.destination == destination
                and binding.destination_type == destination_type
                and binding.routing_key == routing_key
                and binding.arguments == arguments
            ):
                return False
        self.bindings.append(
            Binding(
                source=self.name,
                destination=destination,
                destination_type=destination_type,
                routing_key=routing_key,
                arguments=arguments,
            )
        )
        return True

    def remove_binding(
        self,
        destination: str,
        destination_type: str | None = None,
        routing_key: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> int:
        before = len(self.bindings)
        self.bindings = [
            binding
            for binding in self.bindings
            if not (
                binding.destination == destination
                and (destination_type is None or binding.destination_type == destination_type)
                and (routing_key is None or binding.routing_key == routing_key)
                and (arguments is None or binding.arguments == arguments)
            )
        ]
        return before - len(self.bindings)

    def remove_destination(self, destination: str, destination_type: str | None = None) -> int:
        return self.remove_binding(destination, destination_type)

    # ---- 路由 ---------------------------------------------------------
    def route(self, routing_key: str, headers: dict[str, Any] | None = None) -> list[Binding]:
        return [
            binding
            for binding in self.bindings
            if self.binding_matches(binding, routing_key, headers or {})
        ]

    def binding_matches(
        self, binding: Binding, routing_key: str, headers: dict[str, Any]
    ) -> bool:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "durable": self.durable,
            "auto_delete": self.auto_delete,
            "internal": self.internal,
            "arguments": self.arguments,
        }


class DirectExchange(Exchange):
    type_name = "direct"

    def binding_matches(self, binding: Binding, routing_key: str, headers: dict[str, Any]) -> bool:
        return binding.routing_key == routing_key


class FanoutExchange(Exchange):
    type_name = "fanout"

    def binding_matches(self, binding: Binding, routing_key: str, headers: dict[str, Any]) -> bool:
        return True


class TopicExchange(Exchange):
    type_name = "topic"

    def binding_matches(self, binding: Binding, routing_key: str, headers: dict[str, Any]) -> bool:
        return topic_matches(binding.routing_key, routing_key)


class HeadersExchange(Exchange):
    type_name = "headers"

    def binding_matches(self, binding: Binding, routing_key: str, headers: dict[str, Any]) -> bool:
        criteria = {
            key: value
            for key, value in binding.arguments.items()
            if not str(key).startswith("x-")
        }
        if not criteria:
            return False
        mode = str(binding.arguments.get("x-match", "all")).lower()
        matched = [headers.get(key) == value for key, value in criteria.items()]
        if mode == "any":
            return any(matched)
        # all / all-with-x：mini 把二者都实现为“全部普通键匹配”。
        return all(matched)


class ConsistentHashExchange(Exchange):
    """一致性哈希交换机（对应 rabbitmq_consistent_hash_exchange 插件）。

    ``x-consistent-hash`` 绑定键是权重（例如 "50"）。为保持可测试性，这里
    用排序后的虚拟节点环；默认 100 个虚拟节点/权重单位。
    """

    type_name = "x-consistent-hash"
    plugin = True

    def route(self, routing_key: str, headers: dict[str, Any] | None = None) -> list[Binding]:
        headers = headers or {}
        if not self.bindings:
            return []
        hash_on = str(self.arguments.get("hash-header") or "")
        if hash_on:
            value = str(headers.get(hash_on, ""))
        else:
            value = routing_key
        ring: list[tuple[int, Binding]] = []
        for binding in self.bindings:
            try:
                weight = max(1, int(binding.routing_key or "1"))
            except ValueError:
                weight = 1
            for i in range(weight * 100):
                digest = hashlib.sha1(
                    f"{binding.destination}:{i}".encode("utf-8")
                ).hexdigest()
                ring.append((int(digest[:8], 16), binding))
        ring.sort(key=lambda item: item[0])
        point = int(hashlib.sha1(value.encode("utf-8")).hexdigest()[:8], 16)
        for position, binding in ring:
            if position >= point:
                return [binding]
        return [ring[0][1]]

    def binding_matches(self, binding: Binding, routing_key: str, headers: dict[str, Any]) -> bool:
        # route() 已覆盖此交换机的选择逻辑。
        return False


class ExchangeTypeRegistry:
    """交换机类型注册表，模拟 RabbitMQ 的插件扩展点。"""

    def __init__(self) -> None:
        self._types: dict[str, type[Exchange]] = {}

    def register(self, exchange_class: type[Exchange]) -> None:
        self._types[exchange_class.type_name] = exchange_class

    def create(
        self,
        exchange_type: str,
        name: str,
        *,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> Exchange:
        if exchange_type not in self._types:
            from .errors import InvalidArgument

            raise InvalidArgument(f"unknown exchange type '{exchange_type}'")
        return self._types[exchange_type](
            name,
            durable=durable,
            auto_delete=auto_delete,
            internal=internal,
            arguments=arguments,
        )

    def names(self) -> list[str]:
        return sorted(self._types)


def default_registry() -> ExchangeTypeRegistry:
    registry = ExchangeTypeRegistry()
    for klass in (DirectExchange, FanoutExchange, TopicExchange, HeadersExchange):
        registry.register(klass)
    registry.register(ConsistentHashExchange)
    return registry
