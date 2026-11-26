"""Broker 门面：用户、vhost、投递标签、告警、持久化与维护任务。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .auth import AuthStore, default_auth
from .errors import (
    AccessRefused,
    InvalidArgument,
    NotFound,
    PreconditionFailed,
    PublishRejected,
    ResourceError,
)
from .exchange import ExchangeTypeRegistry, default_registry
from .message import Delivery, Message
from .metrics import Metrics
from .storage import NullStore, SnapshotStore
from .vhost import PublishResult, VHost


LOG = logging.getLogger(__name__)


class Broker:
    def __init__(
        self,
        *,
        data_file: str | Path | None = None,
        auto_checkpoint: bool = True,
        fsync: bool = False,
        auth: AuthStore | None = None,
        node_name: str = "mini@localhost",
        exchange_registry: ExchangeTypeRegistry | None = None,
    ) -> None:
        self.node_name = node_name
        self.metrics = Metrics()
        self.auth = auth or default_auth()
        self.exchange_registry = exchange_registry or default_registry()
        self.vhosts: dict[str, VHost] = {}
        self._lock = threading.RLock()
        self._delivery_seq = 0
        self._deliveries: dict[int, tuple[Any, Delivery]] = {}
        self._consumer_seq = 0
        self._loading = False
        self._alarms: dict[str, str] = {}
        self._alarm_listeners: list[Callable[[str, bool, str], None]] = []
        self._publish_listeners: list[Callable[[str, Message, PublishResult], None]] = []
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._started_at = time.time()
        self.auto_checkpoint = auto_checkpoint
        self.store = SnapshotStore(data_file, fsync=fsync) if data_file else NullStore()
        self._load_snapshot()
        if not self.vhosts:
            self.vhosts["/"] = VHost(self, "/", exchange_registry=self.exchange_registry)
        if not self.auth.users:
            self.auth = default_auth()
        self.refresh_gauges()

    # ---- 生命周期 -----------------------------------------------------
    def _load_snapshot(self) -> None:
        data = self.store.load()
        if not data:
            return
        self._loading = True
        try:
            if "users" in data or "permissions" in data:
                self.auth.load_dict(
                    {
                        "users": data.get("users", []),
                        "permissions": data.get("permissions", {}),
                    }
                )
            for vhost_data in data.get("vhosts", []):
                name = vhost_data.get("name", "/")
                vhost = VHost(self, name, exchange_registry=self.exchange_registry)
                vhost.restore(vhost_data)
                self.vhosts[name] = vhost
        finally:
            self._loading = False

    def close(self) -> None:
        self.stop_maintenance()
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            for queue in list(vhost.queues.values()):
                queue.requeue_all()
        self.refresh_gauges()
        self.flush()

    def flush(self) -> None:
        if self.store is not NullStore:
            self.save()

    # ---- 快照与变更 ---------------------------------------------------
    def snapshot_state(self) -> dict[str, Any]:
        with self._lock:
            vhosts = [vhost.snapshot() for vhost in self.vhosts.values()]
        auth_state = self.auth.to_dict()
        return {
            "format": "mini-rabbitmq-snapshot",
            "version": 1,
            "node": self.node_name,
            "saved_at": time.time(),
            "users": auth_state["users"],
            "permissions": auth_state["permissions"],
            "vhosts": vhosts,
        }

    def save(self) -> None:
        if self._loading:
            return
        self.store.save(self.snapshot_state())

    def note_state_change(self, persistent: bool | None = None) -> None:
        if self._loading or not self.auto_checkpoint:
            return
        if persistent is False:
            return
        try:
            self.save()
        except Exception:
            LOG.exception("snapshot checkpoint failed")

    # ---- vhost 与拓扑 -------------------------------------------------
    def create_vhost(self, name: str) -> VHost:
        with self._lock:
            if name in self.vhosts:
                raise PreconditionFailed(f"vhost '{name}' already exists")
            vhost = VHost(self, name, exchange_registry=self.exchange_registry)
            self.vhosts[name] = vhost
        self.note_state_change()
        self.refresh_gauges()
        return vhost

    def delete_vhost(self, name: str) -> None:
        if name == "/":
            raise PreconditionFailed("the default vhost cannot be deleted")
        with self._lock:
            vhost = self.vhosts.pop(name, None)
        if vhost is None:
            raise NotFound(f"no vhost '{name}'")
        for queue in list(vhost.queues.values()):
            queue.drop_all()
        for user_name, permissions in self.auth.permissions.items():
            permissions.pop(name, None)
        self.note_state_change()

    def vhost(self, name: str = "/") -> VHost:
        try:
            return self.vhosts[name]
        except KeyError as exc:
            raise NotFound(f"no vhost '{name}'") from exc

    def declare_exchange(
        self,
        name: str,
        exchange_type: str = "direct",
        *,
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        arguments: dict[str, Any] | None = None,
        passive: bool = False,
        vhost: str = "/",
    ):
        return self.vhost(vhost).declare_exchange(
            name,
            exchange_type,
            durable=durable,
            auto_delete=auto_delete,
            internal=internal,
            arguments=arguments,
            passive=passive,
        )

    def delete_exchange(self, name: str, *, if_unused: bool = False, vhost: str = "/") -> None:
        self.vhost(vhost).delete_exchange(name, if_unused=if_unused)
        self.refresh_gauges()

    def declare_queue(
        self,
        name: str = "",
        *,
        durable: bool = False,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
        owner: Any = None,
        declaring_user: str | None = None,
        passive: bool = False,
        vhost: str = "/",
    ):
        queue = self.vhost(vhost).declare_queue(
            name,
            durable=durable,
            exclusive=exclusive,
            auto_delete=auto_delete,
            arguments=arguments,
            owner=owner,
            declaring_user=declaring_user,
            passive=passive,
        )
        self.refresh_gauges()
        return queue

    def delete_queue(
        self,
        name: str,
        *,
        if_unused: bool = False,
        if_empty: bool = False,
        vhost: str = "/",
    ) -> int:
        discarded = self.vhost(vhost).delete_queue(
            name, if_unused=if_unused, if_empty=if_empty
        )
        self.refresh_gauges()
        return discarded

    def bind_queue(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        vhost: str = "/",
    ) -> bool:
        return self.vhost(vhost).bind_queue(queue, exchange, routing_key, arguments)

    def unbind_queue(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        vhost: str = "/",
    ) -> None:
        self.vhost(vhost).unbind_queue(queue, exchange, routing_key, arguments)

    def bind_exchange(
        self,
        source: str,
        destination: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        vhost: str = "/",
    ) -> bool:
        return self.vhost(vhost).bind_exchange(source, destination, routing_key, arguments)

    def unbind_exchange(
        self,
        source: str,
        destination: str,
        routing_key: str = "",
        *,
        arguments: dict[str, Any] | None = None,
        vhost: str = "/",
    ) -> None:
        self.vhost(vhost).unbind_exchange(source, destination, routing_key, arguments)

    # ---- 发布 ---------------------------------------------------------
    def publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes | str = b"",
        *,
        properties: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        mandatory: bool = False,
        vhost: str = "/",
    ) -> PublishResult:
        if self._alarms:
            reasons = ", ".join(f"{kind}: {reason}" for kind, reason in self._alarms.items())
            raise ResourceError(f"publisher blocked by resource alarm ({reasons})")
        props = dict(properties or {})
        if headers is not None:
            merged = dict(props.get("headers") or {})
            merged.update(headers)
            props["headers"] = merged
        message = Message(
            body=body,
            properties=props,
            exchange=exchange,
            routing_key=routing_key,
        )
        return self.publish_message(message, mandatory=mandatory, vhost=vhost)

    def publish_message(
        self, message: Message, *, mandatory: bool = False, vhost: str = "/"
    ) -> PublishResult:
        if self._alarms:
            reasons = ", ".join(f"{kind}: {reason}" for kind, reason in self._alarms.items())
            raise ResourceError(f"publisher blocked by resource alarm ({reasons})")
        self.metrics.inc("messages_published_total")
        try:
            result = self.vhost(vhost).publish(message, mandatory=mandatory)
        except PublishRejected:
            self.metrics.inc("publish_rejected_total")
            raise
        self.metrics.inc("messages_routed_total", result.routed)
        for listener in list(self._publish_listeners):
            try:
                listener(vhost, message, result)
            except Exception:
                LOG.exception("publish listener failed")
        self.refresh_gauges()
        return result

    # ---- 拉取与确认 ---------------------------------------------------
    def get(
        self,
        queue: str,
        *,
        auto_ack: bool = False,
        owner: Any = None,
        vhost: str = "/",
    ) -> Delivery | None:
        delivery = self.vhost(vhost).get_queue(queue).get(auto_ack=auto_ack, owner=owner)
        self.refresh_gauges()
        return delivery

    def issue_delivery(self, queue: Any, delivery: Delivery) -> int:
        with self._lock:
            self._delivery_seq += 1
            delivery.delivery_tag = self._delivery_seq
            if not delivery.auto_ack:
                self._deliveries[delivery.delivery_tag] = (queue, delivery)
        self.metrics.inc("messages_delivered_total")
        if delivery.message.redelivered:
            self.metrics.inc("messages_redelivered_total")
        return delivery.delivery_tag

    def release_delivery(self, delivery_tag: int) -> None:
        with self._lock:
            self._deliveries.pop(delivery_tag, None)

    def _validate_delivery_owner(self, delivery: Delivery, owner: Any) -> None:
        if owner is not None and delivery.owner is not owner:
            raise PreconditionFailed("delivery tag does not belong to this channel")

    def ack(
        self,
        delivery_tag: int,
        *,
        multiple: bool = False,
        owner: Any = None,
    ) -> int:
        with self._lock:
            selected = [
                (tag, queue, delivery)
                for tag, (queue, delivery) in self._deliveries.items()
                if (tag <= delivery_tag or delivery_tag == 0)
                and (owner is None or delivery.owner is owner)
            ]
            if multiple:
                selected.sort(key=lambda item: item[0])
            else:
                selected = [item for item in selected if item[0] == delivery_tag]
                if delivery_tag == 0:
                    selected = []
            if not selected:
                raise PreconditionFailed(f"unknown delivery tag {delivery_tag}")
            if not multiple and len(selected) != 1:
                raise PreconditionFailed(f"unknown delivery tag {delivery_tag}")
            self._validate_delivery_owner(selected[0][2], owner)
        by_queue: dict[Any, list[int]] = {}
        for tag, queue, _ in selected:
            by_queue.setdefault(id(queue), []).append(tag)
        count = 0
        for tag, queue, _ in selected:
            count += len(queue.ack(tag, multiple=False, owner=owner))
        self.metrics.inc("messages_acked_total", count)
        self.note_state_change()
        self.refresh_gauges()
        return count

    def nack(
        self,
        delivery_tag: int,
        *,
        requeue: bool = False,
        multiple: bool = False,
        owner: Any = None,
    ) -> int:
        with self._lock:
            selected = [
                (tag, queue, delivery)
                for tag, (queue, delivery) in self._deliveries.items()
                if (tag <= delivery_tag or delivery_tag == 0)
                and (owner is None or delivery.owner is owner)
            ]
            if multiple:
                selected.sort(key=lambda item: item[0])
            else:
                selected = [item for item in selected if item[0] == delivery_tag]
            if not selected:
                raise PreconditionFailed(f"unknown delivery tag {delivery_tag}")
            self._validate_delivery_owner(selected[0][2], owner)
        grouped: dict[int, Any] = {}
        count = 0
        for tag, queue, _ in selected:
            grouped[id(queue)] = queue
            count += len(queue.nack(tag, requeue=requeue, multiple=False, owner=owner))
        self.metrics.inc("messages_nacked_total", count)
        if requeue:
            self.metrics.inc("messages_requeued_total", count)
        self.note_state_change()
        self.refresh_gauges()
        return count

    def reject(
        self,
        delivery_tag: int,
        *,
        requeue: bool = False,
        owner: Any = None,
    ) -> None:
        self.nack(delivery_tag, requeue=requeue, owner=owner)

    def recover_owner(self, owner: Any) -> int:
        count = 0
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            for queue in list(vhost.queues.values()):
                count += queue.requeue_owner(owner)
        self.note_state_change()
        self.refresh_gauges()
        return count

    # ---- 消费者 -------------------------------------------------------
    def add_consumer(
        self,
        queue: str,
        callback: Callable[[Message, Delivery], None],
        *,
        consumer_tag: str = "",
        auto_ack: bool = False,
        prefetch_count: int = 0,
        exclusive: bool = False,
        priority: int = 0,
        owner: Any = None,
        suspended: bool = False,
        dispatch: bool = True,
        vhost: str = "/",
    ):
        if not consumer_tag:
            with self._lock:
                self._consumer_seq += 1
                consumer_tag = f"ctag-{self._consumer_seq}"
        target = self.vhost(vhost).get_queue(queue)
        handle = target.add_consumer(
            consumer_tag,
            callback,
            auto_ack=auto_ack,
            prefetch_count=prefetch_count,
            exclusive=exclusive,
            priority=priority,
            owner=owner,
            suspended=suspended,
        )
        if dispatch:
            target.dispatch()
        self.refresh_gauges()
        return handle

    consume = add_consumer

    def cancel_consumer(self, queue: str, consumer_tag: str, *, vhost: str = "/") -> None:
        target = self.vhost(vhost).get_queue(queue)
        target.remove_consumer(consumer_tag)
        if target.should_auto_delete():
            try:
                self.delete_queue(queue, vhost=vhost)
            except NotFound:
                pass
        self.refresh_gauges()

    def close_owner(self, owner: Any) -> None:
        """连接/信道关闭：取消消费者、重排队未确认消息、删除独占队列。"""

        auto_delete: list[tuple[VHost, str]] = []
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            for queue in list(vhost.queues.values()):
                queue.remove_owner_consumers(owner)
                if queue.exclusive and queue.owner is owner:
                    try:
                        vhost.delete_queue(queue.name)
                    except NotFound:
                        pass
                    continue
                queue.requeue_owner(owner)
                if queue.should_auto_delete():
                    auto_delete.append((vhost, queue.name))
        for vhost, queue_name in auto_delete:
            try:
                vhost.delete_queue(queue_name)
            except NotFound:
                pass
        for vhost in vhosts:
            self._cleanup_auto_delete_exchanges(vhost)
        self.refresh_gauges()

    # ---- 策略 ---------------------------------------------------------
    def set_policy(
        self,
        name: str,
        pattern: str,
        definition: dict[str, Any],
        *,
        apply_to: str = "all",
        priority: int = 0,
        vhost: str = "/",
    ) -> dict[str, Any]:
        result = self.vhost(vhost).set_policy(
            name, pattern, definition, apply_to=apply_to, priority=priority
        )
        self.refresh_gauges()
        return result

    def delete_policy(self, name: str, *, vhost: str = "/") -> None:
        self.vhost(vhost).delete_policy(name)

    # ---- 用户 ---------------------------------------------------------
    def add_user(self, username: str, password: str, *, tags=()) -> None:
        self.auth.add_user(username, password, tags=tags)
        self.note_state_change()

    def delete_user(self, username: str) -> None:
        self.auth.delete_user(username)
        self.note_state_change()

    def set_permissions(
        self,
        username: str,
        vhost: str,
        *,
        configure: str = ".*",
        write: str = ".*",
        read: str = ".*",
    ) -> None:
        self.auth.set_permissions(
            username, vhost, configure=configure, write=write, read=read
        )
        self.note_state_change()

    # ---- 告警 ---------------------------------------------------------
    def set_alarm(self, kind: str, active: bool, reason: str = "") -> None:
        changed = False
        with self._lock:
            if active and kind not in self._alarms:
                self._alarms[kind] = reason or "resource alarm"
                changed = True
            elif active:
                self._alarms[kind] = reason or self._alarms[kind]
            elif kind in self._alarms:
                self._alarms.pop(kind)
                changed = True
            blocked = bool(self._alarms)
        self.metrics.set("alarm_active", 1.0 if blocked else 0.0)
        if changed:
            for listener in list(self._alarm_listeners):
                try:
                    listener(kind, active, reason)
                except Exception:
                    LOG.exception("alarm listener failed")

    def is_blocked(self) -> bool:
        with self._lock:
            return bool(self._alarms)

    def add_alarm_listener(self, listener: Callable[[str, bool, str], None]) -> None:
        self._alarm_listeners.append(listener)

    def remove_alarm_listener(self, listener: Callable[[str, bool, str], None]) -> None:
        try:
            self._alarm_listeners.remove(listener)
        except ValueError:
            pass

    def add_publish_listener(
        self, listener: Callable[[str, Message, PublishResult], None]
    ) -> None:
        self._publish_listeners.append(listener)

    def remove_publish_listener(
        self, listener: Callable[[str, Message, PublishResult], None]
    ) -> None:
        try:
            self._publish_listeners.remove(listener)
        except ValueError:
            pass

    # ---- 维护与统计 ---------------------------------------------------
    def maintenance(self) -> dict[str, list[str]]:
        deleted: dict[str, list[str]] = {}
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            removed = vhost.expire()
            if removed:
                deleted[vhost.name] = removed
            self._cleanup_auto_delete_exchanges(vhost)
        self.refresh_gauges()
        return deleted

    def start_maintenance(self, interval: float = 1.0) -> None:
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            return
        self._maintenance_stop.clear()

        def loop() -> None:
            while not self._maintenance_stop.wait(interval):
                try:
                    self.maintenance()
                except Exception:
                    LOG.exception("maintenance tick failed")

        self._maintenance_thread = threading.Thread(
            target=loop, name="mini-rabbitmq-maintenance", daemon=True
        )
        self._maintenance_thread.start()

    def stop_maintenance(self) -> None:
        self._maintenance_stop.set()
        thread = self._maintenance_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._maintenance_thread = None

    def dead_letter(self, queue: Any, message: Message, reason: str) -> None:
        """队列在过期/拒绝/超长时回调；由对应 vhost 执行路由。"""

        vhost = self.vhosts.get(getattr(queue, "vhost", "/"))
        if vhost is None:
            return
        vhost.dead_letter(queue, message, reason)

    def _cleanup_auto_delete_exchanges(self, vhost: VHost) -> None:
        changed = True
        while changed:
            changed = False
            for exchange in list(vhost.exchanges.values()):
                if exchange.name == "" or not exchange.auto_delete:
                    continue
                if exchange.bindings:
                    continue
                try:
                    vhost.delete_exchange(exchange.name)
                    changed = True
                except (NotFound, PreconditionFailed):
                    continue

    def refresh_gauges(self) -> None:
        totals = {"queues": 0, "exchanges": 0, "consumers": 0, "messages_ready": 0, "messages_unacknowledged": 0}
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            current = vhost.totals()
            for key in totals:
                totals[key] += current[key]
        for key, value in totals.items():
            self.metrics.set(key, value)
        self.metrics.set("vhosts", len(vhosts))

    # ---- 查询 ---------------------------------------------------------
    def queue_names(self, vhost: str = "/") -> list[str]:
        return sorted(self.vhost(vhost).queues)

    def exchange_names(self, vhost: str = "/") -> list[str]:
        return sorted(self.vhost(vhost).exchanges)

    def overview(self) -> dict[str, Any]:
        self.refresh_gauges()
        totals = {
            "queues": 0,
            "exchanges": 0,
            "consumers": 0,
            "messages_ready": 0,
            "messages_unacknowledged": 0,
        }
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            current = vhost.totals()
            for key in totals:
                totals[key] += current[key]
        return {
            "node": self.node_name,
            "version": "0.1.0",
            "started_at": self._started_at,
            "uptime_seconds": int(time.time() - self._started_at),
            "vhosts": sorted(self.vhosts),
            "totals": totals,
            "alarms": dict(self._alarms),
            "metrics": self.metrics.snapshot(),
        }

    def queue_stats(self) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            vhosts = list(self.vhosts.values())
        for vhost in vhosts:
            for queue in vhost.queues.values():
                result.append(queue.to_dict())
        return result
