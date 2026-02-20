// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {TwyneAaveLiquidator, IAaveCollateralVault, IERC20} from "contracts/TwyneAaveLiquidator.sol";

interface IAaveV3ATokenWrapper {
    function asset() external view returns (address);
    function aToken() external view returns (address);
    function redeem(uint256 shares, address receiver, address owner) external returns (uint256);
    function balanceOf(address account) external view returns (uint256);
}
import {MockSwapper} from "./MockSwapper.sol";

interface ICollateralVaultFactory {
    function isCollateralVault(address collateralVault) external view returns (bool);
    function EVC() external view returns (address);
    function createCollateralVault(
        uint8 _vaultType,
        address _asset,
        address _targetVault,
        uint _liqLTV,
        address _targetAsset
    ) external returns (address);
    function paused() external view returns (bool);
    function setCategoryId(address targetVault, address asset, address targetAsset, uint8 categoryId) external;
}

interface IEVault {
    function deposit(uint256 assets, address receiver) external returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function debtOf(address account) external view returns (uint256);
    function asset() external view returns (address);
    function liquidate(address violator, address collateral, uint256 repayAssets, uint256 minYieldBalance) external;
}

interface IEVC {
    struct BatchItem {
        address onBehalfOfAccount;
        address targetContract;
        uint256 value;
        bytes data;
    }
    function enableController(address account, address vault) external;
    function enableCollateral(address account, address vault) external;
    function batch(BatchItem[] calldata items) external;
}

interface IVaultManager {
    function setMaxLiquidationLTV(address collateralAsset, uint16 maxLTV) external;
    function setExternalLiqBuffer(address collateralAsset, uint16 buffer) external;
    function externalLiqBuffers(address collateralAsset) external view returns (uint16);
    function maxTwyneLTVs(address collateralAsset) external view returns (uint16);
    function getIntermediateVault(address collateralAsset) external view returns (address);
    function setAllowedTargetAsset(address intermediateVault, address targetVault, address targetAsset) external;
}

interface IAaveV3Pool {
    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
    function setUserEMode(uint8 categoryId) external;
    function setUserUseReserveAsCollateral(address asset, bool useAsCollateral) external;
    function borrow(address asset, uint256 amount, uint256 interestRateMode, uint16 referralCode, address onBehalfOf) external;
    function repay(address asset, uint256 amount, uint256 interestRateMode, address onBehalfOf) external returns (uint256);
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address borrower,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

contract AaveLiquidatorTest is Test {
    TwyneAaveLiquidator liquidator;
    MockSwapper mockSwapper;

    // Mainnet addresses
    address constant AAVE_POOL = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;

    address collateralVaultFactory;
    address twyneEVCAddress;
    address owner;

    function setUp() public {
        owner = makeAddr("alice");
        vm.deal(owner, 10 ether);

        if (block.chainid == 1) { // mainnet
            collateralVaultFactory = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
            twyneEVCAddress = 0xef39D6493884C4C84D38a4bFF879Ce16CEdE702a;
        } else {
            revert("chainid not recognized");
        }

        vm.label(address(collateralVaultFactory), "collateralVaultFactory");
        vm.label(address(twyneEVCAddress), "twyneEVCAddress");
        vm.label(AAVE_POOL, "AavePool");
        vm.label(USDC, "USDC");
        vm.label(WETH, "WETH");
    }

    function deployLiquidator() internal {
        mockSwapper = new MockSwapper();
        liquidator = new TwyneAaveLiquidator(owner, collateralVaultFactory, address(mockSwapper), AAVE_POOL);
        vm.label(address(liquidator), "aaveLiquidator");

        assertEq(liquidator.owner(), owner);
        assertEq(liquidator.router(), address(mockSwapper));
        assertEq(address(liquidator.factory()), collateralVaultFactory);
    }

    /// @notice Test basic deployment and configuration
    function testDeployment() public {
        vm.rollFork(23600528);
        deployLiquidator();

        assertEq(liquidator.owner(), owner);
        assertEq(address(liquidator.factory()), collateralVaultFactory);
        assertEq(address(liquidator.MORPHO()), MORPHO);
    }

    /// @notice Test liquidating an AAVE collateral vault
    /// @dev This test requires finding a liquidatable AAVE vault at a specific block
    function testLiquidateAaveVault() public {
        // Fork at block where AAVE collateral vaults exist
        // Note: You'll need to update this block number and vault address
        // when actual AAVE vaults become liquidatable on mainnet
        vm.rollFork(23600528);

        deployLiquidator();

        // Skip if no AAVE vaults are liquidatable at this block
        // This serves as a template for when real liquidatable vaults exist
        // TODO: Update with actual liquidatable AAVE vault address when available
        address testVault = address(0); // Placeholder - update with real vault

        if (testVault == address(0)) {
            // Skip test if no vault to liquidate
            return;
        }

        IAaveCollateralVault collateralVault = IAaveCollateralVault(testVault);
        vm.label(address(collateralVault), "collateralVault");

        // Verify it's a valid collateral vault
        assertTrue(ICollateralVaultFactory(collateralVaultFactory).isCollateralVault(address(collateralVault)));

        // Verify vault is liquidatable
        assertTrue(collateralVault.canLiquidate(), "collateral vault cannot be liquidated!");

        // Get tokens
        address wrapperToken = collateralVault.asset();
        address underlyingToken = IAaveV3ATokenWrapper(wrapperToken).asset();
        address targetAsset = collateralVault.targetAsset();

        vm.label(wrapperToken, "wrapperToken");
        vm.label(underlyingToken, "underlyingToken");
        vm.label(targetAsset, "targetAsset");

        // Setup swap
        uint amountIn = IERC20(underlyingToken).balanceOf(address(collateralVault));
        uint amountOut = collateralVault.maxRepay() + 1;

        bytes memory dexData = abi.encodeCall(
            MockSwapper.swap, (underlyingToken, targetAsset, amountIn, amountOut, address(liquidator))
        );
        deal(targetAsset, address(mockSwapper), amountOut);

        // Calculate collateralFlashAmount - this should cover collateralForBorrower amount
        // For testing, we use the underlying balance in the vault
        uint collateralFlashAmount = IERC20(underlyingToken).balanceOf(address(collateralVault));

        // Execute liquidation
        uint initTargetAssetBal = IERC20(targetAsset).balanceOf(address(liquidator));
        liquidator.liquidateCollateralVault(address(collateralVault), collateralFlashAmount, dexData, 1);
        uint postTargetAssetBal = IERC20(targetAsset).balanceOf(address(liquidator));

        assertGe(postTargetAssetBal - initTargetAssetBal, 1, "No profit from liquidation");
    }

    /// @notice Test external liquidation handling with zero debt
    function testExternallyLiquidatedAaveVaultWithZeroDebt() public {
        // Fork at appropriate block
        vm.rollFork(23600528);

        deployLiquidator();

        // TODO: Update with actual externally liquidated AAVE vault when available
        address testVault = address(0); // Placeholder

        if (testVault == address(0)) {
            return;
        }

        IAaveCollateralVault collateralVault = IAaveCollateralVault(testVault);
        vm.label(address(collateralVault), "collateralVault");

        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was not externally liquidated!");
        assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");
        assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
        assertEq(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");

        IERC20 wrapperToken = IERC20(collateralVault.asset());
        vm.label(address(wrapperToken), "wrapperToken");

        uint initBal = wrapperToken.balanceOf(address(collateralVault));
        assertGt(initBal, 0);

        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 0);

        assertEq(wrapperToken.balanceOf(address(collateralVault)), 0);
    }

    /// @notice Test external liquidation handling with remaining debt
    function testExternallyLiquidatedAaveVaultWithDebt() public {
        // Fork at appropriate block
        vm.rollFork(23600528);

        deployLiquidator();

        // TODO: Update with actual externally liquidated AAVE vault with debt when available
        address testVault = address(0); // Placeholder

        if (testVault == address(0)) {
            return;
        }

        IAaveCollateralVault collateralVault = IAaveCollateralVault(testVault);
        vm.label(address(collateralVault), "collateralVault");

        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was not externally liquidated!");
        assertGt(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");

        address wrapperToken = collateralVault.asset();
        address underlyingToken = IAaveV3ATokenWrapper(wrapperToken).asset();
        address targetAsset = collateralVault.targetAsset();

        vm.label(wrapperToken, "wrapperToken");
        vm.label(underlyingToken, "underlyingToken");
        vm.label(targetAsset, "targetAsset");

        // Setup swap
        uint amountIn = IERC20(underlyingToken).balanceOf(address(liquidator));
        uint amountOut = collateralVault.maxRepay() + 1;

        bytes memory dexData = abi.encodeCall(
            MockSwapper.swap, (underlyingToken, targetAsset, amountIn, amountOut, address(liquidator))
        );
        deal(targetAsset, address(mockSwapper), amountOut);

        uint initTargetAssetLiquidatorBal = IERC20(targetAsset).balanceOf(address(liquidator));
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), dexData, 1);

        assertEq(IERC20(wrapperToken).balanceOf(address(collateralVault)), 0);
        assertEq(collateralVault.maxRepay(), 0, "maxRepay not 0 after handleExternalLiquidation");

        uint postTargetAssetLiquidatorBal = IERC20(targetAsset).balanceOf(address(liquidator));
        assertGt(postTargetAssetLiquidatorBal, initTargetAssetLiquidatorBal);
    }

    /// @notice Test sweep function
    function testSweep() public {
        vm.rollFork(23600528);
        deployLiquidator();

        // Deal some tokens to the liquidator
        deal(USDC, address(liquidator), 1000e6);

        uint initOwnerBal = IERC20(USDC).balanceOf(owner);

        // Non-owner cannot sweep
        vm.expectRevert(bytes("t1"));
        liquidator.sweep(USDC, 1000e6);

        // Owner can sweep
        vm.prank(owner);
        liquidator.sweep(USDC, 1000e6);

        assertEq(IERC20(USDC).balanceOf(address(liquidator)), 0);
        assertEq(IERC20(USDC).balanceOf(owner), initOwnerBal + 1000e6);
    }

    /// @notice Test sweepETH function
    function testSweepETH() public {
        vm.rollFork(23600528);

        // Use an EOA that doesn't exist on mainnet for this test
        address testOwner = address(0xdead1234);
        vm.deal(testOwner, 10 ether);

        MockSwapper testMockSwapper = new MockSwapper();
        TwyneAaveLiquidator testLiquidator = new TwyneAaveLiquidator(testOwner, collateralVaultFactory, address(testMockSwapper), AAVE_POOL);

        // Deal some ETH to the liquidator
        vm.deal(address(testLiquidator), 1 ether);

        uint initOwnerBal = testOwner.balance;

        // Owner can sweep ETH
        vm.prank(testOwner);
        testLiquidator.sweepETH(1 ether);

        assertEq(address(testLiquidator).balance, 0);
        assertEq(testOwner.balance, initOwnerBal + 1 ether);
    }

    /// @notice Test setRouter function
    function testSetRouter() public {
        vm.rollFork(23600528);
        deployLiquidator();

        address newRouter = makeAddr("newRouter");

        // Non-owner cannot set router
        vm.expectRevert(bytes("t1"));
        liquidator.setRouter(newRouter);

        // Owner can set router
        vm.prank(owner);
        liquidator.setRouter(newRouter);

        assertEq(liquidator.router(), newRouter);

        // Cannot set to zero address
        vm.prank(owner);
        vm.expectRevert(bytes("zero address"));
        liquidator.setRouter(address(0));
    }

    /// @notice Test that non-collateral vaults are rejected
    function testRevertOnNonCollateralVault() public {
        vm.rollFork(23600528);
        deployLiquidator();

        address fakeVault = makeAddr("fakeVault");

        vm.expectRevert(bytes("The input address is not a Twyne collateral vault"));
        liquidator.liquidateCollateralVault(fakeVault, 0, bytes(""), 0);
    }
}