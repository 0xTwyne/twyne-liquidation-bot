import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import sentry_sdk
from web3 import Web3

from .account_monitor import AccountMonitor
from .config_loader import ChainConfig, load_chain_config
from .event_listener import FactoryListener
from .logging_config import setup_logger
from .notifications import post_error_notification

logger = setup_logger()


class ChainManager:
    """Manages multiple chain instances of the liquidation bot"""

    def __init__(self, chain_ids: List[int], notify: bool = True, execute_liquidation: bool = True):
        self.chain_ids = chain_ids
        self.notify = notify
        self.execute_liquidation = execute_liquidation

        # Initialize configs, monitors, and listeners for each chain
        self.configs: Dict[int, ChainConfig] = {}
        self.monitors: Dict[int, AccountMonitor] = {}
        self.listeners: Dict[int, FactoryListener] = {}
        self.web3s: Dict[int, Web3] = {}

        self._initialize_chains()

    def _initialize_chains(self):
        """Initialize components for each chain"""
        logger.info("Initializing chains: %s", self.chain_ids)
        for chain_id in self.chain_ids:
            # Load chain-specific config
            config = load_chain_config(chain_id)
            self.configs[chain_id] = config

            # Create monitor instance
            monitor = AccountMonitor(
                chain_id=chain_id, config=config, notify=self.notify, execute_liquidation=self.execute_liquidation
            )
            monitor.load_state(config.SAVE_STATE_PATH)
            self.monitors[chain_id] = monitor

            # Create listener instance (now scans all protocol factories)
            listener = FactoryListener(monitor, config)
            self.listeners[chain_id] = listener

    def start(self):
        """Start all chain monitors and listeners"""
        with ThreadPoolExecutor() as executor:
            # Run startup historical scans for ALL chains concurrently (P9 / DEV-555).
            # Previously the scans ran serially: chain N+1's book was unmonitored
            # while chain N rescanned.  With DEV-549 making the chain list
            # configurable, this becomes meaningful as soon as Base is enabled.
            startup_futures = [executor.submit(self._run_startup_scan, chain_id) for chain_id in self.chain_ids]
            for future in startup_futures:
                try:
                    future.result()
                except Exception as e:
                    logger.error("Chain startup scan failed: %s", e, exc_info=True)

            # Start monitors — track which chain each future belongs to so we can
            # send a targeted alert when one dies.
            future_to_chain: Dict = {}
            monitor_futures = []
            for chain_id in self.chain_ids:
                f = executor.submit(self._run_monitor, chain_id)
                future_to_chain[f] = chain_id
                monitor_futures.append(f)

            listener_futures = []
            for chain_id in self.chain_ids:
                f = executor.submit(self._run_listener, chain_id)
                future_to_chain[f] = chain_id
                listener_futures.append(f)

            # Wait for all to complete (they shouldn't unless there's an error).
            #
            # Design decision — crash on irrecoverable death:
            # A dead monitor/listener means liquidations silently stop for that
            # chain.  Keeping a zombie process alive (serving HTTP 200 while doing
            # nothing) is worse than a hard exit that docker restart:unless-stopped
            # immediately revives.  We therefore alert loudly (Slack + Sentry) and
            # then call os._exit(1) so the container manager gets a clean restart
            # rather than a stale process.
            for future in monitor_futures + listener_futures:
                try:
                    future.result()
                except Exception as e:
                    chain_id = future_to_chain.get(future)
                    config = self.configs.get(chain_id) if chain_id is not None else None
                    logger.error("Chain instance failed (chain=%s): %s", chain_id, e, exc_info=True)
                    try:
                        post_error_notification(
                            f"FATAL: chain {chain_id} monitor/listener thread died unexpectedly: {e}\n"
                            f"The process will now exit so docker restart:unless-stopped can revive it.",
                            config,
                        )
                    except Exception as notify_err:  # noqa: BLE001
                        logger.error("Could not post crash notification: %s", notify_err)
                    try:
                        sentry_sdk.capture_exception(e)
                    except Exception as sentry_err:  # noqa: BLE001
                        logger.error("Could not capture exception in Sentry: %s", sentry_err)
                    os._exit(1)

    def _run_startup_scan(self, chain_id: int):
        """Run a single chain's startup historical scan (P9 / DEV-555)."""
        self.listeners[chain_id].batch_account_logs_on_startup()

    def _run_monitor(self, chain_id: int):
        """Run a single chain's monitor"""
        monitor = self.monitors[chain_id]
        monitor.start_queue_monitoring()

    def _run_listener(self, chain_id: int):
        """Run a single chain's listener"""
        listener = self.listeners[chain_id]
        listener.start_event_monitoring()

    def stop(self):
        """Stop all chain instances in a shutdown-safe order.

        Listeners are stopped FIRST so no new accounts are discovered / enqueued
        while we are tearing the monitors down. Then each monitor.stop() drains
        its in-flight workers and runs a FINAL save_state() only after all
        mutators have stopped (see AccountMonitor.stop), so the persisted snapshot
        is consistent.
        """
        logger.info("ChainManager: stopping listeners.")
        for listener in self.listeners.values():
            try:
                listener.stop()
            except Exception as ex:
                logger.error("ChainManager: error stopping listener: %s", ex, exc_info=True)

        logger.info("ChainManager: stopping monitors (drains workers, then final save).")
        for monitor in self.monitors.values():
            try:
                monitor.stop()
            except Exception as ex:
                logger.error("ChainManager: error stopping monitor: %s", ex, exc_info=True)
        logger.info("ChainManager: shutdown complete.")
