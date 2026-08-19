"""
Swap-provider seam (DEV-579).

Abstracts the DEX leg of a liquidation so it can be satisfied either by the live
1inch API (production) or by a local mock contract on an anvil fork (e2e tests).

The liquidator contracts (`TwyneLiquidator` / `TwyneAaveLiquidator`) treat the
swap as opaque `dexData` bytes forwarded verbatim to `router.call(dexData)`, where
`router` is fixed at deploy time. In production `router` is the 1inch router and
`dexData` is 1inch calldata; in an e2e fork run a fresh liquidator is deployed with
`router` = the Solidity `MockSwapper`, and this module's `MockSwapProvider` emits
the matching `MockSwapper.swap(...)` calldata.

The one-method `SwapProvider` interface returns both the calldata and the
guaranteed `min_return`. For 1inch the min-return is decoded from the calldata; for
the mock it is the amount the on-chain mock is instructed to deliver. Surfacing
min_return here (rather than re-decoding 1inch-shaped calldata at the call site)
is what lets the mock participate without producing 1inch-formatted bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from eth_abi import encode as abi_encode
from web3 import Web3

from app.liquidation.logging_config import setup_logger

if TYPE_CHECKING:
    from app.liquidation.config_loader import ChainConfig

logger = setup_logger()


@dataclass
class SwapData:
    """Result of building a swap leg.

    Attributes:
        calldata: the opaque ``dexData`` bytes passed to the liquidator's
            ``liquidateCollateralVault`` / ``liquidateExtLiquidatedCollateralVault``.
        min_return: the guaranteed output amount (target-asset units) the swap will
            deliver — decoded from 1inch calldata, or chosen by the mock.
    """

    calldata: bytes
    min_return: int


@runtime_checkable
class SwapProvider(Protocol):
    """The smallest interface the liquidation execute path needs from a swapper."""

    def build_swap(
        self,
        src_token: str,
        dst_token: str,
        amount: int,
        recipient: str,
        min_return: int,
        externally_liquidated: bool,
    ) -> Optional[SwapData]:
        """Build the swap leg, or return None if no swap can/should be made."""
        ...


# Selector for the Solidity MockSwapper.swap(address,address,uint256,uint256,address)
# (test/MockSwapper.sol). Kept as a module constant so the encoding is computed once.
_MOCK_SWAP_SELECTOR = Web3.keccak(text="swap(address,address,uint256,uint256,address)")[:4]

# MockSwapper does a hard transferFrom of the encoded amountIn from the liquidator. The
# bot's ``amount`` is a swap target that already carries its own safety margin below the
# seized balance, so we pull ~all of it (tiny buffer for rounding); leftover dust stays
# in the liquidator harmlessly.
_MOCK_INPUT_PULL_BPS = 9_990  # pull 99.9% of the bot's amountIn target

# The on-chain flashloan repayment uses ``maxRepay`` read at EXECUTION time, which is
# slightly higher than the simulate-time ``min_return`` the bot requests (interest
# accrues over the few blocks between simulate and send). The mock is funded generously,
# so it over-delivers a small buffer above ``min_return`` to guarantee the flashloan is
# always repaid regardless of that accrual (mirrors a real swap clearing its floor).
_MOCK_OUTPUT_BUFFER_BPS = 100  # deliver min_return + 1%


class MockSwapProvider:
    """Test-only swap provider (DEV-579).

    Emits calldata for the on-chain Solidity ``MockSwapper`` deployed on the anvil
    fork: ``swap`` pulls ``amount`` of ``src_token`` from the liquidator and
    transfers ``out_amount`` of ``dst_token`` to ``recipient`` (the liquidator).
    The on-chain ``MockSwapper`` must be pre-funded with ``dst_token`` by the e2e
    seeder.

    ``out_amount`` is pinned to the caller's required ``min_return`` so the
    flashloan is always repaid and the on-chain ``minProfit`` floor is met, while
    the gross-profit decision the bot makes upstream stays oracle-driven (unchanged).
    """

    def __init__(self, config: "ChainConfig") -> None:
        self.config = config

    def build_swap(
        self,
        src_token: str,
        dst_token: str,
        amount: int,
        recipient: str,
        min_return: int,
        externally_liquidated: bool,
    ) -> Optional[SwapData]:
        if amount <= 0:
            # Zero-amount swaps occur on the external zero-debt path — no swap needed.
            return None

        out_amount = int(min_return) + int(min_return) * _MOCK_OUTPUT_BUFFER_BPS // 10_000
        amount_in = max(1, int(amount) * _MOCK_INPUT_PULL_BPS // 10_000)
        src_token = Web3.to_checksum_address(src_token)
        dst_token = Web3.to_checksum_address(dst_token)
        recipient = Web3.to_checksum_address(recipient)

        encoded_args = abi_encode(
            ["address", "address", "uint256", "uint256", "address"],
            [src_token, dst_token, amount_in, out_amount, recipient],
        )
        calldata = _MOCK_SWAP_SELECTOR + encoded_args

        logger.info(
            "MockSwapProvider: src=%s, dst=%s, amountIn=%s (target=%s), amountOut=%s, recipient=%s",
            src_token,
            dst_token,
            amount_in,
            amount,
            out_amount,
            recipient,
        )
        return SwapData(calldata=calldata, min_return=out_amount)


def make_swap_provider(config: "ChainConfig") -> SwapProvider:
    """Select the swap provider for this run.

    Default is the live 1inch API (production). Set ``SWAP_PROVIDER=mock`` (e2e fork
    runs, DEV-579) to route the swap leg through ``MockSwapProvider`` against an
    on-chain ``MockSwapper``. Any other value falls back to 1inch.
    """
    provider = getattr(config, "SWAP_PROVIDER", "1inch")
    if provider == "mock":
        logger.info("Swap provider: mock (DEV-579 e2e)")
        return MockSwapProvider(config)
    # Imported lazily to avoid an import cycle: swap_1inch imports base_vault, which
    # is imported by the vault modules that import this factory.
    from app.liquidation.swap_1inch import OneInchSwapper

    return OneInchSwapper(config)
