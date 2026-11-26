"""Prompt Registry business rules.

A prompt is a versioned *asset*, not a string constant:

* versions are immutable, so an evaluation report can name the exact bytes;
* aliases (``production``, ``canary``) are mutable pointers, so rollout and
  rollback are one-row operations;
* tags carry ownership, model parameters and review state without changing
  the template;
* rendering is a constrained substitution, never ``eval`` or unrestricted
  ``str.format``.

The storage layer owns SQL.  This module owns naming, template validation,
URI parsing and the resolution order (explicit version > alias > latest).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping, Sequence

from .entities import PromptVersion
from .errors import InvalidParameter, ResourceNotFound, SearchSyntaxError
from .search import Field, evaluate_ast, parse_filter
from .store.sqlite_store import SqliteStore
from .utils import coerce_tag_value, validate_key, validate_name

LOGGER = logging.getLogger("mini_mlflow.prompts")

_PROMPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]{0,127}$")
_MAX_TEMPLATE_BYTES = 1024 * 1024


def validate_prompt_name(name: str) -> str:
    value = validate_name(name, what="prompt name", max_len=128)
    if not _PROMPT_NAME_RE.fullmatch(value):
        raise InvalidParameter(
            "prompt names must start with a letter or digit and contain only "
            "letters, digits, '_', '-' and '.' (max 128 characters)"
        )
    return value


def validate_prompt_alias(alias: str) -> str:
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        raise InvalidParameter(
            "prompt aliases must start with a letter or underscore and contain only "
            "letters, digits, '_', '-' and '.' (max 128 characters)"
        )
    if alias.isdigit():
        raise InvalidParameter("a prompt alias cannot be a bare number")
    return alias


def validate_template(template: str) -> str:
    if not isinstance(template, str):
        raise InvalidParameter("prompt template must be a string")
    if not template.strip():
        raise InvalidParameter("prompt template must not be empty")
    size = len(template.encode("utf-8"))
    if size > _MAX_TEMPLATE_BYTES:
        raise InvalidParameter(f"prompt template exceeds {_MAX_TEMPLATE_BYTES} bytes")
    # We deliberately do not implement Jinja control flow.  Better to reject
    # it than to silently render a template that looks dynamic but is not.
    if "{%" in template or "{#" in template:
        raise InvalidParameter(
            "prompt templates support {{ variable }} substitution only; "
            "control structures ({% ... %} / {# ... #}) are not supported"
        )
    return template


class PromptRegistry:
    def __init__(
        self,
        store: SqliteStore,
        *,
        emit: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.store = store
        self._emit_callback = emit

    def _emit(self, event: str, data: Mapping[str, Any]) -> None:
        if self._emit_callback is None:
            return
        try:
            self._emit_callback(event, dict(data))
        except Exception:  # noqa: BLE001 - notification boundary
            LOGGER.exception("failed to enqueue webhook event %s", event)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def register_prompt(
        self,
        name: str,
        template: str,
        *,
        commit_message: str = "",
        tags: Mapping[str, Any] | None = None,
    ) -> PromptVersion:
        name = validate_prompt_name(name)
        template = validate_template(template)
        if len(commit_message or "") > 64 * 1024:
            raise InvalidParameter("commit_message is too long (max 64 KiB)")
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in (tags or {}).items()
        }
        try:
            self.store.get_prompt_row(name)
            created = False
        except ResourceNotFound:
            created = True
        version = self.store.create_prompt_version(
            name,
            template,
            commit_message=str(commit_message or ""),
            tags=clean_tags,
        )
        if created:
            self._emit("prompt.created", {"name": name})
        self._emit(
            "prompt_version.created",
            {
                "name": version.name,
                "version": str(version.version),
                "template": version.template,
                "commit_message": version.commit_message,
                "tags": dict(version.tags),
            },
        )
        return version

    def set_alias(self, name: str, alias: str, version: int | str) -> None:
        name = validate_prompt_name(name)
        alias = validate_prompt_alias(alias)
        try:
            parsed = int(version)
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("prompt version must be an integer") from exc
        self.store.set_prompt_alias(name, alias, parsed)
        self._emit(
            "prompt_alias.created",
            {"name": name, "alias": alias, "version": str(parsed)},
        )

    def delete_alias(self, name: str, alias: str) -> None:
        name = validate_prompt_name(name)
        alias = validate_prompt_alias(alias)
        self.store.delete_prompt_alias(name, alias)
        self._emit("prompt_alias.deleted", {"name": name, "alias": alias})

    def delete_prompt(self, name: str) -> None:
        name = validate_prompt_name(name)
        self.store.delete_prompt(name)
        self._emit("prompt.deleted", {"name": name})

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get(self, name: str, version: int | str | None = None) -> PromptVersion:
        name = validate_prompt_name(name)
        if version is None:
            return self.store.get_latest_prompt_version(name)
        return self.store.get_prompt_version(name, version)

    def get_by_alias(self, name: str, alias: str) -> PromptVersion:
        return self.store.get_prompt_version_by_alias(
            validate_prompt_name(name), validate_prompt_alias(alias)
        )

    def load(self, uri: str, version: int | str | None = None) -> PromptVersion:
        """Resolve ``prompts:/name/3``, ``prompts:/name@production`` or ``name``.

        Resolution order is: explicit ``version`` argument, ``@alias`` in the
        URI, numeric trailing segment, then the latest version.  A bare name
        never means "whatever was newest when the service started"; it means
        "newest at read time", which is why production readers should pin an
        alias or a version.
        """
        if not isinstance(uri, str) or not uri.strip():
            raise InvalidParameter("prompt URI must be a non-empty string")
        text = uri.strip()
        if text.startswith("prompts:/"):
            text = text[len("prompts:/") :]
        name = text
        alias: str | None = None
        if "@" in text:
            name, alias = text.split("@", 1)
            if not name or not alias:
                raise InvalidParameter("prompt URI alias must look like name@alias")
        elif "/" in text:
            prefix, _, suffix = text.rpartition("/")
            if prefix and suffix.isdigit():
                name = prefix
                if version is None:
                    version = int(suffix)
            else:
                raise InvalidParameter("prompt URI path must be name/version or name@alias")
        if version is not None and alias is not None:
            raise InvalidParameter("pass a version or an alias, not both")
        if alias is not None:
            return self.get_by_alias(name, alias)
        return self.get(name, version)

    def list_versions(self, name: str) -> list[PromptVersion]:
        return self.store.list_prompt_versions(validate_prompt_name(name))

    def list_prompts(self) -> list[str]:
        return self.store.list_prompt_names()

    def search_prompts(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[PromptVersion]:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise InvalidParameter("max_results must be a positive integer")
        ast = parse_filter(filter_string)
        latest: list[PromptVersion] = []
        for name in self.store.list_prompt_names():
            try:
                latest.append(self.store.get_latest_prompt_version(name))
            except ResourceNotFound:
                continue
        if ast is not None:
            filtered: list[PromptVersion] = []
            for item in latest:
                def resolver(field: Field, prompt: PromptVersion = item) -> Any:
                    if field.scope == "attributes":
                        key = field.key.lower()
                        mapping = {
                            "name": prompt.name,
                            "version": prompt.version,
                            "creation_timestamp": prompt.creation_timestamp,
                            "commit_message": prompt.commit_message,
                        }
                        if key not in mapping:
                            raise SearchSyntaxError(f"unknown prompt attribute {field.key!r}")
                        return mapping[key]
                    if field.scope == "tags":
                        return prompt.tags.get(field.key)
                    raise SearchSyntaxError("prompt filters support attributes.* and tags.* only")

                if evaluate_ast(ast, resolver):
                    filtered.append(item)
            latest = filtered
        if order_by:
            for raw in reversed(list(order_by)):
                text = str(raw).strip()
                match = re.fullmatch(
                    r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE
                )
                if not match:
                    raise InvalidParameter(f"invalid prompt order_by entry {raw!r}")
                key = match.group(1).removeprefix("attributes.")
                if key not in {"name", "version", "creation_timestamp"}:
                    raise InvalidParameter(f"cannot order prompts by {match.group(1)!r}")
                latest.sort(
                    key=lambda item, attr=key: getattr(item, attr),
                    reverse=(match.group(2) or "ASC").upper() == "DESC",
                )
        else:
            latest.sort(key=lambda item: (item.name.lower(), item.name))
        return latest[:max_results]
