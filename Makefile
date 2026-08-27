.PHONY: backend-install backend-dev backend-test backend-lint backend-typecheck \
	frontend-install frontend-dev frontend-test frontend-lint frontend-build verify

backend-install:
	cd apps/backend && uv sync --group dev

backend-dev:
	cd apps/backend && uv run bitcoin-intel-api

backend-test:
	cd apps/backend && uv run pytest

backend-lint:
	cd apps/backend && uv run ruff check .
	cd apps/backend && uv run ruff format --check .

backend-typecheck:
	cd apps/backend && uv run mypy src tests scripts

frontend-install:
	cd apps/frontend && npm ci

frontend-dev:
	cd apps/frontend && npm run dev

frontend-test:
	cd apps/frontend && npm test

frontend-lint:
	cd apps/frontend && npm run format:check
	cd apps/frontend && npm run lint

frontend-build:
	cd apps/frontend && npm run build

verify: backend-lint backend-typecheck backend-test frontend-lint frontend-test frontend-build
