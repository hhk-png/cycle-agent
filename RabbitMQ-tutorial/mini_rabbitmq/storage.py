"""持久化快照。

这是教学实现：用原子替换的 JSON 快照代替 RabbitMQ 的 WAL + 队列段文件 +
Quorum Raft 日志。它诚实展示“确认/落盘/恢复”边界，但不追求生产吞吐。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class SnapshotStore:
    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            from .errors import InternalError

            raise InternalError(f"cannot load snapshot {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            from .errors import InternalError

            raise InternalError("snapshot root must be a JSON object")
        return data

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class NullStore:
    def load(self) -> None:
        return None

    def save(self, payload: dict[str, Any]) -> None:
        del payload

    def delete(self) -> None:
        return None
