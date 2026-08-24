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
}

// Liquidation contract for Twyne protocol
// Assumes v1 of Twyne, integration exists only with Euler Finance
contract TwyneLiquidator {
    error Swapper_EmptyError();

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
    function liquidateCollateralVault(address collateralVault, bytes calldata dexData, uint256 minProfit)
        external
        payable
        returns (uint256 profit)
    {
        // Verify collateralVault address is actually a collateral vault that can be liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(IEulerCollateralVault(collateralVault).canLiquidate(), "Collateral vault cannot be liquidated");

        // Cache useful values
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint256 initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint256 maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address collateralAsset = IEulerCollateralVault(collateralVault).asset(); // eToken
        address underlyingToken = IEVault(collateralAsset).asset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint256).max); // needed for repaying debt
        _safeApprove(targetAsset, address(MORPHO), type(uint256).max); // needed for returning flashloan
        _safeApprove(collateralAsset, collateralVault, type(uint256).max); // needed for deferred borrower payout in checkVaultStatus
        _safeApprove(underlyingToken, router, type(uint256).max); // needed for 1inch swap

        // Step 2: Borrow target asset with flashloan
        MORPHO.flashLoan(targetAsset, maxRepay, abi.encode(collateralVault, dexData, true));
        // Logic continues in onMorphoFlashLoan()

        // Step 6: Verify the profit exceeds the minimum
        uint256 postBalance = IERC20(targetAsset).balanceOf(address(this));
        profit = postBalance - initBalance;
        require(profit >= minProfit, "Liquidation is not sufficiently profitable");

        // For safety reasons, reset all approvals to zero
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        _safeApprove(collateralAsset, collateralVault, 0);
        _safeApprove(underlyingToken, router, 0);
        emit Liquidation(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }

    /// @notice Function for handling the external liquidation of a Twyne collateral vault with external debt
    function liquidateExtLiquidatedCollateralVault(address collateralVault, bytes calldata dexData, uint256 minProfit)
        external
        returns (uint256 profit)
    {
        // Verify collateralVault address is actually a collateral vault that was externally liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(
            IEulerCollateralVault(collateralVault).isExternallyLiquidated(),
            "Collateral vault was not externally liquidated"
        );

        if (IEulerCollateralVault(collateralVault).maxRepay() > 0) {
            return liquidateExtLiquidatedCollateralVaultWithDebt(collateralVault, dexData, minProfit);
        } else {
            return liquidateExtLiquidatedCollateralVaultZeroDebt(collateralVault);
        }
    }

    function liquidateExtLiquidatedCollateralVaultWithDebt(
        address collateralVault,
        bytes calldata dexData,
        uint256 minProfit
    ) internal returns (uint256 profit) {
        // enable controller to enable liquidation
        evc.enableController(address(this), IEulerCollateralVault(collateralVault).intermediateVault()); // necessary for Euler Finance EVK borrowing

        // Cache useful values
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint256 initBalance = IERC20(targetAsset).balanceOf(address(this));
        uint256 maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address underlyingToken = IEVault(IEulerCollateralVault(collateralVault).asset()).asset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint256).max); // needed for repaying debt
        _safeApprove(targetAsset, address(MORPHO), type(uint256).max); // needed for returning flashloan
        _safeApprove(underlyingToken, router, type(uint256).max); // needed for 1inch swap

        // Step 2: Borrow target asset with flashloan
        MORPHO.flashLoan(targetAsset, maxRepay, abi.encode(collateralVault, dexData, false));
        // Logic continues in onMorphoFlashLoan()

        // Step 6: Verify the profit exceeds the minimum
        uint256 postBalance = IERC20(targetAsset).balanceOf(address(this));
        profit = postBalance - initBalance;
        require(profit >= minProfit, "Liquidation is not sufficiently profitable");

        // For safety reasons, reset all approvals to zero
        _safeApprove(underlyingToken, router, 0);
        _safeApprove(targetAsset, collateralVault, 0);
        _safeApprove(targetAsset, address(MORPHO), 0);
        emit ExtLiqWithDebt(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }

    function liquidateExtLiquidatedCollateralVaultZeroDebt(address collateralVault) internal returns (uint256 profit) {
        // enable controller to enable liquidation
        address intermediateVault = IEulerCollateralVault(collateralVault).intermediateVault();
        evc.enableController(address(this), intermediateVault); // necessary for Euler Finance EVK borrowing

        // Verify collateralVault address is actually a collateral vault that can be liquidated
        require(factory.isCollateralVault(collateralVault), "The input address is not a Twyne collateral vault");
        require(
            IEulerCollateralVault(collateralVault).isExternallyLiquidated(),
            "Collateral vault was not externally liquidated"
        );

        // Cache useful values
        address targetAsset = IEulerCollateralVault(collateralVault).targetAsset();
        uint256 maxRepay = IEulerCollateralVault(collateralVault).maxRepay();
        address collateralToken = IEulerCollateralVault(collateralVault).asset();
        address underlyingToken = IEVault(collateralToken).asset();

        // Step 1: Perform all approvals for this tx
        _safeApprove(targetAsset, collateralVault, type(uint256).max); // needed for repaying debt in handleExternalLiquidation

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

        _safeApprove(targetAsset, collateralVault, 0);
        emit ExtLiqZeroDebt(collateralVault, targetAsset, underlyingToken, maxRepay, profit);
    }

    function onMorphoFlashLoan(
        uint256,
        /*amount*/
        bytes calldata data
    )
        external
    {
        require(msg.sender == address(MORPHO), "This function should only be called by Morpho during a flashloan");
        (address collateralVault, bytes memory dexData, bool isInternal) = abi.decode(data, (address, bytes, bool));

        address collateralToken = IEulerCollateralVault(collateralVault).asset();

        // Step 3: Branching logic depending on whether the vault is liquidatable or was externally liquidated
        // Use EVC batch to liquidate vault, pay debts, and withdraw everything
        // At the end of this batch, the liquidator holds the underlying asset (WETH, not eulerWETH)
        if (isInternal) {
            // Standard liquidation flow
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
                data: abi.encodeCall(IEulerCollateralVault(collateralVault).repay, (type(uint256).max))
            });
            items[2] = IEVC.BatchItem({
                onBehalfOfAccount: address(this),
                targetContract: collateralVault,
                value: 0,
                data: abi.encodeCall(
                    IEulerCollateralVault(collateralVault).withdraw, (type(uint256).max, address(this))
                )
            });
            evc.batch(items);

            // After batch: we hold remaining eToken shares (deferred borrower payout already pulled).
            // Redeem all remaining eTokens for underlying.
            uint256 eTokenBalance = IERC20(collateralToken).balanceOf(address(this));
            if (eTokenBalance > 0) {
                IEVault(collateralToken).redeem(eTokenBalance, address(this), address(this));
            }
        } else {
            // External liquidation flow - this logic is only relevant when there is target asset debt
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

            // Now need to unwrap the eToken to Token (convert eWETH into WETH) to make the 1inch swap easier
            IEVault(collateralToken).redeem(type(uint256).max, address(this), address(this));
        }

        // Step 4: Use 1inch to swap the underlying asset back to the target asset (flashloaned asset)
        (bool isSuccess, bytes memory returnData) = router.call(dexData);
        if (!isSuccess) {
            if (returnData.length > 0) {
                assembly {
                    revert(add(32, returnData), mload(returnData))
                }
            }
            revert Swapper_EmptyError();
        }

        // Step 5: When the callback ends, Morpho does transferFrom to return the flashloaned assets
    }

    function sweep(address token, uint256 amount) external onlyOwner {
        _safeTransfer(token, msg.sender, amount);
    }

    function sweepETH(uint256 amount) external onlyOwner {
        (bool success,) = payable(owner).call{value: amount}("");
        require(success, "ETH transfer failed");
    }

    function setRouter(address _router) external onlyOwner {
        require(_router != address(0), "zero address");
        router = _router;
    }

    /// @notice Safe approve function that handles non-standard ERC20 tokens like USDT
    /// @dev copied from Solady
    function _safeApprove(address token, address to, uint256 amount) internal {
        assembly ("memory-safe") {
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
        assembly ("memory-safe") {
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
