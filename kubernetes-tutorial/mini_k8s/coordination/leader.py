"""Lease-based leader election, the mechanism behind control-plane HA.

A Lease is a small object in the API server.  Candidates race to write their
identity; a candidate that already holds the lease renews it before the expiry
deadline.  Real Kubernetes adds observation of the leader and a release path;
the core compare-and-swap is implemented here.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from mini_k8s.apimachinery.meta import now_iso

LEASE_GVR = ("coordination.k8s.io", "v1", "leases")


class LeaderElector:
    def __init__(self, store, identity: str | None = None, *, lease_name: str = "mini-controller-manager",
                 namespace: str = "kube-system", lease_duration: float = 15.0,
                 renew_deadline: float = 10.0, retry_period: float = 2.0,
                 clock: Callable[[], float] | None = None):
        self.store = store
        self.identity = identity or f"mini-{uuid.uuid4().hex[:8]}"
        self.lease_name = lease_name
        self.namespace = namespace
        self.lease_duration = lease_duration
        self.renew_deadline = renew_deadline
        self.retry_period = retry_period
        self.clock = clock or time.time

    def _get(self) -> dict | None:
        return self.store.get(LEASE_GVR, self.namespace, self.lease_name)

    def is_leader(self) -> bool:
        lease = self._get()
        return bool(lease and lease.get("spec", {}).get("holderIdentity") == self.identity and not self._expired(lease))

    def _expired(self, lease: dict, now: float | None = None) -> bool:
        renew_time = lease.get("spec", {}).get("renewTime")
        if not renew_time:
            return True
        try:
            from datetime import datetime
            moment = datetime.fromisoformat(renew_time.replace("Z", "+00:00"))
            now = self.clock() if now is None else now
            return now - moment.timestamp() > float(lease.get("spec", {}).get("leaseDurationSeconds", 15))
        except ValueError:
            return True

    def try_acquire_or_renew(self) -> bool:
        now = self.clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        lease = self._get()
        if lease is None:
            body = {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": self.lease_name, "namespace": self.namespace},
                "spec": {
                    "holderIdentity": self.identity,
                    "leaseDurationSeconds": int(self.lease_duration),
                    "acquireTime": stamp,
                    "renewTime": stamp,
                    "leaseTransitions": 0,
                },
            }
            try:
                self.store.create(LEASE_GVR, body, kind="Lease", namespace=self.namespace)
                return True
            except Exception:
                # Another candidate won the create race; fall through to update.
                lease = self._get()
                if lease is None:
                    return False
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity")
        expired = self._expired(lease, now)
        if holder == self.identity:
            updated = dict(lease)
            updated["spec"] = {**spec, "renewTime": stamp}
            self.store.update(LEASE_GVR, updated, kind="Lease", namespace=self.namespace)
            return True
        if expired or not holder:
            updated = dict(lease)
            updated["spec"] = {
                **spec,
                "holderIdentity": self.identity,
                "acquireTime": stamp,
                "renewTime": stamp,
                "leaseTransitions": int(spec.get("leaseTransitions", 0)) + 1,
            }
            try:
                self.store.update(LEASE_GVR, updated, kind="Lease", namespace=self.namespace)
                return True
            except Exception:
                return False
        return False

    def release(self) -> None:
        lease = self._get()
        if lease and lease.get("spec", {}).get("holderIdentity") == self.identity:
            updated = dict(lease)
            updated["spec"] = {**lease["spec"], "holderIdentity": "", "renewTime": now_iso()}
            self.store.update(LEASE_GVR, updated, kind="Lease", namespace=self.namespace)
