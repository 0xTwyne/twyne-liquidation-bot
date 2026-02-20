import pytest
from dotenv import load_dotenv

from app.liquidation.config_loader import ChainConfig, load_chain_config
from app.liquidation.vaults.euler_vault import EulerCollateralVault as CollateralVault

TEST_CHAIN_ID = 1
TEST_COLLATERAL_VAULT_ADDRESS = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"


@pytest.fixture()
def config() -> ChainConfig:
    load_dotenv(dotenv_path=".env.example")
    return load_chain_config(TEST_CHAIN_ID)


@pytest.fixture()
def dummy_vault(config) -> CollateralVault:
    return CollateralVault(TEST_COLLATERAL_VAULT_ADDRESS, config)
