"""DEV-661: Twyne 1.0.7 adaptation of the bot.

Covers, all with mocked RPC (no network):
  - the VaultManager read is keyed on (intermediateVault, targetAsset) through
    ``liqParams``, and ``get_max_twyne_ltv`` takes the second field;
  - a vault whose ``health()`` read keeps failing becomes ``health_unknown``,
    alerts once per ERROR_COOLDOWN, and recovers on the next good read;
  - a ``health_unknown`` vault opens the on-chain liquidation check, so a lens that
    reverts for every vault can never look like a healthy fleet;
  - the custom errors added by 1.0.7 decode by name in the log.
"""

import math
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv
from eth_utils import keccak

from app.liquidation import errors
from app.liquidation.account_monitor import AccountMonitor
from app.liquidation.config_loader import load_chain_config
from app.liquidation.vaults import base_vault as base_vault_module
from app.liquidation.vaults.base_vault import HEALTH_UNKNOWN_FAILURE_THRESHOLD, BaseCollateralVault

ADDR = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
INTERMEDIATE_VAULT = "0x87b8081A3ace680f35125F469526Ac10f5418Ca7"
TARGET_ASSET = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


@pytest.fixture()
def config():
    load_dotenv(dotenv_path=".env.example")
    return load_chain_config(1)


# --------------------------------------------------------------------------- #
# VaultManager liqParams keying
# --------------------------------------------------------------------------- #


def test_get_liq_params_is_keyed_on_intermediate_vault_and_target_asset():
    vault = MagicMock()
    vault.intermediate_vault_address = INTERMEDIATE_VAULT
    vault.target_asset = TARGET_ASSET
    vault.vault_manager.functions.liqParams.return_value.call.return_value = (9400, 10000, 0)

    assert BaseCollateralVault.get_liq_params(vault) == (9400, 10000, 0)
    vault.vault_manager.functions.liqParams.assert_called_once_with(INTERMEDIATE_VAULT, TARGET_ASSET)


def test_get_max_twyne_ltv_takes_the_second_liq_param():
    vault = MagicMock()
    vault.get_liq_params.return_value = (9400, 10000, 0)

    assert BaseCollateralVault.get_max_twyne_ltv(vault) == 10000


# --------------------------------------------------------------------------- #
# health_unknown state
# --------------------------------------------------------------------------- #


class _HealthVault(BaseCollateralVault):
    """A BaseCollateralVault whose health() read is scripted."""

    protocol = "mock"

    def __init__(self, config, health_returns):
        self.config = config
        self.address = ADDR
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        self.internal_health_score_raw = math.inf
        self.external_health_score_raw = math.inf
        self.health_read_failures = 0
        self.health_unknown = False
        self._health_unknown_alerted_at = None
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0
        self.health_state_viewer = MagicMock()
        self.health_state_viewer.functions.health.return_value.call.side_effect = health_returns

    def _init_protocol_contracts(self, config):
        pass

    def get_collateral_for_borrower(self):
        return 0

    def simulate_liquidation(self):
        return (False, None, None)


def _revert(times):
    return [ValueError("execution reverted") for _ in range(times)]


def test_single_health_failure_does_not_mark_unknown(config, monkeypatch):
    posted = MagicMock()
    monkeypatch.setattr(base_vault_module, "post_error_notification", posted)
    vault = _HealthVault(config, _revert(1))

    vault.get_health_score()

    assert vault.health_read_failures == 1
    assert vault.health_unknown is False
    posted.assert_not_called()


def test_repeated_health_failures_mark_unknown_and_alert_once(config, monkeypatch):
    posted = MagicMock()
    monkeypatch.setattr(base_vault_module, "post_error_notification", posted)
    vault = _HealthVault(config, _revert(HEALTH_UNKNOWN_FAILURE_THRESHOLD + 2))

    for _ in range(HEALTH_UNKNOWN_FAILURE_THRESHOLD + 2):
        assert vault.get_health_score() == (math.inf, math.inf)

    assert vault.health_unknown is True
    assert vault.health_read_failures == HEALTH_UNKNOWN_FAILURE_THRESHOLD + 2
    # One alert only: the rest fall inside ERROR_COOLDOWN.
    assert posted.call_count == 1
    assert "Health UNKNOWN" in posted.call_args[0][0]


def test_health_unknown_clears_after_a_good_read(config, monkeypatch):
    posted = MagicMock()
    monkeypatch.setattr(base_vault_module, "post_error_notification", posted)
    good = (2 * 10**18, 3 * 10**18, 1000, 1000)
    vault = _HealthVault(config, _revert(HEALTH_UNKNOWN_FAILURE_THRESHOLD) + [good])

    for _ in range(HEALTH_UNKNOWN_FAILURE_THRESHOLD):
        vault.get_health_score()
    assert vault.health_unknown is True

    vault.get_health_score()

    assert vault.health_unknown is False
    assert vault.health_read_failures == 0
    assert vault.internal_health_score_raw == 3 * 10**18
    assert posted.call_count == 2
    assert "recovered" in posted.call_args[0][0]


def test_health_unknown_is_reported_in_to_dict(config, monkeypatch):
    monkeypatch.setattr(base_vault_module, "post_error_notification", MagicMock())
    vault = _HealthVault(config, _revert(HEALTH_UNKNOWN_FAILURE_THRESHOLD))
    vault.protocol = "mock"
    vault.time_of_next_update = 0
    vault.is_correlated = False
    vault.underlying_asset_address = None
    vault.target_asset = TARGET_ASSET
    vault.balanceOf = 0
    vault.collateral_decimals, vault.collateral_symbol = 18, "eWETH"
    vault.debt_decimals, vault.debt_symbol = 6, "USDT"
    vault.underlying_asset_symbol = "WETH"
    vault._protocol_metadata = lambda: {}
    for _ in range(HEALTH_UNKNOWN_FAILURE_THRESHOLD):
        vault.get_health_score()

    assert vault.to_dict()["health_unknown"] is True


# --------------------------------------------------------------------------- #
# The liquidation gate must open when health is unknown
# --------------------------------------------------------------------------- #


def _account_with_unknown_health(health_unknown):
    account = MagicMock()
    account.time_of_next_update = 0
    account.update_liquidity.return_value = [math.inf, math.inf, False]
    account.internal_health_score_raw = math.inf
    account.external_health_score_raw = math.inf
    account.health_unknown = health_unknown
    account.target_asset = TARGET_ASSET
    account.check_liquidation.return_value = (False, False, 0, 0, 0)
    return account


@pytest.mark.parametrize("health_unknown,expect_check", [(False, False), (True, True)])
def test_gate_opens_only_when_health_is_unknown(config, health_unknown, expect_check):
    monitor = AccountMonitor(chain_id=1, config=config, notify=False, execute_liquidation=False)
    account = _account_with_unknown_health(health_unknown)
    monitor.accounts[ADDR] = account

    monitor.update_account_liquidity(ADDR)

    assert account.check_liquidation.called is expect_check


# --------------------------------------------------------------------------- #
# Custom error decoding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("signature", ["SubAccountBlocked()", "T_BorrowExceedsMaxLTV()", "EnforcedPause()"])
def test_new_1_0_7_errors_decode_by_name(signature):
    selector = "0x" + keccak(text=signature)[:4].hex()

    assert errors.error_name(selector) == signature
    assert errors.describe_revert(ValueError(f"execution reverted, data: {selector}")) == f" [{signature}]"


def test_describe_revert_is_empty_for_an_unknown_selector():
    assert errors.describe_revert(ValueError("execution reverted, data: 0xdeadbeef")) == ""
