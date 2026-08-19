"""
Protocol registry and vault type detection.

A single CollateralVaultFactory emits T_CollateralVaultCreated for both Euler-
and Aave-backed CVs. The two variants share the same proxy front-end, so we
classify by calling `targetVault()` on the CV:

  - Aave CV  → `targetVault` is the Aave Pool (the same pool address for every
               Aave CV on the chain)
  - Euler CV → `targetVault` is a per-asset Euler EVault (an EVK vault that
               holds the borrowed asset)

Why not `aToken()`? The CV proxy enforces an EVC-style guard that reverts on
direct view-function calls with selector `0x335c5fec` for **both** variants,
so the older `aToken()` probe could not distinguish them and silently
classified everything as Euler. `targetVault()` is callable on both proxies
without that guard, which is why we use it. See the test file for a
reproduction.
"""

from web3 import Web3

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
    Detect whether a collateral vault is Aave- or Euler-backed.

    Compares the CV's on-chain `targetVault()` against the configured Aave
    Pool address. Matching → Aave; anything else → Euler.

    Raises on RPC failure rather than silently defaulting, so a misroute
    can't masquerade as a normal "Euler" classification.
    """
    checksum_address = Web3.to_checksum_address(address)
    aave_pool = Web3.to_checksum_address(config.AAVE_POOL)

    # The AAVE CV ABI is a superset (both proxies expose targetVault()).
    instance = create_contract_instance(checksum_address, config.AAVE_CVAULT_ABI_PATH, config)
    target_vault = Web3.to_checksum_address(instance.functions.targetVault().call())

    if target_vault == aave_pool:
        logger.info("detect_protocol: %s is Aave (targetVault matches AAVE_POOL)", checksum_address)
        return "aave"

    logger.info("detect_protocol: %s is Euler (targetVault=%s)", checksum_address, target_vault)
    return "euler"
