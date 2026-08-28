from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    line_channel_secret: str
    line_channel_access_token: str
    openai_api_key: str
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 45.0
    openai_search_timeout_seconds: float = 75.0
    daily_api_budget_jpy: float = 100.0
    daily_api_stop_threshold_jpy: float = 90.0
    usd_jpy_rate: float = 150.0
    conversation_assistant_cooldown_minutes: int = 20
    unanswered_question_delay_seconds: int = 30
    unanswered_question_delay_messages: int = 1
    conversation_assistant_confidence_threshold: float = 0.78
    conversation_proactive_threshold: float = 0.65
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
