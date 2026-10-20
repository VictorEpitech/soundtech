import re
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://sound:sound@localhost:5432/sound"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    service_name: str = "sound"

    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    jwt_secret: str = ""
    bootstrap_admin_email: str = ""
    access_token_ttl: int = 900
    refresh_token_ttl: int = 28800

    ha_url: str = ""
    ha_token: SecretStr = SecretStr("")
    ha_tts_service: str = "notify.alexa_media"
    ha_timeout: float = 10.0
    ha_retries: int = 2
    test_prefix: str = "Ceci est un test."
    announcement_max_length: int = 500

    @field_validator("ha_tts_service")
    @classmethod
    def _service_format(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", v):
            raise ValueError("HA_TTS_SERVICE doit avoir le format domaine.service")
        return v

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_url and self.ha_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
