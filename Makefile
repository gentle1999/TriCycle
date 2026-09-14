PYTHON ?= python3.11

PYTHONPATH := $(CURDIR)/packages/tricycle-core/src:$(CURDIR)/packages/tricycle-bohr/src:$(CURDIR)/packages/tricycle-bohrsubmit/src:$(CURDIR)/packages/tricycle-reaction-db/src:$(CURDIR)/packages/tricycle-sar/src:$(CURDIR)/packages/tricycle-rits/src:$(CURDIR)/packages/tricycle-sampling/src:$(CURDIR)/packages/tricycle-reactionopt/src:$(CURDIR)/packages/tricycle-exploration/src:$(CURDIR)/apps/tricycle-agent/src
export PYTHONPATH

.PHONY: help check ci-test test compile lint format list-plugins list-capabilities

help:
	@printf '%s\n' 'make check          run compile, tests, and optional lint checks' \
		'make ci-test        run the locked uv CI suite for every workspace project' \
		'make test           run root and package-local tests' \
		'make list-plugins   list the registered compatibility components' \
		'make list-capabilities list the Codex-facing capability manifests' \
		'uv sync             create the uv workspace environment'

compile:
	$(PYTHON) -m compileall -q packages apps tests

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-core/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-bohr/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-bohrsubmit/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-reaction-db/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-sar/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-rits/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-sampling/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-exploration/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s packages/tricycle-reactionopt/tests -p 'test_*.py' -v
	$(PYTHON) -m unittest discover -s apps/tricycle-agent/tests -p 'test_*.py' -v

lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check .; else echo 'ruff is not installed; skipped'; fi

format:
	@if command -v ruff >/dev/null 2>&1; then ruff format .; else echo 'ruff is not installed; skipped'; fi

list-plugins:
	$(PYTHON) -m tricycle_agent plugins

list-capabilities:
	$(PYTHON) -m tricycle_agent capabilities

check: compile test lint

ci-test:
	bash tools/ci_test.sh
