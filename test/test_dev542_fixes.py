"""
Tests for DEV-542 hot-path small fixes.

Covers:
  1. _build_liquidation_tx re-check → neither flag set returns ({"profit": 0}, None), no UnboundLocalError.
  2. decorators.retry_request: 4xx (non-429) not retried; 429 is retried.
  3. get_health_score: scaled HF comparison fires correctly; swapped log args fixed.
  4. /liquidation/allPositions?chainId=abc returns HTTP 400.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.liquidation.decorators import retry_request
from app.liquidation.gas import GasFees
from app.liquidation.profitability import USD
from app.liquidation.vaults import euler_vault

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_euler_internal_liquidation.py style)
# ---------------------------------------------------------------------------

LIQUIDATOR_EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
UNIT = "0x0000000000000000000000000000000000000348"
VAULT = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
COLLATERAL_ASSET = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"


def _oracle() -> MagicMock:
    oracle = MagicMock()
    oracle.functions.getQuote.return_value = SimpleNamespace(call=lambda: 1 * USD)
    return oracle


def _fees(effective_gas_price: int = 1) -> GasFees:
    return GasFees(
        max_fee_per_gas=effective_gas_price,
        max_priority_fee_per_gas=effective_gas_price,
        base_fee_at_pending=0,
    )


def _vault_recheck(can_liquidate: bool, externally_liquidated: bool) -> MagicMock:
    """Vault whose re-check inside _build_liquidation_tx returns the given flags."""
    v = MagicMock()
    v.address = VAULT
    v.unit_of_account = UNIT
    v.oracle_router = _oracle()
    v.check_liquidation.return_value = (can_liquidate, externally_liquidated, 0, 1_000, 2_000)
    build = MagicMock(return_value={"to": "0xliq", "data": "0x"})
    v.liqbot_instance.functions.liquidateCollateralVault.return_value.build_transaction = build
    v.liqbot_instance.functions.liquidateExtLiquidatedCollateralVault.return_value.build_transaction = build
    return v


def _config_simple() -> SimpleNamespace:
    eth = MagicMock()
    eth.estimate_gas.return_value = 10
    return SimpleNamespace(
        CHAIN_ID=1,
        LIQUIDATOR_EOA=LIQUIDATOR_EOA,
        WETH=SimpleNamespace(address=WETH),
        MIN_NET_PROFIT_USD=0,
        w3=SimpleNamespace(eth=eth),
    )


def _patch_build(monkeypatch):
    monkeypatch.setattr(euler_vault, "_calculate_swap_amount", lambda *a, **k: 1_000)
    monkeypatch.setattr(euler_vault, "_get_swap_data", lambda *a, **k: b"\x00" * 8)
    monkeypatch.setattr(euler_vault, "get_eip1559_fees", lambda *a, **k: _fees(1))


# ---------------------------------------------------------------------------
# Item 1 — _build_liquidation_tx re-check with neither flag set
# ---------------------------------------------------------------------------


def test_recheck_neither_flag_returns_no_profit_tuple(monkeypatch):
    """
    When the re-check inside _build_liquidation_tx returns (False, False, ...),
    the function must return ({"profit": 0}, None) without raising UnboundLocalError.
    """
    _patch_build(monkeypatch)
    vault = _vault_recheck(can_liquidate=False, externally_liquidated=False)
    result = euler_vault._build_liquidation_tx(
        vault, _config_simple(), True, False, 1_000, 0, 2_000, 50 * USD, COLLATERAL_ASSET
    )
    assert result == ({"profit": 0}, None)


def test_recheck_neither_flag_no_unbound_local_error(monkeypatch):
    """
    The same scenario must not raise any NameError / UnboundLocalError — the
    previous code left `liquidation_tx` unassigned before estimate_gas.
    """
    _patch_build(monkeypatch)
    vault = _vault_recheck(can_liquidate=False, externally_liquidated=False)
    try:
        euler_vault._build_liquidation_tx(
            vault, _config_simple(), True, False, 1_000, 0, 2_000, 50 * USD, COLLATERAL_ASSET
        )
    except (NameError, UnboundLocalError) as exc:
        pytest.fail(f"UnboundLocalError / NameError raised: {exc}")


# ---------------------------------------------------------------------------
# Item 2 — retry_request: 4xx not retried, 429 retried
# ---------------------------------------------------------------------------


def _http_error(status_code: int, body: str = "error") -> requests.HTTPError:
    """Build a requests.HTTPError with a mock response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = body
    mock_response.headers = {}
    err = requests.HTTPError(response=mock_response)
    return err


def test_4xx_non_429_not_retried():
    """A 400 Bad Request must NOT be retried — call count must be 1."""
    import logging

    call_count = 0

    @retry_request(logging.getLogger("test"), max_retries=3, delay=0)
    def _failing_func():
        nonlocal call_count
        call_count += 1
        raise _http_error(400)

    result = _failing_func()
    assert result is None
    assert call_count == 1, f"Expected 1 call (no retry), got {call_count}"


def test_404_not_retried():
    """A 404 Not Found must NOT be retried."""
    import logging

    call_count = 0

    @retry_request(logging.getLogger("test"), max_retries=3, delay=0)
    def _failing_func():
        nonlocal call_count
        call_count += 1
        raise _http_error(404)

    _failing_func()
    assert call_count == 1


def test_429_is_retried():
    """A 429 Too Many Requests MUST be retried (up to max_retries)."""
    import logging

    call_count = 0

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "rate limited"
    mock_response.headers = {}
    err = requests.HTTPError(response=mock_response)

    @retry_request(logging.getLogger("test"), max_retries=3, delay=0)
    def _failing_func():
        nonlocal call_count
        call_count += 1
        raise err

    with patch("time.sleep"):  # don't actually sleep
        _failing_func()

    assert call_count == 3, f"Expected 3 attempts for 429, got {call_count}"


def test_429_respects_retry_after_header():
    """When Retry-After header is present, the sleep duration must use that value."""
    import logging

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "rate limited"
    mock_response.headers = {"Retry-After": "42"}
    err = requests.HTTPError(response=mock_response)

    @retry_request(logging.getLogger("test"), max_retries=2, delay=10)
    def _failing_func():
        raise err

    sleep_calls = []
    with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        _failing_func()

    # First retry sleep should use the Retry-After value, not the default delay.
    assert 42 in sleep_calls, f"Expected sleep(42) from Retry-After header, got {sleep_calls}"


# ---------------------------------------------------------------------------
# Item 3 — get_health_score: scaled comparison and swapped log args
# ---------------------------------------------------------------------------


def _make_vault_stub_for_health(raw_external: int, raw_internal: int, internal_debt: int, external_debt: int):
    """Create a free-form MagicMock suitable for BaseCollateralVault.get_health_score."""
    vault_stub = MagicMock()
    vault_stub.address = VAULT
    vault_stub.health_state_viewer.functions.health.return_value.call.return_value = (
        raw_external,
        raw_internal,
        external_debt,
        internal_debt,
    )
    vault_stub.internal_health_score = 0.0
    vault_stub.external_health_score = 0.0
    vault_stub.internal_value_borrowed = 0
    vault_stub.external_value_borrowed = 0
    return vault_stub


def _capture_health_score_logs(vault_stub):
    """Call BaseCollateralVault.get_health_score on the stub and return log messages."""
    import logging

    from app.liquidation.logging_config import setup_logger
    from app.liquidation.vaults.base_vault import BaseCollateralVault

    captured = []

    class CapturingHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = CapturingHandler()
    lg = setup_logger()
    lg.addHandler(handler)
    try:
        BaseCollateralVault.get_health_score(vault_stub)
    finally:
        lg.removeHandler(handler)
    return captured


def test_health_score_liquidatable_log_fires_on_scaled_hf_below_1():
    """
    The 'can be liquidated!' log must fire when the SCALED HF (i.e. internalHF/1e18
    or externalHF/1e18) is < 1.  With raw internalHF = 0.9e18, the scaled value is
    0.9 — below 1.  The old code compared raw 0.9e18 < 1, which is False.
    """
    raw_internal = int(0.9 * 1e18)  # scaled = 0.9 → liquidatable
    raw_external = int(1.5 * 1e18)  # scaled = 1.5 → healthy
    vault_stub = _make_vault_stub_for_health(raw_external, raw_internal, 1_000_000, 2_000_000)
    msgs = _capture_health_score_logs(vault_stub)
    liquidatable_logs = [m for m in msgs if "can be liquidated" in m]
    assert liquidatable_logs, "Expected 'can be liquidated!' log to fire when scaled HF=0.9 < 1, but it did not."


def test_health_score_log_not_fire_when_both_hf_above_1():
    """The 'can be liquidated!' log must NOT fire when both HFs are above 1."""
    raw_internal = int(1.2 * 1e18)  # scaled = 1.2
    raw_external = int(1.3 * 1e18)  # scaled = 1.3
    vault_stub = _make_vault_stub_for_health(raw_external, raw_internal, 1_000_000, 2_000_000)
    msgs = _capture_health_score_logs(vault_stub)
    liquidatable_logs = [m for m in msgs if "can be liquidated" in m]
    assert not liquidatable_logs, f"'can be liquidated!' log should NOT fire when HF > 1, but got: {liquidatable_logs}"


# ---------------------------------------------------------------------------
# Item 4 — /liquidation/allPositions?chainId=abc returns 400
# ---------------------------------------------------------------------------


@pytest.fixture()
def routes_app():
    """Minimal Flask app with the liquidation blueprint — no monitor thread."""
    from flask import Flask

    from app.liquidation.routes import liquidation

    app = Flask(__name__)
    app.register_blueprint(liquidation, url_prefix="/liquidation")
    app.config["TESTING"] = True
    return app


def test_allpositions_invalid_chain_id_returns_400(routes_app):
    """Non-numeric chainId query param must return HTTP 400 (not 500)."""
    client = routes_app.test_client()
    response = client.get("/liquidation/allPositions?chainId=abc")
    assert response.status_code == 400, f"Expected 400 for non-numeric chainId, got {response.status_code}"
    data = response.get_json()
    assert "error" in data
    assert "Invalid chainId" in data["error"]


def test_allpositions_numeric_chain_id_does_not_400(routes_app):
    """A numeric chainId must not produce a 400 (may 500 if monitor is absent)."""
    client = routes_app.test_client()
    response = client.get("/liquidation/allPositions?chainId=1")
    # The monitor isn't running, so this will be 500 — but NOT 400.
    assert response.status_code != 400, "Numeric chainId should not produce a 400"
