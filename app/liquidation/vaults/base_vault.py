"""
Base classes for multi-protocol collateral vault liquidation.
"""

import math
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from web3 import Web3

from app.liquidation.config_loader import ChainConfig
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import post_error_notification

logger = setup_logger()

UINT256_MAX = int(2**256 - 1)


class BaseCollateralVault(ABC):
    """
    Abstract base class for collateral vaults across protocols (Euler, Aave, etc.).
    Shared scheduling, health score, serialization logic lives here.
    Protocol-specific contract setup and liquidation logic is abstract.
    """

    protocol: str = ""  # Subclasses must set this

    def __init__(self, address: str, config: ChainConfig):
        self.config = config
        self.address = Web3.to_checksum_address(address)

        self.time_of_next_update = time.time()
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        self.balance = 0
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0

        # Subclass fills these in via _init_protocol_contracts
        self.instance = None
        self.asset_address = None
        self.asset = None
        self.underlying_asset_address = None
        self.underlying_asset_symbol = ""
        self.target_asset = None
        self.liqbot_instance = None
        self.health_state_viewer = None

        self._init_protocol_contracts(config)

        self.balanceOf = self.instance.functions.balanceOf(self.address).call()

    @abstractmethod
    def _init_protocol_contracts(self, config: ChainConfig) -> None:
        """Set up protocol-specific contract instances."""

    @abstractmethod
    def get_collateral_for_borrower(self) -> int:
        """Calculate collateralForBorrower using protocol-specific oracle/pricing."""

    @abstractmethod
    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Simulate liquidation and return (profitable, data, params)."""

    def check_liquidation(self, liquidator_address: str) -> Tuple[bool, bool, int, int, int]:
        logger.info("Vault: Checking liquidation for collateral vault %s", self.address)
        try:
            canLiquidate = self.instance.functions.canLiquidate().call()
            externallyLiquidated = self.instance.functions.isExternallyLiquidated().call()
            max_release = self.instance.functions.maxRelease().call()
            max_repay = self.instance.functions.maxRepay().call()
            totalAssets = self.instance.functions.totalAssetsDepositedOrReserved().call()
            return (canLiquidate, externallyLiquidated, max_release, max_repay, totalAssets)
        except Exception as ex:
            logger.error(
                "Vault: Failed to check liquidation status for %s: %s",
                self.address, ex, exc_info=True,
            )
            # Return safe defaults - will be checked again on next update
            return (False, False, 0, 0, 0)

    def convert_to_assets(self, amount: int) -> int:
        return self.instance.functions.convertToAssets(amount).call()

    def get_balanceOf(self) -> int:
        return self.instance.functions.balanceOf(self.address).call()

    def get_balanceOfUnderlying(self):
        balanceOf = self.instance.functions.balanceOf(self.address).call()
        return self.asset.functions.convertToAssets(balanceOf).call()

    def get_health_score(self) -> Tuple[float, float]:
        try:
            externalHF, internalHF, external_liability_value, internal_liability_value = (
                self.health_state_viewer.functions.health(self.address).call()
            )

            self.internal_value_borrowed = internal_liability_value
            self.external_value_borrowed = external_liability_value

            if external_liability_value < 0 or internal_liability_value < 0:
                logger.error(
                    "Vault: %s has negative liability values: internal=%s, external=%s",
                    self.address, internal_liability_value, external_liability_value,
                )
                return (math.inf, math.inf)

            if external_liability_value == 0:
                externalHF = math.inf
            if internal_liability_value == 0:
                internalHF = math.inf

            if externalHF < 0 or internalHF < 0:
                logger.error(
                    "Vault: %s has negative health factors: internal=%s, external=%s",
                    self.address, internalHF, externalHF,
                )
                return (math.inf, math.inf)

            self.internal_health_score = internalHF / 1e18
            self.external_health_score = externalHF / 1e18

            logger.info(
                "Vault: %s, inHF: %s, extHF: %s, internal debt: %s, external debt: %s",
                self.address, self.internal_health_score, self.external_health_score,
                external_liability_value, internal_liability_value,
            )
            if internalHF < 1 or externalHF < 1:
                logger.info("  +++++=====Vault: %s can be liquidated!", self.address)
            return (internalHF, externalHF)
        except Exception as ex:
            logger.error("Vault: Failed to get health score for %s: %s", self.address, ex, exc_info=True)
            return (math.inf, math.inf)

    def update_liquidity(self) -> Tuple[float, float, bool]:
        self.get_health_score()
        self.get_time_of_next_update()
        try:
            externallyLiquidated = self.instance.functions.isExternallyLiquidated().call()
        except Exception as ex:
            logger.error(
                "Vault: Failed to check isExternallyLiquidated for %s: %s",
                self.address, ex, exc_info=True,
            )
            externallyLiquidated = False  # Safe default - will be checked again on next update
        return [self.internal_health_score, self.external_health_score, externallyLiquidated]

    def get_time_of_next_update(self) -> float:
        max_interval = self.config.MAX_UPDATE_INTERVAL_SECONDS

        # Empty vaults (no position) get checked at max interval
        if self.internal_health_score == math.inf and self.external_health_score == math.inf:
            self.time_of_next_update = time.time() + max_interval * random.uniform(0.9, 1.1)
            return self.time_of_next_update

        total_borrowed = self.internal_value_borrowed + self.external_value_borrowed
        if total_borrowed < self.config.TEENY:
            size_prefix = "TEENY"
        elif total_borrowed < self.config.MINI:
            size_prefix = "MINI"
        elif total_borrowed < self.config.SMALL:
            size_prefix = "SMALL"
        elif total_borrowed < self.config.MEDIUM:
            size_prefix = "MEDIUM"
        else:
            size_prefix = "LARGE"

        liq_time = getattr(self.config, f"{size_prefix}_LIQ")
        high_risk_time = getattr(self.config, f"{size_prefix}_HIGH")
        safe_time = getattr(self.config, f"{size_prefix}_SAFE")

        try:
            externally_liquidated = self.instance.functions.isExternallyLiquidated().call()
        except Exception as ex:
            logger.error(
                "Vault: Failed to check isExternallyLiquidated in get_time_of_next_update for %s: %s",
                self.address, ex, exc_info=True,
            )
            externally_liquidated = False  # Safe default

        if (
            self.internal_health_score <= self.config.HS_LIQUIDATION
            or self.external_health_score <= self.config.HS_LIQUIDATION
            or externally_liquidated
        ):
            time_gap = liq_time
        elif (
            self.internal_health_score < self.config.HS_HIGH_RISK
            or self.external_health_score < self.config.HS_HIGH_RISK
        ):
            ratio_internal = (self.internal_health_score - self.config.HS_LIQUIDATION) / (
                self.config.HS_HIGH_RISK - self.config.HS_LIQUIDATION
            )
            ratio_external = (self.external_health_score - self.config.HS_LIQUIDATION) / (
                self.config.HS_HIGH_RISK - self.config.HS_LIQUIDATION
            )
            time_gap_internal = liq_time + (high_risk_time - liq_time) * ratio_internal
            time_gap_external = liq_time + (high_risk_time - liq_time) * ratio_external
            time_gap = min(time_gap_internal, time_gap_external)
        elif self.internal_health_score < self.config.HS_SAFE or self.external_health_score < self.config.HS_SAFE:
            ratio_internal = (self.internal_health_score - self.config.HS_HIGH_RISK) / (
                self.config.HS_SAFE - self.config.HS_HIGH_RISK
            )
            ratio_external = (self.external_health_score - self.config.HS_HIGH_RISK) / (
                self.config.HS_SAFE - self.config.HS_HIGH_RISK
            )
            time_gap_internal = high_risk_time + (safe_time - high_risk_time) * ratio_internal
            time_gap_external = high_risk_time + (safe_time - high_risk_time) * ratio_external
            time_gap = min(time_gap_internal, time_gap_external)
        else:
            time_gap = safe_time

        # Cap time_gap at max interval
        time_gap = min(time_gap, max_interval)

        time_of_next_update = time.time() + time_gap * random.uniform(0.9, 1.1)

        if not (self.time_of_next_update < time_of_next_update and self.time_of_next_update > time.time()):
            self.time_of_next_update = time_of_next_update

        return self.time_of_next_update

    def to_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "protocol": self.protocol,
            "time_of_next_update": self.time_of_next_update,
            "internal_health_score": self.internal_health_score,
            "external_health_score": self.external_health_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], config: ChainConfig) -> "BaseCollateralVault":
        account = cls(address=data["address"], config=config)
        account.time_of_next_update = data["time_of_next_update"]
        account.internal_health_score = data["internal_health_score"]
        account.external_health_score = data["external_health_score"]
        return account


class BaseLiquidator(ABC):
    """Base class for protocol-specific liquidators."""

    @staticmethod
    def execute_liquidation(liquidation_transaction: Dict[str, Any], config: ChainConfig):
        try:
            logger.info("Liquidator: Executing liquidation transaction %s...", liquidation_transaction)
            signed_tx = config.w3.eth.account.sign_transaction(
                liquidation_transaction, config.LIQUIDATOR_EOA_PRIVATE_KEY
            )
            logger.info("Liquidator: Signed transaction: %s", signed_tx)
            tx_hash = config.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info("Liquidator: Transaction hash: %s", tx_hash)
            time.sleep(5)
            tx_receipt = config.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
            logger.info("Liquidator: Liquidation transaction executed successfully.")
            return tx_hash.hex(), tx_receipt
        except Exception as ex:
            message = f"Unexpected error in executing liquidation: {ex}"
            logger.error(message, exc_info=True)
            post_error_notification(message, config)
            return None, None

    @staticmethod
    @abstractmethod
    def calculate_liquidation_profit(vault, config: ChainConfig):
        """Calculate liquidation profit for a vault. Returns (profit_data, params)."""

    @staticmethod
    @abstractmethod
    def simulate_liquidation(vault, config: ChainConfig):
        """Simulate liquidation. Returns (bool, data, params)."""
