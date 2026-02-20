# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Twyne Liquidation Bot — monitors lending positions on the Twyne platform and executes liquidations when positions become unhealthy. Supports both internal (Twyne) and external (Euler/Aave V3) liquidations across Ethereum Mainnet (chain 1) and Base (chain 8453). Forked from Euler's liquidation bot v2.

## Build & Development Commands

```bash
# Setup
cp .env.example .env
foundryup
uv sync
forge install && forge build

# Run locally (Flask app)
uv run flask run --port 8080

# Tests
make tests                          # Runs pytest + forge test (FOUNDRY_PROFILE=mainnet)
uv run pytest test                  # Python tests only
uv run pytest test/test_config_loader.py  # Single test file
FOUNDRY_PROFILE=mainnet forge test  # Solidity tests only

# Lint & Format
make lint                           # Ruff check
make fmt                            # Ruff format + fix
make all                            # fmt + lint + tests

# Deploy contracts
forge script contracts/DeployLiquidator.s.sol --rpc-url $RPC_URL --broadcast -vv

# Docker
make run-docker                     # Or: docker compose build --progress=plain && docker compose up
```

## Architecture

### Execution Flow

1. `application.py` → `app/__init__.py` (`create_app`) starts Flask + spawns `ChainManager` in background thread
2. `ChainManager` (`bot_manager.py`) initializes per-chain: `ChainConfig`, `AccountMonitor`, `EVCListener`
3. `EVCListener` scans historical EVC logs from deployment block, then watches for new `AccountStatusCheck` events
4. `AccountMonitor` processes a priority queue of vaults sorted by `time_of_next_update` (based on health score and position size)
5. When health score < 1.0: simulate liquidation profitability via 1inch swap quote → if profitable, execute via `TwyneLiquidator` or `TwyneAaveLiquidator` contract

### Key Classes (in `app/liquidation/twyne_liquidation_bot.py`)

- **CollateralVault** — represents a single Twyne vault; handles health checks, liquidation simulation, scheduling
- **AccountMonitor** — main monitoring engine with priority queue, 32-worker thread pool, state persistence
- **EVCListener** — watches EVC contract events to discover new/modified positions
- **Liquidator** — static methods for simulation and execution of liquidation transactions

### Smart Contracts (`contracts/`)

- **TwyneLiquidator.sol** — Euler vault liquidations using Morpho Blue flashloans
- **TwyneAaveLiquidator.sol** — Aave V3 collateral vault liquidations
- **DeployLiquidator.s.sol / DeployAaveLiquidator.s.sol** — deployment scripts

### Supporting Modules

- `config_loader.py` — `ChainConfig` class, loads `config.yaml` per chain, manages `Web3Singleton` instances
- `swap_1inch.py` — `OneInchSwapper` with binary search for exact-out swaps (1inch only supports exact-in)
- `utils.py` — Apprise-based notifications (Slack, Ntfy, 200+ channels), logging helpers
- `routes.py` — `GET /liquidation/allPositions?chainId=` returns monitored positions

### Configuration

- `app/config.yaml` — per-chain contract addresses, health score thresholds, update intervals, ABI paths
- `.env` — secrets: `LIQUIDATOR_EOA`, `LIQUIDATOR_PRIVATE_KEY`, RPC URLs, `ONEINCH_API_KEY`, `NOTIFICATION_URL`
- `foundry.toml` — Solidity profiles: `default` (contracts/ src), `base`, `mainnet` (optimizer 20k runs, Cancun EVM)

### State & Persistence

- `state/` — JSON files with tracked vault data per chain (survives restarts)
- `logs/` — timestamped log files
- Both directories are Docker volume-mounted

## Code Style

- Python: Ruff formatter/linter, line length 120, target py313, `E/W/F/I` rules enabled, `E501` ignored
- Solidity: 0.8.26-0.8.28, optimizer enabled, Cancun EVM
- Dependencies managed via uv (`pyproject.toml`)
