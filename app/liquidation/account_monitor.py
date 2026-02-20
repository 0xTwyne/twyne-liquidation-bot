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
from typing import Dict

from app.liquidation.config_loader import ChainConfig
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import (
    post_liquidation_opportunity_notification,
    post_liquidation_result_notification,
    post_low_health_account_report_notification,
    post_unhealthy_account_notification,
)
from app.liquidation.vaults.base_vault import BaseCollateralVault, BaseLiquidator
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
        self.executor = ThreadPoolExecutor(max_workers=32)
        self.running = True
        self.latest_block = 0
        self.last_saved_block = 0
        self.notify = notify
        self.execute_liquidation = execute_liquidation

        self.recently_posted_low_value = {}
        # Track vaults that failed to initialize for retry
        # Format: {address: {"protocol": str, "retry_at": float, "attempts": int}}
        self.failed_initializations: Dict[str, dict] = {}

    def start_queue_monitoring(self) -> None:
        save_thread = threading.Thread(target=self.periodic_save)
        save_thread.start()
        logger.info("AccountMonitor: Save thread started.")

        stale_sweep_thread = threading.Thread(target=self.periodic_sweep_stale_accounts)
        stale_sweep_thread.start()
        logger.info("AccountMonitor: Stale account sweep thread started (runs every hour).")

        failed_init_retry_thread = threading.Thread(target=self.periodic_retry_failed_initializations)
        failed_init_retry_thread.start()
        logger.info("AccountMonitor: Failed initialization retry thread started (runs every 5 minutes).")

        if self.notify:
            low_health_report_thread = threading.Thread(target=self.periodic_report_low_health_accounts)
            low_health_report_thread.start()
            logger.info("AccountMonitor: Low health report thread started.")

        processing_accounts = set()

        while self.running:
            with self.condition:
                while self.update_queue.empty():
                    logger.info("AccountMonitor: Waiting for queue to be non-empty.")
                    self.condition.wait()

                next_update_time, address = self.update_queue.get()
                current_time = time.time()
                if next_update_time > current_time:
                    self.update_queue.put((next_update_time, address))
                    self.condition.wait(next_update_time - current_time)
                    continue

                if address in processing_accounts:
                    continue

                processing_accounts.add(address)
                self.executor.submit(self._process_account_update, address, processing_accounts)

    def _process_account_update(self, address: str, processing_accounts: set) -> None:
        try:
            self.update_account_liquidity(address)
        finally:
            processing_accounts.remove(address)

    def update_account_on_status_check_event(self, address: str, protocol: str = "euler") -> None:
        """
        Update an account based on a status check event.

        Args:
            address: The address of the account to update.
            protocol: The protocol this vault belongs to ("euler", "aave", etc.)
        """
        if address in self.accounts:
            logger.info("AccountMonitor: %s already in list.", address)
            self.update_account_liquidity(address)
            return

        # Try to initialize the vault
        try:
            vault_class = get_vault_class_for_protocol(protocol)
            account = vault_class(address, self.config)

            logger.info("AccountMonitor: Adding %s (%s) to account list.", address, protocol)

            [internal_health_score, external_health_score, externallyLiquidated] = account.update_liquidity()
            next_update_time = account.time_of_next_update

            # Only add to accounts after successful initialization
            self.accounts[address] = account

            with self.condition:
                self.update_queue.put((next_update_time, address))
                self.condition.notify()

            # Remove from failed list if it was there (successful retry)
            if address in self.failed_initializations:
                del self.failed_initializations[address]
                logger.info("AccountMonitor: %s recovered from failed initialization.", address)

            logger.info(
                "AccountMonitor: %s initialized with internal health score %s, external health score %s, externallyLiq %s, next update at %s",
                address, internal_health_score, external_health_score, externallyLiquidated,
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
            if account.target_asset.lower() == self.config.USDS_ADDRESS.lower():
                logger.info("AccountMonitor: Skipping position with USDS debt on Base")
                return

            (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = account.check_liquidation(
                self.config.LIQUIDATOR_EOA
            )

            if can_liquidate or (externally_liquidated and max_release > 0) or internal_health_score < 1 or external_health_score < 1:
                try:
                    logger.info("LIQUIDATION FOUND: %s", address)
                    self._handle_unhealthy_notification(account, address, externally_liquidated,
                                                        internal_health_score, external_health_score)

                    logger.info(
                        "AccountMonitor: %s is UNHEALTHY (inHF=%s, exHF=%s, borrowed=%s), simulating liquidation",
                        address, internal_health_score, external_health_score,
                        account.internal_value_borrowed + account.external_value_borrowed,
                    )

                    self._handle_liquidation(account, address, can_liquidate, externally_liquidated)

                except Exception as ex:
                    logger.error(
                        "AccountMonitor: Exception simulating liquidation for account %s: %s",
                        address, ex, exc_info=True,
                    )

            next_update_time = account.time_of_next_update

            if next_update_time == prev_scheduled_time:
                logger.info(
                    "AccountMonitor: %s next update already scheduled for %s",
                    address, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
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
                address, retry_delay,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(account.time_of_next_update)),
            )
            with self.condition:
                self.update_queue.put((account.time_of_next_update, address))
                self.condition.notify()

    def _handle_unhealthy_notification(self, account, address: str, externally_liquidated: bool,
                                         internal_health_score: float, external_health_score: float) -> None:
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
            post_unhealthy_account_notification(
                address, externally_liquidated, internal_health_score, external_health_score,
                account.internal_value_borrowed, account.external_value_borrowed, self.config,
            )
            if total_borrowed < self.config.SMALL_POSITION_THRESHOLD:
                self.recently_posted_low_value[account.address] = time.time()
        except Exception as ex:
            logger.error(
                "AccountMonitor: Failed to post low health notification for %s: %s",
                address, ex, exc_info=True,
            )

    def _handle_liquidation(self, account, address: str, can_liquidate: bool, externally_liquidated: bool) -> None:
        """Simulate liquidation, execute if profitable, and post notifications."""
        try:
            (result, liquidation_data, params) = account.simulate_liquidation()
        except Exception as sim_error:
            logger.error("simulate_liquidation failed for %s: %s", address, sim_error, exc_info=True)
            return

        if not ((result and can_liquidate) or externally_liquidated):
            logger.info("AccountMonitor: %s is unhealthy but not profitable to liquidate.", address)
            return

        if self.notify:
            try:
                post_liquidation_opportunity_notification(address, liquidation_data, params, self.config)
            except Exception as ex:
                logger.error("Failed to post liquidation notification for %s: %s", address, ex, exc_info=True)

        if self.execute_liquidation:
            try:
                liq_tx_hash, liq_tx_receipt = BaseLiquidator.execute_liquidation(
                    liquidation_data["tx"], self.config
                )

                if liq_tx_hash and liq_tx_receipt:
                    logger.info("AccountMonitor: %s liquidated on collateral %s.",
                                address, liquidation_data["collateral_address"])
                    if self.notify:
                        try:
                            post_liquidation_result_notification(address, liquidation_data, liq_tx_hash, self.config)
                        except Exception as ex:
                            logger.error("Failed to post liquidation result for %s: %s", address, ex, exc_info=True)

                account.update_liquidity()
            except Exception as ex:
                logger.error("Failed to execute liquidation for %s: %s", address, ex, exc_info=True)

    def _track_failed_initialization(self, address: str, protocol: str) -> None:
        """Track a failed vault initialization for later retry."""
        current_time = time.time()

        if address in self.failed_initializations:
            # Increment attempts and use exponential backoff
            entry = self.failed_initializations[address]
            entry["attempts"] += 1
            # Exponential backoff: 1min, 2min, 4min, 8min, ... capped at 1 hour
            backoff = min(60 * (2 ** (entry["attempts"] - 1)), 3600)
            entry["retry_at"] = current_time + backoff
            logger.warning(
                "AccountMonitor: Vault %s failed initialization (attempt %s), will retry in %s seconds at %s",
                address, entry["attempts"], backoff,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry["retry_at"])),
            )
        else:
            # First failure - retry in 60 seconds
            self.failed_initializations[address] = {
                "protocol": protocol,
                "retry_at": current_time + 60,
                "attempts": 1,
            }
            logger.warning(
                "AccountMonitor: Vault %s failed initialization, will retry in 60 seconds at %s",
                address,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_time + 60)),
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

        # Find addresses due for retry
        for address, entry in self.failed_initializations.items():
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
                if address in self.accounts and address not in self.failed_initializations:
                    success_count += 1

            except Exception as ex:
                logger.error(
                    "AccountMonitor: Unexpected error retrying initialization for %s: %s",
                    address, ex, exc_info=True,
                )

        if success_count > 0:
            logger.info(
                "AccountMonitor: Successfully initialized %s/%s previously failed vaults",
                success_count, len(addresses_to_retry),
            )

        return success_count

    def periodic_retry_failed_initializations(self) -> None:
        """
        Periodically retry initialization of failed vaults.
        Should be run in a standalone thread.
        Runs every 5 minutes.
        """
        retry_interval = 300  # 5 minutes
        while self.running:
            time.sleep(retry_interval)
            try:
                if self.failed_initializations:
                    self.retry_failed_initializations()
            except Exception as ex:
                logger.error(
                    "AccountMonitor: Error during failed initialization retry: %s",
                    ex, exc_info=True,
                )

    def save_state(self, local_save: bool = True) -> None:
        try:
            state = {
                "version": 1,
                "accounts": {address: account.to_dict() for address, account in self.accounts.items()},
                "queue": list(self.update_queue.queue),
                "last_saved_block": self.latest_block,
                "failed_initializations": self.failed_initializations,
            }

            if local_save:
                with open(self.config.SAVE_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(state, f)

            self.last_saved_block = self.latest_block

            logger.info(
                "AccountMonitor: State saved at time %s up to block %s",
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), self.latest_block,
            )
        except Exception as ex:
            logger.error("AccountMonitor: Failed to save state: %s", ex, exc_info=True)

    def load_state(self, save_path: str, local_save: bool = True) -> None:
        if not local_save:
            return

        if not os.path.exists(save_path):
            logger.info("AccountMonitor: No saved state found.")
            return

        try:
            with open(save_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError) as ex:
            logger.error("AccountMonitor: Corrupt state file, starting fresh: %s", ex)
            return

        state_version = state.get("version")
        if state_version != 1:
            logger.warning("AccountMonitor: State version mismatch (got %s, expected 1)", state_version)

        try:
            self.accounts = {}
            for address, data in state["accounts"].items():
                protocol = data.get("protocol", "euler")
                vault_class = get_vault_class_for_protocol(protocol)
                self.accounts[address] = vault_class.from_dict(data, self.config)

            logger.info("Loaded %s accounts:", len(self.accounts))

            for address, account in self.accounts.items():
                logger.info(
                    "Account %s (%s), Internal Health Score: %s, External Health Score: %s, Next Update: %s",
                    address, account.protocol, account.internal_health_score, account.external_health_score,
                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(account.time_of_next_update)),
                )

            self.rebuild_queue()

            self.last_saved_block = state["last_saved_block"]
            self.latest_block = self.last_saved_block

            # Load failed initializations if present
            self.failed_initializations = state.get("failed_initializations", {})
            if self.failed_initializations:
                logger.info(
                    "AccountMonitor: Loaded %s failed initializations for retry",
                    len(self.failed_initializations),
                )

            logger.info(
                "AccountMonitor: State loaded from save file %s from block %s to block %s",
                save_path, self.config.CVAULT_FACTORY_DEPLOYMENT_BLOCK, self.latest_block,
            )
        except Exception as ex:
            logger.error("AccountMonitor: Failed to load state: %s", ex, exc_info=True)

    def rebuild_queue(self):
        logger.info("Rebuilding queue based on current account health")

        self.update_queue = queue.PriorityQueue()
        for address, account in self.accounts.items():
            try:
                [internal_health_score, external_health_score, externally_liquidated] = account.update_liquidity()

                next_update_time = account.time_of_next_update
                self.update_queue.put((next_update_time, address))

                if account.internal_health_score == math.inf and account.external_health_score == math.inf:
                    logger.info(
                        "AccountMonitor: %s has no borrow, scheduled for max-interval check at %s",
                        address, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
                    )
                else:
                    logger.info(
                        "AccountMonitor: %s added to queue with inHF=%s, exHF=%s, extLiq=%s, next update at %s",
                        address, internal_health_score, external_health_score, externally_liquidated,
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_update_time)),
                    )
            except Exception as ex:
                logger.error(
                    "AccountMonitor: Failed to put account %s into rebuilt queue: %s", address, ex, exc_info=True
                )
                # Schedule retry to prevent account from being stuck with stale timestamp
                retry_delay = 60  # Retry in 60 seconds
                account.time_of_next_update = time.time() + retry_delay
                self.update_queue.put((account.time_of_next_update, address))
                logger.info(
                    "AccountMonitor: Scheduling retry for failed account %s in %s seconds",
                    address, retry_delay,
                )

        logger.info("AccountMonitor: Queue rebuilt with %s accounts", self.update_queue.qsize())

    def get_accounts_by_health_score(self):
        sorted_accounts = sorted(
            self.accounts.values(),
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
            )
            for account in sorted_accounts
        ]

    def periodic_report_low_health_accounts(self):
        while self.running:
            try:
                sorted_accounts = self.get_accounts_by_health_score()
                post_low_health_account_report_notification(sorted_accounts, self.config)
                time.sleep(self.config.LOW_HEALTH_REPORT_INTERVAL)
            except Exception as ex:
                logger.error("AccountMonitor: Failed to post low health account report: %s", ex, exc_info=True)

    @staticmethod
    def create_from_save_state(
        chain_id: int, config: ChainConfig, save_path: str, local_save: bool = True
    ) -> "AccountMonitor":
        monitor = AccountMonitor(chain_id=chain_id, config=config)
        monitor.load_state(save_path, local_save)
        return monitor

    def periodic_save(self) -> None:
        while self.running:
            time.sleep(self.config.SAVE_INTERVAL)
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

        for address, account in self.accounts.items():
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

        return stale_count

    def periodic_sweep_stale_accounts(self) -> None:
        """
        Periodically sweep for stale accounts and re-queue them.
        Should be run in a standalone thread.
        Runs every hour as a defense-in-depth mechanism.
        """
        sweep_interval = 3600  # 1 hour
        while self.running:
            time.sleep(sweep_interval)
            try:
                self.sweep_stale_accounts()
            except Exception as ex:
                logger.error("AccountMonitor: Error during stale account sweep: %s", ex, exc_info=True)

    def stop(self) -> None:
        self.running = False
        with self.condition:
            self.condition.notify_all()
        self.executor.shutdown(wait=True)
        self.save_state()
