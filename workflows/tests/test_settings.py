from datetime import timedelta

import pytest
from pydantic import ValidationError

from alexandria_workflows.settings import WorkflowSettings


def test_no_interval_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_SYNC_INTERVAL", raising=False)

    assert WorkflowSettings().interval is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3600", timedelta(hours=1)),
        ("90s", timedelta(seconds=90)),
        (" 30M ", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
        ("PT1H", timedelta(hours=1)),
        ("01:30:00", timedelta(hours=1, minutes=30)),
    ],
)
def test_intervals_read_as_seconds_units_or_iso(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: timedelta
) -> None:
    monkeypatch.setenv("ALEXANDRIA_SYNC_INTERVAL", value)

    assert WorkflowSettings().interval == expected


@pytest.mark.parametrize("value", ["0", "1 week", "-5m"])
def test_unusable_intervals_fail_at_startup(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ALEXANDRIA_SYNC_INTERVAL", value)

    with pytest.raises(ValidationError, match="interval"):
        WorkflowSettings()
