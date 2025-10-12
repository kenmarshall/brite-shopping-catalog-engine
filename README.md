# Brite Catalog Builder

A specialized product catalog builder for the Brite mobile shopping ecosystem. This AI utility extracts, structures, and deduplicates product information from e-commerce websites using local LLMs via Ollama, creating a comprehensive product database for mobile app search and price comparison features.

## Role in Brite Ecosystem

This catalog builder is one of three core components:
- **brite-shopping-mobile** (React Native) - User-facing mobile app for product search & price comparison
- **brite-shopping-api** (Backend) - API server that provides structured product data to mobile app  
- **brite-catalog-builder** (This project) - AI utility that populates the product database

**Data Flow**: Websites → AI Extraction → Clean Product Database → API → Mobile App → Users add store prices

## Architecture

The system follows a clean, modular pipeline approach:

**Pipeline Flow:** scrape → extract → tag → deduplicate → save

### Core Modules

- **`modules/web_scraper.py`** - Simple HTTP-based web scraping without LLM dependencies
- **`modules/product_extractor.py`** - Extract structured product info from HTML using Ollama
- **`modules/deduplicator.py`** - Detect duplicates using FAISS and sentence embeddings
- **`modules/pipeline.py`** - Orchestrate the complete data processing pipeline
- **`utils/ollama_client.py`** - Direct HTTP client for Ollama API calls

### Key Features

- **No Agent Framework** - Uses simple, direct HTTP calls to Ollama instead of LangChain
- **Modular Design** - Each processing step is a standalone, testable module
- **JSON-based LLM Tasks** - All LLM prompts are designed to return structured JSON
- **Duplicate Detection** - Uses sentence embeddings and FAISS for similarity matching
- **Error Handling** - Robust error handling with detailed logging
- **Configurable** - Environment variable configuration for URLs and thresholds

## Setup

### Prerequisites

1. **Ollama** - Install and run Ollama locally:
   ```bash
   # Install Ollama (macOS)
   curl -fsSL https://ollama.ai/install.sh | sh
   
   # Pull a model (we recommend a small, fast model)
   ollama pull llama3.2:1b
   # or
   ollama pull mistral:7b
   ```

2. **MongoDB** - Ensure MongoDB is running locally or configure connection string

### Installation

1. Clone and install dependencies:
   ```bash
   git clone <repository-url>
   cd brite-shopping-catalog-engine
   pip install -r requirements.txt
   ```

2. Configure environment variables (optional):
   ```bash
   # Create .env file
   echo "MONGODB_URI=mongodb://localhost:27017" > .env
   echo "MONGODB_DB=brite" >> .env
   echo "MONGODB_COLLECTION=products" >> .env
   echo "SIMILARITY_THRESHOLD=0.15" >> .env
   ```

## Usage

### Basic Usage

```bash
# Process the default URL
python main.py

# Process a specific URL
SCRAPE_URL="https://example-store.com" python main.py

# Process multiple URLs
ADDITIONAL_URLS="https://store1.com,https://store2.com" python main.py

# Adjust duplicate detection sensitivity (lower = more strict)
SIMILARITY_THRESHOLD=0.1 python main.py
```

### Programmatic Usage

```python
from modules.pipeline import process_url, process_urls

# Process a single URL
result = process_url("https://example-store.com")
print(f"Found {result['products_count']} products")

# Process multiple URLs
results = process_urls([
    "https://store1.com",
    "https://store2.com"
])

for result in results:
    print(f"{result['url']}: {result['status']}")
```

### Advanced Usage

```python
from modules.pipeline import ProductPipeline
from modules.product_extractor import ProductExtractor
from modules.deduplicator import ProductDeduplicator

# Create custom pipeline
pipeline = ProductPipeline(similarity_threshold=0.1)

# Process with detailed results
result = pipeline.process_url("https://example.com")
stats = pipeline.get_stats()

print(f"Processed: {stats['products_extracted']} products")
print(f"Saved: {stats['products_saved']} unique products")
print(f"Duplicates: {stats['duplicates_found']}")

pipeline.close()
```

## Configuration

### Environment Variables

- `SCRAPE_URL` - Default URL to scrape (default: https://hiloshoppingja.com)
- `ADDITIONAL_URLS` - Comma-separated list of additional URLs
- `SIMILARITY_THRESHOLD` - Duplicate detection threshold (default: 0.15)
- `MONGODB_URI` - MongoDB connection string (default: mongodb://localhost:27017)
- `MONGODB_DB` - Database name (default: brite)
- `MONGODB_COLLECTION` - Collection name (default: products)

### Ollama Configuration

The system uses Ollama running on `http://localhost:11434` by default. You can modify the model and configuration in `utils/ollama_client.py`:

```python
DEFAULT_MODEL = "llama3.2:1b"  # Change to your preferred model
```

## Data Structure

### Product Schema

```json
{
  "name": "Product Name",
  "brand": "Brand Name or null",
  "size": "Size/quantity or null", 
  "price": "Price with currency",
  "description": "Description or null",
  "category": "Category or null",
  "tags": ["tag1", "tag2", "tag3"],
  "url": "Source URL"
}
```

## Testing

The modular design makes testing straightforward:

```python
# Test individual components
from modules.web_scraper import scrape_website
from modules.product_extractor import extract_products_from_html
from modules.deduplicator import is_duplicate_product

# Test scraping
html = scrape_website("https://example.com")

# Test extraction
products = extract_products_from_html(html)

# Test deduplication
is_dup = is_duplicate_product(products[0])
```

## Performance Notes

- Uses lightweight models by default (llama3.2:1b) for speed
- FAISS indexing provides fast similarity search
- Configurable similarity thresholds balance precision vs. recall
- MongoDB provides scalable storage for large product catalogs

## Troubleshooting

1. **Ollama not available**: Ensure Ollama is running with `ollama serve`
2. **No products found**: Check if the website structure matches expected patterns
3. **JSON parsing errors**: The system includes robust error handling for malformed LLM responses
4. **MongoDB connection**: Verify MongoDB is running and accessible

## Migration from Agent-based Systems

This system replaces LangChain agent frameworks with:
- Direct HTTP calls to Ollama
- Simple function-based processing
- Clear data flow pipeline
- No autonomous behavior or tool selection
