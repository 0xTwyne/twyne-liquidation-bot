// SPDX-License-Identifier: MIT

pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";

import {TwyneLiquidator} from "./TwyneLiquidator.sol";

import "forge-std/console2.sol";

interface ICVF {
    function owner() external view returns (address);
}

contract DeployLiquidator is Script {
    address collateralVaultFactory;
    address router;

    function run() public {
        if (block.chainid == 1) { // mainnet
            collateralVaultFactory = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
            router = 0x111111125421cA6dc452d289314280a0f8842A65; // 1inch router
        } else if (block.chainid == 8453) { // base
            collateralVaultFactory = 0x1666FE8Cf509E6B6eC8c1bc6a53674f6Ee1D0381;
            router = 0x111111125421cA6dc452d289314280a0f8842A65; // 1inch router (same across chains)
        } else {
            revert("chainid not supported");
        }

        uint256 deployerPrivateKey = vm.envUint("LIQUIDATOR_PRIVATE_KEY");
        address deployer = vm.addr(deployerPrivateKey);
        vm.startBroadcast(deployerPrivateKey);

//        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
//        vm.startBroadcast(deployer);

        TwyneLiquidator liquidator = new TwyneLiquidator(ICVF(collateralVaultFactory).owner(), collateralVaultFactory, router);

        console2.log("Deployer address: ", deployer);
        console2.log("Liquidator deployed at: ", address(liquidator));
    }
}
