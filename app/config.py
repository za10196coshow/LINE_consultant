from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    line_channel_secret: str
    line_channel_access_token: str
    openai_api_key: str
    openai_model: str = "gpt-5-mini"
    database_path: str = "data/kanji.db"
    ai_kanji_name: str = "幹事"
    log_level: str = "INFO"
    timezone: str = "Asia/Tokyo"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def ensure_database_directory(self) -> None:
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()

