# Brite Shopping Agent

Agentic scraper and product indexing service for Jamaican grocery stores. This repository (Repo #3) focuses on scraping, normalization, and embedding for semantic search.

## Features (Stage 0–2)

- FastAPI service exposing scraping, indexing, and search endpoints.
- Typer CLI mirroring API workflows.
- Config-driven Playwright scraping pipeline (demo store included).
- Product normalization, deduplication checksum, and MongoDB persistence.
- Ollama embedding client with FAISS vector index for semantic search.
- Dockerized runtime with Makefile helpers and pytest test suite.

## Quickstart

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
make setup
make dev  # http://localhost:8000/docs
```

> **Tip:** `make setup` installs project dependencies into the currently active Python environment. Creating and activating a
> virtual environment (such as `.venv`) keeps those dependencies isolated from your global Python installation, which avoids
> version conflicts across projects.

Launch a demo scrape (uses configuration from `agent/config/stores.yml`):

```bash
make scrape
```

Rebuild the FAISS index from MongoDB:

```bash
make reindex
```

Search for products:

```bash
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"corn flakes","k":5}'
```

## Docker

```bash
docker compose up --build
```

FAISS index, session state, and cached assets are stored in `./data` which is mounted into the container.

## Project Layout

```
agent/
  cli.py                - Typer entrypoints
  config/               - Settings + store definitions
  scraping/             - Playwright client, parsers, normalization, pipeline
  db/                   - Mongo models and utilities
  embeddings/           - Ollama client, featurizer, FAISS index manager
  service/              - FastAPI app and ranking logic
  utils/                - Logging, rate limiting helpers
  vision/               - (Stage 5) placeholders for image search
```

Tests live under `tests/` with fixtures for deterministic scraping results.

## Configuration

Settings are managed via [`pydantic-settings`](https://docs.pydantic.dev/latest/usage/pydantic_settings/). Environment variables can be supplied through `.env`.

## Adding a New Store

1. Duplicate the schema in `agent/config/stores.yml`.
2. Supply navigation paths and CSS selectors.
3. Provide authentication strategy details (if needed).
4. Run `python -m agent scrape --store-id=<store>`.

## Embeddings

By default the agent calls Ollama's embedding API (`/api/embeddings`) with the model defined by `OLLAMA_EMBED_MODEL`. The resulting vectors are indexed by FAISS in `./data/faiss.index`.

## Roadmap

- **Stage 3** – Store-location pricing ingestion.
- **Stage 4** – Auth/session handling for Playwright.
- **Stage 5** – Image-driven semantic search.

Contributions should follow the staged approach and include tests for new features.
