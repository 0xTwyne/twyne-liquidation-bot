"""
Euler protocol collateral vault and liquidator.
"""

import time
from typing import Any, Dict, Optional, Tuple

from web3.exceptions import BlockNotFound, ContractLogicError

from app.liquidation.config_loader import ChainConfig
from app.liquidation.constants import GAS_ESTIMATE_BUFFER, LTV_MAXFACTOR, SWAP_MARGIN_DIVISOR
from app.liquidation.contracts import create_contract_instance
from app.liquidation.exceptions import TransactionBuildError
from app.liquidation.gas import get_eip1559_fees
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import post_error_notification
from app.liquidation.profitability import external_release_and_c_new, min_net_profit_usd_raw, value_in_usd
from app.liquidation.swap_provider import make_swap_provider
from app.liquidation.vaults.base_vault import BaseCollateralVault, BaseLiquidator

logger = setup_logger()

liquidation_error_slack_cooldown = {}

# Minimum profit (in target-asset units) required by TwyneLiquidator on the internal
# liquidation path. Used both as the on-chain `minProfit` argument and as the floor
# the WETH->target swap must clear above the flashloan repayment, so the two never
# disagree. Kept at 1 (effectively "any non-negative profit") on purpose — we want to
# liquidate unhealthy positions even at a thin margin; gas cost is accepted.
MIN_LIQUIDATION_PROFIT = 1


class EulerCollateralVault(BaseCollateralVault):
    """Collateral vault implementation for the Euler protocol."""

    protocol = "euler"

    def _init_protocol_contracts(self, config: ChainConfig) -> None:
        if self._restore_metadata is not None:
            # Fast state-reload path (finding P3): rebuild contract instances locally
            # from persisted immutable addresses. create_contract_instance issues NO
            # eth_call (ABI is lru-cached locally), so this is ~13 fewer serial RPCs
            # per vault on every restart.
            self._init_protocol_contracts_from_metadata(config, self._restore_metadata)
            return

        self.instance = create_contract_instance(self.address, config.EULER_CVAULT_ABI_PATH, config)
        self.health_state_viewer = create_contract_instance(
            config.HEALTHSTATVIEWER_ADDRESS, config.HEALTHSTATVIEWER_ABI_PATH, config
        )

        self.asset_address = self.instance.functions.asset().call()
        self.asset = create_contract_instance(self.asset_address, config.EVAULT_ABI_PATH, config)
        self.underlying_asset_address = self.asset.functions.asset().call()
        self.underlying_asset_symbol = self.asset.functions.symbol().call()

        self.target_asset = self.instance.functions.targetAsset().call()
        self.target_vault_address = self.instance.functions.targetVault().call()
        self.target_vault = create_contract_instance(self.target_vault_address, config.EVAULT_ABI_PATH, config)

        self.intermediate_vault_address = self.instance.functions.intermediateVault().call()
        self.intermediate_vault = create_contract_instance(
            self.intermediate_vault_address, config.EVAULT_ABI_PATH, config
        )
        self.unit_of_account = self.intermediate_vault.functions.unitOfAccount().call()

        self.vault_manager_address = self.instance.functions.twyneVaultManager().call()
        self.vault_manager = create_contract_instance(self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config)
        self.oracle_router_address = self.vault_manager.functions.oracleRouter().call()
        self.oracle_router = create_contract_instance(self.oracle_router_address, config.EULER_ROUTER_ABI_PATH, config)

        self.vault_name = self.instance.functions.name().call()
        self.vault_symbol = self.instance.functions.symbol().call()

        self.liqbot_instance = config.euler_liqbot

        # Cache immutable token decimals/symbol once (finding P6).
        self._cache_token_metadata()

    def _init_protocol_contracts_from_metadata(self, config: ChainConfig, meta: dict) -> None:
        """Reconstruct contract instances from persisted immutable metadata (P3, no RPC)."""
        self.instance = create_contract_instance(self.address, config.EULER_CVAULT_ABI_PATH, config)
        self.health_state_viewer = create_contract_instance(
            config.HEALTHSTATVIEWER_ADDRESS, config.HEALTHSTATVIEWER_ABI_PATH, config
        )

        self.asset_address = meta["asset_address"]
        self.asset = create_contract_instance(self.asset_address, config.EVAULT_ABI_PATH, config)
        self.underlying_asset_address = meta["underlying_asset_address"]
        self.underlying_asset_symbol = meta.get("underlying_asset_symbol", "")

        self.target_asset = meta["target_asset"]
        self.target_vault_address = meta["target_vault_address"]
        self.target_vault = create_contract_instance(self.target_vault_address, config.EVAULT_ABI_PATH, config)

        self.intermediate_vault_address = meta["intermediate_vault_address"]
        self.intermediate_vault = create_contract_instance(
            self.intermediate_vault_address, config.EVAULT_ABI_PATH, config
        )
        self.unit_of_account = meta["unit_of_account"]

        self.vault_manager_address = meta["vault_manager_address"]
        self.vault_manager = create_contract_instance(self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config)
        self.oracle_router_address = meta["oracle_router_address"]
        self.oracle_router = create_contract_instance(self.oracle_router_address, config.EULER_ROUTER_ABI_PATH, config)

        self.vault_name = meta.get("vault_name", "")
        self.vault_symbol = meta.get("vault_symbol", "")

        self.liqbot_instance = config.euler_liqbot

        self._restore_token_metadata(meta)

    def _protocol_metadata(self) -> dict:
        """Immutable Euler vault addresses persisted for fast state reload (P3)."""
        return {
            "asset_address": self.asset_address,
            "underlying_asset_address": self.underlying_asset_address,
            "target_asset": self.target_asset,
            "target_vault_address": self.target_vault_address,
            "intermediate_vault_address": self.intermediate_vault_address,
            "unit_of_account": self.unit_of_account,
            "vault_manager_address": self.vault_manager_address,
            "oracle_router_address": self.oracle_router_address,
            "vault_name": self.vault_name,
            "vault_symbol": self.vault_symbol,
        }

    def get_collateral_for_borrower(self) -> int:
        """Calculate the collateral amount reserved for the borrower."""
        C_native = self.instance.functions.balanceOf(self.address).call()
        C_usd = self.oracle_router.functions.getQuote(C_native, self.asset_address, self.unit_of_account).call()
        B_usd = self.target_vault.functions.accountLiquidity(self.address, True).call()[1]
        C_for_B = self.instance.functions.collateralForBorrower(B_usd, C_usd).call()
        logger.debug(
            "get_collateral_for_borrower: c_native=%s, c_usd=%s, b_usd=%s, c_for_b=%s",
            C_native,
            C_usd,
            B_usd,
            C_for_B,
        )
        return int(C_for_B)

    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        """Simulate a liquidation for this vault."""
        return EulerLiquidator.simulate_liquidation(self, self.config)


class EulerLiquidator(BaseLiquidator):
    """Handles liquidation calculations and execution for Euler vaults."""

    @staticmethod
    def simulate_liquidation(
        vault: EulerCollateralVault, config: ChainConfig
    ) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        """
        Simulate liquidation and return profitability assessment.

        Returns:
            Tuple of (profitable, liquidation_data_dict, params) or (False, None, None).
        """
        collateral_asset = vault.underlying_asset_address
        borrowed_asset = vault.target_asset

        try:
            logger.info(
                "Liquidator: Simulating liquidation for %s (borrowed=%s, collateral=%s)",
                vault.address,
                borrowed_asset,
                collateral_asset,
            )

            profit_data, params = EulerLiquidator.calculate_liquidation_profit(vault, config)

            if profit_data.get("tx"):
                logger.info(
                    "Liquidator: Profitable liquidation for %s — collateral=%s, profit=%s",
                    vault.address,
                    profit_data.get("collateral_asset"),
                    profit_data.get("profit", 0),
                )
                return (True, profit_data, params)

            logger.debug("Liquidator: No profitable liquidation for %s: %s", vault.address, profit_data)
            return (False, None, None)

        except (ValueError, ContractLogicError, BlockNotFound) as ex:
            logger.error("Liquidator: Liquidation simulation failed for %s: %s", vault.address, ex, exc_info=True)
            return (False, None, None)
        except Exception as ex:
            message = f"LiqSim: Unexpected exception for {vault.address} with collateral {collateral_asset}: {ex}"
            logger.error("Liquidator: %s", message, exc_info=True)

            time_of_last_post = liquidation_error_slack_cooldown.get(vault.address, 0)
            total_borrowed = vault.internal_value_borrowed + vault.external_value_borrowed
            now = time.time()
            elapsed = now - time_of_last_post
            if (total_borrowed > config.SMALL_POSITION_THRESHOLD and elapsed > config.ERROR_COOLDOWN) or (
                total_borrowed <= config.SMALL_POSITION_THRESHOLD and elapsed > config.SMALL_POSITION_REPORT_INTERVAL
            ):
                post_error_notification(message, config)
                liquidation_error_slack_cooldown[vault.address] = now

            # TTL-prune the module-level throttle dict to prevent unbounded growth
            # (P8 / DEV-555).  Entries older than the longest applicable cooldown
            # are inert — the next post for that address is already allowed — so
            # they can be evicted safely.  Iterate over a snapshot so concurrent
            # worker threads never trigger "dict changed size during iteration".
            max_cooldown = max(config.ERROR_COOLDOWN, config.SMALL_POSITION_REPORT_INTERVAL)
            stale = [k for k, ts in list(liquidation_error_slack_cooldown.items()) if (now - ts) > max_cooldown]
            for k in stale:
                liquidation_error_slack_cooldown.pop(k, None)

            return (False, None, None)

    @staticmethod
    def calculate_liquidation_profit(
        collateral_vault: EulerCollateralVault, config: ChainConfig
    ) -> Tuple[Dict[str, Any], Optional[Tuple[Any, ...]]]:
        """
        Calculate liquidation profit and build the transaction.

        Args:
            collateral_vault: The vault to liquidate.
            config: Chain configuration.

        Returns:
            Tuple of (profit_data_dict, params_tuple or None).
        """
        collateral_asset = collateral_vault.underlying_asset_address

        # Check liquidation status
        (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = (
            collateral_vault.check_liquidation(config.LIQUIDATOR_EOA)
        )
        logger.info(
            "Liquidation check for %s: canLiq=%s, extLiq=%s, maxRelease=%s, maxRepay=%s, totalAssets=%s",
            collateral_vault.address,
            can_liquidate,
            externally_liquidated,
            max_release,
            max_repay,
            total_assets,
        )

        seized_collateral_assets = total_assets - max_release

        if not can_liquidate and not externally_liquidated:
            return ({"profit": 0}, None)
        if externally_liquidated and max_release == 0:
            logger.info("Externally liquidated with no credit reserved, skipping")
            return ({"profit": 0}, None)
        if seized_collateral_assets <= 0:
            logger.info("No collateral seized, skipping")
            return ({"profit": 0}, None)

        # Calculate profit
        collateral_value = collateral_vault.oracle_router.functions.getQuote(
            seized_collateral_assets, collateral_vault.asset_address, collateral_vault.unit_of_account
        ).call()
        (_, debt_value) = collateral_vault.target_vault.functions.accountLiquidity(
            collateral_vault.address, True
        ).call()

        if externally_liquidated:
            profit = _calculate_external_profit(collateral_vault, max_repay, max_release, debt_value)
        else:
            profit = collateral_value - debt_value

        if profit <= 0 and not externally_liquidated:
            logger.info("No profit for %s (profit=%s)", collateral_vault.address, profit)
            return ({"profit": profit}, None)

        logger.info(
            "Seized=%s, collateral_value=%s, debt_value=%s, profit=%s",
            seized_collateral_assets,
            collateral_value,
            debt_value,
            profit,
        )

        # Build transaction
        try:
            return _build_liquidation_tx(
                collateral_vault,
                config,
                can_liquidate,
                externally_liquidated,
                max_repay,
                max_release,
                total_assets,
                profit,
                collateral_asset,
            )
        except Exception as ex:
            logger.error(
                "Failed to build liquidation tx for %s: %s (canLiq=%s, extLiq=%s, maxRelease=%s, maxRepay=%s, totalAssets=%s)",
                collateral_vault.address,
                ex,
                can_liquidate,
                externally_liquidated,
                max_release,
                max_repay,
                total_assets,
                exc_info=True,
            )
            return ({"profit": 0}, None)


def _calculate_external_profit(vault: EulerCollateralVault, max_repay: int, max_release: int, debt_value: int) -> int:
    """Calculate profit for an externally liquidated vault."""
    # maxTwyneLTVs is keyed by the intermediate vault (not the collateral asset/eToken):
    # keying on asset_address returns 0 and divides-by-zero in the external-profit math
    # below — a latent bug in the external path surfaced by the DEV-579 fork e2e.
    max_ltv = vault.vault_manager.functions.maxTwyneLTVs(vault.intermediate_vault_address).call()
    user_collateral_underlying = vault.oracle_router.functions.getQuote(
        int(max_repay * LTV_MAXFACTOR // max_ltv), vault.target_asset, vault.underlying_asset_address
    ).call()
    collateral_balance = vault.asset.functions.balanceOf(vault.address).call()
    user_collateral = min(
        collateral_balance,
        vault.asset.functions.convertToShares(user_collateral_underlying).call(),
    )
    release_amount, c_new = external_release_and_c_new(collateral_balance, user_collateral, max_release)
    c_new_usd = vault.oracle_router.functions.getQuote(c_new, vault.asset_address, vault.unit_of_account).call()
    borrower_claim = vault.instance.functions.collateralForBorrower(debt_value, c_new_usd).call()
    liquidator_reward_shares = c_new - borrower_claim
    liquidator_reward_usd = vault.oracle_router.functions.getQuote(
        liquidator_reward_shares, vault.asset_address, vault.unit_of_account
    ).call()
    profit = liquidator_reward_usd - debt_value

    logger.info(
        "External liquidation for %s: balance=%s, userCollateral=%s, release=%s, "
        "c_new=%s, borrowerClaim=%s, rewardShares=%s, rewardUSD=%s, debt=%s, profit=%s",
        vault.address,
        collateral_balance,
        user_collateral,
        release_amount,
        c_new,
        borrower_claim,
        liquidator_reward_shares,
        liquidator_reward_usd,
        debt_value,
        profit,
    )
    return profit


def _build_liquidation_tx(
    collateral_vault: EulerCollateralVault,
    config: ChainConfig,
    can_liquidate: bool,
    externally_liquidated: bool,
    max_repay: int,
    max_release: int,
    total_assets: int,
    profit: int,
    collateral_asset: str,
) -> Tuple[Dict[str, Any], Optional[Tuple[Any, ...]]]:
    """Build the liquidation transaction and estimate gas."""
    fees = get_eip1559_fees(config.w3)

    # Re-check liquidation status (state may have changed)
    (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = collateral_vault.check_liquidation(
        config.LIQUIDATOR_EOA
    )

    # Position may have recovered (or check_liquidation hit an RPC error and
    # returned (False, False, 0, 0, 0)).  Either way there is nothing to do —
    # return early so we don't hit an UnboundLocalError on `liquidation_tx`.
    if not can_liquidate and not externally_liquidated:
        logger.info(
            "Re-check: vault %s is no longer liquidatable (position recovered or RPC error); skipping",
            collateral_vault.address,
        )
        return ({"profit": 0}, None)

    # Calculate swap amount
    amount_in_underlying = _calculate_swap_amount(
        collateral_vault, can_liquidate, externally_liquidated, max_repay, max_release, total_assets
    )

    # Get swap data from 1inch
    swap_data_bytes = _get_swap_data(collateral_vault, config, amount_in_underlying, externally_liquidated, max_repay)
    if swap_data_bytes is None:
        return ({"profit": 0}, None)

    # Build the transaction
    try:
        if externally_liquidated:
            logger.info(
                "Building external liquidation tx: vault=%s, swapBytes=%d bytes",
                collateral_vault.address,
                len(swap_data_bytes),
            )

            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
                collateral_vault.address, swap_data_bytes, 0
            ).build_transaction(
                {
                    "chainId": config.CHAIN_ID,
                    **fees.to_tx_fields(),
                    "from": config.LIQUIDATOR_EOA,
                    "nonce": 0,
                }
            )
        elif can_liquidate:
            logger.info(
                "Building internal liquidation tx: vault=%s, swapBytes=%d bytes",
                collateral_vault.address,
                len(swap_data_bytes),
            )

            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateCollateralVault(
                collateral_vault.address, swap_data_bytes, MIN_LIQUIDATION_PROFIT
            ).build_transaction(
                {
                    "chainId": config.CHAIN_ID,
                    **fees.to_tx_fields(),
                    "from": config.LIQUIDATOR_EOA,
                    "nonce": 0,
                }
            )
    except Exception as ex:
        logger.error(
            "Failed to build tx for %s: %s (canLiq=%s, extLiq=%s)",
            collateral_vault.address,
            ex,
            can_liquidate,
            externally_liquidated,
            exc_info=True,
        )
        raise TransactionBuildError(f"Failed to build liquidation tx: {ex}") from ex

    # Estimate gas and calculate net profit. Use the realistic effective price
    # (baseFee + tip) for accounting; maxFeePerGas is just a worst-case ceiling.
    try:
        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * GAS_ESTIMATE_BUFFER
        liquidation_tx['gas'] = int(estimated_gas)
        expected_gas_price = fees.expected_effective_gas_price
        # Gross `profit` is already in the unit of account (USD); value gas (wei) in the
        # SAME unit via the oracle before subtracting, rather than mixing ETH/wei with USD.
        gas_cost_usd = value_in_usd(
            collateral_vault.oracle_router,
            estimated_gas * expected_gas_price,
            config.WETH.address,
            collateral_vault.unit_of_account,
        )
        net_profit = profit - gas_cost_usd
        min_profit_usd = min_net_profit_usd_raw(config)

        logger.info(
            "Gas estimate for %s: gas=%s, maxFee=%s, tip=%s, expectedGasPrice=%s, "
            "grossProfitUSD=%s, gasCostUSD=%s, netProfitUSD=%s, minUSD=%s",
            collateral_vault.address,
            estimated_gas,
            fees.max_fee_per_gas,
            fees.max_priority_fee_per_gas,
            expected_gas_price,
            profit,
            gas_cost_usd,
            net_profit,
            min_profit_usd,
        )

        # Internal path is gated on the configurable USD floor (MIN_NET_PROFIT_USD, which
        # may be negative to clear a position at a small loss). The external path keeps
        # its existing "always clear" policy, clamping a negative report to 0.
        if net_profit < min_profit_usd and can_liquidate:
            logger.info(
                "Net USD profit below floor for %s (net=%s, min=%s)",
                collateral_vault.address,
                net_profit,
                min_profit_usd,
            )
            return ({"profit": 0}, None)
        elif net_profit < 0 and externally_liquidated:
            net_profit = 0

        return (
            {
                "tx": liquidation_tx,
                "profit": net_profit,
                "collateral_address": collateral_vault.address,
                "collateral_asset": collateral_asset,
            },
            (collateral_vault, collateral_asset, max_repay, config.LIQUIDATOR_EOA),
        )
    except Exception as ex:
        logger.error("Failed to estimate gas for %s: %s", collateral_vault.address, ex, exc_info=True)
        return ({"profit": 0}, None)


def _calculate_swap_amount(
    vault: EulerCollateralVault,
    can_liquidate: bool,
    externally_liquidated: bool,
    max_repay: int,
    max_release: int,
    total_assets: int,
) -> int:
    """Calculate the underlying token amount to swap."""
    if can_liquidate:
        C_for_B = vault.get_collateral_for_borrower()
        remaining_shares = (total_assets - max_release) - C_for_B
        amount = vault.asset.functions.convertToAssets(int(remaining_shares)).call()
        safety_margin = amount // SWAP_MARGIN_DIVISOR
        return amount - safety_margin

    if externally_liquidated:
        if max_repay == 0:
            logger.info("External liquidation with zero maxRepay - no swap needed")
            return 0

        # maxTwyneLTVs is keyed by the intermediate vault (not the collateral asset/eToken);
        # see _calculate_external_profit for the DEV-579 keying-bug note.
        max_ltv = vault.vault_manager.functions.maxTwyneLTVs(vault.intermediate_vault_address).call()
        user_collateral_underlying = vault.oracle_router.functions.getQuote(
            int(max_repay * LTV_MAXFACTOR // max_ltv), vault.target_asset, vault.underlying_asset_address
        ).call()
        collateral_balance = vault.asset.functions.balanceOf(vault.address).call()
        user_collateral = min(
            collateral_balance,
            vault.asset.functions.convertToShares(user_collateral_underlying).call(),
        )
        release_amount, c_new = external_release_and_c_new(collateral_balance, user_collateral, max_release)
        c_new_usd = vault.oracle_router.functions.getQuote(c_new, vault.asset_address, vault.unit_of_account).call()
        (_, debt_value_fresh) = vault.target_vault.functions.accountLiquidity(vault.address, True).call()
        borrower_claim = vault.instance.functions.collateralForBorrower(debt_value_fresh, c_new_usd).call()
        liquidator_reward_shares = c_new - borrower_claim
        amount = vault.asset.functions.convertToAssets(liquidator_reward_shares).call()
        logger.info("External liquidation swap amount (underlying): %s", amount)
        return amount

    return 0


def _get_swap_data(
    vault: EulerCollateralVault,
    config: ChainConfig,
    amount_in_underlying: int,
    externally_liquidated: bool,
    max_repay: int,
) -> Optional[bytes]:
    """Build the swap leg. Returns calldata bytes, empty bytes when no swap is
    needed, or None on failure / when the swap cannot cover the flashloan repayment.

    Uses the configured swap provider — 1inch in production, MockSwapProvider on an
    e2e fork (DEV-579). The provider reports the guaranteed min-return directly
    (external: exact-in zero-slippage; internal: slippage tightened to the repayment
    floor), so we skip only when it cannot cover ``max_repay`` (the DEV-508 failure
    mode), never on merely thin profit.
    """
    if amount_in_underlying <= 0:
        logger.debug("No swap needed (amountInUnderlying=%s)", amount_in_underlying)
        return bytes()

    provider = make_swap_provider(config)
    # Anchor the required min-return at the flashloan repayment plus the on-chain
    # minProfit floor so a swap that succeeds on-chain always repays Morpho.
    required_min_return = max_repay + MIN_LIQUIDATION_PROFIT
    result = provider.build_swap(
        vault.underlying_asset_address,
        vault.target_asset,
        int(amount_in_underlying),
        config.EULER_LIQUIDATOR_ADDRESS,
        required_min_return,
        externally_liquidated,
    )
    if result is None:
        logger.error("Swap provider returned no swap data for %s", vault.address)
        return None

    if max_repay > 0 and result.min_return < max_repay:
        # The swap cannot guarantee even the flashloan principal — broadcasting would
        # burn gas on a guaranteed revert. Skip; re-evaluated next monitoring cycle.
        logger.warning(
            "Swap minReturn=%s < maxRepay=%s (shortfall=%s); skipping to avoid a guaranteed flashloan-repay revert",
            result.min_return,
            max_repay,
            max_repay - result.min_return,
        )
        return None

    return result.calldata
