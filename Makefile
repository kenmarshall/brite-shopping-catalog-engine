PYTHON?=python3.11
VENV_DIR?=.venv

FORCE_LOCAL_VENV?=0

ifneq ($(FORCE_LOCAL_VENV),1)
ifneq ($(VIRTUAL_ENV),)
ifneq ($(wildcard $(VIRTUAL_ENV)/bin/python),)
VENV_DIR:=$(VIRTUAL_ENV)
USE_EXISTING_VENV:=1
endif
endif
endif
VENV_BIN=$(VENV_DIR)/bin
VENV_PYTHON=$(VENV_BIN)/python
PIP=$(VENV_PYTHON) -m pip
UVICORN=$(VENV_BIN)/uvicorn
PLAYWRIGHT=$(VENV_BIN)/playwright
PRE_COMMIT=$(VENV_BIN)/pre-commit
PYTEST=$(VENV_BIN)/pytest
RUFF=$(VENV_BIN)/ruff
BLACK=$(VENV_BIN)/black
APP_MODULE=agent.service.api:app

.PHONY: setup dev scrape reindex test lint fmt api

ifeq ($(USE_EXISTING_VENV),1)
setup:
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt
	$(PLAYWRIGHT) install --with-deps
	$(PRE_COMMIT) install || true
else
setup:
	@if ! command -v $(PYTHON) >/dev/null 2>&1; then \
		echo "python interpreter '$(PYTHON)' not found. Install it (e.g. brew install python@3.11) or override PYTHON=/path/to/python"; \
		exit 1; \
	fi
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		$(PYTHON) -m venv $(VENV_DIR); \
	fi
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt
	$(PLAYWRIGHT) install --with-deps
	$(PRE_COMMIT) install || true
endif

dev:
	$(UVICORN) $(APP_MODULE) --reload --host 0.0.0.0 --port 8000

scrape:
	$(VENV_PYTHON) -m agent scrape --store-id demo-grocer

reindex:
	$(VENV_PYTHON) -m agent reindex

test:
	$(PYTEST)

lint:
	$(RUFF) check agent tests

fmt:
	$(BLACK) agent tests

api:
	$(UVICORN) $(APP_MODULE) --host 0.0.0.0 --port 8000
