"""
Custom exceptions for the liquidation bot.
"""


class LiquidationBotError(Exception):
    """Base exception for all liquidation bot errors."""


class ConfigError(LiquidationBotError):
    """Raised for configuration-related errors."""


class ProtocolDetectionError(LiquidationBotError):
    """Raised when protocol detection fails."""


class LiquidationError(LiquidationBotError):
    """Raised for errors during liquidation execution."""


class SwapError(LiquidationBotError):
    """Raised for errors during token swaps."""


class TransactionBuildError(LiquidationError):
    """Raised when building a liquidation transaction fails."""
