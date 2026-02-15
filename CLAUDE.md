# Brite Shopping Catalog Engine

Scraping, normalization, and semantic search service for Jamaican grocery products. Part of the Brite Shopping price comparison platform — helps Jamaican shoppers find products across stores/locations, compare prices, and shop economically.

## Product Vision
- Price comparison platform for Jamaican shoppers — the goal is to help users make **economical grocery choices** (cheaper brands, bulk/wholesale pricing, cheaper store locations)
- Products have an **estimated/average price** computed from all location prices
- Each store location (physical or online) has its own price for a product
- User flow: search → see product + estimated price → see all store locations + their prices → decide
- A "store location" can be online as long as Jamaicans can access it
- The app should let users **infer savings opportunities** from the data — we don't need to be explicit, the comparison logic should make it obvious

## Architecture
- **This repo (catalog engine)** runs on the developer's laptop — handles all heavy AI, scraping, and enrichment. Writes to MongoDB.
- **API** (`brite_shopping_api`) is a lightweight REST mediator on Render — reads from MongoDB, serves to mobile. NO AI/scraping logic.
- **Mobile** (`brite-shopping-mobile`) only talks to the API, never to the catalog engine.
- All repos at `/Users/kennethmarshall/dev/brite_shopping/`
- Shared MongoDB Atlas database (`brite_shopping`)

## Tech Stack
- **Framework**: FastAPI + Typer CLI
- **Scraping**: Playwright (headless), BeautifulSoup for parsing
- **AI/ML**: Ollama (local) — `nomic-embed-text` for embeddings, `llama3.2` for tag generation
- **Search**: FAISS vector index + MongoDB text search, blended ranking
- **Database**: MongoDB Atlas (M0 free tier)
- **Config**: Source configs in `agent/config/sources.yml`

## Development
- Python venv at `.venv/`
- Start API: `make dev` (uvicorn :8000, docs at /docs)
- CLI scrape: `.venv/bin/python -m agent.cli scrape --source-id <id>`
- CLI search: `.venv/bin/python -m agent.cli search "query"`
- Reindex: `.venv/bin/python -m agent.cli reindex`
- Tests: `pytest`
- **Ollama must be running** before scraping: `ollama serve`

## Key Constraints
- Local-first development — avoid cloud costs until revenue
- M0 Atlas cluster auto-pauses after 60 days inactivity — resume from Atlas console
- Atlas CLI installed but cannot pause/resume M0 clusters (needs M10+)

## Source Config Pattern
Sources are defined in `agent/config/sources.yml` with CSS selectors for product parsing.
WooCommerce stores share common selectors (`.woocommerce-loop-product__title`, `.woocommerce-Price-amount`, `a.next.page-numbers`).

## Current Sources
- `grace` — Grace Foods (brand, Joomla)
- `sampars` — ShopSampars (store, WooCommerce, ~1496 products)
- `storetodoor` — Store To Door Jamaica (store, WooCommerce, ~2038 products)
- `coolmarket` — CoolMarket Jamaica (store, Magento 2, not yet scraped)
- `virtualmart` — VirtualMart Jamaica (store, WooCommerce/Playwright, not yet scraped)
- `hilo` — Hi-Lo Food Stores (requires Playwright + login-session, hides prices via CSS)
- `pricesmart` — PriceSmart (JS SPA, requires Playwright + login-session)
- `loshusan` — Loshusan Supermarket (returns 403, requires login-session)
- `megamart` — MegaMart (manual-only, no online catalog)
- `progressive` — Progressive Grocers (manual-only, no online catalog)

### Data Ingestion Methods
- **Automated scraping**: Standard CSS selector-based (WooCommerce, Joomla, Magento)
- **Session-based scraping**: `login-session` command saves Playwright cookies for auth-gated stores
- **Receipt parsing**: `parse-receipt` command uses Ollama vision model (llava) to extract products/prices from receipt images
- **Manual entry**: `add-price` command for stores with no web presence

## Future Work
- **Brand inference**: Brand names are currently hidden in the mobile UI because they're inconsistent across sources. Future: intelligently infer brand from product title, manual entry, or AI curation. Brands enable cross-brand price comparison.
- **Shopping list sync**: Currently local-only (AsyncStorage with anonymous device profile). Future: optional cloud sync if user demand warrants it.
- **Unit price comparison**: Normalize prices per unit (e.g., $/kg, $/L) to enable true value comparison across different package sizes.

## Data Quality Principles
- All prices are in JMD unless explicitly verified otherwise
- Categories should be clean, single-value labels (no comma-separated)
- Image URLs should be real images, not SVG/lazy-load placeholders
- Cross-store product matching uses match_key (SHA256 of normalized_name + brand + size)
- The AI enrichment pipeline (Ollama) generates tags and embeddings — these should be reviewed for accuracy

## UX Design Principles
These principles apply across the entire Brite Shopping platform. Prioritize modern best practices and suggest improvements when they are absent or violated.

- **Search must be forgiving** — support partial matching, typo tolerance, and prefix search. Users should see results as they type, not only after typing a complete word.
- **Data presentation must be consistent** — product cards should show uniform information (name, brand, price, category) without mixing up field types (e.g., brand shown as category).
- **Progressive disclosure** — show the most important info first (name, price, store count), details on tap.
- **Feedback on every action** — loading states, empty states, error states with retry options.
- **Mobile-first** — touch targets >= 44pt, readable text sizes, scrollable content, no horizontal overflow.
- **Accessibility** — support dark/light mode, sufficient contrast ratios, screen reader labels.
- **Performance** — debounce search input, lazy-load images, paginate long lists.
- **Data quality matters for UX** — incorrect categories, missing images, or wrong currencies directly harm user trust. Proactively flag and fix data issues.
