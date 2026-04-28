# Use python3 explicitly so this works before venv activation.
PYTHON ?= python3
VENV ?= .venv
PIP = $(VENV)/bin/pip
UVICORN = $(VENV)/bin/uvicorn

.PHONY: venv install run

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install -U pip
	$(PIP) install -e .

run:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000
