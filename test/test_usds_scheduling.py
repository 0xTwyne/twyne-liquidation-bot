"""DEV-553 / finding B11: USDS-debt positions on Base must skip *liquidation* but stay
on the normal monitoring cadence — the previous early-return orphaned them from the
priority queue until the hourly stale sweep."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.account_monitor import AccountMonitor

USDS = "0x820C137fa70C8691f0e44Dc420a5e53c168921Dc"
OTHER_ASSET = "0x0000000000000000000000000000000000001234"
EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"


def _monitor(chain_id: int) -> AccountMonitor:
    config = SimpleNamespace(w3=MagicMock(), USDS_ADDRESS=USDS, LIQUIDATOR_EOA=EOA)
    return AccountMonitor(chain_id=chain_id, config=config, notify=False, execute_liquidation=False)


def _account(target_asset: str, *, healthy: bool = True) -> MagicMock:
    acct = MagicMock()
    acct.target_asset = target_asset
    acct.time_of_next_update = 1000.0
    acct.internal_value_borrowed = 0
    acct.external_value_borrowed = 0
    hf = 2.0 if healthy else 0.5

    def _update_liquidity():
        acct.time_of_next_update = 2000.0  # the real code reschedules during update_liquidity
        # Mirror production: update_liquidity -> get_health_score sets both the float
        # HFs and the raw 1e18-scaled HFs the liquidation gate compares (DEV-554 Opt A).
        acct.internal_health_score = hf
        acct.external_health_score = hf
        acct.internal_health_score_raw = int(hf * 1e18)
        acct.external_health_score_raw = int(hf * 1e18)
        return [hf, hf, False]

    acct.update_liquidity.side_effect = _update_liquidity
    acct.check_liquidation.return_value = (False, False, 0, 0, 0)
    return acct


def test_usds_position_on_base_skips_liquidation_but_stays_scheduled(monkeypatch):
    monitor = _monitor(8453)
    addr = "0x" + "ab" * 20
    acct = _account(USDS, healthy=False)  # unhealthy: would attempt liquidation if not skipped
    monitor.accounts[addr] = acct
    handle = MagicMock()
    monkeypatch.setattr(monitor, "_handle_liquidation", handle)

    monitor.update_account_liquidity(addr)

    # Liquidation path is skipped for USDS debt on Base...
    acct.check_liquidation.assert_not_called()
    handle.assert_not_called()
    # ...but the account is re-queued on its normal cadence (NOT orphaned until the sweep).
    assert monitor.update_queue.get_nowait() == (2000.0, addr)
    assert monitor.update_queue.empty()


def test_usds_position_off_base_is_not_skipped(monkeypatch):
    monitor = _monitor(1)  # mainnet — the "on Base" skip must not apply
    addr = "0x" + "cd" * 20
    # Unhealthy (HF < 1) so the DEV-554 check-liquidation gate opens; the point of
    # this test is that the USDS skip is scoped to Base, NOT that healthy vaults run
    # check_liquidation (under DEV-554 they intentionally no longer do).
    acct = _account(USDS, healthy=False)
    monitor.accounts[addr] = acct
    monkeypatch.setattr(monitor, "_handle_liquidation", MagicMock())

    monitor.update_account_liquidity(addr)

    acct.check_liquidation.assert_called_once_with(EOA)
    assert monitor.update_queue.get_nowait() == (2000.0, addr)


def test_non_usds_position_on_base_takes_normal_path(monkeypatch):
    monitor = _monitor(8453)
    addr = "0x" + "ef" * 20
    # Unhealthy so the check-liquidation gate opens (see note above).
    acct = _account(OTHER_ASSET, healthy=False)
    monitor.accounts[addr] = acct
    monkeypatch.setattr(monitor, "_handle_liquidation", MagicMock())

    monitor.update_account_liquidity(addr)

    acct.check_liquidation.assert_called_once_with(EOA)
    assert monitor.update_queue.get_nowait() == (2000.0, addr)
