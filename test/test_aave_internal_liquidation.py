import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.gas import GasFees
from app.liquidation.profitability import USD
from app.liquidation.swap_provider import SwapData
from app.liquidation.vaults import aave_vault
from app.liquidation.vaults.aave_vault import MIN_LIQUIDATION_PROFIT, AaveLiquidator

LIQUIDATOR_EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
AAVE_LIQUIDATOR = "0x140556939f9Cfa711078DeFBb01B3e51A53Bc464"
COLLATERAL_VAULT = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
UNDERLYING_ASSET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TARGET_ASSET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC — deliberately NOT WETH
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
UNIT_OF_ACCOUNT = "0x0000000000000000000000000000000000000348"  # USD unit


def _swap_data_bytes(min_return: int) -> bytes:
    data = bytearray(228)
    data[196:228] = min_return.to_bytes(32, "big")
    return bytes(data)


def _oracle(*, target_usd_per_unit: int = 1, weth_usd_per_wei: int = 1, fail: bool = False) -> MagicMock:
    """Oracle router whose getQuote(amount, token, unit) returns a 1e18-scaled USD value.

    Synthetic prices for clean arithmetic: with both prices == 1 ("1 unit ⇒ $1"),
    a raw amount of N reads as $N (i.e. N * 1e18). target_asset and WETH are priced
    separately so a test can prove gas is valued via WETH, not in target-asset units."""
    oracle = MagicMock()

    def get_quote(amount, token, unit):
        if fail:
            raise RuntimeError("oracle unavailable")
        assert unit == UNIT_OF_ACCOUNT
        if token == TARGET_ASSET:
            value = amount * target_usd_per_unit * USD
        elif token == WETH_ADDRESS:
            value = amount * weth_usd_per_wei * USD
        else:
            raise AssertionError(f"unexpected getQuote token {token}")
        return SimpleNamespace(call=lambda: value)

    oracle.functions.getQuote.side_effect = get_quote
    return oracle


def _config(gas_estimate: int = 10, min_net_profit_usd=0):
    eth = SimpleNamespace(estimate_gas=MagicMock(return_value=gas_estimate))
    return SimpleNamespace(
        CHAIN_ID=1,
        LIQUIDATOR_EOA=LIQUIDATOR_EOA,
        AAVE_LIQUIDATOR_ADDRESS=AAVE_LIQUIDATOR,
        WETH=SimpleNamespace(address=WETH_ADDRESS),
        MIN_NET_PROFIT_USD=min_net_profit_usd,
        w3=SimpleNamespace(eth=eth),
    )


def _vault(amount_in_underlying: int = 1_100, oracle=None):
    instance = MagicMock()
    instance.functions.totalAssetsDepositedOrReserved.return_value.call.return_value = 1_500
    instance.functions.maxRelease.return_value.call.return_value = 0

    asset = MagicMock()
    asset.functions.convertToAssets.return_value.call.return_value = amount_in_underlying

    build = MagicMock(return_value={"to": AAVE_LIQUIDATOR, "data": "0xabc"})
    liq_call = MagicMock()
    liq_call.build_transaction = build

    liqbot_instance = MagicMock()
    liqbot_instance.functions.liquidateCollateralVault.return_value = liq_call

    vault = SimpleNamespace(
        address=COLLATERAL_VAULT,
        underlying_asset_address=UNDERLYING_ASSET,
        target_asset=TARGET_ASSET,
        # maxTwyneLTVs is keyed by the intermediate vault (DEV-579 external-path fix).
        intermediate_vault_address="0x75029a47f28550C93Ad5A3BbD2d9b5315204B561",
        oracle_router=oracle if oracle is not None else _oracle(),
        unit_of_account=UNIT_OF_ACCOUNT,
        instance=instance,
        asset=asset,
        liqbot_instance=liqbot_instance,
        get_collateral_for_borrower=MagicMock(return_value=100),
    )
    return vault


def _patch_swapper(monkeypatch, min_return: int):
    """Patch make_swap_provider with a provider whose build_swap yields SwapData(min_return)."""
    provider = MagicMock()
    provider.build_swap.return_value = SwapData(calldata=_swap_data_bytes(min_return), min_return=min_return)
    monkeypatch.setattr(aave_vault, "make_swap_provider", MagicMock(return_value=provider))
    return provider


# Internal and external paths share the same SwapProvider.build_swap seam; the
# builder distinguishes them via the externally_liquidated flag it passes.
def _patch_external_swapper(monkeypatch, min_return: int):
    return _patch_swapper(monkeypatch, min_return)


def _fees(effective_gas_price: int = 1) -> GasFees:
    return GasFees(
        max_fee_per_gas=effective_gas_price,
        max_priority_fee_per_gas=effective_gas_price,
        base_fee_at_pending=0,
    )


def test_aave_internal_min_return_below_max_repay_skips(monkeypatch):
    """Swap can't even cover the debt → skip before any profit/gas pricing."""
    max_repay = 1_000
    swapper = _patch_swapper(monkeypatch, min_return=max_repay - 1)
    config = _config()
    vault = _vault()

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert tx is None
    assert profit_data == {
        "profit": 0,
        "reason": "unprofitable_internal_liquidation",
        "shortfall": 1,
    }
    swapper.build_swap.assert_called_once_with(
        UNDERLYING_ASSET,
        TARGET_ASSET,
        1_099,
        AAVE_LIQUIDATOR,
        max_repay + MIN_LIQUIDATION_PROFIT,
        externally_liquidated=False,
    )
    vault.liqbot_instance.functions.liquidateCollateralVault.assert_not_called()
    config.w3.eth.estimate_gas.assert_not_called()


def test_aave_internal_below_min_usd_profit_skips(monkeypatch):
    """Gas (in USD) exceeds the swap surplus (in USD) → net USD < 0 → skip at default floor 0."""
    max_repay = 1_000
    config = _config(gas_estimate=10, min_net_profit_usd=0)  # gross=$1, gas_wei=20 → $20, net=-$19
    vault = _vault()
    _patch_swapper(monkeypatch, min_return=max_repay + 1)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert tx is None
    assert profit_data["profit"] == 0
    assert profit_data["reason"] == "below_min_net_profit_usd"
    assert profit_data["net_profit_usd"] == -19 * USD
    assert profit_data["min_net_profit_usd"] == 0


def test_aave_internal_returns_usd_net_profit_and_min_profit_argument(monkeypatch):
    """Profitable: reports net USD profit and passes the target-asset MIN_LIQUIDATION_PROFIT on-chain."""
    max_repay = 1_000
    config = _config(gas_estimate=10, min_net_profit_usd=0)  # gross=$50, gas=$20 → net=$30
    vault = _vault()
    _patch_swapper(monkeypatch, min_return=1_050)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert profit_data == {"profit": 30 * USD}
    assert tx == {"to": AAVE_LIQUIDATOR, "data": "0xabc", "gas": 20}
    vault.liqbot_instance.functions.liquidateCollateralVault.assert_called_once()
    args = vault.liqbot_instance.functions.liquidateCollateralVault.call_args.args
    assert args[0] == COLLATERAL_VAULT
    assert args[2] == MIN_LIQUIDATION_PROFIT


def test_aave_internal_negative_threshold_accepts_small_usd_loss(monkeypatch):
    """A NEGATIVE MIN_NET_PROFIT_USD lets the bot clear a position at a small USD loss."""
    max_repay = 1_000
    config = _config(gas_estimate=10, min_net_profit_usd=-20)  # net=-$19, floor=-$20 → execute
    vault = _vault()
    _patch_swapper(monkeypatch, min_return=max_repay + 1)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert tx == {"to": AAVE_LIQUIDATOR, "data": "0xabc", "gas": 20}
    assert profit_data == {"profit": -19 * USD}


def test_aave_internal_loss_beyond_negative_threshold_skips(monkeypatch):
    """A loss larger than the configured negative floor is still rejected."""
    max_repay = 1_000
    config = _config(gas_estimate=20, min_net_profit_usd=-20)  # gross=$1, gas_wei=40 → net=-$39 < -$20
    vault = _vault()
    _patch_swapper(monkeypatch, min_return=max_repay + 1)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert tx is None
    assert profit_data["reason"] == "below_min_net_profit_usd"
    assert profit_data["net_profit_usd"] == -39 * USD
    assert profit_data["min_net_profit_usd"] == -20 * USD


def test_aave_internal_prices_gas_via_weth_and_surplus_via_target(monkeypatch):
    """Gas must be priced through WETH and the surplus through the target asset —
    never mixing wei (ETH) with target-asset units."""
    max_repay = 1_000
    oracle = _oracle()
    config = _config(gas_estimate=10, min_net_profit_usd=0)
    vault = _vault(oracle=oracle)
    _patch_swapper(monkeypatch, min_return=1_050)

    AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    quoted = {(c.args[1]) for c in oracle.functions.getQuote.call_args_list}
    assert TARGET_ASSET in quoted  # swap surplus priced in target asset
    assert WETH_ADDRESS in quoted  # gas priced via WETH, not as target-asset units
    # surplus leg uses gross = min_return - max_repay = 50; gas leg uses gas_wei = 20
    by_token = {c.args[1]: c.args[0] for c in oracle.functions.getQuote.call_args_list}
    assert by_token[TARGET_ASSET] == 50
    assert by_token[WETH_ADDRESS] == 20


def test_aave_usd_pricing_failure_skips(monkeypatch):
    """If the oracle can't value the legs, skip conservatively — never act on an unknown valuation."""
    max_repay = 1_000
    config = _config(gas_estimate=10, min_net_profit_usd=-1000)  # generous floor, but pricing fails
    vault = _vault(oracle=_oracle(fail=True))
    _patch_swapper(monkeypatch, min_return=1_050)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay)

    assert tx is None
    assert profit_data == {"profit": 0, "reason": "usd_pricing_failed"}


def test_aave_external_returns_usd_net_profit(monkeypatch):
    max_repay = 1_000
    config = _config(gas_estimate=10, min_net_profit_usd=0)  # gross=$50, gas_wei=40 → net=$10
    vault = _vault()
    vault.asset_address = UNDERLYING_ASSET
    vault.asset.functions.balanceOf.return_value.call.return_value = 2_000
    vault.asset.functions.latestAnswer.return_value.call.return_value = 1
    vault.asset.functions.decimals.return_value.call.return_value = 0
    vault.asset.functions.convertToAssets.return_value.call.return_value = 1_500
    vault.vault_manager = MagicMock()
    vault.vault_manager.functions.maxTwyneLTVs.return_value.call.return_value = 5_000
    vault.aave_pool = MagicMock()
    vault.aave_pool.functions.getUserAccountData.return_value.call.return_value = (0, 1_000, 0, 0, 0, 0)
    vault.instance.functions.collateralForBorrower.return_value.call.return_value = 500
    build = MagicMock(return_value={"to": AAVE_LIQUIDATOR, "data": "0xdef"})
    liq_call = MagicMock()
    liq_call.build_transaction = build
    vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.return_value = liq_call
    _patch_external_swapper(monkeypatch, min_return=1_050)

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(2), max_repay, max_release=1_000, total_assets=2_000
    )

    assert profit_data == {"profit": 10 * USD}
    assert tx == {"to": AAVE_LIQUIDATOR, "data": "0xdef", "gas": 20}


def test_aave_external_under_repay_skips(monkeypatch):
    """External path also skips when the swap can't cover the debt."""
    max_repay = 1_000
    config = _config(gas_estimate=10)
    vault = _vault()
    vault.asset_address = UNDERLYING_ASSET
    vault.asset.functions.balanceOf.return_value.call.return_value = 2_000
    vault.asset.functions.latestAnswer.return_value.call.return_value = 1
    vault.asset.functions.decimals.return_value.call.return_value = 0
    vault.asset.functions.convertToAssets.return_value.call.return_value = 1_500
    vault.vault_manager = MagicMock()
    vault.vault_manager.functions.maxTwyneLTVs.return_value.call.return_value = 5_000
    vault.aave_pool = MagicMock()
    vault.aave_pool.functions.getUserAccountData.return_value.call.return_value = (0, 1_000, 0, 0, 0, 0)
    vault.instance.functions.collateralForBorrower.return_value.call.return_value = 500
    _patch_external_swapper(monkeypatch, min_return=max_repay - 5)

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(2), max_repay, max_release=1_000, total_assets=2_000
    )

    assert tx is None
    assert profit_data["reason"] == "unprofitable_external_liquidation"
    assert profit_data["shortfall"] == 5


def test_aave_vault_has_no_hard_coded_profit_return():
    source = inspect.getsource(aave_vault)
    assert '{"profit": 1}' not in source
    assert '"profit": 1' not in source


# ---------------------------------------------------------------------------
# Shared helper: build a fully-wired external-path vault (mirrors the setup
# used in test_aave_external_returns_usd_net_profit) so all new external
# USD-gate tests stay DRY.
# ---------------------------------------------------------------------------


def _ext_vault(monkeypatch, *, min_return: int, gas_estimate: int = 10, min_net_profit_usd=0):
    """Return (vault, config) ready for _build_external_liquidation.

    Arithmetic with the default mock values (all prices == 1, max_repay=1_000):
        collateral_balance = 2_000
        max_ltv = 5_000  (MAXFACTOR=10_000)
        latest_answer = 1, decimals = 0
        user_collateral_value = 1_000 * 10_000 // 5_000 = 2_000
        user_collateral_shares = min(2_000, 2_000) = 2_000
        release_amount = min(0, 1_000) = 0
        c_new = 2_000
        c_new_usd = 2_000
        debt_value = 1_000
        borrower_claim = collateralForBorrower(1_000, 2_000) = 500
        liquidator_reward_shares = 1_500
        amount_in_underlying = convertToAssets(1_500) = 1_500  > 0
    """
    config = _config(gas_estimate=gas_estimate, min_net_profit_usd=min_net_profit_usd)
    vault = _vault()
    vault.asset_address = UNDERLYING_ASSET
    vault.asset.functions.balanceOf.return_value.call.return_value = 2_000
    vault.asset.functions.latestAnswer.return_value.call.return_value = 1
    vault.asset.functions.decimals.return_value.call.return_value = 0
    vault.asset.functions.convertToAssets.return_value.call.return_value = 1_500
    vault.vault_manager = MagicMock()
    vault.vault_manager.functions.maxTwyneLTVs.return_value.call.return_value = 5_000
    vault.aave_pool = MagicMock()
    vault.aave_pool.functions.getUserAccountData.return_value.call.return_value = (0, 1_000, 0, 0, 0, 0)
    vault.instance.functions.collateralForBorrower.return_value.call.return_value = 500
    build = MagicMock(return_value={"to": AAVE_LIQUIDATOR, "data": "0xdef"})
    liq_call = MagicMock()
    liq_call.build_transaction = build
    vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.return_value = liq_call
    _patch_external_swapper(monkeypatch, min_return=min_return)
    return vault, config


# ---------------------------------------------------------------------------
# External path — USD-gate symmetry tests
# ---------------------------------------------------------------------------


def test_aave_external_below_min_usd_profit_skips(monkeypatch):
    """Gas (in USD) exceeds the swap surplus (in USD) → net USD < 0 → skip at default floor 0.

    Mirrors test_aave_internal_below_min_usd_profit_skips for the external path.

    Arithmetic (oracle prices == 1, _fees default effective_gas_price=1):
        min_return = 1_001, max_repay = 1_000 → gross_target = 1
        estimated_gas = gas_estimate * 2 = 20
        gas_wei = 20 * effective_gas_price(1) = 20
        gross_usd = 1 * USD
        gas_usd  = 20 * USD
        net_usd  = -19 * USD
        min_net_profit_usd = 0 * USD → net < min → skip
    """
    max_repay = 1_000
    vault, config = _ext_vault(monkeypatch, min_return=max_repay + 1, gas_estimate=10, min_net_profit_usd=0)

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(), max_repay, max_release=1_000, total_assets=2_000
    )

    assert tx is None
    assert profit_data["profit"] == 0
    assert profit_data["reason"] == "below_min_net_profit_usd"
    assert profit_data["net_profit_usd"] == -19 * USD
    assert profit_data["min_net_profit_usd"] == 0


def test_aave_external_negative_threshold_accepts_small_usd_loss(monkeypatch):
    """A NEGATIVE MIN_NET_PROFIT_USD lets the external path clear a position at a small USD loss.

    Mirrors test_aave_internal_negative_threshold_accepts_small_usd_loss.

    Arithmetic (same as above):
        net_usd = -19 * USD
        min_net_profit_usd = -20 → floor = -20 * USD
        -19 * USD > -20 * USD → execute and report net profit
    """
    max_repay = 1_000
    vault, config = _ext_vault(monkeypatch, min_return=max_repay + 1, gas_estimate=10, min_net_profit_usd=-20)

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(), max_repay, max_release=1_000, total_assets=2_000
    )

    assert tx == {"to": AAVE_LIQUIDATOR, "data": "0xdef", "gas": 20}
    assert profit_data == {"profit": -19 * USD}


# ---------------------------------------------------------------------------
# Guard-branch coverage: the early-return guards inside the two builders.
# ---------------------------------------------------------------------------


def test_aave_internal_no_swap_amount_skips(monkeypatch):
    """convertToAssets → 0 ⇒ nothing left to swap ⇒ skip before quoting 1inch."""
    config = _config()
    vault = _vault()
    vault.asset.functions.convertToAssets.return_value.call.return_value = 0
    swapper = _patch_swapper(monkeypatch, min_return=10_000)

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay=1_000)

    assert tx is None
    assert profit_data == {"profit": 0, "reason": "no_swap_amount"}
    swapper.build_swap.assert_not_called()


def test_aave_internal_no_swap_data_skips(monkeypatch):
    """1inch returns nothing usable ⇒ skip with reason no_swap_data."""
    config = _config()
    vault = _vault()
    provider = MagicMock()
    provider.build_swap.return_value = None
    monkeypatch.setattr(aave_vault, "make_swap_provider", MagicMock(return_value=provider))

    profit_data, tx = AaveLiquidator._build_internal_liquidation(vault, config, _fees(), max_repay=1_000)

    assert tx is None
    assert profit_data == {"profit": 0, "reason": "no_swap_data"}
    config.w3.eth.estimate_gas.assert_not_called()


def test_aave_external_zero_debt_builds_tx_without_swap(monkeypatch):
    """max_repay == 0 ⇒ zero-debt path builds the ext-liq tx with empty swap bytes, no quote."""
    config = _config(gas_estimate=10)
    vault = _vault()
    build = MagicMock(return_value={"to": AAVE_LIQUIDATOR, "data": "0xzero"})
    liq_call = MagicMock()
    liq_call.build_transaction = build
    vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.return_value = liq_call
    swapper = _patch_external_swapper(monkeypatch, min_return=10_000)

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(), max_repay=0, max_release=0, total_assets=2_000
    )

    assert profit_data == {"profit": 0}
    assert tx == {"to": AAVE_LIQUIDATOR, "data": "0xzero", "gas": 20}
    # Empty swap bytes passed on-chain; no collateral walk and no 1inch quote.
    args = vault.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.call_args.args
    assert args[1] == bytes()
    vault.asset.functions.balanceOf.assert_not_called()
    swapper.build_swap.assert_not_called()


def test_aave_external_no_swap_amount_skips(monkeypatch):
    """External walk yields a non-positive reward ⇒ skip before quoting 1inch."""
    max_repay = 1_000
    vault, config = _ext_vault(monkeypatch, min_return=10_000, gas_estimate=10)
    vault.asset.functions.convertToAssets.return_value.call.return_value = 0  # liquidator reward → 0
    provider = aave_vault.make_swap_provider(config)  # the patched mock instance

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(), max_repay, max_release=1_000, total_assets=2_000
    )

    assert tx is None
    assert profit_data == {"profit": 0, "reason": "no_swap_amount"}
    provider.build_swap.assert_not_called()


def test_aave_external_no_swap_data_skips(monkeypatch):
    """1inch returns nothing usable on the external path ⇒ skip with reason no_swap_data."""
    max_repay = 1_000
    vault, config = _ext_vault(monkeypatch, min_return=10_000, gas_estimate=10)
    provider = aave_vault.make_swap_provider(config)  # the patched mock instance
    provider.build_swap.return_value = None

    profit_data, tx = AaveLiquidator._build_external_liquidation(
        vault, config, _fees(), max_repay, max_release=1_000, total_assets=2_000
    )

    assert tx is None
    assert profit_data == {"profit": 0, "reason": "no_swap_data"}
    config.w3.eth.estimate_gas.assert_not_called()
