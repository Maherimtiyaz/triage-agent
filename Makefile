# Developer shortcuts. On Windows, run these from a shell that has `make`
# (Git Bash / WSL / chocolatey make). They are thin wrappers over the same
# commands documented in README.md.

.PHONY: help install dev test lint fmt evals demo clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package (runtime only)
	pip install -e .

dev:  ## Install with dev + anthropic extras
	pip install -e ".[dev,anthropic]"

test:  ## Run the full test suite
	pytest -q

lint:  ## Check style and correctness
	ruff check .

fmt:  ## Auto-fix lint issues
	ruff check . --fix

evals:  ## Run the eval gate (accuracy must be 100%)
	python evals/runner.py

demo:  ## Run one investigation end-to-end with a trace
	python -m triage_agent "Alert: checkout error rate spiked to 35% at 14:05."

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
