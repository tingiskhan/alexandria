"""Settings for how the flows are served."""

import re
from datetime import timedelta

from pydantic import field_validator
from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config

_DURATION = re.compile(r"^(\d+)\s*([smhd]?)$")
_UNITS = {"": "seconds", "s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class WorkflowSettings(BaseSettings):
    """The sync schedule (ALEXANDRIA_SYNC_*)."""

    model_config = settings_config("ALEXANDRIA_SYNC_")

    # Run `sync` this often, e.g. "3600", "30m", "1h" or "PT1H". None means runs are only started by hand.
    interval: timedelta | None = None

    @field_validator("interval", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        """Read plain seconds and s/m/h/d units; anything else (ISO 8601, HH:MM:SS) is left to pydantic."""
        if isinstance(value, str) and (match := _DURATION.match(value.strip().lower())):
            return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
        return value

    @field_validator("interval")
    @classmethod
    def _positive(cls, value: timedelta | None) -> timedelta | None:
        """A zero or negative interval would mean a run all the time."""
        if value is not None and value <= timedelta(0):
            raise ValueError("the interval must be longer than zero")
        return value
