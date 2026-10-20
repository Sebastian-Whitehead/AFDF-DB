from functools import lru_cache
import re

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://economy:change-me@localhost:5432/economy"
    api_clients: str = ""
    api_read_only_clients: str = ""
    token_price: int = 10
    challenge_fee_enabled: bool = True
    challenge_fee_percent: int = 5
    log_level: str = "INFO"

    @property
    def api_clients_by_key(self) -> dict[str, str]:
        clients: dict[str, str] = {}
        for entry in self.api_clients.split(","):
            if not entry:
                continue
            name, separator, key = entry.partition("=")
            if not separator:
                continue
            clients[key.strip()] = name.strip()
        return clients

    @property
    def valid_api_keys(self) -> set[str]:
        return set(self.api_clients_by_key)

    @property
    def read_only_clients(self) -> set[str]:
        return {name.strip() for name in self.api_read_only_clients.split(",") if name.strip()}

    @model_validator(mode="after")
    def validate_api_client_configuration(self) -> "Settings":
        entries = [entry.strip() for entry in self.api_clients.split(",") if entry.strip()]
        parsed: list[tuple[str, str]] = []
        for entry in entries:
            name, separator, key = entry.partition("=")
            if not separator or not name.strip() or not key.strip():
                raise ValueError("API_CLIENTS entries must use name=token format")
            name = name.strip()
            key = key.strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None:
                raise ValueError("API_CLIENTS names may contain only letters, digits, '.', '_' and '-'")
            parsed.append((name, key))
        names = [name for name, _ in parsed]
        keys = [key for _, key in parsed]
        if len(names) != len(set(names)):
            raise ValueError("API_CLIENTS names must be unique")
        if len(keys) != len(set(keys)):
            raise ValueError("API_CLIENTS tokens must be unique")
        unknown_clients = self.read_only_clients - set(names)
        if unknown_clients:
            raise ValueError("API_READ_ONLY_CLIENTS must contain only configured API_CLIENTS names")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
