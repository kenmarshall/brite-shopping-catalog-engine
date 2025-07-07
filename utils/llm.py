import requests
from langchain.llms import HuggingFaceEndpoint, Ollama

from .logger import get_logger

logger = get_logger(__name__)


def get_llm():
    """Return an LLM instance, preferring local Ollama."""
    try:
        requests.get("http://localhost:11434", timeout=2)
        logger.info("Using local Ollama LLM")
        return Ollama(base_url="http://localhost:11434", model="llama2")
    except requests.RequestException:
        logger.warning("Ollama not available, falling back to HuggingFace")
        return HuggingFaceEndpoint(repo_id="google/flan-t5-base")
