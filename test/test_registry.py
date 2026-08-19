"""
Tests for protocol classification (`app.liquidation.vaults.registry`).

The old detector probed `aToken()` on the CV proxy. The CV's EVC-style
guard reverts on direct view-function calls for **both** Aave- and
Euler-backed proxies (custom error selector 0x335c5fec), so the broad
`except Exception: return "euler"` arm silently classified every new
CV as Euler. The current detector compares `targetVault()` against the
configured Aave Pool address, which both proxies expose without the
guard. These tests pin both the new behaviour and the regression.
"""

from unittest.mock import MagicMock, patch

import pytest
from web3.exceptions import ContractCustomError

from app.liquidation.vaults import registry
from app.liquidation.vaults.registry import detect_protocol


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
    cfg.AAVE_CVAULT_ABI_PATH = "contracts/AaveV3CollateralVault.json"
    return cfg


def _patch_target_vault(mock_config, target_vault: str):
    """Patch create_contract_instance so detect_protocol sees a fixed targetVault()."""
    instance = MagicMock()
    instance.functions.targetVault.return_value.call.return_value = target_vault
    return patch.object(registry, "create_contract_instance", return_value=instance)


def test_aave_cv_detected_by_target_vault(mock_config):
    """targetVault() == AAVE_POOL → Aave."""
    with _patch_target_vault(mock_config, mock_config.AAVE_POOL):
        assert detect_protocol("0xC69daC4473Aa8eA19F32762d1dAb54D30bA7Cd91", mock_config) == "aave"


def test_euler_cv_detected_when_target_vault_differs(mock_config):
    """targetVault() = an EVault (not the Aave Pool) → Euler."""
    evault = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"
    with _patch_target_vault(mock_config, evault):
        assert detect_protocol("0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720", mock_config) == "euler"


def test_lowercase_address_normalised_before_web3_call(mock_config):
    """
    web3.py rejects non-checksum addresses with InvalidAddress, which the old
    detector swallowed via `except Exception` and silently returned "euler".
    The new detector must checksum the input itself.
    """
    with _patch_target_vault(mock_config, mock_config.AAVE_POOL) as mock_create:
        result = detect_protocol("0xc69dac4473aa8ea19f32762d1dab54d30ba7cd91", mock_config)
    assert result == "aave"
    # And confirm the address handed to web3 is the checksummed form.
    passed_address = mock_create.call_args.args[0]
    assert passed_address == "0xC69daC4473Aa8eA19F32762d1dAb54D30bA7Cd91"


def test_target_vault_case_insensitive_match(mock_config):
    """Aave Pool returned as lowercase should still match the checksummed config value."""
    with _patch_target_vault(mock_config, mock_config.AAVE_POOL.lower()):
        assert detect_protocol("0xC69daC4473Aa8eA19F32762d1dAb54D30bA7Cd91", mock_config) == "aave"


def test_rpc_revert_propagates(mock_config):
    """
    A real contract revert from the RPC must NOT be silently swallowed —
    that was the original failure mode. Callers should see the exception
    and decide (retry, alert, skip) instead of receiving a misleading
    "euler" default.
    """
    instance = MagicMock()
    instance.functions.targetVault.return_value.call.side_effect = ContractCustomError("0x335c5fec", "0x335c5fec")
    with patch.object(registry, "create_contract_instance", return_value=instance):
        with pytest.raises(ContractCustomError):
            detect_protocol("0xC69daC4473Aa8eA19F32762d1dAb54D30bA7Cd91", mock_config)
