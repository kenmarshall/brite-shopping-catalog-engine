"""LLM-powered scraping agent using LangChain."""

from typing import List

from langchain.agents import AgentType, initialize_agent

from utils.logger import get_logger
from utils.llm import get_llm
from tools.scrape_and_parse import scrape_and_parse
from tools.categorize import categorize
from tools.save_product import save_product

logger = get_logger(__name__)


class ScraperAgent:
    """Agent that orchestrates scraping, categorization, and saving."""

    def __init__(self) -> None:
        llm = get_llm()
        self._agent = initialize_agent(
            [scrape_and_parse, categorize, save_product],
            llm,
            agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
            verbose=True,
        )

    def process_url(self, url: str) -> List[str]:
        """Run the agent on the given URL."""
        logger.info("Processing URL with agent: %s", url)
        task = f"Scrape grocery products and store them from {url}"
        result = self._agent.run(task)
        return [result]
