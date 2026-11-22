"""Tool layer: schemas, lifecycle, function tools and a registry.

This mirrors the contracts in ``verl/tools/``:

* ``BaseTool`` owns a per-trajectory instance and implements
  ``create -> execute* -> calc_reward -> release``;
* ``FunctionTool`` turns a plain Python callable into a tool whose schema is
  inferred from its signature;
* ``ToolRegistry`` merges native tools and function tools, rejects name
  collisions, and exports OpenAI-format schemas for prompt assembly.

The implementation is deliberately synchronous and in-process.  Production
verl runs tools behind an async agent loop and a sandbox; the chapter and
appendix explain where those boundaries are and what they cost.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import re
import time
import types
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, get_args, get_origin

__all__ = [
    "ToolSchema",
    "ToolResult",
    "ToolInstance",
    "BaseTool",
    "Tool",
    "FunctionTool",
    "function_tool",
    "CalculatorTool",
    "ToolRegistry",
    "ToolCollisionError",
]


class ToolCollisionError(ValueError):
    """Raised when two different tools claim the same name."""


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    """Map a Python annotation to a JSON-schema fragment.

    Handles the PEP 604 / ``typing`` forms that show up in real tool
    signatures: ``Optional[int]``, ``int | None``, ``list[int]``,
    ``tuple[int, int]``, ``Literal["a", "b"]``.  Unknown annotations degrade to
    a free-form string rather than producing an invalid schema.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    if isinstance(annotation, str):
        return _string_annotation_to_schema(annotation)

    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        schemas = [_annotation_to_schema(member) for member in members]
        types_found: list[str] = []
        for schema in schemas:
            value = schema.get("type")
            if isinstance(value, str) and value not in types_found:
                types_found.append(value)
        if not types_found:
            return {"type": "string"}
        return {"type": types_found[0] if len(types_found) == 1 else types_found}
    if origin is typing.Literal:
        values = list(get_args(annotation))
        element = _annotation_to_schema(type(values[0])) if values else {"type": "string"}
        return {"type": element.get("type", "string"), "enum": values}
    if origin in (list, tuple, set, frozenset):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return {"type": _JSON_TYPES.get(annotation, "string")}


def _string_annotation_to_schema(text: str) -> dict[str, Any]:
    """Best-effort schema for annotations that were not resolvable at runtime."""
    text = text.strip().strip("'\"")
    while text.startswith("Annotated[") and text.endswith("]"):
        text = text[len("Annotated[") : -1].split(",", 1)[0].strip()
    for prefix in ("Optional[", "Union["):
        if text.startswith(prefix) and text.endswith("]"):
            inner = text[len(prefix) : -1]
            parts = _split_top_level(inner)
            return _annotation_to_schema(" | ".join(part for part in parts if part not in {"None", "NoneType"}))
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if part.strip() not in {"None", "NoneType"}]
        if len(parts) == 1:
            return _string_annotation_to_schema(parts[0])
        schemas = [_string_annotation_to_schema(part) for part in parts]
        types_found: list[str] = []
        for schema in schemas:
            value = schema.get("type")
            if isinstance(value, str) and value not in types_found:
                types_found.append(value)
        return {"type": types_found[0] if len(types_found) == 1 else types_found}
    lowered = text.lower()
    if lowered.startswith(("list[", "list ", "tuple[", "tuple ", "set[", "set ", "sequence[", "iterable[")):
        return {"type": "array"}
    if lowered.startswith(("dict[", "dict ", "mapping[", "optional[", "union[")):
        return {"type": "object" if lowered.startswith(("dict", "mapping")) else "string"}
    mapping = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "tuple": "array",
        "set": "array",
        "dict": "object",
        "any": "string",
    }
    return {"type": mapping.get(lowered, "string")}


_TOOL_NAME_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(name: str | None, fallback: str = "tool") -> str:
    """OpenAI function names allow ``[a-zA-Z0-9_-]``, up to 64 chars."""
    candidate = _TOOL_NAME_BAD.sub("_", str(name or fallback)).strip("_")
    if not candidate:
        candidate = fallback
    if candidate[0].isdigit():
        candidate = f"tool_{candidate}"
    return candidate[:64]


def _split_top_level(text: str) -> list[str]:
    """Split ``a, list[b, c]`` on the top-level commas only."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


@dataclass
class ToolSchema:
    """A minimal OpenAI function-tool schema.

    The shape follows ``verl/tools/schemas.py``::

        {"type": "function",
         "function": {"name": ..., "description": ...,
                      "parameters": {"type": "object", "properties": {...},
                                     "required": [...]}}}
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }

    @classmethod
    def from_callable(cls, fn: Callable[..., Any], name: str | None = None) -> "ToolSchema":
        signature = inspect.signature(fn)
        # ``from __future__ import annotations`` stores annotations as strings.
        # Resolve them when possible so Optional[int] / int | None / list[int]
        # become real types instead of falling back to "string".
        try:
            hints = typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001 - local/forward refs may not resolve
            hints = {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                raise TypeError("function tools cannot expose *args/**kwargs")
            annotation = hints.get(parameter.name, parameter.annotation)
            properties[parameter.name] = _annotation_to_schema(annotation)
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
        doc = inspect.getdoc(fn) or ""
        description = doc.strip().splitlines()[0] if doc.strip() else ""
        return cls(
            name=name or fn.__name__,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
        )


# ---------------------------------------------------------------------------
# results and instances
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """What a tool returns to the agent loop.

    ``reward`` is a *step* reward (verl's ``BaseTool.execute`` returns one);
    it is not the terminal task reward and should be small in magnitude unless
    the experiment deliberately trades outcome reward for process reward.
    """

    text: str = ""
    reward: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.text = "" if self.text is None else str(self.text)
        self.reward = float(self.reward)


@dataclass
class ToolInstance:
    instance_id: str
    created_at: float
    state: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class BaseTool:
    """Lifecycle-shaped tool.

    A tool instance is created once per trajectory and released when the
    trajectory ends.  Keeping state on the instance (rather than on the tool)
    is what lets one process serve many concurrent trajectories.
    """

    name = "tool"
    description = ""

    def __init__(self, config: dict[str, Any] | None = None, schema: ToolSchema | None = None):
        self.config = dict(config or {})
        self.schema = schema or ToolSchema(name=self.name, description=self.description)
        self._instances: dict[str, ToolInstance] = {}

    # -- lifecycle ---------------------------------------------------------
    def create(self, instance_id: str | None = None, **kwargs: Any) -> tuple[str, ToolResult]:
        instance_id = instance_id or str(uuid.uuid4())
        if instance_id in self._instances:
            raise ValueError(f"tool instance already exists: {instance_id}")
        self._instances[instance_id] = ToolInstance(instance_id=instance_id, created_at=time.time())
        return instance_id, ToolResult()

    def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    def calc_reward(self, instance_id: str, **kwargs: Any) -> float:
        return 0.0

    def release(self, instance_id: str, **kwargs: Any) -> None:
        self._instances.pop(instance_id, None)

    # -- legacy convenience ------------------------------------------------
    def __call__(self, argument: str) -> str:
        """Single-shot call used by older MiniVerl examples and tests."""
        instance_id, _ = self.create()
        try:
            return self.execute(instance_id, {"expression": argument}).text
        finally:
            self.release(instance_id)

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return self.schema.to_openai()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(name={self.name!r})"


# Backwards-compatible name used by ``mini_verl.agent`` before the tool layer
# grew its lifecycle.  ``Tool`` is the old abstract base; ``BaseTool`` is the
# one new code should subclass.
Tool = BaseTool


class CalculatorTool(BaseTool):
    """Safe arithmetic evaluator used by the agent examples."""

    name = "calculator"
    description = "Evaluate a basic arithmetic expression."

    _DEFAULT_SCHEMA = ToolSchema(
        name="calculator",
        description="Evaluate a basic arithmetic expression.",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression, for example '(3+4)*2'.",
                }
            },
            "required": ["expression"],
        },
    )

    _OPS = {
        "Add": lambda a, b: a + b,
        "Sub": lambda a, b: a - b,
        "Mult": lambda a, b: a * b,
        "Div": lambda a, b: a / b,
        "Pow": lambda a, b: a**b,
        "USub": lambda a: -a,
        "UAdd": lambda a: +a,
    }

    def __init__(self, config: dict[str, Any] | None = None, schema: ToolSchema | None = None):
        # Copy the default so renaming one instance cannot mutate the class-level
        # schema shared by every other CalculatorTool.
        default = ToolSchema(
            name=self._DEFAULT_SCHEMA.name,
            description=self._DEFAULT_SCHEMA.description,
            parameters=copy.deepcopy(self._DEFAULT_SCHEMA.parameters),
        )
        super().__init__(config=config, schema=schema or default)

    def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs: Any) -> ToolResult:
        expression = str(parameters.get("expression", parameters.get("argument", "")))
        try:
            tree = ast.parse(expression, mode="eval")
            value = self._eval_node(tree.body)
            # Formatting can fail too (e.g. Python's int->str digit limit).
            text = self._format(value)
        except Exception:
            return ToolResult(text="error", reward=0.0)
        return ToolResult(text=text)

    def __call__(self, expression: str) -> str:
        return self.execute("inline", {"expression": expression}).text

    # The evaluator itself is a tiny AST walker: no names, calls, attributes or
    # comprehensions.  ``execute`` parses first so that syntax errors are
    # ordinary exceptions rather than process exits.
    _MAX_EXPONENT = 1000
    _MAX_BITS = 10_000

    def _eval_node(self, node: Any) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op).__name__ in self._OPS:
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            if isinstance(node.op, ast.Pow):
                self._check_pow(left, right)
            value = self._OPS[type(node.op).__name__](left, right)
            return self._bounded(value)
        if isinstance(node, ast.UnaryOp) and type(node.op).__name__ in self._OPS:
            return self._OPS[type(node.op).__name__](self._eval_node(node.operand))
        raise ValueError("unsupported expression")

    def _check_pow(self, base: Any, exponent: Any) -> None:
        """Bound exponentiation before it allocates a huge integer.

        ``9**9**9`` evaluates the exponent first; without this check Python
        starts a multi-gigabyte integer and the agent loop never returns.
        """
        if not isinstance(base, (int, float)) or not isinstance(exponent, (int, float)):
            raise ValueError("pow requires numeric operands")
        if isinstance(exponent, int) and abs(exponent) > self._MAX_EXPONENT:
            raise ValueError("exponent too large")
        if isinstance(base, int) and base.bit_length() > self._MAX_BITS:
            raise ValueError("base too large")

    def _bounded(self, value: Any) -> Any:
        if isinstance(value, int) and value.bit_length() > self._MAX_BITS:
            raise ValueError("result too large")
        return value

    @staticmethod
    def _format(value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)


class FunctionTool(BaseTool):
    """Wrap a plain callable, inferring its JSON schema from the signature.

    ``timeout`` is cooperative: the call runs in a worker thread and the
    caller stops waiting after the deadline, but Python cannot kill a running
    thread.  Production sandboxes enforce real timeouts at the process/container
    boundary; see Appendix T.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        timeout: float | None = None,
    ):
        schema = ToolSchema.from_callable(fn, name=name)
        schema.name = _sanitize_tool_name(schema.name, fallback="tool")
        if description is not None:
            schema.description = description
        super().__init__(schema=schema)
        self.name = schema.name
        self.fn = fn
        self.timeout = timeout
        self.is_async = inspect.iscoroutinefunction(fn)
        self._signature = inspect.signature(fn)
        self._executor = ThreadPoolExecutor(max_workers=1) if timeout is not None else None

    def _call(self, parameters: dict[str, Any]) -> Any:
        """Invoke with positional-only parameters reconstructed as positionals."""
        unknown = set(parameters) - set(self._signature.parameters)
        if unknown:
            raise TypeError(f"unexpected parameters: {sorted(unknown)}")
        positional: list[Any] = []
        keyword: dict[str, Any] = {}
        for name, parameter in self._signature.parameters.items():
            if name not in parameters:
                continue
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(parameters[name])
            else:
                keyword[name] = parameters[name]
        return self.fn(*positional, **keyword)

    @staticmethod
    def _error(exc: BaseException) -> ToolResult:
        return ToolResult(
            text=f"error: {type(exc).__name__}",
            reward=0.0,
            metrics={"tool/error": 1.0},
        )

    def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs: Any) -> ToolResult:
        if self.is_async:
            raise TypeError("async function tools require an async agent loop")
        started = time.perf_counter()
        if self._executor is not None:
            # Capture exceptions *inside* the worker.  Then a FutureTimeout
            # raised by result() can only mean the deadline expired, not that
            # the tool itself raised TimeoutError (they share a type on modern
            # Python).
            def invoke() -> tuple[bool, Any]:
                try:
                    return True, self._call(parameters)
                except Exception as exc:  # noqa: BLE001
                    return False, exc

            try:
                future = self._executor.submit(invoke)
            except Exception as exc:  # noqa: BLE001 - executor may be shut down
                return self._error(exc)
            try:
                ok, value = future.result(timeout=self.timeout)
            except FutureTimeout:
                return ToolResult(
                    text="timeout",
                    reward=0.0,
                    metrics={"tool/timeout": 1.0},
                )
            if not ok:
                return self._error(value)
        else:
            try:
                value = self._call(parameters)
            except Exception as exc:  # noqa: BLE001 - tools must not kill the loop
                return self._error(exc)
        elapsed = time.perf_counter() - started
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - non-serializable return value
            return self._error(exc)
        return ToolResult(text=text, metrics={"tool/latency_ms": elapsed * 1000.0})

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        names = list(inspect.signature(self.fn).parameters)
        if len(args) > len(names):
            raise TypeError(f"{self.name} takes at most {len(names)} positional arguments")
        parameters = dict(zip(names, args))
        parameters.update(kwargs)
        doubled = set(names[: len(args)]) & set(kwargs)
        if doubled:
            raise TypeError(f"{self.name} got multiple values for {sorted(doubled)}")
        return self.execute("inline", parameters).text


def function_tool(
    name: str | Callable[..., Any] | None = None,
    *,
    description: str | None = None,
    timeout: float | None = None,
) -> Any:
    """Decorator form of :class:`FunctionTool`.

    Supports ``@function_tool``, ``@function_tool("name")`` and
    ``@function_tool(name="name", timeout=...)``.
    """

    def decorate(fn: Callable[..., Any], tool_name: str | None) -> Callable[..., Any]:
        tool = FunctionTool(fn, name=tool_name, description=description, timeout=timeout)
        fn.tool = tool  # type: ignore[attr-defined]
        return fn

    if callable(name):
        return decorate(name, None)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return decorate(fn, name if isinstance(name, str) else None)

    return decorator


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Name -> tool mapping with collision detection and schema export.

    Two registration styles coexist:

    * ``register("name", callable)`` keeps the pre-tool-layer MiniVerl API;
    * ``register_tool(BaseTool)`` registers a lifecycle-aware tool.

    A bare callable is adapted to :class:`FunctionTool` on demand, so
    ``registry.schemas()`` always returns OpenAI-format entries.
    """

    def __init__(self) -> None:
        self.tools: dict[str, BaseTool] = {}

    def register(self, name: str | BaseTool, tool: Callable[..., Any] | BaseTool | None = None) -> None:
        if isinstance(name, BaseTool):
            self.register_tool(name)
            return
        if tool is None:
            raise TypeError("register(name, tool) requires a tool")
        safe_name = _sanitize_tool_name(name)
        if isinstance(tool, BaseTool):
            already = next((key for key, value in self.tools.items() if value is tool), None)
            if already is not None:
                raise ValueError(
                    f"tool is already registered as {already!r}; call unregister({already!r}) first"
                )
            old_name, old_schema_name = tool.name, tool.schema.name
            tool.name = safe_name
            tool.schema.name = safe_name
            try:
                self.register_tool(tool)
            except ToolCollisionError:
                # Leave the caller's tool untouched if registration fails.
                tool.name = old_name
                tool.schema.name = old_schema_name
                raise
            return
        adapted = FunctionTool(tool, name=safe_name)
        self.register_tool(adapted)

    def register_tool(self, tool: BaseTool) -> None:
        tool.name = _sanitize_tool_name(tool.name)
        tool.schema.name = tool.name
        existing = self.tools.get(tool.name)
        if existing is not None and existing is not tool:
            raise ToolCollisionError(
                f"tool {tool.name!r} is already registered by "
                f"{type(existing).__name__}; refusing to overwrite with "
                f"{type(tool).__name__}"
            )
        self.tools[tool.name] = tool

    def unregister(self, name: str) -> BaseTool | None:
        """Remove a tool, returning it (or ``None`` if it was absent)."""
        return self.tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self.tools[name].get_openai_tool_schema() for name in self.names()]

    def execute(
        self,
        name: str,
        parameters: dict[str, Any],
        instance_id: str | None = None,
    ) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(text=f"unknown tool {name}", reward=0.0)
        created = False
        if instance_id is None:
            instance_id, _ = tool.create()
            created = True
        try:
            return tool.execute(instance_id, parameters)
        except Exception as exc:  # noqa: BLE001 - one bad tool must not stop the loop
            return ToolResult(
                text=f"error: {type(exc).__name__}",
                reward=0.0,
                metrics={"tool/error": 1.0},
            )
        finally:
            if created:
                try:
                    tool.release(instance_id)
                except Exception:  # noqa: BLE001 - release is best effort
                    pass

    def call(self, name: str, argument: str) -> str:
        """Legacy string interface: ``call("calculator", "1+2")``.

        The single string is mapped to ``expression`` when the tool declares
        that parameter, otherwise to its only declared parameter.  Tools with
        more than one parameter must use :meth:`execute` with a dict.
        """
        tool = self.tools.get(name)
        if tool is None:
            return f"unknown tool {name}"
        properties = list(tool.schema.parameters.get("properties", {}))
        key = "expression" if "expression" in properties else (properties[0] if len(properties) == 1 else "expression")
        return self.execute(name, {key: argument}).text

    @classmethod
    def default(cls) -> "ToolRegistry":
        registry = cls()
        registry.register_tool(CalculatorTool())
        return registry
