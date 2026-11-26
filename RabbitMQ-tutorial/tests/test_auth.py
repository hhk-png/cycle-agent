from __future__ import annotations

import pytest

from mini_rabbitmq.auth import (
    AuthStore,
    Permission,
    default_auth,
    hash_password,
    verify_password,
)
from mini_rabbitmq.errors import NotFound


def test_password_hash_roundtrip_and_salt():
    first = hash_password("secret", iterations=1000)
    second = hash_password("secret", iterations=1000)
    assert first != second
    assert verify_password("secret", first)
    assert not verify_password("wrong", first)


def test_malformed_hash_is_rejected():
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "argon2$1$abc$def") is False


def test_add_authenticate_and_delete_user():
    store = AuthStore()
    store.add_user("alice", "pw", tags=("monitoring",))
    user = store.authenticate("alice", "pw")
    assert user is not None
    assert user.name == "alice"
    assert user.has_tag("monitoring")
    assert store.authenticate("alice", "bad") is None
    store.delete_user("alice")
    assert store.authenticate("alice", "pw") is None


def test_unknown_user_authentication_returns_none():
    store = AuthStore()
    assert store.authenticate("nobody", "pw") is None


def test_permission_patterns():
    permission = Permission(configure="^orders$", write="^events$", read="^orders.*")
    assert permission.allows("configure", "orders")
    assert not permission.allows("configure", "orders2")
    assert permission.allows("write", "events")
    assert permission.allows("read", "orders.dlq")
    assert permission.allows("unknown", "orders") is False


def test_invalid_permission_regex_denies():
    permission = Permission(configure="[")
    assert permission.allows("configure", "anything") is False


def test_set_permissions_requires_user():
    store = AuthStore()
    with pytest.raises(NotFound):
        store.set_permissions("missing", "/")


def test_authorize_requires_vhost_permission():
    store = AuthStore()
    store.add_user("bob", "pw", tags=("administrator",))
    store.set_permissions("bob", "/prod", configure=".*", write="^orders$", read=".*")
    assert store.authorize("bob", "/prod", "write", "orders")
    assert not store.authorize("bob", "/prod", "write", "admin")
    assert not store.authorize("bob", "/dev", "write", "orders")
    assert store.can_access_vhost("bob", "/prod")
    assert not store.can_access_vhost("bob", "/dev")


def test_admin_tag_does_not_bypass_vhost_acl():
    store = AuthStore()
    store.add_user("admin2", "pw", tags=("administrator",))
    store.set_permissions("admin2", "/", write="^allowed$")
    assert not store.authorize("admin2", "/", "write", "blocked")


def test_permissions_clear():
    store = AuthStore()
    store.add_user("alice", "pw")
    store.set_permissions("alice", "/")
    assert store.can_access_vhost("alice", "/")
    store.clear_permissions("alice", "/")
    assert not store.can_access_vhost("alice", "/")


def test_auth_store_serialization_roundtrip():
    store = AuthStore()
    store.add_user("alice", "pw", tags=("management",))
    store.set_permissions("alice", "/", configure="^q$", write="^e$", read="^q$")
    data = store.to_dict()
    restored = AuthStore()
    restored.load_dict(data)
    assert restored.authenticate("alice", "pw") is not None
    assert restored.authorize("alice", "/", "configure", "q")
    assert not restored.authorize("alice", "/", "configure", "other")


def test_default_guest_is_administrator():
    store = default_auth()
    guest = store.authenticate("guest", "guest")
    assert guest is not None
    assert guest.has_tag("administrator")
    assert store.authorize("guest", "/", "configure", "anything")


def test_empty_username_rejected():
    from mini_rabbitmq.errors import InvalidArgument

    store = AuthStore()
    with pytest.raises(InvalidArgument):
        store.add_user("", "pw")
