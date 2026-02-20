"""
Module for interacting with 1inch API to swap tokens
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from app.liquidation.config_loader import ChainConfig

from web3 import Web3

from app.liquidation.config_loader import load_chain_config
from app.liquidation.contracts import create_contract_instance
from app.liquidation.decorators import make_api_request
from app.liquidation.logging_config import setup_logger

logger = setup_logger()


class OneInchSwapper:
    """
    Class to handle token swaps using 1inch API
    """

    def __init__(self, config: "ChainConfig") -> None:
        self.config = config
        self.w3 = config.w3
        self.chain_id = config.CHAIN_ID
        self.api_base_url = "https://api.1inch.dev/swap/v6.0"
        self.api_key = config.ONEINCH_API_KEY

        if not self.api_key:
            logger.warning("1inch API key not found in configuration. API requests may be rate limited.")

        self.headers = {"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def get_swap_quote(
        self, src_token: str, dst_token: str, amount: int, slippage: float = 1.0, disable_estimate: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Get a swap quote from 1inch API

        Args:
            src_token (str): Source token address
            dst_token (str): Destination token address
            amount (int): Amount of source token to swap (in wei)
            slippage (float): Maximum acceptable slippage in percentage (default: 1.0)
            disable_estimate (bool): Disable estimation of returned tokens

        Returns:
            Optional[Dict[str, Any]]: Quote data if successful, None otherwise
        """
        try:
            # Convert addresses to checksum format
            src_token = Web3.to_checksum_address(src_token)
            dst_token = Web3.to_checksum_address(dst_token)

            # Prepare request parameters
            params = {
                "src": src_token,
                "dst": dst_token,
                "amount": str(amount),
                "slippage": str(slippage),
                "disableEstimate": str(disable_estimate).lower(),
                "from": self.config.LIQUIDATOR_EOA,
            }

            # Make API request
            url = f"{self.api_base_url}/{self.chain_id}/quote"
            time.sleep(1.1)
            response = make_api_request(url, headers=self.headers, params=params)

            if not response:
                logger.error("Failed to get quote from 1inch API")
                return None

            return response

        except Exception as ex:
            logger.error("Error getting swap quote: %s", ex, exc_info=True)
            return None

    def get_swap_transaction(
        self,
        src_token: str,
        dst_token: str,
        amount: int,
        externallyLiquidated: bool,
        slippage: float = 1.0,
        recipient: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get a swap transaction from 1inch API

        Args:
            src_token (str): Source token address
            dst_token (str): Destination token address
            amount (int): Amount of source token to swap (in wei)
            slippage (float): Maximum acceptable slippage in percentage (default: 1.0)
            recipient (Optional[str]): Address to receive the swapped tokens (default: sender)

        Returns:
            Optional[Dict[str, Any]]: Transaction data if successful, None otherwise
        """
        if amount == 0:
            # amount == zero implies no assets need swapping
            # this happens in the external liquidation case if 100% of the target asset debt is liquidated
            if not externallyLiquidated:
                # So if this branch is entered and the position is NOT externally liquidated, it's an error
                logger.error("Amount is zero, no swap")
            return None
        else:
            try:
                # Convert addresses to checksum format
                src_token = Web3.to_checksum_address(src_token)
                dst_token = Web3.to_checksum_address(dst_token)
                recipient = Web3.to_checksum_address(recipient)

                # Prepare request parameters
                params = {
                    "src": src_token,
                    "dst": dst_token,
                    "amount": str(amount),
                    "slippage": str(slippage),
                    "from": recipient,
                    "receiver": recipient,
                    "disableEstimate": True,
                }

                logger.info(
                    "==1inch Swap Transaction Info==src: %s, dst: %s, amount: %s, slippage: %s, from: %s, receiver: %s",
                    src_token,
                    dst_token,
                    str(amount),
                    str(slippage),
                    self.config.LIQUIDATOR_EOA,
                    recipient,
                )

                # Make API request
                url = f"{self.api_base_url}/{self.chain_id}/swap"
                time.sleep(1.1)
                response = make_api_request(url, headers=self.headers, params=params)

                if not response:
                    logger.error("Failed to get swap transaction from 1inch API")
                    return None

                # Extract data from response
                # The response structure is different from the quote endpoint
                tx = response.get("tx")
                if not tx:
                    logger.error("No transaction data in response")
                    return None

                return tx

            except Exception as ex:
                logger.error("Error getting swap transaction: %s", ex, exc_info=True)
                return None

    def check_allowance(self, token_address: str, amount: int) -> bool:
        """
        Check if the 1inch router has allowance to spend tokens

        Args:
            token_address (str): Token address to check allowance for
            amount (int): Amount to check allowance against

        Returns:
            bool: True if allowance is sufficient, False otherwise
        """
        try:
            token_address = Web3.to_checksum_address(token_address)

            # Get the 1inch router address
            params = {"tokenAddress": token_address}
            url = f"{self.api_base_url}/{self.chain_id}/approve/spender"
            time.sleep(1.1)
            response = make_api_request(url, headers=self.headers, params=params)

            if not response:
                logger.error("Failed to get 1inch router address")
                return False

            router_address = Web3.to_checksum_address(response["address"])

            # Create token contract instance
            token_contract = create_contract_instance(token_address, self.config.ERC20_ABI_PATH, self.config)

            # Check allowance
            current_allowance = token_contract.functions.allowance(self.config.LIQUIDATOR_EOA, router_address).call()

            logger.info("Current allowance for %s: %s, required: %s", token_address, current_allowance, amount)

            return current_allowance >= amount

        except Exception as ex:
            logger.error("Error checking allowance: %s", ex, exc_info=True)
            return False

    def approve_token(self, token_address: str) -> Optional[str]:
        """
        Approve the 1inch router to spend tokens

        Args:
            token_address (str): Token address to approve

        Returns:
            Optional[str]: Transaction hash if successful, None otherwise
        """
        try:
            token_address = Web3.to_checksum_address(token_address)

            # Get the 1inch router address and approval transaction
            params = {
                "tokenAddress": token_address,
                "amount": str(2**256 - 1),  # Max uint256 value
            }
            url = f"{self.api_base_url}/{self.chain_id}/approve/transaction"
            time.sleep(1.1)
            response = make_api_request(url, headers=self.headers, params=params)

            if not response:
                logger.error("Failed to get approval transaction from 1inch API")
                return None

            # Prepare transaction
            tx = {
                "from": self.config.LIQUIDATOR_EOA,
                "to": response["to"],
                "data": response["data"],
                "value": int(response["value"]),
                "gasPrice": self.w3.eth.gas_price * 2,
                "nonce": self.w3.eth.get_transaction_count(self.config.LIQUIDATOR_EOA),
            }

            # Estimate gas
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx) * 2
                tx["gas"] = int(estimated_gas)
            except Exception as ex:
                logger.error("Error estimating gas for approval: %s", ex, exc_info=True)
                tx["gas"] = 100000  # Default gas limit for approvals

            # Sign and send transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.config.LIQUIDATOR_EOA_PRIVATE_KEY)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info("Approval transaction sent: %s", tx_hash.hex())

            # Wait for transaction receipt
            logger.info("Waiting for approval transaction confirmation...")
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if tx_receipt.status == 1:
                logger.info("Approval transaction confirmed successfully")
                return tx_hash.hex()
            else:
                logger.error("Approval transaction failed")
                return None

        except Exception as ex:
            logger.error("Error approving token: %s", ex, exc_info=True)
            return None

    def execute_swap(
        self, src_token: str, dst_token: str, amount: int, slippage: float = 1.0, recipient: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Execute a token swap using 1inch API

        Args:
            src_token (str): Source token address
            dst_token (str): Destination token address
            amount (int): Amount of source token to swap (in wei)
            slippage (float): Maximum acceptable slippage in percentage (default: 1.0)
            recipient (Optional[str]): Address to receive the swapped tokens (default: sender)

        Returns:
            Tuple[Optional[str], Optional[Dict[str, Any]]]: Transaction hash and swap data if successful, None otherwise
        """
        try:
            # Check and approve token if needed
            if not self.check_allowance(src_token, amount):
                logger.info("Insufficient allowance, approving token...")
                approval_tx = self.approve_token(src_token)
                if not approval_tx:
                    logger.error("Failed to approve token")
                    return None, None

            # Get swap transaction
            swap_data = self.get_swap_transaction(src_token, dst_token, amount, False, slippage, recipient)
            if not swap_data:
                logger.error("Failed to get swap transaction")
                return None, None

            # Prepare transaction
            tx = {
                "from": Web3.to_checksum_address(self.config.LIQUIDATOR_EOA),
                "to": Web3.to_checksum_address(swap_data["to"]),
                "data": swap_data["data"],
                "value": int(swap_data["value"]),
                "gasPrice": int(swap_data["gasPrice"]),
                "gas": int(swap_data["gas"]),
                "nonce": self.w3.eth.get_transaction_count(self.config.LIQUIDATOR_EOA),
            }

            # Sign and send transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.config.LIQUIDATOR_EOA_PRIVATE_KEY)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info("Swap transaction sent: %s", tx_hash.hex())

            # Wait for transaction receipt
            logger.info("Waiting for swap transaction confirmation...")
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if tx_receipt.status == 1:
                logger.info(
                    "Swap transaction confirmed successfully",
                )
                return tx_hash.hex(), swap_data
            else:
                logger.error("Swap transaction failed")
                return None, None

        except Exception as ex:
            logger.error("Error executing swap: %s", ex, exc_info=True)
            return None, None


def get_token_balance(token_address: str, owner_address: str, config) -> int:
    """
    Get the token balance for a specific address

    Args:
        token_address (str): Token address
        owner_address (str): Address to check balance for
        config: Configuration object

    Returns:
        int: Token balance in wei
    """
    try:
        token_address = Web3.to_checksum_address(token_address)
        owner_address = Web3.to_checksum_address(owner_address)

        token_contract = create_contract_instance(token_address, config.ERC20_ABI_PATH, config)
        balance = token_contract.functions.balanceOf(owner_address).call()

        return balance
    except Exception as ex:
        logger.error("Error getting token balance: %s", ex, exc_info=True)
        return 0


def main():
    """
    Main function to parse arguments and execute token swaps
    """
    parser = argparse.ArgumentParser(description="Swap tokens using 1inch API")
    parser.add_argument("--chain-id", type=int, default=8453, help="Chain ID (default: 8453 for Base)")
    parser.add_argument("--src-token", type=str, required=True, help="Source token address")
    parser.add_argument("--dst-token", type=str, required=True, help="Destination token address")
    parser.add_argument("--amount", type=str, help="Amount to swap (in token units, e.g., 1.5)")
    parser.add_argument("--amount-wei", type=int, help="Amount to swap (in wei)")
    parser.add_argument("--slippage", type=float, default=1.0, help="Maximum slippage percentage (default: 1.0)")
    parser.add_argument("--recipient", type=str, help="Address to receive swapped tokens (default: sender)")
    parser.add_argument("--all", action="store_true", help="Swap all available tokens")

    args = parser.parse_args()

    # Load config for the specified chain
    config = load_chain_config(args.chain_id)
    swapper = OneInchSwapper(config)

    # Determine amount to swap
    amount = 0
    if args.all:
        amount = get_token_balance(args.src_token, config.LIQUIDATOR_EOA, config)
        if amount == 0:
            logger.error("No tokens available to swap")
            return 1
    elif args.amount_wei:
        amount = args.amount_wei
    elif args.amount:
        # Get token decimals
        token_address = Web3.to_checksum_address(args.src_token)
        token_contract = create_contract_instance(token_address, config.ERC20_ABI_PATH, config)
        decimals = token_contract.functions.decimals().call()
        amount = int(float(args.amount) * (10**decimals))
    else:
        logger.error("Must specify either --amount, --amount-wei, or --all")
        return 1

    logger.info(
        "Swapping %s wei of token %s to token %s with %s%% slippage",
        amount,
        args.src_token,
        args.dst_token,
        args.slippage,
    )
    quote = swapper.get_swap_quote(args.src_token, args.dst_token, amount, args.slippage, args.recipient)
    logger.info("Quote: %s", quote)
    input("!!! DO YOU WANT TO SWAP?")

    # Execute swap
    tx_hash, swap_data = swapper.execute_swap(args.src_token, args.dst_token, amount, args.slippage, args.recipient)

    if tx_hash:
        print(f"Swap successful! Transaction hash: {tx_hash}")
        return 0
    else:
        print("Swap failed. Check logs for details.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
