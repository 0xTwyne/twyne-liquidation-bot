"""
Bucket-boundary tests for the correlation-aware cadence step functions.

Covers every HF threshold defined in config.yaml for both the correlated and
uncorrelated branches, plus the externally_liquidated short-circuit and the
is_correlated wiring (matching pair, non-matching, case-insensitive match).
"""

import math
import time
from unittest.mock import MagicMock

import pytest

from app.liquidation.vaults.base_vault import BaseCollateralVault


class MockVault(BaseCollateralVault):
    """Concrete BaseCollateralVault for unit tests — bypasses on-chain init."""

    protocol = "mock"

    def __init__(self, config, is_correlated: bool = False):
        self.config = config
        self.address = "0x" + "0" * 40
        self.time_of_next_update = 0
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        self.balance = 0
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0
        self.underlying_asset_address = None
        self.target_asset = None
        self.is_correlated = is_correlated
        self.instance = MagicMock()
        self.instance.functions.isExternallyLiquidated.return_value.call.return_value = False

    def _init_protocol_contracts(self, config):
        pass

    def get_collateral_for_borrower(self):
        return 0

    def simulate_liquidation(self):
        return (False, None, None)


# ---------------------------------------------------------------------------
# Correlated bucket boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hf,expected_bucket",
    [
        (0.5, "LIQ"),  # well below
        (0.99, "LIQ"),  # below boundary
        (1.00, "LIQ"),  # at HS_LIQUIDATION (inclusive)
        (1.001, "HIGH"),  # just above HS_LIQUIDATION
        (1.019, "HIGH"),  # just below HS_CORRELATED_HIGH
        (1.02, "SAFE"),  # at HS_CORRELATED_HIGH (exclusive lower bound for HIGH)
        (1.5, "SAFE"),  # well above
    ],
)
def test_bucket_correlated_boundaries(config, hf, expected_bucket):
    vault = MockVault(config, is_correlated=True)
    expected = config.cadence_correlated[expected_bucket]
    assert vault._bucket_correlated(hf, externally_liquidated=False) == expected


def test_bucket_correlated_externally_liquidated_forces_liq(config):
    vault = MockVault(config, is_correlated=True)
    expected = config.cadence_correlated["LIQ"]
    # Even at HF=2.0 (would normally be SAFE), externally_liquidated forces LIQ.
    assert vault._bucket_correlated(2.0, externally_liquidated=True) == expected


# ---------------------------------------------------------------------------
# Uncorrelated bucket boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hf,expected_bucket",
    [
        (0.5, "LIQ"),  # well below
        (1.019, "LIQ"),  # just below HS_CORRELATED_HIGH
        (1.02, "HIGH_1"),  # at HS_CORRELATED_HIGH
        (1.049, "HIGH_1"),  # just below HS_HIGH_RISK
        (1.05, "HIGH_2"),  # at HS_HIGH_RISK
        (1.099, "HIGH_2"),  # just below HS_MEDIUM_RISK
        (1.10, "MEDIUM"),  # at HS_MEDIUM_RISK
        (1.249, "MEDIUM"),  # just below HS_SAFE
        (1.25, "SAFE"),  # at HS_SAFE
        (2.0, "SAFE"),  # well above
    ],
)
def test_bucket_uncorrelated_boundaries(config, hf, expected_bucket):
    vault = MockVault(config, is_correlated=False)
    expected = config.cadence_uncorrelated[expected_bucket]
    assert vault._bucket_uncorrelated(hf, externally_liquidated=False) == expected


def test_bucket_uncorrelated_externally_liquidated_forces_liq(config):
    vault = MockVault(config, is_correlated=False)
    expected = config.cadence_uncorrelated["LIQ"]
    assert vault._bucket_uncorrelated(2.0, externally_liquidated=True) == expected


# ---------------------------------------------------------------------------
# get_time_of_next_update branches on is_correlated
# ---------------------------------------------------------------------------


def test_correlated_vault_at_hf_1_03_uses_correlated_safe(config):
    """A CV at HF=1.03 is SAFE for correlated, but would be HIGH_1 for uncorrelated."""
    correlated = MockVault(config, is_correlated=True)
    correlated.internal_health_score = 1.03
    correlated.external_health_score = 1.03
    correlated.internal_value_borrowed = int(1000e18)

    uncorrelated = MockVault(config, is_correlated=False)
    uncorrelated.internal_health_score = 1.03
    uncorrelated.external_health_score = 1.03
    uncorrelated.internal_value_borrowed = int(1000e18)

    now = time.time()
    c_gap = correlated.get_time_of_next_update() - now
    u_gap = uncorrelated.get_time_of_next_update() - now

    # Correlated CV at HF=1.03 -> SAFE (3600s), uncorrelated -> HIGH_1 (300s).
    # Correlated should be scheduled MUCH later than uncorrelated.
    assert c_gap > u_gap * 5, (
        f"Correlated CV at HF=1.03 ({c_gap:.0f}s) should be scheduled much later "
        f"than uncorrelated CV at the same HF ({u_gap:.0f}s)"
    )


def test_worse_of_internal_external_picks_bucket(config):
    """min(internal_gap, external_gap) wins — at-risk side dominates the cadence."""
    vault = MockVault(config, is_correlated=False)
    vault.internal_health_score = 2.0  # would be SAFE alone
    vault.external_health_score = 1.03  # HIGH_1
    vault.internal_value_borrowed = int(1000e18)

    now = time.time()
    gap = vault.get_time_of_next_update() - now

    expected = config.cadence_uncorrelated["HIGH_1"]
    assert gap < expected * 1.15, f"Should use the worse side (HIGH_1, ~{expected}s), got {gap:.0f}s"


# ---------------------------------------------------------------------------
# _compute_correlation wiring
# ---------------------------------------------------------------------------


def test_compute_correlation_matches_whitelist(config):
    """A vault whose (underlying, target_asset) is on the whitelist tags correlated."""
    pair = next(iter(config.correlated_pairs))
    vault = MockVault(config, is_correlated=False)
    vault.underlying_asset_address = pair[0]
    vault.target_asset = pair[1]
    assert vault._compute_correlation() is True


def test_compute_correlation_rejects_non_whitelist(config):
    vault = MockVault(config, is_correlated=False)
    vault.underlying_asset_address = "0x" + "1" * 40
    vault.target_asset = "0x" + "2" * 40
    assert vault._compute_correlation() is False


def test_compute_correlation_is_case_insensitive(config):
    """Lowercase input addresses must still resolve to the checksum whitelist."""
    pair = next(iter(config.correlated_pairs))
    vault = MockVault(config, is_correlated=False)
    vault.underlying_asset_address = pair[0].lower()
    vault.target_asset = pair[1].lower()
    assert vault._compute_correlation() is True


def test_compute_correlation_missing_addresses_returns_false(config):
    vault = MockVault(config, is_correlated=False)
    vault.underlying_asset_address = None
    vault.target_asset = None
    assert vault._compute_correlation() is False
