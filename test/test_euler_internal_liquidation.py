"""USD net-profit gating for the Euler liquidation path (euler_vault._build_liquidation_tx).

Gross `profit` is already in the unit of account (USD, 1e18) when passed in; the gate
must value gas in the same unit (via the oracle, through WETH) before subtracting, and
must honour MIN_NET_PROFIT_USD (which may be negative to clear a position at a loss)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.liquidation.gas import GasFees
from app.liquidation.profitability import USD
from app.liquidation.vaults import euler_vault

LIQUIDATOR_EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
UNIT = "0x0000000000000000000000000000000000000348"  # USD unit
VAULT = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
COLLATERAL_ASSET = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"


def _oracle(*, weth_usd_per_wei: int = 1, fail: bool = False) -> MagicMock:
    """getQuote(amount, WETH, unit) → 1e18-scaled USD. Synthetic: 1 wei ⇒ $1 for clean math."""
    oracle = MagicMock()

    def get_quote(amount, token, unit):
        if fail:
            raise RuntimeError("oracle unavailable")
        assert token == WETH and unit == UNIT
        return SimpleNamespace(call=lambda: amount * weth_usd_per_wei * USD)

    oracle.functions.getQuote.side_effect = get_quote
    return oracle


def _fees(effective_gas_price: int = 1) -> GasFees:
    return GasFees(
        max_fee_per_gas=effective_gas_price, max_priority_fee_per_gas=effective_gas_price, base_fee_at_pending=0
    )


def _vault(*, can_liquidate=True, externally_liquidated=False, oracle=None) -> MagicMock:
    v = MagicMock()
    v.address = VAULT
    v.unit_of_account = UNIT
    v.oracle_router = oracle if oracle is not None else _oracle()
    # _build_liquidation_tx re-checks status; this governs the path taken.
    v.check_liquidation.return_value = (can_liquidate, externally_liquidated, 0, 1_000, 2_000)
    build = MagicMock(return_value={"to": "0xliq", "data": "0x"})
    v.liqbot_instance.functions.liquidateCollateralVault.return_value.build_transaction = build
    v.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.return_value.build_transaction = build
    return v


def _config(*, gas_estimate=10, min_net_profit_usd=0) -> SimpleNamespace:
    eth = MagicMock()
    eth.estimate_gas.return_value = gas_estimate
    return SimpleNamespace(
        CHAIN_ID=1,
        LIQUIDATOR_EOA=LIQUIDATOR_EOA,
        WETH=SimpleNamespace(address=WETH),
        MIN_NET_PROFIT_USD=min_net_profit_usd,
        w3=SimpleNamespace(eth=eth),
    )


def _patch(monkeypatch, fees_eg=1):
    monkeypatch.setattr(euler_vault, "_calculate_swap_amount", lambda *a, **k: 1_000)
    monkeypatch.setattr(euler_vault, "_get_swap_data", lambda *a, **k: b"\x00" * 8)
    monkeypatch.setattr(euler_vault, "get_eip1559_fees", lambda *a, **k: _fees(fees_eg))


def _build(vault, config, profit):
    return euler_vault._build_liquidation_tx(vault, config, True, False, 1_000, 0, 2_000, profit, COLLATERAL_ASSET)


def test_euler_internal_returns_usd_net_profit(monkeypatch):
    _patch(monkeypatch)  # gas_wei = 20 → $20
    vault = _vault()
    data, _params = _build(vault, _config(min_net_profit_usd=0), profit=50 * USD)
    assert data["profit"] == 30 * USD  # $50 gross - $20 gas
    assert data["tx"] == {"to": "0xliq", "data": "0x", "gas": 20}


def test_euler_internal_gas_priced_in_usd_via_weth(monkeypatch):
    _patch(monkeypatch)
    oracle = _oracle()
    vault = _vault(oracle=oracle)
    _build(vault, _config(min_net_profit_usd=0), profit=50 * USD)
    # The only getQuote in the gate values gas through WETH (not in collateral units).
    call = oracle.functions.getQuote.call_args
    assert call.args[0] == 20  # gas_wei
    assert call.args[1] == WETH


def test_euler_internal_below_floor_skips(monkeypatch):
    _patch(monkeypatch)  # gas = $20
    vault = _vault()
    data, params = _build(vault, _config(min_net_profit_usd=0), profit=5 * USD)  # net = -$15
    assert (data, params) == ({"profit": 0}, None)


def test_euler_internal_negative_threshold_accepts_small_loss(monkeypatch):
    _patch(monkeypatch)  # gas = $20
    vault = _vault()
    data, _params = _build(vault, _config(min_net_profit_usd=-20), profit=5 * USD)  # net = -$15 >= -$20
    assert data["profit"] == -15 * USD
    assert data["tx"]["gas"] == 20


def test_euler_external_proceeds_and_clamps_negative_profit(monkeypatch):
    _patch(monkeypatch)  # gas = $20
    vault = _vault(can_liquidate=False, externally_liquidated=True)
    data, _params = _build(vault, _config(min_net_profit_usd=0), profit=5 * USD)  # net = -$15 → clamp 0
    assert data["profit"] == 0
    assert "tx" in data  # external liquidation still proceeds to clear the position


def test_euler_usd_pricing_failure_skips(monkeypatch):
    _patch(monkeypatch)
    vault = _vault(oracle=_oracle(fail=True))
    data, params = _build(vault, _config(min_net_profit_usd=-1000), profit=50 * USD)
    assert (data, params) == ({"profit": 0}, None)


def test_euler_skips_when_no_swap_data(monkeypatch):
    # _get_swap_data returning None (e.g. the minReturn < maxRepay guard fired) must
    # short-circuit the build into a clean skip rather than broadcasting a doomed tx.
    _patch(monkeypatch)
    monkeypatch.setattr(euler_vault, "_get_swap_data", lambda *a, **k: None)
    vault = _vault()
    data, params = _build(vault, _config(min_net_profit_usd=0), profit=50 * USD)
    assert (data, params) == ({"profit": 0}, None)


def test_euler_build_tx_failure_raises_transaction_build_error(monkeypatch):
    # A failure while encoding the on-chain call surfaces as TransactionBuildError
    # (caught upstream in calculate_liquidation_profit).
    _patch(monkeypatch)
    vault = _vault()
    vault.liqbot_instance.functions.liquidateCollateralVault.return_value.build_transaction = MagicMock(
        side_effect=RuntimeError("encode failed")
    )
    with pytest.raises(euler_vault.TransactionBuildError):
        _build(vault, _config(min_net_profit_usd=0), profit=50 * USD)
