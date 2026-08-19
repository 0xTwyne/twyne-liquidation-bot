// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {HealthStatViewer, ICollateralVaultBase, PositionStats} from "contracts/HealthStatViewer.sol";

interface ICollateralVaultFactory {
    function isCollateralVault(address collateralVault) external view returns (bool);
}

/// @dev Independent, correctly-ordered mirror of Aave V3 DataTypes.CollateralConfig,
/// used to cross-check the contract's externalLiqLTV against the pool's TRUE eMode
/// liquidation threshold (not its ltv).
struct AaveEModeCollateralConfig {
    uint16 ltv;
    uint16 liquidationThreshold;
    uint16 liquidationBonus;
}

interface IAaveEModeReader {
    function getEModeCategoryCollateralConfig(uint8 id) external view returns (AaveEModeCollateralConfig memory);
}

interface IAaveCvCategory {
    function categoryId() external view returns (uint8);
}

/// @dev Minimal getters (address-typed) used to independently reconstruct the real
/// remaining collateral and the maxTwyneLTV cap, without pulling the un-exported
/// IEVault/IVaultManager types from HealthStatViewer.sol into scope.
interface ICvLite {
    function asset() external view returns (address);

    function intermediateVault() external view returns (address);

    function twyneVaultManager() external view returns (address);

    function aToken() external view returns (address);
}

interface IVmLite {
    function maxTwyneLTVs(address intermediateVault) external view returns (uint16);
}

interface IERC20Bal {
    function balanceOf(address account) external view returns (uint256);
}

interface IATokenScaled {
    function scaledBalanceOf(address account) external view returns (uint256);
}

contract HealthStatViewerTest is Test {
    // Deployed HealthStatViewer addresses
    address constant MAINNET_HSV = 0x5A919b9A77ee391AB48208A93e0684c24F99B07a;
    address constant BASE_HSV = 0xe917E65288014092C5d813bb80989D72520606A0;

    // Aave V3 Pool
    address constant MAINNET_AAVE_POOL = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
    address constant BASE_AAVE_POOL = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;

    // Collateral vault factories
    address constant MAINNET_CV_FACTORY = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
    address constant BASE_CV_FACTORY = 0x1666FE8Cf509E6B6eC8c1bc6a53674f6Ee1D0381;

    HealthStatViewer hsv;

    function _deployHsv() internal {
        address aavePool = block.chainid == 1 ? MAINNET_AAVE_POOL : BASE_AAVE_POOL;
        hsv = new HealthStatViewer(aavePool);
    }

    function setUp() public {
        if (block.chainid != 1 && block.chainid != 8453) {
            revert("chainid not recognized");
        }
    }

    /// @notice Verify the deployed HealthStatViewer has correct aavePool set
    function testDeployedAavePool() public {
        if (block.chainid == 1) {
            HealthStatViewer deployed = HealthStatViewer(MAINNET_HSV);
            assertEq(deployed.aavePool(), MAINNET_AAVE_POOL, "mainnet aavePool mismatch");
        } else if (block.chainid == 8453) {
            HealthStatViewer deployed = HealthStatViewer(BASE_HSV);
            assertEq(deployed.aavePool(), BASE_AAVE_POOL, "base aavePool mismatch");
        }
    }

    /// @notice Deploy a fresh HealthStatViewer and verify constructor sets aavePool correctly
    function testDeployFresh() public {
        _deployHsv();
        address expected = block.chainid == 1 ? MAINNET_AAVE_POOL : BASE_AAVE_POOL;
        assertEq(hsv.aavePool(), expected, "aavePool should be set correctly");
    }

    /// @notice Test health() on a mainnet Euler vault with an active position (liquidatable)
    function testHealthEulerVaultMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;
        vm.label(vault, "EulerCollateralVault");

        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a valid collateral vault");

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(vault);

        console2.log("=== health() Euler Mainnet ===");
        console2.log("extHF:", extHF);
        console2.log("inHF:", inHF);
        console2.log("extDebt:", extDebt);
        console2.log("intDebt:", intDebt);

        assertGt(extDebt, 0, "external debt should be non-zero for active position");
    }

    /// @notice Test internalHF() on a mainnet Euler vault
    /// @dev This vault at block 23420000 has only external debt (no internal borrow),
    /// so internal liability is 0 and internal HF is type(uint).max.
    function testInternalHFEulerMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue) = hsv.internalHF(vault);

        console2.log("=== internalHF() Euler Mainnet ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        // This vault has no internal borrow at this block, so liability is 0
        // and health factor should be type(uint).max
        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint256).max, "zero liability should give max HF");
            assertGt(collateralValue, 0, "collateral should still be non-zero");
        } else {
            assertEq(healthFactor, (collateralValue * 1e18) / liabilityValue, "HF calculation mismatch");
        }
    }

    /// @notice Test externalHF() on a mainnet Euler vault
    function testExternalHFEulerMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue) = hsv.externalHF(vault);

        console2.log("=== externalHF() Euler Mainnet ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        // Euler vault, so targetVault != aavePool
        address targetVault = ICollateralVaultBase(vault).targetVault();
        assertTrue(targetVault != MAINNET_AAVE_POOL, "expected Euler target, not Aave");

        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint256).max, "zero liability should give max HF");
        } else {
            assertEq(healthFactor, (collateralValue * 1e18) / liabilityValue, "HF calculation mismatch");
        }
    }

    /// @notice Test health() returns max HF for a vault with zero external debt
    function testHealthZeroDebtReturnsMaxHF() public {
        if (block.chainid != 1) return;
        vm.rollFork(23517900);
        _deployHsv();

        address vault = 0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09;

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(vault);

        console2.log("=== health() Zero Debt ===");
        console2.log("extHF:", extHF);
        console2.log("inHF:", inHF);
        console2.log("extDebt:", extDebt);
        console2.log("intDebt:", intDebt);

        if (extDebt == 0) {
            assertEq(extHF, type(uint256).max, "extHF should be max when no external debt");
            assertEq(inHF, type(uint256).max, "inHF should be max when no external debt");
        }
    }

    /// @notice Test internalHF() returns max HF when liability is zero
    function testInternalHFZeroLiability() public {
        if (block.chainid != 1) return;
        vm.rollFork(23517900);
        _deployHsv();

        address vault = 0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09;

        (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue) = hsv.internalHF(vault);

        console2.log("=== internalHF() Zero Liability ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint256).max, "zero liability should give max HF");
        }
    }

    /// @notice Test health() on a Base Euler vault
    function testHealthEulerVaultBase() public {
        if (block.chainid != 8453) return;
        // Block 41325539 is after the CV factory deployment (38122653)
        vm.rollFork(41325539);
        _deployHsv();

        // Known-active Euler collateral vault on Base at the pinned block above.
        address vault = 0x23CEAd7E58D7d4aFadb4A617f6dA3937ADd6625c;
        vm.label(vault, "BaseEulerCollateralVault");

        assertTrue(ICollateralVaultFactory(BASE_CV_FACTORY).isCollateralVault(vault), "not a valid collateral vault");

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(vault);

        console2.log("=== health() Euler Base ===");
        console2.log("extHF:", extHF);
        console2.log("inHF:", inHF);
        console2.log("extDebt:", extDebt);
        console2.log("intDebt:", intDebt);

        assertGt(extDebt, 0, "external debt should be non-zero");
    }

    /// @notice Test that health() return values match what the Python bot expects:
    /// (extHF, inHF, externalBorrowDebtValue, internalBorrowDebtValue)
    /// The bot divides HFs by 1e18 and checks < 1.0 for liquidation.
    function testHealthReturnValuesMatchPythonBotExpectations() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        (uint256 extHF, uint256 inHF, uint256 extDebt,) = hsv.health(vault);

        // Python bot normalizes by dividing by 1e18
        // A healthy position has HF/1e18 > 1.0 (i.e., HF > 1e18)
        // A liquidatable position has HF/1e18 < 1.0 (i.e., HF < 1e18)

        // This vault was liquidatable at block 23420000, verify at least one HF < 1e18
        bool isLiquidatable = (extHF < 1e18) || (inHF < 1e18);
        assertTrue(isLiquidatable, "vault should be liquidatable at this block");

        assertGt(extDebt, 0, "external debt value should be positive");
    }

    /// @notice Test deploying a fresh HSV produces same results as the deployed contract
    /// at a block where the deployed HSV exists. Validates local code matches production.
    function testFreshDeploymentMatchesDeployed() public {
        if (block.chainid != 1) return;
        // Use a recent block where the deployed HSV exists
        // The deployed HSV is at 0x0dd9... on mainnet

        // We test at current block (no rollFork) since the deployed HSV exists now
        HealthStatViewer deployed = HealthStatViewer(MAINNET_HSV);
        HealthStatViewer fresh = new HealthStatViewer(MAINNET_AAVE_POOL);

        // Use one of the Twyne EOA vaults that should have a position
        address vault = 0xedA3564215b6BB516301b6cd213F56350088f02f;

        // Check if the vault has code (is a deployed contract)
        if (vault.code.length == 0) return;

        // Try calling - if vault has no position, both will return the same result
        (uint256 dExtHF, uint256 dInHF, uint256 dExtDebt, uint256 dIntDebt) = deployed.health(vault);
        (uint256 fExtHF, uint256 fInHF, uint256 fExtDebt, uint256 fIntDebt) = fresh.health(vault);

        assertEq(fExtHF, dExtHF, "extHF mismatch between fresh and deployed");
        assertEq(fInHF, dInHF, "inHF mismatch between fresh and deployed");
        assertEq(fExtDebt, dExtDebt, "extDebt mismatch between fresh and deployed");
        assertEq(fIntDebt, dIntDebt, "intDebt mismatch between fresh and deployed");
    }

    /// @notice Test all three view functions return consistent data for the same vault
    function testConsistencyBetweenFunctions() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        // Get internal HF
        (,, uint256 intLiability) = hsv.internalHF(vault);

        // Get health
        (,,, uint256 healthIntDebt) = hsv.health(vault);

        // internalHF() liability should match health() internalBorrowDebtValue
        assertEq(intLiability, healthIntDebt, "internal liability should match between functions");
    }

    // ---------------------------------------------------------------------
    // positionStats() — semantic confirmations (verified against the contract):
    //  1. Collateral native unit: collateral is sourced from the REAL balance
    //     (Euler IERC20(asset()).balanceOf, Aave IAToken(aToken()).scaledBalanceOf),
    //     with reservedCreditNative = min(maxRelease(), realBalance) and
    //     userCollateralNative = realBalance - reservedCreditNative — both in
    //     cv.asset() accounting units. In every NON-liquidated state realBalance ==
    //     totalAssetsDepositedOrReserved() (the external-liq detector's invariant),
    //     so this matches the prior totalAssets-based values exactly; it diverges
    //     (correctly) only after an external liquidation. The post-fallback
    //     divergence is exercised end-to-end by the twyne-sdk anvil-fork test.
    //  2. USD scale: Euler router.getQuote(.., unitOfAccount) returns 1e18-scaled
    //     USD; all *Usd fields are 1e18 (Aave 1e8 is scaled *1e10 to match).
    //  3. LTV scale: all five LTV fields (incl. maxTwyneLTV) are basis points vs
    //     MAXFACTOR = 1e4; twyneLiqLTV = min(chosen, maxTwyneLTV).
    //  4. Native-B getter (Euler): IEVault(targetVault).debtOf(collateralVault)
    //     returns native target-asset debt; debtOf is present in EVault.json ABI.
    // ---------------------------------------------------------------------

    /// @notice Test positionStats() on a mainnet Euler vault with an active position.
    function testPositionStatsEulerMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;
        vm.label(vault, "EulerCollateralVault");
        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a CV");

        PositionStats memory s = hsv.positionStats(vault);

        console2.log("C native:", s.userCollateralNative);
        console2.log("C usd:", s.userCollateralUsd);
        console2.log("C_LP native:", s.reservedCreditNative);
        console2.log("B native:", s.borrowNative);
        console2.log("B usd:", s.borrowUsd);
        console2.log("liqLTV_t:", s.twyneLiqLTV);
        console2.log("LTV_t:", s.twyneLTV);
        console2.log("liqLTV_e:", s.externalLiqLTV);
        console2.log("LTV_e:", s.externalLTV);

        (uint256 extHF, uint256 inHF, uint256 extDebt,) = hsv.health(vault);
        assertEq(s.borrowUsd, extDebt, "borrowUsd must equal health() externalBorrowDebtValue");
        assertEq(s.extHF, extHF, "extHF parity");
        assertEq(s.inHF, inHF, "inHF parity");
        assertGt(s.borrowUsd, 0, "active position has debt");
        assertLe(s.externalLiqLTV, 1e4, "liqLTV_e is bps <= MAXFACTOR");
        assertLe(s.twyneLiqLTV, 1e4, "liqLTV_t is bps <= MAXFACTOR");

        // Real-balance sourcing is a no-op for a live (non-liquidated) vault:
        // realBalance == totalAssetsDepositedOrReserved, so the native fields match
        // the legacy totalAssets-based values exactly.
        uint256 realBalance = IERC20Bal(ICvLite(vault).asset()).balanceOf(vault);
        assertEq(
            realBalance,
            ICollateralVaultBase(vault).totalAssetsDepositedOrReserved(),
            "live: realBalance == totalAssets (sourcing no-op)"
        );
        assertEq(s.userCollateralNative, realBalance - ICollateralVaultBase(vault).maxRelease(), "C native source");
        assertEq(s.reservedCreditNative, ICollateralVaultBase(vault).maxRelease(), "C_LP native source");
        // Reconstruction invariant: consumers recover the real remaining balance.
        assertEq(s.userCollateralNative + s.reservedCreditNative, realBalance, "C + C_LP == realBalance");

        // maxTwyneLTV is the protocol cap, and twyneLiqLTV is the chosen value clamped to it.
        uint16 cap = IVmLite(ICvLite(vault).twyneVaultManager()).maxTwyneLTVs(ICvLite(vault).intermediateVault());
        assertEq(s.maxTwyneLTV, cap, "maxTwyneLTV == maxTwyneLTVs(iv)");
        assertLe(s.twyneLiqLTV, s.maxTwyneLTV, "twyneLiqLTV <= maxTwyneLTV cap");
        assertLe(s.maxTwyneLTV, 1e4, "maxTwyneLTV bps <= MAXFACTOR");
        // NB: not asserting maxTwyneLTV > 0 here — at this pinned block the per-IV
        // maxTwyneLTVs mapping is still 0 for this vault (per-IV keying / ramp
        // postdate it), so twyneLiqLTV = min(chosen, 0) = 0 too. A populated cap is
        // covered by testPositionStatsAaveMainnet at a more recent block.

        // Smoke bound on operating LTV — well under any uint wrap, catches gross scaling regressions.
        assertLt(s.twyneLTV, 100000, "LTV_t sane");
    }

    /// @notice positionStats() on a REAL mainnet Aave V3 collateral vault with active debt.
    /// @dev Vault discovery: scanned the CV factory's `T_CollateralVaultCreated(address)`
    /// logs (0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332) from the factory deploy block
    /// and filtered for vaults whose targetVault() == MAINNET_AAVE_POOL. This vault
    /// (0x18D62a5E91ecAb839c7E8061B92D252df4258ED0, created block 25202484, awstETH
    /// intermediate vault 0x75029a47f28550C93Ad5A3BbD2d9b5315204B561) has an active
    /// Aave position at the pinned block 25260000 (collateral 7.66e23, debt 6.98e23 at 1e18 scale).
    /// @dev Exercises the Aave branch of positionStats()/health(), which routes through
    /// _aaveExtLiqLTV() -> getReserveData(). This decode requires the ReserveData struct to
    /// match Aave's live ReserveDataLegacy layout; see the struct comment in
    /// HealthStatViewer.sol (a wrong layout reverts here for every eMode Aave CV).
    function testPositionStatsAaveMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(25260000);
        _deployHsv();

        address vault = 0x18D62a5E91ecAb839c7E8061B92D252df4258ED0;
        vm.label(vault, "AaveCollateralVault");
        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a CV");
        // Confirms the Aave branch is the one under test.
        assertEq(ICollateralVaultBase(vault).targetVault(), MAINNET_AAVE_POOL, "expected Aave target");

        PositionStats memory s = hsv.positionStats(vault);
        (uint256 extHF, uint256 inHF, uint256 extDebt,) = hsv.health(vault);

        console2.log("=== positionStats() Aave Mainnet ===");
        console2.log("C native:", s.userCollateralNative);
        console2.log("C usd:", s.userCollateralUsd);
        console2.log("C_LP native:", s.reservedCreditNative);
        console2.log("B native:", s.borrowNative);
        console2.log("B usd:", s.borrowUsd);
        console2.log("liqLTV_e:", s.externalLiqLTV);
        console2.log("LTV_e:", s.externalLTV);

        // Parity with health().
        assertEq(s.borrowUsd, extDebt, "borrowUsd parity with health()");
        assertEq(s.extHF, extHF, "extHF parity");
        assertEq(s.inHF, inHF, "inHF parity");

        // LTV-parameter sanity (basis points <= MAXFACTOR).
        assertLe(s.externalLiqLTV, 1e4, "liqLTV_e bps <= MAXFACTOR");
        assertLe(s.twyneLiqLTV, 1e4, "liqLTV_t bps <= MAXFACTOR");

        // Real-balance sourcing (Aave: scaledBalanceOf(aToken)) is a no-op for a live
        // vault: realBalance == totalAssetsDepositedOrReserved (detector invariant).
        uint256 realBalance = IATokenScaled(ICvLite(vault).aToken()).scaledBalanceOf(vault);
        assertEq(
            realBalance,
            ICollateralVaultBase(vault).totalAssetsDepositedOrReserved(),
            "live: realBalance == totalAssets (sourcing no-op)"
        );
        assertEq(s.userCollateralNative, realBalance - ICollateralVaultBase(vault).maxRelease(), "C native source");
        assertEq(s.reservedCreditNative, ICollateralVaultBase(vault).maxRelease(), "C_LP native source");
        assertEq(s.userCollateralNative + s.reservedCreditNative, realBalance, "C + C_LP == realBalance");

        // maxTwyneLTV is the protocol cap.
        uint16 cap = IVmLite(ICvLite(vault).twyneVaultManager()).maxTwyneLTVs(ICvLite(vault).intermediateVault());
        assertEq(s.maxTwyneLTV, cap, "maxTwyneLTV == maxTwyneLTVs(iv)");
        assertLe(s.twyneLiqLTV, s.maxTwyneLTV, "twyneLiqLTV <= maxTwyneLTV cap");
        assertLe(s.maxTwyneLTV, 1e4, "maxTwyneLTV bps <= MAXFACTOR");

        // This vault has a confirmed active Aave position at the pinned block.
        assertGt(s.borrowUsd, 0, "active position has debt");
        assertGt(s.userCollateralUsd, 0, "active position has USD collateral");
        // Native debt comes from the borrowed targetAsset()'s variable debt token; it must
        // be non-zero for an active position (was 0 when read off the collateral asset).
        // Eyeball: borrowNative (borrowed-asset decimals) should be plausibly proportional
        // to borrowUsd (1e18) once scaled by the borrowed asset's price/decimals.
        assertGt(s.borrowNative, 0, "Aave native debt > 0");

        // eMode liq-threshold regression guard (sibling of the ReserveData fix): the
        // contract's externalLiqLTV must equal the pool's TRUE eMode liquidationThreshold,
        // NOT its ltv. Fetch the eMode config independently with a correctly-ordered struct.
        uint8 categoryId = IAaveCvCategory(vault).categoryId();
        assertTrue(categoryId != 0, "vault must be in an eMode category to exercise this path");
        AaveEModeCollateralConfig memory cfg =
            IAaveEModeReader(MAINNET_AAVE_POOL).getEModeCategoryCollateralConfig(categoryId);
        console2.log("eMode ltv:", cfg.ltv);
        console2.log("eMode liqThreshold:", cfg.liquidationThreshold);
        assertEq(s.externalLiqLTV, cfg.liquidationThreshold, "externalLiqLTV == eMode liqThreshold");
        // Regression guard: with the buggy CollateralConfig order this read ltv instead.
        // For category 45, ltv (9150) != liquidationThreshold (9350), so this asserts the fix.
        assertTrue(cfg.ltv != cfg.liquidationThreshold, "ltv and threshold must differ for a meaningful guard");
        assertTrue(s.externalLiqLTV != cfg.ltv, "externalLiqLTV must not be the eMode ltv (old buggy value)");
    }

    /// @notice positionStats() on a known zero-debt mainnet Euler vault.
    function testPositionStatsZeroDebt() public {
        if (block.chainid != 1) return;
        vm.rollFork(23517900);
        _deployHsv();

        address vault = 0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09;
        vm.label(vault, "ZeroDebtCollateralVault");

        PositionStats memory s = hsv.positionStats(vault);

        console2.log("=== positionStats() Zero Debt ===");
        console2.log("borrowUsd:", s.borrowUsd);
        console2.log("extHF:", s.extHF);
        console2.log("LTV_t:", s.twyneLTV);
        console2.log("LTV_e:", s.externalLTV);

        if (s.borrowUsd == 0) {
            assertEq(s.extHF, type(uint256).max, "extHF max on zero debt");
            assertEq(s.twyneLTV, 0, "LTV_t 0 on zero debt");
            assertEq(s.externalLTV, 0, "LTV_e 0 on zero debt");
        }
    }

    /// @notice positionStats() on a known active Base Euler vault.
    function testPositionStatsEulerBase() public {
        if (block.chainid != 8453) return;
        vm.rollFork(41325539);
        _deployHsv();

        address vault = 0x23CEAd7E58D7d4aFadb4A617f6dA3937ADd6625c;
        vm.label(vault, "BaseEulerCollateralVault");
        assertTrue(ICollateralVaultFactory(BASE_CV_FACTORY).isCollateralVault(vault), "not a CV");

        PositionStats memory s = hsv.positionStats(vault);
        (uint256 extHF, uint256 inHF, uint256 extDebt,) = hsv.health(vault);

        console2.log("=== positionStats() Euler Base ===");
        console2.log("borrowUsd:", s.borrowUsd);
        console2.log("extHF:", s.extHF);
        console2.log("inHF:", s.inHF);

        assertEq(s.borrowUsd, extDebt, "borrowUsd parity with health()");
        assertEq(s.extHF, extHF, "extHF parity");
        assertEq(s.inHF, inHF, "inHF parity");
    }

    /// @notice positionStats() clamps unbounded operating LTVs before uint32 casts.
    function testPositionStatsClampsUnboundedLTVsToUint32Max() public {
        MockHealthERC20 collateral = new MockHealthERC20();
        MockHealthRouter router = new MockHealthRouter();
        MockHealthVaultManager manager = new MockHealthVaultManager(address(router));
        MockHealthEVault targetVault = new MockHealthEVault(address(collateral), address(0x348));
        MockHealthEVault intermediateVault = new MockHealthEVault(address(collateral), address(0x348));
        uint256 debtValue = uint256(type(uint32).max) + 1;
        targetVault.setLiquidity(1, debtValue);
        targetVault.setDebt(debtValue);
        intermediateVault.setLiquidity(1, 0);

        MockHealthCollateralVault vault = new MockHealthCollateralVault(
            address(collateral), address(targetVault), address(intermediateVault), address(manager)
        );
        collateral.setBalance(address(vault), 1);

        hsv = new HealthStatViewer(makeAddr("aavePool"));
        PositionStats memory s = hsv.positionStats(address(vault));

        assertEq(s.userCollateralUsd, 1, "mock collateral quote");
        assertEq(s.borrowUsd, debtValue, "mock debt value");
        assertEq(s.twyneLTV, type(uint32).max, "twyneLTV clamped");
        assertEq(s.externalLTV, type(uint32).max, "externalLTV clamped");
    }

    /// @notice Test that externalHF() and health() return consistent external debt values
    function testExternalDebtConsistency() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        // externalHF returns raw liability from accountLiquidity
        (,, uint256 extLiability) = hsv.externalHF(vault);

        // health() returns externalBorrowDebtValue which is the same raw value for Euler vaults
        (,, uint256 healthExtDebt,) = hsv.health(vault);

        // For Euler vaults, both should get the debt from the same accountLiquidity call
        assertEq(extLiability, healthExtDebt, "external debt should match between externalHF and health");
    }
}

contract MockHealthERC20 {
    mapping(address => uint256) public balanceOf;

    function setBalance(address account, uint256 amount) external {
        balanceOf[account] = amount;
    }
}

contract MockHealthRouter {
    function getQuote(uint256, address, address) external pure returns (uint256) {
        return 1;
    }
}

contract MockHealthVaultManager {
    address public immutable oracleRouter;

    constructor(address oracleRouter_) {
        oracleRouter = oracleRouter_;
    }

    function externalLiqBuffers(address) external pure returns (uint16) {
        return 10_000;
    }

    function maxTwyneLTVs(address) external pure returns (uint16) {
        return 10_000;
    }
}

contract MockHealthEVault {
    address public immutable asset;
    address public immutable unitOfAccount;
    uint16 public ltvLiquidation = 9_000;
    uint256 public debt;
    uint256 public collateralValue;
    uint256 public liabilityValue;

    constructor(address asset_, address unitOfAccount_) {
        asset = asset_;
        unitOfAccount = unitOfAccount_;
    }

    function setDebt(uint256 debt_) external {
        debt = debt_;
    }

    function setLiquidity(uint256 collateralValue_, uint256 liabilityValue_) external {
        collateralValue = collateralValue_;
        liabilityValue = liabilityValue_;
    }

    function debtOf(address) external view returns (uint256) {
        return debt;
    }

    function accountLiquidity(address, bool) external view returns (uint256, uint256) {
        return (collateralValue, liabilityValue);
    }

    function LTVLiquidation(address) external view returns (uint16) {
        return ltvLiquidation;
    }
}

contract MockHealthCollateralVault {
    address public immutable asset;
    address public immutable targetVault;
    address public immutable intermediateVault;
    address public immutable twyneVaultManager;

    constructor(address asset_, address targetVault_, address intermediateVault_, address twyneVaultManager_) {
        asset = asset_;
        targetVault = targetVault_;
        intermediateVault = intermediateVault_;
        twyneVaultManager = twyneVaultManager_;
    }

    function totalAssetsDepositedOrReserved() external pure returns (uint256) {
        return 1;
    }

    function maxRelease() external pure returns (uint256) {
        return 0;
    }

    function twyneLiqLTV() external pure returns (uint256) {
        return 10_000;
    }
}
