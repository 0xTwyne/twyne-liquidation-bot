"""
Config Loader module - part of multi chain refactor
"""

import json
import os
from typing import Any, Dict, FrozenSet, Optional, Tuple

import requests
import yaml
from requests.adapters import HTTPAdapter
from web3 import Web3

# Worker count for the AccountMonitor thread pool (P4 / DEV-555).
# Overridable at deploy time via the MONITOR_WORKER_COUNT env var.
# The default of 32 is preserved for backward compatibility; operators
# running against rate-limited providers should lower this to match their
# request-per-second budget.
MONITOR_WORKER_COUNT: int = int(os.environ.get("MONITOR_WORKER_COUNT", "32"))


class Web3Singleton:
    """
    Singleton class to manage w3 object creation per RPC URL.

    Each instance is created with a requests Session whose HTTPAdapter is
    sized to ``MONITOR_WORKER_COUNT`` connections so that concurrent worker
    threads never queue on the default 10-connection pool or hammer the
    provider into 429s (P4 / DEV-555).
    """

    _instances = {}

    @staticmethod
    def get_instance(rpc_url: Optional[str] = None):
        """
        Set up a Web3 instance using the RPC URL from environment variables or passed parameter.
        Maintains separate instances per unique RPC URL.
        """

        if rpc_url not in Web3Singleton._instances:
            session = requests.Session()
            # Size the connection pool to at least MONITOR_WORKER_COUNT so that
            # all worker threads can hold an open connection simultaneously.
            # pool_connections controls how many distinct host:port pools to keep;
            # pool_maxsize controls how many connections per pool.  Both are set
            # to MONITOR_WORKER_COUNT (one RPC host, many concurrent callers).
            adapter = HTTPAdapter(
                pool_connections=MONITOR_WORKER_COUNT,
                pool_maxsize=MONITOR_WORKER_COUNT,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            Web3Singleton._instances[rpc_url] = Web3(Web3.HTTPProvider(rpc_url, session=session))

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
        # "NOTIFICATION_URL",  # Optional
        # Chain-specific RPC URLs (e.g. MAINNET_RPC_URL, BASE_RPC_URL) are
        # validated per-chain in __init__ via the RPC_NAME config key — they
        # are intentionally NOT listed here so that only the chains actually
        # started require their RPC URL to be set.
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
        rpc_var = self._chain["RPC_NAME"]
        self.RPC_URL = os.environ.get(rpc_var)
        if not self.RPC_URL:
            raise ValueError(f"Missing RPC URL for {self._chain['name']}. Env var {rpc_var} not set")

        self.w3 = setup_w3(self.RPC_URL)
        self.mainnet_w3 = setup_w3(os.environ.get("MAINNET_RPC_URL", self.RPC_URL))

        # Cadence configuration: two step-function bucket tables (correlated vs
        # uncorrelated) plus the per-chain whitelist of correlated (collateral,
        # debt) pairs. Addresses are normalised to checksum form so membership
        # checks are case-insensitive.
        self.cadence_correlated: Dict[str, int] = dict(global_config["cadence_correlated"])
        self.cadence_uncorrelated: Dict[str, int] = dict(global_config["cadence_uncorrelated"])
        self.correlated_pairs: FrozenSet[Tuple[str, str]] = _parse_correlated_pairs(
            chain_config.get("correlated_pairs") or []
        )

        # Set chain-specific paths.
        # Note: LOGS_PATH in global config is the log directory (e.g. "logs");
        # the chain-specific log file path below is currently unused — logging is
        # configured via the LOGS_PATH environment variable in logging_config.py.
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

        # Swap-provider selection + liquidator-address overrides (DEV-579 e2e).
        # Defaults preserve production behavior. On an anvil fork the harness sets
        # SWAP_PROVIDER=mock and points EULER/AAVE_LIQUIDATOR_OVERRIDE at freshly
        # deployed liquidators whose on-chain `router` is the mock swapper. These are
        # set as instance attributes so they shadow the __getattr__ contracts lookup,
        # which means the swap recipient used in the liquidation path is overridden too.
        self.SWAP_PROVIDER = os.environ.get("SWAP_PROVIDER", "1inch")
        self.EULER_LIQUIDATOR_ADDRESS = Web3.to_checksum_address(
            os.environ.get("EULER_LIQUIDATOR_OVERRIDE") or self.EULER_LIQUIDATOR_ADDRESS
        )
        self.AAVE_LIQUIDATOR_ADDRESS = Web3.to_checksum_address(
            os.environ.get("AAVE_LIQUIDATOR_OVERRIDE") or self.AAVE_LIQUIDATOR_ADDRESS
        )
        # Optional deployment-block override (e2e: narrows the FactoryListener startup
        # scan to the fork's post-base blocks so only the seeded CV is discovered).
        _dep_override = os.environ.get("CVAULT_FACTORY_DEPLOYMENT_BLOCK_OVERRIDE")
        if _dep_override:
            self.CVAULT_FACTORY_DEPLOYMENT_BLOCK = int(_dep_override)

        with open(self._global["LIQUIDATOR_CONTRACT_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        euler_abi = interface["abi"]

        self.euler_liqbot = self.w3.eth.contract(address=self.EULER_LIQUIDATOR_ADDRESS, abi=euler_abi)

        with open(self._global["AAVE_LIQUIDATOR_ABI_PATH"], "r", encoding="utf-8") as file:
            interface = json.load(file)
        aave_abi = interface["abi"]

        self.aave_liqbot = self.w3.eth.contract(address=self.AAVE_LIQUIDATOR_ADDRESS, abi=aave_abi)

    def __getattr__(self, name: str) -> Any:
        """Look up config values in chain-specific, then contracts, then global config."""
        # Guard against recursion during pickling / copy / deepcopy when
        # __init__ hasn't yet assigned _chain / _global.
        if name.startswith("_"):
            raise AttributeError(name)
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


def _parse_correlated_pairs(raw_pairs) -> FrozenSet[Tuple[str, str]]:
    """
    Convert a list of {collateral, debt} mappings into a frozenset of
    (checksum_collateral, checksum_debt) tuples for fast membership checks.
    """
    pairs = set()
    for entry in raw_pairs:
        collateral = Web3.to_checksum_address(entry["collateral"])
        debt = Web3.to_checksum_address(entry["debt"])
        pairs.add((collateral, debt))
    return frozenset(pairs)


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
