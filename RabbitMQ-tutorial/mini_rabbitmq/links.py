"""Shovel 与 Federation 的简化实现。

- Shovel：从源队列显式拉取，转发到目标交换机/队列，成功后再确认。
- Federation：在源 broker 的发布事件上复制到下游交易所，用 origin 链防止环路。

真实 RabbitMQ 的 federation/shovel 是 Erlang 插件，支持断线重连、TLS、
上游集合和拓扑恢复；这里用于解释“至少一次转发”和环路防护。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .broker import Broker
from .errors import NotFound
from .message import Message
from .vhost import PublishResult


@dataclass
class ShovelStats:
    forwarded: int = 0
    failed: int = 0
    loop_dropped: int = 0


class Shovel:
    def __init__(
        self,
        name: str,
        source_broker: Broker,
        source_queue: str,
        destination_broker: Broker,
        *,
        destination_exchange: str = "",
        destination_routing_key: str | None = None,
        source_vhost: str = "/",
        destination_vhost: str = "/",
        max_hops: int = 16,
    ) -> None:
        self.name = name
        self.source_broker = source_broker
        self.source_queue = source_queue
        self.destination_broker = destination_broker
        self.destination_exchange = destination_exchange
        self.destination_routing_key = destination_routing_key
        self.source_vhost = source_vhost
        self.destination_vhost = destination_vhost
        self.max_hops = max_hops
        self.stats = ShovelStats()

    def run_once(self, max_messages: int = 100) -> int:
        forwarded = 0
        for _ in range(max_messages):
            delivery = self.source_broker.get(
                self.source_queue,
                auto_ack=False,
                owner=self,
                vhost=self.source_vhost,
            )
            if delivery is None:
                break
            message = delivery.message
            origins = list(message.headers.get("x-shovel-origin") or [])
            if len(origins) >= self.max_hops or self.name in origins:
                self.stats.loop_dropped += 1
                self.source_broker.ack(
                    delivery.delivery_tag, owner=self
                )
                continue
            origins.append(self.name)
            forwarded_message = message.clone(
                exchange=self.destination_exchange,
                routing_key=self.destination_routing_key
                if self.destination_routing_key is not None
                else message.routing_key,
            )
            forwarded_message.headers["x-shovel-origin"] = origins
            try:
                result = self.destination_broker.publish_message(
                    forwarded_message,
                    mandatory=True,
                    vhost=self.destination_vhost,
                )
            except Exception:
                self.stats.failed += 1
                self.source_broker.nack(
                    delivery.delivery_tag, requeue=True, owner=self
                )
                break
            if result.returned:
                self.stats.failed += 1
                self.source_broker.nack(
                    delivery.delivery_tag, requeue=True, owner=self
                )
                break
            self.source_broker.ack(delivery.delivery_tag, owner=self)
            self.stats.forwarded += 1
            forwarded += 1
        return forwarded


class FederationLink:
    """发布事件驱动的 federation 教学实现。"""

    def __init__(
        self,
        upstream_name: str,
        source_broker: Broker,
        source_exchange: str,
        destination_broker: Broker,
        destination_exchange: str,
        *,
        routing_key_pattern: str = ".*",
        source_vhost: str = "/",
        destination_vhost: str = "/",
        max_hops: int = 16,
    ) -> None:
        self.upstream_name = upstream_name
        self.source_broker = source_broker
        self.source_exchange = source_exchange
        self.destination_broker = destination_broker
        self.destination_exchange = destination_exchange
        self.routing_key_pattern = re.compile(routing_key_pattern)
        self.source_vhost = source_vhost
        self.destination_vhost = destination_vhost
        self.max_hops = max_hops
        self.forwarded = 0
        self.skipped = 0
        self._active = False

    def start(self) -> None:
        if not self._active:
            self.source_broker.add_publish_listener(self._on_publish)
            self._active = True

    def stop(self) -> None:
        if self._active:
            self.source_broker.remove_publish_listener(self._on_publish)
            self._active = False

    def _on_publish(self, vhost: str, message: Message, result: PublishResult) -> None:
        del result
        if vhost != self.source_vhost or message.exchange != self.source_exchange:
            return
        if self.routing_key_pattern.fullmatch(message.routing_key) is None:
            return
        origins = list(message.headers.get("x-federation-origin") or [])
        if self.upstream_name in origins or len(origins) >= self.max_hops:
            self.skipped += 1
            return
        origins.append(self.upstream_name)
        forwarded = message.clone(
            exchange=self.destination_exchange,
            redelivered=False,
        )
        forwarded.headers["x-federation-origin"] = origins
        try:
            self.destination_broker.publish_message(
                forwarded, mandatory=False, vhost=self.destination_vhost
            )
            self.forwarded += 1
        except (NotFound,) as exc:
            self.skipped += 1
            del exc
