from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="dev", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    database_path: str = Field(default="./data/sales_concierge.db", alias="DATABASE_PATH")
    action_log_path: str = Field(default="./logs/actions.jsonl", alias="ACTION_LOG_PATH")

    vector_dim: int = Field(default=384, alias="VECTOR_DIM")
    chunk_size: int = Field(default=900, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=140, alias="CHUNK_OVERLAP")
    top_k: int = Field(default=4, alias="TOP_K")
    retrieval_weak_threshold: float = Field(default=0.08, alias="RETRIEVAL_WEAK_THRESHOLD")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    use_openai_chat: bool = Field(default=True, alias="USE_OPENAI_CHAT")

    default_calendly_link: str = Field(
        default="https://calendly.com/globalexpatwealth/30min",
        alias="DEFAULT_CALENDLY_LINK",
    )

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def action_log_file(self) -> Path:
        path = Path(self.action_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
