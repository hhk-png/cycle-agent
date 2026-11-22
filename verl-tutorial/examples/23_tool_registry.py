"""Tool registry: schemas, lifecycle, collisions and an agent trajectory.

The point is the contract, not the calculator: a tool exposes an OpenAI schema
to the model, owns one instance per trajectory, returns text plus an optional
step reward, and is released when the trajectory ends.
"""

from _common import header

from mini_verl.agent import AgentLoop, AgentLoopManager
from mini_verl.data import CharTokenizer
from mini_verl.nn import TinyLinearLM
from mini_verl.tools import (
    CalculatorTool,
    FunctionTool,
    ToolCollisionError,
    ToolRegistry,
    function_tool,
)


@function_tool("word_count")
def word_count(text: str) -> str:
    """Count whitespace-separated words."""
    return str(len(text.split()))


def main() -> None:
    header("ToolRegistry: OpenAI schema + per-trajectory lifecycle")

    registry = ToolRegistry.default()
    registry.register_tool(word_count.tool)
    print("registered:", registry.names())
    for schema in registry.schemas():
        function = schema["function"]
        print(f"  {function['name']}: {function['description']} {function['parameters']}")

    # Lifecycle: create -> execute several times -> release.
    tool = CalculatorTool()
    instance_id, _ = tool.create()
    for expression in ("1+2", "3*4", "(5-1)/2"):
        print(f"  {expression} = {tool.execute(instance_id, {'expression': expression}).text}")
    tool.release(instance_id)

    # Collisions are refused rather than silently shadowed.
    try:
        registry.register_tool(CalculatorTool())
    except ToolCollisionError as exc:
        print("collision refused:", str(exc)[:60])

    # A function tool with a bad signature must not take down the loop.
    broken = FunctionTool(lambda value: 1 / 0, name="broken")
    print("broken tool:", broken.execute("x", {"value": 1}).text)

    # The agent loop consumes the same registry; observations stay masked.
    tokenizer = CharTokenizer()
    model = TinyLinearLM(vocab_size=tokenizer.vocab_size, max_seq_len=32, dim=8)
    loop = AgentLoop(model, tokenizer, max_turns=2, registry=registry)
    script = [tokenizer.encode("<tool>3+4</tool>"), tokenizer.encode("7")]
    loop._generate = lambda ids, max_tokens: script.pop(0)
    trajectory = loop.run("calc ")
    print("trajectory:", trajectory.text)
    print("tool calls:", trajectory.tool_calls)
    print("trainable tokens:", int(trajectory.response_mask.sum()), "masked:", int((trajectory.response_mask == 0).sum()))
    data = AgentLoopManager(loop).to_dataproto([trajectory], max_length=32)
    print("DataProto:", data.batch["input_ids"].shape)


if __name__ == "__main__":
    main()
