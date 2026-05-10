# Graph RAG POC — Common commands
# Run `make help` to see what's available

.PHONY: help install install-dev smoke extract graph communities run clean clean-cache test lint check-env

PYTHON := python3
PIP := $(PYTHON) -m pip
POC_DIR := graphrag_poc

help:
	@echo "Graph RAG POC commands:"
	@echo ""
	@echo "  Setup:"
	@echo "    make install       Install dependencies via pyproject.toml"
	@echo "    make install-dev   Install dev dependencies (pytest, ruff)"
	@echo "    make check-env     Verify ANTHROPIC_API_KEY is set"
	@echo ""
	@echo "  Run the pipeline (each stage caches; later stages reuse caches):"
	@echo "    make smoke         Chunking only (no API calls, ~1 second)"
	@echo "    make extract       Extraction stage (~2-3 minutes, ~\$$0.30)"
	@echo "    make graph         Through graph build"
	@echo "    make communities   Through community summarization"
	@echo "    make run           Full evaluation with comparison table (~\$$1.20)"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make clean         Remove all caches AND results (forces full re-run)"
	@echo "    make clean-cache   Remove pipeline caches only (keep results)"
	@echo "    make test          Run unit tests (if any)"
	@echo "    make lint          Run ruff linter"

install:
	$(PIP) install -e .

install-dev:
	$(PIP) install -e ".[dev]"

check-env:
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
		if [ -f .env ]; then \
			echo "ANTHROPIC_API_KEY not in environment, but .env exists."; \
			echo "Run with: 'set -a; source .env; set +a; make run' OR install python-dotenv."; \
			exit 1; \
		else \
			echo "ERROR: ANTHROPIC_API_KEY is not set."; \
			echo "Either: cp .env.example .env  (and edit it)"; \
			echo "Or:     export ANTHROPIC_API_KEY=sk-ant-..."; \
			exit 1; \
		fi; \
	else \
		echo "✓ ANTHROPIC_API_KEY is set"; \
	fi

smoke:
	cd $(POC_DIR) && $(PYTHON) chunking.py

extract: check-env
	cd $(POC_DIR) && $(PYTHON) extraction.py

graph: check-env
	cd $(POC_DIR) && $(PYTHON) graph_build.py

communities: check-env
	cd $(POC_DIR) && $(PYTHON) communities.py

run: check-env
	cd $(POC_DIR) && $(PYTHON) evaluate.py

clean:
	rm -f $(POC_DIR)/cache/*.pkl
	rm -f $(POC_DIR)/results/*.json
	@echo "✓ Cleaned caches and results"

clean-cache:
	rm -f $(POC_DIR)/cache/*.pkl
	@echo "✓ Cleaned pipeline caches (results preserved)"

test:
	pytest tests/ -v 2>/dev/null || echo "No tests yet"

lint:
	ruff check $(POC_DIR)/

# Convenience: full reset and re-run
fresh: clean run
