"""Local artifact repository with containment and atomic writes.

Artifact URIs in MiniMLflow are ``file://...`` URIs.  Each experiment gets a
directory under the configured artifact root; each run gets a fresh directory:

    <root>/<experiment_id>/<run_id>/artifacts/

The content tree is intentionally human-readable.  Production artifact stores
often separate the namespace tree from a content-addressed blob store, which
enables deduplication and cheap garbage collection; that design is discussed
in the tutorial, while this implementation favors inspectability.

Security properties
-------------------
* Every untrusted path goes through :func:`safe_join`.
* Writes are atomic (temp file + ``os.replace``) so a crash cannot leave a
  partially written file under its final name.
* Directory traversal and absolute paths are rejected even if the caller
  bypasses the HTTP layer.
* The repository refuses to write outside the configured artifact root, which
  bounds the blast radius of a compromised server.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import MiniConfig
from .entities import FileInfo
from .errors import InvalidParameter, ResourceNotFound
from .utils import (
    atomic_write_bytes,
    atomic_write_text,
    copy_file_atomic,
    file_uri_to_path,
    pretty_json,
    safe_join,
)


class ArtifactRepository:
    def __init__(self, config: MiniConfig) -> None:
        self.root = config.artifact_root_path
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def experiment_location(self, experiment_id: str) -> str:
        path = self.root / str(experiment_id)
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve().as_uri()

    def validate_experiment_location(self, location: str) -> str:
        path = file_uri_to_path(location)
        self._ensure_within_root(path)
        return path.resolve().as_uri()

    def new_run_artifact_uri(self, experiment_location: str, run_id: str) -> str:
        location = file_uri_to_path(self.validate_experiment_location(experiment_location))
        run_dir = self._ensure_within_root(location / run_id / "artifacts")
        return run_dir.as_uri()

    def run_dir(self, artifact_uri: str) -> Path:
        path = file_uri_to_path(artifact_uri)
        self._ensure_within_root(path)
        return path

    def _ensure_within_root(self, path: str | Path) -> Path:
        resolved = Path(path).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise InvalidParameter(
                f"artifact path {resolved!s} is outside the configured artifact root {self.root!s}"
            ) from exc
        return resolved

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def log_artifact(
        self,
        artifact_uri: str,
        local_path: str | Path,
        artifact_path: str | None = None,
    ) -> str:
        source = Path(local_path)
        if not source.exists():
            raise ResourceNotFound(f"local artifact not found: {source}")
        if not source.is_file():
            raise InvalidParameter(f"log_artifact expects a file, got {source}")
        run_dir = self.run_dir(artifact_uri)
        prefix = self._normalize_prefix(artifact_path)
        destination = safe_join(run_dir / prefix if prefix else run_dir, source.name)
        copy_file_atomic(source, destination)
        return destination.relative_to(run_dir).as_posix()

    def log_bytes(
        self,
        artifact_uri: str,
        relative_path: str,
        data: bytes,
        *,
        overwrite: bool = True,
    ) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise InvalidParameter("artifact content must be bytes")
        run_dir = self.run_dir(artifact_uri)
        normalized = self._normalize_relative_file(relative_path)
        destination = safe_join(run_dir, normalized)
        if destination.exists() and not overwrite:
            raise InvalidParameter(f"artifact {normalized!r} already exists")
        atomic_write_bytes(destination, bytes(data))
        return destination.relative_to(run_dir).as_posix()

    def log_text(self, artifact_uri: str, text: str, artifact_file: str) -> str:
        if not isinstance(text, str):
            raise InvalidParameter("artifact text must be a string")
        return self.log_bytes(artifact_uri, artifact_file, text.encode("utf-8"))

    def log_dict(self, artifact_uri: str, dictionary: object, artifact_file: str) -> str:
        return self.log_text(artifact_uri, pretty_json(dictionary), artifact_file)

    def log_artifacts(
        self,
        artifact_uri: str,
        local_dir: str | Path,
        artifact_path: str | None = None,
    ) -> list[str]:
        source = Path(local_dir)
        if not source.exists() or not source.is_dir():
            raise ResourceNotFound(f"local artifact directory not found: {source}")
        run_dir = self.run_dir(artifact_uri)
        prefix = self._normalize_prefix(artifact_path)
        base = run_dir / prefix if prefix else run_dir
        written: list[str] = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(source)
            destination = safe_join(base, relative.as_posix())
            copy_file_atomic(path, destination)
            written.append(destination.relative_to(run_dir).as_posix())
        return written

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_artifacts(self, artifact_uri: str, path: str | None = None) -> list[FileInfo]:
        run_dir = self.run_dir(artifact_uri)
        normalized = (path or "").strip().strip("/")
        target = safe_join(run_dir, normalized) if normalized else run_dir
        if not target.exists():
            return []
        if target.is_file():
            return [
                FileInfo(
                    path=target.relative_to(run_dir).as_posix(),
                    is_dir=False,
                    file_size=target.stat().st_size,
                )
            ]
        result: list[FileInfo] = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name.startswith(".tmp-"):
                continue
            result.append(
                FileInfo(
                    path=child.relative_to(run_dir).as_posix(),
                    is_dir=child.is_dir(),
                    file_size=0 if child.is_dir() else child.stat().st_size,
                )
            )
        return result

    def read_bytes(self, artifact_uri: str, relative_path: str) -> bytes:
        run_dir = self.run_dir(artifact_uri)
        target = safe_join(run_dir, relative_path)
        if not target.is_file():
            raise ResourceNotFound(f"artifact file not found: {relative_path}")
        return target.read_bytes()

    def artifact_path(self, artifact_uri: str, relative_path: str) -> Path:
        run_dir = self.run_dir(artifact_uri)
        target = safe_join(run_dir, relative_path)
        if not target.exists():
            raise ResourceNotFound(f"artifact path not found: {relative_path}")
        return target

    def download_artifacts(
        self,
        artifact_uri: str,
        path: str | None,
        dst_path: str | Path,
    ) -> Path:
        run_dir = self.run_dir(artifact_uri)
        relative = (path or "").strip().strip("/")
        source = safe_join(run_dir, relative) if relative else run_dir
        if not source.exists():
            raise ResourceNotFound(f"artifact path not found: {path!r}")
        destination = Path(dst_path)
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            target = destination / source.name
            copy_file_atomic(source, target)
            return target
        for child in source.rglob("*"):
            if child.is_symlink() or not child.is_file():
                continue
            child_relative = child.relative_to(source)
            copy_file_atomic(child, destination / child_relative)
        return destination

    def delete_artifacts(self, artifact_uri: str, path: str | None = None) -> None:
        run_dir = self.run_dir(artifact_uri)
        relative = (path or "").strip().strip("/")
        if not relative:
            if run_dir.exists():
                shutil.rmtree(run_dir)
            return
        target = safe_join(run_dir, relative)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise ResourceNotFound(f"artifact path not found: {relative}")

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_prefix(prefix: str | None) -> str:
        if prefix is None:
            return ""
        text = str(prefix).strip().replace("\\", "/")
        if not text:
            return ""
        # Run the lexical/containment checks before any normalization that
        # could hide an absolute path or traversal attempt.
        safe_join(".", text)
        return text.strip("/")

    @staticmethod
    def _normalize_relative_file(relative_path: str) -> str:
        text = str(relative_path).strip().replace("\\", "/")
        if not text:
            raise InvalidParameter("artifact relative path must not be empty")
        if text.endswith("/"):
            raise InvalidParameter("artifact relative path must name a file")
        if text.startswith("/"):
            raise InvalidParameter("artifact path must be relative")
        if re.match(r"^[A-Za-z]:", text):
            raise InvalidParameter("artifact path must not include a drive letter")
        if any(part == ".." for part in PurePosixPath(text).parts):
            raise InvalidParameter("artifact path must not contain '..'")
        safe_join(".", text)
        return text
