from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.account_monitor import AccountMonitor


def _monitor() -> AccountMonitor:
    return AccountMonitor(chain_id=1, config=SimpleNamespace(w3=MagicMock()), notify=False, execute_liquidation=False)


def test_listener_event_for_existing_account_enqueues_without_direct_processing(monkeypatch):
    monitor = _monitor()
    address = "0x" + "12" * 20
    monitor.accounts[address] = MagicMock()
    process_mock = MagicMock()
    monkeypatch.setattr(monitor, "update_account_liquidity", process_mock)

    monitor.update_account_on_status_check_event(address)

    assert process_mock.call_count == 0
    update_time, queued_address = monitor.update_queue.get_nowait()
    assert update_time > 0
    assert queued_address == address


def test_processing_guard_is_shared_across_entrypoints():
    monitor = _monitor()
    address = "0x" + "34" * 20

    assert monitor._claim_account_processing(address) is True
    assert monitor._claim_account_processing(address) is False

    monitor._release_account_processing(address)

    assert monitor._claim_account_processing(address) is True


def test_process_account_update_releases_inflight_mark(monkeypatch):
    monitor = _monitor()
    address = "0x" + "56" * 20
    process_mock = MagicMock()
    monkeypatch.setattr(monitor, "update_account_liquidity", process_mock)

    assert monitor._claim_account_processing(address) is True

    monitor._process_account_update(address)

    process_mock.assert_called_once_with(address)
    assert address not in monitor.processing_accounts


def test_update_racing_inflight_worker_is_reprocessed_after_release():
    """An update that arrives while the address is mid-processing must not be lost:
    it is re-enqueued exactly once when the in-flight worker releases."""
    monitor = _monitor()
    address = "0x" + "78" * 20

    # Worker A claims the address (in-flight).
    assert monitor._claim_account_processing(address) is True

    # A racing update for the same address fails to claim and is marked dirty.
    assert monitor._claim_account_processing(address) is False
    assert address in monitor.pending_reprocess
    assert monitor.update_queue.empty()  # not enqueued yet — worker still in flight

    # Worker A finishes: the dirty address is re-enqueued for one more pass.
    monitor._release_account_processing(address)
    assert address not in monitor.pending_reprocess
    _update_time, queued_address = monitor.update_queue.get_nowait()
    assert queued_address == address
    assert monitor.update_queue.empty()  # exactly one re-enqueue, not one per racing update


def test_no_reprocess_when_no_racing_update():
    """A clean claim/release cycle (no racing update) does not re-enqueue."""
    monitor = _monitor()
    address = "0x" + "9a" * 20

    assert monitor._claim_account_processing(address) is True
    monitor._release_account_processing(address)

    assert address not in monitor.pending_reprocess
    assert monitor.update_queue.empty()


# ---------------------------------------------------------------------------
# _handle_liquidation guard: None liquidation_data must not inflate failure count
# ---------------------------------------------------------------------------


def _make_account(simulate_return):
    """Build a minimal mock vault account with simulate_liquidation returning simulate_return."""
    account = MagicMock()
    account.simulate_liquidation.return_value = simulate_return
    account.get_position_stats.return_value = None
    return account


def test_handle_liquidation_skips_when_unprofitable_and_externally_liquidated():
    """externally_liquidated=True + simulate_liquidation returning (False, None, None)
    must not dereference liquidation_data and must not increment liquidation_failure_count."""
    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "ab" * 20

    account = _make_account((False, None, None))

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=False, externally_liquidated=True)

    assert monitor.liquidation_failure_count == initial_failures, (
        "liquidation_failure_count must not be incremented when liquidation_data is None"
    )
    account.simulate_liquidation.assert_called_once()


def test_handle_liquidation_skips_when_unprofitable_and_not_liquidatable():
    """can_liquidate=False, externally_liquidated=False, result=False → clean skip, no failure count."""
    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "cd" * 20

    account = _make_account((False, None, None))

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=False, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures


# ---------------------------------------------------------------------------
# _handle_liquidation execution branches: failure-count accounting per outcome
# ---------------------------------------------------------------------------


def _execution_account():
    """Profitable account whose simulate_liquidation yields liquidation_data with tx + collateral_address."""
    liquidation_data = {"tx": {"to": "0x" + "ee" * 20, "data": "0x"}, "collateral_address": "0x" + "ff" * 20}
    return _make_account((True, liquidation_data, None))


def test_handle_liquidation_success_keeps_failure_count(monkeypatch):
    """A mined+successful tx (status==1) must NOT increment liquidation_failure_count
    and must refresh liquidity via account.update_liquidity()."""
    from app.liquidation import account_monitor

    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "01" * 20
    account = _execution_account()

    monkeypatch.setattr(
        account_monitor.BaseLiquidator,
        "execute_liquidation",
        MagicMock(return_value=("0xhash", {"status": 1})),
    )

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=True, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures
    account.update_liquidity.assert_called_once()


def test_handle_liquidation_reverted_increments_failure_count(monkeypatch):
    """A mined-but-reverted tx (status==0) increments liquidation_failure_count by exactly 1."""
    from app.liquidation import account_monitor

    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "02" * 20
    account = _execution_account()

    monkeypatch.setattr(
        account_monitor.BaseLiquidator,
        "execute_liquidation",
        MagicMock(return_value=("0xhash", {"status": 0})),
    )

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=True, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures + 1
    account.update_liquidity.assert_called_once()


def test_handle_liquidation_no_receipt_increments_failure_count(monkeypatch):
    """A returned tx hash with a None receipt increments liquidation_failure_count by exactly 1."""
    from app.liquidation import account_monitor

    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "03" * 20
    account = _execution_account()

    monkeypatch.setattr(
        account_monitor.BaseLiquidator,
        "execute_liquidation",
        MagicMock(return_value=("0xhash", None)),
    )

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=True, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures + 1
    account.update_liquidity.assert_called_once()


def test_handle_liquidation_no_tx_increments_failure_count(monkeypatch):
    """Execution returning (None, None) — no confirmed tx — increments liquidation_failure_count by 1."""
    from app.liquidation import account_monitor

    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "04" * 20
    account = _execution_account()

    monkeypatch.setattr(
        account_monitor.BaseLiquidator,
        "execute_liquidation",
        MagicMock(return_value=(None, None)),
    )

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=True, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures + 1
    account.update_liquidity.assert_called_once()


def test_handle_liquidation_execution_raises_increments_failure_count(monkeypatch):
    """An exception raised inside execute_liquidation is caught by the outer try/except
    and increments liquidation_failure_count by exactly 1."""
    from app.liquidation import account_monitor

    monitor = _monitor()
    monitor.execute_liquidation = True
    address = "0x" + "05" * 20
    account = _execution_account()

    def _raise(*_args, **_kwargs):
        raise Exception("boom")

    monkeypatch.setattr(account_monitor.BaseLiquidator, "execute_liquidation", _raise)

    initial_failures = monitor.liquidation_failure_count
    monitor._handle_liquidation(account, address, can_liquidate=True, externally_liquidated=False)

    assert monitor.liquidation_failure_count == initial_failures + 1
