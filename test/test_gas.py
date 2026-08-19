"""
Tests for app.liquidation.gas EIP-1559 fee helper.
"""

from unittest.mock import MagicMock

from app.liquidation.gas import GasFees, bump_fees, get_eip1559_fees


def _mock_w3(base_fee_wei: int, max_priority_fee_wei: int):
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": base_fee_wei}
    w3.eth.max_priority_fee = max_priority_fee_wei
    w3.to_wei = lambda v, unit: int(v * 10**9) if unit == "gwei" else int(v)
    return w3


def test_uses_floor_when_network_tip_is_lower():
    w3 = _mock_w3(base_fee_wei=10**9, max_priority_fee_wei=10**8)  # baseFee=1 gwei, tip=0.1 gwei
    fees = get_eip1559_fees(w3, tip_gwei=1.0, base_mult=2.0)
    assert fees.max_priority_fee_per_gas == 10**9  # floored at 1 gwei
    assert fees.max_fee_per_gas == 2 * 10**9 + 10**9
    assert fees.base_fee_at_pending == 10**9


def test_uses_network_tip_when_above_floor():
    w3 = _mock_w3(base_fee_wei=10**9, max_priority_fee_wei=5 * 10**9)  # tip=5 gwei
    fees = get_eip1559_fees(w3, tip_gwei=1.0, base_mult=2.0)
    assert fees.max_priority_fee_per_gas == 5 * 10**9
    assert fees.max_fee_per_gas == 2 * 10**9 + 5 * 10**9


def test_expected_effective_gas_price_is_basefee_plus_tip():
    fees = GasFees(max_fee_per_gas=10**10, max_priority_fee_per_gas=10**9, base_fee_at_pending=2 * 10**9)
    assert fees.expected_effective_gas_price == 3 * 10**9


def test_to_tx_fields_uses_eip1559_keys():
    fees = GasFees(max_fee_per_gas=42, max_priority_fee_per_gas=7, base_fee_at_pending=1)
    fields = fees.to_tx_fields()
    assert fields == {"maxFeePerGas": 42, "maxPriorityFeePerGas": 7}


def test_bump_fees_scales_both_fee_fields():
    original = GasFees(max_fee_per_gas=100, max_priority_fee_per_gas=10, base_fee_at_pending=50)
    bumped = bump_fees(original, 1.5)
    assert bumped.max_fee_per_gas == 150
    assert bumped.max_priority_fee_per_gas == 15
    assert bumped.base_fee_at_pending == 50  # unchanged


def test_bump_fees_satisfies_geth_replacement_rule():
    # geth/erigon require ≥10% bump on both fields. 50% factor satisfies this with margin.
    original = GasFees(max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8, base_fee_at_pending=0)
    bumped = bump_fees(original, 1.5)
    assert bumped.max_fee_per_gas >= int(original.max_fee_per_gas * 1.10)
    assert bumped.max_priority_fee_per_gas >= int(original.max_priority_fee_per_gas * 1.10)
