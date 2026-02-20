"""
Standalone script to withdraw collateral from a Twyne collateral vault
"""

import sys
import time

from web3 import Web3

from app.liquidation.config_loader import load_chain_config
from app.liquidation.contracts import create_contract_instance
from app.liquidation.logging_config import setup_logger

logger = setup_logger()


def get_user_collateral_vaults(config, recipient_address: str = None) -> list:
    """
    Get all collateral vaults for a given recipient address
    """
    if not recipient_address:
        recipient_address = config.LIQUIDATOR_EOA

    vault_addresses = config.collateral_vault_factory.functions.getCollateralVaults(recipient_address).call()
    user_owned_vaults = []
    for vault_address in vault_addresses:
        vault = create_contract_instance(vault_address, config.CVAULT_ABI_PATH, config)
        owner = vault.functions.borrower().call()
        amount = vault.functions.totalAssets().call()
        if Web3.to_checksum_address(owner) == Web3.to_checksum_address(recipient_address) and amount > 0:
            user_owned_vaults.append(vault_address)

    logger.info(f"Found {len(user_owned_vaults)} active collateral vaults for {recipient_address}")
    return user_owned_vaults


def withdraw_collateral(vault_address: str, config, recipient_address: str = None) -> None:
    """
    Withdraw all available collateral from a specified vault

    Args:
        vault_address (str): The address of the collateral vault
        config: Configuration object with network settings
        recipient_address (str, optional): Address to receive the withdrawn collateral.
                                          Defaults to the liquidator EOA from config.

    Returns:
        tuple: (tx_hash, tx_receipt) if successful, (None, None) if failed
    """
    try:
        # Convert to checksum address
        vault_address = Web3.to_checksum_address(vault_address)

        # Set recipient to liquidator EOA if not specified
        if not recipient_address:
            recipient_address = config.LIQUIDATOR_EOA
        else:
            recipient_address = Web3.to_checksum_address(recipient_address)

        # Create contract instance
        vault_instance = create_contract_instance(vault_address, config.CVAULT_ABI_PATH, config)

        # Get collateral balance and max withdraw amount
        collateral_balance = vault_instance.functions.totalAssets().call()
        vault_instance.functions.maxWithdraw(vault_address).call()
        vault_owner = vault_instance.functions.borrower().call()
        # eWETH.balanceOf(vault_address)

        # Get transaction parameters
        nonce = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA)
        suggested_gas_price = int(config.w3.eth.gas_price * 50.0)

        logger.info("======= WITHDRAWAL =======")
        logger.info("Withdrawing collateral from vault: %s", vault_address)
        logger.info("collateral_balance: %s", collateral_balance)
        logger.info("Running withdrawal with nonce: %s", nonce)
        logger.info("vault_owner: %s", vault_owner)
        logger.info("recipient_address: %s", recipient_address)
        logger.info("======= END WITHDRAWAL =======")

        # Build withdrawal transaction
        withdrawal_tx = vault_instance.functions.redeemUnderlying(
            collateral_balance, recipient_address
        ).build_transaction(
            {
                "chainId": config.CHAIN_ID,
                "from": config.LIQUIDATOR_EOA,
                "gasPrice": suggested_gas_price,
                "nonce": nonce,
            }
        )

        # Sign and send transaction
        signed_tx = config.w3.eth.account.sign_transaction(withdrawal_tx, config.LIQUIDATOR_EOA_PRIVATE_KEY)

        tx_hash = config.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

        # Wait for transaction receipt
        logger.info("Transaction sent, hash: %s", tx_hash.hex())
        logger.info("Waiting for transaction confirmation...")
        time.sleep(5)
        tx_receipt = config.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        logger.info("Withdrawal successful!")
        logger.info("Transaction hash: %s", tx_hash.hex())
        logger.info("Gas used: %s", tx_receipt.gasUsed)

        return tx_hash.hex(), tx_receipt

    except Exception as ex:
        logger.error("Failed to withdraw collateral: %s", ex, exc_info=True)
        return None, None


def main():
    """
    Main function to parse arguments and execute withdrawal
    """
    # Load config for the specified chain using the load_chain_config function
    config = load_chain_config(8453)
    active_collateral_vaults = get_user_collateral_vaults(config)
    if len(active_collateral_vaults) > 0:
        for vault in active_collateral_vaults:
            tx_hash, tx_receipt = withdraw_collateral(vault, config)
            if tx_hash:
                print(f"Withdrawal successful! Transaction hash: {tx_hash}")
                return 0
            else:
                print("Withdrawal failed. Check logs for details.")
                return 1


if __name__ == "__main__":
    sys.exit(main())
