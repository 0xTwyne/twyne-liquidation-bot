#!/bin/bash

# This script withdraws liquidation profits from the liquidation bot contract.
# The script must be called by the owner of the liquidation bot.
#
# KEYSTORE SETUP (one-time, per machine):
#   cast wallet import liquidation-bot --interactive
#   # Enter the private key when prompted; it is stored encrypted in the
#   # system keystore (~/.foundry/keystores/liquidation-bot).
#
# RUNNING:
#   op run --env-file=.env.template -- bash sweep-liq-funds.sh
#
# The private key is NEVER passed on the command line (not visible in ps /
# /proc/*/cmdline). Only the keystore account name is used with --account.

# Fixed addresses for base
WETH=0x4200000000000000000000000000000000000006
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
liq_contract=0x907d9f1420ab3c1ecc444E3E75B75CedA7454A68

# Secrets are read from environment variables, NOT from a `.env` file on
# disk. Wrap invocations of this script with `op run` so the keys never
# land on host disk:
#
#   op run --env-file=.env.template -- bash sweep-liq-funds.sh
#
USER=${LIQUIDATOR_EOA:?Not set. Run via: op run --env-file=.env.template -- bash $0}
RPC_URL=${BASE_RPC_URL:?Not set. Run via: op run --env-file=.env.template -- bash $0}

# Keystore account name — create once with:
#   cast wallet import liquidation-bot --interactive
# Override via: KEYSTORE_ACCOUNT=my-account bash sweep-liq-funds.sh
KEYSTORE_ACCOUNT=${KEYSTORE_ACCOUNT:-liquidation-bot}

# Withdraw all USDC in the liquidator bot contract
usdcBal=$(cast call $USDC "balanceOf(address)(uint256)" $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "USDC balance: $usdcBal"
if ((usdcBal > 0)); then
    echo "Sweeping USDC..."
    cast send $liq_contract "sweep(address,uint256)()" $USDC $usdcBal --rpc-url $RPC_URL --gas-limit 8000000 --account $KEYSTORE_ACCOUNT
fi

# Withdraw all WETH in the liquidator bot contract
wethBal=$(cast call $WETH "balanceOf(address)(uint256)" $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "WETH balance: $wethBal"
if ((wethBal > 0)); then
    echo "Sweeping WETH..."
    cast send $liq_contract "sweep(address,uint256)()" $WETH $wethBal --rpc-url $RPC_URL --gas-limit 8000000 --account $KEYSTORE_ACCOUNT
fi

# Withdraw all ETH in the liquidator bot contract
ethBal=$(cast balance $liq_contract --rpc-url $RPC_URL | cut -d " " -f 1)
echo "ETH balance: $ethBal"
if ((ethBal > 0)); then
    echo "Sweeping ETH..."
    cast send $liq_contract "sweepETH(uint256)()" $ethBal --rpc-url $RPC_URL --gas-limit 8000000 --account $KEYSTORE_ACCOUNT
fi
