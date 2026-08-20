# Codex Notes - liquidation-bot

`CLAUDE.md` is authoritative for this repo. Read it before editing.

## Purpose

Liquidation bot for Twyne internal and external liquidations on Ethereum mainnet. It includes a Flask app, Python monitoring engine, and Foundry liquidation contracts.

## Stack And Commands

- Python package manager: `uv`
- Solidity tooling: Foundry
- Common commands:

```bash
uv sync
uv run flask run --port 8080
make tests
uv run pytest test
FOUNDRY_PROFILE=mainnet forge test
make lint
make fmt
make all
```

## Conventions

- Use `uv`; do not use `pip`/`pip3` for dependency changes.
- Use `make dev` or `make run-docker` for prod-shaped secrets so `op run` injects secrets without writing plaintext `.env` files.
- Address changes come from `repos/tech-notes/`; do not hardcode guessed addresses.
- Contract ABI/address changes may ripple from `twyne-contracts`.

## Twyne Rules

- Never calculate protocol values manually.
- Run `bash scripts/codex-ripple-check.sh repos/liquidation-bot` from `twyne-root` after edits that affect config, ABIs, liquidation logic, or address wiring.
