"""策略（policy）引擎。

RabbitMQ 的 policy 是按名称正则匹配、按优先级合并的运行时配置。例如：

    {"max-length": 1000, "dead-letter-exchange": "dlx"}

本模块把常见 policy 键映射为队列的 ``x-*`` 参数，并在队列声明时合并。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import InvalidArgument


# policy 定义中的键 -> 队列 arguments 中的键
QUEUE_POLICY_KEYS = {
    "max-length": "x-max-length",
    "max-length-bytes": "x-max-length-bytes",
    "message-ttl": "x-message-ttl",
    "expires": "x-expires",
    "dead-letter-exchange": "x-dead-letter-exchange",
    "dead-letter-routing-key": "x-dead-letter-routing-key",
    "max-priority": "x-max-priority",
    "overflow": "x-overflow",
    "queue-mode": "x-queue-mode",
    "queue-version": "x-queue-version",
    "single-active-consumer": "x-single-active-consumer",
    "delivery-limit": "x-delivery-limit",
    "max-in-memory-length": "x-max-in-memory-length",
    "max-in-memory-bytes": "x-max-in-memory-bytes",
    "stream-max-segment-size-bytes": "x-stream-max-segment-size-bytes",
}


@dataclass
class Policy:
    name: str
    pattern: str
    definition: dict[str, Any]
    apply_to: str = "all"
    priority: int = 0

    def matches(self, resource_name: str, kind: str) -> bool:
        if self.apply_to not in ("all", kind):
            return False
        try:
            return re.fullmatch(self.pattern, resource_name) is not None
        except re.error:
            return False

    def queue_arguments(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.definition.items():
            if key in QUEUE_POLICY_KEYS:
                result[QUEUE_POLICY_KEYS[key]] = value
            elif key.startswith("x-"):
                result[key] = value
            elif key in {"ha-mode", "ha-params", "ha-sync-mode"}:
                # 镜像队列在 RabbitMQ 4.x 已移除；保留解析以便迁移检测。
                result[f"policy:{key}"] = value
            else:
                result[f"policy:{key}"] = value
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "definition": dict(self.definition),
            "apply-to": self.apply_to,
            "priority": self.priority,
        }


class PolicySet:
    def __init__(self) -> None:
        self._policies: dict[str, Policy] = {}

    def set(
        self,
        name: str,
        pattern: str,
        definition: dict[str, Any],
        *,
        apply_to: str = "all",
        priority: int = 0,
    ) -> Policy:
        if apply_to not in ("all", "queues", "exchanges"):
            raise InvalidArgument("apply-to must be all, queues or exchanges")
        if not definition:
            raise InvalidArgument("policy definition must not be empty")
        policy = Policy(
            name=name,
            pattern=pattern,
            definition=dict(definition),
            apply_to=apply_to,
            priority=int(priority),
        )
        self._policies[name] = policy
        return policy

    def delete(self, name: str) -> bool:
        return self._policies.pop(name, None) is not None

    def get(self, name: str) -> Policy | None:
        return self._policies.get(name)

    def matching(self, resource_name: str, kind: str) -> list[Policy]:
        found = [
            policy
            for policy in self._policies.values()
            if policy.matches(resource_name, kind)
        ]
        return sorted(found, key=lambda p: (p.priority, p.name))

    def queue_arguments(self, queue_name: str) -> dict[str, Any]:
        """按优先级从低到高合并；高优先级覆盖低优先级。"""

        merged: dict[str, Any] = {}
        for policy in self.matching(queue_name, "queues"):
            merged.update(policy.queue_arguments())
        return merged

    def exchange_arguments(self, exchange_name: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for policy in self.matching(exchange_name, "exchanges"):
            for key, value in policy.definition.items():
                merged[key] = value
        return merged

    def to_list(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in sorted(self._policies.values(), key=lambda p: p.name)]
