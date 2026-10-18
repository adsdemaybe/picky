# Picky — repo commands. POSIX make (works with macOS make 3.81 and GNU make on Linux).
# Run `make` or `make help` for the list. Config comes from .env (loaded by utils.config), not from this file.

.DEFAULT_GOAL := help
SHELL := /bin/sh

FRONTEND_DIR := arms/frontend
NO_FRONTEND_MSG := "arms/frontend/package.json not found yet: the frontend app has not been scaffolded. See arms/frontend/README.md (pnpm create next-app ...)."

.PHONY: help setup check lint fmt fmt-check rules rules-check test dev demo seed db-up db-down sim gen-types web \
	secret secrets secrets-identity secrets-pull secrets-push

help: ## List all targets
	@echo "Picky make targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-17s %s\n", $$1, $$2}'

setup: ## Install the Python env + dev tools (uv sync --locked; drops extras: re-run `uv sync --extra <arm>`)
	uv sync --locked

check: lint fmt-check rules-check test ## Everything CI runs: lint + format check + rules check + tests

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Format and auto-fix with ruff
	uv run ruff format .
	uv run ruff check --fix .

fmt-check: ## Fail if any file needs formatting
	uv run ruff format --check .

rules: ## Regenerate .cursor/rules/ from CLAUDE.md + arm READMEs
	uv run python scripts/sync_agent_rules.py

rules-check: ## Fail if .cursor/rules/ is stale (run `make rules` to fix)
	uv run python scripts/sync_agent_rules.py --check

test: ## Run pytest (base deps; extras are importorskip'd)
	uv run pytest

dev: ## Run the gateway API with reload (GATEWAY_HOST/GATEWAY_PORT from .env, default 0.0.0.0:8000)
	uv run python -m arms.gateway.app --reload

demo: ## Run the demo trips through the pipeline (fakes unless PICKY_REAL_ARMS is set; `make demo ARGS=--allow-errors`)
	uv run python -m arms.gateway.demo $(ARGS)

seed: ## Seed the DB from data/ (planograms + products); needs `make db-up`
	uv run --extra catalog python -m arms.catalog.seed --schema --apply

db-up: ## Start local Postgres + pgvector (docker compose)
	docker compose up -d db

db-down: ## Stop local Postgres (data volume is kept)
	docker compose down

sim: ## Run the Isaac Sim worker (GPU host only; connects out to the gateway /ws/sim)
	@echo "Note: the Isaac sim worker runs on the Linux GPU box only (see arms/sim/README.md)."
	uv run python -m arms.sim.app

gen-types: ## Export JSON schemas from contracts and generate frontend TS types
	uv run python -m arms.gateway.export_schemas
	@if [ -f $(FRONTEND_DIR)/package.json ]; then \
		cd $(FRONTEND_DIR) && pnpm gen-types; \
	else \
		echo $(NO_FRONTEND_MSG); \
	fi

web: ## Run the frontend dev server (pnpm dev in arms/frontend)
	@if [ -f $(FRONTEND_DIR)/package.json ]; then \
		cd $(FRONTEND_DIR) && pnpm dev; \
	else \
		echo $(NO_FRONTEND_MSG); \
	fi

# --- Secrets (utils.secrets; docs/secrets.md). Values are never printed. -----------------------------------------
secret: ## Store one API key in your local vault (hidden prompt): make secret NAME=PERCEPTRON_ROBOTICS_API_KEY
	@if [ -z "$(NAME)" ]; then echo "usage: make secret NAME=<NAME>   (make secrets lists the names)"; exit 2; fi
	uv run python -m utils.secrets set $(NAME)

secrets: ## List known secrets: set?, source (env/keychain/file/.env), masked, what it is
	uv run python -m utils.secrets list $(ARGS)

secrets-identity: ## Create/show your age key for the team bundle; paste the public key into secrets/recipients.txt
	uv run --extra secrets python -m utils.secrets identity

secrets-pull: ## Decrypt secrets/team.age into your local vault (never overwrites a different value; ARGS=--force)
	uv run --extra secrets python -m utils.secrets pull $(ARGS)

secrets-push: ## Encrypt local secrets into secrets/team.age for everyone in recipients.txt (NAME=... to rotate one)
	uv run --extra secrets python -m utils.secrets push $(NAME) $(ARGS)
