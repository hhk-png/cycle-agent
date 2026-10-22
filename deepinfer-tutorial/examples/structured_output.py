"""Regex, JSON-schema and EBNF constrained generation."""

from __future__ import annotations

import json
import re

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


def main() -> None:
    llm = LLM(
        config=EngineConfig(
            num_blocks=256,
            max_num_batched_tokens=512,
            max_num_seqs=4,
            max_model_len=256,
        )
    )

    regex = r"[a-z]+(?: [a-z]+)*"
    text = llm.generate(
        "DeepInfer is",
        SamplingParams(
            temperature=0.8,
            max_tokens=16,
            seed=3,
            so_args={"schema_type": "regex", "schema": regex},
        ),
    )[0]
    print("regex:", repr(text), "valid:", bool(re.fullmatch(regex, text)))

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "active": {"type": "boolean"},
        },
        "required": ["name", "age", "active"],
        "additionalProperties": False,
    }
    text = llm.generate(
        "DeepInfer is",
        SamplingParams(
            temperature=0.8,
            max_tokens=100,
            seed=4,
            so_args={"schema_type": "json", "schema": json.dumps(schema)},
        ),
    )[0]
    print("json :", text)
    print("parsed:", json.loads(text))

    grammar = 'start = "yes" | "no" | "maybe" ;'
    text = llm.generate(
        "DeepInfer is",
        SamplingParams(
            temperature=0.8,
            max_tokens=8,
            seed=5,
            so_args={"schema_type": "ebnf", "schema": grammar},
        ),
    )[0]
    print("ebnf :", repr(text), "valid:", text in {"yes", "no", "maybe"})


if __name__ == "__main__":
    main()
