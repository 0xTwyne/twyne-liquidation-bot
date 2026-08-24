"""
Test the config_loader module.
"""

import os

import yaml
from web3 import Web3

from app.liquidation.config_loader import _parse_correlated_pairs


def test_config_loaded_ok(config):
    """
    Test the load_chain_config function.
    """
    assert config


def test_config_loader_validates(config):
    """
    Test the load_chain_config function.
    """
    config.validate()


REQUIRED_CONTRACT_ADDRESSES = [
    "CVAULT_FACTORY",
    "HEALTHSTATVIEWER_ADDRESS",
    "EULER_LIQUIDATOR_ADDRESS",
    "AAVE_LIQUIDATOR_ADDRESS",
    "EVC",
    "SWAPPER",
    "SWAP_VERIFIER",
    "AAVE_POOL",
    "ONEINCHROUTER",
    "PYTH",
]


def test_all_chains_have_required_addresses():
    """
    Validate that every chain in config.yaml has all required contract addresses.
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "app", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    errors = []
    for chain_id, chain_config in config["chains"].items():
        chain_name = chain_config.get("name", chain_id)
        contracts = chain_config.get("contracts", {})
        for addr_key in REQUIRED_CONTRACT_ADDRESSES:
            if addr_key not in contracts or not contracts[addr_key]:
                errors.append(f"Chain {chain_name} (id={chain_id}): missing '{addr_key}'")

    assert not errors, "Missing contract addresses in config.yaml:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Correlated-pair whitelist parsing
# ---------------------------------------------------------------------------


def test_correlated_pairs_is_frozenset_of_checksum_tuples(config):
    """ChainConfig.correlated_pairs is a frozenset of (checksum, checksum) tuples."""
    assert isinstance(config.correlated_pairs, frozenset)
    for entry in config.correlated_pairs:
        assert isinstance(entry, tuple) and len(entry) == 2
        collateral, debt = entry
        # Both sides must already be in canonical checksum form.
        assert collateral == Web3.to_checksum_address(collateral)
        assert debt == Web3.to_checksum_address(debt)


def test_parse_correlated_pairs_normalises_case():
    """Lowercase config entries are folded to the same checksum tuple."""
    raw = [
        {
            "collateral": "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",  # lowercase wstETH
            "debt": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # lowercase WETH
        }
    ]
    parsed = _parse_correlated_pairs(raw)
    expected = (
        Web3.to_checksum_address("0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"),
        Web3.to_checksum_address("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"),
    )
    assert parsed == frozenset({expected})


def test_parse_correlated_pairs_empty_input():
    assert _parse_correlated_pairs([]) == frozenset()


def test_mainnet_has_four_correlated_pairs(config):
    """Mainnet whitelist must contain the four pairs documented in the plan."""
    expected = {
        # wstETH / WETH
        (
            Web3.to_checksum_address("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
            Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        ),
        # PT-srUSDe-2APR2026 / USDe
        (
            Web3.to_checksum_address("0x9bf45ab47747f4b4dd09b3c2c73953484b4eb375"),
            Web3.to_checksum_address("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"),
        ),
        # PT-srUSDe-25JUN2026 / USDe
        (
            Web3.to_checksum_address("0x619d75e3b790ebc21c289f2805bb7177a7d732e2"),
            Web3.to_checksum_address("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"),
        ),
        # PT-srUSDe-22OCT2026 / USDe
        (
            Web3.to_checksum_address("0x59bC9FaE5D62B19d4f8d07D758047aCb9EE19d34"),
            Web3.to_checksum_address("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"),
        ),
    }
    assert config.correlated_pairs == frozenset(expected)


# ---------------------------------------------------------------------------
# Cadence dict loading
# ---------------------------------------------------------------------------


def test_cadence_correlated_keys(config):
    expected_keys = {"LIQ", "HIGH", "SAFE"}
    assert set(config.cadence_correlated.keys()) == expected_keys
    for v in config.cadence_correlated.values():
        assert isinstance(v, int) and v > 0


def test_cadence_uncorrelated_keys(config):
    expected_keys = {"LIQ", "HIGH_1", "HIGH_2", "MEDIUM", "SAFE"}
    assert set(config.cadence_uncorrelated.keys()) == expected_keys
    for v in config.cadence_uncorrelated.values():
        assert isinstance(v, int) and v > 0


def test_cadence_correlated_is_monotonic_non_decreasing(config):
    """Less-risky buckets must have intervals >= riskier buckets."""
    c = config.cadence_correlated
    assert c["LIQ"] <= c["HIGH"] <= c["SAFE"]


def test_cadence_uncorrelated_is_monotonic_non_decreasing(config):
    c = config.cadence_uncorrelated
    assert c["LIQ"] <= c["HIGH_1"] <= c["HIGH_2"] <= c["MEDIUM"] <= c["SAFE"]


def test_threshold_constants_present(config):
    """HS_* thresholds used by the bucket functions must all resolve."""
    assert config.HS_LIQUIDATION == 1.0
    assert config.HS_CORRELATED_HIGH == 1.02
    assert config.HS_HIGH_RISK == 1.05
    assert config.HS_MEDIUM_RISK == 1.10
    assert config.HS_SAFE == 1.25
