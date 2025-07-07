# brite-shopping-ai

An autonomous agent that scrapes Jamaican grocery product data, categorizes it
with a local LLM or HuggingFace fallback, deduplicates using FAISS embeddings,
and stores results in MongoDB. The agent is built with LangChain's AI agent
framework to automatically run scraping and data extraction tools.

## Setup

1. Install dependencies
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environment variables in `.env`
   ```env
   MONGODB_URI=mongodb://localhost:27017
   ```
3. Run the agent
   ```bash
   python main.py
   ```

The entry script will scrape `https://hiloshoppingja.com` as an example task.
