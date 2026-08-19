"""
Contract instance creation utilities.
"""

import functools
import json

from web3.contract import Contract

from .config_loader import ChainConfig


@functools.lru_cache(maxsize=None)
def _load_abi(abi_path: str) -> tuple:
    """Read and parse an ABI JSON file once, caching the result per path (P5).

    ``create_contract_instance`` is called in every vault init and on many hot
    paths; re-reading and re-parsing the (large) ABI JSON from disk each time is
    pure overhead since ABI files are immutable for the life of the process.

    Returns a tuple (immutable / hashable) so callers cannot accidentally mutate
    the shared cached ABI and corrupt it for every other contract instance.
    """
    with open(abi_path, "r", encoding="utf-8") as file:
        interface = json.load(file)
    return tuple(interface["abi"])


def create_contract_instance(address: str, abi_path: str, config: ChainConfig) -> Contract:
    """
    Create and return a Web3 contract instance.

    Args:
        address: The address of the contract.
        abi_path: Path to the ABI JSON file.
        config: Chain configuration containing the Web3 instance.

    Returns:
        Web3 contract instance.
    """
    # web3 accepts any iterable for the ABI; pass a fresh list built from the
    # cached tuple so web3's internal handling never mutates the cached object.
    abi = list(_load_abi(abi_path))

    return config.w3.eth.contract(address=address, abi=abi)
