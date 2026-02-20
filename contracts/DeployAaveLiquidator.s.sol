// SPDX-License-Identifier: MIT

pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";

import {TwyneAaveLiquidator} from "./TwyneAaveLiquidator.sol";

import "forge-std/console2.sol";

interface ICVF {
    function owner() external view returns (address);
    function collateralVaultBeacon(address targetVault) external view returns (address beacon);
}

contract DeployAaveLiquidator is Script {
    address collateralVaultFactory;
    address router;
    address aavePool;

    function run() public {
        if (block.chainid == 1) { // mainnet
            collateralVaultFactory = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
            router = 0x111111125421cA6dc452d289314280a0f8842A65; // 1inch router
            aavePool = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2; // Aave V3 Pool
        } else if (block.chainid == 8453) { // base
            collateralVaultFactory = 0x1666FE8Cf509E6B6eC8c1bc6a53674f6Ee1D0381;
            router = 0x111111125421cA6dc452d289314280a0f8842A65; // 1inch router (same across chains)
            aavePool = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5; // Aave V3 Pool on Base
        } else {
            revert("chainid not supported");
        }

        require(ICVF(collateralVaultFactory).collateralVaultBeacon(aavePool) != address(0), "aavepool is valid");

        uint256 deployerPrivateKey = vm.envUint("LIQUIDATOR_PRIVATE_KEY");
        address deployer = vm.addr(deployerPrivateKey);
        vm.startBroadcast(deployerPrivateKey);

//        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
//        vm.startBroadcast(deployer);

        TwyneAaveLiquidator liquidator = new TwyneAaveLiquidator(ICVF(collateralVaultFactory).owner(), collateralVaultFactory, router, aavePool);
        console2.log("Deployer address: ", deployer);
        console2.log("AAVE Liquidator deployed at: ", address(liquidator));
    }
}
