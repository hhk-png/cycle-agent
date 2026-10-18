#!/usr/bin/env python3
"""Elastic training, checkpointing and gang scheduling, without Ray.

Run from the tutorial root:

    python examples/elastic_training_lab.py

Three behaviours are modelled:

1. **Checkpoint/resume**: a failure resumes from the last committed step.
2. **Elastic world size**: workers can leave or join at a checkpoint boundary;
   the global batch is recomputed from the new world size while the optimizer
   state stays valid.
3. **Gang admission**: a training job is admitted only when all workers fit,
   which is why a partial allocation is worse than waiting.

The comparison against a non-elastic ``torchrun`` style restart makes the cost
of redoing work visible: elastic resume wastes zero steps here; static restart
replays every step since the beginning.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Checkpoint:
    version: int
    step: int
    world_size: int
    optimizer_bytes: int


class CheckpointStore:
    def __init__(self) -> None:
        self._items: list[Checkpoint] = []

    def save(self, step: int, world_size: int, optimizer_bytes: int = 64 * 1024 * 1024) -> Checkpoint:
        checkpoint = Checkpoint(len(self._items) + 1, step, world_size, optimizer_bytes)
        self._items.append(checkpoint)
        return checkpoint

    def latest(self) -> Checkpoint | None:
        return self._items[-1] if self._items else None

    def history(self) -> list[Checkpoint]:
        return list(self._items)


class ElasticTrainer:
    """Checkpoint-boundary elastic training with a deterministic clock of steps."""

    def __init__(
        self,
        *,
        world_size: int,
        total_steps: int,
        micro_batch: int = 8,
        checkpoint_every: int = 2,
        elastic: bool = True,
    ):
        self.world_size = world_size
        self.total_steps = total_steps
        self.micro_batch = micro_batch
        self.checkpoint_every = checkpoint_every
        self.elastic = elastic
        self.step = 0
        self.completed: list[int] = []
        self.store = CheckpointStore()
        self.events: list[dict] = []

    @property
    def global_batch(self) -> int:
        return self.micro_batch * self.world_size

    # ------------------------------------------------------------------ training
    def _run_until(self, target: int) -> None:
        while self.step < target:
            self.step += 1
            self.completed.append(self.step)
            if self.step % self.checkpoint_every == 0:
                checkpoint = self.store.save(self.step, self.world_size)
                self.events.append(
                    {
                        "kind": "checkpoint",
                        "step": self.step,
                        "version": checkpoint.version,
                        "world_size": self.world_size,
                        "global_batch": self.global_batch,
                    }
                )

    def run(self, target: int) -> None:
        self._run_until(min(target, self.total_steps))

    def fail(self, lost_workers: int, target: int) -> None:
        """Simulate worker loss; elastic jobs resume, static jobs restart."""
        latest = self.store.latest()
        resume_from = latest.step if latest and self.elastic else 0
        if latest and self.elastic:
            self.world_size = max(1, self.world_size - lost_workers)
            self.step = latest.step
            optimizer_reset = False
        else:
            # torchrun-style restart: fresh workers, fresh optimizer, replay.
            self.step = 0
            self.world_size = max(1, self.world_size - lost_workers)
            optimizer_reset = True
        self.events.append(
            {
                "kind": "failure",
                "lost_workers": lost_workers,
                "resume_from": resume_from,
                "world_size": self.world_size,
                "global_batch": self.global_batch,
                "optimizer_reset": optimizer_reset,
            }
        )
        self._run_until(min(target, self.total_steps))

    def scale_to(self, new_world_size: int, target: int) -> None:
        """Add or remove workers at a checkpoint boundary (elastic only)."""
        if not self.elastic:
            raise RuntimeError("a static torchrun job cannot resize without a restart")
        assert self.step % self.checkpoint_every == 0, "resize only at a checkpoint boundary"
        old_batch = self.global_batch
        self.world_size = new_world_size
        self.events.append(
            {
                "kind": "resize",
                "step": self.step,
                "from": old_batch,
                "to": self.global_batch,
                "global_batch": self.global_batch,
                "world_size": self.world_size,
            }
        )
        self._run_until(min(target, self.total_steps))


def gang_admission(gang_size: int, free_nodes: int, waiting: int = 0) -> tuple[bool, str]:
    if free_nodes >= gang_size:
        return True, f"admit gang of {gang_size}; {free_nodes - gang_size} nodes left"
    return False, f"wait: need {gang_size}, have {free_nodes}; {waiting} jobs queued"


def main() -> int:
    print("== elastic training lab ==")

    # Baseline: one static 4-worker job, a failure at step 8, restart.
    static = ElasticTrainer(world_size=4, total_steps=14, elastic=False)
    static.run(8)
    static.fail(lost_workers=2, target=14)
    static_replayed = len(static.completed) - len(set(static.completed))
    print("\n-- static torchrun-style restart --")
    print(f"completed steps : {len(static.completed)} (unique {len(set(static.completed))})")
    print(f"replayed steps  : {static_replayed}")
    print(f"checkpoints     : {[c.step for c in static.store.history()]}")
    print(f"global batch    : {static.global_batch} (world={static.world_size})")

    # Elastic: same failure, then scale up.
    trainer = ElasticTrainer(world_size=4, total_steps=20, elastic=True)
    trainer.run(8)                       # 4 workers, checkpoints at 2,4,6,8
    trainer.fail(lost_workers=2, target=14)  # resume from 8 with 2 workers
    trainer.scale_to(6, target=20)       # join 4 workers at step 14

    steps = trainer.completed
    duplicates = len(steps) - len(set(steps))
    missing = sorted(set(range(1, 21)) - set(steps))

    print("\n-- elastic resume and resize --")
    for event in trainer.events:
        print(
            "  {kind:<10} step={step:<2} world={world_size} batch={global_batch}"
            .format(
                kind=event["kind"],
                step=event.get("step", event.get("resume_from", 0)),
                world_size=event["world_size"],
                global_batch=event["global_batch"],
            )
        )
    print(f"completed steps : {len(steps)} (unique {len(set(steps))}, duplicates {duplicates})")
    print(f"missing steps   : {missing}")
    print(f"checkpoints     : {[c.step for c in trainer.store.history()]}")
    print(f"final world     : {trainer.world_size}, global batch {trainer.global_batch}")

    print("\n-- gang admission --")
    for free in (2, 5, 6):
        admitted, message = gang_admission(gang_size=6, free_nodes=free, waiting=2)
        print(f"  free={free}: {admitted} ({message})")

    assert duplicates == 0, "elastic resume must not replay committed steps"
    assert missing == [], "every step must run exactly once"
    assert [event["world_size"] for event in trainer.events if event["kind"] in ("failure", "resize")] == [2, 6]
    assert [c.step for c in trainer.store.history()] == [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
    assert static_replayed == 8, static_replayed
    assert gang_admission(6, 5)[0] is False
    assert gang_admission(6, 6)[0] is True

    print("\nOK: elastic resume replayed 0 steps; static restart replayed 8.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
