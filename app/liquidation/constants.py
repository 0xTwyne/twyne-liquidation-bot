"""Named constants for liquidation money-math.

Centralizes magic values that previously appeared as inline literals or repeated
local variables across the vault modules, so their provenance is documented in one
place and a single edit changes every consumer (DEV-557 R4).
"""

# LTV basis-point denominator. Twyne `maxTwyneLTVs` / liquidation LTVs are scaled
# to the integer range [0, 10000] (i.e. 10000 == 100%). Dividing a value scaled by
# this factor recovers the unscaled quantity.
LTV_MAXFACTOR = 10000

# Byte offsets of the encoded `minReturnAmount` (a big-endian uint256) inside 1inch
# v6 swap calldata. The min-return occupies the 32-byte slice [196:228] of the
# decoded swap-data bytes.
ONEINCH_MIN_RETURN_OFFSET = 196
ONEINCH_MIN_RETURN_END = 228

# Multiplier applied to `eth.estimate_gas(...)` on the liquidation broadcast path,
# providing headroom over the node's estimate so the broadcast does not run out of
# gas if on-chain state shifts slightly between estimate and execution.
GAS_ESTIMATE_BUFFER = 2

# Swap-input safety margin divisor: the amount swapped is reduced by
# `amount // SWAP_MARGIN_DIVISOR` (i.e. 0.1%) to leave a small buffer against
# rounding / fee drift between the off-chain quote and on-chain execution.
SWAP_MARGIN_DIVISOR = 1000
