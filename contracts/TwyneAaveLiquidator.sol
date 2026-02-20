// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IEVC} from "./IEVC.sol";
import {IERC20} from "contracts/IEVault.sol";

interface IMorpho {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

interface ICollateralVaultFactory {
    function isCollateralVault(address collateralVault) external view returns (bool);
    function EVC() external view returns (address);
}

interface IAaveCollateralVault {
    function canLiquidate() external view returns (bool);
    function liquidate() external;
    function repay(uint amount) external;
    function redeemUnderlying(uint assets, address receiver) external returns (uint);
    function handleExternalLiquidation() external;
    function isExternallyLiquidated() external view returns (bool);
    function maxRepay() external view returns (uint);
    function maxRelease() external view returns (uint);
    function targetAsset() external view returns (address);
    function targetVault() external view returns (address);
    function asset() external view returns (address);
    function underlyingAsset() external view returns (address);
    function intermediateVault() external view returns (address);
    function borrower() external view returns (address);
    function totalAssetsDepositedOrReserved() external view returns (uint);
    function collateralForBorrower(uint B, uint C) external view returns (uint);
}

interface IAaveV3Pool {
    function withdraw(address asset, uint256 amount, address to) external returns (uint256);
    function getUserAccountData(address user) external view returns (
        uint256 totalCollateralBase,
        uint256 totalDebtBase,
        uint256 availableBorrowsBase,
        uint256 currentLiquidationThreshold,
        uint256 ltv,
        uint256 healthFactor
    );
}

interface IEVault {
    function liquidate(address violator, address collateral, uint256 repayAssets, uint256 minYieldBalance) external;
    function balanceOf(address account) external view returns (uint256);
    function redeem(uint256 shares, address receiver, address owner) external returns (uint256);
}

/// @dev Interface for wrapped aToken that allows depositing underlying
interface IWrappedAToken {
    function deposit(uint256 assets, address receiver) external returns (uint256);
    function mint(uint256 shares, address receiver) external returns (uint256);
    function asset() external view returns (address);
    function latestAnswer() external view returns (int256);
    function decimals() external view returns (uint8);
}

/// @title TwyneAaveLiquidator
/// @notice Liquidation contract for Twyne protocol AAVE V3 collateral vaults
/// @dev Uses Morpho Blue flashloans for capital-efficient liquidations
contract TwyneAaveLiquidator {
    error Swapper_EmptyError();

    /// @dev Internal liquidation params to avoid stack too deep
    struct InternalLiqParams {
        address collateralVault;
        address underlyingAsset;
        uint collateralFlashAmount;
        bytes dexData;
    }

    address public immutable owner;

    address public router;

    IEVC immutable evc;
    ICollateralVaultFactory public immutable factory;
    IAaveV3Pool public immutable aavePool;
    // Morpho Blue flashloan address https://docs.morpho.org/overview/concepts/flashloans/
    IMorpho public constant MORPHO = IMorpho(0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb);
    // NOTE: Balancer is an alternative zero-fee flashloan option
    // https://docs-v2.balancer.fi/reference/contracts/flash-loans.html

    error Unauthorized();
    error LessThanExpectedCollateralReceived();

    constructor(address _owner, address _factory, address _router, address _aavePool) {
        require(_owner != address(0) && _factory != address(0) && _router != address(0) && _aavePool != address(0), "zero address");
        owner = _owner;
        factory = ICollateralVaultFactory(_factory);
        router = _router;
        aavePool = IAaveV3Pool(_aavePool);

        evc = IEVC(ICollateralVaultFactory(_factory).EVC());
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "t1");
        _;
    }

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

    /// @notice Function for liquidating a Twyne AAVE collateral vault
    /// @dev Uses nested flash loans:
    ///      1. Flash loan target asset (for repaying vault debt)
    ///      2. Inside callback: Flash loan underlying, deposit to get wrapped aTokens
    ///      3. Liquidate (transfers wrapped aTokens to borrower, we become new owner)
    ///      4. Repay vault debt with target asset
    ///      5. Redeem underlying from vault
    ///      6. Swap underlying → target asset (profit)
    ///      7. Repay both flash loans
    /// @param collateralVault The address of the collateral vault to liquidate
    /// @param collateralFlashAmount Amount of underlying collateral to flash loan (must cover collateralForBorrower amount)
    /// @param dexData Encoded swap data to swap underlying → target asset (for profit after repaying flash loan)
    /// @param minProfit Minimum profit required for the liquidation to succeed
    /// @return profit The profit from the liquidation
    function liquidateCollateralVault(
        address collateralVault,
        uint collateralFlashAmount,
        bytes calldata dexData,
        uint minProfit
    ) external payable returns (uint profit) {
        // Verify collateralVault address is actually a collateral vault that can be liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(IAaveCollateralVault(collateralVault).canLiquidate(), "Collateral vault cannot be liquidated");

        // Cache useful values
        address targetAsset = IAaveCollateralVault(collateralVault).targetAsset();
        uint initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint maxRepay = IAaveCollateralVault(collateralVault).maxRepay();
        address collateralAsset = IAaveCollateralVault(collateralVault).asset(); // wrapped aToken
        address underlyingAsset = IAaveCollateralVault(collateralVault).underlyingAsset();

        // Step 1: Perform all approvals for this tx
        _approveInternalLiq(targetAsset, collateralAsset, underlyingAsset, collateralVault);

        // Step 2: Flash loan the target asset first (for repaying vault debt)
        // Inside the callback, we'll do a nested flash loan of the underlying
        // callbackType=0 for internal liquidation outer callback
        {
            InternalLiqParams memory params = InternalLiqParams({
                collateralVault: collateralVault,
                underlyingAsset: underlyingAsset,
                collateralFlashAmount: collateralFlashAmount,
                dexData: dexData
            });
            MORPHO.flashLoan(targetAsset, maxRepay, abi.encode(uint8(0), params));
        }
        // Logic continues in onMorphoFlashLoan()

        // Step 8: Verify the profit exceeds the minimum
        profit = IERC20(targetAsset).balanceOf(address(this)) - initBalance;
        require(profit >= minProfit, "Liquidation is not sufficiently profitable");

        // For safety reasons, reset all approvals to zero
        _resetApprovalsInternalLiq(targetAsset, collateralAsset, underlyingAsset, collateralVault);
        emit Liquidation(collateralVault, targetAsset, underlyingAsset, maxRepay, profit);
    }

    /// @dev Helper to set approvals for internal liquidation
    function _approveInternalLiq(address targetAsset, address collateralAsset, address underlyingAsset, address collateralVault) internal {
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt
        _safeApprove(targetAsset, address(MORPHO), type(uint).max); // needed for returning target asset flashloan
        _safeApprove(underlyingAsset, address(MORPHO), type(uint).max); // needed for returning underlying flashloan
        _safeApprove(underlyingAsset, collateralAsset, type(uint).max); // needed for depositing into wrapped aToken
        _safeApprove(underlyingAsset, router, type(uint).max); // needed for 1inch swap
        _safeApprove(collateralAsset, collateralVault, type(uint).max); // needed for liquidate() to transfer collateral to borrower
    }

    /// @dev Helper to reset approvals for internal liquidation
    function _resetApprovalsInternalLiq(address targetAsset, address collateralAsset, address underlyingAsset, address collateralVault) internal {
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        _safeApprove(underlyingAsset, address(MORPHO), 0);
        _safeApprove(underlyingAsset, collateralAsset, 0);
        _safeApprove(underlyingAsset, router, 0);
        _safeApprove(collateralAsset, collateralVault, 0);
    }

    /// @notice Function for handling the external liquidation of a Twyne AAVE collateral vault
    /// @param collateralVault The address of the externally liquidated vault
    /// @param dexData Encoded swap data for 1inch router
    /// @param minProfit Minimum profit required
    /// @return profit The profit from handling the external liquidation
    function liquidateExtLiquidatedCollateralVault(address collateralVault, bytes calldata dexData, uint minProfit) external returns (uint profit) {
        // Verify collateralVault address is actually a collateral vault that was externally liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(IAaveCollateralVault(collateralVault).isExternallyLiquidated(), "Collateral vault was not externally liquidated");
        if (IAaveCollateralVault(collateralVault).maxRepay() > 0) {
            return liquidateExtLiquidatedCollateralVaultWithDebt(collateralVault, dexData, minProfit);
        } else {
            return liquidateExtLiquidatedCollateralVaultZeroDebt(collateralVault);
        }
    }

    /// @notice Internal function for handling external liquidation when debt remains
    function liquidateExtLiquidatedCollateralVaultWithDebt(address collateralVault, bytes calldata dexData, uint minProfit) internal returns (uint profit) {
        // Enable controller to enable liquidation on intermediate vault
        evc.enableController(address(this), IAaveCollateralVault(collateralVault).intermediateVault());

        // Cache useful values
        address targetAsset = IAaveCollateralVault(collateralVault).targetAsset();
        uint initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint maxRepay = IAaveCollateralVault(collateralVault).maxRepay();
        address underlyingAsset = IAaveCollateralVault(collateralVault).underlyingAsset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt in handleExternalLiquidation
        _safeApprove(targetAsset, address(MORPHO), type(uint).max); // needed for returning flashloan
        _safeApprove(underlyingAsset, router, type(uint256).max); // needed for 1inch swap

        // Step 2: Borrow target asset with flashloan
        // callbackType=2 for external liquidation callback
        MORPHO.flashLoan(targetAsset, maxRepay, abi.encode(uint8(2), collateralVault, dexData));
        // Logic continues in onMorphoFlashLoan()

        // Step 6: Verify the profit exceeds the minimum
        uint postBalance = IERC20(targetAsset).balanceOf(address(this));
        profit = postBalance - initBalance;
        require(profit >= minProfit, "Liquidation is not sufficiently profitable");

        // For safety reasons, reset all approvals to zero
        _safeApprove(underlyingAsset, router, 0);
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        emit ExtLiqWithDebt(collateralVault, targetAsset, underlyingAsset, maxRepay, profit);
    }

    /// @notice Internal function for handling external liquidation when no debt remains
    function liquidateExtLiquidatedCollateralVaultZeroDebt(address collateralVault) internal returns (uint profit) {
        // Enable controller to enable liquidation on intermediate vault
        address intermediateVault = IAaveCollateralVault(collateralVault).intermediateVault();
        evc.enableController(address(this), intermediateVault);

        // Cache useful values
        address targetAsset = IAaveCollateralVault(collateralVault).targetAsset();
        uint maxRepay = IAaveCollateralVault(collateralVault).maxRepay();
        address underlyingAsset = IAaveCollateralVault(collateralVault).underlyingAsset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt in handleExternalLiquidation

        // Create batch to perform necessary steps to close out this position
        // For zero debt case, just call handleExternalLiquidation then liquidate intermediate vault bad debt
        IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
        items[0] = IEVC.BatchItem({
            onBehalfOfAccount: address(this),
            targetContract: collateralVault,
            value: 0,
            data: abi.encodeCall(IAaveCollateralVault(collateralVault).handleExternalLiquidation, ())
        });
        items[1] = IEVC.BatchItem({
            onBehalfOfAccount: address(this),
            targetContract: intermediateVault,
            value: 0,
            data: abi.encodeCall(IEVault(intermediateVault).liquidate, (collateralVault, collateralVault, 0, 0))
        });
        evc.batch(items);

        emit ExtLiqZeroDebt(collateralVault, targetAsset, underlyingAsset, maxRepay, profit);
    }

    /// @notice Morpho flashloan callback - handles both internal liquidation (nested) and external liquidation
    /// @dev Callback types determined by `callbackType`:
    ///      0 = Internal liquidation OUTER callback (target asset) - triggers nested flash loan
    ///      1 = Internal liquidation INNER callback (underlying) - executes liquidation
    ///      2 = External liquidation callback (target asset) - handles external liquidation
    function onMorphoFlashLoan(uint amount, bytes calldata data) external {
        require(msg.sender == address(MORPHO), "This function should only be called by Morpho during a flashloan");

        // First decode the callback type
        uint8 callbackType = abi.decode(data, (uint8));

        if (callbackType == 0) {
            // Internal liquidation OUTER callback (target asset flash loan)
            (, InternalLiqParams memory params) = abi.decode(data, (uint8, InternalLiqParams));

            // Step 3: Trigger nested flash loan for underlying collateral
            // Pass callbackType=1 for inner callback
            MORPHO.flashLoan(params.underlyingAsset, params.collateralFlashAmount, abi.encode(uint8(1), params.collateralVault, params.dexData));
            // After nested callback completes, target asset flash loan will be repaid

        } else if (callbackType == 1) {
            // Internal liquidation INNER callback (underlying flash loan)
            (, address collateralVault, bytes memory dexData) = abi.decode(data, (uint8, address, bytes));

            address collateralAsset = IAaveCollateralVault(collateralVault).asset(); // wrapped aToken

            // Step 4: Calculate cForB and deposit the required amount
            // For Aave: B = totalDebtBase (8 decimals USD), C = userOwnedCollateral * latestAnswer / 10^decimals
            uint cForB;
            {
                address _aavePool = IAaveCollateralVault(collateralVault).targetVault();
                (, uint totalDebtBase,,,,) = IAaveV3Pool(_aavePool).getUserAccountData(collateralVault);

                uint totalAssets = IAaveCollateralVault(collateralVault).totalAssetsDepositedOrReserved();
                uint maxRelease = IAaveCollateralVault(collateralVault).maxRelease();
                uint userOwnedCollateral = totalAssets - maxRelease;

                // Get price from wrapper's latestAnswer (Chainlink oracle, 8 decimals)
                int256 latestAnswer = IWrappedAToken(collateralAsset).latestAnswer();
                uint8 decimals = IWrappedAToken(collateralAsset).decimals();
                uint C = userOwnedCollateral * uint(latestAnswer) / (10 ** decimals);

                // Get collateralForBorrower - returns amount in wrapper shares (same decimals as wrapper)
                cForB = IAaveCollateralVault(collateralVault).collateralForBorrower(totalDebtBase, C);
            }

            // Mint exactly cForB wrapper shares (uses underlying from flash loan)
            // This is equivalent to Euler's mint(cForB) pattern
            IWrappedAToken(collateralAsset).mint(cForB, address(this));

            // Step 5: Use EVC batch to: liquidate → repay → redeem
            IEVC.BatchItem[] memory items = new IEVC.BatchItem[](3);
            items[0] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IAaveCollateralVault(collateralVault).liquidate, ())
            });
            items[1] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IAaveCollateralVault(collateralVault).repay, (type(uint).max))
            });
            items[2] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IAaveCollateralVault(collateralVault).redeemUnderlying, (type(uint).max, address(this)))
            });
            evc.batch(items);

            // Step 6: Swap underlying → target asset for profit
            if (dexData.length > 0) {
                (bool isSuccess, bytes memory returnData) = router.call(dexData);
                if (!isSuccess) {
                    if (returnData.length > 0) {
                        assembly {
                            revert(add(32, returnData), mload(returnData))
                        }
                    }
                    revert Swapper_EmptyError();
                }
            }
            // Inner flash loan (underlying) will be repaid when this callback ends

        } else if (callbackType == 2) {
            // External liquidation callback (target asset flash loan)
            (, address collateralVault, bytes memory dexData) = abi.decode(data, (uint8, address, bytes));

            // External liquidation flow - vault was liquidated by Aave
            address intermediateVault = IAaveCollateralVault(collateralVault).intermediateVault();

            IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
            items[0] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IAaveCollateralVault(collateralVault).handleExternalLiquidation, ())
            });
            items[1] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: intermediateVault,
                value: 0,
                data: abi.encodeCall(IEVault(intermediateVault).liquidate, (collateralVault, collateralVault, 0, 0))
            });
            evc.batch(items);

            // Redeem aTokens received for underlying asset from Aave pool
            aavePool.withdraw(IAaveCollateralVault(collateralVault).underlyingAsset(), type(uint).max, address(this));

            // Swap underlying to target asset to repay flash loan
            if (dexData.length > 0) {
                (bool isSuccess, bytes memory returnData) = router.call(dexData);
                if (!isSuccess) {
                    if (returnData.length > 0) {
                        assembly {
                            revert(add(32, returnData), mload(returnData))
                        }
                    }
                    revert Swapper_EmptyError();
                }
            }
        }

        // Flash loan will be repaid via transferFrom when callback ends
    }

    function sweep(address token, uint amount) external onlyOwner {
        _safeTransfer(token, msg.sender, amount);
    }

    function sweepETH(uint amount) external onlyOwner {
        payable(owner).call{value: amount}("");
    }

    function setRouter(address _router) external onlyOwner {
        require(_router != address(0), "zero address");
        router = _router;
    }

    /// @notice Safe approve function that handles non-standard ERC20 tokens like USDT
    /// @dev copied from Solady
    function _safeApprove(address token, address to, uint256 amount) internal {
        assembly("memory-safe") {
            mstore(0x14, to) // Store the `to` argument.
            mstore(0x34, amount) // Store the `amount` argument.
            mstore(0x00, 0x095ea7b3000000000000000000000000) // `approve(address,uint256)`.
            let success := call(gas(), token, 0, 0x10, 0x44, 0x00, 0x20)
            if iszero(and(eq(mload(0x00), 1), success)) {
                if iszero(lt(or(iszero(extcodesize(token)), returndatasize()), success)) {
                    mstore(0x00, 0x3e3f8f73) // `ApproveFailed()`.
                    revert(0x1c, 0x04)
                }
            }
            mstore(0x34, 0) // Restore the part of the free memory pointer that was overwritten.
        }
    }

    /// @dev Sends `amount` of ERC20 `token` from the current contract to `to`.
    /// Reverts upon failure.
    /// @dev copied from Solady
    function _safeTransfer(address token, address to, uint256 amount) internal {
        assembly("memory-safe") {
            mstore(0x14, to) // Store the `to` argument.
            mstore(0x34, amount) // Store the `amount` argument.
            mstore(0x00, 0xa9059cbb000000000000000000000000) // `transfer(address,uint256)`.
            // Perform the transfer, reverting upon failure.
            let success := call(gas(), token, 0, 0x10, 0x44, 0x00, 0x20)
            if iszero(and(eq(mload(0x00), 1), success)) {
                if iszero(lt(or(iszero(extcodesize(token)), returndatasize()), success)) {
                    mstore(0x00, 0x90b8ec18) // `TransferFailed()`.
                    revert(0x1c, 0x04)
                }
            }
            mstore(0x34, 0) // Restore the part of the free memory pointer that was overwritten.
        }
    }

    receive() external payable {}
}
