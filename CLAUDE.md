<!-- SYNC: When updating this file, also update AGENTS.md with the same changes (and vice versa). -->
# Brite Shopping Catalog Engine

Scraping, normalization, and semantic search service for grocery products. Part of the Brite Shopping price comparison platform — helps shoppers find products across stores/locations, compare prices, and shop economically.

## Product Vision
- Price comparison platform for shoppers — the goal is to help users make **economical grocery choices** (cheaper brands, bulk/wholesale pricing, cheaper store locations)
- Products have an **estimated/average price** computed from all location prices
- Each store location (physical or online) has its own price for a product
- User flow: search → see product + estimated price → see all store locations + their prices → decide
- A "store location" can be online as long as it is accessible online
- The app should let users **infer savings opportunities** from the data — we don't need to be explicit, the comparison logic should make it obvious

## Architecture
- **This repo (catalog engine)** runs on the developer's laptop — handles all heavy AI, scraping, and enrichment. Writes to MongoDB.
- **API** (`brite_shopping_api`) is a lightweight REST mediator on Render — reads from MongoDB, serves to mobile. NO AI/scraping logic.
- **Mobile** (`brite-shopping-mobile`) only talks to the API, never to the catalog engine.
- All repos at `/Users/kennethmarshall/dev/brite_shopping/`
- Shared MongoDB Atlas database (`brite_shopping`)

## Tech Stack
- **Framework**: FastAPI + Typer CLI
- **Scraping**: Playwright (headless), BeautifulSoup for HTML parsing, custom API scrapers for SPAs
- **AI/ML**: Ollama (local) — `nomic-embed-text` for embeddings, `llama3.2` for tag generation
- **Search**: FAISS vector index + MongoDB text search, blended ranking
- **Database**: MongoDB Atlas (M0 free tier)
- **Config**: Source configs in `agent/config/sources.yml`

## Development
- Python venv at `.venv/`
- Start API: `make dev` (uvicorn :8000, docs at /docs)
- CLI scrape: `.venv/bin/python -m agent.cli scrape --store-id <id>`
- CLI search: `.venv/bin/python -m agent.cli search "query"`
- Reindex: `.venv/bin/python -m agent.cli reindex`
- Tests: `pytest`
- **Ollama must be running** before scraping: `ollama serve`

## Key Constraints
- Local-first development — avoid cloud costs until revenue
- M0 Atlas cluster auto-pauses after 60 days inactivity — resume from Atlas console
- Atlas CLI installed but cannot pause/resume M0 clusters (needs M10+)

## Source Config Pattern
Sources are defined in `agent/config/sources.yml`. Each source specifies its scraping strategy:
- **CSS selector-based**: WooCommerce, Joomla, Magento stores — selectors for product name, price, image, pagination
- **LocCloud API**: ExtJS SPA stores (Hi-Lo) — custom scraper using trs.exe XML API
- WooCommerce stores share common selectors (`.woocommerce-loop-product__title`, `.woocommerce-Price-amount`, `a.next.page-numbers`)
- Magento stores use `page_param: "p"` for pagination (vs `page` for WooCommerce)

## Current Sources (~7,500+ products total)
- `grace` — Grace Foods (brand, Joomla)
- `sampars` — ShopSampars (store, WooCommerce, ~1,496 products)
- `storetodoor` — Store To Door Jamaica (store, WooCommerce, ~2,038 products)
- `coolmarket` — CoolMarket Jamaica (store, Magento 2, ~1,400+ products)
- `hilo` — Hi-Lo Food Stores (store, LocCloud/ExtJS, ~2,050 products, strategy: loccloud)
- `virtualmart` — VirtualMart Jamaica (store, WooCommerce/Playwright, not yet scraped)
- `pricesmart` — PriceSmart (JS SPA, requires Playwright + login-session)
- `loshusan` — Loshusan Supermarket (returns 403, requires login-session)
- `megamart` — MegaMart (manual-only, no online catalog)
- `progressive` — Progressive Grocers (manual-only, no online catalog)

### Scraping Strategies
- **CSS selector-based** (`strategy: playwright` or default): Standard HTML parsing with BeautifulSoup. Works for WooCommerce, Joomla, Magento.
- **LocCloud** (`strategy: loccloud`): Custom scraper for ExtJS SPAs backed by trs.exe XML API. Used by Hi-Lo. See `agent/scraping/loccloud.py`.
- **Session-based scraping**: `login-session` command saves Playwright cookies for auth-gated stores.
- **Receipt parsing**: `parse-receipt` command uses Ollama vision model (llava) to extract products/prices from receipt images.
- **Manual entry**: `add-price` command for stores with no web presence.

### LocCloud/Hi-Lo Scraping Details
Hi-Lo uses LocCloud POS platform at `hilofoodstoresja.loccloud.net` — a Sencha ExtJS SPA where standard CSS scraping doesn't work. The scraper:
1. Launches Playwright, navigates to the SPA, waits 8s for ExtJS to render
2. Logs in via `input[placeholder*="Email"]` + `input[type="Password"]` + clicks "Sign in" button (Enter key unreliable in ExtJS)
3. Captures `CN` session token from trs.exe response URLs
4. Fetches catalog via `trs.exe?cgi=eStore_pos_itm_catalog.xml&ExtMaxRecords=2000&CN={token}`
5. Parses XML records — field mapping: F01=barcode, F02=full_desc, F22=size, F29=name, F30=price, F155=brand, F2929=image_filename

Credentials stored in `.env` as `HILO_EMAIL` and `HILO_PASSWORD` (loaded via `dotenv_values()`, not shell env, due to special characters).

### Scraping Operations
- Scrapes can be run **periodically** — upsert logic (checksum-based) handles re-runs gracefully
- Enrichment (Ollama tags + embeddings) runs at ~1 product/second
- After scraping, run `reindex` to rebuild the FAISS vector index
- Resume from a specific page: temporarily modify `start_paths` in `sources.yml` (e.g., append `?p=14`)

## Future Work
- **Brand inference**: Brand names are currently hidden in the mobile UI because they're inconsistent across sources. Future: intelligently infer brand from product title, manual entry, or AI curation. Brands enable cross-brand price comparison.
- **Shopping list sync**: Currently local-only (AsyncStorage with anonymous device profile). Future: optional cloud sync if user demand warrants it.
- **Unit price comparison**: Normalize prices per unit (e.g., $/kg, $/L) to enable true value comparison across different package sizes.
- **AI agent curator for cross-store matching**: Current match_key is strict (exact normalized name + brand + size hash). Future: use embedding similarity to fuzzy-match products across stores (e.g., "Grace Coconut Milk 400ml" vs "Coconut Milk - Grace 400 ML"). This should be part of an AI curation agent that reviews and merges near-duplicate products.
- **Store locations with Google Maps**: Stores like Hi-Lo have multiple physical branches. The `StoreLocation` model needs `place_id`, `lat`, `lng` fields for map display, and an `is_online` boolean to distinguish online-only sources. Google Maps integration already exists in the API repo (`GET /stores/search`). Future: per-branch product availability and pricing.

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
