"""A reproducible synthetic corpus for the toy language model.

The corpus is intentionally small and repetitive.  It gives the model enough
local structure to produce readable continuations for the tutorial's prompts
without shipping a copyrighted text or a large checkpoint.
"""

from __future__ import annotations

import random

SUBJECTS = [
    "the cat", "the dog", "a small bird", "the old man", "a young girl",
    "the robot", "the student", "the engineer", "the fox", "the rabbit",
    "the teacher", "the pilot", "the painter", "the gardener", "the librarian",
]
VERBS = [
    "sat on", "ran to", "looked at", "jumped over", "walked into", "found",
    "carried", "watched", "followed", "opened", "cleaned", "painted",
    "measured", "repaired", "described",
]
OBJECTS = [
    "the mat", "the park", "the tree", "the house", "the river", "the book",
    "the door", "the garden", "the mountain", "the city", "the window",
    "the machine", "the notebook", "the bridge", "the road",
]
PLACES = [
    "the station", "the library", "the market", "the factory", "the forest",
    "the harbor", "the village", "the laboratory", "the classroom", "the hill",
]
ADJECTIVES = [
    "quiet", "bright", "cold", "warm", "small", "large", "careful", "curious",
    "patient", "gentle", "busy", "calm", "strange", "familiar", "clear",
]
NAMES = ["Mia", "Leo", "Ada", "Sam", "Nora", "Kai", "Iris", "Owen", "Zoe", "Eli"]

FACTS = [
    "DeepInfer is an inference engine for large language models.",
    "DeepInfer serves DeepSeek V4.1 Flash in production.",
    "An inference engine turns model weights into useful tokens.",
    "Prefill reads the prompt and decode writes the answer.",
    "A KV cache stores attention keys and values for reuse.",
    "Paged attention maps logical tokens to physical cache blocks.",
    "Continuous batching keeps the accelerator busy.",
    "Speculative decoding proposes drafts and verifies them in parallel.",
    "Expert parallelism spreads MoE experts across many devices.",
    "Structured output constrains sampling with a grammar.",
    "A shorter time to first token makes a chat feel responsive.",
    "Throughput and latency are different goals and often trade off.",
    "The decode phase is usually limited by memory bandwidth.",
    "The prefill phase is usually limited by matrix multiplication.",
    "A good scheduler overlaps computation, communication and I/O.",
    "Cache hits avoid recomputing a repeated prompt prefix.",
    "Low precision reduces memory traffic but needs careful scaling.",
    "Deterministic sampling makes evaluation reproducible.",
    "Load balancing keeps every device equally busy.",
    "Observability turns a guess into a measurement.",
]

STORY = (
    "Once upon a time, a small robot lived at the edge of a quiet village. "
    "Every morning it walked to the river and watched the light move across the water. "
    "One day the robot found a broken clock beside the road. "
    "It carried the clock home, cleaned every gear, and listened to the first tick. "
    "The village children gathered around and asked how the clock worked. "
    "The robot explained that many small parts can do a large job when they work together. "
    "From that day on, the clock stood in the square and reminded everyone to be kind. "
)


def build_corpus(seed: int = 7, target_chars: int = 120_000) -> str:
    rng = random.Random(seed)
    parts: list[str] = []
    while sum(len(p) for p in parts) < target_chars:
        choice = rng.random()
        if choice < 0.34:
            parts.append(
                f"{rng.choice(SUBJECTS)} {rng.choice(VERBS)} {rng.choice(OBJECTS)}. "
            )
        elif choice < 0.54:
            parts.append(
                f"On a {rng.choice(ADJECTIVES)} morning, {rng.choice(SUBJECTS)} "
                f"{rng.choice(VERBS)} {rng.choice(PLACES)} and "
                f"{rng.choice(VERBS)} {rng.choice(OBJECTS)}. "
            )
        elif choice < 0.70:
            parts.append(
                f'"{rng.choice(FACTS)}" said {rng.choice(NAMES)}. '
                f'"{rng.choice(FACTS)}" answered {rng.choice(NAMES)}. '
            )
        elif choice < 0.84:
            parts.append(rng.choice(FACTS) + " ")
        elif choice < 0.94:
            parts.append(
                f"{rng.choice(NAMES)} went to {rng.choice(PLACES)} with "
                f"{rng.choice(NAMES)}. They {rng.choice(VERBS)} "
                f"{rng.choice(OBJECTS)} and then {rng.choice(VERBS)} "
                f"{rng.choice(OBJECTS)}. "
            )
        else:
            parts.append(STORY + " ")
        if rng.random() < 0.12:
            parts.append("\n")
    text = "".join(parts)
    return text[:target_chars]


def corpus_bytes(seed: int = 7, target_chars: int = 120_000) -> bytes:
    return build_corpus(seed, target_chars).encode("utf-8")
