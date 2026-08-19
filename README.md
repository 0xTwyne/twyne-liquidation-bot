# Twyne Liquidation Bot

Bot to perform liquidations on the Twyne platform. The [docs explain](https://twyne.gitbook.io/twyne/tech-overview/liquidation-logic) the two paths to liquidation in Twyne:
1. Internal Twyne liquidation, using Twyne-specific logic
2. External protocol liquidation, using the logic of the external lending market

This liquidation bot can trigger both

Note: This repository is forked from Euler's [liquidation bot main-base branch](https://github.com/euler-xyz/liquidation-bot-v2/commits/main-base).

## Testing bot locally

Follow the steps below in the "Running locally" section. The overall flow of how the bot works can be seen in the start() function of bot_manager.py. The basic flow for testing in prod is as follows:

1. Create a borrow position near or at the borrow limit.
2. Make the borrow position liquidatable. This is most easily done by lowering the LTV on the intermediate vault
3. Test that the liquidation bot detects the liquidatable position and liquidates accordingly
4. Verify that the liquidation happened as expected. Can just use cast for this, the frontend is not needed
5. The withdrawal process and 1inch swap is a separate process, handling in the liquidation smart contract logic

When the bot runs for the first time, it will search all blocks for collateral vault creation events (identified by T_CollateralVaultCreated) starting from the CVAULT_FACTORY_DEPLOYMENT_BLOCK value specified in config.yaml. If you are NOT running the bot for the first time, you might want to remove all files from the logs/ and state/ directories. If the logs/ or state/ directories contain cached data for collateral vaults involving a different Twyne deployment, many errors can appear due to changes in the code between deployments.

If the flask app is running as expected, the first step is to cache all data from the CVAULT_FACTORY_DEPLOYMENT_BLOCK block to find all the collateral vaults. After this stage of the bot is complete, the collateral vault addresses are stored in the state/ directory and periodic monitoring begins. The interval of this monitoring is set by the SAVE_INTERVAL value in config.yaml. The main monitoring logic is in start_queue_monitoring() within twyne_liquidation_bot.py. The queue checks vaults with a stored time_of_next_update value that indicates it should be checked. This value is set in get_time_of_next_update(), which chooses the time of the next vault check based on position size and health score.

## Running in production

Make sure the EOA that is used has sufficient gas for transactions.

### Secrets via 1Password (recommended)

The bot's secrets — `LIQUIDATOR_PRIVATE_KEY`, RPC URLs, `ONEINCH_API_KEY`, etc. — live in a 1Password vault rather than a plaintext `.env`. The committed `.env.template` contains only `op://` references; the actual values are resolved at runtime by the [1Password CLI](https://1password.com/downloads/command-line) (`op`).

**One-time setup** (per environment / per operator):

1. Install `op`, sign in: `op signin`
2. Verify every key in the template resolves against the vault: `make verify-vault`
   - The default vault is `liquidation-bot`. Override per-environment: `OP_VAULT=liquidation-bot-staging make verify-vault`

**Daily use:**

| Goal | Command |
|---|---|
| Run the bot in Docker (production) | `make run-docker` — auto-runs `make env-resolve` first to materialize a fresh `.env` from the template. |
| Run flask locally without Docker | `make dev` — uses `op run` to inject secrets directly into the wrapped process; never writes a plaintext `.env` to disk. |
| Manually regenerate `.env` from template | `make env-resolve` |
| Use a non-default vault | `OP_VAULT=<name> make <target>` |

The `.env` file remains gitignored. After `make env-resolve` it contains the resolved plaintext values — treat it as ephemeral runtime state; rotate any secret that appears in it if the file or its host is compromised.

### Deploying on a long-running VM with systemd

For a VM that should auto-start the bot on boot and survive crashes / reboots, run it under systemd. The bot pulls fresh secrets from 1Password on every restart, so rotation is just "update the value in 1P and `systemctl restart`."

> **How the unit gets `OP_SERVICE_ACCOUNT_TOKEN` — two supported options.**
> - **(A) Root-owned `EnvironmentFile`** (steps 2–4 below): the token sits, root-only, in
>   `/etc/liquidation-bot/op-token.env`.
> - **(B) Fetch from a secrets manager at boot.** The
>   unit's `ExecStart` is a wrapper (`/usr/local/bin/start-liquidation-bot`) that pulls the token
>   from **AWS Secrets Manager** via the instance-profile IAM role, exports it, and execs
>   `make run-docker`; nothing lands on disk. With (B) there is **no** `EnvironmentFile=` and
>   **no** `/etc/liquidation-bot/op-token.env` — see "Alternative: fetch the token from AWS
>   Secrets Manager" after the unit below.

#### 1. Install the 1Password CLI on the VM

There's no `op` package on Amazon Linux 2023 by default. The simplest install is the pinned binary from the official mirror:

```bash
OP_VERSION=2.30.3
curl -sSfL -o /tmp/op.zip "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_amd64_v${OP_VERSION}.zip"
unzip -d /tmp/op /tmp/op.zip
sudo install -m 0755 /tmp/op/op /usr/local/bin/op
rm -rf /tmp/op /tmp/op.zip
op --version   # should print 2.30.3
```

#### 2. Create a vault-scoped Service Account in 1Password

In the 1Password web UI: **Developer → Service Accounts → Create**. Give it **read-only** access to the `liquidation-bot` vault only. Copy the `ops_...` token (shown once).

#### 3. Drop the token into a root-owned env file on the VM

```bash
sudo install -d -m 0700 -o root -g root /etc/liquidation-bot
sudoedit /etc/liquidation-bot/op-token.env
# Add a single line, no spaces around =:
#   OP_SERVICE_ACCOUNT_TOKEN=ops_...
sudo chmod 0600 /etc/liquidation-bot/op-token.env
```

Verify (without leaking the token):

```bash
sudo wc -c /etc/liquidation-bot/op-token.env       # should be ~880 bytes
sudo head -c 25 /etc/liquidation-bot/op-token.env  # → "OP_SERVICE_ACCOUNT_TOKEN="
```

Smoke test that the token + op + vault scope all work, by running `op vault list` in a systemd context that mirrors what the service will do:

```bash
sudo systemd-run --collect --wait \
  --uid=$USER --gid=docker \
  -p EnvironmentFile=/etc/liquidation-bot/op-token.env \
  --unit=op-vault-test \
  /usr/local/bin/op vault list
sudo journalctl -u op-vault-test --no-pager -n 5
# expect to see "liquidation-bot" in the listed vaults
```

#### 4. Write the systemd unit

`sudoedit /etc/systemd/system/liquidation-bot.service`:

```ini
[Unit]
Description=Twyne Liquidation Bot
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ec2-user/liquidation-bot
EnvironmentFile=/etc/liquidation-bot/op-token.env
ExecStart=/usr/bin/make run-docker
ExecStop=/usr/local/bin/docker-compose down
Restart=on-failure
RestartSec=30s

User=ec2-user
Group=docker

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Three things to double-check before enabling:

| Directive | Check |
|---|---|
| `WorkingDirectory=` | Match the actual path you cloned into. `/home/ec2-user/liquidation-bot` is typical for EC2; `/opt/liquidation-bot` is the convention for site-installed apps. |
| `User=` / `Group=` | The user must own the working directory and be in the `docker` group: `id <user>` should list `docker`. |
| `ExecStop=` | Path must match your docker-compose binary. `/usr/local/bin/docker-compose` is the standalone install (legacy syntax). If your VM has the modern Docker plugin instead, use `ExecStop=/usr/bin/docker compose down`. Run `which docker-compose` and `docker compose version` to find out. |

Three things deliberately *not* in this unit:

- **`ProtectHome=`** — left at the default. `ProtectHome=true` would mount an empty stub over `/home`, so any `WorkingDirectory` under `/home/<user>/...` fails CHDIR with "Permission denied". Only set this if your repo lives outside `/home`.
- **`ProtectSystem=strict`** — would make most of the filesystem read-only, which can interfere with Docker's image / build layer paths. Skipped for a smaller blast radius of unexpected failure modes; re-add later once the basic flow is verified.
- **`ReadWritePaths=`** — only meaningful in combination with `ProtectSystem=strict`. Omit when that's omitted.

#### Alternative: fetch the token from AWS Secrets Manager

If your VM runs on EC2, you can skip `EnvironmentFile=/etc/liquidation-bot/op-token.env` and
run a wrapper at `ExecStart` instead, so the token is fetched at exec time and never lands on
disk — replace the `EnvironmentFile=` + `ExecStart=` lines from step 4 with a single line:

```ini
# [Service]  (no EnvironmentFile= line)
ExecStart=/usr/local/bin/start-liquidation-bot
```

`/usr/local/bin/start-liquidation-bot` (root-owned, `0755`):

```bash
#!/usr/bin/env bash
set -euo pipefail
OP_SERVICE_ACCOUNT_TOKEN=$(aws --region <your-region> secretsmanager get-secret-value \
  --secret-id <your-secret-id> \
  --query SecretString --output text)
export OP_SERVICE_ACCOUNT_TOKEN
cd /path/to/liquidation-bot
exec /usr/bin/make run-docker
```

Requirements: the EC2 instance-profile role needs `secretsmanager:GetSecretValue` on that one
secret ARN, and the secret holds the `ops_…` service-account token. Skip steps 2–3's
`op-token.env` entirely. Rotation is "update the value in Secrets Manager and `systemctl restart`."

#### 5. Activate

```bash
sudo systemctl daemon-reload
sudo systemctl start liquidation-bot
sudo journalctl -u liquidation-bot -f   # watch first run

# expect, in order:
#   Resolving 1Password references against vault 'liquidation-bot'...
#   Wrote .env (gitignored).
#   docker-compose up --build
#   liquidation-bot-1  | 2026-... - INFO - Initializing chains: [1]
#   liquidation-bot-1  |  * Running on http://0.0.0.0:8080

# verify it's healthy:
sudo systemctl is-active liquidation-bot   # → active
docker ps --filter name=liquidation-bot
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8080/liquidation/allPositions?chainId=1

# enable auto-start on boot (only after start succeeds):
sudo systemctl enable liquidation-bot
```

#### Test crash and reboot resilience

Pick a quiet window — these will briefly interrupt monitoring.

```bash
# A — bot crash auto-restarts (RestartSec=30s)
sudo docker kill liquidation-bot-liquidation-bot-1
sleep 35
sudo systemctl is-active liquidation-bot   # → active

# B — VM reboot survives
sudo reboot
# ssh back after the VM is up:
sudo systemctl is-active liquidation-bot   # → active
```

#### Common first-run failures

| Log message | Cause | Fix |
|---|---|---|
| `Failed at step CHDIR ... Permission denied` | `WorkingDirectory` under `/home/...` while `ProtectHome=true` is set | Remove `ProtectHome=true` (this README's unit already does) |
| `op: command not found` | `op` not installed on the VM | Install per step 1 above |
| `cannot prompt for confirmation` (from `op inject`) | `.env` already exists; without `--force`, op prompts and there's no TTY | The Makefile's `env-resolve` target uses `op inject --force` — pull the latest if you don't have it |
| `docker: 'compose' is not a docker command` | Unit references `docker compose` (plugin) but VM only has standalone `docker-compose` | Set `ExecStop=/usr/local/bin/docker-compose down` (or vice versa for plugin-only VMs) |
| `[ERROR] couldn't find vault 'liquidation-bot'` | Service Account token isn't scoped to that vault, or `OP_VAULT` differs | Check 1P UI → Service Accounts → vault scope; or set `Environment=OP_VAULT=<actual-name>` in the unit `[Service]` section |
| Service is `active` but `docker ps` is empty for >2 min on first start | First-time image build is slow (uv sync + pip dependencies) | Wait. Use `sudo journalctl -u liquidation-bot -f` to watch build progress |

#### Day-to-day commands

```bash
sudo systemctl status liquidation-bot       # current state
sudo systemctl restart liquidation-bot      # apply a config / .env regenerate
sudo systemctl stop liquidation-bot         # take it down
sudo journalctl -u liquidation-bot -f       # live tail
sudo journalctl -u liquidation-bot --since=today
```

To rotate a secret: update its value in 1Password, `sudo systemctl restart liquidation-bot`. The new value is fetched fresh on next start; no SSH'ing into the VM to edit files.

## How it works

1. **Account Monitoring**:
   - The primary way of finding new accounts is [scanning](app/liquidation/liquidation_bot.py#L950) for `AccountStatusCheck` events emitted by the EVC contract to check for new & modified positions.
   - This event is emitted every time a borrow is created or modified, and contains both the account address and vault address.
   - Health scores are calculated using the `accountLiquidity` [function](app/liqiudation/liquidation_bot.py#L101) implemented by the vaults themselves.
   - Accounts are added to a priority queue based on their health score with a time of next update, with low health accounts being checked most frequently.
   - EVC logs are batched on bot startup to catch up to the current block, then scanned for new events at a regular interval.

2. **Liquidation Opportunity Detection**:
   - When an account's health score falls below 1, the bot simulates a liquidation transaction across each collateral asset.
   - The bot gets a quote for how much collateral is needed to swap into the debt repay amount, and simulates a liquidation transaction on the Liquidator.sol contract.
   - Gas cost is estimated for the liquidation transaction, then checks if the leftover collateral after repaying debt is greater than the gas cost when converted to ETH terms.
   - If this is the case, the liquidation is profitable and the bot will attempt to execute the transaction.

3. **Liquidation Execution - [Liquidator.sol](contracts/Liquidator.sol)**:
   - If profitable, the bot constructs a transaction to call the `liquidateSingleCollateral` [function](contracts/Liquidator.sol#L70) on the Liquidator contract.
   - The Liquidator contract then executes a batch of actions via the EVC containing the following steps:
     1. Enables borrow vault as a controller.
     2. Enable collateral vault as a collateral.
     3. Call liquidate() on the violator's position in the borrow vault, which seizes both the collateral and debt position.
     4. Withdraws specified amount of collateral from the collateral vault to the swapper contract.
     5. Calls the swapper contract with a multicall batch to swap the seized collateral, repay the debt, and sweep any remaining dust from the swapper contract.
     6. Transfers remaining collateral to the profit receiver.
     7. Submit batch to EVC.


    - There is a secondary flow still being developed to use the liquidator contract as an EVC operator, which would allow the bot to operate on behalf of another account and pull the debt position alongside the collateral to the account directly. This flow will be particularly useful for liquidating positions without swapping the collateral to the debt asset, for things such as permissioned RWA liquidations.

4. **Swap Quotation**:
   - The bot currently uses 1inch API to get quotes for swapping seized collateral to repay debt.
   - 1inch unfortunatley does not support exact output swaps, so we perform a binary search to find the optimal swap amount resulting in swapping slightly more collateral than needed to repay debt.
   - The bot will eventually have a fallback to uniswap swapping if 1inch is unable to provide a quote, which would also allow for more precise exact output swaps.

5. **Profit Handling**:
   - Any profit (excess collateral after repayment) is sent to a designated receiver address.
   - Profit is sent in the form of ETokens of the collateral asset, and is not withdrawn from the vault or converted to any other asset.

6. **Slack Notifications**:
   - The bot can send notifications to a slack channel when unhealthy accounts are detected, when liquidations are performed, and when errors occur.
   - The bot also sends a report of all low health accounts at regularly scheduled intervals, which can be configured in the config.yaml file.
   - In order to receive notifications, a slack channel must be set up and a webhook URL must be provided in the .env file.

### Improvement Notes

There are quite a few optimizations/improvements that likely could be made with more time, for instance:
   - Storing enabled collateral/controller within the liquidator contract itself to avoid calls to EVC to check & enable already enabled collaterals
   - Reducing the number of calls made to the RPC with smarter caching
   - Smarter gas price & slippage profitability checks
   - Potentially skipping interaction with Liquidator contract entirely and constructing batch off chain
   - More precise swap calculations in tandem with Uniswap swaps to avoid overswapping
   - Additional safety checks on amounts in Liquidator contract
   - Deconstruction of Pull oracle batches to avoid unnecessary updates on oracles that aren't being used
   - Secure routing via flashbots/bundling/etc

## How to run the bot

### Installation

The bot can be run either via building a docker container or manually. In both instances, it runs via a flask app to expose some endpoints for account health dashboards & metrics.

Before running either, setup a .env file by copying the .env.example file and updating with the relevant contract addresses, an EOA private key, & API keys. Then, check config.yaml to make sure parameters, contracts, and ABI paths have been set up correctly.

#### Running locally

To run locally, we need to install some dependencies and build the contracts. This will setup a python virtual environment for installing dependencies. The below command assumes we have foundry installed, which can installed from the [Foundry Book](https://book.getfoundry.sh/).

Setup:

```bash
cp .env.example .env       # dev convenience only — throwaway keys, NEVER prod secrets
foundryup
uv sync
forge install && forge build
# cd lib/evk-periphery && forge build && cd ../..
mkdir logs
```

For the canonical prod-shaped local run that resolves secrets from 1Password (no `.env` on disk), use `make dev` instead of the steps above.

**Run**:
```bash
uv run flask run --port 8080
```
Change the Port number to whatever port is desired for exposing the relevant endpoints from the [routes.py](app/liquidation/routes.py) file.

#### Docker

Canonical entry point:

```bash
make run-docker
```

`run-docker` wraps `docker-compose up` in `op run` so all secrets (signing key, RPC URLs, API keys) are resolved from 1Password into the parent process environment and passed through to the container. No plaintext `.env` file is ever written to host disk by this path. Requirements:

- `op` CLI installed and signed in, OR `OP_SERVICE_ACCOUNT_TOKEN` env var set in the calling shell (recommended for unattended prod hosts so the bot can restart unattended after a host reboot).
- All keys in `.env.template` resolve against the configured `OP_VAULT` (defaults to `liquidation-bot`; override per-environment via `OP_VAULT=liquidation-bot-staging make run-docker`). Verify with `make verify-vault` before deploying.

For one-off operator scripts that need the signing key (`liquidationSetup.sh`, `sweep-liq-funds.sh`), wrap the same way:

```bash
op run --env-file=.env.template -- bash sweep-liq-funds.sh
```

NEVER copy `.env.example` to `.env` and run the bot with prod keys — `.env.example` is a dev-convenience template intended for throwaway keys against a fork RPC.

### Configuration

- The bot uses variables from both the [config.yaml](config.yaml) file and the [.env](.env.example) file to configure settings and private keys.
- The startup code is contained at the end of the [python/liquidation_bot.py](python/liquidation_bot.py#L1311) file, which also has two variable to set for the bot - `notify` & `execute_liquidation`, which determine if the bot will post to slack and if it will execute the liquidations found.

Make sure to build the contracts in both src and lib to have the correct ABIs loaded from the evk-periphery installation

Configuration through `.env` file:

REQUIRED:

- `LIQUIDATOR_EOA, LIQUIDATOR_PRIVATE_KEY` - public/private key of EOA that will be used to liquidate
- `RPC_URL` - RPC provider endpoint (Infura, Rivet, Alchemy etc.)
- `API_KEY_1INCH` - API key for 1inch to help with executing swaps

OPTIONAL:
- `SLACK_WEBHOOK_URL` - Optional URL to post notifications to slack
- `RISK_DASHBOARD_URL` - Optional, can include a link in slack notifications to manually liquidate a position
- `DEPOSITOR_ADDRESS, DEPOSITOR_PRIVATE_KEY, BORROWER_ADDRESS, BORROWER_PRIVATE_KEY` - Optional, for running


Configuration in `config.yaml` file:

- `LOGS_PATH, SAVE_STATE_PATH` - Path directing to save location for Logs & Save State
- `SAVE_INTERVAL` - How often state should be saved
- `HS_LOWER_BOUND, HS_UPPER_BOUND` - Bounds below and above which an account should be updated at the min/max update interval
- `MIN_UPDATE_INTERVAL, MAX_UPDATE_INTERVAL` - Min/Max time between account updates
- `LOW_HEALTH_REPORT_INTERVAL` - Interval between low health reports
- `SLACK_REPORT_HEALTH_SCORE` - Threshold to include an account on the low health report
- `BATCH_SIZE, BATCH_INTERVAL` - Configuration batching logs on bot startup
- `SCAN_INTERVAL` - How often to scan for new events during regular operation
- `NUM_RETRIES, RETRY_DELAY` - Config for how often to retry failing API requests
- `SWAP_DELTA, MAX_SEARCH_ITERATIONS` - Used to define how much overswapping is accetable when searching 1Inch swaps
- `EVC_DEPLOYMENT_BLOCK` - Block that the contracs were deployed
- `WETH, EVC, SWAPPER, SWAP_VERIFIER, LIQUIDATOR_CONTRACT, ORACLE_LENS` - Relevant deployed contract addresses. The liquidator contract has been deployed on Mainnet, but feel free to redeploy.
- `PROFIT_RECEIVER` - Targeted receiver of any profits from liquidations
- `EVAULT_ABI_PATH, EVC_ABI_PATH, LIQUIDATOR_ABI_PATH, ORACLE_LENS_ABI_PATH` - Paths to compiled contracts
- `CHAIN_ID` - Chain ID to run the bot on, Mainnet: 1, Arbitrum: 42161

### Deploying

If you want to deploy your own version of the liquidator contract, you can run the command below (remove `--broadcast` to test without deploying on-chain):

```bash
forge script contracts/DeployLiquidator.s.sol --rpc-url $RPC_URL --broadcast -vv
```

## 1inch API usage notes

The 1inchtest.py file in this repo is used for getting the 1inch calldata to enable a swap. This calldata can be used in the dexData variable in LiquidatorTest.t.sol.

Before using 1inch, the liquidator address must set a token allowance. This is done in the constructor of the on-chain contract, which must be updated to support the appropriate markets. Then use the [classic swap approach](https://portal.1inch.dev/documentation/apis/swap/classic-swap/swagger?method=get&path=%2Fv6.0%2F1%2Fswap) to get the calldata. To get the contract address that must be approved with the allowance, check the 1inch docs for the contract addresses of the [on-chain swap aggregator](https://portal.1inch.dev/documentation/contracts/aggregation-protocol/aggregation-introduction) for the appropriate chain.

## Notifications
[Apprise](https://github.com/caronc/apprise?tab=readme-ov-file) is used for notifications, with more than 200 channels supported.

## Running in Docker
Docker compose is provided to enable the application to run.
```bash
make run-docker
```

## Development

### Tests
Handled using pytest.

```bash
make tests
```

### Linting
Using Ruff formatter/linter.
```bash
make lint
```

### Formatting
Using Ruff formatter/linter.
```bash
make fmt
```

### Releasing
Uses tbump for configs.
```bash
make release
```


