from langchain.tools import tool

from utils.llm import get_llm
from utils.logger import get_logger

logger = get_logger(__name__)
_llm = None


def _ensure_llm():
    global _llm
    if _llm is None:
        _llm = get_llm()


@tool
def categorize(description: str) -> str:
    """Categorize a product description into a grocery category."""
    _ensure_llm()
    prompt = (
        "You are a helpful assistant that categorizes grocery products into high"
        " level categories like 'Beverages', 'Fresh Produce', 'Bakery', etc. "
        "Respond with a short category label only.\nProduct: "
        f"{description[:4000]}"
    )
    try:
        result = _llm.predict(prompt)
        return result.strip()
    except Exception as exc:
        logger.error("Categorization failed: %s", exc)
        return "Uncategorized"
