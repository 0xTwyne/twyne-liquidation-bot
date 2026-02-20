"""
Protocol registry and vault type detection.

There is a single CollateralVaultFactory that emits events for both Euler and Aave
collateral vaults. To distinguish them, we call .aToken() on the vault using the
Aave ABI (superset). If it succeeds, it's Aave; if it reverts, it's Euler.
"""

from app.liquidation.contracts import create_contract_instance
from app.liquidation.logging_config import setup_logger
from app.liquidation.vaults.aave_vault import AaveCollateralVault
from app.liquidation.vaults.euler_vault import EulerCollateralVault

logger = setup_logger()

PROTOCOL_REGISTRY = {
    "euler": {
        "vault_class": EulerCollateralVault,
    },
    "aave": {
        "vault_class": AaveCollateralVault,
    },
}


def get_vault_class_for_protocol(protocol: str):
    """Return the vault class for a given protocol name."""
    entry = PROTOCOL_REGISTRY.get(protocol)
    if not entry:
        raise ValueError(f"Unknown protocol: {protocol}")
    return entry["vault_class"]


def detect_protocol(address: str, config) -> str:
    """
    Detect whether a collateral vault is Euler or Aave by calling .aToken().
    Uses the Aave ABI (superset of Euler ABI). If .aToken() reverts, it's Euler.

    Returns:
        "aave" or "euler"
    """
    try:
        instance = create_contract_instance(address, config.AAVE_CVAULT_ABI_PATH, config)
        atoken = instance.functions.aToken().call()
        # If call succeeds and returns a non-zero address, it's Aave
        if atoken and atoken != "0x0000000000000000000000000000000000000000":
            logger.info("detect_protocol: %s is Aave (aToken=%s)", address, atoken)
            return "aave"
    except Exception as ex:
        logger.debug("detect_protocol: aToken() call failed for %s: %s", address, ex)

    logger.info("detect_protocol: %s is Euler (aToken() reverted or returned zero)", address)
    return "euler"
