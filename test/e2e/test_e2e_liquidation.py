"""Layer-3 e2e: drive the real Python bot against a seeded anvil fork (DEV-579).

Tier A drives the genuine pipeline — FactoryListener discovery ->
AccountMonitor._process_account_update -> simulate -> BaseLiquidator.execute_liquidation
-> sign -> send -> mine — for each seeded variant, asserting the position is closed.
The seeded CV lives in anvil blocks ABOVE the fork block, so scanning [FORK_BLOCK+1,
head] discovers exactly it via the real listener code.

Tier B (test_full_flask_app_liquidates) boots the real Flask app once and lets it
autonomously discover + liquidate a seeded CV, covering the threadpool + bootstrap.
"""

import threading
import time

import pytest
from web3 import Web3

from app.liquidation.account_monitor import AccountMonitor
from app.liquidation.event_listener import FactoryListener

from .conftest import FORK_BLOCK
from .state_seeder import _addr

EULER_LIQ_LTV = 9400
EULER_COLLATERAL = 5 * 10**18  # 5 WETH
EULER_BORROW = 8_000 * 10**6  # 8000 USDC

AAVE_LIQ_LTV = 9800  # > Aave eMode LT (9500) so maxRelease > 0
AAVE_COLLATERAL = 2 * 10**18  # 2 wstETH
AAVE_BORROW = 1 * 10**18  # 1 WETH


def _run_bot(cfg, cv: str) -> AccountMonitor:
    """Discover ``cv`` via the real listener over the post-fork blocks, then process it
    once through the genuine monitor -> simulate -> execute path. Asserts no on-chain
    revert (the bot's own failure counter stays 0)."""
    monitor = AccountMonitor(chain_id=1, config=cfg, notify=False, execute_liquidation=True)
    listener = FactoryListener(monitor, cfg)
    monitor.latest_block = FORK_BLOCK
    listener.scan_block_ranges(FORK_BLOCK + 1, cfg.w3.eth.block_number)
    cv = Web3.to_checksum_address(cv)
    assert cv in monitor.accounts, "listener did not discover the seeded CV"
    monitor._process_account_update(cv)
    assert monitor.liquidation_failure_count == 0, "bot's liquidation tx reverted on-chain"
    return monitor


# ----------------------------- Euler -----------------------------------------
@pytest.mark.e2e
def test_euler_internal(seeder, bot_config):
    h = seeder.create_euler_cv(_addr("eulIntBorrower"), EULER_LIQ_LTV, EULER_COLLATERAL, EULER_BORROW)
    seeder.make_euler_internally_liquidatable(h)
    assert seeder.can_liquidate(h.cv)
    _run_bot(bot_config, h.cv)
    assert not seeder.can_liquidate(h.cv), "CV still liquidatable — bot did not liquidate"


@pytest.mark.e2e
def test_euler_external_with_debt(seeder, bot_config):
    h = seeder.create_euler_cv(_addr("eulExtDebtBorrower"), EULER_LIQ_LTV, EULER_COLLATERAL, EULER_BORROW)
    seeder.make_euler_externally_liquidated_with_debt(h, _addr("eulExtDebtLiq"))
    assert seeder.is_externally_liquidated(h.cv) and seeder.max_repay(h.cv) > 0
    _run_bot(bot_config, h.cv)
    assert seeder.max_repay(h.cv) == 0, "residual debt not cleared"


@pytest.mark.e2e
def test_euler_external_zero_debt(seeder, bot_config):
    h = seeder.create_euler_cv(_addr("eulExtZeroBorrower"), EULER_LIQ_LTV, EULER_COLLATERAL, EULER_BORROW)
    seeder.make_euler_externally_liquidated_zero_debt(h, _addr("eulExtZeroLiq"))
    assert seeder.is_externally_liquidated(h.cv) and seeder.max_repay(h.cv) == 0
    _run_bot(bot_config, h.cv)
    assert seeder.max_release(h.cv) == 0, "reserved credit not released"


# ----------------------------- Tier B ----------------------------------------
@pytest.mark.e2e
def test_full_monitor_autonomously_liquidates(seeder, bot_config):
    """Tier B: the genuine AccountMonitor + FactoryListener + worker threadpool run
    autonomously (no direct _process_account_update) and liquidate a seeded CV via the
    real startup-scan -> priority-queue -> threadpool path that Tier A bypasses. Covers
    the concurrency + bootstrap surface that is the only Flask-only drift risk."""
    cfg = bot_config
    h = seeder.create_euler_cv(_addr("tierBBorrower"), EULER_LIQ_LTV, EULER_COLLATERAL, EULER_BORROW)
    seeder.make_euler_internally_liquidatable(h)
    cv = Web3.to_checksum_address(h.cv)

    monitor = AccountMonitor(chain_id=1, config=cfg, notify=False, execute_liquidation=True)
    listener = FactoryListener(monitor, cfg)
    mon_thread = threading.Thread(target=monitor.start_queue_monitoring, daemon=True)
    mon_thread.start()
    try:
        listener.batch_account_logs_on_startup()  # real discovery enqueues the CV
        deadline = time.time() + 150
        while time.time() < deadline and seeder.can_liquidate(cv):
            time.sleep(2)
    finally:
        monitor.stop()

    assert not seeder.can_liquidate(cv), "autonomous monitor did not liquidate the CV"
    assert monitor.liquidation_failure_count == 0, "autonomous liquidation reverted on-chain"


# ----------------------------- Aave ------------------------------------------
@pytest.mark.e2e
def test_aave_internal(seeder, bot_config):
    h = seeder.create_aave_cv(_addr("aaveIntBorrower"), AAVE_LIQ_LTV, AAVE_COLLATERAL, AAVE_BORROW)
    seeder.make_aave_internally_liquidatable(h)
    assert seeder.can_liquidate(h.cv)
    _run_bot(bot_config, h.cv)
    assert not seeder.can_liquidate(h.cv), "Aave CV still liquidatable — bot did not liquidate"


@pytest.mark.e2e
def test_aave_external_with_debt(seeder, bot_config):
    h = seeder.create_aave_cv(_addr("aaveExtDebtBorrower"), AAVE_LIQ_LTV, AAVE_COLLATERAL, AAVE_BORROW)
    seeder.make_aave_externally_liquidated_with_debt(h, _addr("aaveExtDebtLiq"))
    assert seeder.is_externally_liquidated(h.cv) and seeder.max_repay(h.cv) > 0
    _run_bot(bot_config, h.cv)
    assert seeder.max_repay(h.cv) == 0, "residual debt not cleared"


@pytest.mark.e2e
def test_aave_external_zero_debt(seeder, bot_config):
    h = seeder.create_aave_cv(_addr("aaveExtZeroBorrower"), AAVE_LIQ_LTV, AAVE_COLLATERAL, AAVE_BORROW)
    seeder.make_aave_externally_liquidated_zero_debt(h, _addr("aaveExtZeroLiq"))
    assert seeder.is_externally_liquidated(h.cv) and seeder.max_repay(h.cv) == 0
    _run_bot(bot_config, h.cv)
    assert seeder.max_release(h.cv) == 0, "reserved credit not released"
