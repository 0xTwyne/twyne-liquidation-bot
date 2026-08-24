"""Tests for ``_get_swap_data`` in app/liquidation/vaults/euler_vault.py.

Covers the DEV-508 fix (the guaranteed swap minReturn must cover the flashloan
repayment, or the swap is skipped to avoid a guaranteed on-chain revert) at the
swap-provider seam (DEV-579): ``_get_swap_data`` talks to ``SwapProvider.build_swap``,
which reports the guaranteed min_return directly. The 1inch-specific call shape is
covered separately in test_swap_1inch.py::test_build_swap_*.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.swap_provider import SwapData
from app.liquidation.vaults import euler_vault
from app.liquidation.vaults.euler_vault import MIN_LIQUIDATION_PROFIT, _get_swap_data

EULER_LIQUIDATOR = "0x140556939f9Cfa711078DeFBb01B3e51A53Bc464"
UNDERLYING_ASSET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # WETH
TARGET_ASSET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC

# Mirrors the DEV-508 incident: USDC (6dp) target, ~315.41 flashloan repayment.
MAX_REPAY = 315_410_021


def _swap_data_bytes(min_return: int) -> bytes:
    data = bytearray(228)
    data[196:228] = min_return.to_bytes(32, "big")
    return bytes(data)


def _config() -> SimpleNamespace:
    return SimpleNamespace(EULER_LIQUIDATOR_ADDRESS=EULER_LIQUIDATOR)


def _vault() -> SimpleNamespace:
    return SimpleNamespace(
        address="0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720",
        underlying_asset_address=UNDERLYING_ASSET,
        target_asset=TARGET_ASSET,
    )


def _stub_provider(monkeypatch, *, min_return: int = 0, result="default") -> MagicMock:
    """Patch ``make_swap_provider`` to return a provider whose ``build_swap`` yields a
    ``SwapData(min_return)`` by default (or a custom value such as ``None``)."""
    provider = MagicMock()
    ret = SwapData(calldata=_swap_data_bytes(min_return), min_return=min_return) if result == "default" else result
    provider.build_swap.return_value = ret
    monkeypatch.setattr(euler_vault, "make_swap_provider", MagicMock(return_value=provider))
    return provider


def test_non_positive_amount_returns_empty_bytes_without_provider(monkeypatch):
    provider = _stub_provider(monkeypatch)
    assert _get_swap_data(_vault(), _config(), 0, False, MAX_REPAY) == b""
    assert _get_swap_data(_vault(), _config(), -5, False, MAX_REPAY) == b""
    provider.build_swap.assert_not_called()


def test_internal_happy_path_returns_bytes_and_calls_provider(monkeypatch):
    min_return = MAX_REPAY + MIN_LIQUIDATION_PROFIT
    provider = _stub_provider(monkeypatch, min_return=min_return)
    amount = 10**18

    result = _get_swap_data(_vault(), _config(), amount, False, MAX_REPAY)

    assert result == _swap_data_bytes(min_return)
    assert int.from_bytes(result[196:228], "big") == min_return
    provider.build_swap.assert_called_once_with(
        UNDERLYING_ASSET,
        TARGET_ASSET,
        int(amount),
        EULER_LIQUIDATOR,
        MAX_REPAY + MIN_LIQUIDATION_PROFIT,
        False,
    )


def test_internal_min_return_below_max_repay_returns_none(monkeypatch):
    provider = _stub_provider(monkeypatch, min_return=MAX_REPAY - 1)
    assert _get_swap_data(_vault(), _config(), 10**18, False, MAX_REPAY) is None
    provider.build_swap.assert_called_once()


def test_internal_provider_returns_none(monkeypatch):
    _stub_provider(monkeypatch, result=None)
    assert _get_swap_data(_vault(), _config(), 10**18, False, MAX_REPAY) is None


def test_external_happy_path_returns_bytes_and_calls_provider(monkeypatch):
    min_return = MAX_REPAY + MIN_LIQUIDATION_PROFIT
    provider = _stub_provider(monkeypatch, min_return=min_return)
    amount = 10**18

    result = _get_swap_data(_vault(), _config(), amount, True, MAX_REPAY)

    assert result == _swap_data_bytes(min_return)
    provider.build_swap.assert_called_once_with(
        UNDERLYING_ASSET,
        TARGET_ASSET,
        int(amount),
        EULER_LIQUIDATOR,
        MAX_REPAY + MIN_LIQUIDATION_PROFIT,
        True,
    )


def test_external_min_return_below_max_repay_returns_none(monkeypatch):
    provider = _stub_provider(monkeypatch, min_return=MAX_REPAY - 1)
    assert _get_swap_data(_vault(), _config(), 10**18, True, MAX_REPAY) is None
    provider.build_swap.assert_called_once()


def test_external_max_repay_zero_skips_min_return_check(monkeypatch):
    # max_repay == 0 -> the `if max_repay > 0` guard is skipped; bytes returned
    # unchecked even though minReturn (0) would otherwise fail.
    min_return = 0
    provider = _stub_provider(monkeypatch, min_return=min_return)
    result = _get_swap_data(_vault(), _config(), 10**18, True, 0)
    assert result == _swap_data_bytes(min_return)
    provider.build_swap.assert_called_once()


def test_external_provider_returns_none(monkeypatch):
    _stub_provider(monkeypatch, result=None)
    assert _get_swap_data(_vault(), _config(), 10**18, True, MAX_REPAY) is None


def test_dev508_internal_min_return_one_below_max_repay_returns_none(monkeypatch):
    # DEV-508 reproduction: a swap clearing its own slippage floor but under-repaying
    # the flashloan principal (minReturn = MAX_REPAY - 1) must be skipped.
    _stub_provider(monkeypatch, min_return=MAX_REPAY - 1)
    assert _get_swap_data(_vault(), _config(), 10**18, False, MAX_REPAY) is None


def test_dev508_internal_min_return_at_requirement_returns_bytes(monkeypatch):
    # DEV-508 acceptance: minReturn = MAX_REPAY + MIN_LIQUIDATION_PROFIT covers the
    # repayment, so the decoded swap data is returned.
    min_return = MAX_REPAY + MIN_LIQUIDATION_PROFIT
    _stub_provider(monkeypatch, min_return=min_return)
    result = _get_swap_data(_vault(), _config(), 10**18, False, MAX_REPAY)
    assert result == _swap_data_bytes(min_return)
