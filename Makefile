# mini_agent Makefile
# ====================
# Standard targets for testing, coverage, linting, and cleanup.

PYTEST := python -m pytest
PYTEST_ARGS := -x -q --tb=short
COVERAGE_DIR := htmlcov

.PHONY: help test test-slow test-all test-quick coverage lint clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

test:  ## Run fast test suite (excludes slow + benchmarks)
	$(PYTEST) $(PYTEST_ARGS)

test-slow:  ## Run only slow tests (AgentRuntime, sub-agent, git, desktop ops)
	$(PYTEST) $(PYTEST_ARGS) --run-slow

test-all:  ## Run full suite (fast + slow + benchmarks)
	$(PYTEST) -v --run-slow --run-benchmarks

test-quick:  ## Run fast tests in parallel (faster, less output)
	$(PYTEST) $(PYTEST_ARGS) -n auto 2>/dev/null || $(PYTEST) $(PYTEST_ARGS)

coverage:  ## Run tests with coverage report (terminal)
	$(PYTEST) $(PYTEST_ARGS) \
		--cov=. \
		--cov-report=term-missing \
		--cov-report=html:$(COVERAGE_DIR) \
		-v
	@echo ""
	@echo "HTML report: $(COVERAGE_DIR)/index.html"

coverage-fail:  ## Run coverage, fail if under 80%
	$(PYTEST) $(PYTEST_ARGS) \
		--cov=. \
		--cov-report=term-missing \
		--cov-fail-under=80 \
		-v

lint:  ## Syntax-check all Python files
	@echo "=== py_compile ==="
	python -m py_compile $$(find . -name '*.py' -not -path './venv/*' -not -path './.venv/*' -not -path './__pycache__/*')
	@echo "=== ruff check ==="
	ruff check . --ignore=E501 || true
	@echo "=== ruff format check ==="
	ruff format --check . || true

lint-strict:  ## Lint with strict mode (fail on issues)
	ruff check .
	ruff format --check .

dist:  ## Build distributable for current platform (.dmg/.exe/.AppImage)
	bash build_dist.sh

dist-mac:  ## Build macOS .dmg
	bash build_dist.sh --mac

dist-win:  ## Build Windows .exe installer
	bash build_dist.sh --win

dist-linux:  ## Build Linux .AppImage
	bash build_dist.sh --linux

clean:  ## Remove build artifacts, cache, and coverage
	rm -rf __pycache__ .pytest_cache $(COVERAGE_DIR) .coverage
	rm -rf .ruff_cache *.egg-info dist build
	rm -rf pyinstaller_dist
	rm -rf mini_agent_electron/dist-electron
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "Clean complete."
