import importlib
from types import SimpleNamespace

import pytest

from app.liquidation.event_listener import FactoryListener

event_listener = importlib.import_module("app.liquidation.event_listener")


class _FakeCreatedEvent:
    def __init__(self, calls, failures_by_range=None):
        self.calls = calls
        self.failures_by_range = failures_by_range or {}

    def get_logs(self, *, from_block, to_block):
        self.calls.append((from_block, to_block))
        if self.failures_by_range.get((from_block, to_block), 0) > 0:
            self.failures_by_range[(from_block, to_block)] -= 1
            raise RuntimeError("rpc range failed")
        return []


class _FakeFactoryEvents:
    def __init__(self, created_event):
        self.created_event = created_event

    def T_CollateralVaultCreated(self):
        return self.created_event


class _FakeFactory:
    def __init__(self, created_event):
        self.events = _FakeFactoryEvents(created_event)


class _FakeMonitor:
    def __init__(self, latest_block=0, last_saved_block=0):
        self.latest_block = latest_block
        self.last_saved_block = last_saved_block
        self.saved_blocks = []
        self._last_activity_at = None

    def save_state(self):
        self.saved_blocks.append(self.latest_block)
        self.last_saved_block = self.latest_block

    def update_account_on_status_check_event(self, address, protocol):
        raise AssertionError(f"unexpected event update for {address=} {protocol=}")


def _listener(*, block_number, latest_block=0, last_saved_block=0, deployment_block=0, batch_size=10, failures=None):
    calls = []
    created_event = _FakeCreatedEvent(calls, failures_by_range=failures)
    monitor = _FakeMonitor(latest_block=latest_block, last_saved_block=last_saved_block)
    config = SimpleNamespace(
        w3=SimpleNamespace(eth=SimpleNamespace(block_number=block_number)),
        collateral_vault_factory=_FakeFactory(created_event),
        CVAULT_FACTORY_DEPLOYMENT_BLOCK=deployment_block,
        BATCH_SIZE=batch_size,
        BATCH_INTERVAL=0,
        RETRY_DELAY=0,
        SCAN_INTERVAL=0,
    )

    return FactoryListener(monitor, config), monitor, calls


def test_startup_scan_does_not_save_or_advance_failed_range():
    listener, monitor, calls = _listener(
        block_number=125,
        last_saved_block=100,
        deployment_block=1,
        batch_size=10,
        failures={(111, 121): 3},
    )

    listener.batch_account_logs_on_startup()

    assert calls == [(100, 110), (111, 121), (111, 121), (111, 121)]
    assert monitor.latest_block == 110
    assert monitor.saved_blocks == [110]
    assert monitor.last_saved_block == 110


def test_scan_block_range_raises_without_advancing_cursor_on_terminal_failure():
    listener, monitor, calls = _listener(
        block_number=125,
        latest_block=100,
        failures={(101, 110): 3},
    )

    with pytest.raises(RuntimeError, match="rpc range failed"):
        listener.scan_block_range(101, 110)

    assert calls == [(101, 110), (101, 110), (101, 110)]
    assert monitor.latest_block == 100


def test_live_monitoring_starts_after_latest_block_and_chunks_large_catchup(monkeypatch):
    listener, monitor, calls = _listener(
        block_number=126,
        latest_block=100,
        batch_size=10,
    )

    def stop_after_first_loop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(event_listener.time, "sleep", stop_after_first_loop)

    with pytest.raises(KeyboardInterrupt):
        listener.start_event_monitoring()

    assert calls == [(101, 111), (112, 122), (123, 125)]
    assert monitor.latest_block == 125


def test_live_monitoring_refreshes_liveness_on_successful_scan(monkeypatch):
    listener, monitor, calls = _listener(block_number=126, latest_block=100, batch_size=10)

    alerts = []
    monkeypatch.setattr(event_listener, "post_error_notification", lambda msg, cfg=None: alerts.append(msg))
    monkeypatch.setattr(event_listener.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        listener.start_event_monitoring()

    # A healthy scan refreshes liveness and never alerts.
    assert monitor._last_activity_at is not None
    assert alerts == []


def test_live_monitoring_skips_liveness_and_alerts_on_persistent_failure(monkeypatch):
    listener, monitor, _calls = _listener(block_number=126, latest_block=100, batch_size=10)

    # Force the scan to fail (bypasses the inner retry/sleep machinery).
    monkeypatch.setattr(listener, "scan_block_ranges", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rpc down")))
    alerts = []
    monkeypatch.setattr(event_listener, "post_error_notification", lambda msg, cfg=None: alerts.append(msg))
    monkeypatch.setattr(event_listener.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        listener.start_event_monitoring()

    # A failing scan must NOT refresh liveness (so /health goes stale), must alert,
    # and must not advance the cursor past the failed range.
    assert monitor._last_activity_at is None
    assert len(alerts) == 1
    assert monitor.latest_block == 100


def test_startup_scan_alerts_on_terminal_failure_without_advancing(monkeypatch):
    listener, monitor, calls = _listener(
        block_number=125,
        last_saved_block=100,
        deployment_block=1,
        batch_size=10,
        failures={(111, 121): 3},
    )
    alerts = []
    monkeypatch.setattr(event_listener, "post_error_notification", lambda msg, cfg=None: alerts.append(msg))

    # Non-fatal: startup logs + alerts but does not raise; cursor held at the last good chunk.
    listener.batch_account_logs_on_startup()

    assert calls == [(100, 110), (111, 121), (111, 121), (111, 121)]
    assert monitor.latest_block == 110
    assert len(alerts) == 1
