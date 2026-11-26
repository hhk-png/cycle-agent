"""虚拟主机：拓扑、绑定、路由、策略和死信。

VHost 是 broker 内部的一致性与路由边界。一个连接只操作一个 vhost；不同
vhost 之间没有隐式共享（默认交换机的隐式绑定也仅限本 vhost）。
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    AccessRefused,
    AlreadyExists,
    InvalidArgument,
    NotFound,
    PreconditionFailed,
    PublishRejected,
)
from .exchange import Exchange, ExchangeTypeRegistry, default_registry
from .message import Message
from .policy import PolicySet
from .queue import Queue


LOG = logging.getLogger(__name__)
MAX_DEATH_HOPS = 32


@dataclass
class PublishResult:
    routed: int = 0
    queues: list[str] = field(default_factory=list)
    returned: list[Message] = field(default_factory=list)


class VHost:
    def __init__(
        self,
        broker: Any,
        name: str,
        *,
        exchange_registry: ExchangeTypeRegistry | None = None,
    ) -> None:
        self.broker = broker
        self.name = name
        self._lock = threading.RLock()
        self.registry = exchange_registry or default_registry()
        self.default_exchange = Exchange("", durable=True, arguments={})
        self.exchanges: dict[str, Exchange] = {"": self.default_exchange}
        self.queues: dict[str, Queue] = {}
        self.policies = PolicySet()
        self._exchange_base_args: dict[str, dict[str, Any]] = {"": {}}

    # ---- 交换机 -------------------------------------------------------
    def get_exchange(self, name: str) -> Exchange:
        exchange = self.exchanges.get(name)
        if exchange is None:
            raise NotFound(f"no exchange '{name}' in vhost '{self.name}'")
        return exchange

    def declare_exchange(
        self,
        name: str,
        exchange_type: str,
        *,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
        passive: bool = False,
    ) -> Exchange:
        with self._lock:
            if not name:
                raise AccessRefused("the default exchange cannot be declared")
            if name.startswith("amq.") and name not in self.exchanges:
                raise AccessRefused("exchange names beginning with 'amq.' are reserved")
            existing = self.exchanges.get(name)
            if existing is not None:
                if passive:
                    return existing
                if (
                    existing.type_name != exchange_type
                    or existing.durable != durable
                    or existing.auto_delete != auto_delete
                    or existing.internal != internal
                ):
                    raise PreconditionFailed(
                        f"exchange '{name}' exists with different type/properties"
                    )
                return existing
            if passive:
                raise NotFound(f"no exchange '{name}' in vhost '{self.name}'")
            if durable and auto_delete:
                raise InvalidArgument("durable exchange cannot be auto-delete")
            base_args = dict(arguments or {})
            effective_args = dict(base_args)
            effective_args.update(self.policies.exchange_arguments(name))
            exchange = self.registry.create(
                exchange_type,
                name,
                durable=durable,
                auto_delete=auto_delete,
                internal=internal,
                arguments=effective_args,
            )
            self.exchanges[name] = exchange
            self._exchange_base_args[name] = base_args
            self.broker.note_state_change()
            return exchange

    def delete_exchange(self, name: str, *, if_unused: bool = False) -> None:
        with self._lock:
            if name == "":
                raise AccessRefused("the default exchange cannot be deleted")
            exchange = self.exchanges.get(name)
            if exchange is None:
                raise NotFound(f"no exchange '{name}'")
            # 计数“作为源”和“作为目标”的绑定，if-unused 的语义更接近运维预期。
            inbound = sum(
                1
                for source in self.exchanges.values()
                for binding in source.bindings
                if binding.destination_type == "exchange" and binding.destination == name
            )
            if if_unused and (exchange.bindings or inbound):
                raise PreconditionFailed(f"exchange '{name}' is in use")
            self.exchanges.pop(name, None)
            self._exchange_base_args.pop(name, None)
            for source in self.exchanges.values():
                source.remove_destination(name, "exchange")
            self.broker.note_state_change()

    # ---- 队列 ---------------------------------------------------------
    def _generate_queue_name(self) -> str:
        return "amq.gen-" + base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")

    def get_queue(self, name: str) -> Queue:
        queue = self.queues.get(name)
        if queue is None:
            raise NotFound(f"no queue '{name}' in vhost '{self.name}'")
        return queue

    def declare_queue(
        self,
        name: str,
        *,
        durable: bool = False,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
        owner: Any = None,
        declaring_user: str | None = None,
        passive: bool = False,
    ) -> Queue:
        with self._lock:
            generated = False
            if not name:
                if passive:
                    raise NotFound("passive declare requires a queue name")
                name = self._generate_queue_name()
                generated = True
            elif name.startswith("amq.") and name not in self.queues:
                raise AccessRefused("queue names beginning with 'amq.' are reserved")
            existing = self.queues.get(name)
            if existing is not None:
                if passive:
                    return existing
                if exclusive and existing.owner is not None and existing.owner is not owner:
                    from .errors import ResourceLocked

                    raise ResourceLocked(f"queue '{name}' is exclusive")
                if (
                    existing.durable != durable
                    or existing.exclusive != exclusive
                    or existing.auto_delete != auto_delete
                    or existing.base_arguments != dict(arguments or {})
                ):
                    raise PreconditionFailed(
                        f"queue '{name}' exists with different properties or arguments"
                    )
                return existing
            if passive:
                raise NotFound(f"no queue '{name}' in vhost '{self.name}'")
            if exclusive and durable:
                raise InvalidArgument("exclusive queues must not be durable")
            base_args = dict(arguments or {})
            effective_args = dict(base_args)
            effective_args.update(self.policies.queue_arguments(name))
            queue = Queue(
                name,
                durable=durable,
                exclusive=exclusive,
                auto_delete=auto_delete,
                arguments=effective_args,
                owner=owner,
                broker=self.broker,
                vhost=self.name,
                declaring_user=declaring_user,
            )
            self.queues[name] = queue
            if not generated:
                self.broker.note_state_change()
            else:
                self.broker.note_state_change()
            return queue

    def delete_queue(
        self,
        name: str,
        *,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        with self._lock:
            queue = self.queues.get(name)
            if queue is None:
                raise NotFound(f"no queue '{name}'")
            if if_unused and queue.consumer_count:
                raise PreconditionFailed(f"queue '{name}' has consumers")
            if if_empty and not queue.is_empty():
                raise PreconditionFailed(f"queue '{name}' is not empty")
            self.queues.pop(name, None)
            discarded = queue.drop_all()
            for exchange in self.exchanges.values():
                exchange.remove_destination(name, "queue")
            self.broker.note_state_change()
        self.broker._cleanup_auto_delete_exchanges(self)
        return discarded

    # ---- 绑定 ---------------------------------------------------------
    def bind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            queue = self.get_queue(queue_name)
            if exchange_name == "":
                raise AccessRefused("cannot bind to the default exchange")
            exchange = self.get_exchange(exchange_name)
            created = exchange.add_binding(
                queue.name,
                "queue",
                routing_key,
                dict(arguments or {}),
            )
            if created:
                self.broker.note_state_change()
            return created

    def unbind_queue(
        self,
        queue_name: str,
        exchange_name: str,
        routing_key: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self.get_queue(queue_name)
            exchange = self.get_exchange(exchange_name)
            removed = exchange.remove_binding(
                queue_name,
                "queue",
                routing_key,
                dict(arguments) if arguments is not None else None,
            )
            if not removed:
                raise NotFound("binding not found")
            self.broker.note_state_change()
        self.broker._cleanup_auto_delete_exchanges(self)

    def bind_exchange(
        self,
        source_name: str,
        destination_name: str,
        routing_key: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if source_name == "" or destination_name == "":
                raise AccessRefused("default exchange cannot participate in E2E binding")
            source = self.get_exchange(source_name)
            self.get_exchange(destination_name)
            created = source.add_binding(
                destination_name,
                "exchange",
                routing_key,
                dict(arguments or {}),
            )
            if created:
                self.broker.note_state_change()
            return created

    def unbind_exchange(
        self,
        source_name: str,
        destination_name: str,
        routing_key: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            source = self.get_exchange(source_name)
            self.get_exchange(destination_name)
            removed = source.remove_binding(
                destination_name,
                "exchange",
                routing_key,
                dict(arguments) if arguments is not None else None,
            )
            if not removed:
                raise NotFound("binding not found")
            self.broker.note_state_change()
        self.broker._cleanup_auto_delete_exchanges(self)

    def bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for exchange in self.exchanges.values():
                for binding in exchange.bindings:
                    result.append(
                        {
                            "source": exchange.name,
                            "vhost": self.name,
                            "destination": binding.destination,
                            "destination_type": binding.destination_type,
                            "routing_key": binding.routing_key,
                            "arguments": dict(binding.arguments),
                        }
                    )
            return result

    # ---- 路由 ---------------------------------------------------------
    def publish(self, message: Message, *, mandatory: bool = False) -> PublishResult:
        with self._lock:
            if message.exchange == "":
                queue = self.queues.get(message.routing_key)
                if queue is None:
                    return self._maybe_return(message, mandatory)
                try:
                    queue.enqueue(message)
                except PublishRejected:
                    raise
                result = PublishResult(routed=1, queues=[queue.name])
                self.broker.note_state_change(persistent=message.persistent)
                queue.dispatch()
                return result
            exchange = self.get_exchange(message.exchange)
            if exchange.internal:
                raise AccessRefused(f"exchange '{exchange.name}' is internal")
            destinations: list[str] = []
            visited: set[str] = set()
            routed = self._route_exchange(
                exchange,
                message.routing_key,
                message,
                visited,
                destinations,
                depth=0,
            )
            queues = [name for name in destinations if name in self.queues]
            for queue_name in queues:
                try:
                    self.queues[queue_name].enqueue(message)
                except PublishRejected:
                    raise
            if routed == 0 or not queues:
                alternate = exchange.arguments.get("alternate-exchange")
                if alternate:
                    try:
                        alternate_exchange = self.get_exchange(str(alternate))
                    except NotFound:
                        alternate_exchange = None
                    if alternate_exchange is not None:
                        alt_destinations: list[str] = []
                        self._route_exchange(
                            alternate_exchange,
                            message.routing_key,
                            message,
                            {exchange.name},
                            alt_destinations,
                            depth=1,
                        )
                        queues = [name for name in alt_destinations if name in self.queues]
                        for queue_name in queues:
                            self.queues[queue_name].enqueue(message)
            if not queues:
                result = self._maybe_return(message, mandatory)
                self.broker.note_state_change(persistent=message.persistent)
                return result
            self.broker.note_state_change(persistent=message.persistent)
            for queue_name in queues:
                self.queues[queue_name].dispatch()
            return PublishResult(routed=len(queues), queues=queues)

    def _maybe_return(self, message: Message, mandatory: bool) -> PublishResult:
        self.broker.metrics.inc("messages_unrouted_total")
        if mandatory:
            self.broker.metrics.inc("messages_returned_total")
            return PublishResult(routed=0, returned=[message])
        return PublishResult(routed=0)

    def _route_exchange(
        self,
        exchange: Exchange,
        routing_key: str,
        message: Message,
        visited: set[str],
        destinations: list[str],
        *,
        depth: int,
    ) -> int:
        if depth > 32:
            raise PreconditionFailed("exchange-to-exchange routing exceeds 32 hops")
        if exchange.name in visited:
            return 0
        visited.add(exchange.name)
        bindings = exchange.route(routing_key, message.headers)
        if not bindings:
            alternate = exchange.arguments.get("alternate-exchange")
            if alternate and str(alternate) not in visited:
                try:
                    alt_exchange = self.get_exchange(str(alternate))
                except NotFound:
                    return 0
                return self._route_exchange(
                    alt_exchange,
                    routing_key,
                    message,
                    visited,
                    destinations,
                    depth=depth + 1,
                )
            return 0
        matched = 0
        for binding in bindings:
            if binding.destination_type == "queue":
                if binding.destination in self.queues:
                    if binding.destination not in destinations:
                        destinations.append(binding.destination)
                    matched += 1
            else:
                try:
                    target = self.get_exchange(binding.destination)
                except NotFound:
                    continue
                matched += self._route_exchange(
                    target,
                    routing_key,
                    message,
                    visited,
                    destinations,
                    depth=depth + 1,
                )
        return matched

    # ---- 死信 ---------------------------------------------------------
    def dead_letter(self, queue: Queue, message: Message, reason: str) -> None:
        if not queue.dlx:
            return
        if message.death_hops() >= MAX_DEATH_HOPS:
            LOG.warning("dropping message %s after %d dead-letter hops", message.message_id, MAX_DEATH_HOPS)
            return
        dead = message.add_death(
            queue=queue.name,
            reason=reason,
            exchange=message.exchange,
            routing_keys=[message.routing_key],
        )
        routing_key = (
            str(queue.dlx_routing_key)
            if queue.dlx_routing_key is not None
            else dead.routing_key
        )
        dead.exchange = str(queue.dlx)
        dead.routing_key = routing_key
        dead.redelivered = False
        # 避免继承原 TTL 的“过期瞬间”状态；DLX 中的消息重新计时。
        dead.properties.pop("expiration", None)
        try:
            self.publish(dead, mandatory=False)
        except (NotFound, PublishRejected, AccessRefused):
            LOG.warning(
                "dead-letter target %s/%s unavailable; message %s dropped",
                queue.dlx,
                routing_key,
                message.message_id,
            )
        except Exception:
            LOG.exception("dead-letter routing failed for message %s", message.message_id)

    # ---- 策略 ---------------------------------------------------------
    def set_policy(
        self,
        name: str,
        pattern: str,
        definition: dict[str, Any],
        *,
        apply_to: str = "all",
        priority: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            previous = self.policies.get(name)
            previous_defs = {
                queue_name: dict(queue.arguments)
                for queue_name, queue in self.queues.items()
            }
            policy = self.policies.set(
                name,
                pattern,
                definition,
                apply_to=apply_to,
                priority=priority,
            )
            try:
                for queue in self.queues.values():
                    if policy.matches(queue.name, "queues") or (
                        previous and previous.matches(queue.name, "queues")
                    ):
                        new_args = dict(queue.base_arguments)
                        new_args.update(self.policies.queue_arguments(queue.name))
                        queue.reconfigure(new_args)
            except Exception:
                self.policies.delete(name) if previous is None else self.policies.set(
                    previous.name,
                    previous.pattern,
                    previous.definition,
                    apply_to=previous.apply_to,
                    priority=previous.priority,
                )
                for queue_name, old_args in previous_defs.items():
                    queue = self.queues.get(queue_name)
                    if queue is not None:
                        queue.reconfigure(old_args)
                raise
            self.broker.note_state_change()
            return policy.to_dict()

    def delete_policy(self, name: str) -> None:
        with self._lock:
            policy = self.policies.get(name)
            if policy is None:
                raise NotFound(f"no policy '{name}'")
            self.policies.delete(name)
            for queue in self.queues.values():
                if policy.matches(queue.name, "queues"):
                    new_args = dict(queue.base_arguments)
                    new_args.update(self.policies.queue_arguments(queue.name))
                    queue.reconfigure(new_args)
            self.broker.note_state_change()

    # ---- 维护 ---------------------------------------------------------
    def expire(self, now: float | None = None) -> list[str]:
        deleted: list[str] = []
        with self._lock:
            queues = list(self.queues.values())
        for queue in queues:
            queue.expire_ready()
            if queue.expired_by_inactivity(now):
                try:
                    self.delete_queue(queue.name)
                    deleted.append(queue.name)
                except NotFound:
                    pass
        return deleted

    def dispatch_all(self) -> None:
        with self._lock:
            queues = list(self.queues.values())
        for queue in queues:
            queue.dispatch()

    # ---- 统计 ---------------------------------------------------------
    def totals(self) -> dict[str, int]:
        with self._lock:
            queues = list(self.queues.values())
            return {
                "queues": len(queues),
                "exchanges": len(self.exchanges),
                "consumers": sum(q.consumer_count for q in queues),
                "messages_ready": sum(q.messages_ready for q in queues),
                "messages_unacknowledged": sum(q.messages_unacknowledged for q in queues),
                "messages": sum(q.messages_ready + q.messages_unacknowledged for q in queues),
            }

    # ---- 快照 ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            exchanges = []
            for exchange in self.exchanges.values():
                if exchange.name == "" or not exchange.durable:
                    continue
                exchanges.append(
                    {
                        **exchange.to_dict(),
                        "base_arguments": dict(self._exchange_base_args.get(exchange.name, {})),
                        "bindings": [
                            {
                                "destination": b.destination,
                                "destination_type": b.destination_type,
                                "routing_key": b.routing_key,
                                "arguments": b.arguments,
                            }
                            for b in exchange.bindings
                            if (
                                (b.destination_type == "queue" and self.queues.get(b.destination, None) is not None and self.queues[b.destination].durable)
                                or (
                                    b.destination_type == "exchange"
                                    and self.exchanges.get(b.destination) is not None
                                    and self.exchanges[b.destination].durable
                                )
                            )
                        ],
                    }
                )
            queues = [
                queue.snapshot(persistent_only=True)
                for queue in self.queues.values()
                if queue.durable
            ]
            return {
                "name": self.name,
                "exchanges": exchanges,
                "queues": queues,
                "policies": self.policies.to_list(),
            }

    def restore(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.queues.clear()
            self.exchanges = {"": self.default_exchange}
            self._exchange_base_args = {"": {}}
            self.policies = PolicySet()
            for item in data.get("policies", []):
                self.policies.set(
                    item["name"],
                    item["pattern"],
                    item.get("definition") or {},
                    apply_to=item.get("apply-to", "all"),
                    priority=int(item.get("priority", 0)),
                )
            for item in data.get("exchanges", []):
                exchange = self.registry.create(
                    item.get("type", "direct"),
                    item["name"],
                    durable=bool(item.get("durable", True)),
                    auto_delete=bool(item.get("auto_delete", False)),
                    internal=bool(item.get("internal", False)),
                    arguments=item.get("arguments") or {},
                )
                self.exchanges[exchange.name] = exchange
                self._exchange_base_args[exchange.name] = dict(
                    item.get("base_arguments") or item.get("arguments") or {}
                )
            for item in data.get("queues", []):
                base_arguments = item.get("arguments") or {}
                effective_arguments = dict(base_arguments)
                effective_arguments.update(self.policies.queue_arguments(item["name"]))
                queue = Queue(
                    item["name"],
                    durable=bool(item.get("durable", True)),
                    exclusive=False,
                    auto_delete=bool(item.get("auto_delete", False)),
                    arguments=effective_arguments,
                    owner=None,
                    broker=self.broker,
                    vhost=self.name,
                    declaring_user=item.get("declaring_user"),
                )
                queue.base_arguments = dict(base_arguments)
                queue.restore(item)
                self.queues[queue.name] = queue
            for item in data.get("exchanges", []):
                source = self.exchanges.get(item["name"])
                if source is None:
                    continue
                for binding in item.get("bindings", []):
                    source.add_binding(
                        binding["destination"],
                        binding.get("destination_type", "queue"),
                        binding.get("routing_key", ""),
                        binding.get("arguments") or {},
                    )
