"""
USD-denominated profitability math for liquidation simulation.

Liquidation profit must be evaluated in a single unit of account (USD), not by
subtracting gas — denominated in the chain's gas token (ETH) — from a swap
surplus denominated in the position's target asset. Those are different units,
so the naive ``(min_return - max_repay) - gas_cost`` is only correct when the
target asset happens to be WETH at ~1:1 with ETH. For any other target (USDC,
USDT, DAI, ...) it silently mis-gates.

This module prices both legs through the Twyne/Euler oracle router
(``getQuote(amount, token, unitOfAccount)``, the same primitive the bot already
uses to value collateral and gas), so the gate is correct for any target asset.

Both the returned profit and the configured threshold are integers scaled by
``USD`` (1e18), matching how the rest of the bot stores unit-of-account values
(e.g. ``PositionStats`` and the notification "Profit: $..." line, which divides
the stored value by 1e18).
"""

import os

from app.liquidation.logging_config import setup_logger

logger = setup_logger()

# Unit-of-account scaling returned by the Euler oracle router getQuote().
USD = 10**18

# Default off-chain net-profit floor in whole USD when a chain config does not
# set MIN_NET_PROFIT_USD. May be NEGATIVE to intentionally accept a small USD
# loss in order to clear a liquidatable position (dust / bad-debt cleanup).
_DEFAULT_MIN_NET_PROFIT_USD = float(os.environ.get("LIQ_MIN_NET_PROFIT_USD", "0"))

# Base chain id — USDS-debt positions are skipped for *liquidation* only on Base.
_BASE_CHAIN_ID = 8453


def should_skip_usds_liquidation(chain_id: int, target_asset: str, usds_address: str) -> bool:
    """Whether a position's liquidation should be skipped because it is USDS debt on Base.

    USDS-debt positions on Base (chain 8453) must skip *liquidation* while remaining on
    the normal monitoring cadence (DEV-553). The scope is intentionally Base-only:
    ``usds_address`` is a single global config value applied across all chains, so a
    mainnet target asset can never equal the Base USDS address in practice, but pinning
    the predicate to chain 8453 keeps the "on Base" intent explicit.
    """
    return chain_id == _BASE_CHAIN_ID and target_asset.lower() == usds_address.lower()


def value_in_usd(oracle_router, amount: int, token: str, unit_of_account: str) -> int:
    """Value ``amount`` of ``token`` in the unit of account (USD, 1e18-scaled).

    Returns 0 for non-positive amounts. Propagates any oracle error so the caller
    can skip rather than act on an unknown valuation.
    """
    if amount <= 0:
        return 0
    return int(oracle_router.functions.getQuote(int(amount), token, unit_of_account).call())


def net_liquidation_profit_usd(
    oracle_router,
    unit_of_account: str,
    target_asset: str,
    weth_address: str,
    gross_profit_target: int,
    gas_wei: int,
) -> int:
    """Net liquidation profit in USD (1e18-scaled).

    Args:
        gross_profit_target: swap surplus over debt repayment, in target-asset units.
        gas_wei: estimated gas cost in wei (the chain's gas token, i.e. ETH/WETH).

    Both legs are converted to the unit of account before subtracting, so the
    result is a true USD figure regardless of the target asset's identity/decimals.
    """
    gross_usd = value_in_usd(oracle_router, gross_profit_target, target_asset, unit_of_account)
    gas_usd = value_in_usd(oracle_router, gas_wei, weth_address, unit_of_account)
    return gross_usd - gas_usd


def external_release_and_c_new(
    collateral_balance: int, user_collateral_shares: int, max_release: int
) -> tuple[int, int]:
    """Shared external-liquidation release math (integer-exact, no pricing).

    Given the vault's collateral share ``collateral_balance``, the share amount the
    borrower keeps (``user_collateral_shares``, already capped to the balance by the
    caller), and the credit ``max_release``, returns ``(release_amount, c_new)``:

    - ``release_amount`` — credit shares released, clamped to ``max_release``.
    - ``c_new`` — collateral shares remaining after release.

    The two pricing primitives (Euler oracle ``getQuote`` vs Aave Chainlink
    ``latestAnswer``) and the ``collateralForBorrower`` / reward valuation stay in the
    protocol vault modules; only this pricing-free clamp/subtraction is shared, so
    consolidation introduces no numeric change (DEV-557 R2 fallback).
    """
    release_amount = min(collateral_balance - user_collateral_shares, max_release)
    c_new = collateral_balance - release_amount
    return release_amount, c_new


def min_net_profit_usd_raw(config) -> int:
    """Configured minimum net profit as a 1e18-scaled USD int.

    Resolves ``config.MIN_NET_PROFIT_USD`` (whole USD, may be negative), falling
    back to the LIQ_MIN_NET_PROFIT_USD env default. A negative value means the
    bot will execute liquidations at up to that USD loss to clear the position.
    """
    usd = getattr(config, "MIN_NET_PROFIT_USD", None)
    if usd is None:
        usd = _DEFAULT_MIN_NET_PROFIT_USD
    return int(float(usd) * USD)
