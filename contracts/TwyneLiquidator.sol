// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IEVC} from "./IEVC.sol";

import {IEVault, IERC20} from "contracts/IEVault.sol";
import {IEulerCollateralVault} from "contracts/IEulerCollateralVault.sol";

interface IMorpho {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

interface ICollateralVaultFactory {
    function isCollateralVault(address collateralVault) external view returns (bool);
    function EVC() external view returns (address);
    function owner() external view returns (address);
}

interface IVaultManager {
    function oracleRouter() external view returns (IEulerRouter);
}

interface IEulerRouter {
    function getQuote(uint256 amount, address base, address quote) external view returns (uint256);
}

// Liquidation contract for Twyne protocol
// Assumes v1 of Twyne, integration exists only with Euler Finance
contract TwyneLiquidator {
    error Swapper_EmptyError();

    /// @dev Internal liquidation params to avoid stack too deep
    struct InternalLiqParams {
        address collateralVault;
        address underlyingToken;
        uint collateralFlashAmount;
        bytes dexData;
    }

    address public immutable owner;

    address public router;

    IEVC immutable evc;
    ICollateralVaultFactory public immutable factory;
    // Morpho Blue flashloan address https://docs.morpho.org/overview/concepts/flashloans/
    IMorpho public constant MORPHO = IMorpho(0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb);
    // NOTE: Balancer is an alternative zero-fee flashloan option
    // https://docs-v2.balancer.fi/reference/contracts/flash-loans.html

    error Unauthorized();
    error LessThanExpectedCollateralReceived();

    constructor(address _owner, address _factory, address _router) {
        require(_owner != address(0) && _factory != address(0) && _router != address(0), "zero address");
        owner = _owner;
        factory = ICollateralVaultFactory(_factory);
        router = _router;

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

    /// @notice Function for liquidating a Twyne collateral vault
    /// @dev Uses nested flash loans:
    ///      1. Flash loan target asset (for repaying vault debt)
    ///      2. Inside callback: Flash loan underlying, deposit to get eTokens
    ///      3. Liquidate (transfers eTokens to borrower, we become new owner)
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
        require(IEulerCollateralVault(collateralVault).canLiquidate(), "Collateral vault cannot be liquidated");

        // Cache useful values in struct to avoid stack too deep
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address collateralAsset = IEulerCollateralVault(collateralVault).asset(); // eToken
        address underlyingToken = IEVault(collateralAsset).asset();

        // Step 1: Perform all approvals for this tx
        _approveInternalLiq(targetAsset, collateralAsset, underlyingToken, collateralVault);

        // Step 2: Flash loan the target asset first (for repaying vault debt)
        // Inside the callback, we'll do a nested flash loan of the underlying
        // callbackType=0 for internal liquidation outer callback
        {
            InternalLiqParams memory params = InternalLiqParams({
                collateralVault: collateralVault,
                underlyingToken: underlyingToken,
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
        _resetApprovalsInternalLiq(targetAsset, collateralAsset, underlyingToken, collateralVault);
        emit Liquidation(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }

    /// @dev Helper to set approvals for internal liquidation
    function _approveInternalLiq(address targetAsset, address collateralAsset, address underlyingToken, address collateralVault) internal {
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt
        _safeApprove(targetAsset, address(MORPHO), type(uint).max); // needed for returning target asset flashloan
        _safeApprove(underlyingToken, address(MORPHO), type(uint).max); // needed for returning underlying flashloan
        _safeApprove(underlyingToken, collateralAsset, type(uint).max); // needed for depositing into euler vault
        _safeApprove(underlyingToken, router, type(uint).max); // needed for 1inch swap
        _safeApprove(collateralAsset, collateralVault, type(uint).max); // needed for liquidate() to transfer collateral to borrower
    }

    /// @dev Helper to reset approvals for internal liquidation
    function _resetApprovalsInternalLiq(address targetAsset, address collateralAsset, address underlyingToken, address collateralVault) internal {
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        _safeApprove(underlyingToken, address(MORPHO), 0);
        _safeApprove(underlyingToken, collateralAsset, 0);
        _safeApprove(underlyingToken, router, 0);
        _safeApprove(collateralAsset, collateralVault, 0);
    }

    /// @notice Function for handling the external liquidation of a Twyne collateral vault with external debt
    function liquidateExtLiquidatedCollateralVault(address collateralVault, bytes calldata dexData, uint minProfit) external returns (uint profit) {
        // Verify collateralVault address is actually a collateral vault that was externally liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(IEulerCollateralVault(collateralVault).isExternallyLiquidated(), "Collateral vault was not externally liquidated");

        if (IEulerCollateralVault(collateralVault).maxRepay() > 0) {
            return liquidateExtLiquidatedCollateralVaultWithDebt(collateralVault, dexData, minProfit);
        } else {
            return liquidateExtLiquidatedCollateralVaultZeroDebt(collateralVault);
        }
    }

    function liquidateExtLiquidatedCollateralVaultWithDebt(address collateralVault, bytes calldata dexData, uint minProfit) internal returns (uint profit) {
        // enable controller to enable liquidation
        evc.enableController(address(this), IEulerCollateralVault(collateralVault).intermediateVault()); // necessary for Euler Finance EVK borrowing

        // Cache useful values
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address underlyingToken = IEVault(IEulerCollateralVault(collateralVault).asset()).asset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt
        _safeApprove(targetAsset, address(MORPHO), type(uint).max); // needed for returning flashloan
        _safeApprove(underlyingToken, router, type(uint256).max); // needed for 1inch swap

        // Step 2: Borrow target asset with flashloan
        // callbackType=2 for external liquidation callback
        MORPHO.flashLoan(targetAsset, maxRepay, abi.encode(uint8(2), collateralVault, dexData));
        // Logic continues in onMorphoFlashLoan()

        // Step 6: Verify the profit exceeds the minimum
        uint postBalance = IERC20(targetAsset).balanceOf(address(this));
        profit = postBalance - initBalance;
        require(profit >= minProfit, "Liquidation is not sufficiently profitable");

        // For safety reasons, reset all approvals to zero
        _safeApprove(underlyingToken, router, 0);
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        emit ExtLiqWithDebt(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }


    function liquidateExtLiquidatedCollateralVaultZeroDebt(address collateralVault) internal returns (uint profit) {
        // enable controller to enable liquidation
        address intermediateVault = IEulerCollateralVault(collateralVault).intermediateVault();
        evc.enableController(address(this), intermediateVault); // necessary for Euler Finance EVK borrowing

        // Verify collateralVault address is actually a collateral vault that can be liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(IEulerCollateralVault(collateralVault).isExternallyLiquidated(), "Collateral vault was not externally liquidated");

        // Cache useful values
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address collateralToken = IEulerCollateralVault(collateralVault).asset();
        address underlyingToken = IEVault(collateralToken).asset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint).max); // needed for repaying debt in handleExternalLiquidation

        // Create batch to perform necessary steps to close out this position
        IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
        items[0] = IEVC.BatchItem({
            onBehalfOfAccount: address(this),
            targetContract: collateralVault,
            value: 0,
            data: abi.encodeCall(IEulerCollateralVault(collateralVault).handleExternalLiquidation, ())
        });
        items[1] = IEVC.BatchItem({
            onBehalfOfAccount: address(this),
            targetContract: intermediateVault,
            value: 0,
            data: abi.encodeCall(IEVault(collateralToken).liquidate, (collateralVault, collateralVault, 0, 0))
        });
        evc.batch(items);

        emit ExtLiqWithDebt(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }

    /// @notice Morpho flash loan callback - handles both internal liquidation (nested) and external liquidation
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
            MORPHO.flashLoan(params.underlyingToken, params.collateralFlashAmount, abi.encode(uint8(1), params.collateralVault, params.dexData));
            // After nested callback completes, target asset flash loan will be repaid

        } else if (callbackType == 1) {
            // Internal liquidation INNER callback (underlying flash loan)
            (, address collateralVault, bytes memory dexData) = abi.decode(data, (uint8, address, bytes));

            address collateralToken = IEulerCollateralVault(collateralVault).asset();

            // Step 4: Mint exactly the eTokens needed for collateralForBorrower transfer
            // Calculate B (debt) and C (user collateral) to get collateralForBorrower amount
            uint256 cForB;
            {
                address targetVault = IEulerCollateralVault(collateralVault).targetVault();
                (, uint256 B) = IEVault(targetVault).accountLiquidity(collateralVault, true);

                IVaultManager vaultManager = IVaultManager(IEulerCollateralVault(collateralVault).twyneVaultManager());
                address intermediateVault = IEulerCollateralVault(collateralVault).intermediateVault();
                uint256 userOwnedCollateral = IEulerCollateralVault(collateralVault).totalAssetsDepositedOrReserved()
                    - IEulerCollateralVault(collateralVault).maxRelease();
                uint256 C = vaultManager.oracleRouter().getQuote(
                    userOwnedCollateral,
                    collateralToken,
                    IEVault(intermediateVault).unitOfAccount()
                );

                cForB = IEulerCollateralVault(collateralVault).collateralForBorrower(B, C);
            }
            IEVault(collateralToken).mint(cForB, address(this));

            // Step 5: Use EVC batch to: liquidate → repay → redeem
            IEVC.BatchItem[] memory items = new IEVC.BatchItem[](3);
            items[0] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IEulerCollateralVault(collateralVault).liquidate, ())
            });
            items[1] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IEulerCollateralVault(collateralVault).repay, (type(uint).max))
            });
            items[2] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IEulerCollateralVault(collateralVault).redeemUnderlying, (type(uint).max, address(this)))
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

            address collateralToken = IEulerCollateralVault(collateralVault).asset();

            IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
            items[0] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(IEulerCollateralVault(collateralVault).handleExternalLiquidation, ())
            });
            items[1] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: address(IEulerCollateralVault(collateralVault).intermediateVault()),
                value: 0,
                data: abi.encodeCall(IEVault(collateralToken).liquidate, (collateralVault, collateralVault, 0, 0))
            });
            evc.batch(items);

            // Unwrap eToken to underlying
            IEVault(collateralToken).redeem(type(uint).max, address(this), address(this));

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