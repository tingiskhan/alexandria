"""Settings conventions, and the database settings.

Each package owns the settings it uses, as a small BaseSettings class next to its code, and each entry point
builds only the classes it needs. Every class uses `settings_config` with its own prefix, so
ALEXANDRIA_DATABASE_URL maps to DatabaseSettings.url.

Values come from, in order of precedence: environment variables, a `.env` file in the working directory,
and one file per secret in /run/secrets (Kubernetes Secret volumes, Docker secrets),
e.g. /run/secrets/ALEXANDRIA_DATABASE_URL. Secrets are SecretStr, so they never show up in logs or reprs.
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = Path("/run/secrets")


def settings_config(env_prefix: str) -> SettingsConfigDict:
    """Return the shared settings config with `env_prefix`, e.g. "ALEXANDRIA_DATABASE_"."""
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=".env",
        # An empty `KEY=` line, as in .env.example, counts as unset: defaults apply and required fields fail.
        env_ignore_empty=True,
        # pydantic-settings warns when the directory is missing, which it is outside containers.
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
        extra="ignore",
    )


class DatabaseSettings(BaseSettings):
    """The Postgres connection (ALEXANDRIA_DATABASE_*)."""

    model_config = settings_config("ALEXANDRIA_DATABASE_")

    url: SecretStr  # e.g. postgresql+psycopg://alexandria:<password>@host:5432/alexandria
