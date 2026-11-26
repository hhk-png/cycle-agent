"""Persistence adapters."""

from .sqlite_store import SCHEMA_VERSION, SqliteStore

__all__ = ["SqliteStore", "SCHEMA_VERSION"]
