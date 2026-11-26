"""消息模型：AMQP Basic 属性、TTL、death 记录与序列化。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# AMQP Basic 属性的规范名称。这里全部使用下划线，wire 层负责映射到
# AMQP 的连字符名称（AMQP 字段本身并不带连字符，文档常用连字符表示）。
BASIC_PROPERTIES = (
    "content_type",
    "content_encoding",
    "headers",
    "delivery_mode",
    "priority",
    "correlation_id",
    "reply_to",
    "expiration",
    "message_id",
    "timestamp",
    "type",
    "user_id",
    "app_id",
    "cluster_id",
)


def _as_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, memoryview):
        return body.tobytes()
    if isinstance(body, str):
        return body.encode("utf-8")
    raise TypeError("message body must be bytes or str")


@dataclass
class Message:
    """一条不可变语义为主的消息。

    ``properties`` 保存 AMQP Basic 属性；``headers`` 是其中一个特殊属性。
    内部路由字段 ``exchange``/``routing_key`` 不属于 AMQP 内容，但便于
    管理 API 和调试展示。
    """

    body: bytes = b""
    properties: dict[str, Any] = field(default_factory=dict)
    exchange: str = ""
    routing_key: str = ""
    redelivered: bool = False

    def __post_init__(self) -> None:
        self.body = _as_bytes(self.body)
        self.properties = dict(self.properties or {})
        headers = self.properties.get("headers")
        self.properties["headers"] = dict(headers or {})
        self.properties.setdefault("message_id", uuid.uuid4().hex)
        self.properties.setdefault("timestamp", int(time.time()))
        delivery_mode = int(self.properties.get("delivery_mode", 1) or 1)
        if delivery_mode not in (1, 2):
            raise ValueError("delivery_mode must be 1 (transient) or 2 (persistent)")
        self.properties["delivery_mode"] = delivery_mode

    # ---- 便捷属性 -----------------------------------------------------
    @property
    def headers(self) -> dict[str, Any]:
        return self.properties.setdefault("headers", {})

    @property
    def message_id(self) -> str:
        return str(self.properties.get("message_id", ""))

    @property
    def priority(self) -> int:
        try:
            return int(self.properties.get("priority", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def delivery_mode(self) -> int:
        return int(self.properties.get("delivery_mode", 1) or 1)

    @property
    def persistent(self) -> bool:
        return self.delivery_mode == 2

    @property
    def ttl_ms(self) -> int | None:
        value = self.properties.get("expiration")
        if value is None or value == "":
            return None
        try:
            ttl = int(value)
        except (TypeError, ValueError):
            return None
        return ttl if ttl >= 0 else None

    @property
    def size(self) -> int:
        # 轻量估算：正文 + 属性 JSON 的 UTF-8 大小。用于 max-length-bytes。
        import json

        return len(self.body) + len(
            json.dumps(self.properties, ensure_ascii=False, default=str).encode("utf-8")
        )

    # ---- 派生操作 -----------------------------------------------------
    def clone(self, **changes: Any) -> "Message":
        properties = dict(self.properties)
        properties["headers"] = dict(self.headers)
        cloned = Message(
            body=self.body,
            properties=properties,
            exchange=changes.pop("exchange", self.exchange),
            routing_key=changes.pop("routing_key", self.routing_key),
            redelivered=changes.pop("redelivered", self.redelivered),
        )
        for key, value in changes.items():
            if key in BASIC_PROPERTIES:
                cloned.properties[key] = value
            else:
                setattr(cloned, key, value)
        return cloned

    def add_death(
        self,
        *,
        queue: str,
        reason: str,
        exchange: str,
        routing_keys: list[str],
    ) -> "Message":
        """按 RabbitMQ 的 ``x-death`` 形状追加一条死亡记录。"""

        cloned = self.clone(redelivered=self.redelivered)
        deaths = list(cloned.headers.get("x-death") or [])
        now = int(time.time())
        found = None
        for item in deaths:
            if (
                item.get("queue") == queue
                and item.get("reason") == reason
                and item.get("exchange") == exchange
            ):
                found = item
                break
        if found is None:
            found = {
                "count": 1,
                "reason": reason,
                "queue": queue,
                "exchange": exchange,
                "routing-keys": list(routing_keys),
                "time": now,
            }
            deaths.append(found)
        else:
            found["count"] = int(found.get("count", 1)) + 1
            found["time"] = now
            keys = list(found.get("routing-keys") or [])
            for key in routing_keys:
                if key not in keys:
                    keys.append(key)
            found["routing-keys"] = keys
        cloned.headers["x-death"] = deaths
        cloned.headers.setdefault("x-first-death-reason", reason)
        cloned.headers.setdefault("x-first-death-queue", queue)
        cloned.headers.setdefault("x-first-death-exchange", exchange)
        return cloned

    def death_hops(self) -> int:
        try:
            return sum(int(item.get("count", 1)) for item in self.headers.get("x-death", []))
        except (TypeError, ValueError):
            return 0

    # ---- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        import base64

        return {
            "body": base64.b64encode(self.body).decode("ascii"),
            "properties": self.properties,
            "exchange": self.exchange,
            "routing_key": self.routing_key,
            "redelivered": self.redelivered,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        import base64

        return cls(
            body=base64.b64decode(data.get("body", "")),
            properties=data.get("properties") or {},
            exchange=data.get("exchange", ""),
            routing_key=data.get("routing_key", ""),
            redelivered=bool(data.get("redelivered", False)),
        )


@dataclass
class Delivery:
    """一次投递：把消息、队列、投递标签和连接/信道所有者绑在一起。"""

    message: Message
    delivery_tag: int
    queue: str
    owner: Any = None
    consumer_tag: str | None = None
    auto_ack: bool = False
    redelivered: bool = False
    queued_seq: int = 0
    expires_at: float | None = None
    delivered_at: float = field(default_factory=time.monotonic)
