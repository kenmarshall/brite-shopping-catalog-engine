# Brite Shopping Catalog Engine

Scraping, normalization, and semantic search service for Jamaican grocery products. Part of the **Brite Shopping** ecosystem — a price comparison platform that helps Jamaican shoppers find products across stores and locations, compare prices, and make informed, economical shopping decisions.

## How It Works

The catalog engine scrapes product listings from Jamaican grocery stores and brands (both physical and online), normalizes the data, generates AI-powered tags and embeddings, and stores everything in MongoDB for the API and mobile app to consume.

```
Sources (stores/brands) → Scrape → Normalize → Enrich (AI tags + embeddings) → MongoDB + FAISS
```

A product can appear at multiple store locations, each with its own price. The system tracks **where** a product is available, **how much** it costs at each location, and enables semantic search so users can find products even with approximate or partial names.

### Data Flow (Full Ecosystem)

```
Catalog Engine (laptop) → MongoDB Atlas → REST API (Render) → Mobile App → Jamaican Shoppers
```

The catalog engine runs locally on the developer's machine to keep costs low — all heavy AI, scraping, and enrichment happens here. The API on Render is a lightweight REST mediator that reads from MongoDB and serves data to the mobile app. The mobile app only talks to the API.

## Current Sources

| Source | Type | Products | Platform |
|--------|------|----------|----------|
| Grace Foods | Brand | ~215 | Joomla |
| ShopSampars | Store (online) | ~3,028 | WooCommerce |
| Store To Door Jamaica | Store (online) | ~2,110 | WooCommerce |

Sources are configured in `agent/config/sources.yml`. A store location can be online — as long as Jamaicans can access it, it counts.

## Features

- Config-driven scraping pipeline (Playwright or httpx)
- Product normalization, deduplication (checksum-based), and MongoDB persistence
- AI tag generation via Ollama (llama3.2) for enriched search
- Semantic embeddings via Ollama (nomic-embed-text) with FAISS vector index
- Blended search: 60% vector similarity, 30% text match, 10% tag match
- FastAPI service with scrape, search, and index endpoints
- Typer CLI mirroring API workflows
- Dockerized runtime with Makefile helpers and pytest test suite

## Quickstart

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
make setup
```

**Prerequisites:** Ollama must be running locally (`ollama serve`) with models `nomic-embed-text` and `llama3.2` pulled. MongoDB Atlas cluster must be active.

```bash
make dev  # http://localhost:8000/docs
```

### Scrape a source

```bash
# Via CLI
.venv/bin/python -m agent.cli scrape --store-id sampars

# Via API
curl -X POST http://localhost:8000/scrape/start \
  -H 'Content-Type: application/json' \
  -d '{"store_id":"sampars"}'
```

### Rebuild the FAISS index

```bash
make reindex
```

### Search for products

```bash
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"rice and peas","k":5}'
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
  config/               - Settings + source definitions (sources.yml)
  scraping/             - Playwright client, parsers, normalization, pipeline
  db/                   - Pydantic models (Product, Store, LocationPrice) and MongoDB service
  embeddings/           - Ollama client, featurizer, FAISS index manager
  service/              - FastAPI app and ranking logic
  utils/                - Logging, rate limiting helpers
  vision/               - (Future) placeholders for image search
```

Tests live under `tests/` with fixtures for deterministic scraping results.

## Adding a New Source

1. Inspect the store's website to identify CSS selectors for product cards, names, prices, images.
2. Add an entry to `agent/config/sources.yml` following the existing pattern.
3. Test with `max_pages: 2` first to validate selectors.
4. Run `.venv/bin/python -m agent.cli scrape --store-id=<source_id>`.

## Data Model

- **Product** — a grocery item with name, brand, size, category, tags, embedding, an `estimated_price` (average across locations), and a list of `LocationPrice` entries
- **LocationPrice** — price at a specific store location (location_id, amount, currency). Each store/branch can have a different price.
- **Store** — source metadata with a list of `StoreLocation` entries (physical or online)
- **ScrapeJob** — job tracking with status, stats, and error logs

The checksum (SHA256 of store_id + normalized_name + brand + size) prevents duplicate products within a source. Cross-store product matching (so the same product from different stores is recognized as one) is on the roadmap.

## Roadmap

- **Store-location pricing** — track per-location prices so the same product shows different prices at different stores/branches
- **Cross-store product matching** — deduplicate the same product across different sources for true price comparison
- **Auth/session handling** — support stores requiring login (e.g., PriceSmart membership)
- **Image-driven search** — snap a photo of a product to find it and compare prices
- **Currency handling** — properly detect and normalize USD vs JMD prices per source

## Configuration

Settings are managed via `pydantic-settings`. Environment variables can be supplied through `.env`. See `.env.example` for all options.
