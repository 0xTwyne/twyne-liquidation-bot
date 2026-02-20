"""
Euler protocol collateral vault and liquidator.
"""

import time
from typing import Any, Dict, Optional, Tuple

from web3.exceptions import BlockNotFound, ContractLogicError

from app.liquidation.config_loader import ChainConfig
from app.liquidation.contracts import create_contract_instance
from app.liquidation.exceptions import TransactionBuildError
from app.liquidation.logging_config import setup_logger
from app.liquidation.notifications import post_error_notification
from app.liquidation.swap_1inch import OneInchSwapper
from app.liquidation.vaults.base_vault import BaseCollateralVault, BaseLiquidator

logger = setup_logger()

liquidation_error_slack_cooldown = {}


class EulerCollateralVault(BaseCollateralVault):
    """Collateral vault implementation for the Euler protocol."""

    protocol = "euler"

    def _init_protocol_contracts(self, config: ChainConfig) -> None:
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
        self.vault_manager = create_contract_instance(
            self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config
        )
        self.oracle_router_address = self.vault_manager.functions.oracleRouter().call()
        self.oracle_router = create_contract_instance(
            self.oracle_router_address, config.EULER_ROUTER_ABI_PATH, config
        )

        self.vault_name = self.instance.functions.name().call()
        self.vault_symbol = self.instance.functions.symbol().call()

        self.liqbot_instance = config.euler_liqbot

    def get_collateral_for_borrower(self) -> int:
        """Calculate the collateral amount reserved for the borrower."""
        C_native = self.instance.functions.balanceOf(self.address).call()
        C_usd = self.oracle_router.functions.getQuote(C_native, self.asset_address, self.unit_of_account).call()
        B_usd = self.target_vault.functions.accountLiquidity(self.address, True).call()[1]
        C_for_B = self.instance.functions.collateralForBorrower(B_usd, C_usd).call()
        logger.debug(
            "get_collateral_for_borrower: c_native=%s, c_usd=%s, b_usd=%s, c_for_b=%s",
            C_native, C_usd, B_usd, C_for_B,
        )
        return int(C_for_B)

    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        """Simulate a liquidation for this vault."""
        return EulerLiquidator.simulate_liquidation(self, self.config)


class EulerLiquidator(BaseLiquidator):
    """Handles liquidation calculations and execution for Euler vaults."""

    @staticmethod
    def simulate_liquidation(vault: EulerCollateralVault, config: ChainConfig) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        """
        Simulate liquidation and return profitability assessment.

        Returns:
            Tuple of (profitable, liquidation_data_dict, params) or (False, None, None).
        """
        collateral_asset = vault.underlying_asset_address
        borrowed_asset = vault.target_asset

        if borrowed_asset.lower() == config.USDS_ADDRESS.lower():
            logger.info("Liquidator: Skipping position with USDS debt on Base")
            return (False, None, None)

        try:
            logger.info(
                "Liquidator: Simulating liquidation for %s (borrowed=%s, collateral=%s)",
                vault.address, borrowed_asset, collateral_asset,
            )

            profit_data, params = EulerLiquidator.calculate_liquidation_profit(vault, config)

            if profit_data.get("tx"):
                logger.info(
                    "Liquidator: Profitable liquidation for %s — collateral=%s, profit=%s",
                    vault.address, profit_data.get("collateral_asset"), profit_data.get("profit", 0),
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
                total_borrowed <= config.SMALL_POSITION_THRESHOLD
                and elapsed > config.SMALL_POSITION_REPORT_INTERVAL
            ):
                post_error_notification(message, config)
                liquidation_error_slack_cooldown[vault.address] = now
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
        target_asset = collateral_vault.target_asset

        if target_asset.lower() == config.USDS_ADDRESS.lower():
            logger.info("Skipping USDS debt position for %s", collateral_vault.address)
            return ({"profit": 0}, None)

        # Check liquidation status
        (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = (
            collateral_vault.check_liquidation(config.LIQUIDATOR_EOA)
        )
        logger.info(
            "Liquidation check for %s: canLiq=%s, extLiq=%s, maxRelease=%s, maxRepay=%s, totalAssets=%s",
            collateral_vault.address, can_liquidate, externally_liquidated, max_release, max_repay, total_assets,
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
            seized_collateral_assets, collateral_value, debt_value, profit,
        )

        # Build transaction
        try:
            return _build_liquidation_tx(collateral_vault, config, can_liquidate, externally_liquidated,
                                          max_repay, max_release, total_assets, profit, collateral_asset)
        except Exception as ex:
            logger.error(
                "Failed to build liquidation tx for %s: %s (canLiq=%s, extLiq=%s, maxRelease=%s, maxRepay=%s, totalAssets=%s)",
                collateral_vault.address, ex, can_liquidate, externally_liquidated, max_release, max_repay, total_assets,
                exc_info=True,
            )
            return ({"profit": 0}, None)


def _calculate_external_profit(vault: EulerCollateralVault, max_repay: int, max_release: int, debt_value: int) -> int:
    """Calculate profit for an externally liquidated vault."""
    max_ltv = vault.vault_manager.functions.maxTwyneLTVs(vault.asset_address).call()
    MAXFACTOR = 10000
    user_collateral_underlying = vault.oracle_router.functions.getQuote(
        int(max_repay * MAXFACTOR // max_ltv), vault.target_asset, vault.underlying_asset_address
    ).call()
    collateral_balance = vault.asset.functions.balanceOf(vault.address).call()
    user_collateral = min(
        collateral_balance,
        vault.asset.functions.convertToShares(user_collateral_underlying).call(),
    )
    release_amount = min(collateral_balance - user_collateral, max_release)
    c_new = collateral_balance - release_amount
    c_new_usd = vault.oracle_router.functions.getQuote(
        c_new, vault.asset_address, vault.unit_of_account
    ).call()
    borrower_claim = vault.instance.functions.collateralForBorrower(debt_value, c_new_usd).call()
    liquidator_reward_shares = c_new - borrower_claim
    liquidator_reward_usd = vault.oracle_router.functions.getQuote(
        liquidator_reward_shares, vault.asset_address, vault.unit_of_account
    ).call()
    profit = liquidator_reward_usd - debt_value

    logger.info(
        "External liquidation for %s: balance=%s, userCollateral=%s, release=%s, "
        "c_new=%s, borrowerClaim=%s, rewardShares=%s, rewardUSD=%s, debt=%s, profit=%s",
        vault.address, collateral_balance, user_collateral, release_amount,
        c_new, borrower_claim, liquidator_reward_shares, liquidator_reward_usd, debt_value, profit,
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
    base_gas_price = config.w3.eth.gas_price * 2
    max_priority_fee = config.w3.eth.max_priority_fee * 2
    suggested_gas_price = min(int(base_gas_price), int(base_gas_price + max_priority_fee))
    nonce = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA)

    # Re-check liquidation status (state may have changed)
    (can_liquidate, externally_liquidated, max_release, max_repay, total_assets) = (
        collateral_vault.check_liquidation(config.LIQUIDATOR_EOA)
    )

    # Calculate swap amount
    amount_in_underlying = _calculate_swap_amount(
        collateral_vault, can_liquidate, externally_liquidated, max_repay, max_release, total_assets
    )

    # Get swap data from 1inch
    swap_data_bytes = _get_swap_data(
        collateral_vault, config, amount_in_underlying, externally_liquidated, max_repay
    )
    if swap_data_bytes is None:
        return ({"profit": 0}, None)

    # Build the transaction
    try:
        if can_liquidate:
            C_for_B = collateral_vault.get_collateral_for_borrower()
            C_for_B_underlying = collateral_vault.asset.functions.previewMint(int(C_for_B)).call()
            collateral_flash_amount = C_for_B_underlying * 3

            logger.info(
                "Building internal liquidation tx: vault=%s, flashAmount=%s, swapBytes=%d bytes",
                collateral_vault.address, collateral_flash_amount, len(swap_data_bytes),
            )

            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateCollateralVault(
                collateral_vault.address, collateral_flash_amount, swap_data_bytes, 1
            ).build_transaction({
                "chainId": config.CHAIN_ID,
                "gasPrice": suggested_gas_price,
                "from": config.LIQUIDATOR_EOA,
                "nonce": nonce,
            })
        elif externally_liquidated:
            logger.info(
                "Building external liquidation tx: vault=%s, swapBytes=%d bytes",
                collateral_vault.address, len(swap_data_bytes),
            )

            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
                collateral_vault.address, swap_data_bytes, 0
            ).build_transaction({
                "chainId": config.CHAIN_ID,
                "gasPrice": suggested_gas_price,
                "from": config.LIQUIDATOR_EOA,
                "nonce": nonce,
            })
    except Exception as ex:
        logger.error(
            "Failed to build tx for %s: %s (canLiq=%s, extLiq=%s)",
            collateral_vault.address, ex, can_liquidate, externally_liquidated,
            exc_info=True,
        )
        raise TransactionBuildError(f"Failed to build liquidation tx: {ex}") from ex

    # Estimate gas and calculate net profit
    try:
        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * 2
        liquidation_tx['gas'] = int(estimated_gas)
        net_profit = profit - (estimated_gas * suggested_gas_price)

        logger.info(
            "Gas estimate for %s: gas=%s, gasPrice=%s, grossProfit=%s, netProfit=%s",
            collateral_vault.address, estimated_gas, suggested_gas_price, profit, net_profit,
        )

        if net_profit <= 0 and can_liquidate:
            logger.info("No profit after gas costs for %s", collateral_vault.address)
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
        user_owned_underlying = vault.asset.functions.convertToAssets(int(total_assets - max_release)).call()
        C_for_B_underlying = vault.asset.functions.previewMint(int(C_for_B)).call()
        safety_margin = C_for_B_underlying // 1000
        return user_owned_underlying - C_for_B_underlying - safety_margin

    if externally_liquidated:
        if max_repay == 0:
            logger.info("External liquidation with zero maxRepay - no swap needed")
            return 0

        max_ltv = vault.vault_manager.functions.maxTwyneLTVs(vault.asset_address).call()
        MAXFACTOR = 10000
        user_collateral_underlying = vault.oracle_router.functions.getQuote(
            int(max_repay * MAXFACTOR // max_ltv), vault.target_asset, vault.underlying_asset_address
        ).call()
        collateral_balance = vault.asset.functions.balanceOf(vault.address).call()
        user_collateral = min(
            collateral_balance,
            vault.asset.functions.convertToShares(user_collateral_underlying).call(),
        )
        release_amount = min(collateral_balance - user_collateral, max_release)
        c_new = collateral_balance - release_amount
        c_new_usd = vault.oracle_router.functions.getQuote(
            c_new, vault.asset_address, vault.unit_of_account
        ).call()
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
    """Get 1inch swap data. Returns bytes or None on failure."""
    if amount_in_underlying <= 0:
        logger.debug("No swap needed (amountInUnderlying=%s)", amount_in_underlying)
        return bytes()

    swapper = OneInchSwapper(config)
    slippage = 0 if externally_liquidated else 1.0
    logger.info(
        "1inch swap: src=%s, dst=%s, amount=%s, extLiq=%s, slippage=%s",
        vault.underlying_asset_address, vault.target_asset, int(amount_in_underlying),
        externally_liquidated, slippage,
    )

    oneinch_data = swapper.get_swap_transaction(
        vault.underlying_asset_address, vault.target_asset,
        int(amount_in_underlying), externally_liquidated, slippage,
        config.EULER_LIQUIDATOR_ADDRESS,
    )

    if not oneinch_data or 'data' not in oneinch_data:
        logger.error("Invalid 1inch swap data: %s", oneinch_data)
        return None

    swap_data_bytes = bytes.fromhex(oneinch_data['data'].replace('0x', ''))

    if externally_liquidated and max_repay > 0:
        min_return = int.from_bytes(swap_data_bytes[196:228], 'big')
        if min_return < max_repay:
            shortfall = max_repay - min_return
            logger.warning(
                "Unprofitable external liquidation: minReturn=%s < maxRepay=%s, shortfall=%s",
                min_return, max_repay, shortfall,
            )
            return None

    return swap_data_bytes
