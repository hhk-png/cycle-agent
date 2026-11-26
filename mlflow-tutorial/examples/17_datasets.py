"""Evaluation datasets: create, digest, merge, search and delete.

A dataset is not a run input.  It is a versioned collection of evaluation
samples with its own identity, content digest and schema/profile.  This example
stays offline and uses the local SQLite backend unless
``MINI_MLFLOW_TRACKING_URI`` points at a server.

Run from the tutorial root:

    python examples/17_datasets.py
"""

from __future__ import annotations

import os

from _common import client, print_json


def main() -> None:
    active = client()
    suffix = os.environ.get("MINI_MLFLOW_DEMO_SUFFIX") or "demo"
    name = f"support-eval-{suffix}"
    records = [
        {
            "inputs": {"question": "What is the refund window?", "locale": "en"},
            "expectations": {"answer": "30 days"},
            "tags": {"slice": "policy"},
        },
        {
            "inputs": {"question": "Can I return a used item?", "locale": "en"},
            "expectations": {"answer": "Only if defective"},
            "tags": {"slice": "policy"},
        },
    ]
    created_ids: list[str] = []
    try:
        first = active.create_dataset(
            name,
            records,
            source="s3://warehouse/eval/support-2026-11-20.jsonl",
            source_type="s3",
            tags={"owner": "cx", "stage": "golden"},
        )
        created_ids.append(first.dataset_id)

        # Same records, same order -> same digest, even with a different name.
        copy = active.create_dataset(f"{name}-copy", records)
        created_ids.append(copy.dataset_id)

        merged = active.merge_dataset_records(
            first.dataset_id,
            [
                {
                    "inputs": {"question": "Where is order A-1042?", "locale": "en"},
                    "expectations": {"answer": "shipped"},
                    "tags": {"slice": "order-status"},
                }
            ],
        )
        fetched = active.get_dataset(first.dataset_id)
        found = active.search_datasets(
            f"tags.owner = 'cx' AND attributes.name = '{name}'",
            order_by=["attributes.version DESC"],
        )
        print_json(
            {
                "dataset_id": fetched.dataset_id,
                "name": fetched.name,
                "version": merged.version,
                "record_count": fetched.record_count,
                "digest": fetched.digest,
                "digest_is_content_addressed": first.digest == copy.digest,
                "schema": fetched.schema,
                "profile": fetched.profile,
                "search_hits": [
                    {"name": item.name, "version": item.version, "records": item.record_count}
                    for item in found
                ],
                "first_record": fetched.records[0],
            }
        )
    finally:
        # Datasets are assets; demos should clean up after themselves.
        for dataset_id in created_ids:
            active.delete_dataset(dataset_id)
        active.close()


if __name__ == "__main__":
    main()
