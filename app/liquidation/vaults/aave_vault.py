"""
Aave V3 protocol collateral vault and liquidator.
"""

from typing import Any, Dict, Optional, Tuple

from app.liquidation.config_loader import ChainConfig
from app.liquidation.contracts import create_contract_instance
from app.liquidation.logging_config import setup_logger
from app.liquidation.swap_1inch import OneInchSwapper
from app.liquidation.vaults.base_vault import BaseCollateralVault, BaseLiquidator

logger = setup_logger()


class AaveCollateralVault(BaseCollateralVault):
    protocol = "aave"

    def _init_protocol_contracts(self, config: ChainConfig) -> None:
        self.instance = create_contract_instance(self.address, config.AAVE_CVAULT_ABI_PATH, config)

        self.health_state_viewer = create_contract_instance(
            config.HEALTHSTATVIEWER_ADDRESS, config.HEALTHSTATVIEWER_ABI_PATH, config
        )

        # Asset is the AaveV3ATokenWrapper
        self.asset_address = self.instance.functions.asset().call()
        self.asset = create_contract_instance(self.asset_address, config.AAVE_WRAPPER_ABI_PATH, config)

        # Underlying is the actual token (e.g., WETH)
        self.underlying_asset_address = self.instance.functions.underlyingAsset().call()
        self.underlying_asset_symbol = "AAVE"  # placeholder

        # aToken
        self.atoken_address = self.instance.functions.aToken().call()

        self.target_asset = self.instance.functions.targetAsset().call()

        # For Aave, targetVault is the Aave Pool
        self.aave_pool_address = self.instance.functions.targetVault().call()
        self.aave_pool = create_contract_instance(self.aave_pool_address, config.AAVE_POOL_ABI_PATH, config)

        self.intermediate_vault_address = self.instance.functions.intermediateVault().call()
        self.intermediate_vault = create_contract_instance(
            self.intermediate_vault_address, config.EVAULT_ABI_PATH, config
        )

        self.vault_manager_address = self.instance.functions.twyneVaultManager().call()
        self.vault_manager = create_contract_instance(
            self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config
        )

        self.liqbot_instance = create_contract_instance(
            config.AAVE_LIQUIDATOR_ADDRESS, config.AAVE_LIQUIDATOR_ABI_PATH, config
        )

    def get_collateral_for_borrower(self) -> int:
        account_data = self.aave_pool.functions.getUserAccountData(self.address).call()
        total_debt_base = account_data[1]

        total_assets = self.instance.functions.totalAssetsDepositedOrReserved().call()
        max_release = self.instance.functions.maxRelease().call()
        user_owned_collateral = total_assets - max_release

        latest_answer = self.asset.functions.latestAnswer().call()
        decimals = self.asset.functions.decimals().call()
        C = user_owned_collateral * latest_answer // (10 ** decimals)

        c_for_b = self.instance.functions.collateralForBorrower(total_debt_base, C).call()

        logger.info(
            "get_collateral_for_borrower: B=%s, C=%s, cForB=%s",
            total_debt_base, C, c_for_b
        )
        return int(c_for_b)

    def get_health_factor(self) -> float:
        account_data = self.aave_pool.functions.getUserAccountData(self.address).call()
        health_factor = account_data[5]
        return health_factor / 1e18

    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        return AaveLiquidator.simulate_liquidation(self, self.config)


class AaveLiquidator(BaseLiquidator):

    @staticmethod
    def simulate_liquidation(vault: AaveCollateralVault, config: ChainConfig):
        try:
            profit_data, tx = AaveLiquidator.calculate_liquidation_profit(vault, config)
            if tx:
                return (True, {"tx": tx, "profit": profit_data.get("profit", 0), "collateral_address": vault.address, "collateral_asset": vault.underlying_asset_address}, None)
            return (False, None, None)
        except Exception as ex:
            logger.error("AaveLiquidator: simulate_liquidation failed for %s: %s", vault.address, ex, exc_info=True)
            return (False, None, None)

    @staticmethod
    def calculate_liquidation_profit(
        collateral_vault: AaveCollateralVault,
        config: ChainConfig
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT START ===")
        logger.info("Collateral Vault Address: %s", collateral_vault.address)

        can_liquidate, externally_liquidated, max_release, max_repay, total_assets = \
            collateral_vault.check_liquidation(config.LIQUIDATOR_EOA)

        logger.info("canLiquidate: %s, externallyLiquidated: %s", can_liquidate, externally_liquidated)
        logger.info("max_release: %s, max_repay: %s, total_assets: %s", max_release, max_repay, total_assets)

        if not can_liquidate and not externally_liquidated:
            logger.info("Vault is not liquidatable")
            return ({"profit": 0, "reason": "not_liquidatable"}, None)

        nonce = config.w3.eth.get_transaction_count(config.LIQUIDATOR_EOA)
        suggested_gas_price = config.w3.eth.gas_price

        if can_liquidate:
            return AaveLiquidator._build_internal_liquidation(
                collateral_vault, config, nonce, suggested_gas_price, max_repay
            )
        elif externally_liquidated:
            return AaveLiquidator._build_external_liquidation(
                collateral_vault, config, nonce, suggested_gas_price, max_repay, max_release, total_assets
            )

        return ({"profit": 0}, None)

    @staticmethod
    def _build_internal_liquidation(
        collateral_vault: AaveCollateralVault,
        config: ChainConfig,
        nonce: int,
        gas_price: int,
        max_repay: int
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("Building Aave internal liquidation transaction")

        c_for_b = collateral_vault.get_collateral_for_borrower()
        underlying_for_c_for_b = collateral_vault.asset.functions.previewMint(c_for_b).call()
        collateral_flash_amount = underlying_for_c_for_b * 3

        user_owned_collateral = collateral_vault.instance.functions.totalAssetsDepositedOrReserved().call() - \
                                collateral_vault.instance.functions.maxRelease().call()
        remaining_shares = user_owned_collateral - c_for_b
        amount_in_underlying = collateral_vault.asset.functions.convertToAssets(remaining_shares).call()

        safety_margin = amount_in_underlying // 1000
        amount_in_underlying = amount_in_underlying - safety_margin

        logger.info("cForB: %s, flash amount: %s, swap amount: %s",
                   c_for_b, collateral_flash_amount, amount_in_underlying)

        if amount_in_underlying <= 0:
            logger.warning("No underlying to swap after liquidation")
            return ({"profit": 0, "reason": "no_swap_amount"}, None)

        swapper = OneInchSwapper(config)
        oneinch_data = swapper.get_swap_transaction(
            collateral_vault.underlying_asset_address,
            collateral_vault.target_asset,
            int(amount_in_underlying),
            False,
            1.0,
            config.AAVE_LIQUIDATOR_ADDRESS
        )

        if not oneinch_data or 'data' not in oneinch_data:
            logger.error("Failed to get 1inch swap data")
            return ({"profit": 0, "reason": "no_swap_data"}, None)

        swap_data_bytes = bytes.fromhex(oneinch_data['data'].replace('0x', ''))

        liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateCollateralVault(
            collateral_vault.address, collateral_flash_amount, swap_data_bytes, 1
        ).build_transaction({
            "chainId": config.CHAIN_ID,
            "gasPrice": gas_price,
            "from": config.LIQUIDATOR_EOA,
            "nonce": nonce,
        })

        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * 2
        liquidation_tx["gas"] = int(estimated_gas)
        logger.info("Estimated gas for internal liquidation: %s", estimated_gas)

        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT END ===")
        return ({"profit": 1}, liquidation_tx)

    @staticmethod
    def _build_external_liquidation(
        collateral_vault: AaveCollateralVault,
        config: ChainConfig,
        nonce: int,
        gas_price: int,
        max_repay: int,
        max_release: int,
        total_assets: int
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("Building Aave external liquidation transaction")

        if max_repay == 0:
            logger.info("External liquidation with zero debt")
            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
                collateral_vault.address, bytes(), 0
            ).build_transaction({
                "chainId": config.CHAIN_ID,
                "gasPrice": gas_price,
                "from": config.LIQUIDATOR_EOA,
                "nonce": nonce,
            })
            estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * 2
            liquidation_tx["gas"] = int(estimated_gas)
            logger.info("Estimated gas for zero-debt external liquidation: %s", estimated_gas)
            return ({"profit": 0}, liquidation_tx)

        collateral_balance = collateral_vault.asset.functions.balanceOf(collateral_vault.address).call()
        max_ltv = collateral_vault.vault_manager.functions.maxTwyneLTVs(collateral_vault.asset_address).call()
        MAXFACTOR = 10000

        latest_answer = collateral_vault.asset.functions.latestAnswer().call()
        decimals = collateral_vault.asset.functions.decimals().call()

        user_collateral_value = max_repay * MAXFACTOR // max_ltv
        user_collateral_shares = user_collateral_value * (10 ** decimals) // latest_answer
        user_collateral_shares = min(collateral_balance, user_collateral_shares)

        release_amount = min(collateral_balance - user_collateral_shares, max_release)
        c_new = collateral_balance - release_amount
        c_new_usd = c_new * latest_answer // (10 ** decimals)

        account_data = collateral_vault.aave_pool.functions.getUserAccountData(collateral_vault.address).call()
        debt_value = account_data[1]

        borrower_claim = collateral_vault.instance.functions.collateralForBorrower(debt_value, c_new_usd).call()
        liquidator_reward_shares = c_new - borrower_claim
        amount_in_underlying = collateral_vault.asset.functions.convertToAssets(liquidator_reward_shares).call()

        logger.info("External liquidation: liquidatorReward=%s, amountInUnderlying=%s",
                   liquidator_reward_shares, amount_in_underlying)

        if amount_in_underlying <= 0:
            logger.warning("No underlying to swap")
            return ({"profit": 0, "reason": "no_swap_amount"}, None)

        swapper = OneInchSwapper(config)
        oneinch_data = swapper.get_swap_transaction(
            collateral_vault.underlying_asset_address,
            collateral_vault.target_asset,
            int(amount_in_underlying),
            True,
            0,
            config.AAVE_LIQUIDATOR_ADDRESS
        )

        if not oneinch_data or 'data' not in oneinch_data:
            logger.error("Failed to get 1inch swap data")
            return ({"profit": 0, "reason": "no_swap_data"}, None)

        swap_data_bytes = bytes.fromhex(oneinch_data['data'].replace('0x', ''))

        min_return = int.from_bytes(swap_data_bytes[196:228], 'big')
        logger.info("External liquidation check: minReturn=%s, maxRepay=%s", min_return, max_repay)

        if min_return < max_repay:
            shortfall = max_repay - min_return
            logger.warning(
                "Skipping unprofitable external liquidation: minReturn (%s) < maxRepay (%s), shortfall=%s",
                min_return, max_repay, shortfall
            )
            return ({"profit": 0, "reason": "unprofitable_external_liquidation", "shortfall": shortfall}, None)

        liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
            collateral_vault.address, swap_data_bytes, 0
        ).build_transaction({
            "chainId": config.CHAIN_ID,
            "gasPrice": gas_price,
            "from": config.LIQUIDATOR_EOA,
            "nonce": nonce,
        })

        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * 2
        liquidation_tx["gas"] = int(estimated_gas)
        logger.info("Estimated gas for external liquidation: %s", estimated_gas)

        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT END ===")
        return ({"profit": 1}, liquidation_tx)
