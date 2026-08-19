# Fixed addresses for Base (chainid = 8453)
WETH=0x4200000000000000000000000000000000000006
eulerWETH=0x859160DB5841E5cfB8D3f144C6b3381A85A4b410
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

# Change addresses and RPC for current deployment
vaultManager=0xf7Ef58109aE78AbD5f6DEb48A4a8f12c8fbDd354
user_collateral_vault=0xc79ce2c340058321357a47937f23043e59d3b8e4
new_safety_buffer=10000 # original was 10000
RPC_URL="https://base.gateway.tenderly.co"

# Read the active Euler liquidator address from app/config.yaml (Base, chain 8453).
# This avoids hardcoding a stale address here — always reflects what the bot is
# configured to use.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
liquidator=$(python3 -c "import yaml; print(yaml.safe_load(open('${SCRIPT_DIR}/app/config.yaml'))['chains'][8453]['contracts']['EULER_LIQUIDATOR_ADDRESS'])")
echo "Using liquidator contract: $liquidator"

ether=1000000000000000000

# Secrets are read from environment variables, NOT from a `.env` file on
# disk. Wrap invocations of this script with `op run` so the private key
# never lands on host disk:
#
#   op run --env-file=.env.template -- bash liquidationSetup.sh
#
# KEYSTORE SETUP (one-time, per machine):
#   cast wallet import liquidation-bot --interactive
#   # Enter the private key when prompted; it is stored encrypted in the
#   # system keystore (~/.foundry/keystores/liquidation-bot).
#
# The private key is NEVER passed on the command line (not visible in ps /
# /proc/*/cmdline). Only the keystore account name is used with --account.
: "${LIQUIDATOR_EOA:?Not set. Run via: op run --env-file=.env.template -- bash $0}"

USER=${LIQUIDATOR_EOA}

# Keystore account name — create once with:
#   cast wallet import liquidation-bot --interactive
# Override via: KEYSTORE_ACCOUNT=my-account bash liquidationSetup.sh
KEYSTORE_ACCOUNT=${KEYSTORE_ACCOUNT:-liquidation-bot}

# NOTE: If you want to pause after each cast command, add this below each cast call
# printf "%s " "Press enter to continue"
# read ans

# print balance and borrows
echo "balanceOf()"
cast call $user_collateral_vault "balanceOf(address)(uint)" $user_collateral_vault --rpc-url $RPC_URL -vv
echo "maxRelease()"
cast call $user_collateral_vault "maxRelease()(uint)" --rpc-url $RPC_URL -vv
echo "maxRepay()"
cast call $user_collateral_vault "maxRepay()(uint)" --rpc-url $RPC_URL -vv
echo ""

# print current safety buffer
cast call $vaultManager "externalLiqBuffers(address)(uint16)" $eulerWETH --rpc-url $RPC_URL -vv

# Set new_safety_buffer
cast send $vaultManager "setExternalLiqBuffer(address,uint16)()" $eulerWETH $new_safety_buffer --gas-limit 8000000 --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv
printf "%s " "Press enter to continue"
read ans

# print current safety buffer
cast call $vaultManager "externalLiqBuffers(address)(uint16)" $eulerWETH --rpc-url $RPC_URL -vv

# call canLiquidate on the vault
cast call $user_collateral_vault "canLiquidate()(bool)" --rpc-url $RPC_URL -vv

# ######################################

# Manually withdraw

# cast send $user_collateral_vault "withdraw(uint256,address)()" 162000000000000 $USER --gas-limit 8000000 --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv

# ######################################

# Transfer vaultManager ownership
# NEW_OWNER=0xNEW_OWNER_ADDRESS

# cast call $vaultManager "owner()(address)" --rpc-url $RPC_URL -vv
# cast send $vaultManager "transferOwnership(address)()" $NEW_OWNER --gas-limit 8000000 --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv
# cast call $vaultManager "owner()(address)" --rpc-url $RPC_URL -vv

# ######################################

# Manually liquidate a vault

# # print balance and borrows
# echo "balanceOf()"
# cast call $user_collateral_vault "balanceOf(address)(uint)" $user_collateral_vault --rpc-url $RPC_URL -vv
# echo "maxRelease()"
# cast call $user_collateral_vault "maxRelease()(uint)" --rpc-url $RPC_URL -vv
# echo "maxRepay()"
# cast call $user_collateral_vault "maxRepay()(uint)" --rpc-url $RPC_URL -vv
# echo ""

# cast send $liquidator "liquidateCollateralVault(address,bytes,uint256)(uint256)" $user_collateral_vault "0x07ed23790000000000000000000000006ea77f83ec8693666866ece250411c974ab962a80000000000000000000000004200000000000000000000000000000000000006000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda029130000000000000000000000006ea77f83ec8693666866ece250411c974ab962a800000000000000000000000072978bb38c805485b440967eb2b610f3bb0b059300000000000000000000000000000000000000000000000000005af39354027e0000000000000000000000000000000000000000000000000000000000042b090000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000012000000000000000000000000000000000000000000000000000000000000002aa00000000000000000000000000000000028c00025e00004e00a0744c8c09420000000000000000000000000000000000000690cbe4bdd538d6e9b379bff5fe72c3d67a521de500000000000000000000000000000000000000000000000000000045d9c9364700a007e5c0d20000000000000000000000000000000000000000000000000001ec0000b05121000000000022d473030f116ddee9f6b43ac78ba34200000000000000000000000000000000000006004487517c45000000000000000000000000420000000000000000000000000000000000000600000000000000000000000076578ecf9a141296ec657847fb45b0585bcda3a600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000412076578ecf9a141296ec657847fb45b0585bcda3a60064750283bc0000000000000000000000007b4c560f33a71a9f7a500af3c4c65b46fbbafdb70000000000000000000000004200000000000000000000000000000000000006000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda0291300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000042b09000000000000000000000000000000000000000000000000000000006851830900000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000080a06c4eca27833589fcd6edb6e08f4c7c32d4f71b54bda02913111111125421ca6dc452d289314280a0f8842a650000000000000000000000000000000000000000000011d252a3" 100 --gas-limit 8000000 --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv

# # print balance and borrows
# echo "balanceOf()"
# cast call $user_collateral_vault "balanceOf(address)(uint)" $user_collateral_vault --rpc-url $RPC_URL -vv
# echo "maxRelease()"
# cast call $user_collateral_vault "maxRelease()(uint)" --rpc-url $RPC_URL -vv
# echo "maxRepay()"
# cast call $user_collateral_vault "maxRepay()(uint)" --rpc-url $RPC_URL -vv
# echo ""

# ######################################

# Manually swap with 1inch API

# # Approve 1inch swap router
# router=0x111111125421cA6dc452d289314280a0f8842A65
# leftoverexch=0x6Ea77f83eC8693666866ecE250411C974aB962a8

# cast call $WETH "approve(address,uint256)(bool)" $leftoverexch $ether --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv

# # Do swap using raw call, because the 1inch API returns the function signature and everything else in one block
# cast send $router "0x07ed23790000000000000000000000006ea77f83ec8693666866ece250411c974ab962a80000000000000000000000004200000000000000000000000000000000000006000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda029130000000000000000000000006ea77f83ec8693666866ece250411c974ab962a800000000000000000000000063bca2fa05bebde99666b68753be81fb504a5bc7000000000000000000000000000000000000000000000000000009184e72a0000000000000000000000000000000000000000000000000000000000000006a950000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000012000000000000000000000000000000000000000000000000000000000000002aa00000000000000000000000000000000028c00025e00004e00a0744c8c09420000000000000000000000000000000000000690cbe4bdd538d6e9b379bff5fe72c3d67a521de500000000000000000000000000000000000000000000000000000006fc23ac0000a007e5c0d20000000000000000000000000000000000000000000000000001ec0000b05121000000000022d473030f116ddee9f6b43ac78ba34200000000000000000000000000000000000006004487517c45000000000000000000000000420000000000000000000000000000000000000600000000000000000000000076578ecf9a141296ec657847fb45b0585bcda3a600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000412076578ecf9a141296ec657847fb45b0585bcda3a60064750283bc0000000000000000000000007b4c560f33a71a9f7a500af3c4c65b46fbbafdb70000000000000000000000004200000000000000000000000000000000000006000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda0291300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000006a95000000000000000000000000000000000000000000000000000000006851794e00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000080a06c4eca27833589fcd6edb6e08f4c7c32d4f71b54bda02913111111125421ca6dc452d289314280a0f8842a650000000000000000000000000000000000000000000011d252a3" --gas-limit 8000000 --account $KEYSTORE_ACCOUNT --rpc-url $RPC_URL -vv
