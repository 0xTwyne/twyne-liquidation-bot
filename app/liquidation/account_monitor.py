"""
Protocol-agnostic AccountMonitor - manages collateral vaults across all protocols.
"""

import json
import math
import os
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict

from app.liquidation import rpc_metrics
from app.liquidation.config_loader import MONITOR_WORKER_COUNT, ChainConfig
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import (
    post_liquidation_failed_notification,
    post_liquidation_opportunity_notification,
    post_liquidation_result_notification,
    post_low_health_account_report_notification,
    post_unhealthy_account_notification,
)
from app.liquidation.vaults.base_vault import HF_ONE, BaseCollateralVault, BaseLiquidator
from app.liquidation.vaults.registry import get_vault_class_for_protocol

logger = setup_logger()


class AccountMonitor:
    """
    Primary class for the liquidation bot system.
    Maintains accounts across all protocols, schedules updates,
    triggers liquidations, and manages state persistence.
    """

    def __init__(self, chain_id: int, config: ChainConfig, notify=False, execute_liquidation=False):
        self.chain_id = chain_id
        self.w3 = config.w3
        self.config = config
        self.accounts: Dict[str, BaseCollateralVault] = {}
        self.vaults = {}
        self.update_queue = queue.PriorityQueue()
        self.condition = threading.Condition()
        # Guards mutation and snapshotting of the shared in-memory state that is
        # touched by multiple threads: ``accounts`` (mutated by the listener
        # thread, iterated by save_state / sweeps / the Flask routes thread),
        # ``failed_initializations`` and the observability counters.
        #
        # LOCK-ORDERING RULE (deadlock safety): if both ``condition`` and
        # ``state_lock`` are ever needed, acquire ``condition`` FIRST and
        # ``state_lock`` SECOND, never the reverse. In practice ``state_lock``
        # only ever guards short, in-memory snapshot/mutation work — it is never
        # held while blocking on ``condition`` (no condition.wait() under it) and
        # never held across an RPC / network call or any long operation. Because
        # ``state_lock`` is always released promptly and never tries to acquire
        # ``condition`` while held, no circular wait can form.
        self.state_lock = threading.Lock()
        # Set by stop() to wake the periodic background threads out of their
        # interval waits promptly instead of sleeping up to an hour.
        self._stop_event = threading.Event()
        # Background threads started by start_queue_monitoring, joined by stop().
        self._background_threads: list[threading.Thread] = []
        # Set whenever shared state changes since the last successful save; lets
        # periodic_save skip rewriting an unchanged state file (P7 dirty flag).
        self._state_dirty = True
        self.processing_accounts: set[str] = set()
        # Addresses whose update arrived while they were already being processed.
        # The in-flight worker may have read pre-change state, so we guarantee one
        # more pass after it releases instead of silently dropping the coalesced update.
        self.pending_reprocess: set[str] = set()
        # Worker count is sized to match the HTTPProvider's connection pool (P4 / DEV-555).
        self.executor = ThreadPoolExecutor(max_workers=MONITOR_WORKER_COUNT)
        self.running = True
        self.latest_block = 0
        self.last_saved_block = 0
        self.notify = notify
        self.execute_liquidation = execute_liquidation

        self.recently_posted_low_value = {}
        # Track vaults that failed to initialize for retry
        # Format: {address: {"protocol": str, "retry_at": float, "attempts": int}}
        self.failed_initializations: Dict[str, dict] = {}

        # Observability counters/caches surfaced via /internal/observability.
        # Monotonic count of liquidation-execution failures (exceptions or
        # missing tx receipt); the metrics exporter alerts on its delta().
        self.liquidation_failure_count = 0
        # Cached (timestamp, signer_balance_eth) so the observability endpoint
        # doesn't issue an RPC call on every scrape. Refreshed at most once per
        # SIGNER_BALANCE_TTL_SECONDS.
        self._signer_balance_cache: "tuple[float, float | None]" = (0.0, None)

        # Liveness signal for the /health endpoint. Updated by both
        # _process_account_update and the FactoryListener loop; if neither
        # has run within HEALTH_STALE_SECONDS, /health returns 503 and the
        # synthetic-monitoring probe pages us.
        self._last_activity_at: "datetime | None" = None

    SIGNER_BALANCE_TTL_SECONDS = 60

    def _get_signer_balance_eth(self, now: float) -> "float | None":
        """Signer (liquidator EOA) gas balance in ETH, cached to avoid an RPC
        call on every observability scrape. Returns the last known value on
        RPC error rather than None, so a transient RPC blip doesn't look like
        an empty wallet."""
        ts, cached = self._signer_balance_cache
        if cached is not None and (now - ts) < self.SIGNER_BALANCE_TTL_SECONDS:
            return cached
        try:
            wei = self.w3.eth.get_balance(self.config.LIQUIDATOR_EOA)
            value = wei / 1e18
        except Exception as ex:
            logger.error("AccountMonitor: failed to read signer balance: %s", ex)
            value = cached  # keep last known (may be None on first failure)
        self._signer_balance_cache = (now, value)
        return value

    def get_observability_snapshot(self) -> dict:
        """Raw observability values for the metrics exporter. Thresholds live in
        Grafana, not here. Count-gated freshness fields are omitted when no CV
        has been checked yet, so an empty book reports NoData rather than a
        false staleness alert."""
        now = time.time()
        # Snapshot the shared state under the lock so a concurrent insert by the
        # listener thread can't raise "dict changed size during iteration".
        with self.state_lock:
            accounts = list(self.accounts.values())
            latest_block = self.latest_block
            liquidation_failure_count = self.liquidation_failure_count
            failed_init_count = len(self.failed_initializations)
        checked = [a.last_checked_at for a in accounts if getattr(a, "last_checked_at", None)]

        snap = {
            "monitored_cv_count": len(accounts),
            "cvs_below_external_boundary": sum(1 for a in accounts if a.external_health_score < 1),
            "unhealthy_cv_count": sum(1 for a in accounts if a.internal_health_score < 1),
            "latest_scanned_block": latest_block,
            "liquidation_failure_count": liquidation_failure_count,
            "failed_init_count": failed_init_count,
        }
        balance = self._get_signer_balance_eth(now)
        if balance is not None:
            snap["signer_balance_eth"] = balance
        if checked:
            snap["seconds_since_last_cv_check"] = max(0.0, now - max(checked))
            snap["oldest_cv_check_age_seconds"] = max(0.0, now - min(checked))
        return snap

    def start_queue_monitoring(self) -> None:
        # Background threads are daemons and tracked so stop() can join them.
        # They wait on self._stop_event (instead of a bare time.sleep) so they
        # exit promptly on shutdown instead of sleeping out a full interval.
        self._background_threads = []

        save_thread = threading.Thread(target=self.periodic_save, daemon=True)
        save_thread.start()
        self._background_threads.append(save_thread)
        logger.info("AccountMonitor: Save thread started.")

        stale_sweep_thread = threading.Thread(target=self.periodic_sweep_stale_accounts, daemon=True)
        stale_sweep_thread.start()
        self._background_threads.append(stale_sweep_thread)
        logger.info("AccountMonitor: Stale account sweep thread started (runs every hour).")

        failed_init_retry_thread = threading.Thread(target=self.periodic_retry_failed_initializations, daemon=True)
        failed_init_retry_thread.start()
        self._background_threads.append(failed_init_retry_thread)
        logger.info("AccountMonitor: Failed initialization retry thread started (runs every 5 minutes).")

        if self.notify:
            low_health_report_thread = threading.Thread(target=self.periodic_report_low_health_accounts, daemon=True)
            low_health_report_thread.start()
            self._background_threads.append(low_health_report_thread)
            logger.info("AccountMonitor: Low health report thread started.")

        while self.running:
            with self.condition:
                while self.running and self.update_queue.empty():
                    logger.info("AccountMonitor: Waiting for queue to be non-empty.")
                    self.condition.wait()

                if not self.running:
                    break

                next_update_time, address = self.update_queue.get()
                current_time = time.time()
                if next_update_time > current_time:
                    self.update_queue.put((next_update_time, address))
                    self.condition.wait(next_update_time - current_time)
                    continue

                if not self._claim_account_processing(address):
                    continue

                self.executor.submit(self._process_account_update, address)

    def enqueue_account_update(self, address: str, update_time: float | None = None) -> None:
        with self.condition:
            self.update_queue.put((time.time() if update_time is None else update_time, address))
            self.condition.notify()

    def _claim_account_processing(self, address: str) -> bool:
        with self.condition:
            if address in self.processing_accounts:
                # An update raced an in-flight worker; mark dirty so the worker
                # re-enqueues this address once on release rather than losing the signal.
                self.pending_reprocess.add(address)
                return False
            self.processing_accounts.add(address)
            return True

    def _release_account_processing(self, address: str) -> None:
        with self.condition:
            self.processing_accounts.discard(address)
            if address in self.pending_reprocess:
                self.pending_reprocess.discard(address)
                # Re-enqueue immediately for one more pass with fresh state.
                self.update_queue.put((time.time(), address))
            self.condition.notify()

    def _process_account_update(self, address: str) -> None:
        self._last_activity_at = datetime.now(timezone.utc)
        # Reset the per-thread eth_call counter so we can log how many eth_calls this
        # single tick issued — proof of the DEV-554 steady-state reduction (~9 -> <=2).
        # install_eth_call_counter is idempotent and fail-open.
        rpc_metrics.install_eth_call_counter(self.w3)
        rpc_metrics.reset_eth_call_count()
        try:
            self.update_account_liquidity(address)
        finally:
            logger.debug(
                "AccountMonitor: %s tick issued %d eth_call(s)",
                address,
                rpc_metrics.get_eth_call_count(),
            )
            self._release_account_processing(address)

    def update_account_on_status_check_event(self, address: str, protocol: str = "euler") -> None:
        """
        Update an account based on a status check event.

        Args:
            address: The address of the account to update.
            protocol: The protocol this vault belongs to ("euler", "aave", etc.)
        """
        with self.state_lock:
            already_known = address in self.accounts
        if already_known:
            logger.info("AccountMonitor: %s already in list.", address)
            self.enqueue_account_update(address)
            return

        # Try to initialize the vault
        try:
            vault_class = get_vault_class_for_protocol(protocol)
            account = vault_class(address, self.config)

            logger.info("AccountMonitor: Adding %s (%s) to account list.", address, protocol)

            # NOTE: the RPC-bound update_liquidity() runs OUTSIDE state_lock so we
            # never hold the lock across a network call (lock-ordering rule).
            [internal_health_score, external_health_score, externallyLiquidated] = account.update_liquidity()
            next_update_time = account.time_of_next_update

            # Only add to accounts after successful initialization. The mutation of
            # accounts / failed_initializations is the only state_lock-guarded part.
            with self.state_lock:
                self.accounts[address] = account
                # Remove from failed list if it was there (successful retry)
                recovered = self.failed_initializations.pop(address, None) is not None
                self._state_dirty = True
            if recovered:
                logger.info("AccountMonitor: %s recovered from failed initialization.", address)

            self.enqueue_account_update(address, next_update_time)

            logger.info(
                "AccountMonitor: %s initialized with internal health score %s, external health score %s, externallyLiq %s, next update at %s",
                address,
                internal_health_score,
                external_health_score,
                externallyLiquidated,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
            )

        except Exception as ex:
            logger.error("AccountMonitor: Failed to initialize account %s: %s", address, ex, exc_info=True)
            self._track_failed_initialization(address, protocol)

    def update_account_liquidity(self, address: str) -> None:
        try:
            account = self.accounts.get(address)

            if not account:
                logger.error("AccountMonitor: %s not found in account list.", address, exc_info=True)
                return

            prev_scheduled_time = account.time_of_next_update

            [internal_health_score, external_health_score, externally_liquidated] = account.update_liquidity()

            if (
                account.internal_health_score_raw < HF_ONE
                or account.external_health_score_raw < HF_ONE
                or externally_liquidated
                or account.health_unknown
            ):
                # Gate the (5-eth_call) on-chain liquidation check on the cheap
                # snapshot already in hand. For a healthy, non-externally-liquidated
                # vault canLiquidate() is provably False — the HealthStatViewer health
                # factors mirror the very _canLiquidate() math the contract uses, so
                # inHF >= 1 AND extHF >= 1 implies canLiquidate() == False — so skipping
                # the check changes NO liquidation decision while cutting the
                # steady-state tick to <=2 eth_calls (health + isExternallyLiquidated).
                # See DEV-554. Once the gate opens, the authoritative on-chain
                # check_liquidation result drives the decision exactly as before.
                #
                # `health_unknown` also opens the gate (DEV-661). When the health read
                # fails repeatedly the health factors hold a fallback value that reads
                # as healthy, so without this the bot would silently stop liquidating.
                # The on-chain canLiquidate() does not use the lens, so it still gives
                # the right answer. This costs the full 5-eth_call check per tick for
                # every affected vault, which is the intended trade for not going blind.
                #
                # Compare the RAW 1e18-scaled HF integers (set by get_health_score during
                # the update_liquidity() call above), NOT the /1e18 floats: float64
                # granularity at 1e18 is ~128, so a raw HF of (1e18 - 1) — truly
                # liquidatable — would round to 1.0 and wrongly keep this gate closed.
                # The raw comparison is exact at the boundary. math.inf < HF_ONE is False,
                # so inf/zero-liability stays "healthy" exactly like the old `inf < 1`.
                (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = (
                    account.check_liquidation(self.config.LIQUIDATOR_EOA)
                )

                if (
                    can_liquidate
                    or (externally_liquidated and max_release > 0)
                    or internal_health_score < 1
                    or external_health_score < 1
                ):
                    try:
                        logger.info("LIQUIDATION FOUND: %s", address)
                        self._handle_unhealthy_notification(
                            account, address, externally_liquidated, internal_health_score, external_health_score
                        )

                        logger.info(
                            "AccountMonitor: %s is UNHEALTHY (inHF=%s, exHF=%s, borrowed=%s), simulating liquidation",
                            address,
                            internal_health_score,
                            external_health_score,
                            account.internal_value_borrowed + account.external_value_borrowed,
                        )

                        self._handle_liquidation(account, address, can_liquidate, externally_liquidated)

                    except Exception as ex:
                        logger.error(
                            "AccountMonitor: Exception simulating liquidation for account %s: %s",
                            address,
                            ex,
                            exc_info=True,
                        )

            next_update_time = account.time_of_next_update

            if next_update_time == prev_scheduled_time:
                logger.info(
                    "AccountMonitor: %s next update already scheduled for %s",
                    address,
                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
                )
                return

            with self.condition:
                self.update_queue.put((next_update_time, address))
                self.condition.notify()

        except Exception as ex:
            logger.error("AccountMonitor: Exception updating account %s: %s", address, ex, exc_info=True)
            # Schedule retry to prevent account from being stuck with stale timestamp
            retry_delay = 60  # Retry in 60 seconds
            account.time_of_next_update = time.time() + retry_delay
            logger.info(
                "AccountMonitor: Scheduling retry for %s in %s seconds at %s",
                address,
                retry_delay,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(account.time_of_next_update)),
            )
            with self.condition:
                self.update_queue.put((account.time_of_next_update, address))
                self.condition.notify()

    def _handle_unhealthy_notification(
        self,
        account,
        address: str,
        externally_liquidated: bool,
        internal_health_score: float,
        external_health_score: float,
    ) -> None:
        """Post unhealthy account notification with throttling for small positions."""
        if not self.notify:
            return

        total_borrowed = account.internal_value_borrowed + account.external_value_borrowed
        if account.address in self.recently_posted_low_value:
            if (
                time.time() - self.recently_posted_low_value[account.address] < self.config.LOW_HEALTH_REPORT_INTERVAL
                and total_borrowed < self.config.SMALL_POSITION_THRESHOLD
            ):
                logger.info("Skipping posting notification for account %s, recently posted", address)
                return

        try:
            stats = account.get_position_stats()
            post_unhealthy_account_notification(
                address,
                externally_liquidated,
                internal_health_score,
                external_health_score,
                account.internal_value_borrowed,
                account.external_value_borrowed,
                self.config,
                stats=stats,
            )
            if total_borrowed < self.config.SMALL_POSITION_THRESHOLD:
                self.recently_posted_low_value[account.address] = time.time()
        except Exception as ex:
            logger.error(
                "AccountMonitor: Failed to post low health notification for %s: %s",
                address,
                ex,
                exc_info=True,
            )

    def _increment_liquidation_failure_count(self) -> None:
        """Bump the liquidation-failure counter under state_lock; it is read by the
        observability snapshot thread."""
        with self.state_lock:
            self.liquidation_failure_count += 1

    def _handle_liquidation(self, account, address: str, can_liquidate: bool, externally_liquidated: bool) -> None:
        """Simulate liquidation, execute if profitable, and post notifications."""
        try:
            (result, liquidation_data, params) = account.simulate_liquidation()
        except Exception as sim_error:
            logger.error("simulate_liquidation failed for %s: %s", address, sim_error, exc_info=True)
            return

        if not ((result and can_liquidate) or externally_liquidated) or liquidation_data is None:
            logger.info("AccountMonitor: %s is unhealthy but not profitable to liquidate.", address)
            return

        stats = account.get_position_stats()

        if self.notify:
            try:
                post_liquidation_opportunity_notification(address, liquidation_data, params, self.config, stats=stats)
            except Exception as ex:
                logger.error("Failed to post liquidation notification for %s: %s", address, ex, exc_info=True)

        if self.execute_liquidation:
            try:
                liq_tx_hash, liq_tx_receipt = BaseLiquidator.execute_liquidation(liquidation_data["tx"], self.config)

                tx_status = liq_tx_receipt.get("status") if liq_tx_receipt is not None else None

                if liq_tx_hash and tx_status == 1:
                    logger.info(
                        "AccountMonitor: %s liquidated on collateral %s (tx %s).",
                        address,
                        liquidation_data["collateral_address"],
                        liq_tx_hash,
                    )
                    if self.notify:
                        try:
                            post_liquidation_result_notification(
                                address,
                                liquidation_data,
                                liq_tx_hash,
                                self.config,
                                stats=stats,
                                liq_tx_receipt=liq_tx_receipt,
                                account=account,
                            )
                        except Exception as ex:
                            logger.error("Failed to post liquidation result for %s: %s", address, ex, exc_info=True)
                elif liq_tx_hash and liq_tx_receipt is not None:
                    # Transaction mined but REVERTED (status == 0): no liquidation, gas lost.
                    self._increment_liquidation_failure_count()
                    logger.warning(
                        "AccountMonitor: liquidation of %s REVERTED on-chain (tx %s, status=%s).",
                        address,
                        liq_tx_hash,
                        tx_status,
                    )
                    if self.notify:
                        try:
                            post_liquidation_failed_notification(
                                address,
                                liquidation_data,
                                liq_tx_hash,
                                self.config,
                                liq_tx_receipt=liq_tx_receipt,
                                account=account,
                                stats=stats,
                            )
                        except Exception as ex:
                            logger.error("Failed to post liquidation failure for %s: %s", address, ex, exc_info=True)
                else:
                    # Execution returned without a confirmed tx — count as a failure.
                    self._increment_liquidation_failure_count()
                    logger.warning("AccountMonitor: liquidation of %s returned no tx receipt.", address)

                account.update_liquidity()
            except Exception as ex:
                self._increment_liquidation_failure_count()
                logger.error("Failed to execute liquidation for %s: %s", address, ex, exc_info=True)

    def _track_failed_initialization(self, address: str, protocol: str) -> None:
        """Track a failed vault initialization for later retry."""
        current_time = time.time()

        with self.state_lock:
            entry = self.failed_initializations.get(address)
            if entry is not None:
                # Increment attempts and use exponential backoff
                entry["attempts"] += 1
                # Exponential backoff: 1min, 2min, 4min, 8min, ... capped at 1 hour
                backoff = min(60 * (2 ** (entry["attempts"] - 1)), 3600)
                entry["retry_at"] = current_time + backoff
                attempts = entry["attempts"]
                retry_at = entry["retry_at"]
            else:
                # First failure - retry in 60 seconds
                backoff = 60
                retry_at = current_time + 60
                attempts = 1
                self.failed_initializations[address] = {
                    "protocol": protocol,
                    "retry_at": retry_at,
                    "attempts": 1,
                }
            self._state_dirty = True

        logger.warning(
            "AccountMonitor: Vault %s failed initialization (attempt %s), will retry in %s seconds at %s",
            address,
            attempts,
            backoff,
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(retry_at)),
        )

    def retry_failed_initializations(self) -> int:
        """
        Retry initialization of vaults that previously failed.

        Returns:
            int: Number of vaults successfully initialized.
        """
        current_time = time.time()
        success_count = 0
        addresses_to_retry = []

        # Find addresses due for retry. Iterate over a snapshot taken under the
        # lock so a concurrent mutation by the listener thread can't raise
        # "dictionary changed size during iteration".
        with self.state_lock:
            failed_snapshot = list(self.failed_initializations.items())
        for address, entry in failed_snapshot:
            if entry["retry_at"] <= current_time:
                addresses_to_retry.append((address, entry["protocol"]))

        if not addresses_to_retry:
            return 0

        logger.info(
            "AccountMonitor: Retrying initialization for %s failed vaults",
            len(addresses_to_retry),
        )

        for address, protocol in addresses_to_retry:
            try:
                # This will either succeed and remove from failed_initializations,
                # or fail and update the retry time with backoff
                self.update_account_on_status_check_event(address, protocol)

                # Check if it succeeded (address should now be in accounts)
                with self.state_lock:
                    succeeded = address in self.accounts and address not in self.failed_initializations
                if succeeded:
                    success_count += 1

            except Exception as ex:
                logger.error(
                    "AccountMonitor: Unexpected error retrying initialization for %s: %s",
                    address,
                    ex,
                    exc_info=True,
                )

        if success_count > 0:
            logger.info(
                "AccountMonitor: Successfully initialized %s/%s previously failed vaults",
                success_count,
                len(addresses_to_retry),
            )

        return success_count

    # Maximum number of initialization attempts before a vault is abandoned (P8 / DEV-555).
    # With the exponential back-off capping at 3600 s, 20 attempts ≈ 14+ hours of retries.
    # Beyond this point the vault is assumed to require manual intervention.
    MAX_FAILED_INIT_ATTEMPTS = 20

    def periodic_retry_failed_initializations(self) -> None:
        """
        Periodically retry initialization of failed vaults.
        Should be run in a standalone thread.
        Runs every 5 minutes.
        """
        retry_interval = 300  # 5 minutes
        # Event.wait returns True as soon as stop() sets the event, so we exit the
        # interval promptly on shutdown instead of sleeping out the full 5 minutes.
        while not self._stop_event.wait(retry_interval):
            try:
                with self.state_lock:
                    has_failures = bool(self.failed_initializations)
                if has_failures:
                    self.retry_failed_initializations()

                # TTL-prune failed_initializations entries that have exhausted their
                # retry budget (P8 / DEV-555).  After MAX_FAILED_INIT_ATTEMPTS the
                # back-off has been sitting at ~1 h for many cycles; continued retrying
                # is unlikely to help and keeps the dict growing forever for truly
                # broken vaults.  Pruning under state_lock so the observability
                # snapshot and the listener-thread insert path are serialised correctly.
                with self.state_lock:
                    exhausted = [
                        addr
                        for addr, entry in self.failed_initializations.items()
                        if entry.get("attempts", 0) >= self.MAX_FAILED_INIT_ATTEMPTS
                    ]
                    for addr in exhausted:
                        entry = self.failed_initializations.pop(addr)
                        logger.warning(
                            "AccountMonitor: Abandoning failed initialization for %s after %s attempts "
                            "(>= MAX_FAILED_INIT_ATTEMPTS=%s); removing from retry queue. "
                            "Manual intervention may be required.",
                            addr,
                            entry.get("attempts", 0),
                            self.MAX_FAILED_INIT_ATTEMPTS,
                        )
                    if exhausted:
                        self._state_dirty = True
            except Exception as ex:
                logger.error(
                    "AccountMonitor: Error during failed initialization retry: %s",
                    ex,
                    exc_info=True,
                )

    def save_state(self, local_save: bool = True, force: bool = False) -> None:
        try:
            # Snapshot everything under the lock so the listener thread can't
            # mutate accounts / failed_initializations while we serialise them.
            # Capture latest_block BEFORE the accounts snapshot so the persisted
            # cursor can never be ahead of the account set: a vault discovered
            # after this point will be re-scanned from this (older) cursor, never
            # skipped. The (short, in-memory) to_dict() calls are CPU-only and
            # take no locks of their own, so holding state_lock here is safe.
            with self.state_lock:
                latest_block = self.latest_block
                # Dirty if any guarded state changed (_state_dirty) OR the scan
                # cursor advanced since the last persisted block. The latter
                # covers the listener thread advancing latest_block without
                # touching accounts (it doesn't go through the dirty flag).
                cursor_advanced = latest_block != self.last_saved_block
                if not force and not self._state_dirty and not cursor_advanced:
                    logger.debug("AccountMonitor: State unchanged since last save, skipping write.")
                    return
                state = {
                    "version": 1,
                    "accounts": {address: account.to_dict() for address, account in self.accounts.items()},
                    "last_saved_block": latest_block,
                    "failed_initializations": dict(self.failed_initializations),
                }
                # Clear the dirty flag optimistically inside the lock. If the write
                # below fails we re-set it so the next cadence retries the save.
                self._state_dirty = False

            if local_save:
                self._atomic_write_state(self.config.SAVE_STATE_PATH, state)

            with self.state_lock:
                self.last_saved_block = latest_block

            logger.info(
                "AccountMonitor: State saved at time %s up to block %s",
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                latest_block,
            )
        except Exception as ex:
            # The persisted state is now stale relative to memory; mark dirty so a
            # later save attempts to flush it again.
            with self.state_lock:
                self._state_dirty = True
            logger.error("AccountMonitor: Failed to save state: %s", ex, exc_info=True)

    @staticmethod
    def _atomic_write_state(path: str, state: dict) -> None:
        """Crash-safe state write (B7).

        Sequence:
        1. Serialise to ``path + ".tmp"``, then ``flush`` + ``os.fsync`` so the
           bytes are durably on disk before we swap.
        2. COPY (not move) the current primary to ``path + ".bak"`` so a complete
           previous good copy always survives even if the swap below crashes.
        3. ``os.replace(tmp, path)`` — atomic on POSIX, so the primary file is
           never observed truncated: a reader/crash sees either the complete old
           state or the complete new state.

        We copy rather than rename the primary into ``.bak`` precisely so that the
        primary is never momentarily absent: if the process dies between steps 2
        and 3, the primary still holds the complete OLD state, and ``.bak`` is an
        independent complete copy. If it dies mid-step-3, ``os.replace`` guarantees
        the primary is either fully old or fully new, and ``.bak`` still holds the
        previous good state for load_state to fall back to."""
        tmp_path = path + ".tmp"
        bak_path = path + ".bak"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())

        # Keep a complete copy of the previous good file as .bak (copy, not move,
        # so the primary is never absent during the swap window).
        if os.path.exists(path):
            try:
                with open(path, "rb") as src, open(bak_path, "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
            except OSError as ex:  # noqa: BLE001 - backup is best-effort
                logger.warning("AccountMonitor: failed to write state backup: %s", ex)

        os.replace(tmp_path, path)

    def load_state(self, save_path: str, local_save: bool = True) -> None:
        if not local_save:
            return

        state = self._read_state_file(save_path)
        if state is None:
            logger.info("AccountMonitor: No usable saved state found, starting fresh.")
            return

        state_version = state.get("version")
        if state_version != 1:
            logger.warning("AccountMonitor: State version mismatch (got %s, expected 1)", state_version)

        try:
            # Restore the cursor BEFORE reconstructing accounts (B12). A vault that
            # fails to reconstruct routes into failed_initializations rather than
            # aborting the whole load, and the cursor is preserved either way, so a
            # single transient RPC failure can never silently reset last_saved_block
            # to 0 and trigger a full factory rescan.
            self.last_saved_block = state.get("last_saved_block", 0)
            self.latest_block = self.last_saved_block

            self.accounts = {}
            persisted_failed = dict(state.get("failed_initializations", {}))

            reconstruct_failures = 0
            for address, data in state.get("accounts", {}).items():
                protocol = data.get("protocol", "euler")
                try:
                    vault_class = get_vault_class_for_protocol(protocol)
                    self.accounts[address] = vault_class.from_dict(data, self.config)
                except Exception as ex:
                    # Per-account isolation: one bad vault must not abandon the rest.
                    reconstruct_failures += 1
                    logger.error(
                        "AccountMonitor: Failed to reconstruct account %s (%s) on load, "
                        "routing to failed_initializations for retry: %s",
                        address,
                        protocol,
                        ex,
                        exc_info=True,
                    )
                    persisted_failed[address] = {
                        "protocol": protocol,
                        "retry_at": time.time() + 60,
                        "attempts": 1,
                    }

            logger.info(
                "Loaded %s accounts (%s failed to reconstruct):",
                len(self.accounts),
                reconstruct_failures,
            )

            for address, account in self.accounts.items():
                logger.info(
                    "Account %s (%s), Internal Health Score: %s, External Health Score: %s, Next Update: %s",
                    address,
                    account.protocol,
                    account.internal_health_score,
                    account.external_health_score,
                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(account.time_of_next_update)),
                )

            # rebuild_queue runs after the cursor is set and accounts are loaded; a
            # per-account failure above does not skip it.
            self.rebuild_queue()

            self.failed_initializations = persisted_failed
            if self.failed_initializations:
                logger.info(
                    "AccountMonitor: Loaded %s failed initializations for retry",
                    len(self.failed_initializations),
                )

            # State on disk already reflects what we just loaded.
            self._state_dirty = False

            logger.info(
                "AccountMonitor: State loaded from save file %s from block %s to block %s",
                save_path,
                self.config.CVAULT_FACTORY_DEPLOYMENT_BLOCK,
                self.latest_block,
            )
        except Exception as ex:
            logger.error("AccountMonitor: Failed to load state: %s", ex, exc_info=True)

    @staticmethod
    def _read_state_file(save_path: str) -> "dict | None":
        """Read and parse the state file, falling back to the ``.bak`` copy if the
        primary is missing or corrupt (B7). Returns the parsed dict, or None if no
        usable state exists."""
        bak_path = save_path + ".bak"
        for path, is_backup in ((save_path, False), (bak_path, True)):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if is_backup:
                    logger.warning(
                        "AccountMonitor: primary state file unusable, recovered from backup %s",
                        path,
                    )
                return state
            except (json.JSONDecodeError, IOError, OSError) as ex:
                logger.error("AccountMonitor: state file %s unusable (%s), trying fallback.", path, ex)
                continue
        return None

    def rebuild_queue(self):
        logger.info("Rebuilding queue based on current account health")

        self.update_queue = queue.PriorityQueue()
        # Snapshot the items under the lock; update_liquidity() (RPC-bound) runs
        # against the snapshotted account objects outside the lock.
        with self.state_lock:
            account_items = list(self.accounts.items())

        # Refresh each account's health in PARALLEL (each update_liquidity is an
        # independent, RPC-bound op). On a restart this turns the previously serial
        # per-vault health refresh into ~N/workers wall-clock instead of N serial
        # round-trips — the "parallelize remaining reconstruction" half of finding P3
        # (the immutable metadata RPCs are eliminated entirely via from_dict). Queue
        # puts stay serial on THIS thread so the PriorityQueue is never mutated
        # concurrently; only the network refresh fans out.
        def _refresh(item):
            address, account = item
            try:
                account.update_liquidity()
                return (address, account, None)
            except Exception as ex:  # noqa: BLE001 - captured, handled serially below
                return (address, account, ex)

        results = list(self.executor.map(_refresh, account_items)) if account_items else []

        for address, account, exc in results:
            if exc is None:
                next_update_time = account.time_of_next_update
                self.update_queue.put((next_update_time, address))

                if account.internal_health_score == math.inf and account.external_health_score == math.inf:
                    logger.info(
                        "AccountMonitor: %s has no borrow, scheduled for max-interval check at %s",
                        address,
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
                    )
                else:
                    logger.info(
                        "AccountMonitor: %s added to queue with inHF=%s, exHF=%s, next update at %s",
                        address,
                        account.internal_health_score,
                        account.external_health_score,
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
                    )
            else:
                logger.error(
                    "AccountMonitor: Failed to put account %s into rebuilt queue: %s", address, exc, exc_info=exc
                )
                # Schedule retry to prevent account from being stuck with stale timestamp
                retry_delay = 60  # Retry in 60 seconds
                account.time_of_next_update = time.time() + retry_delay
                self.update_queue.put((account.time_of_next_update, address))
                logger.info(
                    "AccountMonitor: Scheduling retry for failed account %s in %s seconds",
                    address,
                    retry_delay,
                )

        logger.info("AccountMonitor: Queue rebuilt with %s accounts", self.update_queue.qsize())

    def get_accounts_by_health_score(self):
        # Called from the Flask routes thread as well as the report thread; snapshot
        # the account objects under the lock so a concurrent insert by the listener
        # thread can't raise "dictionary changed size during iteration". The
        # subsequent sort reads attributes off the snapshotted objects only.
        with self.state_lock:
            accounts_snapshot = list(self.accounts.values())
        sorted_accounts = sorted(
            accounts_snapshot,
            key=lambda account: min(account.internal_health_score, account.external_health_score),
        )

        return [
            (
                account.address,
                account.internal_health_score,
                account.external_health_score,
                account.balanceOf,
                account.internal_value_borrowed,
                account.external_value_borrowed,
                account.underlying_asset_symbol,
                account,
            )
            for account in sorted_accounts
        ]

    def periodic_report_low_health_accounts(self):
        while self.running:
            try:
                sorted_accounts = self.get_accounts_by_health_score()
                post_low_health_account_report_notification(sorted_accounts, self.config)
            except Exception as ex:
                logger.error("AccountMonitor: Failed to post low health account report: %s", ex, exc_info=True)
            # Wait on the stop event so shutdown wakes us promptly instead of
            # sleeping out the full report interval; wait() returning True means
            # stop() fired, so exit the loop.
            if self._stop_event.wait(self.config.LOW_HEALTH_REPORT_INTERVAL):
                break

    @staticmethod
    def create_from_save_state(
        chain_id: int, config: ChainConfig, save_path: str, local_save: bool = True
    ) -> "AccountMonitor":
        monitor = AccountMonitor(chain_id=chain_id, config=config)
        monitor.load_state(save_path, local_save)
        return monitor

    def periodic_save(self) -> None:
        # Wait on the stop event so shutdown wakes us promptly. save_state only
        # writes when state changed since the last save (P7 dirty flag).
        while not self._stop_event.wait(self.config.SAVE_INTERVAL):
            self.save_state()

    def sweep_stale_accounts(self) -> int:
        """
        Find accounts with stale timestamps (too far in the past) and re-queue them.
        This is a defense-in-depth mechanism to catch accounts that may have been
        orphaned due to errors.

        Returns:
            int: Number of stale accounts found and re-queued.
        """
        current_time = time.time()
        stale_threshold = 3600  # Consider stale if timestamp is more than 1 hour in the past
        stale_count = 0

        # Iterate over a snapshot taken under the lock so a concurrent insert by
        # the listener thread can't raise "dict changed size during iteration".
        with self.state_lock:
            account_items = list(self.accounts.items())
        for address, account in account_items:
            if account.time_of_next_update < current_time - stale_threshold:
                logger.warning(
                    "AccountMonitor: Found stale account %s with timestamp %s (%s ago), re-queueing",
                    address,
                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(account.time_of_next_update)),
                    f"{(current_time - account.time_of_next_update) / 3600:.1f} hours",
                )
                # Schedule for immediate check with small jitter to avoid thundering herd
                account.time_of_next_update = current_time + random.uniform(0, 60)
                with self.condition:
                    self.update_queue.put((account.time_of_next_update, address))
                    self.condition.notify()
                stale_count += 1

        if stale_count > 0:
            logger.info("AccountMonitor: Stale account sweep found and re-queued %s accounts", stale_count)

        # TTL-prune the recently_posted_low_value throttle dict (P8 / DEV-555).
        # Entries older than LOW_HEALTH_REPORT_INTERVAL are dead weight — the
        # cooldown window has passed and any future post for that address is
        # allowed unconditionally already, so there is no value in keeping the
        # entry.  Iterate over a snapshot to avoid "dict changed size" errors
        # from concurrent worker writes.
        prune_cutoff = current_time - self.config.LOW_HEALTH_REPORT_INTERVAL
        stale_throttle = [addr for addr, ts in list(self.recently_posted_low_value.items()) if ts < prune_cutoff]
        for addr in stale_throttle:
            self.recently_posted_low_value.pop(addr, None)
        if stale_throttle:
            logger.debug("AccountMonitor: Pruned %s stale recently_posted_low_value entries", len(stale_throttle))

        return stale_count

    def periodic_sweep_stale_accounts(self) -> None:
        """
        Periodically sweep for stale accounts and re-queue them.
        Should be run in a standalone thread.
        Runs every hour as a defense-in-depth mechanism.
        """
        sweep_interval = 3600  # 1 hour
        # Wait on the stop event so shutdown wakes us promptly instead of sleeping
        # out the full hour.
        while not self._stop_event.wait(sweep_interval):
            try:
                self.sweep_stale_accounts()
            except Exception as ex:
                logger.error("AccountMonitor: Error during stale account sweep: %s", ex, exc_info=True)

    def stop(self) -> None:
        """Graceful shutdown (B10). Ordering is load-bearing:

        1. Flip ``running`` and set ``_stop_event`` so the dispatcher loop, the
           periodic background threads, and any in-flight workers stop scheduling
           new work.
        2. Wake the dispatcher (waiting on ``condition``) and the background
           threads (waiting on ``_stop_event``).
        3. ``executor.shutdown(wait=True)`` DRAINS in-flight workers so no thread
           is still mutating ``accounts`` / counters when we persist.
        4. Join the background threads so the save thread can't race the final save.
        5. Run a FINAL forced ``save_state()`` AFTER all mutators have stopped, so
           the persisted snapshot is consistent and not torn by a concurrent write.
        """
        self.running = False
        self._stop_event.set()
        with self.condition:
            self.condition.notify_all()

        # Drain workers FIRST so nothing mutates shared state during the final save.
        self.executor.shutdown(wait=True)

        # Join the periodic background threads (they wake on _stop_event). Done
        # before the final save so the periodic save thread can't race it.
        for thread in self._background_threads:
            thread.join(timeout=10)
        self._background_threads = []

        # Final save runs only after all mutators have stopped. force=True so we
        # always flush the latest state on shutdown even if the dirty flag is clear.
        self.save_state(force=True)
