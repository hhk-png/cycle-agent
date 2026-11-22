"""Multi-turn agent loop and tools.

The shape follows verl's ``experimental/agent_loop``: an agent loop owns the
conversation, calls tools, and returns token ids plus a response mask where
tool observations are masked out of the policy loss.  MiniVerl's model is tiny,
so the loop is deliberately simple, but the trajectory/mask contract is real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .data import CharTokenizer
from .nn import TinyLM
from .protocol import DataProto
from .tools import BaseTool, CalculatorTool, Tool, ToolRegistry

__all__ = [
    "Tool",
    "BaseTool",
    "CalculatorTool",
    "ToolRegistry",
    "AgentTrajectory",
    "AgentLoop",
    "AgentLoopManager",
]


@dataclass
class AgentTrajectory:
    prompt: str
    input_ids: np.ndarray
    response_mask: np.ndarray
    text: str
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)
    turns: int = 0


_TOOL_CALL = re.compile(r"<tool>(.*?)</tool>", re.S)
_FUNCTION_CALL = re.compile(r"calculator\[(.*?)\]", re.S)


class AgentLoop:
    def __init__(
        self,
        model: TinyLM,
        tokenizer: CharTokenizer,
        registry: ToolRegistry | None = None,
        max_turns: int = 3,
        max_tokens_per_turn: int = 16,
        seed: int = 0,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.registry = registry or ToolRegistry.default()
        self.max_turns = max_turns
        self.max_tokens_per_turn = max_tokens_per_turn
        self.rng = np.random.default_rng(seed)

    def _generate(self, ids: list[int], max_tokens: int) -> list[int]:
        generated = []
        for _ in range(max_tokens):
            logits = self.model(np.asarray([ids], dtype=np.int64)).data[0, -1]
            token = int(np.argmax(logits))
            if token == self.tokenizer.eos_token_id:
                break
            generated.append(token)
            ids = ids + [token]
        return generated

    def run(self, prompt: str) -> AgentTrajectory:
        prompt_ids = self.tokenizer.encode(prompt)
        ids = list(prompt_ids)
        # 1 = assistant token (policy gradient), 0 = prompt or tool observation.
        token_mask = [0.0] * len(prompt_ids)
        text = prompt
        tool_calls: list[tuple[str, str, str]] = []
        turns = 0
        for _ in range(self.max_turns):
            generated = self._generate(ids, self.max_tokens_per_turn)
            if not generated:
                break
            ids.extend(generated)
            token_mask.extend([1.0] * len(generated))
            chunk = self.tokenizer.decode(generated)
            text += chunk
            turns += 1
            match = _TOOL_CALL.search(chunk) or _FUNCTION_CALL.search(chunk)
            if not match:
                break
            expression = match.group(1).strip()
            observation = self.registry.call("calculator", expression)
            tool_calls.append(("calculator", expression, observation))
            observation_text = f"<obs>{observation}</obs>"
            observation_ids = self.tokenizer.encode(observation_text)
            ids.extend(observation_ids)
            token_mask.extend([0.0] * len(observation_ids))
            text += observation_text
        response_mask = np.asarray(token_mask[len(prompt_ids) :], dtype=np.float32)
        return AgentTrajectory(
            prompt=prompt,
            input_ids=np.asarray(ids, dtype=np.int64),
            response_mask=response_mask,
            text=text,
            tool_calls=tool_calls,
            turns=turns,
        )


class AgentLoopManager:
    def __init__(self, loop: AgentLoop):
        self.loop = loop

    def run(self, prompts: list[str]) -> list[AgentTrajectory]:
        return [self.loop.run(prompt) for prompt in prompts]

    def to_dataproto(self, trajectories: list[AgentTrajectory], max_length: int, pad_token_id: int = 0) -> DataProto:
        batch = len(trajectories)
        input_ids = np.full((batch, max_length), pad_token_id, dtype=np.int64)
        response_mask = np.zeros((batch, max_length), dtype=np.float32)
        attention = np.zeros((batch, max_length), dtype=np.float32)
        prompts = np.empty(batch, dtype=object)
        for row, trajectory in enumerate(trajectories):
            length = min(max_length, len(trajectory.input_ids))
            input_ids[row, :length] = trajectory.input_ids[:length]
            attention[row, :length] = 1.0
            # Align the response mask to the right edge of the sequence.
            prompt_length = len(trajectory.input_ids) - len(trajectory.response_mask)
            begin = min(prompt_length, length)
            response_mask[row, begin:length] = trajectory.response_mask[: length - begin]
            prompts[row] = trajectory.prompt
        return DataProto(
            batch={"input_ids": input_ids, "attention_mask": attention, "response_mask": response_mask},
            non_tensor_batch={"prompts": prompts},
            meta_info={"agent_turns": float(np.mean([t.turns for t in trajectories])) if trajectories else 0.0},
        )
