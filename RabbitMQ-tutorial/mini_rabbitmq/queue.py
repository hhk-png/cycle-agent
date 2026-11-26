"""队列实现：优先级、TTL、长度上限、消费者预取、确认与死信。

设计目标不是复刻 RabbitMQ 的磁盘段文件和 Raft，而是把队列语义显式建模，
让每一条可靠性规则都能被单元测试和网络测试验证。
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import InvalidArgument, PreconditionFailed, PublishRejected, ResourceLocked
from .message import Delivery, Message


LOG = logging.getLogger(__name__)


@dataclass
class ConsumerHandle:
    consumer_tag: str
    queue_name: str
    callback: Callable[[Message, Delivery], None]
    auto_ack: bool = False
    prefetch_count: int = 0
    exclusive: bool = False
    priority: int = 0
    owner: Any = None
    active: bool = True
    suspended: bool = False
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)


def _int_arg(arguments: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(arguments.get(key, default))
    except (TypeError, ValueError):
        return default


def _bool_arg(arguments: dict[str, Any], key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


class Queue:
    """经典队列的教学实现；``quorum`` 只复用数据结构，不提供真实复制。"""

    allowed_types = {"classic", "quorum"}

    def __init__(
        self,
        name: str,
        *,
        durable: bool = False,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict[str, Any] | None = None,
        owner: Any = None,
        broker: Any = None,
        vhost: str = "/",
        declaring_user: str | None = None,
    ) -> None:
        self.name = name
        self.durable = bool(durable)
        self.exclusive = bool(exclusive)
        self.auto_delete = bool(auto_delete)
        self.base_arguments = dict(arguments or {})
        self.arguments = dict(arguments or {})
        self.owner = owner
        self.broker = broker
        self.vhost = vhost
        self.declaring_user = declaring_user
        self.created_at = time.time()
        self.last_used = self.created_at
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._dispatching = False
        self._dispatch_pending = False
        self._heap: list[tuple[int, int, float | None, Message]] = []
        self._seq = 0
        self._bytes = 0
        self._unacked: dict[int, Delivery] = {}
        self._consumers: dict[str, ConsumerHandle] = {}
        self._rr_cursor = 0
        self._had_consumers = False
        self._delivery_counts: dict[int, int] = {}
        self.stats: dict[str, int] = {
            "published": 0,
            "delivered": 0,
            "acked": 0,
            "redelivered": 0,
            "dead_lettered": 0,
            "expired": 0,
            "purged": 0,
        }
        self._refresh_settings()
        if self.queue_type not in self.allowed_types:
            raise InvalidArgument(
                f"queue type '{self.queue_type}' is not implemented in the "
                "core queue; use mini_rabbitmq.streams.Stream for stream labs"
            )
        if self.queue_type == "quorum":
            if exclusive:
                raise InvalidArgument("quorum queues cannot be exclusive")
            if not durable:
                raise InvalidArgument("quorum queues must be durable")
            if self.max_priority:
                raise InvalidArgument("quorum queues do not support x-max-priority")

    # ---- 配置 ---------------------------------------------------------
    def _refresh_settings(self) -> None:
        args = self.arguments
        self.queue_type = str(args.get("x-queue-type", "classic"))
        raw_priority = _int_arg(args, "x-max-priority", 0)
        if raw_priority < 0 or raw_priority > 255:
            raise InvalidArgument("x-max-priority must be between 0 and 255")
        self.max_priority = raw_priority
        self.message_ttl = _int_arg(args, "x-message-ttl", -1)
        self.expires = _int_arg(args, "x-expires", 0)
        self.max_length = max(0, _int_arg(args, "x-max-length", 0))
        self.max_length_bytes = max(0, _int_arg(args, "x-max-length-bytes", 0))
        self.overflow = str(args.get("x-overflow", "drop-head"))
        self.dlx = args.get("x-dead-letter-exchange")
        self.dlx_routing_key = args.get("x-dead-letter-routing-key")
        self.single_active_consumer = _bool_arg(args, "x-single-active-consumer")
        self.delivery_limit = max(0, _int_arg(args, "x-delivery-limit", 0))
        known_overflow = {"drop-head", "reject-publish", "reject-publish-dlx"}
        if self.overflow not in known_overflow:
            raise InvalidArgument(f"invalid x-overflow value '{self.overflow}'")
        if self.message_ttl == 0:
            # 0 是合法的“立即过期”；负数表示未设置。
            self.message_ttl = 0
        elif self.message_ttl < 0:
            self.message_ttl = None  # type: ignore[assignment]
        if self.expires < 0:
            raise InvalidArgument("x-expires must be positive")
        if self.expires == 0:
            self.expires = None  # type: ignore[assignment]
        if self.queue_type == "quorum":
            if self.max_priority:
                raise InvalidArgument("quorum queues do not support x-max-priority")
            if not self.durable:
                raise InvalidArgument("quorum queues must be durable")
            if self.exclusive:
                raise InvalidArgument("quorum queues cannot be exclusive")

    def _enforce_limits_locked(self) -> None:
        if not self.max_length and not self.max_length_bytes:
            return
        while self._heap and (
            (self.max_length and len(self._heap) > self.max_length)
            or (self.max_length_bytes and self._bytes > self.max_length_bytes)
        ):
            _, _, _, message = heapq.heappop(self._heap)
            self._bytes -= message.size
            self.stats["dead_lettered"] += 1 if self.dlx else 0
            if self.dlx:
                self._dead_letter(message, "maxlen")

    def reconfigure(self, arguments: dict[str, Any]) -> None:
        """策略变更后的运行时重配置。"""

        with self._lock:
            old_type = self.queue_type
            old_priority = self.max_priority
            old_expires = self.expires
            self.arguments = dict(arguments)
            self._refresh_settings()
            if self.queue_type != old_type:
                raise PreconditionFailed("x-queue-type cannot be changed by policy after declaration")
            if old_priority != self.max_priority:
                raise PreconditionFailed("x-max-priority cannot be changed after declaration")
            if old_expires != self.expires:
                self.last_used = time.time()
            self._enforce_limits_locked()

    # ---- 指标与状态 ---------------------------------------------------
    @property
    def messages_ready(self) -> int:
        with self._lock:
            return len(self._heap)

    @property
    def messages_unacknowledged(self) -> int:
        with self._lock:
            return len(self._unacked)

    @property
    def message_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def consumer_count(self) -> int:
        with self._lock:
            return len(self._consumers)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._heap and not self._unacked

    def has_consumers(self) -> bool:
        with self._lock:
            return bool(self._consumers)

    # ---- 消息入队 -----------------------------------------------------
    def _compute_expiry(self, message: Message) -> float | None:
        now = time.time()
        ttls: list[int] = []
        message_ttl = message.ttl_ms
        if message_ttl is not None:
            ttls.append(message_ttl)
        if self.message_ttl is not None:
            ttls.append(int(self.message_ttl))
        return now + min(ttls) / 1000.0 if ttls else None

    def _priority_of(self, message: Message) -> int:
        priority = max(0, min(255, message.priority))
        if self.max_priority:
            priority = min(priority, self.max_priority)
        return priority

    def enqueue(self, message: Message) -> bool:
        """入队。返回 False 表示消息被丢弃；队列满且 reject 时抛异常。"""

        with self._lock:
            self.last_used = time.time()
            self._expire_locked()
            if self._would_exceed_locked(message):
                handled = self._handle_overflow_locked(message)
                if handled:
                    return False
            expires_at = self._compute_expiry(message)
            if expires_at is not None and expires_at <= time.time():
                self.stats["expired"] += 1
                self._dead_letter(message, "expired")
                return False
            self._push_locked(message, expires_at)
            self.stats["published"] += 1
            return True

    def _would_exceed_locked(self, message: Message) -> bool:
        if self.max_length and len(self._heap) + 1 > self.max_length:
            return True
        if self.max_length_bytes and self._bytes + message.size > self.max_length_bytes:
            return True
        return False

    def _handle_overflow_locked(self, message: Message) -> bool:
        if self.overflow in ("reject-publish", "reject-publish-dlx"):
            if self.overflow == "reject-publish-dlx":
                self.stats["dead_lettered"] += 1
                self._dead_letter(message, "maxlen")
            raise PublishRejected(
                f"queue '{self.name}' is full (length={self.max_length}, "
                f"bytes={self.max_length_bytes})"
            )
        # drop-head：删除最旧/最低优先级的消息，再尝试放入新消息。
        victim = None
        if self._heap:
            _, _, _, victim = heapq.heappop(self._heap)
            self._bytes -= victim.size
            self.stats["dead_lettered"] += 1 if self.dlx else 0
            if self.dlx:
                self._dead_letter(victim, "maxlen")
        # 如果限制为 0 或仍然放不下，新消息也被丢弃/死信。
        if self._would_exceed_locked(message):
            if self.dlx:
                self.stats["dead_lettered"] += 1
                self._dead_letter(message, "maxlen")
            return True
        return False

    def _push_locked(self, message: Message, expires_at: float | None) -> int:
        self._seq += 1
        seq = self._seq
        heapq.heappush(
            self._heap,
            (-self._priority_of(message), seq, expires_at, message),
        )
        self._bytes += message.size
        return seq

    # ---- 过期 ---------------------------------------------------------
    def _expire_locked(self) -> None:
        now = time.time()
        if not self._heap:
            return
        kept: list[tuple[int, int, float | None, Message]] = []
        expired: list[Message] = []
        for item in self._heap:
            expires_at = item[2]
            if expires_at is not None and expires_at <= now:
                expired.append(item[3])
                self._bytes -= item[3].size
                self.stats["expired"] += 1
                self.stats["dead_lettered"] += 1 if self.dlx else 0
            else:
                kept.append(item)
        if len(kept) != len(self._heap):
            self._heap = kept
            heapq.heapify(self._heap)
        for message in expired:
            self._dead_letter(message, "expired")

    def expire_ready(self) -> int:
        with self._lock:
            before = len(self._heap)
            self._expire_locked()
            return before - len(self._heap)

    def expired_by_inactivity(self, now: float | None = None) -> bool:
        with self._lock:
            if not self.expires or self._consumers:
                return False
            now = time.time() if now is None else now
            return (now - self.last_used) * 1000.0 >= self.expires

    # ---- 拉取与投递 ---------------------------------------------------
    def _make_delivery(
        self,
        message: Message,
        *,
        owner: Any,
        consumer_tag: str | None,
        auto_ack: bool,
        queued_seq: int,
        expires_at: float | None,
    ) -> Delivery:
        delivery = Delivery(
            message=message,
            delivery_tag=0,
            queue=self.name,
            owner=owner,
            consumer_tag=consumer_tag,
            auto_ack=auto_ack,
            redelivered=message.redelivered,
            queued_seq=queued_seq,
            expires_at=expires_at,
        )
        if self.broker is not None:
            self.broker.issue_delivery(self, delivery)
        return delivery

    def get(self, *, auto_ack: bool = False, owner: Any = None) -> Delivery | None:
        with self._lock:
            self.last_used = time.time()
            self._expire_locked()
            if not self._heap:
                return None
            _, seq, expires_at, message = heapq.heappop(self._heap)
            self._bytes -= message.size
            delivery = self._make_delivery(
                message,
                owner=owner,
                consumer_tag=None,
                auto_ack=auto_ack,
                queued_seq=seq,
                expires_at=expires_at,
            )
            if not auto_ack:
                self._unacked[delivery.delivery_tag] = delivery
                self._delivery_counts[seq] = self._delivery_counts.get(seq, 0) + 1
            self.stats["delivered"] += 1
        self.dispatch()
        return delivery

    def _next_consumer_locked(self) -> ConsumerHandle | None:
        if not self._consumers:
            return None
        consumers = sorted(
            self._consumers.values(),
            key=lambda c: (-c.priority, c.created_at, c.consumer_tag),
        )
        count = len(consumers)
        start = self._rr_cursor % count
        for offset in range(count):
            consumer = consumers[(start + offset) % count]
            if consumer.cancelled or not consumer.active or consumer.suspended:
                continue
            if consumer.auto_ack or consumer.prefetch_count <= 0:
                self._rr_cursor = (start + offset + 1) % count
                return consumer
            unacked = sum(1 for d in self._unacked.values() if d.consumer_tag == consumer.consumer_tag)
            if unacked < consumer.prefetch_count:
                self._rr_cursor = (start + offset + 1) % count
                return consumer
        return None

    def dispatch(self) -> int:
        """把可投递消息推给消费者；返回本轮新投递数量。"""

        with self._dispatch_lock:
            if self._dispatching:
                self._dispatch_pending = True
                return 0
            self._dispatching = True
            delivered = 0
            try:
                while True:
                    with self._lock:
                        self._expire_locked()
                        if not self._heap:
                            break
                        consumer = self._next_consumer_locked()
                        if consumer is None:
                            break
                        _, seq, expires_at, message = heapq.heappop(self._heap)
                        self._bytes -= message.size
                        delivery = self._make_delivery(
                            message,
                            owner=consumer.owner,
                            consumer_tag=consumer.consumer_tag,
                            auto_ack=consumer.auto_ack,
                            queued_seq=seq,
                            expires_at=expires_at,
                        )
                        if not consumer.auto_ack:
                            self._unacked[delivery.delivery_tag] = delivery
                            self._delivery_counts[seq] = self._delivery_counts.get(seq, 0) + 1
                        self.stats["delivered"] += 1
                    delivered += 1
                    try:
                        consumer.callback(message, delivery)
                    except Exception:  # 消费者异常不应杀死 broker 线程
                        LOG.exception("consumer %s callback failed", consumer.consumer_tag)
            finally:
                self._dispatching = False
            pending = self._dispatch_pending
            self._dispatch_pending = False
        if pending:
            delivered += self.dispatch()
        return delivered

    # ---- 消费者 -------------------------------------------------------
    def add_consumer(
        self,
        consumer_tag: str,
        callback: Callable[[Message, Delivery], None],
        *,
        auto_ack: bool = False,
        prefetch_count: int = 0,
        exclusive: bool = False,
        priority: int = 0,
        owner: Any = None,
        suspended: bool = False,
    ) -> ConsumerHandle:
        with self._lock:
            if consumer_tag in self._consumers:
                raise PreconditionFailed(f"consumer tag '{consumer_tag}' already in use")
            same_connection = (
                self.owner is owner
                or (
                    getattr(self.owner, "connection_id", None) is not None
                    and getattr(self.owner, "connection_id", None)
                    == getattr(owner, "connection_id", None)
                )
            )
            if self.exclusive and self.owner is not None and not same_connection:
                raise ResourceLocked(f"queue '{self.name}' is exclusive")
            if exclusive and self._consumers:
                raise ResourceLocked(
                    f"queue '{self.name}' already has consumers; exclusive consume refused"
                )
            if any(c.exclusive for c in self._consumers.values()):
                raise ResourceLocked(f"queue '{self.name}' has an exclusive consumer")
            active = True
            if self.single_active_consumer and self._consumers:
                active = False
            consumer = ConsumerHandle(
                consumer_tag=consumer_tag,
                queue_name=self.name,
                callback=callback,
                auto_ack=bool(auto_ack),
                prefetch_count=max(0, int(prefetch_count)),
                exclusive=bool(exclusive),
                priority=int(priority),
                owner=owner,
                active=active,
                suspended=suspended,
            )
            self._consumers[consumer_tag] = consumer
            self._had_consumers = True
            self.last_used = time.time()
            return consumer

    def resume_consumer(self, consumer_tag: str) -> None:
        with self._lock:
            consumer = self._consumers.get(consumer_tag)
            if consumer is not None:
                consumer.suspended = False
        self.dispatch()

    def remove_consumer(self, consumer_tag: str) -> ConsumerHandle | None:
        with self._lock:
            consumer = self._consumers.pop(consumer_tag, None)
            if consumer is not None:
                consumer.cancelled = True
                if self.single_active_consumer and consumer.active:
                    for candidate in sorted(
                        self._consumers.values(), key=lambda c: c.created_at
                    ):
                        candidate.active = True
                        break
                    else:
                        consumer.active = False
            return consumer

    def remove_owner_consumers(self, owner: Any) -> list[ConsumerHandle]:
        with self._lock:
            tags = [tag for tag, c in self._consumers.items() if c.owner is owner]
        removed = []
        for tag in tags:
            consumer = self.remove_consumer(tag)
            if consumer is not None:
                removed.append(consumer)
        return removed

    def should_auto_delete(self) -> bool:
        with self._lock:
            return bool(self.auto_delete and self._had_consumers and not self._consumers)

    # ---- 结算 ---------------------------------------------------------
    def _ack_locked(self, tag: int, multiple: bool, owner: Any) -> list[Delivery]:
        if multiple:
            tags = sorted(
                t
                for t, d in self._unacked.items()
                if t <= tag and (owner is None or d.owner is owner)
            )
            if tag != 0 and tag not in self._unacked and not tags:
                raise PreconditionFailed(f"unknown delivery tag {tag}")
        else:
            if tag not in self._unacked:
                raise PreconditionFailed(f"unknown delivery tag {tag}")
            tags = [tag]
        acked = []
        for item_tag in tags:
            delivery = self._unacked.pop(item_tag, None)
            if delivery is None:
                continue
            self._delivery_counts.pop(delivery.queued_seq, None)
            self.stats["acked"] += 1
            if self.broker is not None:
                self.broker.release_delivery(item_tag)
            acked.append(delivery)
        return acked

    def ack(self, tag: int, *, multiple: bool = False, owner: Any = None) -> list[Delivery]:
        with self._lock:
            acked = self._ack_locked(tag, multiple, owner)
        self.dispatch()
        return acked

    def _requeue_locked(self, delivery: Delivery, *, at_head: bool = False) -> bool:
        message = delivery.message.clone(redelivered=True)
        self.stats["redelivered"] += 1
        now = time.time()
        expires_at = delivery.expires_at
        if expires_at is not None and expires_at <= now:
            self.stats["expired"] += 1
            self.stats["dead_lettered"] += 1 if self.dlx else 0
            self._dead_letter(message, "expired")
            return False
        seq = delivery.queued_seq or (self._seq + 1)
        if seq > self._seq:
            self._seq = seq
        heapq.heappush(
            self._heap,
            (-self._priority_of(message), seq, expires_at, message),
        )
        self._bytes += message.size
        return True

    def nack(
        self,
        tag: int,
        *,
        requeue: bool,
        multiple: bool = False,
        owner: Any = None,
    ) -> list[Delivery]:
        with self._lock:
            if multiple:
                tags = sorted(
                    t
                    for t, d in self._unacked.items()
                    if t <= tag and (owner is None or d.owner is owner)
                )
                if tag != 0 and tag not in self._unacked and not tags:
                    raise PreconditionFailed(f"unknown delivery tag {tag}")
            else:
                if tag not in self._unacked:
                    raise PreconditionFailed(f"unknown delivery tag {tag}")
                tags = [tag]
            handled = []
            for item_tag in tags:
                delivery = self._unacked.pop(item_tag, None)
                if delivery is None:
                    continue
                if self.broker is not None:
                    self.broker.release_delivery(item_tag)
                count = self._delivery_counts.get(delivery.queued_seq, 1)
                if requeue and self.delivery_limit and count >= self.delivery_limit:
                    self.stats["dead_lettered"] += 1
                    self._dead_letter(delivery.message.clone(redelivered=True), "delivery_limit")
                    requeue_this = False
                elif requeue:
                    requeue_this = self._requeue_locked(delivery)
                else:
                    requeue_this = False
                    self._dead_letter(delivery.message.clone(redelivered=True), "rejected")
                # 成功重排队保留投递计数，让 x-delivery-limit 能持续累计；
                # 最终丢弃/死信或 ack 时才清理。
                if not requeue_this:
                    self._delivery_counts.pop(delivery.queued_seq, None)
                handled.append(delivery)
        self.dispatch()
        return handled

    def requeue_owner(self, owner: Any) -> int:
        count = 0
        with self._lock:
            tags = [tag for tag, d in self._unacked.items() if d.owner is owner]
            for tag in tags:
                delivery = self._unacked.pop(tag, None)
                if delivery is None:
                    continue
                if self.broker is not None:
                    self.broker.release_delivery(tag)
                if delivery.auto_ack:
                    continue
                self._requeue_locked(delivery)
                count += 1
        self.dispatch()
        return count

    def recover_owner(self, owner: Any) -> int:
        return self.requeue_owner(owner)

    def requeue_all(self) -> int:
        with self._lock:
            owners = {delivery.owner for delivery in self._unacked.values()}
        count = 0
        for owner in owners:
            count += self.requeue_owner(owner)
        return count

    # ---- 清理 ---------------------------------------------------------
    def purge(self) -> int:
        with self._lock:
            count = len(self._heap)
            self.stats["purged"] += count
            self._heap.clear()
            self._bytes = 0
        return count

    def drop_all(self) -> int:
        """队列删除时调用：丢弃 ready 和 unacked，返回被丢弃消息数。"""

        with self._lock:
            count = len(self._heap) + len(self._unacked)
            for tag, delivery in self._unacked.items():
                if self.broker is not None:
                    self.broker.release_delivery(tag)
                del delivery
            self._unacked.clear()
            self._heap.clear()
            self._bytes = 0
            self._delivery_counts.clear()
        return count

    # ---- 快照 ---------------------------------------------------------
    def snapshot(self, persistent_only: bool = True) -> dict[str, Any]:
        with self._lock:
            ready = []
            for _, seq, expires_at, message in sorted(self._heap, key=lambda i: (i[0], i[1])):
                if persistent_only and not message.persistent:
                    continue
                ready.append(
                    {
                        "seq": seq,
                        "expires_at": expires_at,
                        "message": message.to_dict(),
                    }
                )
            unacked = []
            for delivery in self._unacked.values():
                if persistent_only and not delivery.message.persistent:
                    continue
                unacked.append(
                    {
                        "seq": delivery.queued_seq,
                        "expires_at": delivery.expires_at,
                        "message": delivery.message.to_dict(),
                    }
                )
            return {
                "name": self.name,
                "durable": self.durable,
                "exclusive": self.exclusive,
                "auto_delete": self.auto_delete,
                "arguments": self.base_arguments,
                "owner": None,
                "declaring_user": self.declaring_user,
                "messages": ready,
                "unacked": unacked,
            }

    def restore(self, data: dict[str, Any]) -> None:
        """从快照恢复；不触发 DLX/长度拒绝，尽量保留原顺序。"""

        with self._lock:
            self._seq = 0
            for bucket in ("messages", "unacked"):
                for item in data.get(bucket, []):
                    message = Message.from_dict(item["message"])
                    message.redelivered = bool(item.get("redelivered", bucket == "unacked"))
                    seq = int(item.get("seq", 0)) or (self._seq + 1)
                    self._seq = max(self._seq, seq)
                    expires_at = item.get("expires_at")
                    if expires_at is None:
                        expires_at = self._compute_expiry(message)
                    heapq.heappush(
                        self._heap,
                        (-self._priority_of(message), seq, expires_at, message),
                    )
                    self._bytes += message.size
        self.dispatch()

    # ---- 展示 ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "vhost": self.vhost,
                "type": self.queue_type,
                "durable": self.durable,
                "exclusive": self.exclusive,
                "auto_delete": self.auto_delete,
                "arguments": dict(self.arguments),
                "messages_ready": len(self._heap),
                "messages_unacknowledged": len(self._unacked),
                "messages": len(self._heap) + len(self._unacked),
                "message_bytes": self._bytes,
                "consumers": len(self._consumers),
                "consumer_details": [
                    {
                        "consumer_tag": c.consumer_tag,
                        "ack_required": not c.auto_ack,
                        "prefetch_count": c.prefetch_count,
                        "active": c.active,
                        "exclusive": c.exclusive,
                        "priority": c.priority,
                    }
                    for c in self._consumers.values()
                ],
                "stats": dict(self.stats),
            }

    # ---- 死信 ---------------------------------------------------------
    def _dead_letter(self, message: Message, reason: str) -> None:
        if self.broker is None:
            return
        try:
            self.broker.dead_letter(self, message, reason)
        except Exception:
            LOG.exception("failed to dead-letter message from %s", self.name)
