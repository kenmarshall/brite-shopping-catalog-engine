from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class MongoSettings(BaseSettings):
    uri: str = Field(..., alias="MONGO_URI")
    db: str = Field("brite_shopping", alias="MONGO_DB")
    collection_products: str = Field("products", alias="MONGO_COLLECTION_PRODUCTS")
    collection_stores: str = Field("stores", alias="MONGO_COLLECTION_STORES")
    collection_jobs: str = Field("scrape_jobs", alias="MONGO_COLLECTION_JOBS")

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class OllamaSettings(BaseSettings):
    base_url: HttpUrl = Field("http://localhost:11434", alias="OLLAMA_BASE_URL")
    embed_model: str = Field("nomic-embed-text", alias="OLLAMA_EMBED_MODEL")
    tag_model: str | None = Field(None, alias="OLLAMA_TAG_MODEL")

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ScraperSettings(BaseSettings):
    requests_per_minute: int = Field(30, alias="REQUESTS_PER_MINUTE")
    max_concurrency: int = Field(4, alias="MAX_CONCURRENCY")
    headless: bool = Field(True, alias="HEADLESS")

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class Settings(BaseSettings):
    # Pylance does not understand that BaseSettings pulls values from env vars.
    # type: ignore[call-arg]
    mongo: MongoSettings = Field(default_factory=MongoSettings)  # type: ignore[call-arg]
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)  # type: ignore[call-arg]
    scraper: ScraperSettings = Field(default_factory=ScraperSettings)  # type: ignore[call-arg]
    stores_config_path: Path = Path("agent/config/stores.yml")
    data_dir: Path = Path("data")
    faiss_index_path: Path = data_dir / "faiss.index"
    faiss_meta_path: Path = data_dir / "faiss_meta.json"

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


__all__: list[str] = ["Settings", "get_settings"]
