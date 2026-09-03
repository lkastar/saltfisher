"""Application settings. All env vars use the SFD_ prefix."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SFD_", env_file=".env", extra="ignore")

    # Required. There is no default: a monitoring panel that silently starts
    # unauthenticated is worse than one that refuses to start.
    api_token: str = Field(min_length=8)

    data_dir: Path = Path("data")
    log_level: str = "INFO"

    # Anti-ban floor. Also enforced per-row via Field(ge=...) on the models.
    min_interval_seconds: int = 60

    # Collector
    browser_timeout_s: int = 30
    seller_profile_ttl_days: int = 7
    max_image_urls: int = 5

    # Notification
    price_drop_ratio: float = 0.05
    renotify_cooldown_minutes: int = 30
    max_consecutive_failures: int = 5

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"


def load_settings() -> Settings:
    """Fail fast with a readable message instead of a validation traceback."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:
        raise SystemExit(
            "startup refused: invalid configuration.\n"
            "  SFD_API_TOKEN must be set to at least 8 characters.\n"
            f"  detail: {exc}"
        ) from exc


settings = load_settings()
