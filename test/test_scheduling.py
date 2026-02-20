"""
Tests for vault update scheduling logic.
"""

import math
import time
from unittest.mock import MagicMock

from app.liquidation.vaults.base_vault import BaseCollateralVault


class MockVault(BaseCollateralVault):
    """Concrete implementation of BaseCollateralVault for testing."""

    protocol = "mock"

    def __init__(self, config):
        # Skip the parent __init__ which calls _init_protocol_contracts
        self.config = config
        self.address = "0x" + "0" * 40
        self.time_of_next_update = 0
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        self.balance = 0
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0
        self.instance = MagicMock()
        self.instance.functions.isExternallyLiquidated.return_value.call.return_value = False

    def _init_protocol_contracts(self, config):
        pass

    def get_collateral_for_borrower(self):
        return 0

    def simulate_liquidation(self):
        return (False, None, None)


def test_empty_vault_scheduled_at_max_interval(config):
    """Empty vaults (no position) should be scheduled at MAX_UPDATE_INTERVAL_SECONDS, not -1."""
    vault = MockVault(config)
    vault.internal_health_score = math.inf
    vault.external_health_score = math.inf

    now = time.time()
    next_update = vault.get_time_of_next_update()

    # Should NOT be -1
    assert next_update != -1, "Empty vault should not have time_of_next_update = -1"

    # Should be in the future
    assert next_update > now, "Next update should be in the future"

    # Should be approximately MAX_UPDATE_INTERVAL_SECONDS from now (with 10% jitter)
    max_interval = config.MAX_UPDATE_INTERVAL_SECONDS
    expected_min = now + max_interval * 0.85  # Allow some margin
    expected_max = now + max_interval * 1.15

    assert expected_min < next_update < expected_max, (
        f"Empty vault should be scheduled ~{max_interval}s from now, "
        f"got {next_update - now}s"
    )


def test_vault_never_scheduled_more_than_max_interval(config):
    """No vault should ever be scheduled more than MAX_UPDATE_INTERVAL_SECONDS in the future."""
    max_interval = config.MAX_UPDATE_INTERVAL_SECONDS

    # Test with a "safe" vault (high health score, should use safe_time)
    vault = MockVault(config)
    vault.internal_health_score = 2.0  # Very safe
    vault.external_health_score = 2.0
    vault.internal_value_borrowed = int(1e18)  # Some debt
    vault.external_value_borrowed = int(1e18)

    now = time.time()
    next_update = vault.get_time_of_next_update()

    time_until_update = next_update - now

    # Should never exceed max interval (with jitter margin)
    assert time_until_update <= max_interval * 1.15, (
        f"Vault scheduled {time_until_update}s in future, "
        f"exceeds max interval {max_interval}s"
    )


def test_high_risk_vault_scheduled_soon(config):
    """High-risk vaults should be scheduled sooner than safe vaults."""
    # High risk vault
    high_risk_vault = MockVault(config)
    high_risk_vault.internal_health_score = 1.02  # Just above liquidation
    high_risk_vault.external_health_score = 1.02
    high_risk_vault.internal_value_borrowed = int(1000e18)  # $1000 debt
    high_risk_vault.external_value_borrowed = 0

    # Safe vault
    safe_vault = MockVault(config)
    safe_vault.internal_health_score = 2.0  # Very safe
    safe_vault.external_health_score = 2.0
    safe_vault.internal_value_borrowed = int(1000e18)
    safe_vault.external_value_borrowed = 0

    now = time.time()
    high_risk_update = high_risk_vault.get_time_of_next_update()
    safe_update = safe_vault.get_time_of_next_update()

    high_risk_gap = high_risk_update - now
    safe_gap = safe_update - now

    assert high_risk_gap < safe_gap, (
        f"High-risk vault should be scheduled sooner than safe vault. "
        f"High-risk: {high_risk_gap}s, Safe: {safe_gap}s"
    )


def test_time_of_next_update_always_positive(config):
    """time_of_next_update should always be a positive timestamp, never -1 or 0."""
    test_cases = [
        # (internal_hs, external_hs, internal_borrowed, external_borrowed, ext_liq, description)
        (math.inf, math.inf, 0, 0, False, "empty vault"),
        (1.5, 1.5, int(100e18), 0, False, "normal vault"),
        (0.95, 1.5, int(100e18), 0, False, "liquidatable vault"),
        (1.5, 0.95, 0, int(100e18), True, "externally liquidatable vault"),
    ]

    for internal_hs, external_hs, internal_borrowed, external_borrowed, ext_liq, description in test_cases:
        vault = MockVault(config)
        vault.internal_health_score = internal_hs
        vault.external_health_score = external_hs
        vault.internal_value_borrowed = internal_borrowed
        vault.external_value_borrowed = external_borrowed
        vault.instance.functions.isExternallyLiquidated.return_value.call.return_value = ext_liq

        next_update = vault.get_time_of_next_update()

        assert next_update > 0, f"time_of_next_update should be positive for {description}, got {next_update}"
        assert next_update > time.time(), f"time_of_next_update should be in the future for {description}"


def test_liquidatable_vault_scheduled_very_soon(config):
    """Vaults at or below liquidation threshold should be scheduled at LIQ interval."""
    vault = MockVault(config)
    vault.internal_health_score = 0.99  # Below liquidation threshold
    vault.external_health_score = 1.5
    vault.internal_value_borrowed = int(1000e18)
    vault.external_value_borrowed = 0

    now = time.time()
    next_update = vault.get_time_of_next_update()

    time_until_update = next_update - now

    # For SMALL position ($1000), LIQ time is 15 seconds
    # With 10% jitter, should be between ~13.5 and ~16.5 seconds
    assert time_until_update < 20, (
        f"Liquidatable vault should be scheduled within ~15s, got {time_until_update}s"
    )
