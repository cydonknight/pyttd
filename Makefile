.PHONY: help dev test test-ts test-all rebuild clean

help: ## Print available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

dev: ## Create venv, install dev deps, and npm install
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	cd vscode-pyttd && npm install

test: ## Run Python tests
	.venv/bin/pytest tests/ -v --benchmark-disable

test-ts: ## Run VSCode extension tests
	cd vscode-pyttd && npm test

test-all: test test-ts ## Run all test suites

rebuild: ## Recompile C extension
	.venv/bin/pip install -e .

clean: ## Remove build artifacts
	rm -rf build/ dist/ *.egg-info .eggs
	rm -rf .venv/lib/*/site-packages/pyttd_native*.so
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
