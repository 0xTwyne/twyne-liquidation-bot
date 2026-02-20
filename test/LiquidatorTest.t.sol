// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {TwyneLiquidator, IEulerCollateralVault, IEVault, IERC20} from "contracts/TwyneLiquidator.sol";
import {IEulerCollateralVault} from "contracts/IEulerCollateralVault.sol";
import {MockSwapper} from "./MockSwapper.sol";

contract LiquidatorTest is Test {
    address constant LIQUIDATOR_CONTRACT_ADDRESS = 0x8d244058D946801bf39df29F1F67C7A4b3201521;
    address constant VAULT = 0xce45EF0414dE3516cAF1BCf937bF7F2Cf67873De;
    address constant ACCOUNT = 0xA5f0f68dCc5bE108126d79ded881ef2993841c2f;

    TwyneLiquidator liquidator;
    MockSwapper mockSwapper;

    address collateralVaultFactory;
    address twyneEVCAddress;
    address owner;

    function setUp() public {
        owner = makeAddr("alice");
        vm.deal(owner, 10 ether);

        if (block.chainid == 1) { // mainnet
            collateralVaultFactory = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
            twyneEVCAddress = 0xef39D6493884C4C84D38a4bFF879Ce16CEdE702a;
        } else if (block.chainid == 8453) { // base
            collateralVaultFactory = 0x5A0228f9A968Bf735a6e88dcC4cECf4953A94037;
            twyneEVCAddress = 0x00F3BE2c13FB10129E91dff8EF667e503C7a961E;
        } else {
            revert("chainid not recognized");
        }

        vm.label(address(collateralVaultFactory), "collateralVaultFactory");
        vm.label(address(twyneEVCAddress), "twyneEVCAddress");
    }

    function deployLiquidator() internal {
        mockSwapper = new MockSwapper();
        liquidator = new TwyneLiquidator(owner, collateralVaultFactory, address(mockSwapper));
        vm.label(address(liquidator), "liquidator");

        assertEq(liquidator.owner(), owner);
        assertEq(liquidator.router(), address(mockSwapper));
        assertEq(address(liquidator.factory()), collateralVaultFactory);
    }

    function testLiquidateVault() public {
        IEulerCollateralVault collateralVault;
        if (block.chainid == 8453) {
            vm.rollFork(31876119);
            collateralVault = IEulerCollateralVault(0x7368B2f4d37Bf788F4831E1c8dC7427019EFdF8E); // engn33r's user address
        } else if (block.chainid == 1) {
            vm.rollFork(23420000);
            collateralVault = IEulerCollateralVault(0xA3ab8138A6c621f6afD2c3C57016F9b44837f767);
        } else {
            revert("chainid not supported");
        }

        deployLiquidator();

        vm.label(address(collateralVault), "collateralVault");

        address collateralAsset = collateralVault.asset();
        vm.label(collateralAsset, "collateralAsset");
        // Verify vault is liquidatable
        assertTrue(IEulerCollateralVault(collateralVault).canLiquidate(), "collateral vault cannot be liquidated at early block!");
        // Perform liquidation
        address tokenIn = IEVault(collateralAsset).asset();
        vm.label(tokenIn, "underlyingCollateralAsset");
        uint amountIn = IERC20(tokenIn).balanceOf(address(collateralVault));
        address tokenOut = collateralVault.targetAsset();
        vm.label(tokenOut, "targetAsset");

        uint amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData = abi.encodeCall(
            MockSwapper.swap, (tokenIn, tokenOut, amountIn, amountOut, address(liquidator))
        );
        deal(tokenOut, address(mockSwapper), amountOut);

        // Calculate collateralFlashAmount - this should cover collateralForBorrower amount
        // For testing, we use the full collateral balance
        uint collateralFlashAmount = amountIn;

        assertEq(IERC20(collateralAsset).balanceOf(address(liquidator)), 0);
        uint initTargetAssetBal = IERC20(tokenOut).balanceOf(address(liquidator));
        liquidator.liquidateCollateralVault(address(collateralVault), collateralFlashAmount, dexData, 1);
        assertEq(IERC20(collateralAsset).balanceOf(address(liquidator)), 0);
        uint postTargetAssetBal = IERC20(tokenOut).balanceOf(address(liquidator));
        assertGe(postTargetAssetBal - initTargetAssetBal, 1);

        uint initTargetAssetOwnerBal = IERC20(tokenOut).balanceOf(owner);

        vm.expectRevert(bytes("t1"));
        liquidator.sweep(tokenOut, postTargetAssetBal);

        vm.startPrank(liquidator.owner());
        liquidator.sweep(tokenOut, postTargetAssetBal);

        assertEq(IERC20(tokenOut).balanceOf(address(liquidator)), 0);
        assertEq(IERC20(tokenOut).balanceOf(owner) - initTargetAssetOwnerBal, postTargetAssetBal);
    }


    function testExternallyLiquidatedVaultWithZeroDebt() public {
        IEulerCollateralVault collateralVault;
        if (block.chainid == 1) {
            collateralVault = IEulerCollateralVault(0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09);
            vm.rollFork(23517900);

            assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was liquidated externally!");
            assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");
            assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
            assertEq(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");
        } else {
            revert ("chainid not supported");
        }

        deployLiquidator();
        vm.label(address(collateralVault), "collateralVault");

        IERC20 collateralAsset = IERC20(collateralVault.asset());
        vm.label(address(collateralAsset), "collateralAsset");
        vm.label(collateralVault.borrower(), "borrower");
        vm.label(collateralVault.intermediateVault(), "intermediateVault");

        uint initBal = collateralAsset.balanceOf(address(collateralVault));
        assertGt(initBal, 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 1);
        assertEq(collateralAsset.balanceOf(address(collateralVault)), 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0, "0 maxRepay CV shouldn't pay liquidator");
    }

    function testExternallyLiquidatedVaultWithDebt() public {
        IEulerCollateralVault collateralVault;
        if (block.chainid == 1) {
            collateralVault = IEulerCollateralVault(0x8A0899aAA9D91D8E95F8edbAE9339a37702E0A09);
            vm.rollFork(23517900);

            assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was liquidated externally!");
            assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");

            assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
            vm.startPrank(address(collateralVault));

            IEVault(collateralVault.targetVault()).borrow(1000, owner);
            assertGt(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");
        } else {
            revert ("chainid not supported");
        }

        deployLiquidator();
        vm.label(address(collateralVault), "collateralVault");

        IERC20 collateralAsset = IERC20(collateralVault.asset());
        vm.label(address(collateralAsset), "collateralAsset");
        vm.label(collateralVault.borrower(), "borrower");
        vm.label(collateralVault.intermediateVault(), "intermediateVault");

        uint initBal = collateralAsset.balanceOf(address(collateralVault));
        assertGt(initBal, 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);
        vm.expectRevert(TwyneLiquidator.Swapper_EmptyError.selector);
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 1);

        address tokenIn = IEVault(address(collateralAsset)).asset();
        vm.label(tokenIn, "underlyingCollateralAsset");
        uint amountIn = IERC20(tokenIn).balanceOf(address(collateralVault));
        address tokenOut = collateralVault.targetAsset();
        vm.label(tokenOut, "targetAsset");

        uint amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData = abi.encodeCall(
            MockSwapper.swap, (tokenIn, tokenOut, amountIn, amountOut, address(liquidator))
        );
        deal(tokenOut, address(mockSwapper), amountOut);

        uint initTargetAssetLiquidatorBal = IERC20(tokenOut).balanceOf(address(liquidator));
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), dexData, 1);
        assertEq(collateralAsset.balanceOf(address(collateralVault)), 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);
        assertEq(collateralVault.maxRepay(), 0, "maxRepay 0 after handleExternalLiquidation");

        uint postTargetAssetLiquidatorBal = IERC20(tokenOut).balanceOf(address(liquidator));
        assertGt(postTargetAssetLiquidatorBal, initTargetAssetLiquidatorBal);
    }
}
