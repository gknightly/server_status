# errors.py
"""Custom exceptions for the server status bot."""


class BotError(Exception):
    """Base exception for bot errors."""
    pass


class AWSError(BotError):
    """AWS operation failed."""
    pass


class MinecraftError(BotError):
    """Minecraft server check failed."""
    pass


class ConfigError(BotError):
    """Configuration invalid."""
    pass


class ProxyError(BotError):
    """Proxy operation failed."""
    pass
