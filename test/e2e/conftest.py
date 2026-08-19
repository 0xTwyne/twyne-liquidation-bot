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

FORK_BLOCK = 25_340_000

# Throwaway signer derived at runtime — no secret material in the repo.
_SIGNER = Account.from_key(Web3.keccak(text="twyne-dev579-e2e-liquidator-signer"))
LIQUIDATOR_EOA = _SIGNER.address
LIQUIDATOR_PK = "0x" + _SIGNER.key.hex().removeprefix("0x")


@pytest.fixture(scope="session")
def fork():
    url = os.environ.get("E2E_FORK_RPC_URL", "https://eth.drpc.org")
    f = AnvilFork(url, FORK_BLOCK).start()
    f.set_balance(LIQUIDATOR_EOA, 10**21)  # 1000 ETH for gas
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
    # Narrow the FactoryListener startup scan (Tier B) to the fork's post-base blocks.
    # Start ABOVE the fork base: a getLogs range that includes the base block makes anvil
    # proxy to the (rate-limited) archive RPC; blocks > base are served locally.
    monkeypatch.setenv("CVAULT_FACTORY_DEPLOYMENT_BLOCK_OVERRIDE", str(FORK_BLOCK + 1))
    return load_chain_config(1)
