"""DEV-546: Fatal chain-monitor crash alerts + /health stale-threshold tests.

O1 — A dead monitor/listener future must trigger BOTH a Slack notification
     (post_error_notification) AND a Sentry event (sentry_sdk.capture_exception)
     before the process exits (os._exit(1)).

B21 — HEALTH_STALE_THRESHOLD must be large enough that /health stays 200
      across an entire idle SCAN_INTERVAL (300 s) cycle with margin to spare.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import HEALTH_STALE_THRESHOLD
from app.liquidation import bot_manager as bm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_listener():
    """Minimal FactoryListener stand-in: batch scan is a no-op."""
    m = MagicMock()
    m.batch_account_logs_on_startup.return_value = None
    return m


def _make_chain_manager(monkeypatch):
    """Build a ChainManager without touching any real config or RPC."""
    # Bypass __init__ (which calls _initialize_chains → load_chain_config → RPC).
    monkeypatch.setattr(bm.ChainManager, "_initialize_chains", lambda self: None)

    cm = bm.ChainManager.__new__(bm.ChainManager)
    cm.chain_ids = [1]
    cm.notify = False
    cm.execute_liquidation = False
    # Minimal config object — only CHAIN_NAME and NOTIFICATION_URL are referenced
    # by post_error_notification (after our monkeypatch it's irrelevant, but keep
    # it realistic so the test doesn't break on a later refactor).
    cm.configs = {1: SimpleNamespace(CHAIN_NAME="mainnet-test", NOTIFICATION_URL=None)}
    cm.monitors = {}
    cm.web3s = {}
    cm.listeners = {1: _fake_listener()}
    return cm


# ---------------------------------------------------------------------------
# O1: dead monitor/listener triggers Slack + Sentry + os._exit(1)
# ---------------------------------------------------------------------------


def test_dead_monitor_fires_slack_sentry_and_exits(monkeypatch):
    """When the monitor future dies, post_error_notification AND
    sentry_sdk.capture_exception MUST both be called before os._exit(1)."""
    cm = _make_chain_manager(monkeypatch)

    slack_calls = []
    sentry_calls = []
    exit_codes = []

    monkeypatch.setattr(bm, "post_error_notification", lambda msg, cfg=None: slack_calls.append(msg) or True)
    monkeypatch.setattr(bm.sentry_sdk, "capture_exception", lambda exc: sentry_calls.append(exc))
    monkeypatch.setattr(bm.os, "_exit", lambda code: exit_codes.append(code))

    boom = RuntimeError("monitor thread died unexpectedly")

    def _run_monitor_raise(chain_id):
        raise boom

    def _run_listener_noop(chain_id):
        pass  # listener completes fine

    cm._run_monitor = _run_monitor_raise
    cm._run_listener = _run_listener_noop

    cm.start()

    # Both alert channels must have fired.
    assert len(slack_calls) >= 1, "Slack notification not sent after monitor death"
    assert len(sentry_calls) >= 1, "Sentry event not captured after monitor death"

    # The captured exception must be the original one (not a wrapper).
    assert sentry_calls[0] is boom, f"Wrong exception captured: {sentry_calls[0]!r}"

    # Process must have been told to exit with code 1.
    assert exit_codes, "os._exit was not called"
    assert exit_codes[0] == 1, f"Expected exit code 1, got {exit_codes[0]}"


def test_dead_listener_fires_slack_sentry_and_exits(monkeypatch):
    """Same guarantees when the *listener* future dies (not the monitor)."""
    cm = _make_chain_manager(monkeypatch)

    slack_calls = []
    sentry_calls = []
    exit_codes = []

    monkeypatch.setattr(bm, "post_error_notification", lambda msg, cfg=None: slack_calls.append(msg) or True)
    monkeypatch.setattr(bm.sentry_sdk, "capture_exception", lambda exc: sentry_calls.append(exc))
    monkeypatch.setattr(bm.os, "_exit", lambda code: exit_codes.append(code))

    kaboom = ConnectionError("listener RPC connection lost")

    def _run_monitor_noop(chain_id):
        pass

    def _run_listener_raise(chain_id):
        raise kaboom

    cm._run_monitor = _run_monitor_noop
    cm._run_listener = _run_listener_raise

    cm.start()

    assert len(slack_calls) >= 1, "Slack notification not sent after listener death"
    assert len(sentry_calls) >= 1, "Sentry event not captured after listener death"
    assert sentry_calls[0] is kaboom
    assert exit_codes and exit_codes[0] == 1


def test_healthy_run_does_not_call_os_exit(monkeypatch):
    """When both futures complete normally no alert or exit must occur."""
    cm = _make_chain_manager(monkeypatch)

    exit_codes = []
    monkeypatch.setattr(bm.os, "_exit", lambda code: exit_codes.append(code))
    # post_error_notification / sentry are not patched — a call would raise,
    # which would itself be surfaced as a test failure.

    cm._run_monitor = lambda chain_id: None
    cm._run_listener = lambda chain_id: None

    cm.start()

    assert exit_codes == [], "os._exit called on a healthy run"


# ---------------------------------------------------------------------------
# B21: /health stale threshold must survive one full SCAN_INTERVAL idle cycle
# ---------------------------------------------------------------------------

# SCAN_INTERVAL from config.yaml global section (seconds).
_SCAN_INTERVAL = 300


def test_health_stale_threshold_exceeds_two_scan_intervals():
    """HEALTH_STALE_THRESHOLD default must be > 2 × SCAN_INTERVAL so that
    /health never flips to 503 just because the scanner had one quiet cycle."""
    assert HEALTH_STALE_THRESHOLD > timedelta(seconds=2 * _SCAN_INTERVAL), (
        f"HEALTH_STALE_THRESHOLD ({HEALTH_STALE_THRESHOLD}) must exceed 2 × SCAN_INTERVAL ({2 * _SCAN_INTERVAL} s)"
    )


def test_health_200_when_last_activity_one_scan_interval_ago():
    """A monitor whose _last_activity_at is exactly SCAN_INTERVAL seconds old
    must still pass the staleness check (not yet stale)."""
    now = datetime.now(timezone.utc)
    last_activity = now - timedelta(seconds=_SCAN_INTERVAL)
    assert (now - last_activity) <= HEALTH_STALE_THRESHOLD, (
        "/health would incorrectly report stale after just one SCAN_INTERVAL; "
        f"HEALTH_STALE_THRESHOLD={HEALTH_STALE_THRESHOLD}, elapsed={now - last_activity}"
    )


def test_health_endpoint_200_after_one_scan_interval(monkeypatch):
    """Integration: Flask /health returns 200 when the monitor last updated
    exactly SCAN_INTERVAL seconds ago (well within the stale window)."""
    from app import create_app
    from app.liquidation import routes as routes_module

    # Build a minimal ChainManager-like object with one monitor whose
    # _last_activity_at is SCAN_INTERVAL seconds in the past.
    last_activity = datetime.now(timezone.utc) - timedelta(seconds=_SCAN_INTERVAL)
    mock_monitor = SimpleNamespace(_last_activity_at=last_activity)
    mock_chain_manager = SimpleNamespace(monitors={1: mock_monitor})

    # Inject it before create_app so the /health handler sees it immediately.
    monkeypatch.setattr(routes_module.start_monitor, "_chain_manager", mock_chain_manager, raising=False)

    # Prevent the background monitor thread from starting.
    monkeypatch.setattr("app.threading.Thread", lambda target, args=(), **kw: MagicMock())

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/health")
    assert resp.status_code == 200, (
        f"/health returned {resp.status_code} when last activity was {_SCAN_INTERVAL}s ago; "
        f"body: {resp.get_data(as_text=True)}"
    )


def test_health_endpoint_503_when_genuinely_stale(monkeypatch):
    """Integration: Flask /health returns 503 when _last_activity_at is older
    than HEALTH_STALE_THRESHOLD (confirms the guard still fires eventually)."""
    from app import create_app
    from app.liquidation import routes as routes_module

    # Last activity far in the past — definitely stale.
    last_activity = datetime.now(timezone.utc) - HEALTH_STALE_THRESHOLD - timedelta(seconds=60)
    mock_monitor = SimpleNamespace(_last_activity_at=last_activity)
    mock_chain_manager = SimpleNamespace(monitors={1: mock_monitor})

    monkeypatch.setattr(routes_module.start_monitor, "_chain_manager", mock_chain_manager, raising=False)
    monkeypatch.setattr("app.threading.Thread", lambda target, args=(), **kw: MagicMock())

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/health")
    assert resp.status_code == 503, (
        f"/health returned {resp.status_code} when activity was older than threshold; "
        f"body: {resp.get_data(as_text=True)}"
    )
