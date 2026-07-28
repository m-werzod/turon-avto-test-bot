# =============================================================================
# Development and operations shortcuts.
#
#   make help    list every target
# =============================================================================

.DEFAULT_GOAL := help
.PHONY: help install dev test test-cov lint format typecheck check \
        migrate migration downgrade run \
        up down restart logs shell psql build clean backup

PYTHON  ?= python
VENV    := .venv
BIN     := $(VENV)/bin
COMPOSE := docker compose

# Windows virtualenvs put executables in Scripts/ rather than bin/.
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
endif

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Local environment --------------------------------------------------------

install: ## Create a virtualenv and install the package
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e .

dev: ## Install with development dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

# --- Quality ------------------------------------------------------------------

test: ## Run the test suite
	$(BIN)/pytest -q

test-cov: ## Run tests with a coverage report
	$(BIN)/pytest --cov=bot --cov-report=term-missing --cov-report=html

lint: ## Check formatting and lint rules
	$(BIN)/ruff check bot/ tests/ alembic/
	$(BIN)/black --check -l 100 bot/ tests/ alembic/

format: ## Apply formatting and autofixes
	$(BIN)/ruff check --fix bot/ tests/ alembic/
	$(BIN)/black -l 100 bot/ tests/ alembic/

typecheck: ## Run mypy
	$(BIN)/mypy bot/

check: lint typecheck test ## Everything CI runs

# --- Database -----------------------------------------------------------------

migrate: ## Apply migrations
	$(BIN)/alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

# --- Running ------------------------------------------------------------------

run: ## Run the bot locally
	$(BIN)/python -m bot

# --- Docker -------------------------------------------------------------------

build: ## Build the image
	$(COMPOSE) build

up: ## Start the stack in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Restart the bot only, leaving PostgreSQL up
	$(COMPOSE) restart bot

logs: ## Follow the bot's logs
	$(COMPOSE) logs -f bot

shell: ## Open a shell in the bot container
	$(COMPOSE) exec bot sh

psql: ## Open psql against the running database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-turon} -d $${POSTGRES_DB:-turon_avto}

backup: ## Dump the database to backups/ from the host
	@mkdir -p backups
	$(COMPOSE) exec -T postgres pg_dump -U $${POSTGRES_USER:-turon} $${POSTGRES_DB:-turon_avto} \
		| gzip > backups/pg_$$(date -u +%Y%m%d_%H%M%S).sql.gz
	@echo "Written to backups/"

# --- Housekeeping -------------------------------------------------------------

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
