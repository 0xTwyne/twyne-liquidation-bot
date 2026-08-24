"""
Slack notification functions for the liquidation bot.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from apprise import Apprise
from web3 import Web3

from .config_loader import ChainConfig
from .contracts import create_contract_instance
from .logging_config import setup_logger

logger = setup_logger()


def _hex(value: Any) -> str:
    """Normalize a topic/bytes value to a lowercase hex string without a 0x prefix."""
    if hasattr(value, "hex"):
        value = value.hex()
    value = str(value).lower()
    return value[2:] if value.startswith("0x") else value


# Liquidation event signatures emitted by TwyneLiquidator / TwyneAaveLiquidator.
# All three share the layout:
#   (address indexed violator, address repaidBorrowAsset,
#    address seizedCollateralAsset, uint256 amountRepaid, uint256 amountProfit)
_LIQUIDATION_EVENT_TOPICS = {
    _hex(Web3.keccak(text=sig))
    for sig in (
        "Liquidation(address,address,address,uint256,uint256)",
        "ExtLiqWithDebt(address,address,address,uint256,uint256)",
        "ExtLiqZeroDebt(address,address,address,uint256,uint256)",
    )
}


def _gas_cost(receipt: Any, config: ChainConfig, account: Any = None) -> Tuple[Any, Optional[Any]]:
    """Return ``(gas_cost_eth, gas_cost_usd_or_None)`` for a mined transaction receipt.

    The ETH figure is exact (``gasUsed * effectiveGasPrice``). The USD figure is a
    best-effort conversion using the same on-chain oracle the bot uses to value
    profit, and is ``None`` if no oracle is available or the quote fails.
    """
    gas_used = int(receipt.get("gasUsed", 0) or 0)
    gas_price = int(receipt.get("effectiveGasPrice", 0) or 0)
    gas_wei = gas_used * gas_price
    gas_eth = Web3.from_wei(gas_wei, "ether")

    gas_usd = None
    oracle = getattr(account, "oracle_router", None)
    unit = getattr(account, "unit_of_account", None)
    weth = getattr(config, "WETH", None)
    if oracle is not None and unit is not None and weth is not None and gas_wei > 0:
        try:
            usd_wei = oracle.functions.getQuote(gas_wei, weth.address, unit).call()
            gas_usd = Web3.from_wei(usd_wei, "ether")
        except Exception as ex:  # noqa: BLE001 - best-effort; never block a notification
            logger.warning("Could not value gas in USD via oracle: %s", ex)
    return gas_eth, gas_usd


def _realized_profit(receipt: Any, config: ChainConfig) -> Optional[Tuple[int, str, Any]]:
    """Decode the realized liquidation profit from a successful tx receipt.

    Returns ``(raw_amount, symbol, human_amount)`` parsed from the TwyneLiquidator
    ``Liquidation`` / ``ExtLiq*`` event (``amountProfit`` is denominated in the
    repaid borrow asset), or ``None`` if no such event is present.
    """
    for log in reversed(receipt.get("logs", []) or []):
        topics = log.get("topics", [])
        if not topics or _hex(topics[0]) not in _LIQUIDATION_EVENT_TOPICS:
            continue
        data_hex = _hex(log.get("data", ""))
        words = [data_hex[i : i + 64] for i in range(0, len(data_hex), 64)]
        if len(words) < 4:
            continue
        repaid_asset = Web3.to_checksum_address("0x" + words[0][24:])
        amount_profit = int(words[3], 16)
        symbol, human = "tokens", amount_profit
        try:
            token = create_contract_instance(repaid_asset, config.ERC20_ABI_PATH, config)
            decimals = token.functions.decimals().call()
            symbol = token.functions.symbol().call()
            human = amount_profit / (10**decimals)
        except Exception as ex:  # noqa: BLE001 - fall back to the raw integer
            logger.warning("Could not resolve repaid-asset metadata for %s: %s", repaid_asset, ex)
        return amount_profit, symbol, human
    return None


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
    stats=None,
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
    )
    if stats is not None:
        message += f"*Position*\n{stats.render_table()}\n"
    message += (
        f"Time of detection: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}\n\n"
    )
    logger.info("Unhealthy account notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Unhealthy Account Detected")


def post_liquidation_opportunity_notification(
    vault_address: str,
    liquidation_data: Optional[Dict[str, Any]],
    params: Optional[Tuple[Any, ...]],
    config: ChainConfig,
    stats=None,
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

    if stats is not None:
        message += f"\n*Position*\n{stats.render_table()}"

    logger.info("Liquidation opportunity notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Profitable Liquidation Opportunity Detected")


def post_liquidation_result_notification(
    vault_address: str,
    liquidation_data: Optional[Dict[str, Any]],
    liq_tx_hash: Optional[str],
    config: ChainConfig,
    stats=None,
    liq_tx_receipt=None,
    account=None,
) -> bool:
    """Post a Slack notification about a liquidation that succeeded on-chain.

    When ``liq_tx_receipt`` is provided, the *realized* profit is decoded from the
    liquidator's on-chain ``Liquidation`` event and the *actual* gas spent is
    reported, instead of the pre-trade simulated estimate.
    """
    message = f":moneybag: *Liquidation Completed* :moneybag:\n\n*Vault*: `{vault_address}`"

    liq_tx_url = f"{config.EXPLORER_URL}/tx/{liq_tx_hash}"

    # Prefer realized profit from the receipt; fall back to the simulated estimate.
    profit_line = None
    if liq_tx_receipt is not None:
        realized = _realized_profit(liq_tx_receipt, config)
        if realized is not None:
            _, symbol, human = realized
            profit_line = f"• Realized Profit: `{human} {symbol}`\n"
    if profit_line is None:
        simulated = liquidation_data.get("profit", 0) if liquidation_data else 0
        profit_line = f"• Profit (simulated estimate): ${Web3.from_wei(simulated, 'ether')}\n"

    gas_line = ""
    if liq_tx_receipt is not None:
        gas_eth, gas_usd = _gas_cost(liq_tx_receipt, config, account)
        gas_line = f"• Gas Spent: `{gas_eth} ETH`"
        gas_line += f" (~${gas_usd})\n" if gas_usd is not None else "\n"

    collateral_address = (liquidation_data or {}).get("collateral_address", "unknown")
    collateral_asset = (liquidation_data or {}).get("collateral_asset", "unknown")
    formatted_data = (
        f"*Liquidation Details:*\n"
        f"{profit_line}"
        f"{gas_line}"
        f"• Collateral Vault Address: `{collateral_address}`\n"
        f"• Collateral Asset: `{collateral_asset}`\n"
        f"• Liquidation Transaction: <{liq_tx_url}|View Transaction on Explorer>\n"
        f"Time of liquidation: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}"
    )
    message += f"\n\n{formatted_data}"

    if stats is not None:
        message += f"\n*Position*\n{stats.render_table()}"

    logger.info("Liquidation result notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Liquidation Completed")


def post_liquidation_failed_notification(
    vault_address: str,
    liquidation_data: Optional[Dict[str, Any]],
    liq_tx_hash: Optional[str],
    config: ChainConfig,
    liq_tx_receipt=None,
    account=None,
    stats=None,
) -> bool:
    """Post a Slack notification about a liquidation that REVERTED on-chain.

    The transaction mined but failed (``receipt.status == 0``), so no liquidation
    occurred and the gas was lost. Reports the wasted gas in ETH and (best-effort)
    USD so the loss is visible rather than mislabeled as a success.
    """
    message = f":x: *Liquidation FAILED — reverted on-chain* :x:\n\n*Vault*: `{vault_address}`"

    liq_tx_url = f"{config.EXPLORER_URL}/tx/{liq_tx_hash}"

    gas_line = "• Gas Lost: `unknown`\n"
    if liq_tx_receipt is not None:
        gas_eth, gas_usd = _gas_cost(liq_tx_receipt, config, account)
        gas_line = f"• Gas Lost: `{gas_eth} ETH`"
        gas_line += f" (~${gas_usd})\n" if gas_usd is not None else "\n"

    collateral_address = (liquidation_data or {}).get("collateral_address", vault_address)
    collateral_asset = (liquidation_data or {}).get("collateral_asset", "unknown")

    formatted_data = (
        f"*Failure Details:*\n"
        f"• Status: `REVERTED` (no liquidation occurred, no profit realized)\n"
        f"{gas_line}"
        f"• Collateral Vault Address: `{collateral_address}`\n"
        f"• Collateral Asset: `{collateral_asset}`\n"
        f"• Reverted Transaction: <{liq_tx_url}|View Transaction on Explorer>\n"
        f"Time of failure: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Network: `{config.CHAIN_NAME}` {_slack_mentions(config)}"
    )
    message += f"\n\n{formatted_data}"

    if stats is not None:
        message += f"\n*Position*\n{stats.render_table()}"

    logger.info("Liquidation failed notification:\n%s", message)

    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=message, title="Liquidation Failed")


def post_low_health_account_report_notification(
    sorted_accounts: List[Tuple[str, float, ...]], config: ChainConfig
) -> bool:
    """
    Post a report of accounts with low health scores to Slack.

    Args:
        sorted_accounts: List of tuples with account health data, sorted by health score ascending.
        config: Chain configuration.
    """
    twyne_eoa_vaults = set(v.lower() for v in getattr(config, "TWYNE_EOA_VAULTS", []))

    low_health_accounts = [
        (vault_addr, in_hs, ex_hs, bal, in_borrowed, ex_borrowed, symbol, account)
        for vault_addr, in_hs, ex_hs, bal, in_borrowed, ex_borrowed, symbol, account in sorted_accounts
        if (
            in_hs < config.SLACK_REPORT_HEALTH_SCORE
            or ex_hs < config.SLACK_REPORT_HEALTH_SCORE
            or str(vault_addr).lower() in twyne_eoa_vaults
        )
    ]

    total_internal_value_borrowed_value = sum(t[4] / 10**18 for t in sorted_accounts)

    message = "*Account Health Report*\n\n"

    if not low_health_accounts:
        message += f"No accounts with health score below `{config.SLACK_REPORT_HEALTH_SCORE}` detected.\n"
    else:
        for i, (vault_addr, in_hs, ex_hs, _, in_borrowed, ex_borrowed, symbol, account) in enumerate(
            low_health_accounts, start=1
        ):
            spy_link = get_spy_link(vault_addr, config)
            label = " *Twyne EOA*" if str(vault_addr).lower() in twyne_eoa_vaults else ""
            stats = account.get_position_stats()
            if stats is not None:
                message += f"{i}.{label} {stats.render_row(vault=vault_addr, spy_link=spy_link)}\n"
            else:
                formatted_in_hf = f"{in_hs:.4f}"
                formatted_ex_hf = f"{ex_hs:.4f}"
                formatted_value = f"{(in_borrowed + ex_borrowed) / 10**18:.2f}"
                message += (
                    f"{i}. `{vault_addr}`{label} Internal health score: `{formatted_in_hf}`, "
                    f"External health score: `{formatted_ex_hf}`, Total borrow value: `${formatted_value}`, "
                    f"collateral asset: `{symbol}`, <{spy_link}|Spy Mode>\n"
                )

            if i >= 50:
                break

        message += (
            f"\nTotal accounts with health score below `{config.SLACK_REPORT_HEALTH_SCORE}`: "
            f"`{len(low_health_accounts)}`"
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

    if config is None:
        return False
    apprise = setup_apprise_notification_object(config)
    return apprise.notify(body=error_message, title="Error Notification")
