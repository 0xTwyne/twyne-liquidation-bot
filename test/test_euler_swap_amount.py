"""Tests for _calculate_swap_amount in app/liquidation/vaults/euler_vault.py.

Covers all five behaviour branches:
- can_liquidate=True (normal + tiny amount where margin rounds to zero)
- externally_liquidated=True with max_repay==0 (early return)
- externally_liquidated=True with max_repay>0 (full oracle walk)
- can_liquidate=False AND externally_liquidated=False (returns 0)
"""

from unittest.mock import MagicMock

from app.liquidation.vaults.euler_vault import _calculate_swap_amount

VAULT_ADDR = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"
ASSET_ADDR = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
TARGET_ASSET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
UNDERLYING_ASSET = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
UNIT_OF_ACCOUNT = "0x0000000000000000000000000000000000000348"


def _make_vault() -> MagicMock:
    """Build a minimal vault MagicMock with all contract sub-attributes."""
    vault = MagicMock()
    vault.address = VAULT_ADDR
    vault.asset_address = ASSET_ADDR
    vault.target_asset = TARGET_ASSET
    vault.underlying_asset_address = UNDERLYING_ASSET
    vault.unit_of_account = UNIT_OF_ACCOUNT
    return vault


# ---------------------------------------------------------------------------
# can_liquidate=True path
# ---------------------------------------------------------------------------


class TestCanLiquidatePath:
    def test_normal_amount_applies_safety_margin(self):
        """Safety margin = amount // 1000; returned amount = amount - margin."""
        vault = _make_vault()

        C_for_B = 500_000
        total_assets = 2_000_000
        max_release = 300_000
        # remaining_shares = (total_assets - max_release) - C_for_B
        #                  = (2_000_000 - 300_000) - 500_000 = 1_200_000
        expected_remaining_shares = 1_200_000

        vault.get_collateral_for_borrower.return_value = C_for_B
        vault.asset.functions.convertToAssets.return_value.call.return_value = 1_000_000

        result = _calculate_swap_amount(
            vault,
            can_liquidate=True,
            externally_liquidated=False,
            max_repay=0,
            max_release=max_release,
            total_assets=total_assets,
        )

        # amount=1_000_000, margin=1_000 → 999_000
        assert result == 999_000

        # convertToAssets must be called with the exact remaining_shares
        vault.asset.functions.convertToAssets.assert_called_once_with(expected_remaining_shares)

    def test_tiny_amount_margin_rounds_to_zero(self):
        """When amount < 1000 the integer division yields 0 and amount is returned unchanged."""
        vault = _make_vault()

        C_for_B = 100
        total_assets = 1_000
        max_release = 400
        # remaining_shares = (1_000 - 400) - 100 = 500
        expected_remaining_shares = 500

        vault.get_collateral_for_borrower.return_value = C_for_B
        vault.asset.functions.convertToAssets.return_value.call.return_value = 500

        result = _calculate_swap_amount(
            vault,
            can_liquidate=True,
            externally_liquidated=False,
            max_repay=0,
            max_release=max_release,
            total_assets=total_assets,
        )

        # 500 // 1000 == 0, so margin = 0 → returns 500 unchanged
        assert result == 500
        vault.asset.functions.convertToAssets.assert_called_once_with(expected_remaining_shares)

    def test_convertToAssets_called_with_int(self):
        """remaining_shares is cast to int before being passed to convertToAssets."""
        vault = _make_vault()
        vault.get_collateral_for_borrower.return_value = 0
        vault.asset.functions.convertToAssets.return_value.call.return_value = 2_000

        _calculate_swap_amount(
            vault,
            can_liquidate=True,
            externally_liquidated=False,
            max_repay=0,
            max_release=0,
            total_assets=1_000,
        )

        # The sole argument to convertToAssets must be a plain int
        (pos_arg,) = vault.asset.functions.convertToAssets.call_args.args
        assert isinstance(pos_arg, int)


# ---------------------------------------------------------------------------
# externally_liquidated=True, max_repay==0 path
# ---------------------------------------------------------------------------


class TestExternallyLiquidatedZeroRepay:
    def test_returns_zero_immediately(self):
        vault = _make_vault()

        result = _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=0,
            max_release=5_000,
            total_assets=10_000,
        )

        assert result == 0

    def test_no_contract_calls_made(self):
        """With max_repay==0 the function must not touch any contract sub-attribute."""
        vault = _make_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=0,
            max_release=5_000,
            total_assets=10_000,
        )

        vault.get_max_twyne_ltv.assert_not_called()
        vault.oracle_router.functions.getQuote.assert_not_called()
        vault.asset.functions.balanceOf.assert_not_called()
        vault.asset.functions.convertToShares.assert_not_called()
        vault.target_vault.functions.accountLiquidity.assert_not_called()
        vault.instance.functions.collateralForBorrower.assert_not_called()


# ---------------------------------------------------------------------------
# externally_liquidated=True, max_repay>0 path
# ---------------------------------------------------------------------------


class TestExternallyLiquidatedWithRepay:
    def _setup_vault(self) -> tuple[MagicMock, dict]:
        """Return a fully-wired vault and the fixture numbers used."""
        vault = _make_vault()

        max_repay = 800_000
        max_release = 100_000
        MAXFACTOR = 10_000

        max_ltv = 8_000  # vault.get_max_twyne_ltv() -> liqParams(iv, targetAsset)[1]

        # user_collateral_underlying = oracle.getQuote(max_repay * MAXFACTOR // max_ltv, ...)
        # = oracle.getQuote(800_000 * 10_000 // 8_000, ...) = oracle.getQuote(1_000_000, ...)
        user_collateral_underlying_quote = 950_000

        collateral_balance = 2_000_000  # asset.balanceOf(vault.address)
        shares_for_collateral = 900_000  # asset.convertToShares(user_collateral_underlying)

        # user_collateral = min(2_000_000, 900_000) = 900_000
        # release_amount = min(2_000_000 - 900_000, 100_000) = min(1_100_000, 100_000) = 100_000
        # c_new = 2_000_000 - 100_000 = 1_900_000

        c_new_usd_quote = 1_800_000  # oracle.getQuote(c_new, asset_address, unit_of_account)
        debt_value_fresh = 500_000  # accountLiquidity returns (_, 500_000)
        borrower_claim = 1_600_000  # instance.collateralForBorrower(...)
        # liquidator_reward_shares = c_new - borrower_claim = 1_900_000 - 1_600_000 = 300_000
        final_amount = 280_000  # asset.convertToAssets(liquidator_reward_shares)

        # Wire the VaultManager read (liqParams(intermediateVault, targetAsset)[1])
        vault.get_max_twyne_ltv.return_value = max_ltv

        # Wire oracle_router for BOTH getQuote calls via side_effect
        first_getquote = MagicMock()
        first_getquote.call.return_value = user_collateral_underlying_quote
        second_getquote = MagicMock()
        second_getquote.call.return_value = c_new_usd_quote

        vault.oracle_router.functions.getQuote.side_effect = [first_getquote, second_getquote]

        # Wire asset sub-functions
        vault.asset.functions.balanceOf.return_value.call.return_value = collateral_balance
        vault.asset.functions.convertToShares.return_value.call.return_value = shares_for_collateral
        vault.asset.functions.convertToAssets.return_value.call.return_value = final_amount

        # Wire accountLiquidity → (ignored_surplus, debt_value_fresh)
        vault.target_vault.functions.accountLiquidity.return_value.call.return_value = (0, debt_value_fresh)

        # Wire collateralForBorrower
        vault.instance.functions.collateralForBorrower.return_value.call.return_value = borrower_claim

        params = {
            "max_repay": max_repay,
            "max_release": max_release,
            "MAXFACTOR": MAXFACTOR,
            "max_ltv": max_ltv,
            "user_collateral_underlying_quote": user_collateral_underlying_quote,
            "collateral_balance": collateral_balance,
            "shares_for_collateral": shares_for_collateral,
            "c_new_usd_quote": c_new_usd_quote,
            "debt_value_fresh": debt_value_fresh,
            "borrower_claim": borrower_claim,
            "final_amount": final_amount,
        }
        return vault, params

    def test_returns_final_convertToAssets_value(self):
        """The return value equals the final convertToAssets call result."""
        vault, params = self._setup_vault()

        result = _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=5_000_000,  # not used in this branch
        )

        assert result == params["final_amount"]

    def test_max_twyne_ltv_read_once_from_the_vault_helper(self):
        # DEV-579 / DEV-661: the max Twyne LTV comes from the shared helper, which reads
        # VaultManager.liqParams(intermediateVault, targetAsset). Keying that read on the
        # collateral asset returns 0 and divides-by-zero in the math below.
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        vault.get_max_twyne_ltv.assert_called_once_with()

    def test_first_getQuote_uses_scaled_repay(self):
        """First oracle call: getQuote(max_repay * MAXFACTOR // max_ltv, target_asset, underlying_asset)."""
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        expected_quote_amount = params["max_repay"] * params["MAXFACTOR"] // params["max_ltv"]
        first_call_args = vault.oracle_router.functions.getQuote.call_args_list[0].args
        assert first_call_args[0] == expected_quote_amount
        assert first_call_args[1] == vault.target_asset
        assert first_call_args[2] == vault.underlying_asset_address

    def test_second_getQuote_uses_c_new(self):
        """Second oracle call: getQuote(c_new, asset_address, unit_of_account)."""
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        # c_new = collateral_balance - release_amount
        # user_collateral = min(collateral_balance, shares_for_collateral) = 900_000
        # release_amount = min(collateral_balance - user_collateral, max_release) = 100_000
        # c_new = 2_000_000 - 100_000 = 1_900_000
        expected_c_new = 1_900_000
        second_call_args = vault.oracle_router.functions.getQuote.call_args_list[1].args
        assert second_call_args[0] == expected_c_new
        assert second_call_args[1] == vault.asset_address
        assert second_call_args[2] == vault.unit_of_account

    def test_accountLiquidity_called_with_vault_address(self):
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        vault.target_vault.functions.accountLiquidity.assert_called_once_with(vault.address, True)

    def test_collateralForBorrower_called_with_debt_and_c_new_usd(self):
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        vault.instance.functions.collateralForBorrower.assert_called_once_with(
            params["debt_value_fresh"], params["c_new_usd_quote"]
        )

    def test_liquidator_reward_shares_equals_c_new_minus_borrower_claim(self):
        """convertToAssets receives (c_new - borrower_claim) as the liquidator reward."""
        vault, params = self._setup_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=True,
            max_repay=params["max_repay"],
            max_release=params["max_release"],
            total_assets=0,
        )

        expected_c_new = 1_900_000
        expected_reward_shares = expected_c_new - params["borrower_claim"]  # 1_900_000 - 1_600_000 = 300_000
        vault.asset.functions.convertToAssets.assert_called_once_with(expected_reward_shares)


# ---------------------------------------------------------------------------
# Neither branch (returns 0)
# ---------------------------------------------------------------------------


class TestNeitherBranch:
    def test_both_false_returns_zero(self):
        vault = _make_vault()

        result = _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=False,
            max_repay=999_999,
            max_release=500_000,
            total_assets=2_000_000,
        )

        assert result == 0

    def test_both_false_no_contract_calls(self):
        vault = _make_vault()

        _calculate_swap_amount(
            vault,
            can_liquidate=False,
            externally_liquidated=False,
            max_repay=0,
            max_release=0,
            total_assets=0,
        )

        vault.asset.functions.convertToAssets.assert_not_called()
        vault.get_max_twyne_ltv.assert_not_called()
        vault.oracle_router.functions.getQuote.assert_not_called()
