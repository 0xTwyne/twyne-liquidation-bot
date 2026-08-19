// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity ^0.8.24;

import {Test, console2} from "forge-std/Test.sol";
import {TwyneAaveLiquidator, IAaveCollateralVault, IERC20} from "contracts/TwyneAaveLiquidator.sol";
import {LiquidationStateBuilder} from "./LiquidationStateBuilder.sol";

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
        uint256 _liqLTV,
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
        address targetContract;
        address onBehalfOfAccount;
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
    function borrow(address asset, uint256 amount, uint256 interestRateMode, uint16 referralCode, address onBehalfOf)
        external;
    function repay(address asset, uint256 amount, uint256 interestRateMode, address onBehalfOf)
        external
        returns (uint256);
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address borrower,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

contract AaveLiquidatorTest is LiquidationStateBuilder {
    TwyneAaveLiquidator liquidator;
    MockSwapper mockSwapper;

    // AAVE_POOL / USDC / WETH are inherited from LiquidationStateBuilder.
    address constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;

    address collateralVaultFactory;
    address twyneEVCAddress;
    address owner;
    address borrower;

    // Sizing for the manufactured Aave CV (wstETH collateral, WETH debt).
    uint16 constant AAVE_LIQ_LTV = 9800; // == live maxTwyneLTVs(awstETH IV); > external eMode LT (9500) so the CV reserves credit
    uint256 constant AAVE_COLLATERAL_WSTETH = 2 ether;
    uint256 constant AAVE_BORROW_WETH = 1 ether;

    event ExtLiqZeroDebt(
        address indexed violatorAddress,
        address repaidBorrowAsset,
        address seizedCollateralAsset,
        uint256 amountRepaid,
        uint256 amountProfit
    );

    function setUp() public {
        owner = makeAddr("twyneAaveLiqOwner");
        borrower = makeAddr("twyneAaveLiqBorrower");
        require(owner.code.length == 0 && borrower.code.length == 0, "test EOA collides with live contract");
        vm.deal(owner, 10 ether);

        if (block.chainid == 1) {
            // mainnet
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

    /// @notice Variant 4: Twyne-internal liquidation of an AAVE collateral vault.
    /// @dev State is manufactured on the live protocol via the builder (DEV-569).
    function testLiquidateAaveVault() public {
        _forkMainnet();
        deployLiquidator();

        CVHandles memory h = _createAaveCV(borrower, AAVE_LIQ_LTV, AAVE_COLLATERAL_WSTETH, AAVE_BORROW_WETH);
        makeAaveInternallyLiquidatable(h);

        IAaveCollateralVault collateralVault = IAaveCollateralVault(h.cv);
        assertTrue(ICollateralVaultFactory(collateralVaultFactory).isCollateralVault(h.cv));
        assertTrue(collateralVault.canLiquidate(), "collateral vault cannot be liquidated!");

        address wrapperToken = collateralVault.asset();
        address underlyingToken = IAaveV3ATokenWrapper(wrapperToken).asset();
        address targetAsset = collateralVault.targetAsset();

        uint256 amountIn = IERC20(underlyingToken).balanceOf(h.cv);
        uint256 amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData =
            abi.encodeCall(MockSwapper.swap, (underlyingToken, targetAsset, amountIn, amountOut, address(liquidator)));
        deal(targetAsset, address(mockSwapper), amountOut);

        uint256 initTargetAssetBal = IERC20(targetAsset).balanceOf(address(liquidator));
        liquidator.liquidateCollateralVault(h.cv, dexData, 1);
        uint256 postTargetAssetBal = IERC20(targetAsset).balanceOf(address(liquidator));

        assertGe(postTargetAssetBal - initTargetAssetBal, 1, "No profit from liquidation");
    }

    /// @notice Variant 6: externally liquidated AAVE vault with all target debt cleared (zero debt).
    function testExternallyLiquidatedAaveVaultWithZeroDebt() public {
        _forkMainnet();
        deployLiquidator();

        CVHandles memory h = _createAaveCV(borrower, AAVE_LIQ_LTV, AAVE_COLLATERAL_WSTETH, AAVE_BORROW_WETH);
        makeAaveExternallyLiquidatedZeroDebt(h, makeAddr("aaveExtLiquidator"));

        IAaveCollateralVault collateralVault = IAaveCollateralVault(h.cv);
        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was not externally liquidated!");
        assertNotEq(collateralVault.borrower(), address(0), "handleExternalLiquidation is yet to be called!");
        assertGt(collateralVault.maxRelease(), 0, "anyone can call handleExternalLiquidation!");
        assertEq(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");

        IERC20 wrapperToken = IERC20(collateralVault.asset());
        uint256 initBal = wrapperToken.balanceOf(h.cv);
        assertGt(initBal, 0);

        liquidator.liquidateExtLiquidatedCollateralVault(h.cv, bytes(""), 0);

        assertEq(wrapperToken.balanceOf(h.cv), 0);
    }

    function testExternallyLiquidatedAaveZeroDebtEmitsAndClearsApprovalWithMocks() public {
        MockAaveEVC mockEvc = new MockAaveEVC();
        MockAaveCollateralVaultFactory mockFactory = new MockAaveCollateralVaultFactory(address(mockEvc));
        MockAaveERC20 targetAsset = new MockAaveERC20();
        MockAaveERC20 wrapperToken = new MockAaveERC20();
        MockAaveERC20 underlyingAsset = new MockAaveERC20();
        MockAaveIntermediateVault intermediateVault = new MockAaveIntermediateVault();
        MockAaveCollateralVault collateralVault = new MockAaveCollateralVault(
            address(targetAsset), address(wrapperToken), address(underlyingAsset), address(intermediateVault)
        );
        mockFactory.setCollateralVault(address(collateralVault), true);

        MockSwapper testMockSwapper = new MockSwapper();
        TwyneAaveLiquidator testLiquidator =
            new TwyneAaveLiquidator(owner, address(mockFactory), address(testMockSwapper), makeAddr("aavePool"));

        vm.expectEmit(true, false, false, false, address(testLiquidator));
        emit ExtLiqZeroDebt(address(collateralVault), address(targetAsset), address(underlyingAsset), 0, 0);
        testLiquidator.liquidateExtLiquidatedCollateralVault(address(collateralVault), bytes(""), 1);

        assertTrue(collateralVault.handledExternalLiquidation(), "handleExternalLiquidation not called");
        assertTrue(intermediateVault.liquidated(), "intermediate vault not liquidated");
        assertEq(targetAsset.allowance(address(testLiquidator), address(collateralVault)), 0, "target allowance to CV");
    }

    /// @notice Variant 5: externally liquidated AAVE vault with residual target debt (maxRepay > 0).
    function testExternallyLiquidatedAaveVaultWithDebt() public {
        _forkMainnet();
        deployLiquidator();

        CVHandles memory h = _createAaveCV(borrower, AAVE_LIQ_LTV, AAVE_COLLATERAL_WSTETH, AAVE_BORROW_WETH);
        makeAaveExternallyLiquidatedWithDebt(h, makeAddr("aaveExtLiquidator"));

        IAaveCollateralVault collateralVault = IAaveCollateralVault(h.cv);
        assertTrue(collateralVault.isExternallyLiquidated(), "collateral vault was not externally liquidated!");
        assertGt(collateralVault.maxRepay(), 0, "collateral vault has 0 debt");

        address wrapperToken = collateralVault.asset();
        address underlyingToken = IAaveV3ATokenWrapper(wrapperToken).asset();
        address targetAsset = collateralVault.targetAsset();

        uint256 amountIn = IERC20(underlyingToken).balanceOf(address(liquidator));
        uint256 amountOut = collateralVault.maxRepay() + 1;
        bytes memory dexData =
            abi.encodeCall(MockSwapper.swap, (underlyingToken, targetAsset, amountIn, amountOut, address(liquidator)));
        deal(targetAsset, address(mockSwapper), amountOut);

        uint256 initTargetAssetLiquidatorBal = IERC20(targetAsset).balanceOf(address(liquidator));
        liquidator.liquidateExtLiquidatedCollateralVault(h.cv, dexData, 1);

        assertEq(IERC20(wrapperToken).balanceOf(h.cv), 0);
        assertEq(collateralVault.maxRepay(), 0, "maxRepay not 0 after handleExternalLiquidation");

        uint256 postTargetAssetLiquidatorBal = IERC20(targetAsset).balanceOf(address(liquidator));
        assertGt(postTargetAssetLiquidatorBal, initTargetAssetLiquidatorBal);
    }

    /// @notice Test sweep function
    function testSweep() public {
        vm.rollFork(23600528);
        deployLiquidator();

        // Deal some tokens to the liquidator
        deal(USDC, address(liquidator), 1000e6);

        uint256 initOwnerBal = IERC20(USDC).balanceOf(owner);

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
        TwyneAaveLiquidator testLiquidator =
            new TwyneAaveLiquidator(testOwner, collateralVaultFactory, address(testMockSwapper), AAVE_POOL);

        // Deal some ETH to the liquidator
        vm.deal(address(testLiquidator), 1 ether);

        uint256 initOwnerBal = testOwner.balance;

        // Non-owner cannot sweep ETH
        vm.expectRevert(bytes("t1"));
        testLiquidator.sweepETH(1 ether);

        // Owner can sweep ETH
        vm.prank(testOwner);
        testLiquidator.sweepETH(1 ether);

        assertEq(address(testLiquidator).balance, 0);
        assertEq(testOwner.balance, initOwnerBal + 1 ether);
    }

    /// @notice Test sweepETH reverts when the owner rejects ETH
    function testSweepETHRevertsWhenTransferFails() public {
        vm.rollFork(23600528);

        RevertingAaveETHReceiver rejectingOwner = new RevertingAaveETHReceiver();
        MockSwapper testMockSwapper = new MockSwapper();
        TwyneAaveLiquidator testLiquidator = new TwyneAaveLiquidator(
            address(rejectingOwner), collateralVaultFactory, address(testMockSwapper), AAVE_POOL
        );
        vm.deal(address(testLiquidator), 1 ether);

        vm.prank(address(rejectingOwner));
        vm.expectRevert(bytes("ETH transfer failed"));
        testLiquidator.sweepETH(1 ether);
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
        liquidator.liquidateCollateralVault(fakeVault, bytes(""), 0);
    }
}

contract MockAaveERC20 {
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => uint256) public balanceOf;

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
}

contract MockAaveCollateralVaultFactory {
    address public immutable EVC;
    mapping(address => bool) public isCollateralVault;

    constructor(address evc) {
        EVC = evc;
    }

    function setCollateralVault(address collateralVault, bool valid) external {
        isCollateralVault[collateralVault] = valid;
    }
}

contract MockAaveEVC {
    struct BatchItem {
        address targetContract;
        address onBehalfOfAccount;
        uint256 value;
        bytes data;
    }

    function enableController(address, address) external {}

    function batch(BatchItem[] calldata items) external {
        for (uint256 i = 0; i < items.length; i++) {
            (bool success, bytes memory data) = items[i].targetContract.call{value: items[i].value}(items[i].data);
            if (!success) {
                assembly {
                    revert(add(32, data), mload(data))
                }
            }
        }
    }
}

contract MockAaveIntermediateVault {
    bool public liquidated;

    function liquidate(address, address, uint256, uint256) external {
        liquidated = true;
    }
}

contract MockAaveCollateralVault {
    address public immutable targetAsset;
    address public immutable asset;
    address public immutable underlyingAsset;
    address public immutable intermediateVault;
    bool public handledExternalLiquidation;

    constructor(address targetAsset_, address asset_, address underlyingAsset_, address intermediateVault_) {
        targetAsset = targetAsset_;
        asset = asset_;
        underlyingAsset = underlyingAsset_;
        intermediateVault = intermediateVault_;
    }

    function canLiquidate() external pure returns (bool) {
        return false;
    }

    function isExternallyLiquidated() external pure returns (bool) {
        return true;
    }

    function maxRepay() external pure returns (uint256) {
        return 0;
    }

    function maxRelease() external pure returns (uint256) {
        return 1;
    }

    function borrower() external pure returns (address) {
        return address(0x1234);
    }

    function targetVault() external pure returns (address) {
        return address(0);
    }

    function totalAssetsDepositedOrReserved() external pure returns (uint256) {
        return 1;
    }

    function handleExternalLiquidation() external {
        handledExternalLiquidation = true;
    }

    function liquidate() external {}
    function repay(uint256) external {}
    function withdraw(uint256, address) external {}

    function redeemUnderlying(uint256, address) external pure returns (uint256) {
        return 0;
    }
}

contract RevertingAaveETHReceiver {
    receive() external payable {
        revert("reject ETH");
    }
}
