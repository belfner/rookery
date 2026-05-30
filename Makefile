.PHONY: clean build upload-test upload install dev lint format typecheck check

clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

build: clean
	uv build

upload-test:
	uv run twine upload --repository testpypi dist/* --verbose

upload:
	uv run twine upload dist/* --verbose

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
