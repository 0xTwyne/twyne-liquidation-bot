"""
Factory event listener.
Scans T_CollateralVaultCreated events from the single CollateralVaultFactory
and detects whether each vault is Euler or Aave using .aToken() probe.
"""

import time

from app.liquidation.account_monitor import AccountMonitor
from app.liquidation.config_loader import ChainConfig
from app.liquidation.logging_config import setup_logger
from app.liquidation.vaults.registry import detect_protocol

logger = setup_logger()


class FactoryListener:
    """
    Listener for T_CollateralVaultCreated events from the single factory contract.
    Detects protocol type per vault and passes it to AccountMonitor.
    """

    def __init__(self, account_monitor: AccountMonitor, config: ChainConfig):
        self.config = config
        self.w3 = config.w3
        self.account_monitor = account_monitor
        self.scanned_blocks = set()

        # Single factory for all protocols
        self.factory_instance = config.collateral_vault_factory
        self.deployment_block = int(config.CVAULT_FACTORY_DEPLOYMENT_BLOCK)

    def start_event_monitoring(self) -> None:
        while True:
            try:
                current_block = self.w3.eth.block_number - 1
                if self.account_monitor.latest_block < current_block:
                    self.scan_block_range(self.account_monitor.latest_block, current_block)
            except Exception as ex:
                logger.error("FactoryListener: Unexpected exception in event monitoring: %s", ex, exc_info=True)

            time.sleep(self.config.SCAN_INTERVAL)

    def scan_block_range(
        self,
        start_block: int,
        end_block: int,
        max_retries: int = 3,
        seen_accounts: set = None,
        startup_mode: bool = False,
    ) -> None:
        if seen_accounts is None:
            seen_accounts = set()

        for attempt in range(max_retries):
            try:
                logger.info(
                    "FactoryListener: Scanning blocks %s to %s for T_CollateralVaultCreated events.",
                    start_block, end_block,
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
                        account_address, protocol,
                    )

                    try:
                        self.account_monitor.update_account_on_status_check_event(account_address, protocol)
                    except Exception as ex:
                        logger.error(
                            "FactoryListener: Exception updating account %s (%s): %s",
                            account_address, protocol, ex, exc_info=True,
                        )

                logger.info(
                    "FactoryListener: Finished scanning blocks %s to %s.",
                    start_block, end_block,
                )

                self.account_monitor.latest_block = end_block
                return
            except Exception as ex:
                logger.error(
                    "FactoryListener: Exception scanning block range %s to %s (attempt %s/%s): %s",
                    start_block, end_block, attempt + 1, max_retries, ex, exc_info=True,
                )
                if attempt == max_retries - 1:
                    logger.error(
                        "FactoryListener: Failed to scan block range %s to %s after %s attempts",
                        start_block, end_block, max_retries, exc_info=True,
                    )
                else:
                    time.sleep(self.config.RETRY_DELAY)

    def batch_account_logs_on_startup(self) -> None:
        try:
            start_block = max(self.deployment_block, self.account_monitor.last_saved_block)

            current_block = self.w3.eth.block_number
            batch_block_size = self.config.BATCH_SIZE

            logger.info(
                "FactoryListener: Starting batch scan from block %s to %s.",
                start_block, current_block,
            )

            seen_accounts = set()

            while start_block < current_block:
                end_block = min(start_block + batch_block_size, current_block)

                self.scan_block_range(start_block, end_block, seen_accounts=seen_accounts, startup_mode=True)
                self.account_monitor.save_state()

                start_block = end_block + 1
                time.sleep(self.config.BATCH_INTERVAL)

            logger.info(
                "FactoryListener: Finished batch scan from block %s to %s.",
                start_block, current_block,
            )

        except Exception as ex:
            logger.error(
                "FactoryListener: Unexpected exception in batch scanning on startup: %s", ex, exc_info=True
            )

    @staticmethod
    def get_account_owner_and_subaccount_number(account, config):
        evc = config.evc
        owner = evc.functions.getAccountOwner(account).call()
        if owner == "0x0000000000000000000000000000000000000000":
            owner = account
        subaccount_number = int(int(account, 16) ^ int(owner, 16))
        return owner, subaccount_number
