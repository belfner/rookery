.PHONY: clean build publish publish-test install dev lint format typecheck check

clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

build:
	uv build

publish: clean build
	@set -a && . ./.env && set +a && uv publish

publish-test: clean build
	@set -a && . ./.env && set +a && uv publish \
		--publish-url https://test.pypi.org/legacy/ \
		--token "$$UV_PUBLISH_TOKEN_TESTPYPI"

install:
	uv pip install -e .

dev:
	uv sync --dev

lint:
	uv run ruff check src/

format:
	uv run ruff format src/

typecheck:
	uv run mypy src/

check: lint format typecheck
