"""Fixtures for the e2e fork harness (DEV-579).

Session-scoped anvil fork + fork-deployed contracts, with a per-test snapshot/revert
so each variant is seeded on a clean baseline (after deploy + funding).

The bot signer is a throwaway key DERIVED at runtime from a fixed seed (never a key
literal in a tracked file, per CLAUDE.md §2.15) and funded with ETH on the fork.
"""

import os

import pytest
from eth_account import Account
from web3 import Web3

from app.liquidation.config_loader import load_chain_config

from .anvil import AnvilFork
from .deploy import deploy_fork_contracts
from .state_seeder import StateSeeder
from .upgrade_107 import apply_upgrade, is_upgraded, should_apply

# Twyne 1.0.7 is not on mainnet yet, so the harness forks the LATEST block and applies
# the upgrade itself (see upgrade_107.py). Set E2E_FORK_BLOCK to pin a block instead.
_DEFAULT_RPC = "https://eth.drpc.org"


def _fork_url() -> str:
    return os.environ.get("E2E_FORK_RPC_URL", _DEFAULT_RPC)


def _fork_block(url: str) -> int:
    pinned = os.environ.get("E2E_FORK_BLOCK")
    if pinned:
        return int(pinned)
    # Fork one block behind the head: anvil serves the base block from the upstream
    # archive, and the very tip can still be re-organised while the suite runs.
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60})).eth.block_number - 1


# Throwaway signer derived at runtime — no secret material in the repo.
_SIGNER = Account.from_key(Web3.keccak(text="twyne-dev579-e2e-liquidator-signer"))
LIQUIDATOR_EOA = _SIGNER.address
LIQUIDATOR_PK = "0x" + _SIGNER.key.hex().removeprefix("0x")


@pytest.fixture(scope="session")
def fork():
    url = _fork_url()
    block = _fork_block(url)
    f = AnvilFork(url, block).start()
    f.fork_base_block = block
    f.set_balance(LIQUIDATOR_EOA, 10**21)  # 1000 ETH for gas
    if should_apply():
        apply_upgrade(f)
    assert is_upgraded(f), (
        "the fork does not run Twyne 1.0.7. Set E2E_APPLY_TWYNE_107=1 to apply Safe nonce 29 "
        "on the fork, or fork a block after the upgrade goes live."
    )
    yield f
    f.stop()


@pytest.fixture(scope="session")
def deployed(fork):
    return deploy_fork_contracts(fork, owner=LIQUIDATOR_EOA)


@pytest.fixture(scope="session")
def _baseline(fork, deployed):
    # Snapshot taken AFTER deploy + funding; each test reverts here for isolation.
    return {"id": fork.snapshot()}


@pytest.fixture
def seeder(fork, deployed, _baseline):
    # evm_revert consumes the snapshot id, so re-snapshot for the next test.
    fork.revert(_baseline["id"])
    _baseline["id"] = fork.snapshot()
    fork.set_balance(LIQUIDATOR_EOA, 10**21)
    return StateSeeder(fork)


@pytest.fixture
def bot_config(fork, deployed, seeder, monkeypatch):
    """A real ChainConfig (chain 1) pointed at the fork, with the bot wired to use the
    mock swap provider and the fork-deployed liquidators."""
    monkeypatch.setenv("MAINNET_RPC_URL", fork.rpc_url)
    monkeypatch.setenv("LIQUIDATOR_EOA", LIQUIDATOR_EOA)
    monkeypatch.setenv("LIQUIDATOR_PRIVATE_KEY", LIQUIDATOR_PK)
    monkeypatch.setenv("ONEINCH_API_KEY", "unused-with-mock-swapper")
    monkeypatch.setenv("RISK_DASHBOARD_URL", "http://localhost")
    monkeypatch.setenv("SWAP_PROVIDER", "mock")
    monkeypatch.setenv("EULER_LIQUIDATOR_OVERRIDE", deployed.euler_liquidator)
    monkeypatch.setenv("AAVE_LIQUIDATOR_OVERRIDE", deployed.aave_liquidator)
    monkeypatch.setenv("HEALTHSTATVIEWER_OVERRIDE", deployed.health_stat_viewer)
    # Narrow the FactoryListener startup scan (Tier B) to the fork's post-base blocks.
    # Start ABOVE the fork base: a getLogs range that includes the base block makes anvil
    # proxy to the (rate-limited) archive RPC; blocks > base are served locally.
    monkeypatch.setenv("CVAULT_FACTORY_DEPLOYMENT_BLOCK_OVERRIDE", str(fork.fork_base_block + 1))
    return load_chain_config(1)
