// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {HealthStatViewer, ICollateralVaultBase, PositionStats} from "contracts/HealthStatViewer.sol";

/// @dev Fork tests for the Twyne 1.0.7 HealthStatViewer (DEV-660).
///
/// The lens targets the 1.0.7 contracts only (VaultManager V5 `liqParams(iv, targetAsset)`).
/// Until Safe nonce 29 executes on mainnet, run these tests against an anvil fork with the
/// upgrade applied by impersonation (see twyne-knowledge
/// `notes/xkubush/2026-09-09_safe-tx-verification-twyne-1.0.7-upgrade.md` §6):
///
///   forge test --match-contract HealthStatViewerTest --fork-url http://127.0.0.1:8545
///
/// After the upgrade is live, any mainnet archive RPC works. `setUp` reverts with a clear
/// message when the chain is not on 1.0.7. All fork tests read the fork tip; they pin live
/// vaults, not blocks, so they keep working as positions evolve as long as the vaults exist.
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

    function targetAsset() external view returns (address);

    function aToken() external view returns (address);
}

/// @dev VaultManager 1.0.7: parameters keyed by (intermediateVault, targetAsset).
interface IVmLite {
    function liqParams(address intermediateVault, address targetAsset)
        external
        view
        returns (uint16 externalLiqBuffer, uint16 maxTwyneLiqLTV, uint16 borrowBuffer);

    function maxTwyneLTVs(address intermediateVault, address targetAsset) external view returns (uint16);
}

interface IERC20Bal {
    function balanceOf(address account) external view returns (uint256);
}

interface IATokenScaled {
    function scaledBalanceOf(address account) external view returns (uint256);
}

contract HealthStatViewerTest is Test {
    // Aave V3 Pool
    address constant MAINNET_AAVE_POOL = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;

    // Twyne core (proxies; addresses are stable across the 1.0.7 upgrade)
    address constant MAINNET_CV_FACTORY = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
    address constant MAINNET_VAULT_MANAGER = 0x0acd3A3c8Ab6a5F7b5A594C88DFa28999dA858aC;

    // Live mainnet collateral vaults used as fixtures (all created before the upgrade;
    // every vault is a beacon proxy, so they run the 1.0.7 implementation after it).
    // Euler eWETH -> USDT vault with external and internal debt.
    address constant EULER_ACTIVE = 0x9dbE1De4F8BDF241Cc41fCF5b868f28Eeaa6dF67;
    // Euler ewstETH -> WETH vault with an active position.
    address constant EULER_ACTIVE_2 = 0x5F50c8B1996247C4d3ea379C85c006F5E0198BE2;
    // Aave PT-sUSDe-22OCT2026 -> USDe vault, eMode category 48, active position.
    address constant AAVE_ACTIVE = 0x6250C32d91149b94A61148DDDDa7F7060E664278;
    // Euler eWETH -> USDC vault with no deposit and no debt.
    address constant ZERO_DEBT = 0x6a4Ab7fa660546EE0158F59dcFdf4CB917B43198;

    HealthStatViewer hsv;

    function _deployHsv() internal {
        hsv = new HealthStatViewer(MAINNET_AAVE_POOL);
    }

    function _isForkTest() internal view returns (bool) {
        return block.chainid == 1;
    }

    function setUp() public {
        if (!_isForkTest()) return; // pure mock tests run without a fork
        // The lens needs VaultManager V5. Fail loudly on a pre-upgrade chain instead of
        // letting every fork test revert with empty data.
        (bool ok,) = MAINNET_VAULT_MANAGER.staticcall(
            abi.encodeWithSignature("liqParams(address,address)", address(0), address(0))
        );
        require(ok, "chain is not on Twyne 1.0.7 (VaultManager.liqParams missing): use an upgraded fork");
    }

    /// @notice Deploy a fresh HealthStatViewer and verify constructor sets aavePool correctly
    function testDeployFresh() public {
        _deployHsv();
        assertEq(hsv.aavePool(), MAINNET_AAVE_POOL, "aavePool should be set correctly");
    }

    /// @notice health() on a live Euler vault with an active position
    function testHealthEulerVaultMainnet() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = EULER_ACTIVE;
        vm.label(vault, "EulerCollateralVault");

        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a valid collateral vault");

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(vault);

        console2.log("=== health() Euler Mainnet ===");
        console2.log("extHF:", extHF);
        console2.log("inHF:", inHF);
        console2.log("extDebt:", extDebt);
        console2.log("intDebt:", intDebt);

        assertGt(extDebt, 0, "external debt should be non-zero for active position");
        assertGt(extHF, 0, "extHF finite and non-zero");
        assertLt(extHF, type(uint256).max, "extHF finite for an active position");
        assertLt(inHF, type(uint256).max, "inHF finite for an active position");
    }

    /// @notice internalHF() on a live Euler vault: liability equals health()'s internal debt.
    function testInternalHFEulerMainnet() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = EULER_ACTIVE;

        (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue) = hsv.internalHF(vault);
        (,,, uint256 healthIntDebt) = hsv.health(vault);

        console2.log("=== internalHF() Euler Mainnet ===");
        console2.log("healthFactor:", healthFactor);
        console2.log("collateralValue:", collateralValue);
        console2.log("liabilityValue:", liabilityValue);

        assertEq(liabilityValue, healthIntDebt, "internal liability parity with health()");
        if (liabilityValue == 0) {
            assertEq(healthFactor, type(uint256).max, "zero liability => max HF");
        } else {
            assertEq(healthFactor, collateralValue * 1e18 / liabilityValue, "HF = collateral / liability");
        }
    }

    /// @notice health() on a vault with no debt returns max HFs and zero debt values.
    function testHealthZeroDebtReturnsMaxHF() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = ZERO_DEBT;
        vm.label(vault, "ZeroDebtCollateralVault");
        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a CV");
        assertEq(ICollateralVaultBase(vault).maxRelease(), 0, "fixture must have no debt");

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(vault);

        assertEq(extDebt, 0, "no external debt");
        assertEq(intDebt, 0, "no internal debt");
        assertEq(extHF, type(uint256).max, "extHF max on zero debt");
        assertEq(inHF, type(uint256).max, "inHF max on zero debt");
    }

    /// @notice internalHF() with zero liability returns max HF.
    function testInternalHFZeroLiability() public {
        if (!_isForkTest()) return;
        _deployHsv();

        (uint256 healthFactor,, uint256 liabilityValue) = hsv.internalHF(ZERO_DEBT);
        assertEq(liabilityValue, 0, "no internal liability");
        assertEq(healthFactor, type(uint256).max, "max HF on zero liability");
    }

    /// @notice The Python bot unpacks health() as (extHF, inHF, extDebt, intDebt), all 1e18.
    function testHealthReturnValuesMatchPythonBotExpectations() public {
        if (!_isForkTest()) return;
        _deployHsv();

        (uint256 extHF, uint256 inHF, uint256 extDebt, uint256 intDebt) = hsv.health(EULER_ACTIVE);

        // HF_ONE in the bot is 1e18; a live, healthy position sits above 1e17 and below 1e20.
        assertGt(extHF, 1e17, "extHF in a sane 1e18 range");
        assertLt(extHF, 1e20, "extHF in a sane 1e18 range");
        assertGt(inHF, 1e17, "inHF in a sane 1e18 range");
        assertLt(inHF, 1e20, "inHF in a sane 1e18 range");
        assertGt(extDebt, 0, "extDebt non-zero");
        assertGe(extDebt, intDebt, "external debt covers the internal reservation");
    }

    /// @notice Three view functions return consistent data for the same vault
    function testConsistencyBetweenFunctions() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = EULER_ACTIVE;

        (,, uint256 intLiability) = hsv.internalHF(vault);
        (,,, uint256 healthIntDebt) = hsv.health(vault);
        assertEq(intLiability, healthIntDebt, "internal liability should match between functions");

        (,, uint256 extLiability) = hsv.externalHF(vault);
        (,, uint256 healthExtDebt,) = hsv.health(vault);
        assertEq(extLiability, healthExtDebt, "external debt should match between externalHF and health");
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
    //     (correctly) only after an external liquidation.
    //  2. USD scale: Euler router.getQuote(.., unitOfAccount) returns 1e18-scaled
    //     USD; all *Usd fields are 1e18 (Aave 1e8 is scaled *1e10 to match).
    //  3. LTV scale: all five LTV fields (incl. maxTwyneLTV) are basis points vs
    //     MAXFACTOR = 1e4; twyneLiqLTV = min(chosen, maxTwyneLTV).
    //  4. maxTwyneLTV (1.0.7): the cap is VaultManager.liqParams(iv, targetAsset).
    // ---------------------------------------------------------------------

    /// @notice positionStats() on a live Euler vault with an active position.
    function testPositionStatsEulerMainnet() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = EULER_ACTIVE;
        vm.label(vault, "EulerCollateralVault");
        assertTrue(ICollateralVaultFactory(MAINNET_CV_FACTORY).isCollateralVault(vault), "not a CV");

        PositionStats memory s = hsv.positionStats(vault);
        (uint256 extHF, uint256 inHF, uint256 extDebt,) = hsv.health(vault);

        console2.log("=== positionStats() Euler Mainnet ===");
        console2.log("C native:", s.userCollateralNative);
        console2.log("C usd:", s.userCollateralUsd);
        console2.log("C_LP native:", s.reservedCreditNative);
        console2.log("B native:", s.borrowNative);
        console2.log("B usd:", s.borrowUsd);
        console2.log("liqLTV_t:", s.twyneLiqLTV);
        console2.log("LTV_t:", s.twyneLTV);
        console2.log("liqLTV_e:", s.externalLiqLTV);
        console2.log("LTV_e:", s.externalLTV);

        // Parity with health().
        assertEq(s.borrowUsd, extDebt, "borrowUsd parity with health()");
        assertEq(s.extHF, extHF, "extHF parity");
        assertEq(s.inHF, inHF, "inHF parity");

        // LTV-parameter sanity (basis points <= MAXFACTOR).
        assertLe(s.externalLiqLTV, 1e4, "liqLTV_e bps <= MAXFACTOR");
        assertLe(s.twyneLiqLTV, 1e4, "liqLTV_t bps <= MAXFACTOR");

        // Real-balance sourcing is a no-op for a live vault.
        uint256 realBalance = IERC20Bal(ICvLite(vault).asset()).balanceOf(vault);
        assertEq(
            realBalance,
            ICollateralVaultBase(vault).totalAssetsDepositedOrReserved(),
            "live: realBalance == totalAssets (sourcing no-op)"
        );
        assertEq(s.userCollateralNative, realBalance - ICollateralVaultBase(vault).maxRelease(), "C native source");
        assertEq(s.reservedCreditNative, ICollateralVaultBase(vault).maxRelease(), "C_LP native source");

        // Operating LTVs are derived from the USD fields.
        assertEq(s.twyneLTV, uint32(s.borrowUsd * 1e4 / s.userCollateralUsd), "LTV_t = B/C");
        assertEq(
            s.externalLTV,
            uint32(s.borrowUsd * 1e4 / (s.userCollateralUsd + s.reservedCreditUsd)),
            "LTV_e = B/(C+C_LP)"
        );

        assertGt(s.borrowUsd, 0, "active position has debt");
        assertGt(s.borrowNative, 0, "Euler native debt > 0");
        assertGt(s.userCollateralUsd, 0, "active position has USD collateral");
    }

    /// @notice positionStats() on a live Aave (eMode) vault with an active position.
    function testPositionStatsAaveMainnet() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = AAVE_ACTIVE;
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

        // Real-balance sourcing (Aave: scaledBalanceOf(aToken)) is a no-op for a live vault.
        uint256 realBalance = IATokenScaled(ICvLite(vault).aToken()).scaledBalanceOf(vault);
        assertEq(
            realBalance,
            ICollateralVaultBase(vault).totalAssetsDepositedOrReserved(),
            "live: realBalance == totalAssets (sourcing no-op)"
        );
        assertEq(s.userCollateralNative, realBalance - ICollateralVaultBase(vault).maxRelease(), "C native source");
        assertEq(s.reservedCreditNative, ICollateralVaultBase(vault).maxRelease(), "C_LP native source");
        assertEq(s.userCollateralNative + s.reservedCreditNative, realBalance, "C + C_LP == realBalance");

        assertGt(s.borrowUsd, 0, "active position has debt");
        assertGt(s.userCollateralUsd, 0, "active position has USD collateral");
        // Native debt comes from the borrowed targetAsset()'s variable debt token.
        assertGt(s.borrowNative, 0, "Aave native debt > 0");

        // eMode liq-threshold regression guard: externalLiqLTV must equal the pool's TRUE
        // eMode liquidationThreshold, NOT its ltv.
        uint8 categoryId = IAaveCvCategory(vault).categoryId();
        assertTrue(categoryId != 0, "vault must be in an eMode category to exercise this path");
        AaveEModeCollateralConfig memory cfg =
            IAaveEModeReader(MAINNET_AAVE_POOL).getEModeCategoryCollateralConfig(categoryId);
        console2.log("eMode ltv:", cfg.ltv);
        console2.log("eMode liqThreshold:", cfg.liquidationThreshold);
        assertEq(s.externalLiqLTV, cfg.liquidationThreshold, "externalLiqLTV == eMode liqThreshold");
        assertTrue(cfg.ltv != cfg.liquidationThreshold, "ltv and threshold must differ for a meaningful guard");
        assertTrue(s.externalLiqLTV != cfg.ltv, "externalLiqLTV must not be the eMode ltv (old buggy value)");
    }

    /// @notice 1.0.7: maxTwyneLTV and twyneLiqLTV come from liqParams(iv, targetAsset), for both
    /// vault types. This is the key change of the port; the one-argument getter is gone.
    function testMaxTwyneLTVComesFromLiqParamsPair() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address[3] memory vaults = [EULER_ACTIVE, EULER_ACTIVE_2, AAVE_ACTIVE];
        for (uint256 i = 0; i < vaults.length; i++) {
            address vault = vaults[i];
            IVmLite vmgr = IVmLite(ICvLite(vault).twyneVaultManager());
            address iv = ICvLite(vault).intermediateVault();
            address targetAsset = ICvLite(vault).targetAsset();
            (, uint16 cap,) = vmgr.liqParams(iv, targetAsset);

            PositionStats memory s = hsv.positionStats(vault);
            assertEq(s.maxTwyneLTV, cap, "maxTwyneLTV == liqParams(iv, targetAsset).maxTwyneLiqLTV");
            assertEq(cap, vmgr.maxTwyneLTVs(iv, targetAsset), "liqParams and maxTwyneLTVs agree");
            uint256 chosen = ICollateralVaultBase(vault).twyneLiqLTV();
            assertEq(s.twyneLiqLTV, uint16(chosen < cap ? chosen : cap), "twyneLiqLTV = min(chosen, cap)");
            assertLe(s.maxTwyneLTV, 1e4, "maxTwyneLTV bps <= MAXFACTOR");
        }
    }

    /// @notice positionStats() on a vault with no deposit and no debt.
    function testPositionStatsZeroDebt() public {
        if (!_isForkTest()) return;
        _deployHsv();

        address vault = ZERO_DEBT;
        vm.label(vault, "ZeroDebtCollateralVault");

        PositionStats memory s = hsv.positionStats(vault);

        assertEq(s.borrowUsd, 0, "no debt");
        assertEq(s.extHF, type(uint256).max, "extHF max on zero debt");
        assertEq(s.inHF, type(uint256).max, "inHF max on zero debt");
        assertEq(s.twyneLTV, 0, "LTV_t 0 on zero debt");
        assertEq(s.externalLTV, 0, "LTV_e 0 on zero debt");
        // The cap is still reported for an empty vault (it is a pair parameter, not position state).
        assertGt(s.maxTwyneLTV, 0, "maxTwyneLTV reported for an empty vault");
    }

    /// @notice positionStats() clamps unbounded operating LTVs before uint32 casts (pure mocks, no fork).
    function testPositionStatsClampsUnboundedLTVsToUint32Max() public {
        MockHealthERC20 collateral = new MockHealthERC20();
        MockHealthRouter router = new MockHealthRouter();
        MockHealthVaultManager manager = new MockHealthVaultManager();
        MockHealthEVault targetVault = new MockHealthEVault(address(collateral), address(0x348), address(router));
        MockHealthEVault intermediateVault = new MockHealthEVault(address(collateral), address(0x348), address(router));
        uint256 debtValue = uint256(type(uint32).max) + 1;
        targetVault.setLiquidity(1, debtValue);
        targetVault.setDebt(debtValue);
        intermediateVault.setLiquidity(1, 0);

        MockHealthCollateralVault vault = new MockHealthCollateralVault(
            address(collateral), address(targetVault), address(intermediateVault), address(manager)
        );
        collateral.setBalance(address(vault), 1);
        manager.expectKey(address(intermediateVault), vault.targetAsset());

        hsv = new HealthStatViewer(makeAddr("aavePool"));
        PositionStats memory s = hsv.positionStats(address(vault));

        assertEq(s.userCollateralUsd, 1, "mock collateral quote");
        assertEq(s.borrowUsd, debtValue, "mock debt value");
        assertEq(s.twyneLTV, type(uint32).max, "twyneLTV clamped");
        assertEq(s.externalLTV, type(uint32).max, "externalLTV clamped");
        assertEq(s.maxTwyneLTV, 10_000, "cap from liqParams(iv, targetAsset)");
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

/// @dev VaultManager 1.0.7 surface used by the lens. Answers only for the expected
/// (intermediateVault, targetAsset) key, so a call with the wrong key reverts the test.
contract MockHealthVaultManager {
    address public expectedIntermediateVault;
    address public expectedTargetAsset;

    function expectKey(address intermediateVault, address targetAsset) external {
        expectedIntermediateVault = intermediateVault;
        expectedTargetAsset = targetAsset;
    }

    function liqParams(address intermediateVault, address targetAsset)
        external
        view
        returns (uint16 externalLiqBuffer, uint16 maxTwyneLiqLTV, uint16 borrowBuffer)
    {
        require(intermediateVault == expectedIntermediateVault, "liqParams: wrong intermediate vault");
        require(targetAsset == expectedTargetAsset, "liqParams: wrong target asset");
        return (10_000, 10_000, 0);
    }
}

contract MockHealthEVault {
    address public immutable asset;
    address public immutable unitOfAccount;
    address public immutable oracle;
    uint16 public ltvLiquidation = 9_000;
    uint256 public debt;
    uint256 public collateralValue;
    uint256 public liabilityValue;

    constructor(address asset_, address unitOfAccount_, address oracle_) {
        asset = asset_;
        unitOfAccount = unitOfAccount_;
        oracle = oracle_;
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
    address public constant targetAsset = address(0xDEB7);

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
