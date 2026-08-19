# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Twyne Liquidation Bot — monitors lending positions on the Twyne platform and executes liquidations when positions become unhealthy. Supports both internal (Twyne) and external (Euler/Aave V3) liquidations across Ethereum Mainnet (chain 1) and Base (chain 8453). Forked from Euler's liquidation bot v2.

## Build & Development Commands

```bash
# Setup
cp .env.example .env   # dev convenience — use throwaway keys, NEVER prod secrets
foundryup
uv sync
forge install && forge build

# Run locally (Flask app) — for prod-shaped secrets, use `make dev` which wraps
# everything in `op run` so plaintext secrets never land on disk.
uv run flask run --port 8080

# Tests
make tests                          # Runs pytest + forge test (FOUNDRY_PROFILE=mainnet)
uv run pytest test                  # Python tests only (e2e fork tests are deselected by default)
uv run pytest test/test_config_loader.py  # Single test file
FOUNDRY_PROFILE=mainnet forge test  # Solidity tests only

# Layer-3 e2e fork tests (DEV-579): drive the real bot end-to-end against a local
# anvil mainnet fork seeded with liquidatable CVs (all 6 variants). Marked `e2e` and
# DESELECTED by default. Need anvil + forge on PATH and an archive RPC. The full
# suite's cumulative load rate-limits public free-tier RPCs, so use an AUTHENTICATED
# archive endpoint via E2E_FORK_RPC_URL (each test passes on the public default
# individually). NOTE: anvil/forge are not installed in the base devcontainer — install
# Foundry first (`curl -L https://foundry.paradigm.xyz | bash && foundryup`).
forge build && E2E_FORK_RPC_URL=<archive-rpc> uv run pytest test/e2e -m e2e

# Lint & Format
make lint                           # Ruff check
make fmt                            # Ruff format + fix
make all                            # fmt + lint + tests

# Deploy contracts
forge script contracts/DeployLiquidator.s.sol --rpc-url $RPC_URL --broadcast -vv

# Docker — `make run-docker` wraps `docker-compose up` in `op run` so the
# signing key never lands on disk as a long-lived gitignored .env file.
# Requires `op` CLI signed in, OR OP_SERVICE_ACCOUNT_TOKEN env var for prod hosts.
make run-docker
```

## Architecture

### Execution Flow

1. `application.py` → `app/__init__.py` (`create_app`) starts Flask, resolves the chain list via `_parse_chain_ids()` (env-driven), and spawns `ChainManager` in a background thread
2. `ChainManager` (`bot_manager.py`) initializes per-chain: `ChainConfig`, `AccountMonitor`, `FactoryListener`
3. `FactoryListener` (`event_listener.py`) scans historical `T_CollateralVaultCreated` events from the `CollateralVaultFactory` (deployment block → head), then keeps scanning new blocks to discover newly created collateral vaults
4. `AccountMonitor` processes a priority queue of vaults sorted by `time_of_next_update` (based on health score and position size), using a worker thread pool (`MONITOR_WORKER_COUNT`, default 32)
5. When a vault is liquidatable: simulate liquidation profitability via 1inch swap quote → if net-USD-profitable, execute via the `TwyneLiquidator` (Euler) or `TwyneAaveLiquidator` (Aave V3) contract

### Key Classes

- **`AccountMonitor`** (`account_monitor.py`) — main monitoring engine: priority queue, worker thread pool, scheduling cadence, state persistence, periodic reporting
- **`FactoryListener`** (`event_listener.py`) — watches `T_CollateralVaultCreated` on the `CollateralVaultFactory` to discover new collateral vaults
- **`BaseCollateralVault`** / **`BaseLiquidator`** (ABCs, `vaults/base_vault.py`) — shared vault state, health checks, and the liquidation simulate/execute interface; `SignerNonceManager` (same file) coordinates per-EOA nonce/tx submission
- **`EulerCollateralVault`** / **`EulerLiquidator`** (`vaults/euler_vault.py`) and **`AaveCollateralVault`** / **`AaveLiquidator`** (`vaults/aave_vault.py`) — protocol-specific vault + liquidation logic
- **Protocol detection** — `detect_protocol()` / `get_vault_class_for_protocol()` (`vaults/registry.py`) select the right vault class per collateral vault

### Smart Contracts (`contracts/`)

- **TwyneLiquidator.sol** — Euler vault liquidations using Morpho Blue flashloans
- **TwyneAaveLiquidator.sol** — Aave V3 collateral vault liquidations
- **DeployLiquidator.s.sol / DeployAaveLiquidator.s.sol** — deployment scripts

### Supporting Modules

- `config_loader.py` — `ChainConfig` class, loads `config.yaml` per chain, manages `Web3Singleton` instances. Test/dev env overrides: `SWAP_PROVIDER`, `EULER_LIQUIDATOR_OVERRIDE`, `AAVE_LIQUIDATOR_OVERRIDE`, `CVAULT_FACTORY_DEPLOYMENT_BLOCK_OVERRIDE` (DEV-579 e2e)
- `swap_provider.py` — `SwapProvider` seam (DEV-579): `make_swap_provider(config)` returns `OneInchSwapper` (prod) or `MockSwapProvider` (`SWAP_PROVIDER=mock`, e2e fork) so the swap leg can be satisfied locally. `build_swap()` returns `SwapData(calldata, min_return)`
- `swap_1inch.py` — `OneInchSwapper` with binary search for exact-out swaps (1inch only supports exact-in); `build_swap()` implements the `SwapProvider` seam
- `profitability.py` — USD-denominated net-profit math shared by both protocols (prices both legs via the oracle router)
- `notifications.py` — Apprise-based notifications (Slack, Ntfy, 200+ channels)
- `routes.py` — `GET /liquidation/allPositions?chainId=` returns monitored positions

### Configuration

- `app/config.yaml` — per-chain contract addresses, health score thresholds, update intervals, ABI paths
- `.env.template` — 1Password references (committed). Resolved at runtime by `make run-docker`/`make dev` via `op run`. NO plaintext `.env` is written to disk by the canonical entrypoints. `.env.example` is for dev convenience with throwaway keys only.
- `foundry.toml` — Solidity profiles: `default` (contracts/ src), `base`, `mainnet` (optimizer 20k runs, Cancun EVM)

### State & Persistence

- `state/` — JSON files with tracked vault data per chain (survives restarts)
- `logs/` — timestamped log files
- Both directories are Docker volume-mounted

## Code Style

- Python: Ruff formatter/linter, line length 120, target py313, `E/W/F/I` rules enabled, `E501` ignored
- Solidity: 0.8.26-0.8.28, optimizer enabled, Cancun EVM
- Dependencies managed via uv (`pyproject.toml`)

## Twyne Protocol Context

Twyne is a credit delegation protocol. Tilde (~) denotes liquidation LTV.
Never calculate protocol values manually — use the calculator.
Protocol reference: `[twyne-root]/repos/internal-twyne-docs/CLAUDE.md`
Calculator: `[twyne-root]/repos/internal-twyne-docs/FAQ-Knowledge-Base/tools/twyne_calculator.py`
