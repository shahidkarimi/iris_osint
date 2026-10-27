.PHONY: install css test lint fmt up down harvest demo integration report export

install:            ## local dev environment
	uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]" && npm ci

css:                ## rebuild the Tailwind stylesheet
	npm run build:css

test:               ## unit tests (no database needed)
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src

fmt:
	.venv/bin/ruff format src tests && .venv/bin/ruff check --fix src tests

up:                 ## start db + web + scheduler
	docker compose up -d --build

down:
	docker compose down

harvest:            ## one-off live harvest (incremental)
	docker compose run --rm harvester harvest

demo:               ## offline demo from fixtures (no network access to jll.com needed)
	docker compose run --rm harvester harvest --fixtures tests/fixtures/html

integration:       ## full suite incl. PostGIS integration tests
	docker compose --profile test run --rm tests

report:             ## write the quality report + sample exports to ./samples
	docker compose run --rm harvester report --out samples/quality_report.md
	docker compose run --rm harvester export --out-dir samples
