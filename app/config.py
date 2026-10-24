import re
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
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
    announce_voices: list[str] = [
        "Celine", "Mathieu", "Lea", "Vicki", "Hans", "Marlene", "Amy", "Brian",
        "Conchita", "Enrique",
    ]  # fmt: skip
    announce_langs: list[str] = ["fr-FR", "en-GB", "de-DE", "es-ES"]
    announce_default_voice: str | None = None
    announce_default_lang: str | None = None
    announce_wpm: int = 150

    # Volume forcé pendant la diffusion, restauré ensuite (0 = ne pas forcer)
    alert_force_volume: bool = True
    alert_volume: int = Field(default=100, ge=0, le=100)
    # Son joué avant le TTS d'une alerte (media_id HA ; vide = désactivé) et plafond d'attente
    alert_sound_media_id: str = "media-source://media_source/local/Alarme City.mp3"
    alert_sound_max_wait: float = Field(default=15.0, ge=1, le=60)
    announce_force_volume: bool = True
    announce_volume: int = Field(default=100, ge=0, le=100)
    volume_settle: float = Field(default=0.5, ge=0, le=10)
    volume_restore_margin: float = Field(default=3.0, ge=0, le=60)
    volume_restore_max_wait: float = Field(default=120.0, ge=1, le=900)
    volume_restore_retries: int = Field(default=3, ge=1, le=10)
    volume_state_ttl: int = Field(default=3600, ge=60)

    ma_url: str = ""
    ma_token: SecretStr = SecretStr("")
    ma_timeout: float = 10.0
    scene_announce_wait: float = 20.0

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
    rl_music_image_limit: int = 600
    ma_image_max_bytes: int = 2_000_000
    rl_webhook_limit: int = 3000
    rl_planning_limit: int = 60
    rl_planning_run_limit: int = 10

    teams_app_id: str = ""
    teams_app_secret: SecretStr = SecretStr("")
    teams_tenant_id: str = ""
    teams_service_url: str = ""
    teams_conversation_id: str = ""
    teams_timeout: float = 10.0

    # Liens de réponse individuels (sans bot) et diffusion par webhook (Power Automate)
    response_links: bool = False  # force le mode liens même sans webhook (liens copiés à la main)
    response_link_ttl_hours: int = Field(default=48, ge=1, le=24 * 30)
    alert_webhook_url: SecretStr = SecretStr("")  # secret : l'URL contient la signature du flow
    alert_webhook_enabled: bool = True
    alert_webhook_batch: int = Field(default=50, ge=1, le=200)
    alert_webhook_timeout: float = Field(default=15.0, ge=1, le=120)
    alert_webhook_retries: int = Field(default=3, ge=1, le=6)
    alert_webhook_card_max_bytes: int = Field(default=24_000, ge=2_000, le=28_000)
    rl_response_link_limit: int = 600  # par IP et par fenêtre : un campus partage souvent une IP

    roster_group_id: str = ""
    roster_timeout: float = 15.0
    roster_cache_ttl: int = 120
    response_retention_days: int = 90
    remind_cooldown: int = 300
    auto_remind_interval: int = Field(default=600, ge=0, le=86400)  # s ; 0 = désactivé
    auto_remind_max: int = Field(default=3, ge=0, le=50)  # 0 = désactivé
    auto_remind_lock_ttl: int = Field(default=120, ge=10)

    vapid_private_key: SecretStr = SecretStr("")
    vapid_public_key: str = ""
    vapid_subject: str = ""
    push_allowed_hosts: str = ""  # suffixes d'hôtes push supplémentaires, séparés par des virgules
    push_timeout: float = Field(default=5.0, ge=1, le=30)
    rl_push_limit: int = 30
    notify_no_response_min: int = Field(default=5, ge=0)  # 0 = déclencheur désactivé
    notify_rate_threshold: int = Field(default=50, ge=0, le=100)  # % ; 0 = désactivé
    notify_rate_after_min: int = Field(default=15, ge=0)
    notify_teams_user_ids: str = ""  # identifiants Entra (oid) séparés par des virgules

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
    def webhook_configured(self) -> bool:
        return bool(self.alert_webhook_enabled and self.alert_webhook_url.get_secret_value())

    @property
    def links_active(self) -> bool:
        """Mode liens : les répondants attendus viennent de l'annuaire `students`."""
        return self.response_links or self.webhook_configured

    @property
    def push_configured(self) -> bool:
        return bool(
            self.vapid_private_key.get_secret_value()
            and self.vapid_public_key
            and self.vapid_subject
        )

    @property
    def notify_teams_ids(self) -> list[str]:
        return [x.strip() for x in self.notify_teams_user_ids.split(",") if x.strip()]

    @property
    def ma_configured(self) -> bool:
        return bool(self.ma_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
