// SPDX-License-Identifier: GPL-2.0-or-later

pragma solidity >=0.8.0;

/// @title ISwapper
/// @custom:security-contact security@euler.xyz
/// @author Euler Labs (https://www.eulerlabs.com/)
/// @notice Interface of helper contracts, which handle swapping of assets for Euler Vault Kit
interface ISwapper {
    /// @title SwapParams
    /// @notice This struct holds all the parameters needed to carry out a swap
    struct SwapParams {
        // An id of the swap handler to use
        bytes32 handler;
        // Swap mode to execute
        // 0 - exact input swap
        // 1 - exect output swap
        // 2 - exact output swap and repay, targeting a debt amount of an account
        uint256 mode;
        // An EVC compatible account address, used e.g. as receiver of repay in swap and repay mode
        address account;
        // Sold asset
        address tokenIn;
        // Bought asset
        address tokenOut;
        // Vault to which the unused input in exact output swap will be deposited back
        address vaultIn;
        // An EVC compatible account address, to which the unused input in exact output swap will be deposited back
        address accountIn;
        // In swapping modes (0 and 1) - address of the intended recipient of the bought tokens
        // In swap and repay mode (2) - address of the liability vault of the account, where to repay debt
        // Note that if the swap uses off-chain encoded payload, the receiver might be ignored. The user
        // should verify the assets are in fact in the receiver address after the swap
        address receiver;
        // In exact input mode (0) - ignored
        // In exact output mode (1) - amount of `tokenOut` to buy
        // In swap and repay mode (2) - amount of debt the account should have after swap and repay.
        //    To repay all debt without leaving any dust, set this to zero.
        uint256 amountOut;
        // Auxiliary payload for swap providers. For GenericHandler it's an abi encoded tuple: target contract address
        // and call data
        bytes data;
    }
}
