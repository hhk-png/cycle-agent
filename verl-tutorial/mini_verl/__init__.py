"""MiniVerl: a runnable, NumPy-only miniature of verl's HybridFlow design.

The package deliberately keeps the production names (DataProto, WorkerGroup,
Dispatch, ActorRolloutRefWorker, PPO trainer, GRPO advantages) so that the
tutorial's MiniVerl chapters can be read next to the real verl source.
"""

__version__ = "0.9.1-mini.1"

from .protocol import DataProto
from .config import load_config, PPOConfig

__all__ = ["DataProto", "PPOConfig", "load_config", "__version__"]
