// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.24;

/// @notice Minimal Chainlink-style aggregator used to override an Aave price feed in fork tests via
///         vm.etch. Mirrors twyne-contracts test/mocks/MockAaveFeed.sol (8-decimal USD feed).
interface IAggregator {
    function decimals() external view returns (uint8);
    function latestAnswer() external view returns (int256);
}

contract MockAaveFeed is IAggregator {
    uint256 price;

    function setPrice(uint256 _price) external {
        price = _price;
    }

    function decimals() external pure returns (uint8) {
        return 8;
    }

    function latestAnswer() external view returns (int256) {
        return int256(price);
    }
}
