// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {TwyneLiquidator, IEulerCollateralVault, IEVault, IERC20} from "contracts/TwyneLiquidator.sol";
import {MockSwapper} from "./MockSwapper.sol";
import {LiquidationStateBuilder} from "./LiquidationStateBuilder.sol";

/// @notice Tests for TwyneLiquidator (Euler) across all three liquidation variants. State is manufactured
///         on demand against the LIVE deployed protocol via LiquidationStateBuilder (DEV-569) — no
///         dependence on a hardcoded historical block where a liquidatable vault happened to exist.
contract LiquidatorTest is LiquidationStateBuilder {
    TwyneLiquidator liquidator;
    MockSwapper mockSwapper;

    address collateralVaultFactory;
    address twyneEVCAddress;
    address owner;
    address borrower;

    // Sizing for the manufactured Euler CV (small: the live eWETH IV has a ~7 eWETH supply cap).
    uint16 constant LIQ_LTV = 9400; // <= live maxTwyneLTVs(eWETH IV) == 9400
    uint256 constant COLLATERAL_WETH = 5 ether;
    uint256 constant BORROW_USDC = 5000e6;

    event Liquidation(
        address indexed violatorAddress,
        address repaidBorrowAsset,
        address seizedCollateralAsset,
        uint256 amountRepaid,
        uint256 amountProfit
    );
    event ExtLiqWithDebt(
        address indexed violatorAddress,
        address repaidBorrowAsset,
        address seizedCollateralAsset,
        uint256 amountRepaid,
        uint256 amountProfit
    );
    event ExtLiqZeroDebt(
        address indexed violatorAddress,
        address repaidBorrowAsset,
        address seizedCollateralAsset,
        uint256 amountRepaid,
        uint256 amountProfit
    );

    function setUp() public {
        _forkMainnet();
        // NB: use collision-unlikely labels — on a mainnet fork a makeAddr() address can land on a live
        // contract (e.g. makeAddr("alice") has code at this block, which would swallow swept ETH).
        owner = makeAddr("twyneLiqBotOwner");
        borrower = makeAddr("twyneLiqBotBorrower");
        require(owner.code.length == 0 && borrower.code.length == 0, "test EOA collides with live contract");
        vm.deal(owner, 10 ether);

        collateralVaultFactory = FACTORY;
        twyneEVCAddress = TWYNE_EVC;
        vm.label(collateralVaultFactory, "collateralVaultFactory");
        vm.label(twyneEVCAddress, "twyneEVCAddress");
    }

    function deployLiquidator() internal {
        mockSwapper = new MockSwapper();
        liquidator = new TwyneLiquidator(owner, collateralVaultFactory, address(mockSwapper));
        vm.label(address(liquidator), "liquidator");

        assertEq(liquidator.owner(), owner);
        assertEq(liquidator.router(), address(mockSwapper));
        assertEq(address(liquidator.factory()), collateralVaultFactory);
    }

    /// @dev Variant 1: Twyne-internal liquidation (canLiquidate via collateral price drop).
    function testLiquidateVault() public {
        deployLiquidator();
        CVHandles memory h = _createEulerCV(borrower, LIQ_LTV, COLLATERAL_WETH, BORROW_USDC);
        makeEulerInternallyLiquidatable(h);

        IEulerCollateralVault collateralVault = IEulerCollateralVault(h.cv);
        address collateralAsset = collateralVault.asset();
        assertTrue(collateralVault.canLiquidate(), "collateral vault should be liquidatable");

        address tokenIn = IEVault(collateralAsset).asset();
        uint256 amountIn = IERC20(tokenIn).balanceOf(address(collateralVault));
        address tokenOut = collateralVault.targetAsset();
        uint256 amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData =
            abi.encodeCall(MockSwapper.swap, (tokenIn, tokenOut, amountIn, amountOut, address(liquidator)));
        deal(tokenOut, address(mockSwapper), amountOut);

        assertEq(IERC20(collateralAsset).balanceOf(address(liquidator)), 0);
        uint256 initTargetAssetBal = IERC20(tokenOut).balanceOf(address(liquidator));
        vm.expectEmit(true, false, false, false, address(liquidator));
        emit Liquidation(address(collateralVault), tokenOut, tokenIn, amountOut, 0);
        liquidator.liquidateCollateralVault(address(collateralVault), dexData, 1);

        assertEq(IERC20(collateralAsset).balanceOf(address(liquidator)), 0);
        uint256 postTargetAssetBal = IERC20(tokenOut).balanceOf(address(liquidator));
        assertGe(postTargetAssetBal - initTargetAssetBal, 1);
        assertEq(IERC20(tokenOut).allowance(address(liquidator), address(collateralVault)), 0, "target allowance to CV");
        assertEq(
            IERC20(tokenOut).allowance(address(liquidator), address(liquidator.MORPHO())),
            0,
            "target allowance to Morpho"
        );
        assertEq(
            IERC20(collateralAsset).allowance(address(liquidator), address(collateralVault)),
            0,
            "collateral allowance to CV"
        );
        assertEq(
            IERC20(tokenIn).allowance(address(liquidator), address(mockSwapper)), 0, "underlying allowance to router"
        );

        // owner-gated sweep of the profit
        uint256 initOwnerBal = IERC20(tokenOut).balanceOf(owner);
        vm.expectRevert(bytes("t1"));
        liquidator.sweep(tokenOut, postTargetAssetBal);
        vm.prank(liquidator.owner());
        liquidator.sweep(tokenOut, postTargetAssetBal);
        assertEq(IERC20(tokenOut).balanceOf(address(liquidator)), 0);
        assertEq(IERC20(tokenOut).balanceOf(owner) - initOwnerBal, postTargetAssetBal);
    }

    /// @dev Variant 1 negative path: revert when realized profit is below the requested minimum.
    function testLiquidateVaultRevertsWhenBelowMinProfit() public {
        deployLiquidator();
        CVHandles memory h = _createEulerCV(borrower, LIQ_LTV, COLLATERAL_WETH, BORROW_USDC);
        makeEulerInternallyLiquidatable(h);

        IEulerCollateralVault collateralVault = IEulerCollateralVault(h.cv);
        address collateralAsset = collateralVault.asset();
        address tokenIn = IEVault(collateralAsset).asset();
        uint256 amountIn = IERC20(tokenIn).balanceOf(address(collateralVault));
        address tokenOut = collateralVault.targetAsset();
        uint256 amountOut = collateralVault.maxRepay() + 1; // realized profit == 1
        bytes memory dexData =
            abi.encodeCall(MockSwapper.swap, (tokenIn, tokenOut, amountIn, amountOut, address(liquidator)));
        deal(tokenOut, address(mockSwapper), amountOut);

        vm.expectRevert(bytes("Liquidation is not sufficiently profitable"));
        liquidator.liquidateCollateralVault(address(collateralVault), dexData, 2);
    }

    /// @dev Variant 3: externally liquidated with all target debt cleared (zero debt), maxRelease > 0.
    function testExternallyLiquidatedVaultWithZeroDebt() public {
        deployLiquidator();
        CVHandles memory h = _createEulerCV(borrower, LIQ_LTV, COLLATERAL_WETH, BORROW_USDC);
        makeEulerExternallyLiquidatedZeroDebt(h, makeAddr("extLiquidator"));

        IEulerCollateralVault collateralVault = IEulerCollateralVault(h.cv);
        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was liquidated externally!");
        assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");
        assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
        assertEq(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");

        IERC20 collateralAsset = IERC20(collateralVault.asset());
        uint256 initBal = collateralAsset.balanceOf(address(collateralVault));
        assertGt(initBal, 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);
        address targetAsset = collateralVault.targetAsset();
        address underlyingToken = IEVault(address(collateralAsset)).asset();

        vm.expectEmit(true, false, false, false, address(liquidator));
        emit ExtLiqZeroDebt(address(collateralVault), targetAsset, underlyingToken, 0, 0);
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 1);

        assertEq(collateralAsset.balanceOf(address(collateralVault)), 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0, "0 maxRepay CV shouldn't pay liquidator");
        assertEq(
            IERC20(targetAsset).allowance(address(liquidator), address(collateralVault)), 0, "target allowance to CV"
        );
    }

    /// @dev Variant 2: externally liquidated with residual target debt (maxRepay > 0).
    function testExternallyLiquidatedVaultWithDebt() public {
        deployLiquidator();
        CVHandles memory h = _createEulerCV(borrower, LIQ_LTV, COLLATERAL_WETH, BORROW_USDC);
        makeEulerExternallyLiquidatedWithDebt(h, makeAddr("extLiquidator"));

        IEulerCollateralVault collateralVault = IEulerCollateralVault(h.cv);
        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was liquidated externally!");
        assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");
        assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
        assertGt(collateralVault.maxRepay(), 0, "collateral vault has residual debt");

        IERC20 collateralAsset = IERC20(collateralVault.asset());
        uint256 initBal = collateralAsset.balanceOf(address(collateralVault));
        assertGt(initBal, 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);

        // without dex data the swap leg is empty and the with-debt path reverts
        vm.expectRevert(TwyneLiquidator.Swapper_EmptyError.selector);
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 1);

        address tokenIn = IEVault(address(collateralAsset)).asset();
        uint256 amountIn = IERC20(tokenIn).balanceOf(address(collateralVault));
        address tokenOut = collateralVault.targetAsset();
        uint256 amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData =
            abi.encodeCall(MockSwapper.swap, (tokenIn, tokenOut, amountIn, amountOut, address(liquidator)));
        deal(tokenOut, address(mockSwapper), amountOut);

        uint256 initTargetAssetLiquidatorBal = IERC20(tokenOut).balanceOf(address(liquidator));
        vm.expectEmit(true, false, false, false, address(liquidator));
        emit ExtLiqWithDebt(address(collateralVault), tokenOut, tokenIn, amountOut, 0);
        liquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), dexData, 1);

        assertEq(collateralAsset.balanceOf(address(collateralVault)), 0);
        assertEq(collateralVault.balanceOf(address(liquidator)), 0);
        assertEq(collateralVault.maxRepay(), 0, "maxRepay 0 after handleExternalLiquidation");
        assertEq(IERC20(tokenOut).allowance(address(liquidator), address(collateralVault)), 0, "target allowance to CV");
        assertGt(IERC20(tokenOut).balanceOf(address(liquidator)), initTargetAssetLiquidatorBal);
    }

    function testTwyneLiquidatorAdminParity() public {
        deployLiquidator();

        address token = USDC;
        deal(token, address(liquidator), 1000e6);
        deal(address(liquidator), 1 ether);

        uint256 initOwnerTokenBal = IERC20(token).balanceOf(owner);
        uint256 initOwnerEthBal = owner.balance;

        vm.expectRevert(bytes("t1"));
        liquidator.sweep(token, 1000e6);
        vm.expectRevert(bytes("t1"));
        liquidator.sweepETH(1 ether);
        vm.expectRevert(bytes("t1"));
        liquidator.setRouter(makeAddr("blockedRouter"));

        vm.startPrank(owner);
        liquidator.sweep(token, 1000e6);
        liquidator.sweepETH(1 ether);
        address newRouter = makeAddr("newRouter");
        liquidator.setRouter(newRouter);
        vm.expectRevert(bytes("zero address"));
        liquidator.setRouter(address(0));
        vm.stopPrank();

        assertEq(IERC20(token).balanceOf(address(liquidator)), 0);
        assertEq(IERC20(token).balanceOf(owner), initOwnerTokenBal + 1000e6);
        assertEq(address(liquidator).balance, 0);
        assertEq(owner.balance, initOwnerEthBal + 1 ether);
        assertEq(liquidator.router(), newRouter);
    }

    function testTwyneLiquidatorSweepETHRevertsWhenTransferFails() public {
        RevertingETHReceiver rejectingOwner = new RevertingETHReceiver();
        MockSwapper testMockSwapper = new MockSwapper();
        TwyneLiquidator testLiquidator =
            new TwyneLiquidator(address(rejectingOwner), collateralVaultFactory, address(testMockSwapper));
        vm.deal(address(testLiquidator), 1 ether);

        vm.prank(address(rejectingOwner));
        vm.expectRevert(bytes("ETH transfer failed"));
        testLiquidator.sweepETH(1 ether);
    }
}

contract RevertingETHReceiver {
    receive() external payable {
        revert("reject ETH");
    }
}
