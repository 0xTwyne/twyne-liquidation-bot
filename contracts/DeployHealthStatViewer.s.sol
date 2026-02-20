// SPDX-License-Identifier: MIT

pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";

import {HealthStatViewer} from "./HealthStatViewer.sol";

import "forge-std/console2.sol";

contract DeployHealthStatViewer is Script {
    address aavePool;

    function run() public {
        if (block.chainid == 1) { // mainnet
            aavePool = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
        } else if (block.chainid == 8453) { // base
            aavePool = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
        } else {
            revert("chainid not supported");
        }

        uint256 deployerPrivateKey = vm.envUint("LIQUIDATOR_PRIVATE_KEY");
        address deployer = vm.addr(deployerPrivateKey);
        vm.startBroadcast(deployerPrivateKey);

        HealthStatViewer hsv = new HealthStatViewer(aavePool);

        console2.log("Deployer address: ", deployer);
        console2.log("HealthStatViewer deployed at: ", address(hsv));
    }
}
