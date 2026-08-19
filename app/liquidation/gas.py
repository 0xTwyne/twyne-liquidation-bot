"""
EIP-1559 fee suggestion for liquidation transactions.

Returns a `GasFees` namedtuple containing maxFeePerGas, maxPriorityFeePerGas, and
the pending baseFee. Callers spread the type-2 fields into build_transaction()
via `to_tx_fields()` and use `expected_effective_gas_price` for net-profit math.
"""

import os
from typing import NamedTuple, Optional

from web3 import Web3

_DEFAULT_TIP_GWEI = float(os.environ.get("LIQ_MIN_PRIORITY_FEE_GWEI", "1.0"))
_DEFAULT_BASEFEE_MULT = float(os.environ.get("LIQ_BASEFEE_MULTIPLIER", "2.0"))


class GasFees(NamedTuple):
    """EIP-1559 gas parameters for a single tx submission."""

    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    base_fee_at_pending: int

    @property
    def expected_effective_gas_price(self) -> int:
        """Realistic per-gas cost the EOA will pay = pending baseFee + tip."""
        return self.base_fee_at_pending + self.max_priority_fee_per_gas

    def to_tx_fields(self) -> dict:
        """Spread into a build_transaction() dict for a type-2 envelope."""
        return {
            "maxFeePerGas": self.max_fee_per_gas,
            "maxPriorityFeePerGas": self.max_priority_fee_per_gas,
        }


def get_eip1559_fees(
    w3: Web3,
    tip_gwei: Optional[float] = None,
    base_mult: Optional[float] = None,
) -> GasFees:
    """
    Suggest EIP-1559 fees from current network state.

    Args:
        w3: Web3 instance.
        tip_gwei: Floor for the priority fee in gwei. Defaults to env
            LIQ_MIN_PRIORITY_FEE_GWEI (1.0).
        base_mult: Multiplier on the pending baseFee for maxFeePerGas headroom.
            Defaults to env LIQ_BASEFEE_MULTIPLIER (2.0).

    Returns:
        GasFees with maxFeePerGas, maxPriorityFeePerGas, base_fee_at_pending.
    """
    tip_gwei = tip_gwei if tip_gwei is not None else _DEFAULT_TIP_GWEI
    base_mult = base_mult if base_mult is not None else _DEFAULT_BASEFEE_MULT

    pending = w3.eth.get_block("pending")
    base_fee = int(pending.get("baseFeePerGas", 0) or 0)

    try:
        suggested_tip = int(w3.eth.max_priority_fee or 0)
    except Exception:
        suggested_tip = 0
    min_tip = int(w3.to_wei(tip_gwei, "gwei"))
    tip = max(suggested_tip, min_tip)

    max_fee = int(base_fee * base_mult) + tip
    return GasFees(
        max_fee_per_gas=max_fee,
        max_priority_fee_per_gas=tip,
        base_fee_at_pending=base_fee,
    )


def bump_fees(fees: GasFees, factor: float) -> GasFees:
    """
    Return a new GasFees with both maxFeePerGas and maxPriorityFeePerGas scaled
    by `factor`. Used for replacement transactions on TimeExhausted retry —
    geth/erigon require both fields to be ≥10% higher than the original.
    """
    return GasFees(
        max_fee_per_gas=int(fees.max_fee_per_gas * factor),
        max_priority_fee_per_gas=int(fees.max_priority_fee_per_gas * factor),
        base_fee_at_pending=fees.base_fee_at_pending,
    )
