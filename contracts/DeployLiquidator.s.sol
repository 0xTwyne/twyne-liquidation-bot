// SPDX-License-Identifier: MIT

pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";

import {TwyneLiquidator} from "./TwyneLiquidator.sol";

import "forge-std/console2.sol";

contract DeployLiquidator is Script {
    address collateralVaultFactory;
    address router;

    function run() public {
        if (block.chainid == 1) {
            // mainnet
            collateralVaultFactory = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
            router = 0x111111125421cA6dc452d289314280a0f8842A65; // 1inch router
        } else {
            // To add a chain: add a branch with its CollateralVaultFactory and 1inch router.
            revert("chainid not supported");
        }

        uint256 deployerPrivateKey = vm.envUint("LIQUIDATOR_PRIVATE_KEY");
        address liquidatorOwner = vm.envAddress("LIQUIDATOR_OWNER");
        require(liquidatorOwner != address(0), "zero liquidator owner");
        address deployer = vm.addr(deployerPrivateKey);
        vm.startBroadcast(deployerPrivateKey);

        //        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
        //        vm.startBroadcast(deployer);

        TwyneLiquidator liquidator = new TwyneLiquidator(liquidatorOwner, collateralVaultFactory, router);

        console2.log("Deployer address: ", deployer);
        console2.log("Liquidator owner: ", liquidatorOwner);
        console2.log("Liquidator deployed at: ", address(liquidator));
    }
}
