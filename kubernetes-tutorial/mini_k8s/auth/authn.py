"""Authentication: static bearer tokens and educational service-account JWTs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from mini_k8s.errors import unauthorized


@dataclass
class User:
    name: str
    groups: list[str] = field(default_factory=list)
    extra: dict[str, list[str]] = field(default_factory=dict)
    authenticated: bool = True

    @property
    def is_service_account(self) -> bool:
        return self.name.startswith("system:serviceaccount:")

    def service_account(self) -> tuple[str, str] | None:
        if not self.is_service_account:
            return None
        parts = self.name.split(":")
        if len(parts) != 4:
            return None
        return parts[2], parts[3]


ANONYMOUS = User("system:anonymous", ["system:unauthenticated"], authenticated=False)
SYSTEM_MASTERS = "system:masters"


class StaticTokenAuthenticator:
    def __init__(self, tokens: dict[str, User | dict[str, Any]] | None = None):
        self.tokens: dict[str, User] = {}
        for token, user in (tokens or {}).items():
            if isinstance(user, User):
                self.tokens[token] = user
            else:
                self.tokens[token] = User(user.get("username", "unknown"), list(user.get("groups", [])))

    def authenticate(self, authorization: str | None) -> User:
        if not authorization:
            return ANONYMOUS
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise unauthorized("unsupported authorization header")
        user = self.tokens.get(token)
        if user is None:
            raise unauthorized("the server has asked for the client to provide credentials")
        return user


class ServiceAccountTokenIssuer:
    """A compact HMAC-signed token format.

    It is intentionally *not* a complete JWT implementation: it demonstrates
    the claims, audience and binding model without importing a crypto library.
    Production Kubernetes uses RSA/ECDSA-signed JWTs and OIDC discovery.
    """

    def __init__(self, secret: bytes, *, issuer: str = "https://mini-k8s.local", ttl: int = 3600):
        self.secret = secret
        self.issuer = issuer
        self.ttl = ttl

    @staticmethod
    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @staticmethod
    def _unb64(raw: str) -> bytes:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded.encode())

    def _sign(self, body: str) -> str:
        return self._b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())

    def issue(self, namespace: str, name: str, *, audiences: list[str] | None = None,
              expiration_seconds: int | None = None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        exp = int(now) + (expiration_seconds or self.ttl)
        header = {"alg": "HS256", "typ": "JWT"}
        claims = {
            "iss": self.issuer,
            "sub": f"system:serviceaccount:{namespace}:{name}",
            "aud": audiences or ["https://kubernetes.default.svc.cluster.local"],
            "iat": int(now),
            "nbf": int(now),
            "exp": exp,
            "kubernetes.io": {
                "namespace": namespace,
                "serviceaccount": {"name": name, "uid": f"sa-{namespace}-{name}"},
            },
        }
        body = self._b64(json.dumps(header, separators=(",", ":")).encode()) + "." + self._b64(
            json.dumps(claims, separators=(",", ":")).encode()
        )
        token = body + "." + self._sign(body)
        return {"token": token, "expirationTimestamp": _rfc3339(exp), "claims": claims}

    def validate(self, token: str, *, audience: str | None = None, now: float | None = None) -> User:
        parts = token.split(".")
        if len(parts) != 3:
            raise unauthorized("invalid service account token")
        body = parts[0] + "." + parts[1]
        if not hmac.compare_digest(self._sign(body), parts[2]):
            raise unauthorized("service account token signature is invalid")
        try:
            claims = json.loads(self._unb64(parts[1]))
        except (ValueError, json.JSONDecodeError) as exc:
            raise unauthorized("invalid service account token claims") from exc
        now = time.time() if now is None else now
        if claims.get("exp", 0) < now:
            raise unauthorized("service account token has expired")
        if claims.get("nbf", 0) > now + 60:
            raise unauthorized("service account token is not valid yet")
        if audience and audience not in claims.get("aud", []):
            raise unauthorized("service account token audience mismatch")
        sub = claims.get("sub", "")
        kubernetes = claims.get("kubernetes.io", {})
        namespace = kubernetes.get("namespace", "")
        groups = ["system:serviceaccounts", "system:authenticated"]
        if namespace:
            groups.append(f"system:serviceaccounts:{namespace}")
        return User(sub, groups)


def _rfc3339(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass
class AuthenticatorChain:
    token_authenticator: StaticTokenAuthenticator
    sa_issuer: ServiceAccountTokenIssuer | None = None

    def authenticate(self, authorization: str | None) -> User:
        try:
            return self.token_authenticator.authenticate(authorization)
        except Exception:
            if self.sa_issuer is None:
                raise
            _, _, token = (authorization or "").partition(" ")
            if not token:
                raise
            return self.sa_issuer.validate(token)
