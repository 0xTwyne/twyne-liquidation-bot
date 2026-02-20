"""
Test the config_loader module.
"""


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
