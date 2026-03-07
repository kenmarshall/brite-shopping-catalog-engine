from typing import Optional

import typer

from agent.db.models import LocationPrice, Product, SizeInfo
from agent.db.mongo import MongoService
from agent.embeddings.faiss_index import FaissIndex
from agent.embeddings.featurizer import build_embedding_text
from agent.embeddings.ollama_client import embed_sync
from agent.scraping.normalizer import (
    build_checksum,
    build_match_key,
    normalize_brand,
    normalize_category,
    normalize_name,
    parse_price,
    parse_size,
)
from agent.scraping.pipeline import run_scrape
from agent.utils.logging import get_logger

app = typer.Typer(help="Brite Shopping Agent CLI")
LOGGER = get_logger(__name__)


@app.command()
def scrape(
    store_id: Optional[str] = typer.Option(None, "--store-id"),
    url: Optional[str] = typer.Option(None, "--url"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    job = run_scrape(store_id=store_id, url=url, use_playwright=False)
    typer.echo(job)


@app.command()
def reindex() -> None:
    mongo = MongoService()
    index = FaissIndex()
    products = mongo.list_products()
    vectors: list[list[float]] = []
    ids: list[str] = []
    for product in products:
        vector = product.get("embedding")
        if not vector:
            model = Product.model_validate(product)
            text = build_embedding_text(model)
            vector = embed_sync([text])[0]
            mongo.update_embedding(product["_id"], vector)
        vectors.append(vector)
        ids.append(str(product["_id"]))
    if vectors:
        index.reset(len(vectors[0]))
        index.add_vectors(vectors, ids)
    typer.echo({"indexed": len(vectors)})


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    top_k: int = typer.Option(10, "--top-k", help="Number of results"),
) -> None:
    from fastapi.testclient import TestClient

    from agent.service.api import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.post("/search", json={"query": query, "k": top_k})
    typer.echo(response.json())


@app.command()
def backfill() -> None:
    """Backfill match_key, estimated_price, and fix location_id on legacy products."""
    mongo = MongoService()
    products = mongo.list_products()
    fixed = 0
    for doc in products:
        updates: dict = {}

        # Fix location_id: "default" → store_id
        location_prices = doc.get("location_prices", [])
        lp_changed = False
        for lp in location_prices:
            if lp.get("location_id") == "default":
                lp["location_id"] = doc["store_id"]
                lp_changed = True
            if not lp.get("store_name"):
                lp["store_name"] = doc.get("store_name")
                lp_changed = True
        if lp_changed:
            updates["location_prices"] = location_prices

        # Add estimated_price if missing
        if doc.get("estimated_price") is None and location_prices:
            amounts = [float(lp["amount"]) for lp in location_prices if lp.get("amount") is not None]
            if amounts:
                updates["estimated_price"] = round(sum(amounts) / len(amounts), 2)

        # Add match_key if missing
        if not doc.get("match_key"):
            normalized = doc.get("normalized_name") or normalize_name(doc.get("name", ""))
            brand = doc.get("brand")
            size_doc = doc.get("size", {})
            size = SizeInfo(value=size_doc.get("value"), unit=size_doc.get("unit"))
            updates["match_key"] = build_match_key(normalized, brand, size)

        if updates:
            mongo.products.update_one({"_id": doc["_id"]}, {"$set": updates})
            fixed += 1

    typer.echo(f"Backfilled {fixed}/{len(products)} products")


@app.command()
def curate(
    batch_size: int = typer.Option(50, "--batch-size", help="Products per batch"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without writing"),
) -> None:
    """AI-powered data curation: fix missing/bad categories, detect brand-as-category, etc."""
    import asyncio

    import httpx

    from agent.config.settings import get_settings

    settings = get_settings().ollama

    async def ask_llm(prompt: str) -> str:
        async with httpx.AsyncClient(base_url=str(settings.base_url), timeout=60.0) as client:
            resp = await client.post("/api/generate", json={
                "model": settings.tag_model,
                "prompt": prompt,
                "stream": False,
            })
            resp.raise_for_status()
            return resp.json().get("response", "").strip()

    mongo = MongoService()

    # Find products needing curation: missing category, or comma-separated
    query = {"$or": [
        {"category": None},
        {"category": ""},
        {"category": {"$regex": ","}},
    ]}
    products = list(mongo.products.find(query).limit(batch_size))
    typer.echo(f"Found {len(products)} products to curate")

    if not products:
        return

    fixed = 0
    for doc in products:
        name = doc.get("name", "")
        brand = doc.get("brand", "")
        old_cat = doc.get("category", "")
        tags = doc.get("tags", [])

        prompt = (
            "You are a Jamaican grocery store category assistant. "
            "Given a product name, brand, and existing tags, return the SINGLE best "
            "grocery category for this product. Use standard grocery categories like: "
            "Snacks, Dairy & Non-Dairy, Frozen Foods, Canned & Packaged, Beverages, "
            "Condiments & Sauces, Cereal & Breakfast, Rice, Pasta & Soups, Bakery, "
            "Meats, Seafood, Produce, Spices, Personal Care, Hair Care, Skin Care, "
            "Baby & Infant, Cleaning Chemicals, Laundry Centre, Medicine, Pet Care, "
            "Household, Paper & Plastics, Hot Beverages, Fats & Oil.\n"
            "Respond with ONLY the category name, nothing else.\n\n"
            f"Product: {name}\n"
            f"Brand: {brand or 'Unknown'}\n"
            f"Current category: {old_cat or 'None'}\n"
            f"Tags: {', '.join(tags) if tags else 'None'}\n"
        )

        try:
            new_cat = asyncio.run(ask_llm(prompt))
            new_cat = new_cat.split("\n")[0].strip().strip('"').strip("'").title()
        except Exception as e:
            LOGGER.warning("LLM failed for %s: %s", name, e)
            continue

        if not new_cat or len(new_cat) > 40:
            continue

        if dry_run:
            typer.echo(f"  {name[:50]:50s} | {old_cat or 'None':25s} -> {new_cat}")
        else:
            mongo.products.update_one({"_id": doc["_id"]}, {"$set": {"category": new_cat}})
            LOGGER.info("Fixed category: %s -> %s for %s", old_cat, new_cat, name)
        fixed += 1

    typer.echo(f"{'Would fix' if dry_run else 'Fixed'} {fixed}/{len(products)} products")


@app.command(name="add-price")
def add_price(
    store_id: str = typer.Option(..., "--store-id", help="Store identifier (e.g. hilo, pricesmart)"),
    store_name: str = typer.Option(..., "--store-name", help="Display name (e.g. 'Hi-Lo Half Way Tree')"),
    name: str = typer.Option(..., "--name", help="Product name"),
    price: float = typer.Option(..., "--price", help="Price amount"),
    currency: str = typer.Option("JMD", "--currency", help="Currency code"),
    brand: Optional[str] = typer.Option(None, "--brand"),
    category: Optional[str] = typer.Option(None, "--category"),
) -> None:
    """Manually add a product price for stores without web scraping (MegaMart, Progressive, etc.)."""
    from datetime import datetime

    from agent.scraping.normalizer import normalize_brand, normalize_category

    mongo = MongoService()
    normalized = normalize_name(name)
    size = parse_size(name)
    norm_brand = normalize_brand(brand)
    norm_category = normalize_category(category)
    match_key = build_match_key(normalized, norm_brand, size)
    checksum = build_checksum(store_id, normalized, norm_brand, size)

    location_price = {
        "location_id": store_id,
        "store_name": store_name,
        "amount": price,
        "currency": currency,
        "last_seen_at": datetime.utcnow(),
    }

    # Try to merge with existing product via match_key
    existing = mongo.products.find_one({"match_key": match_key})
    if existing:
        current_prices = existing.get("location_prices", [])
        # Update or add this store's price
        updated = False
        for i, lp in enumerate(current_prices):
            if lp.get("location_id") == store_id:
                current_prices[i] = location_price
                updated = True
                break
        if not updated:
            current_prices.append(location_price)

        amounts = [float(lp["amount"]) for lp in current_prices if lp.get("amount") is not None]
        estimated = round(sum(amounts) / len(amounts), 2) if amounts else None

        mongo.products.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "location_prices": current_prices,
                "estimated_price": estimated,
                "updated_at": datetime.utcnow(),
            }},
        )
        typer.echo(f"Updated existing product: {existing.get('name')} (added {store_name} @ ${price})")
    else:
        product = Product(
            store_id=store_id,
            store_name=store_name,
            name=name,
            normalized_name=normalized,
            brand=norm_brand,
            size=size,
            category=norm_category,
            url=f"manual://{store_id}",
            checksum=checksum,
            match_key=match_key,
            location_prices=[
                LocationPrice(
                    location_id=store_id,
                    store_name=store_name,
                    amount=price,
                    currency=currency,
                )
            ],
            estimated_price=price,
        )
        oid, _ = mongo.upsert_product(product)
        typer.echo(f"Created new product: {name} @ {store_name} ${price} (id: {oid})")


@app.command(name="parse-receipt")
def parse_receipt(
    image_path: str = typer.Argument(..., help="Path to receipt image"),
    store_id: str = typer.Option(..., "--store-id", help="Store identifier"),
    store_name: str = typer.Option(..., "--store-name", help="Store display name"),
    currency: str = typer.Option("JMD", "--currency"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview parsed items without saving"),
) -> None:
    """Parse a grocery receipt image using Ollama vision model and add prices to the database."""
    import asyncio
    import base64
    import json
    from pathlib import Path

    import httpx

    from agent.config.settings import get_settings

    receipt_path = Path(image_path)
    if not receipt_path.exists():
        typer.echo(f"Error: File not found: {image_path}", err=True)
        raise typer.Exit(1)

    # Read and encode image
    image_bytes = receipt_path.read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    settings = get_settings().ollama

    async def call_vision(prompt: str, image: str) -> str:
        async with httpx.AsyncClient(base_url=str(settings.base_url), timeout=120.0) as client:
            resp = await client.post("/api/generate", json={
                "model": settings.vision_model,
                "prompt": prompt,
                "images": [image],
                "stream": False,
            })
            resp.raise_for_status()
            return resp.json().get("response", "").strip()

    prompt = (
        "You are analyzing a grocery store receipt image. "
        "Extract ALL product line items with their names and prices. "
        "Return ONLY a JSON array where each element has:\n"
        '  {"name": "product name", "price": 123.45}\n'
        "Rules:\n"
        "- Use the exact product name as printed on the receipt\n"
        "- Price should be a number (no currency symbol)\n"
        "- Skip subtotals, tax lines, totals, and non-product entries\n"
        "- Skip discounts or negative amounts\n"
        "Return ONLY the JSON array, no other text."
    )

    typer.echo(f"Parsing receipt: {receipt_path.name} (model: {settings.vision_model})")
    try:
        result = asyncio.run(call_vision(prompt, image_b64))
    except Exception as e:
        typer.echo(f"Vision model error: {e}", err=True)
        raise typer.Exit(1)

    # Parse JSON from response (handle markdown fences)
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        typer.echo(f"Could not parse LLM response as JSON:\n{result}", err=True)
        raise typer.Exit(1)

    if not isinstance(items, list):
        typer.echo(f"Expected JSON array, got: {type(items).__name__}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Found {len(items)} items on receipt")

    saved = 0
    for item in items:
        item_name = item.get("name", "").strip()
        item_price = item.get("price")
        if not item_name or item_price is None:
            continue
        try:
            item_price = float(item_price)
        except (TypeError, ValueError):
            continue
        if item_price <= 0:
            continue

        if dry_run:
            typer.echo(f"  {item_name:50s} ${item_price:.2f}")
        else:
            # Use the add-price logic inline
            from datetime import datetime

            from agent.scraping.normalizer import normalize_brand, normalize_category

            normalized = normalize_name(item_name)
            size = parse_size(item_name)
            match_key = build_match_key(normalized, None, size)
            checksum = build_checksum(store_id, normalized, None, size)

            product = Product(
                store_id=store_id,
                store_name=store_name,
                name=item_name,
                normalized_name=normalized,
                size=size,
                url=f"receipt://{store_id}/{receipt_path.stem}",
                checksum=checksum,
                match_key=match_key,
                location_prices=[
                    LocationPrice(
                        location_id=store_id,
                        store_name=store_name,
                        amount=item_price,
                        currency=currency,
                    )
                ],
                estimated_price=item_price,
            )
            mongo = MongoService()
            mongo.upsert_product(product)
        saved += 1

    typer.echo(f"{'Would save' if dry_run else 'Saved'} {saved}/{len(items)} items from receipt")


@app.command(name="login-session")
def login_session(
    store_id: str = typer.Option(..., "--store-id", help="Store to create a session for"),
) -> None:
    """Open a browser for manual login, then save the session for automated scraping.

    Use this for auth-gated stores like PriceSmart or Hi-Lo. You log in manually,
    and the session cookies are saved for subsequent scrape runs.
    """
    import yaml

    from agent.config.settings import get_settings

    settings = get_settings()
    sources_path = settings.sources_config_path
    with open(sources_path) as f:
        sources = yaml.safe_load(f)

    source = next((s for s in sources if s["source_id"] == store_id), None)
    if not source:
        typer.echo(f"Error: No source config found for '{store_id}'", err=True)
        raise typer.Exit(1)

    storage_path = source.get("auth", {}).get("storage_state_path")
    if not storage_path:
        storage_path = f"./data/sessions/{store_id}.json"

    start_url = source.get("base_url", "https://google.com")
    nav = source.get("navigation", {})
    start_paths = nav.get("start_paths", [])
    if start_paths:
        start_url = start_paths[0]

    typer.echo(f"Opening browser for {store_id}...")
    typer.echo(f"Navigate to: {start_url}")
    typer.echo("Log in manually if needed, then close the browser.")
    typer.echo(f"Session will be saved to: {storage_path}")

    from pathlib import Path

    from playwright.sync_api import sync_playwright

    Path(storage_path).parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(start_url)

        typer.echo("\n--- Browser is open. Log in and browse, then CLOSE the browser window. ---\n")

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        context.storage_state(path=storage_path)
        browser.close()

    typer.echo(f"Session saved to {storage_path}")
    typer.echo(f"You can now run: python -m agent.cli scrape --store-id {store_id}")


@app.command(name="dedup-audit")
def dedup_audit(
    threshold: float = typer.Option(0.90, "--threshold", help="Embedding similarity threshold"),
    limit: int = typer.Option(100, "--limit", help="Max potential duplicates to report"),
    dry_run: bool = typer.Option(True, "--dry-run/--merge", help="Preview only (default) or merge duplicates"),
) -> None:
    """Find potential duplicate products across stores using embedding similarity."""
    mongo = MongoService()
    index = FaissIndex()

    if index.index is None:
        typer.echo("FAISS index not loaded. Run 'reindex' first.", err=True)
        raise typer.Exit(1)

    products = mongo.list_products({"embedding": {"$ne": None}})
    typer.echo(f"Checking {len(products)} products for near-duplicates (threshold={threshold})...")

    # Build lookup maps
    id_to_doc = {str(doc["_id"]): doc for doc in products}
    seen_pairs: set[tuple[str, str]] = set()
    duplicates: list[dict] = []

    for doc in products:
        doc_id = str(doc["_id"])
        embedding = doc.get("embedding")
        if not embedding:
            continue

        candidates = index.search(embedding, k=5)
        for cand_id, score in candidates:
            if cand_id == doc_id:
                continue
            if score < threshold:
                continue

            # Ensure consistent pair ordering to avoid reporting A-B and B-A
            pair = tuple(sorted([doc_id, cand_id]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            cand_doc = id_to_doc.get(cand_id)
            if not cand_doc:
                continue

            # Only flag cross-store duplicates (same store dedup is handled by checksum)
            if doc.get("store_id") == cand_doc.get("store_id"):
                continue

            # Skip if they already share a match_key (already merged)
            if doc.get("match_key") and doc.get("match_key") == cand_doc.get("match_key"):
                continue

            duplicates.append({
                "score": round(score, 4),
                "product_a": {
                    "id": doc_id,
                    "name": doc.get("name", ""),
                    "store": doc.get("store_name", ""),
                    "match_key": doc.get("match_key", "")[:12],
                },
                "product_b": {
                    "id": cand_id,
                    "name": cand_doc.get("name", ""),
                    "store": cand_doc.get("store_name", ""),
                    "match_key": cand_doc.get("match_key", "")[:12],
                },
            })

            if len(duplicates) >= limit:
                break
        if len(duplicates) >= limit:
            break

    typer.echo(f"\nFound {len(duplicates)} potential cross-store duplicates:\n")
    for dup in duplicates:
        a = dup["product_a"]
        b = dup["product_b"]
        typer.echo(f"  [{dup['score']:.2f}] {a['name'][:45]:45s} ({a['store']})")
        typer.echo(f"         {b['name'][:45]:45s} ({b['store']})")
        typer.echo()

    if not dry_run and duplicates:
        typer.echo("Merge mode not yet implemented. Use the admin dashboard to review and merge.")


@app.command(name="seed-barcodes")
def seed_barcodes(
    store_id: str = typer.Option("hilo", "--store-id", help="Store to extract barcodes from"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
) -> None:
    """Extract barcodes from existing Hi-Lo product URLs and populate barcode_mappings.

    Hi-Lo product URLs contain the barcode in the `item` query param:
      ...&item=5012345678901
    This command reads existing products from MongoDB and seeds the barcode DB
    without re-scraping.
    """
    import re
    from urllib.parse import parse_qs, urlparse

    mongo = MongoService()
    products = mongo.list_products({"store_id": store_id, "url": {"$regex": r"item="}})
    typer.echo(f"Found {len(products)} {store_id} products with item= in URL")

    seeded = 0
    skipped = 0
    for doc in products:
        url = doc.get("url", "")
        parsed = urlparse(url)
        # item= may be in query or fragment (Hi-Lo uses hash-based routing)
        params = parse_qs(parsed.query) or parse_qs(parsed.fragment.split("?", 1)[-1] if "?" in parsed.fragment else "")
        barcode_values = params.get("item", [])
        if not barcode_values:
            # Fallback: regex extract from full URL
            m = re.search(r"[&?]item=(\d{4,})", url)
            if m:
                barcode_values = [m.group(1)]
        if not barcode_values:
            skipped += 1
            continue

        barcode = barcode_values[0].strip()
        if not barcode or not barcode.isdigit():
            skipped += 1
            continue

        product_id = str(doc["_id"])
        product_name = doc.get("name", "")

        if dry_run:
            typer.echo(f"  {barcode:15s} → {product_name[:60]}")
        else:
            mongo.upsert_barcode(barcode, product_id, f"{store_id}_seed", product_name)
        seeded += 1

    typer.echo(
        f"{'Would seed' if dry_run else 'Seeded'} {seeded} barcodes "
        f"(skipped {skipped} products without barcodes)"
    )


@app.command(name="fix-apostrophes")
def fix_apostrophes(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without writing"),
    field: Optional[str] = typer.Option(None, "--field", help="Limit to 'brand' or 'category' (default: both)"),
) -> None:
    """Fix 'S uppercase in brand/category fields caused by old .title() normalization.

    Finds products where brand or category contains an apostrophe followed by an
    uppercase letter (e.g. "Member'S") and re-applies the correct normalization.
    """
    import re

    mongo = MongoService()
    apostrophe_pattern = re.compile(r"'[A-Z]")
    fields_to_check = []
    if field in ("brand", None):
        fields_to_check.append("brand")
    if field in ("category", None):
        fields_to_check.append("category")

    query: dict = {
        "$or": [
            {f: {"$regex": r"'[A-Z]"}} for f in fields_to_check
        ]
    }
    docs = list(mongo.products.find(query, {f: 1 for f in fields_to_check}))
    typer.echo(f"Found {len(docs)} products with apostrophe-uppercase in {fields_to_check}")

    updated = 0
    for doc in docs:
        changes: dict = {}
        for f in fields_to_check:
            val = doc.get(f)
            if not val or not apostrophe_pattern.search(val):
                continue
            fixed = normalize_brand(val) if f == "brand" else normalize_category(val)
            if fixed != val:
                changes[f] = fixed
        if not changes:
            continue

        if dry_run:
            for f, fixed in changes.items():
                typer.echo(f"  [{doc['_id']}] {f}: {doc[f]!r} → {fixed!r}")
        else:
            mongo.products.update_one({"_id": doc["_id"]}, {"$set": changes})
        updated += 1

    typer.echo(
        f"{'Would update' if dry_run else 'Updated'} {updated} products"
    )


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
