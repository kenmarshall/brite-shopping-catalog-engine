PYTHON=python3
PIP=$(PYTHON) -m pip
APP_MODULE=agent.service.api:app

.PHONY: setup dev scrape reindex test lint fmt api

setup:
$(PIP) install -r requirements.txt
$(PYTHON) -m playwright install --with-deps
pre-commit install || true

dev:
uvicorn $(APP_MODULE) --reload --host 0.0.0.0 --port 8000

scrape:
$(PYTHON) -m agent scrape --store-id demo-grocer

reindex:
$(PYTHON) -m agent reindex

test:
pytest

lint:
ruff check agent tests

fmt:
black agent tests

api:
uvicorn $(APP_MODULE) --host 0.0.0.0 --port 8000
