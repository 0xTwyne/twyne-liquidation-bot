"""
Tests for the get ids.
"""


def test_vault(dummy_vault, config):
    """
    Test the vault initialization.
    """
    assert dummy_vault
    assert config
