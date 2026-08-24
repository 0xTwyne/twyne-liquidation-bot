"""Validate the state seeder against the live fork (DEV-579) — seeding only, no bot.

These prove each make_* lever reaches its target on-chain state before the full
bot pipeline is driven in test_e2e_liquidation.py.
"""

import pytest

from .state_seeder import _addr

EULER_LIQ_LTV = 9400
EULER_COLLATERAL = 5 * 10**18  # 5 WETH
EULER_BORROW = 8_000 * 10**6  # 8000 USDC


@pytest.mark.e2e
def test_seed_euler_internal(seeder):
    h = seeder.create_euler_cv(_addr("eulerBorrower1"), EULER_LIQ_LTV, EULER_COLLATERAL, EULER_BORROW)
    assert not seeder.can_liquidate(h.cv), "fresh CV should be healthy before the drop"
    seeder.make_euler_internally_liquidatable(h)
    assert seeder.can_liquidate(h.cv)
    assert not seeder.is_externally_liquidated(h.cv)
    assert seeder.max_repay(h.cv) > 0
