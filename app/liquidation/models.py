"""
Data classes for structured returns in the liquidation bot.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class LiquidationCheckResult:
    """Result of checking whether a vault can be liquidated."""

    can_liquidate: bool
    externally_liquidated: bool
    max_release: int
    max_repay: int
    total_assets: int


@dataclass
class LiquidationData:
    """Data describing a liquidation opportunity or result."""

    tx: Optional[Dict[str, Any]]
    profit: int
    collateral_address: Optional[str] = None
    collateral_asset: Optional[str] = None
    reason: Optional[str] = None
    shortfall: Optional[int] = None


@dataclass
class SimulationResult:
    """Result of simulating a liquidation."""

    profitable: bool
    data: Optional[LiquidationData] = None
    params: Optional[Tuple[Any, ...]] = None


@dataclass
class HealthUpdate:
    """Health score update for a vault."""

    internal_health_score: float
    external_health_score: float
    externally_liquidated: bool


@dataclass
class AccountHealthEntry:
    """A single account's health information for reporting."""

    address: str
    internal_health_score: float
    external_health_score: float
    balance: int
    internal_value_borrowed: int
    external_value_borrowed: int
    underlying_asset_symbol: str
