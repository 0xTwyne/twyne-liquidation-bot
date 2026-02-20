"""
Config Loader module - part of multi chain refactor
"""

import json
import os
from typing import Any, Dict, Optional

import yaml
from web3 import Web3


class Web3Singleton:
    """
    Singleton class to manage w3 object creation per RPC URL
    """

    _instances = {}

    @staticmethod
    def get_instance(rpc_url: Optional[str] = None):
        """
        Set up a Web3 instance using the RPC URL from environment variables or passed parameter.
        Maintains separate instances per unique RPC URL.
        """

        if rpc_url not in Web3Singleton._instances:
            Web3Singleton._instances[rpc_url] = Web3(Web3.HTTPProvider(rpc_url))

        return Web3Singleton._instances[rpc_url]


def setup_w3(rpc_url: Optional[str] = None) -> Web3:
    """
    Get the Web3 instance from the singleton class

    Args:
        rpc_url (Optional[str]): Optional RPC URL to override environment variable

    Returns:
        Web3: Web3 instance.
    """
    return Web3Singleton.get_instance(rpc_url)


class ChainConfig:
    """
    Chain Config object to access config variables
    """

    required_env_vars = [
        "LIQUIDATOR_EOA",
        "LIQUIDATOR_PRIVATE_KEY",
        "ONEINCH_API_KEY",
        "RISK_DASHBOARD_URL",
        "BASE_RPC_URL",
        # "NOTIFICATION_URL",  # Optional
    ]

    def __init__(self, chain_id: int, global_config: Dict[str, Any], chain_config: Dict[str, Any]):
        self.CHAIN_ID = chain_id
        self.CHAIN_NAME = chain_config["name"]
        self._global = global_config
        self._chain = chain_config

        # validate env
        self.validate()
        # Load global EOA settings
        self.LIQUIDATOR_EOA = Web3.to_checksum_address(os.environ["LIQUIDATOR_EOA"])
        self.LIQUIDATOR_EOA_PRIVATE_KEY = os.environ["LIQUIDATOR_PRIVATE_KEY"]
        self.ONEINCH_API_KEY = os.environ["ONEINCH_API_KEY"]
        self.NOTIFICATION_URL = os.environ.get("NOTIFICATION_URL", "")
        self.RISK_DASHBOARD_URL = os.environ["RISK_DASHBOARD_URL"]

        # Load Slack mention IDs and Twyne EOA vaults from env (comma-separated)
        slack_ids_raw = os.environ.get("SLACK_MENTION_IDS", "")
        self.SLACK_MENTION_IDS = [s.strip() for s in slack_ids_raw.split(",") if s.strip()]

        eoa_vaults_raw = os.environ.get("TWYNE_EOA_VAULTS", "")
        self.TWYNE_EOA_VAULTS = [s.strip() for s in eoa_vaults_raw.split(",") if s.strip()]

        # Load chain-specific RPC from env using RPC_NAME from config
        self.RPC_URL = os.environ[self._chain["RPC_NAME"]]
        if not self.RPC_URL:
            raise ValueError(f"Missing RPC URL for {self._chain['name']}. Env var {self._chain['RPC_NAME']} not found")

        self.w3 = setup_w3(self.RPC_URL)
        self.mainnet_w3 = setup_w3(os.environ.get("MAINNET_RPC_URL", self.RPC_URL))

        # Set chain-specific paths
        self.LOGS_PATH = f"{self._global['LOGS_PATH']}/{self._chain['name']}_monitor.log"
        self.SAVE_STATE_PATH = f"{self._global['SAVE_STATE_PATH']}/{self._chain['name']}_state.json"

        with open(self._global["EVC_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        abi = interface["abi"]

        self.evc = self.w3.eth.contract(address=self.EVC, abi=abi)

        # Twyne
        with open(self._global["CVAULT_FACTORY_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        abi = interface["abi"]

        self.collateral_vault_factory = self.w3.eth.contract(address=self.CVAULT_FACTORY, abi=abi)

        with open(self._global["ERC20_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        abi = interface["abi"]

        self.USDC = self.w3.eth.contract(address=self.USDC, abi=abi)

        self.WETH = self.w3.eth.contract(address=self.WETH, abi=abi)

        with open(self._global["LIQUIDATOR_CONTRACT_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        euler_abi = interface["abi"]

        self.euler_liqbot = self.w3.eth.contract(address=Web3.to_checksum_address(self.EULER_LIQUIDATOR_ADDRESS), abi=euler_abi)

        with open(self._global["AAVE_LIQUIDATOR_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        aave_abi = interface["abi"]

        self.aave_liqbot = self.w3.eth.contract(address=Web3.to_checksum_address(self.AAVE_LIQUIDATOR_ADDRESS), abi=aave_abi)


    def __getattr__(self, name: str) -> Any:
        """Look up config values in chain-specific, then contracts, then global config."""
        if name in self._chain:
            return self._chain[name]
        if name in self._chain.get("contracts", {}):
            return self._chain["contracts"][name]
        if name in self._global:
            return self._global[name]
        raise AttributeError(f"Config has no attribute '{name}'")

    def validate(self) -> None:
        """
        Validates that all required environment variables are set.
        Raises an error if any are missing.
        """
        missing_keys = [key for key in self.required_env_vars if not os.getenv(key)]
        if missing_keys:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_keys)}")


def load_chain_config(chain_id: int) -> ChainConfig:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(os.path.dirname(current_dir), "config.yaml")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Config file not found at {config_path}") from exc
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing YAML file: {e}") from e

    if chain_id not in config["chains"]:
        raise ValueError(f"No configuration found for chain ID {chain_id}")

    return ChainConfig(chain_id=chain_id, global_config=config["global"], chain_config=config["chains"][chain_id])
