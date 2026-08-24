"""DEV-551: state persistence, thread-safety, and graceful shutdown.

These tests exercise AccountMonitor without any RPC / on-chain dependency:
- atomic + crash-safe state writes (B7)
- concurrent mutation of shared state under state_lock (B8/B14)
- per-account load resilience + cursor preservation (B12)
- prompt shutdown of the periodic background threads + final save ordering (B10)
- dirty-flag save cadence + dropped legacy ``queue`` field (P7)
"""

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.liquidation import account_monitor as am_module
from app.liquidation.account_monitor import AccountMonitor


def _config(tmp_path, **overrides):
    """Minimal ChainConfig stand-in covering only what these tests touch."""
    cfg = SimpleNamespace(
        w3=MagicMock(),
        SAVE_STATE_PATH=str(tmp_path / "mainnet_state.json"),
        SAVE_INTERVAL=0.01,
        LOW_HEALTH_REPORT_INTERVAL=0.01,
        CVAULT_FACTORY_DEPLOYMENT_BLOCK=1000,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _monitor(tmp_path, **overrides):
    return AccountMonitor(chain_id=1, config=_config(tmp_path, **overrides), notify=False, execute_liquidation=False)


class _FakeVault:
    """A serialisable vault stub. to_dict round-trips through from_dict."""

    protocol = "euler"

    def __init__(self, address, time_of_next_update=0.0, inHF=1.5, exHF=1.5):
        self.address = address
        self.time_of_next_update = time_of_next_update
        self.internal_health_score = inHF
        self.external_health_score = exHF
        # Fields read by get_accounts_by_health_score (routes-style read).
        self.balanceOf = 0
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0
        self.underlying_asset_symbol = "TEST"

    def to_dict(self):
        return {
            "address": self.address,
            "protocol": self.protocol,
            "time_of_next_update": self.time_of_next_update,
            "internal_health_score": self.internal_health_score,
            "external_health_score": self.external_health_score,
        }

    def update_liquidity(self):
        return [self.internal_health_score, self.external_health_score, False]


def _install_fake_vault_class(monkeypatch, fail_addresses=None):
    """Patch get_vault_class_for_protocol so from_dict reconstructs _FakeVault,
    raising for any address in fail_addresses to simulate a transient RPC failure."""
    fail_addresses = fail_addresses or set()

    class _FakeVaultClass:
        @classmethod
        def from_dict(cls, data, config):
            if data["address"] in fail_addresses:
                raise RuntimeError("transient RPC failure reconstructing vault")
            v = _FakeVault(data["address"], data["time_of_next_update"])
            v.internal_health_score = data["internal_health_score"]
            v.external_health_score = data["external_health_score"]
            return v

    monkeypatch.setattr(am_module, "get_vault_class_for_protocol", lambda _p: _FakeVaultClass)


# --------------------------------------------------------------------------- #
# Item 1 / B7 — atomic, crash-safe state writes
# --------------------------------------------------------------------------- #


def test_save_state_writes_no_dead_queue_field(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.accounts["0x" + "11" * 20] = _FakeVault("0x" + "11" * 20)
    monitor.latest_block = 1234

    monitor.save_state()

    with open(monitor.config.SAVE_STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)

    assert "queue" not in state, "the dead 'queue' field must be dropped from the persisted format"
    assert state["last_saved_block"] == 1234
    assert "0x" + "11" * 20 in state["accounts"]


def test_crash_between_tmp_and_rename_leaves_old_state_intact(tmp_path, monkeypatch):
    """Simulate a crash AFTER the tmp file is written but BEFORE os.replace swaps it
    into place: the primary file must remain the complete OLD state (never truncated),
    and recovery via the backup must yield valid state."""
    monitor = _monitor(tmp_path)
    path = monitor.config.SAVE_STATE_PATH

    # First, a complete successful save establishes the "old" state.
    monitor.accounts["0x" + "aa" * 20] = _FakeVault("0x" + "aa" * 20)
    monitor.latest_block = 100
    monitor.save_state()
    old_on_disk = json.loads(open(path, encoding="utf-8").read())
    assert old_on_disk["last_saved_block"] == 100

    # Now mutate state and attempt a save, but crash inside os.replace such that the
    # FINAL replace (tmp -> primary) raises. The primary must still be the old state.
    monitor.accounts["0x" + "bb" * 20] = _FakeVault("0x" + "bb" * 20)
    monitor.latest_block = 200

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        # The first replace rotates primary -> .bak (allow it); the second replace
        # (tmp -> primary) is the crash point.
        if src.endswith(".tmp"):
            raise OSError("simulated crash during final rename")
        return real_replace(src, dst)

    monkeypatch.setattr(am_module.os, "replace", _flaky_replace)
    monitor.save_state()  # must swallow the error, not raise
    monkeypatch.undo()

    # Primary file must be complete + parseable, holding the OLD state (the atomic
    # final replace never happened, so it was never truncated to the new state).
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk["last_saved_block"] == 100, "primary must still hold the complete old state"
    # The tmp file must be fully written (complete JSON), proving fsync-then-replace.
    tmp_on_disk = json.loads(open(path + ".tmp", encoding="utf-8").read())
    assert tmp_on_disk["last_saved_block"] == 200
    # A complete backup of the previous good state survives for recovery.
    bak_on_disk = json.loads(open(path + ".bak", encoding="utf-8").read())
    assert bak_on_disk["last_saved_block"] == 100

    # A failed write must re-arm the dirty flag so the next save retries.
    assert monitor._state_dirty is True

    # Recovery: a fresh monitor loads the complete (old) primary state.
    _install_fake_vault_class(monkeypatch)
    fresh = _monitor(tmp_path)
    fresh.load_state(path)
    assert fresh.last_saved_block == 100


def test_load_state_falls_back_to_backup_when_primary_corrupt(tmp_path, monkeypatch):
    _install_fake_vault_class(monkeypatch)
    monitor = _monitor(tmp_path)
    path = monitor.config.SAVE_STATE_PATH

    # Two successful saves: the second rotates the first into .bak.
    monitor.accounts["0x" + "cc" * 20] = _FakeVault("0x" + "cc" * 20)
    monitor.latest_block = 300
    monitor.save_state()
    monitor.latest_block = 400
    monitor._state_dirty = True
    monitor.save_state()
    assert os.path.exists(path + ".bak")

    # Corrupt the primary (truncated half-write).
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "accou')

    fresh = _monitor(tmp_path)
    fresh.load_state(path)

    # Recovered from .bak (block 300, the previous good copy).
    assert fresh.last_saved_block == 300
    assert "0x" + "cc" * 20 in fresh.accounts


# --------------------------------------------------------------------------- #
# Item 2 / B8 / B14 — concurrency: no RuntimeError, no skipped save
# --------------------------------------------------------------------------- #


def test_concurrent_insert_during_iteration_does_not_raise(tmp_path):
    """A thread inserting into accounts while save_state + sweep + the routes-style
    read iterate must not raise 'dict changed size during iteration', and the save
    must still complete."""
    monitor = _monitor(tmp_path)

    # Pre-seed accounts to make iteration windows non-trivial.
    for i in range(200):
        addr = f"0x{i:040x}"
        monitor.accounts[addr] = _FakeVault(addr, time_of_next_update=time.time() + 10)

    errors = []
    stop = threading.Event()

    def _inserter():
        # Insert under state_lock, exactly as the production listener thread does via
        # update_account_on_status_check_event. The contract under test: when every
        # mutator AND every iterator takes state_lock and iterates over snapshots,
        # concurrent inserts never raise "dict changed size during iteration". The
        # account count is bounded so the readers' serialisation work stays cheap.
        i = 1000
        while not stop.is_set() and i < 5000:
            addr = f"0x{i:040x}"
            with monitor.state_lock:
                monitor.accounts[addr] = _FakeVault(addr, time_of_next_update=time.time() + 10)
            i += 1

    def _iterators():
        try:
            for _ in range(200):
                # local_save=False exercises the under-lock accounts snapshot path
                # without the disk-write cost; the snapshot is where the unguarded
                # "dict changed size during iteration" would surface.
                monitor.save_state(force=True, local_save=False)
                monitor.sweep_stale_accounts()
                monitor.get_accounts_by_health_score()
        except Exception as ex:  # noqa: BLE001 - capture any RuntimeError
            errors.append(ex)
        finally:
            stop.set()

    t_ins = threading.Thread(target=_inserter)
    t_ins.start()
    _iterators()
    stop.set()
    t_ins.join(timeout=5)

    assert not errors, f"concurrent iteration raised: {errors}"

    # And a real on-disk save still produces a complete, parseable file.
    monitor.save_state(force=True)
    with open(monitor.config.SAVE_STATE_PATH, encoding="utf-8") as f:
        json.load(f)


def test_latest_block_captured_before_accounts_snapshot(tmp_path):
    """The persisted cursor must never be ahead of the persisted account set."""
    monitor = _monitor(tmp_path)
    monitor.latest_block = 555
    monitor.accounts["0x" + "dd" * 20] = _FakeVault("0x" + "dd" * 20)
    monitor.save_state()

    with open(monitor.config.SAVE_STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    assert state["last_saved_block"] == 555


# --------------------------------------------------------------------------- #
# Item 3 / B12 — per-account load resilience + cursor preservation
# --------------------------------------------------------------------------- #


def test_one_failing_vault_does_not_abort_load_or_reset_cursor(tmp_path, monkeypatch):
    bad = "0x" + "ba" * 20
    good1 = "0x" + "01" * 20
    good2 = "0x" + "02" * 20

    state = {
        "version": 1,
        "last_saved_block": 4242,
        "accounts": {
            good1: _FakeVault(good1, 1.0).to_dict(),
            bad: _FakeVault(bad, 2.0).to_dict(),
            good2: _FakeVault(good2, 3.0).to_dict(),
        },
        "failed_initializations": {},
    }
    path = str(tmp_path / "mainnet_state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    _install_fake_vault_class(monkeypatch, fail_addresses={bad})

    monitor = _monitor(tmp_path)
    monitor.load_state(path)

    # The two good vaults loaded; the bad one routed to failed_initializations.
    assert good1 in monitor.accounts
    assert good2 in monitor.accounts
    assert bad not in monitor.accounts
    assert bad in monitor.failed_initializations
    # Cursor preserved — NOT reset to 0.
    assert monitor.last_saved_block == 4242
    assert monitor.latest_block == 4242
    # rebuild_queue still ran (queue populated for the two good accounts).
    assert monitor.update_queue.qsize() == 2


def test_cursor_set_before_reconstruction(tmp_path, monkeypatch):
    """Even if EVERY vault fails to reconstruct, the cursor is preserved (set first)."""
    a1 = "0x" + "01" * 20
    state = {
        "version": 1,
        "last_saved_block": 9999,
        "accounts": {a1: _FakeVault(a1).to_dict()},
        "failed_initializations": {},
    }
    path = str(tmp_path / "mainnet_state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    _install_fake_vault_class(monkeypatch, fail_addresses={a1})

    monitor = _monitor(tmp_path)
    monitor.load_state(path)

    assert monitor.last_saved_block == 9999
    assert a1 in monitor.failed_initializations


# --------------------------------------------------------------------------- #
# Item 4 / B10 — prompt shutdown + final save ordering
# --------------------------------------------------------------------------- #


def test_periodic_save_thread_exits_promptly_on_stop(tmp_path):
    """periodic_save waits on _stop_event, so stop() wakes it well within the
    (otherwise long) interval rather than sleeping it out."""
    monitor = _monitor(tmp_path, SAVE_INTERVAL=3600)  # 1 hour — would hang if using time.sleep
    monitor.accounts["0x" + "ee" * 20] = _FakeVault("0x" + "ee" * 20)
    monitor.latest_block = 10

    t = threading.Thread(target=monitor.periodic_save, daemon=True)
    t.start()
    time.sleep(0.05)  # let it enter the wait

    monitor.running = False
    monitor._stop_event.set()

    t.join(timeout=2)
    assert not t.is_alive(), "periodic_save did not exit promptly after stop()"


def test_sweep_and_retry_threads_exit_promptly_on_stop(tmp_path):
    monitor = _monitor(tmp_path)
    threads = [
        threading.Thread(target=monitor.periodic_sweep_stale_accounts, daemon=True),
        threading.Thread(target=monitor.periodic_retry_failed_initializations, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)

    monitor.running = False
    monitor._stop_event.set()

    for t in threads:
        t.join(timeout=2)
        assert not t.is_alive()


def test_stop_runs_final_save_after_workers_drained(tmp_path):
    """stop() must drain the executor BEFORE the final save, and the final save
    must run even when the dirty flag is clear (force=True)."""
    monitor = _monitor(tmp_path)
    monitor.accounts["0x" + "ff" * 20] = _FakeVault("0x" + "ff" * 20)
    monitor.latest_block = 77

    order = []
    real_shutdown = monitor.executor.shutdown

    def _tracked_shutdown(*a, **k):
        order.append("shutdown")
        return real_shutdown(*a, **k)

    monitor.executor.shutdown = _tracked_shutdown

    real_save = monitor.save_state

    def _tracked_save(*a, **k):
        order.append(("save", k.get("force")))
        return real_save(*a, **k)

    monitor.save_state = _tracked_save

    monitor._state_dirty = False  # would skip a non-forced save
    monitor.stop()

    assert order[0] == "shutdown", "workers must be drained before the final save"
    assert ("save", True) in order, "final save must be forced so it always flushes on shutdown"
    # The forced save actually wrote the file.
    with open(monitor.config.SAVE_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f)["last_saved_block"] == 77


def test_factory_listener_stop_flag_exits_loop():
    from app.liquidation.event_listener import FactoryListener

    monitor = SimpleNamespace(latest_block=0, _last_activity_at=None)
    config = SimpleNamespace(
        w3=SimpleNamespace(eth=SimpleNamespace(block_number=1)),
        collateral_vault_factory=SimpleNamespace(),
        CVAULT_FACTORY_DEPLOYMENT_BLOCK=0,
        SCAN_INTERVAL=0,
    )
    listener = FactoryListener(monitor, config)
    assert listener.running is True
    listener.stop()
    assert listener.running is False
    # With running already False, the loop body must not execute at all.
    listener.start_event_monitoring()  # returns immediately


# --------------------------------------------------------------------------- #
# Item 5 / P7 — dirty-flag save cadence
# --------------------------------------------------------------------------- #


def test_save_skipped_when_state_unchanged(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.accounts["0x" + "12" * 20] = _FakeVault("0x" + "12" * 20)
    monitor._state_dirty = True
    monitor.latest_block = 5
    monitor.save_state()

    mtime1 = os.path.getmtime(monitor.config.SAVE_STATE_PATH)
    time.sleep(0.02)

    # No state change since last save: a non-forced save must NOT rewrite the file.
    monitor.save_state()
    mtime2 = os.path.getmtime(monitor.config.SAVE_STATE_PATH)
    assert mtime1 == mtime2, "unchanged state must not be re-written"


def test_save_runs_when_cursor_advances(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.accounts["0x" + "12" * 20] = _FakeVault("0x" + "12" * 20)
    monitor.latest_block = 5
    monitor.save_state()

    monitor._state_dirty = False  # only the cursor changed
    monitor.latest_block = 6
    monitor.save_state()

    with open(monitor.config.SAVE_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f)["last_saved_block"] == 6


def test_force_save_writes_even_when_clean(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.accounts["0x" + "12" * 20] = _FakeVault("0x" + "12" * 20)
    monitor.latest_block = 5
    monitor._state_dirty = False
    monitor.last_saved_block = 5  # cursor not advanced either

    monitor.save_state(force=True)
    assert os.path.exists(monitor.config.SAVE_STATE_PATH)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
