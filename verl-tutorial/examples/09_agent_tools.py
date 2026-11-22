"""Multi-turn agent loop with a calculator tool and loss masking."""

from _common import header

from mini_verl.agent import AgentLoop, AgentLoopManager, CalculatorTool
from mini_verl.data import CharTokenizer
from mini_verl.nn import TinyLinearLM


def main() -> None:
    header("AgentLoop: assistant tokens train, tool observations are masked")
    tokenizer = CharTokenizer()
    model = TinyLinearLM(vocab_size=tokenizer.vocab_size, max_seq_len=32, dim=8)
    loop = AgentLoop(model, tokenizer, max_turns=2)
    script = [tokenizer.encode("<tool>3+4</tool>"), tokenizer.encode("7")]
    loop._generate = lambda ids, max_tokens: script.pop(0)
    trajectory = loop.run("calc ")
    print("text:", trajectory.text)
    print("tool calls:", trajectory.tool_calls)
    print("response mask:", trajectory.response_mask.astype(int).tolist())
    print("masked tokens:", int((trajectory.response_mask == 0).sum()))
    manager = AgentLoopManager(loop)
    data = manager.to_dataproto([trajectory], max_length=32)
    print("DataProto shape:", data.batch["input_ids"].shape)
    print("calculator:", CalculatorTool()("2*(3+4)"))


if __name__ == "__main__":
    main()
