# 1Password vault that holds this bot's secrets. Override on the command
# line for per-environment vaults: `OP_VAULT=liquidation-bot-staging make run-docker`.
OP_VAULT ?= liquidation-bot

tests:
	uv run pytest test
	FOUNDRY_PROFILE=mainnet forge test

fmt:
	uv run ruff format test app
	uv run ruff check test app --fix --unsafe-fixes

lint:
	uv run ruff check test app

# Line coverage of the money-deciding code (DEV-556). The acceptance bar is
# >=80% on the euler/aave profit + swap functions.
coverage:
	uv run pytest test \
		--cov=app.liquidation.vaults.euler_vault \
		--cov=app.liquidation.vaults.aave_vault \
		--cov-report=term-missing


release:
	$(eval current_version := $(shell uv run tbump current-version))
	@echo "Current version is $(current_version)"
	$(eval new_version := $(shell python -c "import semver; print(semver.bump_patch('$(current_version)'))"))
	@echo "New version is $(new_version)"
	uv run tbump $(new_version)

all: fmt lint tests


# Verify every key in .env.template has a matching item in the vault.
# Useful pre-deploy sanity check.
#
# Excluded keys (plaintext config, not vault-backed):
#   - FOUNDRY_PROFILE
#   - SENTRY_ENVIRONMENT
#   - SENTRY_TRACES_SAMPLE_RATE
verify-vault:
	@command -v op >/dev/null 2>&1 || { echo "ERROR: op CLI not installed"; exit 1; }
	@echo "Verifying all keys in .env.template resolve against vault '$(OP_VAULT)'..."
	@missing=0; ok=0; \
	for key in $$(grep -oE '^[A-Z_][A-Z0-9_]*' .env.template | grep -vE '^(FOUNDRY_PROFILE|SENTRY_ENVIRONMENT|SENTRY_TRACES_SAMPLE_RATE)$$'); do \
		if op read "op://$(OP_VAULT)/$$key/password" >/dev/null 2>&1; then \
			printf "  ok      %s\n" "$$key"; ok=$$((ok + 1)); \
		else \
			printf "  MISSING %s\n" "$$key"; missing=$$((missing + 1)); \
		fi; \
	done; \
	echo "Result: $$ok ok, $$missing missing"; \
	[ $$missing -eq 0 ] || exit 1


# Run the bot under Docker. Secrets are resolved by `op run` into the
# parent process's environment and passed through to docker-compose via
# the `environment:` passthrough block in compose.yaml. The plaintext
# values never land on host disk as a long-lived gitignored .env file.
#
# Requires OP_SERVICE_ACCOUNT_TOKEN in the calling shell (prod) or an
# interactive `op signin` session (dev). See README.md "Docker" section.
#
# SENTRY_RELEASE is injected from the current git SHA so Sentry can
# correlate exceptions with deploys ("regression after abc123" alerts).
run-docker:
	@command -v op >/dev/null 2>&1 || { echo "ERROR: op CLI not installed. See https://1password.com/downloads/command-line"; exit 1; }
	@if [ -z "$$OP_SERVICE_ACCOUNT_TOKEN" ]; then op account list >/dev/null 2>&1 || { echo "ERROR: op not signed in. Run: op signin (or set OP_SERVICE_ACCOUNT_TOKEN)"; exit 1; }; fi
	@if [ ! -d "logs" ]; then \
		mkdir -p logs; \
		sudo chown -R 1000:1000 logs; \
	fi
	@if [ ! -d "state" ]; then \
		mkdir -p state; \
		sudo chown -R 1000:1000 state; \
	fi
	@grep -v '^[[:space:]]*#' .env.template | OP_VAULT=$(OP_VAULT) envsubst '$$OP_VAULT' > /tmp/.lb-env-resolved
	docker-compose build
	docker image prune -f
	SENTRY_RELEASE=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
		op run --env-file=/tmp/.lb-env-resolved -- docker-compose up; \
		rc=$$?; rm -f /tmp/.lb-env-resolved; exit $$rc


# Run flask locally without docker. Uses `op run` to inject secrets
# into the wrapped process — no plaintext .env is ever written to disk.
dev:
	@command -v op >/dev/null 2>&1 || { echo "ERROR: op CLI not installed"; exit 1; }
	@grep -v '^[[:space:]]*#' .env.template | OP_VAULT=$(OP_VAULT) envsubst '$$OP_VAULT' > /tmp/.lb-env-resolved
	SENTRY_RELEASE=$$(git rev-parse --short HEAD 2>/dev/null || echo "dev") \
	op run --env-file=/tmp/.lb-env-resolved -- uv run flask run --port 8080; \
		rc=$$?; rm -f /tmp/.lb-env-resolved; exit $$rc
