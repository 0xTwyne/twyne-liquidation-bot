"""
Contract instance creation utilities.
"""

import json

from web3.contract import Contract

from .config_loader import ChainConfig


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
    with open(abi_path, "r", encoding="utf-8") as file:
        interface = json.load(file)
    abi = interface["abi"]

    return config.w3.eth.contract(address=address, abi=abi)
