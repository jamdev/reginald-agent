# Local development only. Deploys run on the host itself: see deploy/update.sh.
.PHONY: dev test lint doctor

dev:            ## sync the project venv from uv.lock (dev group is implicit)
	uv sync --extra agents --extra observability

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

doctor:
	uv run reginald doctor
