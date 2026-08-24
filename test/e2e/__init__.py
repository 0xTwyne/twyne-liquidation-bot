"""End-to-end fork tests for the Twyne liquidation bot (DEV-579).

Layer 3: drive the real Python bot against a local anvil mainnet fork seeded with a
liquidatable Collateral Vault, exercising discover -> schedule -> simulate -> sign ->
send -> mine and asserting an on-chain liquidation that closes the position.

These tests are marked ``e2e`` and are deselected by default (see pyproject.toml
``addopts``). They require ``anvil``/``forge`` on PATH and an archive RPC
(``E2E_FORK_RPC_URL``, default ``https://eth.drpc.org``). Run with:

    forge build && uv run pytest test/e2e -m e2e
"""
