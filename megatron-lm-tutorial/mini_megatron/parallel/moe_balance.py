"""Auxiliary-loss-free MoE balancing and node-limited routing.

DeepSeek-V3 replaces the Switch-style auxiliary loss with a per-expert bias
that is added **only for expert selection** and updated from the observed load.
The language-model loss therefore has no auxiliary term, while the router still
converges to a balanced assignment.

This module also implements node-limited routing (at most ``max_nodes``
distinct physical nodes per token), which matters when expert-parallel
all-to-all crosses NVLink domains.

Both pieces are reference implementations: they operate on a ``[tokens,
experts]`` score matrix and do not depend on the rest of mini-megatron.  The
tests pin the selection rule, the bias direction, and the node constraint.
"""
from __future__ import annotations

from typing import Dict, Tuple

import torch

from . import comm


class AuxFreeBalancer:
    """Per-expert selection bias with a sign-based load update.

    Parameters
    ----------
    num_experts:
        Number of routed experts.
    update_speed:
        ``gamma`` in the DeepSeek-V3 update.  The bias moves by ``gamma`` per
        update, independent of the magnitude of the imbalance.
    normalize:
        If true, top-k gate weights are renormalised to sum to one per token.
    """

    def __init__(
        self,
        num_experts: int,
        *,
        update_speed: float = 1e-3,
        normalize: bool = True,
        device=None,
    ) -> None:
        if num_experts < 1:
            raise ValueError("num_experts must be positive")
        self.num_experts = num_experts
        self.update_speed = float(update_speed)
        self.normalize = normalize
        self.bias = torch.zeros(num_experts, dtype=torch.float32, device=device)
        self.updates = 0

    def select(
        self, scores: torch.Tensor, top_k: int
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Select experts using ``scores + bias``; return weights and indices.

        ``scores`` are the affinity scores (normally ``sigmoid(router_logits)``)
        in ``[0, 1]``.  The bias changes *which* experts are selected but not
        the gate weight, which is the key difference from an auxiliary loss.
        """
        if scores.dim() != 2 or scores.size(-1) != self.num_experts:
            raise ValueError("scores must be [tokens, num_experts]")
        if not 1 <= top_k <= self.num_experts:
            raise ValueError("top_k out of range")
        biased = scores + self.bias.to(scores.device)
        indices = biased.topk(top_k, dim=-1).indices
        weights = scores.gather(-1, indices)
        if self.normalize:
            weights = weights / weights.sum(dim=-1, keepdim=True).clamp_min(1e-9)
        return weights, indices

    @torch.no_grad()
    def update(self, expert_counts: torch.Tensor) -> torch.Tensor:
        """Move the bias against the load direction.

        ``expert_counts`` must be the *global* count over the whole batch and,
        in an EP setup, over all EP ranks (see :func:`global_expert_counts`).
        Overloaded experts get a negative nudge, idle experts a positive one.
        """
        counts = expert_counts.detach().float().to(self.bias.device)
        if counts.numel() != self.num_experts:
            raise ValueError("expert_counts has the wrong length")
        mean = counts.mean()
        # sign(mean - count): overloaded -> -1, idle -> +1, exact -> 0.
        self.bias.add_(torch.sign(mean - counts) * self.update_speed)
        self.updates += 1
        return self.bias

    def state_dict(self) -> Dict:
        return {
            "num_experts": self.num_experts,
            "update_speed": self.update_speed,
            "normalize": self.normalize,
            "bias": self.bias.detach().cpu().clone(),
            "updates": self.updates,
        }

    def load_state_dict(self, state: Dict) -> None:
        if int(state["num_experts"]) != self.num_experts:
            raise ValueError("expert count mismatch")
        # Hyperparameters are part of the balancer's behaviour: restoring the
        # bias but silently keeping a different update_speed/normalize would
        # make a resumed run diverge from the saved one.
        if "update_speed" in state:
            self.update_speed = float(state["update_speed"])
        if "normalize" in state:
            self.normalize = bool(state["normalize"])
        self.bias.copy_(state["bias"].float().to(self.bias.device))
        self.updates = int(state["updates"])


def global_expert_counts(
    local_counts: torch.Tensor, group=None
) -> torch.Tensor:
    """Sum per-expert assignment counts across a process group.

    In EP, each rank only sees the tokens routed to its local experts; the bias
    update needs the global histogram.  With ``group=None`` or an
    uninitialised backend this is a no-op, which keeps unit tests simple.
    """
    if group is None or not comm.is_initialized():
        return local_counts
    return comm.all_reduce(local_counts, op=comm.SUM, group=group)


def node_limited_topk(
    scores: torch.Tensor,
    top_k: int,
    *,
    experts_per_node: int,
    max_nodes: int,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Top-k routing with at most ``max_nodes`` distinct nodes per token.

    Experts are laid out node-major: experts ``[0, epn)`` are on node 0,
    ``[epn, 2*epn)`` on node 1, and so on.  When the unconstrained top-k spans
    too many nodes, the node whose best selected expert has the *lowest* score
    is masked out and the top-k is recomputed.  Masking the weakest node is a
    deterministic, easy-to-audit rule; production implementations may use a
    capacity-aware variant.
    """
    if scores.dim() != 2:
        raise ValueError("scores must be [tokens, num_experts]")
    tokens, experts = scores.shape
    if experts % experts_per_node != 0:
        raise ValueError("num_experts must be divisible by experts_per_node")
    if not 1 <= top_k <= experts:
        raise ValueError("top_k out of range")
    if max_nodes < 1 or max_nodes * experts_per_node < top_k:
        raise ValueError("max_nodes * experts_per_node must cover top_k")

    num_nodes = experts // experts_per_node
    node_of = torch.arange(experts, device=scores.device) // experts_per_node
    result_scores = torch.empty(tokens, top_k, dtype=scores.dtype, device=scores.device)
    result_indices = torch.empty(tokens, top_k, dtype=torch.long, device=scores.device)

    for i in range(tokens):
        row = scores[i].clone()
        for _ in range(num_nodes):
            top = row.topk(top_k)
            selected_nodes = torch.unique(node_of[top.indices])
            if selected_nodes.numel() <= max_nodes:
                break
            # Score of a selected node = its best expert's score.  Drop the
            # weakest selected node to make room for a stronger one.
            node_best = torch.full(
                (num_nodes,), float("-inf"), dtype=row.dtype, device=row.device
            )
            for node in selected_nodes.tolist():
                mask = node_of == node
                node_best[node] = row[mask].max()
            drop = selected_nodes[torch.argmin(node_best[selected_nodes])]
            row = row.masked_fill(node_of == drop, float("-inf"))
        else:  # pragma: no cover - max_nodes >= 1 guarantees a break
            raise RuntimeError("node-limited routing did not converge")
        top = row.topk(top_k)
        result_scores[i] = top.values
        result_indices[i] = top.indices
    return result_scores, result_indices
