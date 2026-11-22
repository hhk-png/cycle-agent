"""Tool layer: schema inference, lifecycle, registry and failure handling."""

from __future__ import annotations

import json
import time
from typing import List, Optional

import pytest

from mini_verl.tools import (
    BaseTool,
    CalculatorTool,
    FunctionTool,
    ToolCollisionError,
    ToolRegistry,
    ToolResult,
    ToolSchema,
    function_tool,
)


def test_schema_from_callable_infers_types_and_required():
    def search(query: str, limit: int = 3) -> str:
        """Search a corpus."""
        return f"{query}:{limit}"

    schema = ToolSchema.from_callable(search)
    assert schema.name == "search"
    assert schema.description == "Search a corpus."
    assert schema.parameters["properties"]["query"] == {"type": "string"}
    assert schema.parameters["properties"]["limit"] == {"type": "integer"}
    assert schema.parameters["required"] == ["query"]
    payload = schema.to_openai()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "search"


def test_function_tool_decorator_forms():
    @function_tool
    def add(a: int, b: int = 1) -> str:
        """Add two integers."""
        return str(a + b)

    @function_tool("renamed")
    def mul(a: int, b: int) -> int:
        return a * b

    assert add.tool.name == "add"
    assert add.tool.execute("x", {"a": 2}).text == "3"
    assert add.tool(2, 5) == "7"
    assert mul.tool.name == "renamed"
    assert mul.tool.execute("y", {"a": 3, "b": 4}).text == "12"
    with pytest.raises(TypeError):
        add.tool(1, 2, 3)


def test_future_annotation_forms_infer_useful_types():
    def tool(
        count: Optional[int] = None,
        maybe: int | None = None,
        tags: list[str] | None = None,
        legacy: List[int] | None = None,
    ) -> str:
        return "ok"

    schema = ToolSchema.from_callable(tool)
    properties = schema.parameters["properties"]
    assert properties["count"]["type"] == "integer"
    assert properties["maybe"]["type"] == "integer"
    assert properties["tags"]["type"] == "array"
    assert properties["legacy"]["type"] == "array"
    assert schema.parameters["required"] == []


def test_schema_export_is_a_defensive_copy():
    registry = ToolRegistry.default()
    exported = registry.schemas()[0]
    exported["function"]["parameters"]["properties"].clear()
    assert registry.schemas()[0]["function"]["parameters"]["properties"]["expression"]["type"] == "string"
    assert registry.call("calculator", "1+2") == "3"


def test_non_serializable_return_is_contained():
    tool = FunctionTool(lambda value: {value})
    result = tool.execute("x", {"value": 1})
    assert result.text == "error: TypeError"
    assert result.metrics["tool/error"] == 1.0


def test_function_tool_errors_do_not_escape():
    def broken(value: int) -> str:
        raise RuntimeError("boom")

    tool = FunctionTool(broken)
    result = tool.execute("x", {"value": 1})
    assert result.text == "error: RuntimeError"
    assert result.metrics["tool/error"] == 1.0


def test_function_tool_distinguishes_timeout_from_raised_timeout_error():
    def raises(value: int) -> str:
        raise TimeoutError("tool's own timeout")

    result = FunctionTool(raises).execute("x", {"value": 1})
    assert result.text == "error: TimeoutError"
    assert result.metrics["tool/error"] == 1.0


def test_function_tool_timeout_is_cooperative():
    def slow(value: int) -> str:
        time.sleep(0.05)
        return str(value)

    tool = FunctionTool(slow, timeout=0.001)
    result = tool.execute("x", {"value": 1})
    assert result.text == "timeout"
    assert result.metrics["tool/timeout"] == 1.0


def test_lifecycle_instance_isolation():
    class Counter(BaseTool):
        name = "counter"

        def execute(self, instance_id: str, parameters: dict, **kwargs) -> ToolResult:
            instance = self._instances[instance_id]
            instance.state["n"] = instance.state.get("n", 0) + int(parameters["by"])
            return ToolResult(text=str(instance.state["n"]))

    tool = Counter()
    first, _ = tool.create()
    second, _ = tool.create()
    assert tool.execute(first, {"by": 2}).text == "2"
    assert tool.execute(first, {"by": 3}).text == "5"
    assert tool.execute(second, {"by": 1}).text == "1"
    tool.release(first)
    assert tool.execute(second, {"by": 1}).text == "2"
    # A released id may be recreated, but an id that is still live may not.
    reused, _ = tool.create(first)
    assert reused == first
    with pytest.raises(ValueError):
        tool.create(first)


def test_calculator_legacy_and_lifecycle():
    tool = CalculatorTool()
    assert tool("3+4") == "7"
    assert tool("2*(3+4)") == "14"
    assert tool("bad(") == "error"
    instance_id, _ = tool.create()
    assert tool.execute(instance_id, {"expression": "5-2"}).text == "3"
    tool.release(instance_id)


def test_calculator_rejects_huge_powers_and_formatting_overflow():
    tool = CalculatorTool()
    assert tool("9**9**9") == "error"
    assert tool("9**9**9**9") == "error"
    assert tool("10**100000") == "error"


def test_registry_rename_is_atomic_and_leaves_no_alias():
    registry = ToolRegistry()
    tool = CalculatorTool()
    registry.register_tool(tool)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("calc2", tool)
    assert registry.names() == ["calculator"]
    assert tool.name == "calculator"

    other = CalculatorTool()
    other.name = "other"
    other.schema.name = "other"
    with pytest.raises(ToolCollisionError):
        registry.register("calculator", other)
    assert other.name == "other"
    assert other.schema.name == "other"


def test_registry_unregister_and_unknown_instance_id():
    class Stateful(BaseTool):
        name = "stateful"

        def execute(self, instance_id, parameters, **kwargs):
            self._instances[instance_id]  # raises KeyError for an unknown id
            return ToolResult(text="ok")

    registry = ToolRegistry()
    tool = Stateful()
    registry.register_tool(tool)
    assert registry.unregister("stateful") is tool
    assert registry.names() == []
    registry.register_tool(tool)
    result = registry.execute("stateful", {}, instance_id="missing")
    assert result.text == "error: KeyError"


def test_lambda_and_bad_names_are_sanitized():
    tool = FunctionTool(lambda value: value)
    assert tool.name == "lambda"
    assert all(char.isalnum() or char in "_-" for char in tool.name)


def test_positional_only_parameters_are_supported():
    def pos_only(a, /, b=2) -> str:
        return f"{a},{b}"

    tool = FunctionTool(pos_only)
    assert tool.execute("x", {"a": 1, "b": 3}).text == "1,3"
    assert tool(1, 3) == "1,3"


def test_mixed_positional_and_keyword_call():
    def two(a: int, b: int = 99) -> str:
        return f"{a},{b}"

    tool = FunctionTool(two)
    assert tool(1, b=2) == "1,2"
    with pytest.raises(TypeError, match="multiple values"):
        tool(1, a=2)
    assert tool.execute("x", {"a": 1, "typo": 2}).text == "error: TypeError"


def test_registry_default_schema_and_collision():
    registry = ToolRegistry.default()
    assert registry.names() == ["calculator"]
    assert registry.schemas()[0]["function"]["name"] == "calculator"
    with pytest.raises(ToolCollisionError):
        registry.register_tool(CalculatorTool())


def test_registry_accepts_plain_callables_and_exports_schema():
    registry = ToolRegistry()

    def echo(text: str) -> str:
        """Echo text."""
        return text

    registry.register("echo", echo)
    assert registry.call("echo", "hi") == "hi"
    schema = registry.schemas()[0]
    assert schema["function"]["parameters"]["properties"]["text"] == {"type": "string"}


def test_registry_unknown_tool_is_a_result_not_an_exception():
    result = ToolRegistry().execute("missing", {})
    assert result.text == "unknown tool missing"
    assert result.reward == 0.0


def test_renaming_one_instance_does_not_mutate_the_default_schema():
    registry = ToolRegistry()
    renamed = CalculatorTool()
    registry.register("calc2", renamed)
    assert renamed.name == "calc2"
    assert renamed.schema.name == "calc2"
    fresh = CalculatorTool()
    assert fresh.name == "calculator"
    assert fresh.schema.name == "calculator"


def test_function_tool_rejects_varargs():
    def variadic(*args) -> str:
        return json.dumps(args)

    with pytest.raises(TypeError):
        FunctionTool(variadic)
