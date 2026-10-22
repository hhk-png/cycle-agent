"""Exception types used by the teaching engine."""


class MiniDeepInferError(Exception):
    """Base class for all package errors."""


class ConfigError(MiniDeepInferError, ValueError):
    """Invalid configuration."""


class PromptTooLongError(MiniDeepInferError, ValueError):
    """Prompt plus requested output exceeds the model's context limit."""


class OutOfBlocksError(MiniDeepInferError):
    """The paged KV cache cannot satisfy an allocation."""


class StructuredOutputError(MiniDeepInferError):
    """A grammar could not be compiled or a token violates it."""


class EngineClosedError(MiniDeepInferError):
    """The engine has already been closed."""


class DrainingError(MiniDeepInferError):
    """The server is draining and no longer admits new requests."""
