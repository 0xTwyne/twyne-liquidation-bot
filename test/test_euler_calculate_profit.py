"""Profit calculation for the Euler liquidation path.

Covers two functions in euler_vault:

* `_calculate_external_profit` — pure profit math for an externally-liquidated
  vault: ``liquidator_reward_usd - debt_value``.
* `EulerLiquidator.calculate_liquidation_profit` — the early-return decision
  ladder (not-liquidatable, ext-liq-no-credit, no-collateral-seized,
  negative internal profit gate) plus the positive-profit delegation to
  `_build_liquidation_tx` (mocked out, since its USD/gas gate is covered by
  test_euler_internal_liquidation.py).

All contract calls are mocked — no network/RPC.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.vaults import euler_vault
from app.liquidation.vaults.euler_vault import EulerLiquidator

LIQUIDATOR_EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
VAULT = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
COLLATERAL_ASSET = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
UNDERLYING_ASSET = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
TARGET_ASSET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ASSET_ADDRESS = "0x1111111111111111111111111111111111111111"
UNIT = "0x0000000000000000000000000000000000000348"


def _quote(value):
    """One getQuote(...) result whose .call() returns ``value``."""
    q = MagicMock()
    q.call.return_value = value
    return q


def _config(*, liquidator_eoa=LIQUIDATOR_EOA):
    return SimpleNamespace(LIQUIDATOR_EOA=liquidator_eoa)


# ---------------------------------------------------------------------------
# G3 — _calculate_external_profit
# ---------------------------------------------------------------------------


def test_calculate_external_profit_returns_reward_minus_debt():
    """One clean numeric example end-to-end.

    max_ltv=8000, getQuote#1=500, balanceOf=1000, convertToShares=600,
      user_collateral = min(1000, 600) = 600
    max_release=300 → release_amount = min(1000-600, 300) = 300
      c_new = 1000 - 300 = 700
    getQuote#2 (c_new_usd) = 7000
    collateralForBorrower = 100 → liquidator_reward_shares = 700 - 100 = 600
    getQuote#3 (liquidator_reward_usd) = 50000
    profit = 50000 - debt_value(12345) = 37655
    """
    vault = MagicMock()
    vault.address = VAULT
    vault.asset_address = ASSET_ADDRESS
    vault.target_asset = TARGET_ASSET
    vault.underlying_asset_address = UNDERLYING_ASSET
    vault.unit_of_account = UNIT

    vault.vault_manager.functions.maxTwyneLTVs.return_value.call.return_value = 8000
    # getQuote is called three times in order: user_collateral_underlying,
    # c_new_usd, liquidator_reward_usd.
    vault.oracle_router.functions.getQuote.side_effect = [
        _quote(500),
        _quote(7000),
        _quote(50000),
    ]
    vault.asset.functions.balanceOf.return_value.call.return_value = 1000
    vault.asset.functions.convertToShares.return_value.call.return_value = 600
    vault.instance.functions.collateralForBorrower.return_value.call.return_value = 100

    debt_value = 12345
    profit = euler_vault._calculate_external_profit(vault, max_repay=4000, max_release=300, debt_value=debt_value)

    assert profit == 50000 - debt_value


def test_calculate_external_profit_can_be_negative():
    """When liquidator_reward_usd < debt_value, profit is negative."""
    vault = MagicMock()
    vault.address = VAULT
    vault.asset_address = ASSET_ADDRESS
    vault.target_asset = TARGET_ASSET
    vault.underlying_asset_address = UNDERLYING_ASSET
    vault.unit_of_account = UNIT

    vault.vault_manager.functions.maxTwyneLTVs.return_value.call.return_value = 8000
    vault.oracle_router.functions.getQuote.side_effect = [
        _quote(500),
        _quote(7000),
        _quote(1000),  # liquidator_reward_usd
    ]
    vault.asset.functions.balanceOf.return_value.call.return_value = 1000
    vault.asset.functions.convertToShares.return_value.call.return_value = 600
    vault.instance.functions.collateralForBorrower.return_value.call.return_value = 100

    profit = euler_vault._calculate_external_profit(vault, max_repay=4000, max_release=300, debt_value=5000)

    assert profit == 1000 - 5000


# ---------------------------------------------------------------------------
# G4 — EulerLiquidator.calculate_liquidation_profit
# ---------------------------------------------------------------------------


def _vault(
    *,
    target_asset=TARGET_ASSET,
    can_liquidate=True,
    externally_liquidated=False,
    max_release=100,
    max_repay=4000,
    total_assets=1000,
    collateral_value=None,
    debt_value=None,
):
    v = MagicMock()
    v.address = VAULT
    v.underlying_asset_address = UNDERLYING_ASSET
    v.target_asset = target_asset
    v.asset_address = ASSET_ADDRESS
    v.unit_of_account = UNIT
    v.check_liquidation.return_value = (
        can_liquidate,
        externally_liquidated,
        max_release,
        max_repay,
        total_assets,
    )
    if collateral_value is not None:
        v.oracle_router.functions.getQuote.return_value.call.return_value = collateral_value
    if debt_value is not None:
        v.target_vault.functions.accountLiquidity.return_value.call.return_value = (0, debt_value)
    return v


def test_not_liquidatable_returns_zero():
    # not can_liquidate and not externally_liquidated
    vault = _vault(can_liquidate=False, externally_liquidated=False)
    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())
    assert result == ({"profit": 0}, None)


def test_externally_liquidated_no_credit_returns_zero():
    # externally_liquidated and max_release == 0
    vault = _vault(can_liquidate=False, externally_liquidated=True, max_release=0)
    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())
    assert result == ({"profit": 0}, None)


def test_no_collateral_seized_returns_zero():
    # seized_collateral_assets = total_assets - max_release <= 0
    vault = _vault(can_liquidate=True, total_assets=500, max_release=500)
    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())
    assert result == ({"profit": 0}, None)


def test_negative_internal_profit_gate():
    # can_liquidate, collateral_value <= debt_value → profit <= 0 → ({"profit": profit}, None)
    collateral_value = 700
    debt_value = 1000
    vault = _vault(
        can_liquidate=True,
        externally_liquidated=False,
        max_release=100,
        total_assets=1000,
        collateral_value=collateral_value,
        debt_value=debt_value,
    )
    data, params = EulerLiquidator.calculate_liquidation_profit(vault, _config())
    assert data == {"profit": collateral_value - debt_value}
    assert params is None


def test_positive_internal_profit_delegates_to_build_tx(monkeypatch):
    collateral_value = 5000
    debt_value = 1000
    vault = _vault(
        can_liquidate=True,
        externally_liquidated=False,
        max_release=100,
        total_assets=1000,
        collateral_value=collateral_value,
        debt_value=debt_value,
    )
    sentinel = ({"profit": 42, "tx": {"to": "0xliq"}}, ("params",))
    captured = {}

    def fake_build(cv, cfg, can_liq, ext_liq, mrepay, mrelease, tassets, profit, casset):
        captured["profit"] = profit
        return sentinel

    monkeypatch.setattr(euler_vault, "_build_liquidation_tx", fake_build)

    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())

    assert result == sentinel
    assert captured["profit"] == collateral_value - debt_value


def test_build_tx_exception_is_caught(monkeypatch):
    vault = _vault(
        can_liquidate=True,
        externally_liquidated=False,
        max_release=100,
        total_assets=1000,
        collateral_value=5000,
        debt_value=1000,
    )

    def boom(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(euler_vault, "_build_liquidation_tx", boom)

    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())
    assert result == ({"profit": 0}, None)


def test_externally_liquidated_uses_external_profit_and_delegates(monkeypatch):
    """externally_liquidated path: profit comes from _calculate_external_profit and the
    negative-profit gate is bypassed (it only applies when NOT externally liquidated),
    so it always delegates to _build_liquidation_tx."""
    vault = _vault(
        can_liquidate=False,
        externally_liquidated=True,
        max_release=100,
        total_assets=1000,
        collateral_value=5000,
        debt_value=9000,  # would be a loss, but ext-liq still proceeds
    )
    ext_profit = -4000  # negative: proves the `profit <= 0 and not externally_liquidated` gate is skipped
    monkeypatch.setattr(euler_vault, "_calculate_external_profit", lambda *a, **k: ext_profit)

    sentinel = ({"profit": 7, "tx": {"to": "0xliq"}}, ("params",))
    captured = {}

    def fake_build(cv, cfg, can_liq, ext_liq, mrepay, mrelease, tassets, profit, casset):
        captured["profit"] = profit
        captured["ext_liq"] = ext_liq
        return sentinel

    monkeypatch.setattr(euler_vault, "_build_liquidation_tx", fake_build)

    result = EulerLiquidator.calculate_liquidation_profit(vault, _config())

    assert result == sentinel
    assert captured["profit"] == ext_profit
    assert captured["ext_liq"] is True
