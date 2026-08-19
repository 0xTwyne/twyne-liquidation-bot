"""
Factory event listener.
Scans T_CollateralVaultCreated events from the single CollateralVaultFactory
and detects whether each vault is Euler or Aave using .aToken() probe.
"""

import os
import time
from datetime import datetime, timezone

from app.liquidation.account_monitor import AccountMonitor
from app.liquidation.config_loader import ChainConfig
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import post_error_notification
from app.liquidation.vaults.registry import detect_protocol

logger = setup_logger()

# How often to re-alert while the scanner is stuck on the same cursor: page on the
# first failure, then every Nth consecutive failure thereafter (avoids alert spam
# while still keeping a persistent stall loud).
_SCAN_FAILURE_ALERT_INTERVAL = int(os.environ.get("LIQ_SCAN_FAILURE_ALERT_INTERVAL", "5"))


class FactoryListener:
    """
    Listener for T_CollateralVaultCreated events from the single factory contract.
    Detects protocol type per vault and passes it to AccountMonitor.
    """

    def __init__(self, account_monitor: AccountMonitor, config: ChainConfig):
        self.config = config
        self.w3 = config.w3
        self.account_monitor = account_monitor

        # Single factory for all protocols
        self.factory_instance = config.collateral_vault_factory
        self.deployment_block = int(config.CVAULT_FACTORY_DEPLOYMENT_BLOCK)

        # Cleared by stop() so the start_event_monitoring loop exits promptly on
        # graceful shutdown instead of running forever.
        self.running = True

    def _alert(self, message: str) -> None:
        """Best-effort operator alert. A notification-backend failure must never
        escalate into the scan loop, so swallow and log any error here."""
        try:
            post_error_notification(message, self.config)
        except Exception as ex:  # noqa: BLE001 - notifications are best-effort
            logger.warning("FactoryListener: failed to post alert notification: %s", ex)

    def stop(self) -> None:
        """Signal the event-monitoring loop to exit at its next iteration."""
        self.running = False

    def start_event_monitoring(self) -> None:
        consecutive_failures = 0
        while self.running:
            try:
                current_block = self.w3.eth.block_number - 1
                if self.account_monitor.latest_block < current_block:
                    self.scan_block_ranges(self.account_monitor.latest_block + 1, current_block)

                # The scan progressed (or there was nothing new to scan): the scanner
                # is healthy. Liveness is refreshed ONLY on success so that a persistent
                # stall surfaces as unhealthy to the /health endpoint and synthetic
                # monitoring, instead of hiding behind a green check while making no
                # forward progress.
                if consecutive_failures:
                    logger.info(
                        "FactoryListener: scan recovered after %s consecutive failure(s); cursor at %s.",
                        consecutive_failures,
                        self.account_monitor.latest_block,
                    )
                consecutive_failures = 0
                self.account_monitor._last_activity_at = datetime.now(timezone.utc)
            except Exception as ex:
                consecutive_failures += 1
                logger.error(
                    "FactoryListener: event monitoring scan failed (consecutive=%s); cursor held at %s: %s",
                    consecutive_failures,
                    self.account_monitor.latest_block,
                    ex,
                    exc_info=True,
                )
                # Cursor is NOT advanced past the failed range (no discovery gap), but a
                # persistent failure must be loud rather than a silent no-progress loop:
                # alert the operator and skip the liveness refresh so /health goes stale.
                if consecutive_failures == 1 or consecutive_failures % _SCAN_FAILURE_ALERT_INTERVAL == 0:
                    self._alert(
                        f"FactoryListener stuck on chain {getattr(self.config, 'CHAIN_ID', '?')}: "
                        f"{consecutive_failures} consecutive scan failure(s) from block "
                        f"{self.account_monitor.latest_block + 1}. Liquidation discovery is not "
                        f"advancing — investigate the RPC / getLogs range."
                    )

            time.sleep(self.config.SCAN_INTERVAL)

    def scan_block_range(
        self,
        start_block: int,
        end_block: int,
        max_retries: int = 3,
        seen_accounts: set = None,
        startup_mode: bool = False,
    ) -> bool:
        if seen_accounts is None:
            seen_accounts = set()

        for attempt in range(max_retries):
            try:
                logger.info(
                    "FactoryListener: Scanning blocks %s to %s for T_CollateralVaultCreated events.",
                    start_block,
                    end_block,
                )

                logs = self.factory_instance.events.T_CollateralVaultCreated().get_logs(
                    from_block=start_block, to_block=end_block
                )

                for log in logs:
                    account_address = log["args"]["vault"]

                    if account_address in seen_accounts:
                        continue
                    seen_accounts.add(account_address)

                    # Detect protocol by probing .aToken()
                    protocol = detect_protocol(account_address, self.config)

                    logger.info(
                        "FactoryListener: T_CollateralVaultCreated for %s (detected: %s), triggering monitor update.",
                        account_address,
                        protocol,
                    )

                    try:
                        self.account_monitor.update_account_on_status_check_event(account_address, protocol)
                    except Exception as ex:
                        logger.error(
                            "FactoryListener: Exception updating account %s (%s): %s",
                            account_address,
                            protocol,
                            ex,
                            exc_info=True,
                        )

                logger.info(
                    "FactoryListener: Finished scanning blocks %s to %s.",
                    start_block,
                    end_block,
                )

                self.account_monitor.latest_block = end_block
                return True
            except Exception as ex:
                logger.error(
                    "FactoryListener: Exception scanning block range %s to %s (attempt %s/%s): %s",
                    start_block,
                    end_block,
                    attempt + 1,
                    max_retries,
                    ex,
                    exc_info=True,
                )
                if attempt == max_retries - 1:
                    logger.error(
                        "FactoryListener: Failed to scan block range %s to %s after %s attempts",
                        start_block,
                        end_block,
                        max_retries,
                        exc_info=True,
                    )
                    raise
                else:
                    time.sleep(self.config.RETRY_DELAY)

        return False

    def scan_block_ranges(
        self,
        start_block: int,
        end_block: int,
        seen_accounts: set = None,
        startup_mode: bool = False,
        save_after_chunk: bool = False,
        sleep_between_chunks: bool = False,
    ) -> None:
        if start_block > end_block:
            return

        if seen_accounts is None:
            seen_accounts = set()

        while start_block <= end_block:
            chunk_end_block = min(start_block + self.config.BATCH_SIZE, end_block)

            self.scan_block_range(
                start_block,
                chunk_end_block,
                seen_accounts=seen_accounts,
                startup_mode=startup_mode,
            )

            if save_after_chunk:
                self.account_monitor.save_state()

            start_block = chunk_end_block + 1

            if sleep_between_chunks and start_block <= end_block:
                time.sleep(self.config.BATCH_INTERVAL)

    def batch_account_logs_on_startup(self) -> None:
        try:
            start_block = max(self.deployment_block, self.account_monitor.last_saved_block)

            current_block = self.w3.eth.block_number

            logger.info(
                "FactoryListener: Starting batch scan from block %s to %s.",
                start_block,
                current_block,
            )

            seen_accounts = set()

            if start_block < current_block:
                self.scan_block_ranges(
                    start_block,
                    current_block,
                    seen_accounts=seen_accounts,
                    startup_mode=True,
                    save_after_chunk=True,
                    sleep_between_chunks=True,
                )

            logger.info(
                "FactoryListener: Finished batch scan from block %s to %s.",
                start_block,
                current_block,
            )

        except Exception as ex:
            # Terminal failure: scan_block_range did NOT advance or save the cursor past
            # the failed range, so no permanent discovery gap is created. Startup is
            # intentionally non-fatal — live monitoring resumes from the un-advanced
            # cursor — but we alert rather than swallow so a persistent startup failure
            # is visible instead of silently proceeding behind a green health check.
            logger.error(
                "FactoryListener: startup batch scan failed; cursor held at %s, live monitoring "
                "will resume from there: %s",
                self.account_monitor.latest_block,
                ex,
                exc_info=True,
            )
            self._alert(
                f"FactoryListener startup scan failed on chain {getattr(self.config, 'CHAIN_ID', '?')}; "
                f"discovery cursor held at block {self.account_monitor.latest_block}. Live monitoring "
                f"will retry from there."
            )
