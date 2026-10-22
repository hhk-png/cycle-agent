"""High-level offline generation API, mirroring ``deepinfer.LLM``."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import __version__
from .config import EngineConfig
from .engine import LLMEngine
from .manifest import ModelManifest
from .model.tiny_transformer import TinyTransformer, load_model
from .sampling_params import SamplingParams
from .sequence import RequestOutput
from .tokenizer import StreamingDetokenizer, Tokenizer


class LLM:
    def __init__(
        self,
        model: str | Path | TinyTransformer | None = None,
        *,
        config: EngineConfig | None = None,
        tokenizer: Tokenizer | None = None,
        draft_model: str | Path | TinyTransformer | None = None,
        manifest: ModelManifest | None = None,
    ) -> None:
        config = config or EngineConfig()
        if model is None:
            model_obj = TinyTransformer.from_file(config.resolve_model_path())
        elif isinstance(model, TinyTransformer):
            model_obj = model
        else:
            model_obj = load_model(model)
        if tokenizer is None:
            vocab_path = config.resolve_tokenizer_path()
            tokenizer = Tokenizer.from_file(vocab_path) if Path(vocab_path).exists() else Tokenizer()
        if manifest is not None:
            # Fail at startup rather than serving weights with the wrong
            # tokenizer, template or KV capacity.
            manifest.assert_compatible(
                engine_version=__version__,
                vocab_size=tokenizer.vocab_size,
                max_model_len=config.max_model_len,
                dtype="float32",
                quantization=None,
            )
            # Only verify artifacts that came from explicit paths; an
            # in-memory model/tokenizer has no file to hash.
            manifest.assert_files(
                model_path=config.model_path or None,
                tokenizer_path=config.tokenizer_path or None,
            )
        draft_obj = None
        if isinstance(draft_model, TinyTransformer):
            draft_obj = draft_model
        elif draft_model is not None:
            draft_obj = load_model(draft_model)
        elif config.enable_speculative:
            draft_path = Path(__file__).resolve().parent / "assets" / "draft_lm.npz"
            if draft_path.exists():
                draft_obj = load_model(draft_path)
        self.engine = LLMEngine(
            config, model=model_obj, tokenizer=tokenizer, draft_model=draft_obj
        )

    def generate(
        self,
        prompts: str | list[str] | None = None,
        sampling_params: SamplingParams | list[SamplingParams] | None = None,
        *,
        prompt_token_ids: list[list[int]] | None = None,
        return_objects: bool = False,
        stream: Callable[[str, str, bool], None] | None = None,
    ) -> list[str] | list[RequestOutput]:
        if prompts is None and prompt_token_ids is None:
            raise ValueError("prompts or prompt_token_ids is required")
        if isinstance(prompts, str):
            prompts = [prompts]
        if prompt_token_ids is None:
            prompt_token_ids = [self.engine.tokenizer.encode(p) for p in prompts]  # type: ignore[union-attr]
        else:
            prompts = [""] * len(prompt_token_ids)

        if sampling_params is None:
            params = [SamplingParams() for _ in prompt_token_ids]
        elif isinstance(sampling_params, SamplingParams):
            params = [sampling_params.copy() for _ in prompt_token_ids]
        else:
            params = [p.copy() for p in sampling_params]
        if len(params) != len(prompt_token_ids):
            raise ValueError("sampling_params length must match prompts")

        order: list[str] = []
        for index, (ids, sp) in enumerate(zip(prompt_token_ids, params)):
            request_id = f"req-{index}"
            order.append(request_id)
            self.engine.add_request(
                sampling_params=sp,
                request_id=request_id,
                prompt_token_ids=ids,
            )

        finals: dict[str, RequestOutput] = {}
        detokenizers = {request_id: StreamingDetokenizer(self.engine.tokenizer) for request_id in order}
        idle_steps = 0
        while self.engine.has_unfinished_requests() or len(finals) < len(order):
            outputs = self.engine.step()
            if not outputs:
                idle_steps += 1
                if idle_steps > 8:
                    raise RuntimeError("engine made no progress")
                continue
            idle_steps = 0
            for output in outputs:
                if stream is not None:
                    detokenizer = detokenizers[output.request_id]
                    delta = "".join(
                        detokenizer.feed(token)
                        for token in output.extra.get("delta_token_ids", [])
                    )
                    if output.finished:
                        delta += detokenizer.flush()
                    if delta:
                        stream(output.request_id, delta, output.finished)
                if output.finished:
                    finals[output.request_id] = output
            if self.engine.has_unfinished_requests() is False and len(finals) >= len(order):
                break

        results = [finals[request_id] for request_id in order]
        if return_objects:
            return results
        return [result.text for result in results]

    def generate_chat(
        self,
        messages: list[dict[str, str]] | list[list[dict[str, str]]],
        sampling_params: SamplingParams | list[SamplingParams] | None = None,
        *,
        return_objects: bool = False,
        stream: Callable[[str, str, bool], None] | None = None,
    ):
        if messages and isinstance(messages[0], dict):
            conversations = [messages]  # type: ignore[list-item]
        else:
            conversations = messages  # type: ignore[assignment]
        ids = [
            self.engine.tokenizer.apply_chat_template(conversation)  # type: ignore[arg-type]
            for conversation in conversations
        ]
        return self.generate(
            prompt_token_ids=ids,
            sampling_params=sampling_params,
            return_objects=return_objects,
            stream=stream,
        )

    def close(self) -> None:
        self.engine.close()
