"""
Slack notification functions for the liquidation bot.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from apprise import Apprise
from web3 import Web3

from .config_loader import ChainConfig
from .logging_config import setup_logger

logger = setup_logger()


def setup_apprise_notification_object(config: ChainConfig) -> Apprise:
    """Set up the Apprise notification engine."""
    apprise = Apprise()
    apprise.add(config.NOTIFICATION_URL)
    return apprise


def get_spy_link(account: str, config: ChainConfig) -> str:
    """
    Build a Twyne spy-mode URL for a given account.

    Args:
        account: The vault/account address.
        config: Chain configuration with EVC contract.

    Returns:
        Spy-mode URL string.
    """
    owner = config.evc.functions.getAccountOwner(account).call()
    if owner == "0x0000000000000000000000000000000000000000":
        owner = account

    subaccount_number = int(int(account, 16) ^ int(owner, 16))
    return f"https://app.twyne.xyz/account/{subaccount_number}?spy={owner}&chainId={config.CHAIN_ID}"


def _slack_mentions(config: ChainConfig) -> str:
    """Build Slack mention string from config."""
    mention_ids = getattr(config, "SLACK_MENTION_IDS", [])
    return " ".join(f"<@{uid}>" for uid in mention_ids)


def post_unhealthy_account_notification(
    vault_address: str,
    externally_liquidated: bool,
    internal_health_score: float,
    external_health_score: float,
    internal_value_borrowed: int,
    external_value_borrowed: int,
    config: ChainConfig,
) -> bool:
    """Post a Slack notification about an unhealthy account."""
    message = (
        ":warning: *Unhealthy Account Detected* :warning:\n\n"
        f"*Vault*: `{vault_address}`\n"
        f"*Externally Liquidated*: `{externally_liquidated}`\n"
        f"*Internal Health Score*: `{internal_health_score:.4f}`\n"
        f"*External Health Score*: `{external_health_score:.4f}`\n"
        f"*Internal Value Borrowed*: `${internal_value_borrowed / 10**18:.2f}`\n"
        f"*External Value Borrowed*: `${external_value_borrowed / 10**18:.2f}`\n"
        f"Time of detection: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}\n\n"
    )
    logger.info("Unhealthy account notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Unhealthy Account Detected")


def post_liquidation_opportunity_notification(
    vault_address: str, liquidation_data: Optional[Dict[str, Any]], params: Optional[Tuple[Any, ...]], config: ChainConfig
) -> bool:
    """Post a Slack notification about a profitable liquidation opportunity."""
    message = f"Liquidation detected for vault {vault_address}"
    if liquidation_data and params:
        collateral_vault, collateral_asset, max_target_repay, liquidator = params

        message = (
            ":rotating_light: *Profitable Liquidation Opportunity Detected* :rotating_light:\n\n"
            f"*Vault*: `{vault_address}`"
        )

        formatted_data = (
            f"*Liquidation Opportunity Details:*\n"
            f"• Profit: ${Web3.from_wei(liquidation_data['profit'], 'ether')}\n"
            f"• Collateral Vault Address: `{liquidation_data['collateral_address']}`\n"
            f"• Collateral Asset: `{liquidation_data['collateral_asset']}`\n"
            f"Time of detection: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}"
        )
        message += f"\n\n{formatted_data}"

    logger.info("Liquidation opportunity notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Profitable Liquidation Opportunity Detected")


def post_liquidation_result_notification(
    vault_address: str, liquidation_data: Optional[Dict[str, Any]], liq_tx_hash: Optional[str], config: ChainConfig
) -> bool:
    """Post a Slack notification about a completed liquidation."""
    message = (
        ":moneybag: *Liquidation Completed* :moneybag:\n\n"
        f"*Vault*: `{vault_address}`"
    )

    liq_tx_url = f"{config.EXPLORER_URL}/tx/{liq_tx_hash}"

    formatted_data = (
        f"*Liquidation Details:*\n"
        f"• Profit: ${Web3.from_wei(liquidation_data['profit'], 'ether')}\n"
        f"• Collateral Vault Address: `{liquidation_data['collateral_address']}`\n"
        f"• Collateral Asset: `{liquidation_data['collateral_asset']}`\n"
        f"• Liquidation Transaction: <{liq_tx_url}|View Transaction on Explorer>\n"
        f"Time of liquidation: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}"
    )
    message += f"\n\n{formatted_data}"
    logger.info("Liquidation result notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Liquidation Completed")


def post_low_health_account_report_notification(sorted_accounts: List[Tuple[str, float, ...]], config: ChainConfig) -> bool:
    """
    Post a report of accounts with low health scores to Slack.

    Args:
        sorted_accounts: List of tuples with account health data, sorted by health score ascending.
        config: Chain configuration.
    """
    twyne_eoa_vaults = set(v.lower() for v in getattr(config, "TWYNE_EOA_VAULTS", []))

    low_health_accounts = [
        (vault_addr, in_hs, ex_hs, bal, in_borrowed, ex_borrowed, symbol)
        for vault_addr, in_hs, ex_hs, bal, in_borrowed, ex_borrowed, symbol in sorted_accounts
        if (
            in_hs < config.SLACK_REPORT_HEALTH_SCORE
            or ex_hs < config.SLACK_REPORT_HEALTH_SCORE
            or str(vault_addr).lower() in twyne_eoa_vaults
        )
    ]

    total_internal_value_borrowed_value = sum(
        in_borrowed / 10**18 for _, _, _, _, in_borrowed, _, _ in sorted_accounts
    )

    message = "*Account Health Report*\n\n"

    if not low_health_accounts:
        message += f"No accounts with health score below `{config.SLACK_REPORT_HEALTH_SCORE}` detected.\n"
    else:
        for i, (vault_addr, in_hs, ex_hs, _, in_borrowed, ex_borrowed, symbol) in enumerate(low_health_accounts, start=1):
            formatted_in_hf = f"{in_hs:.4f}"
            formatted_ex_hf = f"{ex_hs:.4f}"
            formatted_value = f"{(in_borrowed + ex_borrowed) / 10**18:.2f}"
            spy_link = get_spy_link(vault_addr, config)

            label = " *Twyne EOA*" if str(vault_addr).lower() in twyne_eoa_vaults else ""
            message += (
                f"{i}. `{vault_addr}`{label} Internal health score: `{formatted_in_hf}`, "
                f"External health score: `{formatted_ex_hf}`, Total borrow value: `${formatted_value}`, "
                f"collateral asset: `{symbol}`, <{spy_link}|Spy Mode>\n"
            )

            if i >= 50:
                break

        usd_threshold = config.BORROW_VALUE_THRESHOLD / 1e6
        message += (
            f"\nTotal accounts with health score below `{config.SLACK_REPORT_HEALTH_SCORE}` "
            f"and value larger than `{usd_threshold}`: `{len(low_health_accounts)}`"
        )

    message += f"\nTotal Twyne reserved assets amount in USD across all `{len(sorted_accounts)}` collateral vaults: `${total_internal_value_borrowed_value:,.2f}`"
    message += f"\n<{config.RISK_DASHBOARD_URL}|Risk Dashboard>"
    message += f"\nTime of report: `{time.strftime('%Y-%m-%d %H:%M:%S')}`"
    message += f"\nNetwork: `{config.CHAIN_NAME}`"
    logger.info("Low health account report:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Account Health Report")


def post_error_notification(message: str, config: ChainConfig = None) -> bool:
    """Post an error notification to Slack."""
    error_message = f":rotating_light: *Error Notification* :rotating_light:\n\n{message}\n\n"
    error_message += f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    if config:
        error_message += f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}"

    logger.info("Error notification:\n%s", error_message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=error_message, title="Error Notification")
