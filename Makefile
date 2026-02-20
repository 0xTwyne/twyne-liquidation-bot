tests:
	uv run pytest test
	FOUNDRY_PROFILE=mainnet forge test

fmt:
	uv run ruff format test app
	uv run ruff check test app --fix --unsafe-fixes

lint:
	uv run ruff check test app


release:
	$(eval current_version := $(shell uv run tbump current-version))
	@echo "Current version is $(current_version)"
	$(eval new_version := $(shell python -c "import semver; print(semver.bump_patch('$(current_version)'))"))
	@echo "New version is $(new_version)"
	uv run tbump $(new_version)

all: fmt lint tests


run-docker:
	@if [ ! -d "logs" ]; then \
		mkdir -p logs; \
		sudo chown -R 1000:1000 logs; \
	fi
	@if [ ! -d "state" ]; then \
		mkdir -p state; \
		sudo chown -R 1000:1000 state; \
	fi
	docker-compose up --build


