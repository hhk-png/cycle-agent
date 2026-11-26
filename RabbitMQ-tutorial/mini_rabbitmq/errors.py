"""异常体系。

真实 AMQP 0-9-1 的错误分为“信道异常”和“连接异常”。本模块把这两类错误
都建模成带 reply-code 的异常，网络层根据 ``hard`` 决定关闭信道还是连接。
"""

from __future__ import annotations


class RabbitMQError(Exception):
    """所有 mini-rabbitmq 异常的基类。"""

    default_code = 541

    def __init__(self, message: str = "", *, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = self.default_code if code is None else code

    def __str__(self) -> str:  # pragma: no cover - 直观字符串
        return f"[{self.code}] {self.message}" if self.message else f"[{self.code}]"


class ConnectionError(RabbitMQError):
    default_code = 501


class ChannelError(RabbitMQError):
    default_code = 406


class NotFound(ChannelError):
    default_code = 404


class ResourceLocked(ChannelError):
    default_code = 405


class PreconditionFailed(ChannelError):
    default_code = 406


class AccessRefused(ChannelError):
    default_code = 403


class InvalidArgument(ChannelError):
    default_code = 406


class AlreadyExists(ChannelError):
    default_code = 406


class NoRoute(ChannelError):
    default_code = 312


class NotAllowed(ChannelError):
    default_code = 530


class NotImplementedError_(ChannelError):
    default_code = 540

    def __init__(self, message: str = "not implemented") -> None:
        super().__init__(message)


class ResourceError(ChannelError):
    default_code = 506


class ProtocolError(ConnectionError):
    default_code = 501


class SyntaxError_(ConnectionError):
    default_code = 502


class FrameError(ConnectionError):
    default_code = 501


class UnexpectedFrame(ConnectionError):
    default_code = 505


class InternalError(RabbitMQError):
    default_code = 541


class PublishRejected(ChannelError):
    """队列长度/字节上限导致的发布拒绝（publisher confirm 会回 nack）。"""

    default_code = 406


class NoQuorum(RabbitMQError):
    default_code = 506


# 便于网络层和文档引用的 reply-code 表
REPLY_CODES = {
    311: "CONTENT_TOO_LARGE",
    312: "NO_ROUTE",
    313: "NO_CONSUMERS",
    320: "CONNECTION_FORCED",
    402: "INVALID_PATH",
    403: "ACCESS_REFUSED",
    404: "NOT_FOUND",
    405: "RESOURCE_LOCKED",
    406: "PRECONDITION_FAILED",
    501: "FRAME_ERROR",
    502: "SYNTAX_ERROR",
    503: "COMMAND_INVALID",
    504: "CHANNEL_ERROR",
    505: "UNEXPECTED_FRAME",
    506: "RESOURCE_ERROR",
    530: "NOT_ALLOWED",
    540: "NOT_IMPLEMENTED",
    541: "INTERNAL_ERROR",
}
