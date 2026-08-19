"""
Tests for the notifications module.
"""

from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

from app.liquidation.notifications import (
    _gas_cost,
    _hex,
    _realized_profit,
    post_error_notification,
    post_liquidation_failed_notification,
    post_liquidation_opportunity_notification,
    post_liquidation_result_notification,
    post_low_health_account_report_notification,
    post_unhealthy_account_notification,
)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _word(value: str) -> str:
    """Left-pad a hex value (no 0x) to a 32-byte EVM word."""
    return value.rjust(64, "0")


def _liquidation_receipt(repaid_asset: str, amount_repaid: int, amount_profit: int, status: int = 1):
    """Build a minimal tx receipt carrying a TwyneLiquidator Liquidation event."""
    topic0 = Web3.keccak(text="Liquidation(address,address,address,uint256,uint256)")
    data = (
        "0x"
        + (
            _word(repaid_asset[2:])  # repaidBorrowAsset
            + _word(repaid_asset[2:])  # seizedCollateralAsset (reuse for the test)
            + _word(f"{amount_repaid:x}")  # amountRepaid
            + _word(f"{amount_profit:x}")  # amountProfit
        )
    )
    return {
        "status": status,
        "gasUsed": 1_156_404,
        "effectiveGasPrice": 7_692_000_000,
        "logs": [{"topics": [topic0], "data": data, "address": USDC}],
    }


@pytest.fixture(autouse=True)
def mock_apprise_setup():
    """Patch setup_apprise_notification_object to prevent live ntfy.sh sends.

    Uses unittest.mock.patch (importlib-based resolution) because
    app.liquidation is shadowed in the app namespace by a Flask Blueprint of
    the same name; monkeypatch.setattr's attribute-traversal approach hits the
    Blueprint rather than the subpackage module.

    Yields the mock Apprise object so that tests can assert on notify() call
    args (body/title) without any real network I/O.
    """
    mock_obj = MagicMock()
    mock_obj.notify.return_value = True
    with patch(
        "app.liquidation.notifications.setup_apprise_notification_object",
        return_value=mock_obj,
    ):
        yield mock_obj


# ---------------------------------------------------------------------------
# Notification send tests — these use mock_apprise_setup to verify formatting
# ---------------------------------------------------------------------------


def test_post_error_notification(config, mock_apprise_setup):
    result = post_error_notification("Test error message", config)
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Error Notification"
    assert ":rotating_light: *Error Notification* :rotating_light:" in kwargs["body"]
    assert "Test error message" in kwargs["body"]
    assert "Network: `Ethereum`" in kwargs["body"]


def test_post_error_notification_no_config_does_not_raise():
    """post_error_notification() with no config argument must not raise AttributeError."""
    # With config=None the function should log and return False cleanly.
    result = post_error_notification("Test error without config")
    assert result is False


def test_post_liquidation_opportunity_notification(config, mock_apprise_setup):
    # Empty liquidation_data ({} is falsy) → falls back to the simple one-liner body.
    result = post_liquidation_opportunity_notification("0xTestVault", {}, (), config)
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Profitable Liquidation Opportunity Detected"
    assert "Liquidation detected for vault 0xTestVault" in kwargs["body"]


def test_post_liquidation_result_notification(config, mock_apprise_setup):
    result = post_liquidation_result_notification(
        "0xTestVault",
        {
            "profit": 10,
            "collateral_address": "0x",
            "collateral_asset": "ETH",
        },
        "0x",
        config=config,
    )
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Liquidation Completed"
    body = kwargs["body"]
    assert ":moneybag: *Liquidation Completed* :moneybag:" in body
    assert "*Vault*: `0xTestVault`" in body
    # No receipt supplied — falls back to simulated profit estimate.
    assert "Profit (simulated estimate)" in body
    assert "• Collateral Vault Address:" in body
    assert "• Collateral Asset: `ETH`" in body
    assert "https://etherscan.io/tx/0x" in body


def test_post_liquidation_result_notification_with_receipt(config, mock_apprise_setup):
    receipt = _liquidation_receipt(USDC, amount_repaid=315_410_021, amount_profit=4_554_175, status=1)
    result = post_liquidation_result_notification(
        "0xTestVault",
        {"profit": 10, "collateral_address": "0x", "collateral_asset": "ETH"},
        "0xabc",
        config=config,
        liq_tx_receipt=receipt,
    )
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Liquidation Completed"
    body = kwargs["body"]
    assert ":moneybag: *Liquidation Completed* :moneybag:" in body
    assert "*Vault*: `0xTestVault`" in body
    # Receipt is present — realized profit is decoded from the Liquidation event.
    assert "Realized Profit" in body
    assert "Gas Spent" in body
    assert "https://etherscan.io/tx/0xabc" in body


def test_post_liquidation_failed_notification(config, mock_apprise_setup):
    receipt = {"status": 0, "gasUsed": 1_385_647, "effectiveGasPrice": 5_009_815_496, "logs": []}
    result = post_liquidation_failed_notification(
        "0xTestVault",
        {"profit": 0, "collateral_address": "0x", "collateral_asset": "ETH"},
        "0xdef",
        config=config,
        liq_tx_receipt=receipt,
    )
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Liquidation Failed"
    body = kwargs["body"]
    assert ":x: *Liquidation FAILED — reverted on-chain* :x:" in body
    assert "*Vault*: `0xTestVault`" in body
    assert "REVERTED" in body
    assert "Gas Lost" in body
    assert "https://etherscan.io/tx/0xdef" in body


def test_post_low_health_account_report_notification(config, dummy_vault, mock_apprise_setup):
    result = post_low_health_account_report_notification([[dummy_vault.address] + [0] * 6 + [dummy_vault]], config)
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Account Health Report"
    body = kwargs["body"]
    assert "*Account Health Report*" in body
    # At least one vault entry must appear in the low-health section.
    assert dummy_vault.address in body


def test_post_unhealthy_account_notification(config, mock_apprise_setup):
    result = post_unhealthy_account_notification(
        vault_address="0xTestVault",
        externally_liquidated=False,
        internal_health_score=0.5,
        external_health_score=0.3,
        internal_value_borrowed=1,
        external_value_borrowed=1.5,
        config=config,
    )
    assert result is True
    mock_apprise_setup.notify.assert_called_once()
    kwargs = mock_apprise_setup.notify.call_args.kwargs
    assert kwargs["title"] == "Unhealthy Account Detected"
    body = kwargs["body"]
    assert ":warning: *Unhealthy Account Detected* :warning:" in body
    assert "*Vault*: `0xTestVault`" in body
    assert "*Internal Health Score*: `0.5000`" in body
    assert "*External Health Score*: `0.3000`" in body
    assert "Network: `Ethereum`" in body


# ---------------------------------------------------------------------------
# Pure-logic tests — no network I/O (autouse mock is harmlessly active)
# ---------------------------------------------------------------------------


def test_hex_normalizes_topics():
    assert _hex("0xABCD") == "abcd"
    assert _hex("ABCD") == "abcd"
    assert _hex(Web3.keccak(text="x")) == Web3.keccak(text="x").hex().lower().removeprefix("0x")


def test_gas_cost_eth_exact_usd_none_without_oracle(config):
    receipt = {"gasUsed": 1_000_000, "effectiveGasPrice": 5_000_000_000}
    gas_eth, gas_usd = _gas_cost(receipt, config, account=None)
    assert gas_eth == Web3.from_wei(1_000_000 * 5_000_000_000, "ether")
    assert gas_usd is None


def test_realized_profit_decodes_event(config):
    receipt = _liquidation_receipt(USDC, amount_repaid=315_410_021, amount_profit=4_554_175)
    raw, symbol, human = _realized_profit(receipt, config)
    assert raw == 4_554_175
    # USDC resolves to 6 decimals on mainnet; if RPC metadata is unavailable, fall back is raw.
    assert symbol in ("USDC", "tokens")
    assert human in (4.554175, 4_554_175)


def test_realized_profit_returns_none_without_event(config):
    assert _realized_profit({"logs": []}, config) is None
