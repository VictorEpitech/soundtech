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

    ma_url: str = ""
    ma_token: SecretStr = SecretStr("")
    ma_timeout: float = 10.0

    alert_confirm_text: str = "ALERTE"
    alert_test_confirm_text: str = "TEST"
    alert_duck_timeout: float = 2.0
    alert_rate_limit: int = 10
    alert_rate_window: int = 60
    alert_send_lease: int = 120

    rl_window: int = 60
    rl_auth_limit: int = 30
    rl_refresh_limit: int = 30
    rl_announce_limit: int = 20
    rl_music_limit: int = 120
    rl_webhook_limit: int = 3000

    teams_app_id: str = ""
    teams_app_secret: SecretStr = SecretStr("")
    teams_tenant_id: str = ""
    teams_service_url: str = ""
    teams_conversation_id: str = ""
    teams_timeout: float = 10.0

    roster_group_id: str = ""
    roster_timeout: float = 15.0
    roster_cache_ttl: int = 120
    response_retention_days: int = 90
    remind_cooldown: int = 300

    @field_validator("ha_tts_service")
    @classmethod
    def _service_format(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", v):
            raise ValueError("HA_TTS_SERVICE doit avoir le format domaine.service")
        return v

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_url and self.ha_token.get_secret_value())

    @property
    def teams_configured(self) -> bool:
        return bool(
            self.teams_app_id
            and self.teams_app_secret.get_secret_value()
            and (self.teams_tenant_id or self.entra_tenant_id)
            and self.teams_service_url
            and self.teams_conversation_id
        )

    @property
    def ma_configured(self) -> bool:
        return bool(self.ma_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
