"""Simple HTTP client for Ollama LLM."""

import json
import requests
from typing import Dict, Any, Optional

from .logger import get_logger

logger = get_logger(__name__)

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:1b"  # Using a smaller model as default


class OllamaClient:
    """Simple HTTP client for Ollama."""
    
    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = DEFAULT_MODEL):
        self.base_url = base_url
        self.model = model
    
    def is_available(self) -> bool:
        """Check if Ollama is running."""
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return response.status_code == 200
        except requests.RequestException:
            return False
    
    def generate_text(self, prompt: str, temperature: float = 0.1) -> Optional[str]:
        """Generate text using Ollama."""
        if not self.is_available():
            logger.error("Ollama is not available at %s", self.base_url)
            return None
        
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature
            }
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            return result.get("response", "").strip()
            
        except requests.RequestException as e:
            logger.error("Ollama request failed: %s", e)
            return None
    
    def generate_json(self, prompt: str, temperature: float = 0.1) -> Optional[Dict[str, Any]]:
        """Generate JSON response using Ollama with error handling."""
        json_prompt = f"{prompt}\n\nRespond with valid JSON only."
        
        response = self.generate_text(json_prompt, temperature)
        if not response:
            return None
        
        # Try to extract JSON from response
        try:
            # Handle cases where response might have extra text
            response = response.strip()
            
            # Look for JSON object boundaries
            start = response.find('{')
            end = response.rfind('}') + 1
            
            if start != -1 and end > start:
                json_str = response[start:end]
                return json.loads(json_str)
            else:
                # Try parsing the entire response
                return json.loads(response)
                
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from Ollama response: %s", e)
            logger.error("Raw response: %s", response)
            return None


# Global client instance
_client = None

def get_ollama_client() -> OllamaClient:
    """Get or create the global Ollama client."""
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client 