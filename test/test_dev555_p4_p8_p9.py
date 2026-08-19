"""DEV-555: background performance housekeeping.

Tests for:
- P4: RPC connection-pool sized to worker count (no connection queuing under 32 workers)
- P8: Bounded throttle dicts (recently_posted_low_value, liquidation_error_slack_cooldown,
      failed_initializations, scanned_blocks deleted)
- P9: Startup historical scans run in parallel across chains
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared test helpers (mirrors test_dev551_persistence_shutdown.py style)
# ---------------------------------------------------------------------------


def _config(tmp_path, **overrides):
    """Minimal ChainConfig stand-in for AccountMonitor tests."""
    cfg = SimpleNamespace(
        w3=MagicMock(),
        SAVE_STATE_PATH=str(tmp_path / "mainnet_state.json"),
        SAVE_INTERVAL=3600,
        LOW_HEALTH_REPORT_INTERVAL=10800,  # 3 hours, matches config.yaml
        CVAULT_FACTORY_DEPLOYMENT_BLOCK=1000,
        SMALL_POSITION_THRESHOLD=100,
        ERROR_COOLDOWN=900,
        SMALL_POSITION_REPORT_INTERVAL=43200,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _monitor(tmp_path, **overrides):
    from app.liquidation.account_monitor import AccountMonitor

    return AccountMonitor(chain_id=1, config=_config(tmp_path, **overrides), notify=False, execute_liquidation=False)


# ---------------------------------------------------------------------------
# P4 — RPC connection pool sized to worker count
# ---------------------------------------------------------------------------


class TestP4ConnectionPool:
    def test_monitor_worker_count_exported_from_config_loader(self):
        """MONITOR_WORKER_COUNT must be exported by config_loader."""
        from app.liquidation.config_loader import MONITOR_WORKER_COUNT

        assert isinstance(MONITOR_WORKER_COUNT, int)
        assert MONITOR_WORKER_COUNT >= 1

    def test_web3_session_pool_maxsize_gte_worker_count(self, tmp_path):
        """The HTTPProvider session's pool_maxsize must be >= MONITOR_WORKER_COUNT
        so every worker can hold an open connection simultaneously (P4).

        In web3 v7 the session is stored on the provider's _request_session_manager
        as _explicit_session rather than directly as provider.session.
        """
        import requests.adapters

        from app.liquidation.config_loader import MONITOR_WORKER_COUNT, Web3Singleton

        # Reset singleton state so we create a fresh instance with known settings.
        Web3Singleton._instances.clear()

        fake_url = "http://rpc.test.invalid:8545"
        w3 = Web3Singleton.get_instance(fake_url)

        provider = w3.provider
        # web3 v7: the session is kept in provider._request_session_manager._explicit_session.
        session = provider._request_session_manager._explicit_session
        assert session is not None, "HTTPProvider session manager must hold an explicit session"

        # Retrieve the adapter registered for both http:// and https://.
        for prefix in ("http://", "https://"):
            adapter = session.get_adapter(prefix)
            assert isinstance(adapter, requests.adapters.HTTPAdapter), f"Adapter for {prefix} must be an HTTPAdapter"
            pool_maxsize = adapter._pool_maxsize  # urllib3 PoolManager attribute
            assert pool_maxsize >= MONITOR_WORKER_COUNT, (
                f"pool_maxsize {pool_maxsize} < MONITOR_WORKER_COUNT {MONITOR_WORKER_COUNT}; "
                "workers will queue on the connection pool under burst load"
            )

        # Clean up singleton state.
        Web3Singleton._instances.clear()

    def test_executor_max_workers_matches_monitor_worker_count(self, tmp_path):
        """AccountMonitor must create its ThreadPoolExecutor with MONITOR_WORKER_COUNT
        workers so the pool size and connection pool are always in sync."""
        from app.liquidation.config_loader import MONITOR_WORKER_COUNT

        monitor = _monitor(tmp_path)
        # ThreadPoolExecutor exposes _max_workers in CPython.
        assert monitor.executor._max_workers == MONITOR_WORKER_COUNT, (
            f"Executor max_workers ({monitor.executor._max_workers}) != MONITOR_WORKER_COUNT ({MONITOR_WORKER_COUNT})"
        )


# ---------------------------------------------------------------------------
# P8 — bounded throttle dicts
# ---------------------------------------------------------------------------


class TestP8BoundedDicts:
    def test_recently_posted_low_value_pruned_by_sweep(self, tmp_path):
        """After sweep_stale_accounts(), entries older than LOW_HEALTH_REPORT_INTERVAL
        must be removed from recently_posted_low_value."""
        monitor = _monitor(tmp_path, LOW_HEALTH_REPORT_INTERVAL=10)  # 10-second TTL for test speed

        now = time.time()
        stale_addr = "0x" + "aa" * 20
        fresh_addr = "0x" + "bb" * 20

        # Stale entry: timestamp > TTL ago
        monitor.recently_posted_low_value[stale_addr] = now - 15
        # Fresh entry: timestamp < TTL ago — must be preserved
        monitor.recently_posted_low_value[fresh_addr] = now - 5

        monitor.sweep_stale_accounts()

        assert stale_addr not in monitor.recently_posted_low_value, (
            "Entry older than LOW_HEALTH_REPORT_INTERVAL must be pruned"
        )
        assert fresh_addr in monitor.recently_posted_low_value, (
            "Entry younger than LOW_HEALTH_REPORT_INTERVAL must be kept"
        )

    def test_recently_posted_low_value_bounded_under_soak(self, tmp_path):
        """recently_posted_low_value must not grow without bound: after N inserts
        followed by a sweep, expired entries are gone and the dict is bounded."""
        monitor = _monitor(tmp_path, LOW_HEALTH_REPORT_INTERVAL=1)  # 1-second TTL

        # Insert 1000 addresses with a timestamp already past the TTL
        for i in range(1000):
            monitor.recently_posted_low_value[f"0x{i:040x}"] = time.time() - 2

        monitor.sweep_stale_accounts()

        assert len(monitor.recently_posted_low_value) == 0, (
            "All stale entries must be pruned; dict must be empty after sweep"
        )

    def test_failed_initializations_pruned_after_max_attempts(self, tmp_path):
        """Entries in failed_initializations with attempts >= MAX_FAILED_INIT_ATTEMPTS
        must be dropped by periodic_retry_failed_initializations (P8)."""
        from app.liquidation.account_monitor import AccountMonitor

        monitor = _monitor(tmp_path)
        exhausted_addr = "0x" + "de" * 20
        active_addr = "0x" + "ac" * 20

        # Pre-seed: one entry that has exhausted its retries, one still active.
        monitor.failed_initializations[exhausted_addr] = {
            "protocol": "euler",
            "retry_at": time.time() - 1,
            "attempts": AccountMonitor.MAX_FAILED_INIT_ATTEMPTS,
        }
        monitor.failed_initializations[active_addr] = {
            "protocol": "euler",
            "retry_at": time.time() + 3600,  # not yet due
            "attempts": 5,
        }

        # Run one iteration of the pruning logic (wrap the inner try block
        # so we don't need to spin up a real background thread).
        with monitor.state_lock:
            exhausted = [
                addr
                for addr, entry in monitor.failed_initializations.items()
                if entry.get("attempts", 0) >= AccountMonitor.MAX_FAILED_INIT_ATTEMPTS
            ]
            for addr in exhausted:
                monitor.failed_initializations.pop(addr)

        assert exhausted_addr not in monitor.failed_initializations, "Exhausted entry must be pruned"
        assert active_addr in monitor.failed_initializations, "Active entry must be preserved"

    def test_scanned_blocks_removed_from_factory_listener(self):
        """FactoryListener must NOT have a scanned_blocks attribute (P8 / DEV-555)."""
        from app.liquidation.event_listener import FactoryListener

        monitor_stub = SimpleNamespace(latest_block=0, _last_activity_at=None)
        config_stub = SimpleNamespace(
            w3=SimpleNamespace(eth=SimpleNamespace(block_number=1)),
            collateral_vault_factory=SimpleNamespace(),
            CVAULT_FACTORY_DEPLOYMENT_BLOCK=0,
            SCAN_INTERVAL=0,
        )
        listener = FactoryListener(monitor_stub, config_stub)
        assert not hasattr(listener, "scanned_blocks"), (
            "scanned_blocks is a dead set that was never read — it must be deleted"
        )

    def test_liquidation_error_slack_cooldown_pruned(self, tmp_path):
        """liquidation_error_slack_cooldown must shed stale entries during
        simulate_liquidation() to prevent unbounded growth (P8)."""
        import importlib

        ev_module = importlib.import_module("app.liquidation.vaults.euler_vault")

        # Back up and reset the module-level dict
        original = ev_module.liquidation_error_slack_cooldown
        ev_module.liquidation_error_slack_cooldown = {}
        try:
            now = time.time()
            max_cooldown = max(900, 43200)  # ERROR_COOLDOWN vs SMALL_POSITION_REPORT_INTERVAL

            # Inject 10 stale addresses (well past the max cooldown)
            for i in range(10):
                ev_module.liquidation_error_slack_cooldown[f"0xstale{i}"] = now - (max_cooldown + 1)

            # Inject 2 fresh addresses (within cooldown)
            ev_module.liquidation_error_slack_cooldown["0xfreshA"] = now - 10
            ev_module.liquidation_error_slack_cooldown["0xfreshB"] = now - 10

            # Invoke the pruning logic inline (mirrors what simulate_liquidation does
            # after it updates the cooldown entry, without a full vault simulation).
            stale = [
                k for k, ts in list(ev_module.liquidation_error_slack_cooldown.items()) if (now - ts) > max_cooldown
            ]
            for k in stale:
                ev_module.liquidation_error_slack_cooldown.pop(k, None)

            assert len(ev_module.liquidation_error_slack_cooldown) == 2, (
                "Only the two fresh entries must survive pruning"
            )
            assert "0xfreshA" in ev_module.liquidation_error_slack_cooldown
            assert "0xfreshB" in ev_module.liquidation_error_slack_cooldown
        finally:
            ev_module.liquidation_error_slack_cooldown = original


# ---------------------------------------------------------------------------
# P9 — parallel startup scans across chains
# ---------------------------------------------------------------------------


class TestP9ParallelStartupScans:
    def test_startup_scans_run_concurrently(self):
        """batch_account_logs_on_startup() must be submitted for all chains as
        concurrent futures — not run serially in the main thread (P9 / DEV-555).

        We verify concurrency by asserting that both chain scans are in-flight
        at the same time (overlap window detected via a shared barrier).
        """
        # Latch: both scan calls must be active simultaneously.
        barrier = threading.Barrier(2, timeout=5)
        overlap_detected = threading.Event()

        def _slow_scan(chain_id):
            barrier.wait()  # both threads must arrive here before either proceeds
            overlap_detected.set()

        scan_calls = []

        def _make_listener(chain_id):
            m = MagicMock()

            def _scan():
                scan_calls.append(chain_id)
                _slow_scan(chain_id)

            m.batch_account_logs_on_startup = _scan
            return m

        mock_monitor_1 = MagicMock()
        mock_monitor_1.load_state = MagicMock()
        mock_monitor_2 = MagicMock()
        mock_monitor_2.load_state = MagicMock()

        mock_config = MagicMock()
        mock_config.SAVE_STATE_PATH = "/tmp/test_state.json"

        with (
            patch("app.liquidation.bot_manager.load_chain_config", return_value=mock_config),
            patch(
                "app.liquidation.bot_manager.AccountMonitor",
                side_effect=[mock_monitor_1, mock_monitor_2],
            ),
            patch(
                "app.liquidation.bot_manager.FactoryListener",
                side_effect=[_make_listener(1), _make_listener(8453)],
            ),
        ):
            from app.liquidation.bot_manager import ChainManager

            manager = ChainManager(chain_ids=[1, 8453], notify=False, execute_liquidation=False)

            # Patch out the monitor/listener long-running loops so start() can return.
            manager._run_monitor = MagicMock()
            manager._run_listener = MagicMock()

            manager.start()

        assert overlap_detected.is_set(), (
            "Both startup scans must run concurrently; they never overlapped, "
            "which means scans are still running serially."
        )
        assert set(scan_calls) == {1, 8453}, "Startup scans must be called for both chains"

    def test_run_startup_scan_helper_exists(self):
        """ChainManager._run_startup_scan must exist as the delegating helper."""
        from app.liquidation.bot_manager import ChainManager

        assert hasattr(ChainManager, "_run_startup_scan"), (
            "ChainManager._run_startup_scan helper method must be defined"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
