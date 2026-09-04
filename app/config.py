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
    # How many search pages one cycle reads. Every page is one more upstream
    # request, so this is a safety setting, not a throughput knob
    # (spec/backend/collector-guidelines.md). Default 2 rather than the 3 the
    # live probe covered: the smallest increment that widens the aperture is
    # also the smallest probe of how risk control reacts. The ceiling is hard
    # on purpose — 5 pages is 5x the request rate of M2.
    search_pages: int = Field(default=2, ge=1, le=5)
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


# Rows asked of one search page. Not a Settings field: it is the upstream page
# size we have actually verified, and nothing about a deployment changes it.
# It lives here rather than at the scheduler's call site because
# `analytics.listing_duration` has to report the observation aperture
# (pages x rows) a duration distribution was measured through, and the two
# halves of that number must not drift apart.
SEARCH_ROWS = 30
