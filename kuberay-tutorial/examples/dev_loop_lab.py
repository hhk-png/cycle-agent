#!/usr/bin/env python3
"""A deterministic model of the interactive development loop around KubeRay.

Run from the tutorial root:

    python examples/dev_loop_lab.py

Getting a notebook or a laptop connected to a Ray cluster is mostly a
packaging and identity problem:

* which files enter ``runtime_env.working_dir`` and which must be excluded;
* why a 600 MiB working directory is worse than a baked image;
* how ``submission_id`` makes retries safe;
* why the Ray Jobs API is the supported path and Ray Client is deprecated;
* how idle notebooks are culled without killing long-running work.

The lab walks each decision with deterministic numbers.  It does not import
Ray, open sockets or read the filesystem.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

MIB = 1024 * 1024


@dataclass(frozen=True)
class File:
    path: str
    content: bytes
    mtime: int = 0


DEFAULT_EXCLUDES = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".ipynb_checkpoints",
)

WORKING_DIR_LIMIT = 512 * MIB


def is_excluded(path: str, excludes: tuple[str, ...]) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(part in excludes for part in parts)


@dataclass
class Package:
    files: list[str]
    size: int
    digest: str
    warnings: list[str] = field(default_factory=list)


def package_working_dir(
    files: list[File],
    *,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    extra_excludes: tuple[str, ...] = (),
    limit: int = WORKING_DIR_LIMIT,
) -> Package:
    """Apply Ray-style working_dir packaging rules.

    mtimes are deliberately excluded from the digest: a ``touch`` must not
    invalidate every runtime_env cache entry.  Paths and bytes are hashed, so
    a real code change does change the digest.
    """
    rules = tuple(excludes) + tuple(extra_excludes)
    kept: list[File] = [item for item in files if not is_excluded(item.path, rules)]
    kept.sort(key=lambda item: item.path)
    digest = hashlib.sha256()
    size = 0
    for item in kept:
        payload = item.path.encode() + b"\0" + hashlib.sha256(item.content).digest()
        digest.update(payload)
        size += len(item.content)
    warnings: list[str] = []
    if size > limit:
        warnings.append(
            f"working_dir is {size / MIB:.0f} MiB (> {limit / MIB:.0f} MiB): "
            "bake dependencies into the image or use a remote URI"
        )
    return Package([item.path for item in kept], size, digest.hexdigest()[:12], warnings)


# ---------------------------------------------------------------------------
# Job submission with idempotency
# ---------------------------------------------------------------------------
class Conflict(Exception):
    pass


@dataclass
class Job:
    submission_id: str
    entrypoint: str
    runtime_env_digest: str
    state: str = "PENDING"


class JobServer:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.submissions = 0

    def submit(self, submission_id: str, entrypoint: str, digest: str) -> Job:
        self.submissions += 1
        existing = self.jobs.get(submission_id)
        if existing is not None:
            if (existing.entrypoint, existing.runtime_env_digest) != (entrypoint, digest):
                raise Conflict(
                    f"submission_id {submission_id!r} already exists with different content"
                )
            return existing
        job = Job(submission_id, entrypoint, digest, "RUNNING")
        self.jobs[submission_id] = job
        return job


# ---------------------------------------------------------------------------
# Connection choices
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Connection:
    name: str
    supported: bool
    note: str


def connection_matrix(inside_cluster: bool, client_version: str, server_version: str) -> list[Connection]:
    jobs = Connection(
        "Ray Jobs API (dashboard :8265)",
        True,
        "HTTP submission; client Ray version does not have to match the server",
    )
    auto = Connection("ray.init(address='auto')", inside_cluster, "only inside a Ray node")
    client = Connection(
        "Ray Client (ray://...:10001)",
        inside_cluster or client_version == server_version,
        "deprecated; requires an exact Ray/Python version match",
    )
    return [jobs, auto, client]


# ---------------------------------------------------------------------------
# Notebook lifecycle
# ---------------------------------------------------------------------------
@dataclass
class NotebookSession:
    name: str
    last_activity: int
    idle_timeout: int = 300
    state: str = "Running"

    def heartbeat(self, now: int) -> None:
        if self.state == "Running":
            self.last_activity = now

    def cull(self, now: int) -> bool:
        if self.state == "Running" and now - self.last_activity >= self.idle_timeout:
            self.state = "Culled"
            return True
        return False


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def demo_tree() -> list[File]:
    return [
        File("src/train.py", b"x" * (2 * MIB), mtime=100),
        File("src/data.py", b"y" * (8 * MIB), mtime=100),
        File("notebooks/explore.ipynb", b"z" * (10 * 1024), mtime=100),
        File(".git/objects/pack.bin", b"g" * (40 * MIB), mtime=100),
        File(".venv/lib/python3.13/site-packages/ray/lib.so", b"v" * (200 * MIB), mtime=100),
        File("data/raw.parquet", b"d" * (600 * MIB), mtime=100),
        File("__pycache__/train.cpython-313.pyc", b"p" * MIB, mtime=100),
    ]


def scenario_packaging() -> None:
    package = package_working_dir(demo_tree(), extra_excludes=("data",))
    # Only src/ and notebooks/ survive; VCS, virtualenv, bytecode and data are
    # excluded.  This is what keeps a runtime_env upload small and repeatable.
    assert package.files == ["notebooks/explore.ipynb", "src/data.py", "src/train.py"]
    assert package.size == 10 * MIB + 10 * 1024
    assert not package.warnings

    # Reordering files and touching mtimes must not change the digest.
    reordered = list(reversed([File(f.path, f.content, mtime=999) for f in demo_tree()]))
    again = package_working_dir(reordered, extra_excludes=("data",))
    assert again.digest == package.digest

    # A one-byte code change must change it.
    changed = [File(f.path, f.content, f.mtime) for f in demo_tree()]
    changed[0] = File("src/train.py", b"x" * (2 * MIB) + b"!", 100)
    assert package_working_dir(changed, extra_excludes=("data",)).digest != package.digest
    print(
        f"[packaging] {len(package.files)} files, {package.size / MIB:.1f} MiB, "
        f"digest={package.digest}; .git/.venv/__pycache__/data excluded"
    )


def scenario_too_large() -> None:
    package = package_working_dir(demo_tree())  # no user exclude for data/
    assert package.size > WORKING_DIR_LIMIT
    assert package.warnings
    print(f"[size] without excluding data/: {package.size / MIB:.0f} MiB -> warning raised")


def scenario_idempotent_submit() -> None:
    server = JobServer()
    first = server.submit("train-2026-10-18", "python src/train.py", "abc123")
    retry = server.submit("train-2026-10-18", "python src/train.py", "abc123")
    assert retry is first
    assert server.submissions == 2
    assert len(server.jobs) == 1
    try:
        server.submit("train-2026-10-18", "python src/other.py", "abc123")
    except Conflict as exc:
        assert "different content" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("a conflicting retry must be rejected")
    print("[idempotency] same submission_id + same content = same job; different content = conflict")


def scenario_connections() -> None:
    inside = connection_matrix(True, "2.59.0", "2.59.0")
    outside = connection_matrix(False, "2.58.0", "2.59.0")
    assert all(item.supported for item in inside)
    jobs, auto, client = outside
    assert jobs.supported
    assert not auto.supported
    assert not client.supported, "mismatched Ray Client versions must fail"
    assert "deprecated" in client.note
    print("[connect] outside: Jobs API works across versions; Ray Client 10001 is deprecated")


def scenario_notebook_cull() -> None:
    session = NotebookSession("alice", last_activity=0)
    session.heartbeat(60)
    session.heartbeat(120)
    assert not session.cull(419)
    assert session.cull(420)
    assert session.state == "Culled"

    busy = NotebookSession("bob", last_activity=0)
    busy.heartbeat(400)
    assert not busy.cull(420)
    print("[notebook] idle culler fires at last_activity+300s; active kernels survive")


def scenario_runtime_env_identity() -> None:
    # The same code in two different projects produces different working_dir
    # digests because the relative paths differ; the image must be the stable
    # part of the identity.
    a = package_working_dir(
        [File("project/train.py", b"print(1)")],
        excludes=(),
    )
    b = package_working_dir(
        [File("train.py", b"print(1)")],
        excludes=(),
    )
    assert a.digest != b.digest
    print("[identity] path layout is part of the runtime_env digest; pin the image for stability")


def main() -> int:
    scenario_packaging()
    scenario_too_large()
    scenario_idempotent_submit()
    scenario_connections()
    scenario_notebook_cull()
    scenario_runtime_env_identity()
    print(
        "OK: the dev loop has a deterministic packaging, submission, connection "
        "and idle-culling contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
