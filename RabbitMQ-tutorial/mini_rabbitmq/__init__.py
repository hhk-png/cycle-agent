"""mini-rabbitmq：RabbitMQ 教程的工程化教学实现。"""

from .auth import AuthStore, default_auth
from .broker import Broker
from .errors import RabbitMQError
from .message import Delivery, Message
from .queue import Queue
from .vhost import PublishResult, VHost

__version__ = "0.1.0"

__all__ = [
    "AuthStore",
    "Broker",
    "Delivery",
    "Message",
    "PublishResult",
    "Queue",
    "RabbitMQError",
    "VHost",
    "default_auth",
    "__version__",
]
