from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DROP_", env_file=".env")

    token: str
    database_url: str
    host: str = "127.0.0.1"
    port: int = 9731
    max_body_bytes: int = 10_485_760  # 10 MiB
    cors_origins: str = ""

    @field_validator("token")
    @classmethod
    def token_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("DROP_TOKEN must not be empty")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
