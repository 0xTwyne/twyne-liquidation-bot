"""
Base classes for multi-protocol collateral vault liquidation.
"""

import math
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from app.liquidation.config_loader import ChainConfig
from app.liquidation.errors import describe_revert
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import post_error_notification

# Retry policy for TimeExhausted on tx submission.
# Total submission budget = 1 original + (MAX_ATTEMPTS - 1) retries at the same nonce.
# Each retry bumps maxFeePerGas + maxPriorityFeePerGas by FEE_BUMP_FACTOR
# (geth/erigon require ≥10% on both fields for replacement).
LIQ_MAX_SUBMIT_ATTEMPTS = int(os.environ.get("LIQ_MAX_SUBMIT_ATTEMPTS", "3"))
LIQ_FEE_BUMP_FACTOR = float(os.environ.get("LIQ_FEE_BUMP_FACTOR", "1.50"))
LIQ_RECEIPT_TIMEOUT_SECS = int(os.environ.get("LIQ_RECEIPT_TIMEOUT_SECS", "60"))

# Number of consecutive HealthStatViewer.health() failures after which a vault moves
# to the explicit `health_unknown` state (DEV-661). One or two failures are transient
# (an RPC hiccup), and the vault keeps its last known health. More than that means the
# bot cannot see the position at all — the usual cause is a lens that reverts for every
# vault after a protocol upgrade. Such a vault must never look healthy.
HEALTH_UNKNOWN_FAILURE_THRESHOLD = int(os.environ.get("HEALTH_UNKNOWN_FAILURE_THRESHOLD", "3"))

logger = setup_logger()

UINT256_MAX = int(2**256 - 1)

# HealthStatViewer.health() returns each health factor scaled by 1e18 (which is why
# get_health_score divides by 1e18 to form the float HFs). HF_ONE is that raw "== 1.0"
# boundary: a position is liquidatable on a given side when its raw HF < HF_ONE. The
# liquidation gate compares these RAW integers (not the /1e18 floats) so float64
# granularity near 1e18 (~128 at that magnitude) can never round a just-under-1 HF up
# to 1.0 and skip a truly-liquidatable position. math.inf (zero-liability / error
# sentinel) compares as NOT liquidatable, exactly as the old `inf < 1` did.
HF_ONE = 10**18


class SignerNonceManager:
    """Thread-safe local nonce allocator for one signer on one chain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_nonce: int | None = None

    def _next(self, config: ChainConfig) -> int:
        if self._next_nonce is None:
            self._next_nonce = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA, "pending")
        nonce = self._next_nonce
        self._next_nonce += 1
        return nonce

    def invalidate(self) -> None:
        with self._lock:
            self._next_nonce = None

    def sign_and_send(self, tx: Dict[str, Any], config: ChainConfig, nonce: int | None = None):
        with self._lock:
            tx_to_send = dict(tx)
            assigned_nonce = self._next(config) if nonce is None else nonce
            tx_to_send["nonce"] = assigned_nonce
            signed_tx = config.w3.eth.account.sign_transaction(tx_to_send, config.LIQUIDATOR_EOA_PRIVATE_KEY)
            tx_hash = config.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            return tx_hash, tx_to_send


class BaseCollateralVault(ABC):
    """
    Abstract base class for collateral vaults across protocols (Euler, Aave, etc.).
    Shared scheduling, health score, serialization logic lives here.
    Protocol-specific contract setup and liquidation logic is abstract.
    """

    protocol: str = ""  # Subclasses must set this

    def __init__(self, address: str, config: ChainConfig, metadata: "dict | None" = None):
        self.config = config
        self.address = Web3.to_checksum_address(address)

        self.time_of_next_update = time.time()
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        # Raw 1e18-scaled health factors (the exact values health() returns), kept
        # alongside the /1e18 floats. The liquidation gate compares these to avoid
        # float64 precision loss near the 1e18 boundary (DEV-554, Option A). Same
        # math.inf sentinel as the floats, so an un-read vault is treated as healthy.
        self.internal_health_score_raw = math.inf
        self.external_health_score_raw = math.inf
        # Explicit "the bot does not know this vault's health" state (DEV-661). The
        # health factors above fall back to math.inf on a failed read, which reads as
        # "healthy" everywhere else in the bot. `health_unknown` separates the two, so
        # a lens that reverts for every vault raises an alert and still opens the
        # on-chain liquidation check instead of showing a healthy fleet.
        self.health_read_failures = 0
        self.health_unknown = False
        self._health_unknown_alerted_at: "float | None" = None
        self.balance = 0
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0

        # Timestamp (epoch seconds) of the most recent completed health
        # re-evaluation. Read by the /internal/observability endpoint so the
        # metrics exporter can detect a wedged monitor queue that the /health
        # liveness probe (refreshed by the block scanner) would miss.
        self.last_checked_at: "float | None" = None

        # Subclass fills these in via _init_protocol_contracts
        self.instance = None
        self.asset_address = None
        self.asset = None
        self.underlying_asset_address = None
        self.underlying_asset_symbol = ""
        self.target_asset = None
        self.liqbot_instance = None
        self.health_state_viewer = None

        # Immutable token metadata (decimals/symbol) cached at init so the read
        # paths (get_position_stats / periodic reports) never re-fetch it via RPC
        # on every call (finding P6). Populated by _init_protocol_contracts.
        self.collateral_decimals = 18
        self.collateral_symbol = ""
        self.debt_decimals = 18
        self.debt_symbol = ""

        # When reconstructing from persisted state (finding P3), immutable vault
        # metadata (asset/target/vault-manager addresses, symbols, decimals) is
        # supplied here so _init_protocol_contracts rebuilds the contract instances
        # LOCALLY (ABI is lru-cached — no disk read, no RPC) instead of issuing the
        # ~13 serial immutable eth_calls a fresh discovery makes. None => live
        # discovery path (unchanged).
        self._restore_metadata = metadata

        self._init_protocol_contracts(config)

        # is_correlated is derived state recomputed every time the vault is
        # instantiated (including state reload), so the cadence policy follows
        # the current config without any explicit migration.
        self.is_correlated = self._compute_correlation()

        if metadata is not None:
            # balanceOf is mutable; the persisted value is the last-known snapshot
            # (the live code only ever sets it at init too). The subsequent
            # rebuild_queue → update_liquidity refresh re-evaluates health, so a
            # restart no longer pays a per-vault balanceOf RPC during reconstruction.
            self.balanceOf = metadata.get("balanceOf", 0)
        else:
            self.balanceOf = self.instance.functions.balanceOf(self.address).call()

    def _compute_correlation(self) -> bool:
        if not self.underlying_asset_address or not self.target_asset:
            return False
        pair = (
            Web3.to_checksum_address(self.underlying_asset_address),
            Web3.to_checksum_address(self.target_asset),
        )
        return pair in self.config.correlated_pairs

    @abstractmethod
    def _init_protocol_contracts(self, config: ChainConfig) -> None:
        """Set up protocol-specific contract instances."""

    @abstractmethod
    def get_collateral_for_borrower(self) -> int:
        """Calculate collateralForBorrower using protocol-specific oracle/pricing."""

    @abstractmethod
    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        """Simulate liquidation and return (profitable, data, params)."""

    def check_liquidation(self, liquidator_address: str) -> Tuple[bool, bool, int, int, int]:
        logger.info("Vault: Checking liquidation for collateral vault %s", self.address)
        try:
            canLiquidate = self.instance.functions.canLiquidate().call()
            externallyLiquidated = self.instance.functions.isExternallyLiquidated().call()
            max_release = self.instance.functions.maxRelease().call()
            max_repay = self.instance.functions.maxRepay().call()
            totalAssets = self.instance.functions.totalAssetsDepositedOrReserved().call()
            return (canLiquidate, externallyLiquidated, max_release, max_repay, totalAssets)
        except Exception as ex:
            logger.error(
                "Vault: Failed to check liquidation status for %s: %s",
                self.address,
                ex,
                exc_info=True,
            )
            # Return safe defaults - will be checked again on next update
            return (False, False, 0, 0, 0)

    def get_liq_params(self) -> Tuple[int, int, int]:
        """Read the liquidation parameters of this vault from the VaultManager.

        Twyne 1.0.7 keys them on (intermediateVault, targetAsset) and returns
        (externalLiqBuffer, maxTwyneLiqLTV, borrowBuffer), all in 1e4 precision. The
        one-argument getters of the earlier VaultManager do not exist any more.
        `intermediate_vault_address` and `target_asset` are immutable per vault and are
        read once at init, so this is a single eth_call.
        """
        buffer_, max_twyne_liq_ltv, borrow_buffer = self.vault_manager.functions.liqParams(
            self.intermediate_vault_address, self.target_asset
        ).call()
        return (buffer_, max_twyne_liq_ltv, borrow_buffer)

    def get_max_twyne_ltv(self) -> int:
        """Effective maxTwyneLiqLTV (1e4) of this vault's (intermediate vault, target asset) pair."""
        return self.get_liq_params()[1]

    def get_health_score(self) -> Tuple[float, float]:
        try:
            externalHF, internalHF, external_liability_value, internal_liability_value = (
                self.health_state_viewer.functions.health(self.address).call()
            )

            self.internal_value_borrowed = internal_liability_value
            self.external_value_borrowed = external_liability_value

            if external_liability_value < 0 or internal_liability_value < 0:
                logger.error(
                    "Vault: %s has negative liability values: internal=%s, external=%s",
                    self.address,
                    internal_liability_value,
                    external_liability_value,
                )
                return (math.inf, math.inf)

            if external_liability_value == 0:
                externalHF = math.inf
            if internal_liability_value == 0:
                internalHF = math.inf

            if externalHF < 0 or internalHF < 0:
                logger.error(
                    "Vault: %s has negative health factors: internal=%s, external=%s",
                    self.address,
                    internalHF,
                    externalHF,
                )
                return (math.inf, math.inf)

            self.internal_health_score = internalHF / 1e18
            self.external_health_score = externalHF / 1e18
            # Raw 1e18-scaled counterparts for the exact-integer liquidation gate.
            # internalHF/externalHF here are either the contract's raw 1e18-scaled int
            # or math.inf (zero-liability branch above) — mirror both exactly. Only the
            # success path updates these; the early-return error paths leave the prior
            # values untouched, identical to how the float attrs behave.
            self.internal_health_score_raw = internalHF
            self.external_health_score_raw = externalHF

            logger.info(
                "Vault: %s, inHF: %s, extHF: %s, internal debt: %s, external debt: %s",
                self.address,
                self.internal_health_score,
                self.external_health_score,
                internal_liability_value,
                external_liability_value,
            )
            if self.internal_health_score < 1 or self.external_health_score < 1:
                logger.info("  +++++=====Vault: %s can be liquidated!", self.address)
            self._mark_health_known()
            return (internalHF, externalHF)
        except Exception as ex:
            logger.error("Vault: Failed to get health score for %s: %s", self.address, ex, exc_info=True)
            self._record_health_failure(ex)
            return (math.inf, math.inf)

    def _mark_health_known(self) -> None:
        """Clear the failure counter after a successful health() read."""
        recovered = self.health_unknown and self._health_unknown_alerted_at is not None
        failures = self.health_read_failures
        self.health_read_failures = 0
        self.health_unknown = False
        self._health_unknown_alerted_at = None
        if recovered:
            logger.warning("Vault: %s health read recovered after %s failures", self.address, failures)
            post_error_notification(
                f"*Health read recovered* for collateral vault `{self.address}` ({self.protocol}) "
                f"after {failures} consecutive failures.",
                self.config,
            )

    def _record_health_failure(self, ex: Exception) -> None:
        """Count a failed health() read and mark the vault health_unknown if they repeat.

        Below the threshold the vault keeps its last known health, which is the old
        behaviour for a transient RPC failure. At the threshold the vault becomes
        health_unknown and the bot alerts. The alert repeats at most once per
        ERROR_COOLDOWN so a fleet-wide lens failure does not flood the channel.
        """
        self.health_read_failures += 1
        if self.health_read_failures < HEALTH_UNKNOWN_FAILURE_THRESHOLD:
            return

        self.health_unknown = True
        now = time.time()
        cooldown = getattr(self.config, "ERROR_COOLDOWN", 900)
        last_alert = self._health_unknown_alerted_at
        if last_alert is not None and now - last_alert < cooldown:
            return

        self._health_unknown_alerted_at = now
        viewer = getattr(self.config, "HEALTHSTATVIEWER_ADDRESS", "unknown")
        post_error_notification(
            f"*Health UNKNOWN* for collateral vault `{self.address}` ({self.protocol}): "
            f"{self.health_read_failures} consecutive HealthStatViewer.health() failures. "
            f"Viewer: `{viewer}`. Last error: {ex}. "
            "This vault is NOT known to be healthy. Check that the HealthStatViewer address "
            "matches the deployed Twyne contract version.",
            self.config,
        )

    def get_position_stats(self):
        """Fetch full position stats from HealthStatViewer.positionStats().

        Issues exactly ONE eth_call (positionStats); token decimals/symbol come
        from the immutable metadata cached at init (finding P6), not from 2-4 extra
        RPC reads per call as before.

        Returns a PositionStats or None on failure (callers fall back to HF-only).
        """
        from app.liquidation.position_stats import PositionStats

        try:
            raw = self.health_state_viewer.functions.positionStats(self.address).call()
        except Exception as ex:
            logger.error("Vault: positionStats failed for %s: %s", self.address, ex, exc_info=True)
            return None

        try:
            return PositionStats.from_raw(
                raw,
                collateral_decimals=self.collateral_decimals,
                collateral_symbol=self.collateral_symbol,
                debt_decimals=self.debt_decimals,
                debt_symbol=self.debt_symbol,
            )
        except Exception as ex:
            logger.error("Vault: PositionStats.from_raw failed for %s: %s", self.address, ex, exc_info=True)
            return None

    def _cache_token_metadata(self) -> None:
        """Populate the immutable collateral/debt decimals+symbol cache (finding P6).

        Called once during live init. ``self.asset`` is the collateral vault's asset
        contract (eVault share token for Euler, aToken wrapper for Aave); the debt
        token is ``self.target_asset`` read through the ERC20 ABI. Both are immutable
        for the life of the vault, so we read them exactly once here instead of on
        every get_position_stats / report tick.
        """
        from app.liquidation.contracts import create_contract_instance

        self.collateral_decimals, self.collateral_symbol = self._safe_token_meta(
            self.asset, default_decimals=18, default_symbol=""
        )
        try:
            debt = create_contract_instance(self.target_asset, self.config.ERC20_ABI_PATH, self.config)
        except Exception:
            debt = None
        self.debt_decimals, self.debt_symbol = self._safe_token_meta(debt, default_decimals=18, default_symbol="")

    def _restore_token_metadata(self, metadata: dict) -> None:
        """Restore the immutable token-metadata cache from persisted state (no RPC)."""
        self.collateral_decimals = metadata.get("collateral_decimals", 18)
        self.collateral_symbol = metadata.get("collateral_symbol", "")
        self.debt_decimals = metadata.get("debt_decimals", 18)
        self.debt_symbol = metadata.get("debt_symbol", "")

    def _build_metadata(self) -> dict:
        """Immutable metadata persisted in the state file for fast reload (finding P3).

        Combines the base token-metadata cache + last-known balanceOf with the
        protocol-specific immutable addresses from ``_protocol_metadata``.
        """
        meta = {
            "balanceOf": self.balanceOf,
            "collateral_decimals": self.collateral_decimals,
            "collateral_symbol": self.collateral_symbol,
            "debt_decimals": self.debt_decimals,
            "debt_symbol": self.debt_symbol,
            "underlying_asset_symbol": self.underlying_asset_symbol,
        }
        meta.update(self._protocol_metadata())
        return meta

    def _protocol_metadata(self) -> dict:
        """Protocol-specific immutable addresses for fast state reload (P3).

        Default is empty (no fast-reload metadata); concrete protocol vaults
        override this. Kept non-abstract so lightweight test subclasses that don't
        persist can still be instantiated.
        """
        return {}

    @staticmethod
    def _safe_token_meta(contract, *, default_decimals, default_symbol):
        decimals, symbol = default_decimals, default_symbol
        if contract is None:
            return decimals, symbol
        try:
            decimals = contract.functions.decimals().call()
        except Exception:
            pass
        try:
            symbol = contract.functions.symbol().call()
        except Exception:
            pass
        return decimals, symbol

    def _read_externally_liquidated(self) -> bool:
        """Single on-chain read of the externally-liquidated flag, error-safe.

        Centralised so a tick reads ``isExternallyLiquidated()`` exactly ONCE and
        threads the result, instead of the previous 3-4 redundant reads per cycle
        (finding P2/P6)."""
        try:
            return self.instance.functions.isExternallyLiquidated().call()
        except Exception as ex:
            logger.error(
                "Vault: Failed to check isExternallyLiquidated for %s: %s",
                self.address,
                ex,
                exc_info=True,
            )
            return False  # Safe default - will be checked again on next update

    def update_liquidity(self) -> Tuple[float, float, bool]:
        self.get_health_score()
        # Read the externally-liquidated flag ONCE and thread it into the cadence
        # computation, instead of re-reading it 2-3 more times (finding P2).
        externallyLiquidated = self._read_externally_liquidated()
        self.get_time_of_next_update(externally_liquidated=externallyLiquidated)
        # Stamp liveness: a full health re-evaluation just completed for this CV.
        self.last_checked_at = time.time()
        return (self.internal_health_score, self.external_health_score, externallyLiquidated)

    def get_time_of_next_update(self, externally_liquidated: "bool | None" = None) -> float:
        max_interval = self.config.MAX_UPDATE_INTERVAL_SECONDS

        # The caller (update_liquidity) reads isExternallyLiquidated once and threads
        # it in. When called directly (e.g. unit tests / ad-hoc rescheduling) with no
        # value, fall back to a single on-chain read here.
        if externally_liquidated is None:
            externally_liquidated = self._read_externally_liquidated()

        # Empty vaults (no position) get checked at max interval, unless they
        # are externally liquidated and still need handling.
        if (
            self.internal_health_score == math.inf
            and self.external_health_score == math.inf
            and not externally_liquidated
        ):
            self.time_of_next_update = time.time() + max_interval * random.uniform(0.9, 1.1)
            return self.time_of_next_update

        bucket_fn = self._bucket_correlated if self.is_correlated else self._bucket_uncorrelated
        time_gap_internal = bucket_fn(self.internal_health_score, externally_liquidated)
        time_gap_external = bucket_fn(self.external_health_score, externally_liquidated)
        time_gap = min(time_gap_internal, time_gap_external, max_interval)

        time_of_next_update = time.time() + time_gap * random.uniform(0.9, 1.1)

        if not (self.time_of_next_update < time_of_next_update and self.time_of_next_update > time.time()):
            self.time_of_next_update = time_of_next_update

        return self.time_of_next_update

    def _bucket_correlated(self, hf: float, externally_liquidated: bool) -> int:
        """Cadence step function for correlated (collateral, debt) pairs."""
        c = self.config.cadence_correlated
        if externally_liquidated or hf <= self.config.HS_LIQUIDATION:
            return c["LIQ"]
        if hf < self.config.HS_CORRELATED_HIGH:
            return c["HIGH"]
        return c["SAFE"]

    def _bucket_uncorrelated(self, hf: float, externally_liquidated: bool) -> int:
        """Cadence step function for all other pairs."""
        c = self.config.cadence_uncorrelated
        if externally_liquidated or hf < self.config.HS_CORRELATED_HIGH:
            return c["LIQ"]
        if hf < self.config.HS_HIGH_RISK:
            return c["HIGH_1"]
        if hf < self.config.HS_MEDIUM_RISK:
            return c["HIGH_2"]
        if hf < self.config.HS_SAFE:
            return c["MEDIUM"]
        return c["SAFE"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "protocol": self.protocol,
            "time_of_next_update": self.time_of_next_update,
            "internal_health_score": self.internal_health_score,
            "external_health_score": self.external_health_score,
            # True while repeated health() reads fail. Surfaced through the API so an
            # operator sees "unknown" instead of a health score that is really a
            # fallback value (DEV-661). Not restored by from_dict: the first tick
            # after a restart re-evaluates it.
            "health_unknown": self.health_unknown,
            # Derived state — persisted for offline tooling (pair_audit) and
            # surfacing through the API. Recomputed every time the vault is
            # reconstructed via from_dict, so a stale persisted value cannot
            # cause the bot to schedule on the wrong policy.
            "is_correlated": self.is_correlated,
            "pair_collateral": self.underlying_asset_address,
            "pair_debt": self.target_asset,
            # Immutable vault metadata so a restart can reconstruct the contract
            # instances locally without the ~13 serial immutable eth_calls per vault
            # (finding P3). Read back by from_dict on the fast-reload path.
            "metadata": self._build_metadata(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], config: ChainConfig) -> "BaseCollateralVault":
        # Pass persisted immutable metadata (if present) so the constructor takes the
        # no-RPC fast path; absent (legacy state files) => full live discovery.
        account = cls(address=data["address"], config=config, metadata=data.get("metadata"))
        account.time_of_next_update = data["time_of_next_update"]
        account.internal_health_score = data["internal_health_score"]
        account.external_health_score = data["external_health_score"]
        return account


class BaseLiquidator(ABC):
    """Base class for protocol-specific liquidators."""

    @staticmethod
    def _nonce_manager(config: ChainConfig) -> SignerNonceManager:
        manager = getattr(config, "_liquidator_nonce_manager", None)
        if manager is None:
            manager = SignerNonceManager()
            setattr(config, "_liquidator_nonce_manager", manager)
        return manager

    @staticmethod
    def sign_and_send_transaction(tx: Dict[str, Any], config: ChainConfig, nonce: int | None = None):
        nonce_manager = BaseLiquidator._nonce_manager(config)
        try:
            return nonce_manager.sign_and_send(tx, config, nonce)
        except Exception:
            nonce_manager.invalidate()
            raise

    @staticmethod
    def execute_liquidation(liquidation_transaction: Dict[str, Any], config: ChainConfig):
        """
        Sign, broadcast, and wait for a liquidation transaction.

        On TimeExhausted, bumps both EIP-1559 fee fields by LIQ_FEE_BUMP_FACTOR and
        retries at the same nonce (replacement) up to LIQ_MAX_SUBMIT_ATTEMPTS total
        attempts. Returns (tx_hash_hex, receipt) on success or (None, None) on
        terminal failure / unrecoverable error.
        """
        tx = dict(liquidation_transaction)  # mutable copy; only fee fields change across attempts
        tx.pop("nonce", None)
        nonce = None
        last_hash = None
        # Track every submitted hash so we can find a mined tx even if it wasn't the
        # last replacement attempt (an earlier bump may have landed before we timed out).
        submitted_hashes: list = []

        logger.info("Liquidator: Executing liquidation transaction %s...", liquidation_transaction)

        def _find_mined_hash(hashes: list):
            """Return (hash_hex, receipt) for the first hash in *hashes* that has a
            receipt, or (None, None) if none are found."""
            for h in hashes:
                try:
                    rcpt = config.w3.eth.get_transaction_receipt(h)
                    logger.info(
                        "Liquidator: found mined receipt for %s status=%s",
                        h.hex(),
                        rcpt["status"],
                    )
                    return h.hex(), rcpt
                except TransactionNotFound:
                    pass
            return None, None

        for attempt in range(LIQ_MAX_SUBMIT_ATTEMPTS):
            try:
                last_hash, signed_tx_payload = BaseLiquidator.sign_and_send_transaction(tx, config, nonce)
                submitted_hashes.append(last_hash)
                nonce = signed_tx_payload["nonce"]
                tx["nonce"] = nonce
                logger.info(
                    "Liquidator: submit attempt=%d/%d hash=%s nonce=%d maxFee=%s tip=%s",
                    attempt + 1,
                    LIQ_MAX_SUBMIT_ATTEMPTS,
                    last_hash.hex(),
                    nonce,
                    tx.get("maxFeePerGas"),
                    tx.get("maxPriorityFeePerGas"),
                )
                # Poll for the receipt immediately with a tight interval instead of a
                # fixed 5s pre-sleep (finding P10): on fast inclusion this returns in well
                # under a second rather than always burning 5s before the first poll, on
                # the latency-critical quote→broadcast→confirm path.
                tx_receipt = config.w3.eth.wait_for_transaction_receipt(
                    last_hash, timeout=LIQ_RECEIPT_TIMEOUT_SECS, poll_latency=0.5
                )
                logger.info(
                    "Liquidator: mined block=%s status=%s gasUsed=%s effectiveGasPrice=%s",
                    tx_receipt["blockNumber"],
                    tx_receipt["status"],
                    tx_receipt["gasUsed"],
                    tx_receipt.get("effectiveGasPrice"),
                )
                return last_hash.hex(), tx_receipt

            except TimeExhausted:
                # Did a tx at our nonce already mine while we were waiting?
                mined_count = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA, "latest")
                if nonce is not None and mined_count > nonce:
                    # An earlier attempt may have mined rather than the last one;
                    # scan all submitted hashes so we don't mis-report a success as failure.
                    h_hex, rcpt = _find_mined_hash(submitted_hashes)
                    if h_hex is not None:
                        return h_hex, rcpt
                    logger.warning(
                        "Liquidator: nonce %d mined but none of our %d submitted hashes found",
                        nonce,
                        len(submitted_hashes),
                    )
                    return None, None

                if attempt + 1 >= LIQ_MAX_SUBMIT_ATTEMPTS:
                    msg = (
                        f"TimeExhausted after {LIQ_MAX_SUBMIT_ATTEMPTS} attempts, "
                        f"nonce={nonce}, last_hash={last_hash.hex() if last_hash else None}"
                    )
                    logger.error(msg)
                    post_error_notification(msg, config)
                    return None, None

                # Bump both 1559 fee fields for replacement.
                new_tip = int(tx["maxPriorityFeePerGas"] * LIQ_FEE_BUMP_FACTOR)
                new_max = int(tx["maxFeePerGas"] * LIQ_FEE_BUMP_FACTOR)
                logger.warning(
                    "Liquidator: TimeExhausted attempt=%d, retrying nonce=%d with maxFee=%s tip=%s",
                    attempt + 1,
                    nonce,
                    new_max,
                    new_tip,
                )
                tx["maxPriorityFeePerGas"] = new_tip
                tx["maxFeePerGas"] = new_max

            except ValueError as ex:
                # send_raw rejection: nonce too low means a prior attempt already mined.
                # Scan all submitted hashes before declaring failure.
                logger.error(
                    "Liquidator: send_raw rejected attempt=%d: %s (last_hash=%s)",
                    attempt + 1,
                    ex,
                    last_hash.hex() if last_hash else None,
                )
                if submitted_hashes:
                    h_hex, rcpt = _find_mined_hash(submitted_hashes)
                    if h_hex is not None:
                        return h_hex, rcpt
                return None, None
            except Exception as ex:
                message = f"Unexpected error in executing liquidation: {ex}{describe_revert(ex)}"
                logger.error(message, exc_info=True)
                # The send may have broadcast at `nonce` before this error (e.g. a
                # receipt-wait connection drop). Force a chain re-seed on the next
                # send so the local counter can't strand subsequent nonces behind a
                # dropped tx. invalidate() is idempotent and re-seeds from "pending".
                BaseLiquidator._nonce_manager(config).invalidate()
                post_error_notification(message, config)
                return None, None

        return None, None

    @staticmethod
    @abstractmethod
    def calculate_liquidation_profit(vault, config: ChainConfig):
        """Calculate liquidation profit for a vault. Returns (profit_data, params)."""

    @staticmethod
    @abstractmethod
    def simulate_liquidation(vault, config: ChainConfig):
        """Simulate liquidation. Returns (bool, data, params)."""
