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

# Fixed addresses for Ethereum mainnet
WETH=0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
USDC=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48

# Read the active Euler liquidator address from app/config.yaml (mainnet, chain 1).
# This avoids hardcoding a stale address here — always reflects what the bot is
# configured to use. To sweep the Aave liquidator instead, use AAVE_LIQUIDATOR_ADDRESS.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
liq_contract=$(cd "${SCRIPT_DIR}" && uv run python -c "import yaml; print(yaml.safe_load(open('app/config.yaml'))['chains'][1]['contracts']['EULER_LIQUIDATOR_ADDRESS'])")
echo "Using liquidator contract: $liq_contract"

# Secrets are read from environment variables, NOT from a `.env` file on
# disk. Wrap invocations of this script with `op run` so the keys never
# land on host disk:
#
#   op run --env-file=.env.template -- bash sweep-liq-funds.sh
#
USER=${LIQUIDATOR_EOA:?Not set. Run via: op run --env-file=.env.template -- bash $0}
RPC_URL=${MAINNET_RPC_URL:?Not set. Run via: op run --env-file=.env.template -- bash $0}

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
