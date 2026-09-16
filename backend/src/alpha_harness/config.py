"""Application settings.

Values come from (in precedence order): process environment, the repo-root ``.env``,
then the defaults below.

**No BRAIN credentials here.** They are typed into the sign-in screen and sealed in the
local vault, and nothing reads them from the environment: seeding them from a file would
let a checked-out repository sign in as its owner.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .../backend/src/alpha_harness/config.py -> .../alpha-harness
REPO_ROOT = Path(__file__).resolve().parents[3]

# Not a setting: the sign-in screen sends the password here, so no file may redirect it.
BRAIN_API_BASE = "https://api.worldquantbrain.com"


class Settings(BaseSettings):
    """Runtime configuration for the Alpha Harness backend."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="AH_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- BRAIN platform -------------------------------------------------
    # A consultant's daily simulation allowance, shown until the day's first simulation
    # POST returns its x-ratelimit-* headers.
    daily_simulation_allowance: int = 5000

    # --- Local storage --------------------------------------------------
    # Home-relative by default. Must stay on native ext4 — see CLAUDE.md.
    data_dir: Path = Path.home() / ".alpha-harness"

    # --- HTTP -----------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # The built UI, served by the backend itself.
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    # Ceiling on a single poll loop, so a stuck server-side job cannot hang a
    # request forever. Simulations are polled by the background tracker, not here.
    poll_timeout_seconds: float = 300.0
    # Attempts for retryable failures (429, 503, transport errors).
    request_attempts: int = 6

    @field_validator("data_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def sqlite_path(self) -> Path:
        """Operational state: credentials, sessions, simulation records, templates."""
        return self.data_dir / "harness.db"

    @property
    def duckdb_path(self) -> Path:
        """Analytical store: the data-field catalog."""
        return self.data_dir / "catalog.duckdb"

    @property
    def key_path(self) -> Path:
        """AES-GCM master key. Created 0600 on first run."""
        return self.data_dir / "key"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
