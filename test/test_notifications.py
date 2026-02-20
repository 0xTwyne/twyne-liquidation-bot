"""
Tests for the notifications module.
"""

from app.liquidation.notifications import (
    post_error_notification,
    post_liquidation_opportunity_notification,
    post_liquidation_result_notification,
    post_low_health_account_report_notification,
    post_unhealthy_account_notification,
)


def test_post_error_notification(config):
    assert post_error_notification("Test error message", config)


def test_post_liquidation_opportunity_notification(config):
    assert post_liquidation_opportunity_notification("0xTestVault", {}, (), config)


def test_post_liquidation_result_notification(config):
    assert post_liquidation_result_notification(
        "0xTestVault",
        {
            "profit": 10,
            "collateral_address": "0x",
            "collateral_asset": "ETH",
        },
        "0x",
        config=config,
    )


def test_post_low_health_account_report_notification(config, dummy_vault):
    assert post_low_health_account_report_notification([[dummy_vault.address] + [0] * 6], config)


def test_post_unhealthy_account_notification(config):
    assert post_unhealthy_account_notification(
        vault_address="0xTestVault",
        externally_liquidated=False,
        internal_health_score=0.5,
        external_health_score=0.3,
        internal_value_borrowed=1,
        external_value_borrowed=1.5,
        config=config,
    )
