"""Apply the Twyne 1.0.7 upgrade to an anvil fork (DEV-661).

The upgrade is Gnosis Safe transaction nonce 29 on Ethereum mainnet. Until it
executes, a fork of the latest block still runs the 1.0.6 contracts, and the whole
e2e harness plus the new HealthStatViewer need the upgraded contracts. This module
replays the 23 inner calls of that Safe transaction on the fork:

* a call whose target is the timelock is an
  ``execute(target, value, data, predecessor, salt)`` payload. The helper decodes it
  and sends the inner call from the timelock itself, so the waiting period of the
  timelock does not apply;
* every other call is sent from the Safe.

Both accounts are impersonated with anvil cheatcodes. The calldata lives in
``twyne_107_safe_nonce29.json``.

Set ``E2E_APPLY_TWYNE_107=0`` after the Safe transaction executes on mainnet; a fork
of a later block then already holds the upgraded implementations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from eth_abi import decode as abi_decode
from web3 import Web3

from .anvil import AnvilFork

_DATA_FILE = Path(__file__).with_name("twyne_107_safe_nonce29.json")

# ERC-1967 implementation slot: keccak256("eip1967.proxy.implementation") - 1.
_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# TimelockController.execute(address,uint256,bytes,bytes32,bytes32)
_EXECUTE_SELECTOR = "134008d3"

_GAS_MONEY = 10 * 10**18


def should_apply() -> bool:
    """True while the harness must apply the upgrade itself (the default)."""
    return os.environ.get("E2E_APPLY_TWYNE_107", "1") not in ("0", "false", "False")


def _load() -> dict:
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


def _implementation_of(fork: AnvilFork, proxy: str) -> str:
    raw = fork.get_storage(Web3.to_checksum_address(proxy), _IMPL_SLOT)
    return Web3.to_checksum_address("0x" + raw[-40:])


def is_upgraded(fork: AnvilFork) -> bool:
    """True when every proxy already points at its 1.0.7 implementation."""
    expected = _load()["expected_implementations"]
    return all(_implementation_of(fork, proxy) == Web3.to_checksum_address(impl) for proxy, impl in expected.items())


def apply_upgrade(fork: AnvilFork) -> None:
    """Replay Safe nonce 29 on the fork, then check both implementation slots.

    Does nothing when the chain already runs 1.0.7, so the helper is safe to call
    against a fork taken after the upgrade goes live.
    """
    spec = _load()
    if is_upgraded(fork):
        return

    safe = Web3.to_checksum_address(spec["safe"])
    timelock = Web3.to_checksum_address(spec["timelock"])
    fork.set_balance(safe, _GAS_MONEY)
    fork.set_balance(timelock, _GAS_MONEY)

    for call in spec["calls"]:
        target = Web3.to_checksum_address(call["to"])
        data = call["data"]
        if target == timelock:
            if not data.startswith(_EXECUTE_SELECTOR):
                raise RuntimeError(f"call {call['index']} to the timelock is not an execute() payload")
            inner_target, _value, inner_data, _predecessor, _salt = abi_decode(
                ["address", "uint256", "bytes", "bytes32", "bytes32"], bytes.fromhex(data[8:])
            )
            sender = timelock
            target = Web3.to_checksum_address(inner_target)
            payload = "0x" + inner_data.hex()
        else:
            sender = safe
            payload = "0x" + data

        # AnvilFork.send impersonates the sender, waits for the receipt and raises on a revert.
        try:
            fork.send(sender, target, payload, value=call["value"])
        except RuntimeError as ex:
            raise RuntimeError(f"Twyne 1.0.7 call {call['index']} to {target} failed: {ex}") from ex

    for proxy, impl in spec["expected_implementations"].items():
        got = _implementation_of(fork, proxy)
        if got != Web3.to_checksum_address(impl):
            raise RuntimeError(f"proxy {proxy} points at {got}, expected {impl}")
