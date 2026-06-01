.PHONY: help install dev-up dev-down create-tables apply-schema seed seed-dry api test lint

export PYTHONPATH := .:src

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install:  ## Install package + dev deps into the active venv
	pip install -e ".[dev]"

dev-up:  ## Start DynamoDB Local + Postgres via docker compose
	docker compose up -d

dev-down:  ## Stop local infra
	docker compose down

create-tables:  ## Create the DynamoDB table (uses .env / config)
	python scripts/create_dynamo_tables.py

apply-schema:  ## Apply the Aurora/Postgres schema
	python scripts/apply_aurora_schema.py

seed:  ## Seed the fake data centre into DynamoDB
	python scripts/seed_data.py

seed-dry:  ## Generate the fake data centre to JSON (no AWS needed)
	python scripts/seed_data.py --dry-run

api:  ## Run the FastAPI app locally
	uvicorn floorcast.api.main:app --reload --app-dir src --port 8080

test:  ## Run the test suite
	pytest

lint:  ## Lint with ruff
	ruff check src scripts tests
