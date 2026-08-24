"""Direct unit tests for the shared money-math helpers consolidated in DEV-557 R2.

These cover the pricing-free pieces lifted out of euler_vault / aave_vault:
- ``decode_1inch_min_return`` — byte-slice decode of the 1inch v6 minReturn slot.
- ``external_release_and_c_new`` — release/collateral clamp shared by both protocols.

The protocol-specific pricing (Euler ``getQuote`` vs Aave ``latestAnswer``) stays in
the vault modules; here we exercise the shared skeleton under inputs that mirror both
pricing chains to prove the consolidated arithmetic is identical to the originals.
"""

import pytest

from app.liquidation.constants import ONEINCH_MIN_RETURN_END, ONEINCH_MIN_RETURN_OFFSET
from app.liquidation.profitability import external_release_and_c_new
from app.liquidation.swap_1inch import decode_1inch_min_return


def _swap_calldata(min_return: int, total_len: int = ONEINCH_MIN_RETURN_END) -> bytes:
    """Build encoded swap calldata of ``total_len`` bytes with ``min_return`` in slot."""
    data = bytearray(total_len)
    data[ONEINCH_MIN_RETURN_OFFSET:ONEINCH_MIN_RETURN_END] = min_return.to_bytes(32, "big")
    return bytes(data)


# --- decode_1inch_min_return ------------------------------------------------


def test_decode_known_calldata_blob():
    min_return = 123_456_789_000_000_000_000
    blob = _swap_calldata(min_return)
    assert decode_1inch_min_return(blob) == min_return


def test_decode_matches_raw_slice_for_padded_calldata():
    # A realistic >228-byte blob: the decode must read exactly the [196:228] slot,
    # bit-identical to the raw ``int.from_bytes`` the vaults used before.
    min_return = 2**200 + 7
    blob = _swap_calldata(min_return, total_len=400)
    assert decode_1inch_min_return(blob) == int.from_bytes(
        blob[ONEINCH_MIN_RETURN_OFFSET:ONEINCH_MIN_RETURN_END], "big"
    )


def test_decode_zero_min_return():
    assert decode_1inch_min_return(_swap_calldata(0)) == 0


def test_decode_too_short_raises_value_error():
    with pytest.raises(ValueError, match="too short to decode minReturn"):
        decode_1inch_min_return(bytes(ONEINCH_MIN_RETURN_END - 1))


# --- external_release_and_c_new ---------------------------------------------


def test_release_clamped_to_max_release():
    # collateral_balance - user_collateral_shares = 800; max_release caps it at 500.
    release_amount, c_new = external_release_and_c_new(
        collateral_balance=1_000, user_collateral_shares=200, max_release=500
    )
    assert release_amount == 500
    assert c_new == 500


def test_release_below_max_release_uses_full_credit():
    # collateral_balance - user_collateral_shares = 300 < max_release (1_000).
    release_amount, c_new = external_release_and_c_new(
        collateral_balance=1_000, user_collateral_shares=700, max_release=1_000
    )
    assert release_amount == 300
    assert c_new == 700


def test_release_euler_pricing_chain_inputs():
    # Mirrors euler _calculate_external_profit: user_collateral already capped to the
    # balance by the caller's ``min(...)``; the skeleton just clamps the release.
    collateral_balance = 10**18
    user_collateral = 4 * 10**17  # capped share count from convertToShares(getQuote(...))
    max_release = 3 * 10**17
    release_amount, c_new = external_release_and_c_new(collateral_balance, user_collateral, max_release)
    assert release_amount == 3 * 10**17  # min(6e17, 3e17)
    assert c_new == 7 * 10**17
    # liquidator_reward_shares = c_new - borrower_claim (computed locally per protocol)
    borrower_claim = 2 * 10**17
    assert c_new - borrower_claim == 5 * 10**17


def test_release_aave_pricing_chain_inputs():
    # Mirrors aave _build_external_liquidation: user_collateral_shares from the
    # Chainlink latestAnswer division, already min()-capped to the balance.
    collateral_balance = 5_000 * 10**8  # 8-decimal aToken-style shares
    user_collateral_shares = 1_000 * 10**8
    max_release = 10_000 * 10**8  # larger than the gap -> uses full gap
    release_amount, c_new = external_release_and_c_new(collateral_balance, user_collateral_shares, max_release)
    assert release_amount == 4_000 * 10**8
    assert c_new == 1_000 * 10**8


def test_release_user_collateral_equals_balance_zero_release():
    release_amount, c_new = external_release_and_c_new(
        collateral_balance=1_000, user_collateral_shares=1_000, max_release=500
    )
    assert release_amount == 0
    assert c_new == 1_000
