"""Plumbing smoke test for the e2e fork harness (DEV-579).

Validates AnvilFork start + deal + deploy + archive reads before the full seeder.
Run: uv run pytest test/e2e/test_smoke.py -s
"""

import os

import pytest
from web3 import Web3

from .anvil import AnvilFork
from .artifacts import creation_bytecode

FORK_BLOCK = 25_340_000
FACTORY = "0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
EXPECTED_EVC = "0xef39D6493884C4C84D38a4bFF879Ce16CEdE702a"


@pytest.mark.e2e
def test_anvil_plumbing():
    fork_url = os.environ.get("E2E_FORK_RPC_URL", "https://eth.drpc.org")
    fork = AnvilFork(fork_url, FORK_BLOCK).start()
    try:
        w3 = fork.w3
        assert w3.is_connected()
        assert w3.eth.block_number >= FORK_BLOCK

        # archive read against the live factory
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(FACTORY),
            abi=[
                {
                    "inputs": [],
                    "name": "EVC",
                    "outputs": [{"type": "address"}],
                    "stateMutability": "view",
                    "type": "function",
                }
            ],
        )
        assert Web3.to_checksum_address(factory.functions.EVC().call()) == Web3.to_checksum_address(EXPECTED_EVC)

        # deal WETH
        holder = "0x00000000000000000000000000000000DeaDBeef"
        fork.deal(WETH, holder, 5 * 10**18)
        erc20 = w3.eth.contract(address=Web3.to_checksum_address(WETH), abi=_erc20_abi())
        assert erc20.functions.balanceOf(Web3.to_checksum_address(holder)).call() == 5 * 10**18

        # deploy MockSwapper and confirm code landed
        deployer = "0x000000000000000000000000000000000000C0DE"
        swapper = fork.deploy(deployer, creation_bytecode("MockSwapper"))
        assert len(w3.eth.get_code(swapper)) > 0
    finally:
        fork.stop()


def _erc20_abi():
    return [
        {
            "constant": True,
            "inputs": [{"name": "", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]
