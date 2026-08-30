"""Process-level settings.

Anything municipality-specific belongs in a municipality YAML file, not here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://firewatch:firewatch@localhost:5433/firewatch"

    municipalities_dir: Path = PACKAGE_ROOT / "municipalities"
    firewatch_cache_dir: Path = REPO_ROOT / "data" / "cache"

    # Sent to public APIs so operators can identify our traffic. Overpass and
    # several government endpoints expect a real contact string.
    firewatch_contact: str = "firewatch@yellowducklabs.example"

    # Some municipal portals sit behind bot protection that rejects default
    # client UAs. We identify ourselves honestly but need a browser-shaped UA.
    http_user_agent: str = (
        "YellowDuckLabs-FireWatch/0.1 (municipal wildfire intelligence; +{contact})"
    )
    http_timeout_seconds: float = 120.0

    firms_map_key: str | None = None

    anthropic_api_key: str | None = None
    firewatch_llm_model: str = "claude-sonnet-4-5"

    @property
    def user_agent(self) -> str:
        return self.http_user_agent.format(contact=self.firewatch_contact)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
settings.firewatch_cache_dir.mkdir(parents=True, exist_ok=True)
