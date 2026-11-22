"""Verifiable math rewards and the GSM8K-lite dataset shape."""

from _common import header

from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.rewards import extract_boxed, format_reward, math_reward, normalize_number


def main() -> None:
    header("Math verification: boxed answers, normalization and format shaping")
    cases = [
        ("\\boxed{12}", "12"),
        ("the answer is 7", "7"),
        ("\\boxed{1,000}", "1000"),
        ("\\boxed{8}", "7"),
    ]
    for response, answer in cases:
        print(f"response={response!r:28} answer={answer!r:6} -> {math_reward(response, answer)}")
    print("extract:", extract_boxed("so \\boxed{42}"))
    print("normalize:", normalize_number("$3.50"))
    print("format shaping:", format_reward("\\boxed{3}"))

    tokenizer = CharTokenizer()
    dataset = build_dataset("math", 4, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 96, 8)
    print("prompt:", data.non_tensor_batch["prompts"][0])
    print("answer:", data.non_tensor_batch["answers"][0])
    print("input shape:", data.batch["input_ids"].shape, "response tokens:", int(data.batch["response_mask"].sum()))


if __name__ == "__main__":
    main()
