"""
Standalone script to test Aave collateral vault liquidation on Base.

Usage:
    python test_aave_liquidation.py <collateral_vault_address>
"""

import logging
import sys

from dotenv import load_dotenv
load_dotenv()

from app.liquidation.config_loader import load_chain_config
from app.liquidation.vaults.aave_vault import AaveCollateralVault, AaveLiquidator
from app.liquidation.vaults.base_vault import BaseLiquidator as Liquidator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("test_aave_liquidation")

BASE_CHAIN_ID = 8453


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_aave_liquidation.py <collateral_vault_address>")
        sys.exit(1)

    vault_address = sys.argv[1]
    config = load_chain_config(BASE_CHAIN_ID)

    logger.info("Creating AaveCollateralVault for %s", vault_address)
    vault = AaveCollateralVault(vault_address, config)

    logger.info("Underlying asset: %s", vault.underlying_asset_address)
    logger.info("Target asset: %s", vault.target_asset)
    logger.info("Aave pool: %s", vault.aave_pool_address)

    # Check liquidation status
    can_liq, ext_liq, max_release, max_repay, total_assets = vault.check_liquidation(config.LIQUIDATOR_EOA)
    logger.info("canLiquidate=%s, externallyLiquidated=%s, maxRelease=%s, maxRepay=%s, totalAssets=%s",
                can_liq, ext_liq, max_release, max_repay, total_assets)

    health_factor = vault.get_health_factor()
    logger.info("Aave health factor: %s", health_factor)

    if not can_liq and not ext_liq:
        logger.info("Vault is not liquidatable. Exiting.")
        return

    # Build liquidation transaction
    logger.info("Calculating liquidation profit...")
    result, tx = AaveLiquidator.calculate_liquidation_profit(vault, config)
    logger.info("Result: %s", result)

    if tx is None:
        logger.info("No viable liquidation transaction built. Reason: %s", result.get("reason", "unknown"))
        return

    logger.info("Transaction built successfully:")
    logger.info("  to: %s", tx.get("to"))
    logger.info("  gas: %s", tx.get("gas"))
    logger.info("  data length: %s bytes", len(tx.get("data", "")) // 2)

    # Execute
    answer = input("EXECUTE TX? (y/n)")
    if answer != "y":
        logger.info("Exiting.")
        return

    logger.info("Executing liquidation transaction...")
    tx_hash, tx_receipt = Liquidator.execute_liquidation(tx, config)

    if tx_hash:
        logger.info("Liquidation succeeded! TX hash: %s", tx_hash)
        logger.info("Gas used: %s", tx_receipt.get("gasUsed") if tx_receipt else "unknown")
        logger.info("Explorer: %s/tx/%s", config.EXPLORER_URL, tx_hash)
    else:
        logger.error("Liquidation failed.")


if __name__ == "__main__":
    main()
