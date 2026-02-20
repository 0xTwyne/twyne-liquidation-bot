#!/bin/bash

# This script is used to withdraw liquidation profits from the liquidation bot
# The script should be called by the owner of the liquidation bot and will be sent to this owner's address

# Fixed addresses for base
WETH=0x4200000000000000000000000000000000000006
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
liq_contract=0x907d9f1420ab3c1ecc444E3E75B75CedA7454A68

# Load user addresses and keys from .env
if [ -f .env ]; then
  export $(grep -E '^LIQUIDATOR_EOA=' .env | xargs)
  export $(grep -E '^LIQUIDATOR_PRIVATE_KEY=' .env | xargs)
  export $(grep -E '^BASE_RPC_URL=' .env | xargs)
else
  echo ".env file not found in the current directory."
  exit 1
fi

USER=${LIQUIDATOR_EOA:?LIQUIDATOR_EOA not set in .env}
USER_KEY=${LIQUIDATOR_PRIVATE_KEY:?LIQUIDATOR_PRIVATE_KEY not set in .env}
RPC_URL=${BASE_RPC_URL:?BASE_RPC_URL not set in .env}

# Withdraw all USDC in the liquidator bot contract
usdcBal=$(cast call $USDC "balanceOf(address)(uint256)" $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "USDC balance: $usdcBal"
if ((usdcBal > 0)); then
    echo "Sweeping USDC..."
    cast send $liq_contract "sweep(address,uint256)()" $USDC $usdcBal --rpc-url $RPC_URL --gas-limit 8000000 --private-key $USER_KEY
fi

# Withdraw all WETH in the liquidator bot contract
wethBal=$(cast call $WETH "balanceOf(address)(uint256)" $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "WETH balance: $wethBal"
if ((wethBal > 0)); then
    echo "Sweeping WETH..."
    cast send $liq_contract "sweep(address,uint256)()" $WETH $wethBal --rpc-url $RPC_URL --gas-limit 8000000 --private-key $USER_KEY
fi

# Withdraw all ETH in the liquidator bot contract
ethBal=$(cast balance $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "ETH balance: $ethBal"
if ((ethBal > 0)); then
    echo "Sweeping WETH..."
    cast send $liq_contract "sweepETH(uint256)()" $ethBal --rpc-url $RPC_URL --gas-limit 8000000 --private-key $USER_KEY
fi