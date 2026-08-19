"""
Module for interacting with 1inch API to swap tokens
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from app.liquidation.config_loader import ChainConfig

from web3 import Web3

from app.liquidation.config_loader import load_chain_config
from app.liquidation.constants import ONEINCH_MIN_RETURN_END, ONEINCH_MIN_RETURN_OFFSET
from app.liquidation.contracts import create_contract_instance
from app.liquidation.decorators import make_api_request
from app.liquidation.logging_config import setup_logger
from app.liquidation.swap_provider import SwapData
from app.liquidation.vaults.base_vault import BaseLiquidator

logger = setup_logger()


def decode_1inch_min_return(swap_data_bytes: bytes) -> int:
    """Decode the guaranteed ``minReturnAmount`` from encoded 1inch v6 swap calldata.

    The min-return is a big-endian uint256 occupying the 32-byte slice
    ``[ONEINCH_MIN_RETURN_OFFSET:ONEINCH_MIN_RETURN_END]`` of the decoded swap-data
    bytes. Raises ``ValueError`` if the calldata is too short to contain that slot;
    valid 1inch v6 swap calldata is always long enough.
    """
    if len(swap_data_bytes) < ONEINCH_MIN_RETURN_END:
        raise ValueError(f"1inch swap data is too short to decode minReturn: {len(swap_data_bytes)} bytes")
    return int.from_bytes(swap_data_bytes[ONEINCH_MIN_RETURN_OFFSET:ONEINCH_MIN_RETURN_END], "big")


# 1inch's free tier allows ~1 request/second per API key. The old code slept a
# flat 1.1s BEFORE every request unconditionally, adding ≥2.2s of pure latency to
# the quote+swap sequence on the critical liquidation path even when no request had
# been made for minutes (finding P1). Instead, enforce a *minimum interval* between
# consecutive requests: a call only waits for the time still owed since the previous
# call, so an idle bot pays zero spacing latency and a bursting bot is still throttled
# to the allowed rate. Reactive 429 Retry-After back-off is handled separately and
# unchanged in decorators.retry_request (DEV-542) — this limiter is the proactive
# spacing layer and deliberately does not duplicate that logic.
ONEINCH_MIN_REQUEST_INTERVAL_SECONDS = 1.1


class _MinIntervalRateLimiter:
    """Token-bucket-style minimum-interval limiter shared across all swappers.

    All ``OneInchSwapper`` instances hit ``api.1inch.dev`` with the same API key
    (the key is per-account, not per-chain), so a single process-wide limiter is the
    correct granularity. ``acquire()`` reserves the next time slot under a lock, then
    sleeps OUTSIDE the lock for only the residual wait, so concurrent worker threads
    are spaced to the configured rate without one thread blocking another for the full
    interval. ``time.monotonic`` is used so wall-clock adjustments can't skew spacing.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = min_interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def acquire(self) -> float:
        """Block until this caller's slot is due. Returns the seconds slept (for tests/logging)."""
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed_at)
            # Reserve the slot AFTER this one so the next caller is spaced by min_interval.
            self._next_allowed_at = scheduled + self.min_interval
            sleep_for = scheduled - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        return sleep_for


# Process-wide limiter for all 1inch HTTP requests.
_ONEINCH_RATE_LIMITER = _MinIntervalRateLimiter(ONEINCH_MIN_REQUEST_INTERVAL_SECONDS)


def _slippage_for_min_return(expected_out: int, min_return: int, default_slippage: float = 1.0) -> float:
    """Pick a 1inch slippage % so the *guaranteed* minReturn covers ``min_return``.

    1inch is exact-in: it sets ``minReturnAmount = expectedOut * (1 - slippage)``. If
    slippage is a fixed % of the expected output, that floor can land *below* a hard
    downstream requirement (e.g. a flashloan repayment), so a swap can clear its own
    floor yet still under-deliver. We instead tighten slippage just enough that
    ``expectedOut * (1 - slippage/100) >= min_return``:

    - Comfortable margin (expectedOut >> min_return): returns ``default_slippage``
      (keeps the usual MEV/slippage protection).
    - Thin margin: returns a smaller slippage so the floor is pinned at ``min_return``.
    - Underwater (expectedOut <= min_return): returns 0.0 (tightest floor; the swap
      will revert downstream rather than silently under-deliver).
    """
    if expected_out <= 0:
        return default_slippage
    headroom_pct = max(0.0, (expected_out - min_return) / expected_out * 100.0)
    return min(default_slippage, headroom_pct)


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
            _ONEINCH_RATE_LIMITER.acquire()
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
                recipient = Web3.to_checksum_address(recipient or self.config.LIQUIDATOR_EOA)

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
                _ONEINCH_RATE_LIMITER.acquire()
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

    def get_swap_transaction_with_min_return(
        self,
        src_token: str,
        dst_token: str,
        amount: int,
        recipient: str,
        min_return: int,
        default_slippage: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        """Build a swap whose guaranteed ``minReturnAmount`` covers ``min_return``.

        Fetches a fresh quote to learn the expected output, derives a slippage that
        pins the 1inch floor at ``min_return`` for thin margins (and keeps
        ``default_slippage`` for comfortable ones), then requests the swap tx. This
        prevents the failure mode where a swap clears its own (expected-relative)
        floor but still under-delivers versus a hard downstream requirement such as a
        flashloan repayment.
        """
        quote = self.get_swap_quote(src_token, dst_token, amount, default_slippage)
        expected_out = 0
        if quote:
            raw = quote.get("dstAmount") or quote.get("toAmount")
            try:
                expected_out = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                expected_out = 0

        slippage = _slippage_for_min_return(expected_out, min_return, default_slippage)
        if expected_out and expected_out < min_return:
            logger.warning(
                "1inch quote (expectedOut=%s) is below the required minReturn=%s; "
                "swap will use slippage=0 and likely revert downstream",
                expected_out,
                min_return,
            )
        logger.info(
            "1inch min-return swap: expectedOut=%s, minReturn=%s, slippage=%.4f%%",
            expected_out,
            min_return,
            slippage,
        )
        return self.get_swap_transaction(
            src_token,
            dst_token,
            amount,
            externallyLiquidated=False,
            slippage=slippage,
            recipient=recipient,
        )

    def build_swap(
        self,
        src_token: str,
        dst_token: str,
        amount: int,
        recipient: str,
        min_return: int,
        externally_liquidated: bool,
    ) -> Optional["SwapData"]:
        """SwapProvider seam (DEV-579): build a swap leg and report its guaranteed min-return.

        Behavior-preserving wrapper over the existing 1inch flows: the external path uses
        an exact-in zero-slippage swap; the internal path tightens slippage so the
        guaranteed floor covers ``min_return`` (the flashloan-repayment requirement). The
        min-return is decoded from the returned 1inch v6 calldata via ``decode_1inch_min_return``.
        """
        if amount <= 0:
            return None

        if externally_liquidated:
            oneinch_data = self.get_swap_transaction(src_token, dst_token, int(amount), True, 0, recipient)
        else:
            oneinch_data = self.get_swap_transaction_with_min_return(
                src_token, dst_token, int(amount), recipient, min_return, default_slippage=1.0
            )

        if not oneinch_data or "data" not in oneinch_data:
            logger.error("Invalid 1inch swap data: %s", oneinch_data)
            return None

        calldata = bytes.fromhex(oneinch_data["data"].replace("0x", ""))
        try:
            decoded_min_return = decode_1inch_min_return(calldata)
        except ValueError as ex:
            logger.error("Cannot decode 1inch minReturn: %s", ex)
            return None
        return SwapData(calldata=calldata, min_return=decoded_min_return)

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
            _ONEINCH_RATE_LIMITER.acquire()
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
            _ONEINCH_RATE_LIMITER.acquire()
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
            }

            # Estimate gas
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx) * 2
                tx["gas"] = int(estimated_gas)
            except Exception as ex:
                logger.error("Error estimating gas for approval: %s", ex, exc_info=True)
                tx["gas"] = 100000  # Default gas limit for approvals

            # Sign and send transaction through the per-EOA nonce manager (DEV-530).
            # sign_and_send_transaction internally uses signed_tx.raw_transaction, so it
            # also carries the web3 v7 rename from DEV-539.
            tx_hash, _signed_tx_payload = BaseLiquidator.sign_and_send_transaction(tx, self.config)

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
            }

            # Sign and send transaction through the per-EOA nonce manager (DEV-530).
            # sign_and_send_transaction internally uses signed_tx.raw_transaction, so it
            # also carries the web3 v7 rename from DEV-539.
            tx_hash, _signed_tx_payload = BaseLiquidator.sign_and_send_transaction(tx, self.config)

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
    quote = swapper.get_swap_quote(args.src_token, args.dst_token, amount, args.slippage)
    logger.info("Quote: %s", quote)
    confirmation = input("!!! DO YOU WANT TO SWAP? Type y/yes to continue: ").strip().lower()
    if confirmation not in {"y", "yes"}:
        print("Swap aborted.")
        return 1

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
