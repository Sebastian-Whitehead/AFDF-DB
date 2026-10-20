from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://economy:change-me@localhost:5432/economy"
    api_keys: str = ""
    token_price: int = 10
    challenge_fee_enabled: bool = True
    challenge_fee_percent: int = 5
    log_level: str = "INFO"

    @property
    def valid_api_keys(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
