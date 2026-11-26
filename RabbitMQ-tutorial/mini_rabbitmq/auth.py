"""用户、密码哈希、vhost 权限。

认证回答“你是谁”，授权回答“你能对哪个 vhost 的哪类资源做什么”。真实
RabbitMQ 的权限分为 configure / write / read 三个正则，本模块保持一致。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from typing import Any


ITERATIONS = 120_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            _ALGO,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations_s, salt_b64, digest_b64 = encoded.split("$", 3)
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations_s)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass
class User:
    name: str
    password_hash: str
    tags: set[str] = field(default_factory=set)

    def has_tag(self, tag: str) -> bool:
        return "administrator" in self.tags or tag in self.tags

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "password_hash": self.password_hash, "tags": sorted(self.tags)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "User":
        return cls(
            name=data["name"],
            password_hash=data["password_hash"],
            tags=set(data.get("tags") or []),
        )


@dataclass
class Permission:
    configure: str = ".*"
    write: str = ".*"
    read: str = ".*"

    def allows(self, action: str, resource: str) -> bool:
        pattern = getattr(self, action, None)
        if pattern is None:
            return False
        try:
            return re.fullmatch(pattern, resource) is not None
        except re.error:
            return False

    def to_dict(self) -> dict[str, str]:
        return {"configure": self.configure, "write": self.write, "read": self.read}


class AuthStore:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.permissions: dict[str, dict[str, Permission]] = {}

    # ---- 用户 ---------------------------------------------------------
    def add_user(
        self, username: str, password: str, *, tags: tuple[str, ...] | list[str] = ()
    ) -> User:
        if not username:
            from .errors import InvalidArgument

            raise InvalidArgument("username must not be empty")
        user = User(username, hash_password(password), set(tags))
        self.users[username] = user
        self.permissions.setdefault(username, {})
        return user

    def delete_user(self, username: str) -> None:
        self.users.pop(username, None)
        self.permissions.pop(username, None)

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.users.get(username)
        if user is None:
            # 仍执行一次哈希，减小用户名枚举的时序差异。
            verify_password(password, hash_password("invalid"))
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    # ---- 权限 ---------------------------------------------------------
    def set_permissions(
        self,
        username: str,
        vhost: str,
        *,
        configure: str = ".*",
        write: str = ".*",
        read: str = ".*",
    ) -> None:
        if username not in self.users:
            from .errors import NotFound

            raise NotFound(f"no such user '{username}'")
        self.permissions.setdefault(username, {})[vhost] = Permission(
            configure=configure, write=write, read=read
        )

    def clear_permissions(self, username: str, vhost: str) -> None:
        self.permissions.get(username, {}).pop(vhost, None)

    def get_permission(self, username: str, vhost: str) -> Permission | None:
        return self.permissions.get(username, {}).get(vhost)

    def can_access_vhost(self, username: str, vhost: str) -> bool:
        return username in self.users and vhost in self.permissions.get(username, {})

    def authorize(self, username: str, vhost: str, action: str, resource: str) -> bool:
        permission = self.get_permission(username, vhost)
        return bool(permission and permission.allows(action, resource))

    # ---- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "users": [user.to_dict() for user in self.users.values()],
            "permissions": {
                username: {vhost: perm.to_dict() for vhost, perm in vhosts.items()}
                for username, vhosts in self.permissions.items()
            },
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        self.users = {
            item["name"]: User.from_dict(item) for item in data.get("users", [])
        }
        self.permissions = {
            username: {
                vhost: Permission(**perm) for vhost, perm in vhosts.items()
            }
            for username, vhosts in (data.get("permissions") or {}).items()
        }


def default_auth() -> AuthStore:
    store = AuthStore()
    store.add_user("guest", "guest", tags=("administrator", "management"))
    store.set_permissions("guest", "/")
    return store
