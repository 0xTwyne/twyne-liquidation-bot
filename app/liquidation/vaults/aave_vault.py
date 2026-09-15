"""
Aave V3 protocol collateral vault and liquidator.
"""

from typing import Any, Dict, Optional, Tuple

from web3 import Web3

from app.liquidation.config_loader import ChainConfig
from app.liquidation.constants import GAS_ESTIMATE_BUFFER, LTV_MAXFACTOR, SWAP_MARGIN_DIVISOR
from app.liquidation.contracts import create_contract_instance
from app.liquidation.errors import describe_revert
from app.liquidation.gas import GasFees, get_eip1559_fees
from app.liquidation.logging_config import setup_logger
from app.liquidation.profitability import (
    external_release_and_c_new,
    min_net_profit_usd_raw,
    net_liquidation_profit_usd,
)
from app.liquidation.swap_provider import make_swap_provider
from app.liquidation.vaults.base_vault import BaseCollateralVault, BaseLiquidator

logger = setup_logger()

# On-chain `minProfit` floor (in target-asset units) enforced by TwyneAaveLiquidator
# and used to anchor the 1inch swap min-return so the flashloan can always be repaid.
# This is NOT the profitability decision — that is made off-chain in USD via
# MIN_NET_PROFIT_USD (see app/liquidation/profitability.py), which may be negative.
MIN_LIQUIDATION_PROFIT = 1


class AaveCollateralVault(BaseCollateralVault):
    protocol = "aave"

    def _init_protocol_contracts(self, config: ChainConfig) -> None:
        if self._restore_metadata is not None:
            # Fast state-reload path (finding P3): no immutable eth_calls.
            self._init_protocol_contracts_from_metadata(config, self._restore_metadata)
            return

        self.instance = create_contract_instance(self.address, config.AAVE_CVAULT_ABI_PATH, config)

        self.health_state_viewer = create_contract_instance(
            config.HEALTHSTATVIEWER_ADDRESS, config.HEALTHSTATVIEWER_ABI_PATH, config
        )

        # Asset is the AaveV3ATokenWrapper
        self.asset_address = self.instance.functions.asset().call()
        self.asset = create_contract_instance(self.asset_address, config.AAVE_WRAPPER_ABI_PATH, config)

        # Underlying is the actual token (e.g., WETH)
        self.underlying_asset_address = self.instance.functions.underlyingAsset().call()
        underlying_contract = create_contract_instance(self.underlying_asset_address, config.ERC20_ABI_PATH, config)
        _, self.underlying_asset_symbol = self._safe_token_meta(
            underlying_contract, default_decimals=18, default_symbol=""
        )

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
        self.vault_manager = create_contract_instance(self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config)

        # Oracle router + unit of account (USD) used to value profit/gas in a single
        # unit, exactly as the Euler path does. Also lets notifications value Aave gas
        # in USD (notifications._gas_cost reads account.oracle_router/unit_of_account).
        self.unit_of_account = self.intermediate_vault.functions.unitOfAccount().call()
        # An Aave collateral vault values its collateral with the wrapper's latestAnswer()
        # and uses a router only to price the target asset and WETH in USD for the profit
        # gate. Twyne 1.0.7 removed VaultManager.oracleRouter(), and the router of the Aave
        # intermediate vault prices the aToken wrapper but not the raw tokens, so prefer the
        # chain's Euler oracle router from config.yaml and fall back to the intermediate
        # vault's oracle when the chain does not configure one.
        self.oracle_router_address = self._pricing_router_address(config)
        self.oracle_router = create_contract_instance(self.oracle_router_address, config.EULER_ROUTER_ABI_PATH, config)

        self.liqbot_instance = create_contract_instance(
            config.AAVE_LIQUIDATOR_ADDRESS, config.AAVE_LIQUIDATOR_ABI_PATH, config
        )

        # Cache immutable token decimals/symbol once (finding P6).
        self._cache_token_metadata()

    def _pricing_router_address(self, config: ChainConfig) -> str:
        """Address of the router that prices the raw target asset and WETH in USD."""
        try:
            configured = config.ORACLE_ROUTER_ADDRESS
        except AttributeError:
            configured = None
        if configured:
            return Web3.to_checksum_address(configured)
        return self.intermediate_vault.functions.oracle().call()

    def _init_protocol_contracts_from_metadata(self, config: ChainConfig, meta: dict) -> None:
        """Reconstruct contract instances from persisted immutable metadata (P3, no RPC)."""
        self.instance = create_contract_instance(self.address, config.AAVE_CVAULT_ABI_PATH, config)
        self.health_state_viewer = create_contract_instance(
            config.HEALTHSTATVIEWER_ADDRESS, config.HEALTHSTATVIEWER_ABI_PATH, config
        )

        self.asset_address = meta["asset_address"]
        self.asset = create_contract_instance(self.asset_address, config.AAVE_WRAPPER_ABI_PATH, config)

        self.underlying_asset_address = meta["underlying_asset_address"]
        self.underlying_asset_symbol = meta.get("underlying_asset_symbol", "")

        self.atoken_address = meta["atoken_address"]
        self.target_asset = meta["target_asset"]

        self.aave_pool_address = meta["aave_pool_address"]
        self.aave_pool = create_contract_instance(self.aave_pool_address, config.AAVE_POOL_ABI_PATH, config)

        self.intermediate_vault_address = meta["intermediate_vault_address"]
        self.intermediate_vault = create_contract_instance(
            self.intermediate_vault_address, config.EVAULT_ABI_PATH, config
        )

        self.vault_manager_address = meta["vault_manager_address"]
        self.vault_manager = create_contract_instance(self.vault_manager_address, config.VAULT_MANAGER_ABI_PATH, config)

        self.unit_of_account = meta["unit_of_account"]
        self.oracle_router_address = meta["oracle_router_address"]
        self.oracle_router = create_contract_instance(self.oracle_router_address, config.EULER_ROUTER_ABI_PATH, config)

        self.liqbot_instance = create_contract_instance(
            config.AAVE_LIQUIDATOR_ADDRESS, config.AAVE_LIQUIDATOR_ABI_PATH, config
        )

        self._restore_token_metadata(meta)

    def _protocol_metadata(self) -> dict:
        """Immutable Aave vault addresses persisted for fast state reload (P3)."""
        return {
            "asset_address": self.asset_address,
            "underlying_asset_address": self.underlying_asset_address,
            "atoken_address": self.atoken_address,
            "target_asset": self.target_asset,
            "aave_pool_address": self.aave_pool_address,
            "intermediate_vault_address": self.intermediate_vault_address,
            "vault_manager_address": self.vault_manager_address,
            "unit_of_account": self.unit_of_account,
            "oracle_router_address": self.oracle_router_address,
        }

    def get_collateral_for_borrower(self) -> int:
        account_data = self.aave_pool.functions.getUserAccountData(self.address).call()
        total_debt_base = account_data[1]

        total_assets = self.instance.functions.totalAssetsDepositedOrReserved().call()
        max_release = self.instance.functions.maxRelease().call()
        user_owned_collateral = total_assets - max_release

        latest_answer = self.asset.functions.latestAnswer().call()
        decimals = self.asset.functions.decimals().call()
        C = user_owned_collateral * latest_answer // (10**decimals)

        c_for_b = self.instance.functions.collateralForBorrower(total_debt_base, C).call()

        logger.info("get_collateral_for_borrower: B=%s, C=%s, cForB=%s", total_debt_base, C, c_for_b)
        return int(c_for_b)

    def get_health_factor(self) -> float:
        account_data = self.aave_pool.functions.getUserAccountData(self.address).call()
        health_factor = account_data[5]
        return health_factor / 1e18

    def simulate_liquidation(self) -> Tuple[bool, Optional[Dict[str, Any]], Any]:
        return AaveLiquidator.simulate_liquidation(self, self.config)


class AaveLiquidator(BaseLiquidator):
    @staticmethod
    def simulate_liquidation(vault: AaveCollateralVault, config: ChainConfig):
        try:
            profit_data, tx = AaveLiquidator.calculate_liquidation_profit(vault, config)
            if tx:
                return (
                    True,
                    {
                        "tx": tx,
                        "profit": profit_data.get("profit", 0),
                        "collateral_address": vault.address,
                        "collateral_asset": vault.underlying_asset_address,
                    },
                    None,
                )
            return (False, None, None)
        except Exception as ex:
            logger.error(
                "AaveLiquidator: simulate_liquidation failed for %s: %s%s",
                vault.address,
                ex,
                describe_revert(ex),
                exc_info=True,
            )
            return (False, None, None)

    @staticmethod
    def calculate_liquidation_profit(
        collateral_vault: AaveCollateralVault, config: ChainConfig
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT START ===")
        logger.info("Collateral Vault Address: %s", collateral_vault.address)

        can_liquidate, externally_liquidated, max_release, max_repay, total_assets = collateral_vault.check_liquidation(
            config.LIQUIDATOR_EOA
        )

        logger.info("canLiquidate: %s, externallyLiquidated: %s", can_liquidate, externally_liquidated)
        logger.info("max_release: %s, max_repay: %s, total_assets: %s", max_release, max_repay, total_assets)

        if not can_liquidate and not externally_liquidated:
            logger.info("Vault is not liquidatable")
            return ({"profit": 0, "reason": "not_liquidatable"}, None)

        fees = get_eip1559_fees(config.w3)

        if externally_liquidated:
            return AaveLiquidator._build_external_liquidation(
                collateral_vault, config, fees, max_repay, max_release, total_assets
            )
        elif can_liquidate:
            return AaveLiquidator._build_internal_liquidation(collateral_vault, config, fees, max_repay)

        return ({"profit": 0}, None)

    @staticmethod
    def _build_internal_liquidation(
        collateral_vault: AaveCollateralVault, config: ChainConfig, fees: GasFees, max_repay: int
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("Building Aave internal liquidation transaction")

        c_for_b = collateral_vault.get_collateral_for_borrower()

        user_owned_collateral = (
            collateral_vault.instance.functions.totalAssetsDepositedOrReserved().call()
            - collateral_vault.instance.functions.maxRelease().call()
        )
        remaining_shares = user_owned_collateral - c_for_b
        amount_in_underlying = collateral_vault.asset.functions.convertToAssets(remaining_shares).call()

        safety_margin = amount_in_underlying // SWAP_MARGIN_DIVISOR
        amount_in_underlying = amount_in_underlying - safety_margin

        logger.info("cForB: %s, swap amount: %s", c_for_b, amount_in_underlying)

        if amount_in_underlying <= 0:
            logger.warning("No underlying to swap after liquidation")
            return ({"profit": 0, "reason": "no_swap_amount"}, None)

        provider = make_swap_provider(config)
        required_min_return = max_repay + MIN_LIQUIDATION_PROFIT
        result = provider.build_swap(
            collateral_vault.underlying_asset_address,
            collateral_vault.target_asset,
            int(amount_in_underlying),
            config.AAVE_LIQUIDATOR_ADDRESS,
            required_min_return,
            externally_liquidated=False,
        )

        if result is None:
            logger.error("Failed to get swap data")
            return ({"profit": 0, "reason": "no_swap_data"}, None)

        swap_data_bytes = result.calldata
        min_return = result.min_return
        logger.info("Internal liquidation check: minReturn=%s, maxRepay=%s", min_return, max_repay)

        if min_return < max_repay:
            shortfall = max_repay - min_return
            logger.warning(
                "Skipping unprofitable internal liquidation: minReturn (%s) < maxRepay (%s), shortfall=%s",
                min_return,
                max_repay,
                shortfall,
            )
            return ({"profit": 0, "reason": "unprofitable_internal_liquidation", "shortfall": shortfall}, None)

        liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateCollateralVault(
            collateral_vault.address, swap_data_bytes, MIN_LIQUIDATION_PROFIT
        ).build_transaction(
            {
                "chainId": config.CHAIN_ID,
                **fees.to_tx_fields(),
                "from": config.LIQUIDATOR_EOA,
                # Placeholder: the real nonce is assigned at send time by the per-EOA
                # nonce manager (DEV-530).
                "nonce": 0,
            }
        )

        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * GAS_ESTIMATE_BUFFER
        liquidation_tx["gas"] = int(estimated_gas)
        gas_wei = estimated_gas * fees.expected_effective_gas_price

        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT END ===")
        return AaveLiquidator._finalize_with_usd_profit(
            collateral_vault, config, min_return, max_repay, gas_wei, liquidation_tx, "internal"
        )

    @staticmethod
    def _finalize_with_usd_profit(
        collateral_vault: AaveCollateralVault,
        config: ChainConfig,
        min_return: int,
        max_repay: int,
        gas_wei: int,
        liquidation_tx: Dict[str, Any],
        label: str,
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        """Gate a built liquidation tx on USD net profit.

        Profit is the swap surplus (min_return - max_repay, target-asset units)
        minus gas (wei), each priced into the unit of account (USD, 1e18-scaled).
        Skips when net USD profit is below MIN_NET_PROFIT_USD (which may be negative
        to accept a small loss to clear a position). Any oracle pricing failure is
        a conservative skip — never execute on an unknown valuation.
        """
        gross_profit_target = min_return - max_repay
        try:
            net_profit_usd = net_liquidation_profit_usd(
                collateral_vault.oracle_router,
                collateral_vault.unit_of_account,
                collateral_vault.target_asset,
                config.WETH.address,
                gross_profit_target,
                gas_wei,
            )
        except Exception as ex:  # noqa: BLE001 - never act on an unknown valuation
            logger.warning(
                "Skipping %s liquidation for %s: USD profit pricing failed: %s",
                label,
                collateral_vault.address,
                ex,
            )
            return ({"profit": 0, "reason": "usd_pricing_failed"}, None)

        min_profit_usd = min_net_profit_usd_raw(config)
        logger.info(
            "%s liquidation USD profit: grossTarget=%s, gasWei=%s, netUSD(1e18)=%s, minUSD(1e18)=%s",
            label,
            gross_profit_target,
            gas_wei,
            net_profit_usd,
            min_profit_usd,
        )

        if net_profit_usd < min_profit_usd:
            logger.info(
                "Skipping %s liquidation below minimum USD net profit: vault=%s, netUSD=%s, minUSD=%s",
                label,
                collateral_vault.address,
                net_profit_usd,
                min_profit_usd,
            )
            return (
                {
                    "profit": 0,
                    "reason": "below_min_net_profit_usd",
                    "net_profit_usd": net_profit_usd,
                    "min_net_profit_usd": min_profit_usd,
                },
                None,
            )

        return ({"profit": net_profit_usd}, liquidation_tx)

    @staticmethod
    def _build_external_liquidation(
        collateral_vault: AaveCollateralVault,
        config: ChainConfig,
        fees: GasFees,
        max_repay: int,
        max_release: int,
        total_assets: int,
    ) -> Tuple[Dict[str, Any], Optional[Dict]]:
        logger.info("Building Aave external liquidation transaction")

        if max_repay == 0:
            logger.info("External liquidation with zero debt")
            liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
                collateral_vault.address, bytes(), 0
            ).build_transaction(
                {
                    "chainId": config.CHAIN_ID,
                    **fees.to_tx_fields(),
                    "from": config.LIQUIDATOR_EOA,
                    "nonce": 0,  # assigned at send time by the per-EOA nonce manager (DEV-530)
                }
            )
            estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * GAS_ESTIMATE_BUFFER
            liquidation_tx["gas"] = int(estimated_gas)
            logger.info("Estimated gas for zero-debt external liquidation: %s", estimated_gas)
            return ({"profit": 0}, liquidation_tx)

        collateral_balance = collateral_vault.asset.functions.balanceOf(collateral_vault.address).call()
        # The liquidation parameters are keyed by (intermediate vault, target asset) since
        # Twyne 1.0.7. Keying on the collateral wrapper returns 0 and divides-by-zero
        # (DEV-579 external-path fix).
        max_ltv = collateral_vault.get_max_twyne_ltv()

        latest_answer = collateral_vault.asset.functions.latestAnswer().call()
        decimals = collateral_vault.asset.functions.decimals().call()

        user_collateral_value = max_repay * LTV_MAXFACTOR // max_ltv
        user_collateral_shares = user_collateral_value * (10**decimals) // latest_answer
        user_collateral_shares = min(collateral_balance, user_collateral_shares)

        release_amount, c_new = external_release_and_c_new(collateral_balance, user_collateral_shares, max_release)
        c_new_usd = c_new * latest_answer // (10**decimals)

        account_data = collateral_vault.aave_pool.functions.getUserAccountData(collateral_vault.address).call()
        debt_value = account_data[1]

        borrower_claim = collateral_vault.instance.functions.collateralForBorrower(debt_value, c_new_usd).call()
        liquidator_reward_shares = c_new - borrower_claim
        amount_in_underlying = collateral_vault.asset.functions.convertToAssets(liquidator_reward_shares).call()

        logger.info(
            "External liquidation: liquidatorReward=%s, amountInUnderlying=%s",
            liquidator_reward_shares,
            amount_in_underlying,
        )

        if amount_in_underlying <= 0:
            logger.warning("No underlying to swap")
            return ({"profit": 0, "reason": "no_swap_amount"}, None)

        provider = make_swap_provider(config)
        required_min_return = max_repay + MIN_LIQUIDATION_PROFIT
        result = provider.build_swap(
            collateral_vault.underlying_asset_address,
            collateral_vault.target_asset,
            int(amount_in_underlying),
            config.AAVE_LIQUIDATOR_ADDRESS,
            required_min_return,
            externally_liquidated=True,
        )

        if result is None:
            logger.error("Failed to get swap data")
            return ({"profit": 0, "reason": "no_swap_data"}, None)

        swap_data_bytes = result.calldata
        min_return = result.min_return
        logger.info("External liquidation check: minReturn=%s, maxRepay=%s", min_return, max_repay)

        if min_return < max_repay:
            shortfall = max_repay - min_return
            logger.warning(
                "Skipping unprofitable external liquidation: minReturn (%s) < maxRepay (%s), shortfall=%s",
                min_return,
                max_repay,
                shortfall,
            )
            return ({"profit": 0, "reason": "unprofitable_external_liquidation", "shortfall": shortfall}, None)

        liquidation_tx = collateral_vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault(
            collateral_vault.address, swap_data_bytes, 0
        ).build_transaction(
            {
                "chainId": config.CHAIN_ID,
                **fees.to_tx_fields(),
                "from": config.LIQUIDATOR_EOA,
                "nonce": 0,  # assigned at send time by the per-EOA nonce manager (DEV-530)
            }
        )

        estimated_gas = config.w3.eth.estimate_gas(liquidation_tx) * GAS_ESTIMATE_BUFFER
        liquidation_tx["gas"] = int(estimated_gas)
        gas_wei = estimated_gas * fees.expected_effective_gas_price

        logger.info("=== AAVE CALCULATE LIQUIDATION PROFIT END ===")
        return AaveLiquidator._finalize_with_usd_profit(
            collateral_vault, config, min_return, max_repay, gas_wei, liquidation_tx, "external"
        )
