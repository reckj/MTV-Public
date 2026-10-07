# Run from the repo root. PYTHON picks the interpreter used to create .venv;
# the target machine has python3 = 3.13 (Raspberry Pi OS Trixie); the code stays 3.11-compatible.
PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: dev test lint install clean

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

install: $(BIN)/python
	$(BIN)/pip install -q -r requirements.txt -r requirements-dev.txt

dev: install
	$(BIN)/python -m monitoni --mock --mock-purchase

test: install
	$(BIN)/python -m pytest

lint: install
	$(BIN)/ruff check .

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
