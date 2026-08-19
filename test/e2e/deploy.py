"""Deploy the e2e fork contracts (DEV-579 Part 2).

The production liquidators route swaps to the 1inch router (fixed at deploy time),
so an e2e fork run deploys FRESH liquidators whose ``router`` is the Solidity
``MockSwapper``, and pre-funds the MockSwapper with the target assets so it can
deliver the swap output locally. The bot is then pointed at these liquidator
addresses via EULER/AAVE_LIQUIDATOR_OVERRIDE + SWAP_PROVIDER=mock.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from web3 import Web3

from .anvil import AnvilFork
from .artifacts import creation_bytecode

# Live mainnet addresses (chain 1) — mirror test/LiquidationStateBuilder.sol.
FACTORY = "0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332"
AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

_DEPLOYER = "0x000000000000000000000000000000000000c0DE"


@dataclass
class ForkContracts:
    mock_swapper: str
    euler_liquidator: str
    aave_liquidator: str


def _creation_hex(name: str, arg_types: list, args: list) -> str:
    bc = creation_bytecode(name)
    if not bc.startswith("0x"):
        bc = "0x" + bc
    if arg_types:
        bc += abi_encode(arg_types, args).hex()
    return bc


def deploy_fork_contracts(fork: AnvilFork, owner: str, fund: int = 5_000 * 10**18) -> ForkContracts:
    """Deploy MockSwapper + fork liquidators (router=MockSwapper) and fund the swapper.

    ``owner`` is the liquidator owner (use the bot's LIQUIDATOR_EOA). ``fund`` is the
    amount of each target asset (USDC scaled to 6dp, WETH 18dp) dealt to the swapper.
    """
    owner = Web3.to_checksum_address(owner)
    factory = Web3.to_checksum_address(FACTORY)

    mock = fork.deploy(_DEPLOYER, _creation_hex("MockSwapper", [], []))

    euler_liq = fork.deploy(
        _DEPLOYER,
        _creation_hex("TwyneLiquidator", ["address", "address", "address"], [owner, factory, mock]),
    )
    aave_liq = fork.deploy(
        _DEPLOYER,
        _creation_hex(
            "TwyneAaveLiquidator",
            ["address", "address", "address", "address"],
            [owner, factory, mock, Web3.to_checksum_address(AAVE_POOL)],
        ),
    )

    # Pre-fund the mock swapper so it can deliver the target asset on either path.
    fork.deal(USDC, mock, 5_000_000 * 10**6)  # USDC (6dp) for the Euler path
    fork.deal(WETH, mock, fund)  # WETH (18dp) for the Aave path

    return ForkContracts(mock_swapper=mock, euler_liquidator=euler_liq, aave_liquidator=aave_liq)
