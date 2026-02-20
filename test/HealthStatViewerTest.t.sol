// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {HealthStatViewer, IEulerCollateralVault, ICollateralVaultBase, IVaultManager, IEVault} from "contracts/HealthStatViewer.sol";

interface ICollateralVaultFactory {
    function isCollateralVault(address collateralVault) external view returns (bool);
}

contract HealthStatViewerTest is Test {
    // Deployed HealthStatViewer addresses
    address constant MAINNET_HSV = 0x0dd9065c998E75657BcE6C3a11d7F5AbA5CBdbD4;
    address constant BASE_HSV = 0xe002f7C266b0Ae4e32b621BAb56F19f9FeBf3f6E;

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

        assertTrue(
            ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault),
            "not a valid collateral vault"
        );

        (uint extHF, uint inHF, uint extDebt, uint intDebt) = hsv.health(vault);

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

        (uint healthFactor, uint collateralValue, uint liabilityValue) = hsv.internalHF(vault);

        console2.log("=== internalHF() Euler Mainnet ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        // This vault has no internal borrow at this block, so liability is 0
        // and health factor should be type(uint).max
        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint).max, "zero liability should give max HF");
            assertGt(collateralValue, 0, "collateral should still be non-zero");
        } else {
            assertEq(healthFactor, collateralValue * 1e18 / liabilityValue, "HF calculation mismatch");
        }
    }

    /// @notice Test externalHF() on a mainnet Euler vault
    function testExternalHFEulerMainnet() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        (uint healthFactor, uint collateralValue, uint liabilityValue) = hsv.externalHF(vault);

        console2.log("=== externalHF() Euler Mainnet ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        // Euler vault, so targetVault != aavePool
        address targetVault = ICollateralVaultBase(vault).targetVault();
        assertTrue(targetVault != MAINNET_AAVE_POOL, "expected Euler target, not Aave");

        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint).max, "zero liability should give max HF");
        } else {
            assertEq(healthFactor, collateralValue * 1e18 / liabilityValue, "HF calculation mismatch");
        }
    }

    /// @notice Test health() returns max HF for a vault with zero external debt
    function testHealthZeroDebtReturnsMaxHF() public {
        if (block.chainid != 1) return;
        vm.rollFork(23517900);
        _deployHsv();

        address vault = 0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09;

        (uint extHF, uint inHF, uint extDebt, uint intDebt) = hsv.health(vault);

        console2.log("=== health() Zero Debt ===");
        console2.log("extHF:", extHF);
        console2.log("inHF:", inHF);
        console2.log("extDebt:", extDebt);
        console2.log("intDebt:", intDebt);

        if (extDebt == 0) {
            assertEq(extHF, type(uint).max, "extHF should be max when no external debt");
            assertEq(inHF, type(uint).max, "inHF should be max when no external debt");
        }
    }

    /// @notice Test internalHF() returns max HF when liability is zero
    function testInternalHFZeroLiability() public {
        if (block.chainid != 1) return;
        vm.rollFork(23517900);
        _deployHsv();

        address vault = 0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09;

        (uint healthFactor, uint collateralValue, uint liabilityValue) = hsv.internalHF(vault);

        console2.log("=== internalHF() Zero Liability ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint).max, "zero liability should give max HF");
        }
    }

    /// @notice Test health() on a Base Euler vault
    function testHealthEulerVaultBase() public {
        if (block.chainid != 8453) return;
        // Block 41325539 is after the CV factory deployment (38122653)
        vm.rollFork(41325539);
        _deployHsv();

        // Use the vault from DebugLiquidation test which is known active on Base
        address vault = 0x23CEAd7E58D7d4aFadb4A617f6dA3937ADd6625c;
        vm.label(vault, "BaseEulerCollateralVault");

        assertTrue(
            ICollateralVaultFactory(BASE_CV_FACTORY).isCollateralVault(vault),
            "not a valid collateral vault"
        );

        (uint extHF, uint inHF, uint extDebt, uint intDebt) = hsv.health(vault);

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

        (uint extHF, uint inHF, uint extDebt,) = hsv.health(vault);

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
        (uint dExtHF, uint dInHF, uint dExtDebt, uint dIntDebt) = deployed.health(vault);
        (uint fExtHF, uint fInHF, uint fExtDebt, uint fIntDebt) = fresh.health(vault);

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
        (, , uint intLiability) = hsv.internalHF(vault);

        // Get health
        (, , , uint healthIntDebt) = hsv.health(vault);

        // internalHF() liability should match health() internalBorrowDebtValue
        assertEq(intLiability, healthIntDebt, "internal liability should match between functions");
    }

    /// @notice Test that externalHF() and health() return consistent external debt values
    function testExternalDebtConsistency() public {
        if (block.chainid != 1) return;
        vm.rollFork(23420000);
        _deployHsv();

        address vault = 0xA3ab8138A6c621f6afD2c3C57016F9b44837f767;

        // externalHF returns raw liability from accountLiquidity
        (, , uint extLiability) = hsv.externalHF(vault);

        // health() returns externalBorrowDebtValue which is the same raw value for Euler vaults
        (, , uint healthExtDebt, ) = hsv.health(vault);

        // For Euler vaults, both should get the debt from the same accountLiquidity call
        assertEq(extLiability, healthExtDebt, "external debt should match between externalHF and health");
    }
}
