"""Chat-template encoding and streaming generation."""

from __future__ import annotations

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


def main() -> None:
    llm = LLM(config=EngineConfig(num_blocks=128, max_num_batched_tokens=256, max_model_len=256))
    messages = [
        {"role": "system", "content": "You are a concise inference engineer."},
        {"role": "user", "content": "Explain prefill and decode in one sentence."},
    ]
    ids = llm.engine.tokenizer.apply_chat_template(messages)
    print("prompt token ids:", ids)
    print("decoded template:", llm.engine.tokenizer.decode(ids, skip_special_tokens=False))
    print("answer:")

    def on_delta(_request_id: str, delta: str, finished: bool) -> None:
        print(delta, end="", flush=True)
        if finished:
            print()

    llm.generate_chat(messages, SamplingParams(temperature=0.2, max_tokens=24, seed=1), stream=on_delta)


if __name__ == "__main__":
    main()
