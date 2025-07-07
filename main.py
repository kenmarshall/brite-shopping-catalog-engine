from agents.scraper_agent import ScraperAgent
from utils.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    url = "https://hiloshoppingja.com"
    agent = ScraperAgent()
    results = agent.process_url(url)
    logger.info("Processing finished: %s", results)


if __name__ == "__main__":
    main()
